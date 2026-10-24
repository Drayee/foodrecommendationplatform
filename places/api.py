"""places REST API（/api/v1/places/...）。

统一响应结构沿用平台约定：`{"code": 0, "msg": "ok", "data": ...}`。

坐标约定
- 前端定位得到 WGS-84，默认 `coord=wgs84`，服务端转 GCJ-02 再查高德；
- 响应里的 `center` 一律是 **GCJ-02**（与地图、POI 同系），`center_wgs84` 便于调试。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from functools import wraps

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Avg, Count
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from places import coord, keypool, service
from places.models import PlaceFavorite, PlaceInteraction, PlaceReview

logger = logging.getLogger("places.api")


def _ok(data=None, msg: str = "ok", status: int = 200) -> JsonResponse:
    return JsonResponse({"code": 0, "msg": msg, "data": data}, status=status, json_dumps_params={"ensure_ascii": False})


def _err(msg: str, status: int = 400, code: int = 1) -> JsonResponse:
    return JsonResponse({"code": code, "msg": msg, "data": None}, status=status, json_dumps_params={"ensure_ascii": False})


def _body(request) -> dict:
    try:
        return json.loads(request.body or "{}") or {}
    except json.JSONDecodeError:
        return {}


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default: float | None = None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _err("请先登录", status=401, code=401)
        return view(request, *args, **kwargs)

    return wrapper


def _session_key(request) -> str:
    if not request.session.session_key:
        request.session.save()
    return request.session.session_key or "anonymous"


def _lnglat(request) -> tuple[float, float] | None:
    """解析 lng/lat 并统一转成 GCJ-02；非法坐标返回 None。"""
    lng = _float(request.GET.get("lng"))
    lat = _float(request.GET.get("lat"))
    if lng is None or lat is None:
        return None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None
    if (request.GET.get("coord") or "wgs84").lower() == "wgs84":
        return coord.wgs84_to_gcj02(lng, lat)
    return round(lng, 6), round(lat, 6)


def _track(request, poi_id: str, name: str, action: str, weight: float = 1.0, **extra) -> None:
    try:
        PlaceInteraction.objects.create(
            user=request.user if request.user.is_authenticated else None,
            session_key=_session_key(request),
            poi_id=poi_id[:40],
            place_name=(name or "")[:120],
            action=action,
            weight=weight,
            extra=extra or {},
        )
    except Exception as exc:  # pragma: no cover - 埋点不影响主流程
        logger.debug("POI 埋点失败：%s", exc)


def _taste_terms(user) -> list[str]:
    """把用户口味画像转成 POI 文本匹配词（菜系 + 标签 + 忌口反向）。"""
    if not user or not getattr(user, "is_authenticated", False):
        return []
    try:
        profile = getattr(user, "profile", None)
        if profile is None:
            return []
        terms = [c.name for c in profile.preferred_cuisines.all()][:5]
        terms += [t.name for t in profile.preferred_tags.all()][:5]
        return [t for t in terms if t]
    except Exception:  # pragma: no cover
        return []


# ---------------------------------------------------------------------------
# 配置 / Key 池
# ---------------------------------------------------------------------------
@require_GET
def config(request):
    """前端启动配置：JS Key（池）、默认半径、品类、当前 Key 池健康度。"""
    cfg = getattr(settings, "AMAP", {}) or {}
    js_pool = keypool.get_pool("js")
    web_pool = keypool.get_pool("amap")
    return _ok(
        {
            "js_key": js_pool.keys[0] if js_pool.keys else "",
            "js_keys": js_pool.keys,
            "security_code": cfg.get("SECURITY_CODE", ""),
            "enabled": bool(cfg.get("ENABLED", True)) and web_pool.has_keys,
            "default_radius": int(cfg.get("DEFAULT_RADIUS", 3000)),
            "max_radius": int(cfg.get("MAX_RADIUS", 50000)),
            "types": [{"key": k, "label": v[0], "code": v[1]} for k, v in service.TYPE_PRESETS.items()],
            "radius_options": [500, 1000, 2000, 3000, 5000, 10000],
            "sorts": [
                {"key": "smart", "label": "综合推荐"},
                {"key": "distance", "label": "距离最近"},
                {"key": "rating", "label": "评分最高"},
                {"key": "cost_asc", "label": "人均从低到高"},
                {"key": "cost_desc", "label": "人均从高到低"},
            ],
            "key_pool": {
                "web_total": len(web_pool),
                "web_available": web_pool.available(),
                "js_total": len(js_pool),
            },
        }
    )


@require_GET
def key_status(request):
    """Key 池健康度（脱敏）。staff 可见全部；普通用户只看汇总。"""
    stats = keypool.pool_stats()
    if request.user.is_authenticated and request.user.is_staff:
        return _ok(stats)
    return _ok(
        {
            "web": {"total": stats["web"]["total"], "available": stats["web"]["available"]},
            "js": {"total": stats["js"]["total"], "available": stats["js"]["available"]},
        }
    )


@csrf_exempt
@require_POST
def key_reset(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return _err("需要管理员权限", status=403, code=403)
    keypool.get_pool("amap").reset()
    keypool.get_pool("js").reset()
    return _ok(keypool.pool_stats(), "Key 冷却已重置")


# ---------------------------------------------------------------------------
# 附近搜索
# ---------------------------------------------------------------------------
@require_GET
def nearby(request):
    center = _lnglat(request)
    if center is None:
        return _err("缺少或非法的 lng/lat")
    lng, lat = center
    radius = _int(request.GET.get("radius"), 0) or None
    sort = (request.GET.get("sort") or "smart").strip()
    budget = _float(request.GET.get("budget"))
    keywords = (request.GET.get("q") or request.GET.get("keywords") or "").strip()
    preset = (request.GET.get("type") or "all").strip()
    types = request.GET.get("types") or None
    page = _int(request.GET.get("page"), 1)

    if keywords:
        _track(request, "", keywords, PlaceInteraction.Action.SEARCH, 1.0, radius=radius)

    result = service.nearby(
        lng, lat,
        radius=radius,
        keywords=keywords,
        type_preset=preset,
        types=types,
        page=page,
        sort=sort,
        budget=budget,
        taste_terms=_taste_terms(request.user),
    )
    result["center_wgs84"] = {"lng": coord.gcj02_to_wgs84(lng, lat)[0], "lat": coord.gcj02_to_wgs84(lng, lat)[1]}
    result["key_pool"] = {
        "available": keypool.get_pool("amap").available(),
        "total": len(keypool.get_pool("amap")),
    }
    return _ok(result)


@require_GET
def regeo(request):
    center = _lnglat(request)
    if center is None:
        return _err("缺少或非法的 lng/lat")
    lng, lat = center
    return _ok(service.regeo(lng, lat))


@require_GET
def locate(request):
    """IP 定位兜底（GPS 被拒绝时）。"""
    ip = request.GET.get("ip") or None
    if not ip:
        forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
        ip = forwarded or request.META.get("REMOTE_ADDR") or None
    if ip in {"127.0.0.1", "::1"}:
        ip = None  # 本机 IP 无意义，交给高德按请求来源判断
    return _ok(service.locate_by_ip(ip))


@require_GET
def tips(request):
    keywords = (request.GET.get("q") or request.GET.get("keywords") or "").strip()
    if not keywords:
        return _err("请输入关键词")
    city = (request.GET.get("city") or "").strip()
    lng = _float(request.GET.get("lng"))
    lat = _float(request.GET.get("lat"))
    return _ok(service.tips(keywords, city=city, lng=lng, lat=lat))


@require_GET
def detail(request, poi_id: str):
    result = service.place_detail(poi_id)
    reviews = (
        PlaceReview.objects.filter(poi_id=poi_id, is_hidden=False)
        .select_related("user")
        .order_by("-created_at")[:20]
    )
    agg = PlaceReview.objects.filter(poi_id=poi_id, is_hidden=False).aggregate(
        n=Count("id"), avg=Avg("rating")
    )
    result["reviews"] = [r.to_dict() for r in reviews]
    result["review_stats"] = {
        "count": agg["n"] or 0,
        "avg_rating": round(agg["avg"] or 0, 2),
        "favorited": PlaceFavorite.objects.filter(poi_id=poi_id).count(),
    }
    if request.user.is_authenticated:
        result["is_favorite"] = PlaceFavorite.objects.filter(user=request.user, poi_id=poi_id).exists()
    _track(request, poi_id, (result.get("place") or {}).get("name", ""), PlaceInteraction.Action.VIEW, 1.0)
    return _ok(result)


# ---------------------------------------------------------------------------
# 收藏
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
@login_required
def favorite(request, poi_id: str):
    data = _body(request)
    existing = PlaceFavorite.objects.filter(user=request.user, poi_id=poi_id).first()
    if existing:
        existing.delete()
        _track(request, poi_id, existing.name, PlaceInteraction.Action.FAVORITE, -1.0, removed=True)
        return _ok({"is_favorite": False, "count": PlaceFavorite.objects.filter(poi_id=poi_id).count()}, "已取消收藏")

    snapshot = data.get("place") or {}
    if not snapshot.get("name"):
        # 没带快照就现查缓存/详情，避免存一条空店
        cached = service.place_detail(poi_id).get("place") or {}
        snapshot = cached or {}
    if not snapshot.get("name"):
        return _err("缺少店铺信息，请从列表重新操作")

    fav = PlaceFavorite.objects.create(
        user=request.user,
        poi_id=poi_id[:40],
        name=str(snapshot.get("name", ""))[:120],
        address=str(snapshot.get("address", ""))[:255],
        lng=_float(snapshot.get("lng"), 0) or 0,
        lat=_float(snapshot.get("lat"), 0) or 0,
        tel=str(snapshot.get("tel", ""))[:60],
        typecode=str(snapshot.get("typecode", ""))[:20],
        type_label=str(snapshot.get("type_label", ""))[:60],
        rating=_float(snapshot.get("rating"), 0) or 0,
        cost=_float(snapshot.get("cost"), 0) or 0,
        note=str(data.get("note", ""))[:200],
    )
    _track(request, poi_id, fav.name, PlaceInteraction.Action.FAVORITE, 3.0)
    return _ok(
        {"is_favorite": True, "count": PlaceFavorite.objects.filter(poi_id=poi_id).count(), "item": fav.to_dict()},
        "已收藏",
    )


@require_GET
@login_required
def my_favorites(request):
    rows = PlaceFavorite.objects.filter(user=request.user).order_by("-created_at")[:100]
    return _ok([r.to_dict() for r in rows])


# ---------------------------------------------------------------------------
# 打卡评价
# ---------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["GET", "POST"])
def reviews(request, poi_id: str):
    if request.method == "GET":
        rows = (
            PlaceReview.objects.filter(poi_id=poi_id, is_hidden=False)
            .select_related("user")
            .order_by("-created_at")[:50]
        )
        agg = PlaceReview.objects.filter(poi_id=poi_id, is_hidden=False).aggregate(
            n=Count("id"), avg=Avg("rating")
        )
        return _ok(
            {
                "items": [r.to_dict() for r in rows],
                "count": agg["n"] or 0,
                "avg_rating": round(agg["avg"] or 0, 2),
            }
        )

    if not request.user.is_authenticated:
        return _err("请先登录", status=401, code=401)
    data = _body(request)
    rating = _int(data.get("rating"), 0)
    if not 1 <= rating <= 5:
        return _err("评分需在 1~5 之间")
    content = str(data.get("content") or "").strip()
    if len(content) > 1000:
        return _err("评价过长（最多 1000 字）")
    name = str(data.get("place_name") or data.get("name") or "")[:120]
    if not name:
        cached = service.place_detail(poi_id).get("place") or {}
        name = str(cached.get("name") or "这家店")[:120]

    visited = None
    raw_date = data.get("visited_at")
    if raw_date:
        try:
            visited = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            visited = None

    review = PlaceReview.objects.create(
        user=request.user,
        poi_id=poi_id[:40],
        place_name=name,
        rating=rating,
        content=content,
        photos=[str(p)[:300] for p in (data.get("photos") or [])][:9],
        dishes=[str(d)[:40] for d in (data.get("dishes") or [])][:10],
        cost=_float(data.get("cost"), 0) or 0,
        visited_at=visited,
    )
    _track(request, poi_id, name, PlaceInteraction.Action.CHECKIN, 4.0, rating=rating)
    return _ok(review.to_dict(), "打卡成功")


@csrf_exempt
@require_POST
@login_required
def checkin(request, poi_id: str):
    """到店打卡（不写评价，只记录到访）。"""
    data = _body(request)
    name = str(data.get("place_name") or data.get("name") or "")[:120]
    PlaceReview.objects.create(
        user=request.user,
        poi_id=poi_id[:40],
        place_name=name or "这家店",
        rating=_int(data.get("rating"), 5) or 5,
        content="",
        cost=_float(data.get("cost"), 0) or 0,
    )
    _track(request, poi_id, name, PlaceInteraction.Action.CHECKIN, 2.5)
    return _ok({"checked_in": True}, "打卡已记录")


@csrf_exempt
@require_POST
def track(request):
    """前端埋点：view / route / click。"""
    data = _body(request)
    action = str(data.get("action") or "").strip()
    if action not in {"view", "route", "click"}:
        return _err("不支持的行为类型")
    _track(
        request,
        str(data.get("poi_id") or "")[:40],
        str(data.get("name") or "")[:120],
        action,
        2.0 if action == "route" else 1.0,
    )
    return _ok(None, "已记录")
