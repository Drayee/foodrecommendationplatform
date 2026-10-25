"""附近美食服务层：缓存、归一化、混合排序、离线降级。

一次「附近」请求的完整链路
    (WGS-84 定位) → 转 GCJ-02 → 查缓存
        ├─ 命中 → 直接排序返回（cached=True）
        └─ 未命中 → 高德周边搜索（Key 池）
              ├─ 成功 → 归一化 POI → 写缓存 → 排序 → 返回
              └─ 失败（无 Key / 配额耗尽 / 网络）→ fixture 降级（degraded=True）

排序 `smart` 的五个信号（权重见 settings.AMAP["SORT_WEIGHTS"]）
    距离衰减 · 评分 · 人均与预算匹配 · 口味偏好命中 · 近期热度
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from places import coord
from places.amap import AmapError, get_client
from places.models import PlaceCache, PlaceInteraction

logger = logging.getLogger("places.service")

# 餐饮大类（高德 POI 分类码 050000）
FOOD_TYPES = "050000"
# 快捷筛选：给前端的品类 -> 高德 typecode 前缀
TYPE_PRESETS = {
    "all": ("全部美食", FOOD_TYPES),
    "chinese": ("中餐", "050100"),
    "foreign": ("西餐/异国", "050200"),
    "fast": ("快餐", "050300"),
    "leisure": ("休闲餐饮", "050400"),
    "cafe": ("咖啡", "050500"),
    "tea": ("茶饮", "050600"),
    "dessert": ("甜品", "050900"),
}


# ---------------------------------------------------------------------------
# POI 归一化
# ---------------------------------------------------------------------------
@dataclass
class Place:
    poi_id: str
    name: str
    typecode: str = ""
    type_label: str = ""
    address: str = ""
    lng: float = 0.0
    lat: float = 0.0
    distance_m: float = 0.0
    rating: float = 0.0
    cost: float = 0.0
    tel: str = ""
    photos: list[str] = field(default_factory=list)
    business_hours: str = ""
    tag: str = ""
    city: str = ""
    district: str = ""
    adcode: str = ""
    pname: str = ""
    source: str = "amap"
    score: float = 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["distance_text"] = coord.format_distance(self.distance_m)
        return data


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", [], {}):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_photo(raw: dict) -> list[str]:
    photos = raw.get("photos") or []
    urls: list[str] = []
    if isinstance(photos, list):
        for item in photos[:3]:
            if isinstance(item, dict) and item.get("url"):
                urls.append(item["url"])
            elif isinstance(item, str) and item.startswith("http"):
                urls.append(item)
    return urls


def _type_label(raw: dict) -> str:
    """`餐饮服务;中餐厅;川菜` → `川菜`（更贴近用户认知）。"""
    type_str = str(raw.get("type") or "")
    parts = [p for p in type_str.split(";") if p]
    if not parts:
        return ""
    tail = parts[-1]
    if tail in {"餐饮服务", "餐饮相关场所"} and len(parts) > 1:
        tail = parts[-2]
    return tail


def _clean_hours(raw) -> str:
    """高德的 open_time / opentime2 偶尔重复（`09:00-22:00 09:00-22:00`），这里按段去重。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for part in text.split():
        if part not in seen:
            seen.add(part)
            out.append(part)
    return " ".join(out)


def normalize_poi(raw: dict, origin: tuple[float, float] | None = None) -> Place | None:
    """把高德原始 POI 转成平台统一结构；坐标缺失则丢弃。"""
    if not isinstance(raw, dict):
        return None
    location = str(raw.get("location") or "")
    if "," not in location:
        return None
    try:
        lng_s, lat_s = location.split(",", 1)
        lng, lat = float(lng_s), float(lat_s)
    except ValueError:
        return None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None

    biz = raw.get("biz_ext") or {}
    distance = _to_float(raw.get("distance"), 0.0)
    if not distance and origin:
        distance = coord.haversine_m(origin[0], origin[1], lng, lat)

    return Place(
        poi_id=str(raw.get("id") or ""),
        name=str(raw.get("name") or "").strip(),
        typecode=str(raw.get("typecode") or ""),
        type_label=_type_label(raw),
        address=str(raw.get("address") or ""),
        lng=lng,
        lat=lat,
        distance_m=round(distance, 1),
        rating=_to_float(biz.get("rating"), 0.0),
        cost=_to_float(biz.get("cost"), 0.0),
        tel=str(raw.get("tel") or biz.get("tel") or ""),
        photos=_first_photo(raw),
        business_hours=_clean_hours(biz.get("open_time") or biz.get("opentime2")),
        tag=str(raw.get("tag") or ""),
        city=str(raw.get("cityname") or ""),
        district=str(raw.get("adname") or ""),
        adcode=str(raw.get("adcode") or ""),
        pname=str(raw.get("pname") or ""),
    )


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def cache_key(lng: float, lat: float, radius: int, keywords: str, types: str) -> str:
    raw = f"{coord.grid_key(lng, lat, 3)}|{radius}|{(keywords or '').strip()}|{types or FOOD_TYPES}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _get_cache(key: str) -> PlaceCache | None:
    try:
        row = PlaceCache.objects.filter(cache_key=key).first()
    except Exception as exc:  # pragma: no cover - 缓存不该影响主流程
        logger.debug("读缓存失败：%s", exc)
        return None
    if row is None:
        return None
    if row.is_expired():
        return None
    try:
        row.touch()
    except Exception:  # pragma: no cover
        pass
    return row


def _store_cache(key: str, lng: float, lat: float, radius: int, keywords: str, types: str,
                 pois: list, degraded: bool = False, key_label: str = "") -> None:
    ttl = int((getattr(settings, "AMAP", {}) or {}).get("CACHE_TTL", 600))
    try:
        PlaceCache.objects.update_or_create(
            cache_key=key,
            defaults={
                "lng": lng,
                "lat": lat,
                "radius": radius,
                "keywords": keywords[:60],
                "types": types[:30],
                "payload": pois,
                "degraded": degraded,
                "poi_count": len(pois),
                "key_label": key_label[:30],
                "expires_at": timezone.now() + timedelta(seconds=ttl),
            },
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("写缓存失败：%s", exc)


# ---------------------------------------------------------------------------
# 离线降级 fixture
# ---------------------------------------------------------------------------
def _fixture_candidates() -> list[dict]:
    """读取本地 POI 样例（真实高德结构），用于无 Key / 断网 / 配额耗尽时演示。"""
    cfg = getattr(settings, "AMAP", {}) or {}
    fixture_dir = cfg.get("FIXTURE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "places", "fixtures"
    )
    pois: list[dict] = []
    if not os.path.isdir(fixture_dir):
        return pois
    for name in sorted(os.listdir(fixture_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(fixture_dir, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # pragma: no cover
            logger.warning("fixture 读取失败 %s：%s", name, exc)
            continue
        if isinstance(data, dict):
            pois.extend(data.get("pois") or [])
        elif isinstance(data, list):
            pois.extend(data)
    return pois


def _degraded_items(lng: float, lat: float, radius: int, keywords: str = "") -> list[Place]:
    items: list[Place] = []
    for raw in _fixture_candidates():
        place = normalize_poi(raw, origin=(lng, lat))
        if place is None:
            continue
        place.source = "fixture"
        if keywords and keywords not in (place.name + place.type_label + place.tag + place.address):
            continue
        items.append(place)
    items = [p for p in items if p.distance_m <= max(radius, 1000) * 1.5]
    items.sort(key=lambda p: p.distance_m)
    return items[:25]


# ---------------------------------------------------------------------------
# 排序
# ---------------------------------------------------------------------------
def _taste_affinity(place: Place, taste_terms: Iterable[str]) -> float:
    terms = [t for t in taste_terms if t]
    if not terms:
        return 0.5
    haystack = f"{place.name} {place.type_label} {place.tag} {place.address}"
    hits = sum(1 for term in terms if term in haystack)
    if hits == 0:
        return 0.25
    return min(1.0, 0.55 + 0.15 * hits)


def _heat_map(poi_ids: list[str], days: int = 30) -> dict[str, float]:
    if not poi_ids:
        return {}
    since = timezone.now() - timedelta(days=days)
    try:
        rows = (
            PlaceInteraction.objects.filter(poi_id__in=poi_ids, created_at__gte=since)
            .values("poi_id")
            .annotate(n=Count("id"))
        )
    except Exception:  # pragma: no cover
        return {}
    counts = {row["poi_id"]: float(row["n"]) for row in rows}
    if not counts:
        return {}
    top = max(counts.values()) or 1.0
    return {k: v / top for k, v in counts.items()}


def rank_places(
    items: list[Place],
    *,
    radius: int,
    sort: str = "smart",
    budget: float | None = None,
    taste_terms: Iterable[str] | None = None,
) -> list[Place]:
    if not items:
        return []
    if sort == "distance":
        return sorted(items, key=lambda p: (p.distance_m, -p.rating))
    if sort == "rating":
        return sorted(items, key=lambda p: (-p.rating, p.distance_m))
    if sort == "cost_asc":
        return sorted(items, key=lambda p: (p.cost if p.cost > 0 else 1e9, p.distance_m))
    if sort == "cost_desc":
        return sorted(items, key=lambda p: (-p.cost, p.distance_m))

    weights = (getattr(settings, "AMAP", {}) or {}).get("SORT_WEIGHTS") or {}
    w_dist = float(weights.get("distance", 0.35))
    w_rate = float(weights.get("rating", 0.25))
    w_cost = float(weights.get("cost", 0.15))
    w_taste = float(weights.get("taste", 0.15))
    w_heat = float(weights.get("heat", 0.10))
    total_w = sum([w_dist, w_rate, w_cost, w_taste, w_heat]) or 1.0

    heat = _heat_map([p.poi_id for p in items])
    terms = list(taste_terms or [])
    decay = max(radius, 500) * 0.6

    def score(place: Place) -> float:
        dist = math.exp(-place.distance_m / decay)
        rate = (place.rating - 3.0) / 2.0 if place.rating > 0 else 0.45
        rate = max(0.0, min(1.0, rate))
        if budget:
            diff = abs(place.cost - budget) if place.cost > 0 else budget * 0.6
            cost = max(0.0, 1.0 - diff / max(budget, 30.0))
        else:
            cost = 0.55 if place.cost > 0 else 0.4
        taste = _taste_affinity(place, terms)
        hot = heat.get(place.poi_id, 0.0)
        return (
            w_dist * dist + w_rate * rate + w_cost * cost + w_taste * taste + w_heat * hot
        ) / total_w

    ranked = sorted(items, key=lambda p: score(p), reverse=True)
    for place in ranked:
        place.score = round(score(place), 4)
    return ranked


# ---------------------------------------------------------------------------
# 对外：附近搜索
# ---------------------------------------------------------------------------
def nearby(
    lng: float,
    lat: float,
    *,
    radius: int | None = None,
    keywords: str = "",
    type_preset: str = "all",
    types: str | None = None,
    page: int = 1,
    sort: str = "smart",
    budget: float | None = None,
    taste_terms: Iterable[str] | None = None,
    use_cache: bool = True,
) -> dict:
    """周边美食搜索（入参坐标为 GCJ-02）。"""
    cfg = getattr(settings, "AMAP", {}) or {}
    radius = int(radius or cfg.get("DEFAULT_RADIUS", 3000))
    radius = max(100, min(radius, int(cfg.get("MAX_RADIUS", 50000))))
    page = max(1, min(int(page), 100))
    if types is None:
        types = TYPE_PRESETS.get(type_preset, ("", FOOD_TYPES))[1]
    keywords = (keywords or "").strip()[:60]

    started = time.perf_counter()
    key = cache_key(lng, lat, radius, keywords, types)
    cached = _get_cache(key) if use_cache else None

    if cached is not None:
        pois = cached.payload or []
        items = [p for p in (normalize_poi(raw, origin=(lng, lat)) for raw in pois) if p]
        if cached.degraded:
            for item in items:
                item.source = "fixture"
        else:
            for item in items:
                item.source = "amap"
        ranked = rank_places(items, radius=radius, sort=sort, budget=budget, taste_terms=taste_terms)
        return {
            "items": [p.to_dict() for p in ranked],
            "total": len(ranked),
            "center": {"lng": lng, "lat": lat},
            "radius": radius,
            "keywords": keywords,
            "types": types,
            "page": page,
            "sort": sort,
            "cached": True,
            "degraded": bool(cached.degraded),
            "provider": "fixture" if cached.degraded else "amap",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "message": "离线样例数据" if cached.degraded else "",
        }

    client = get_client()
    degraded = False
    message = ""
    payload: list = []
    key_label = ""
    provider = "amap"

    try:
        data = client.around(
            lng, lat, radius=radius, keywords=keywords, types=types, page=page, offset=25
        )
        payload = list(data.get("pois") or [])
        key_label = data.get("_key_label", "")
        if not payload:
            message = "该范围内没有搜到餐饮 POI"
    except AmapError as exc:
        degraded = True
        provider = "fixture"
        message = f"高德不可用（{exc.infocode or 'ERROR'}：{exc}），已降级为离线样例"
        logger.warning("周边搜索降级：%s", exc)
        payload = _fixture_candidates()
    except Exception as exc:  # pragma: no cover - 兜底
        degraded = True
        provider = "fixture"
        message = f"周边搜索异常（{exc}），已降级为离线样例"
        logger.exception("周边搜索异常")
        payload = _fixture_candidates()

    items = [p for p in (normalize_poi(raw, origin=(lng, lat)) for raw in payload) if p]
    if degraded:
        for item in items:
            item.source = "fixture"
        items = [p for p in items if p.distance_m <= radius * 1.5][:25]

    _store_cache(key, lng, lat, radius, keywords, types, payload, degraded=degraded, key_label=key_label)

    ranked = rank_places(items, radius=radius, sort=sort, budget=budget, taste_terms=taste_terms)
    return {
        "items": [p.to_dict() for p in ranked],
        "total": len(ranked),
        "center": {"lng": lng, "lat": lat},
        "radius": radius,
        "keywords": keywords,
        "types": types,
        "page": page,
        "sort": sort,
        "cached": False,
        "degraded": degraded,
        "provider": provider,
        "key_label": key_label,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "message": message,
    }


# ---------------------------------------------------------------------------
# 对外：逆地理 / IP 定位 / 输入提示 / 详情
# ---------------------------------------------------------------------------
def regeo(lng: float, lat: float) -> dict:
    """逆地理编码：返回省市区、商圈与最近 POI。"""
    key = f"regeo:{coord.grid_key(lng, lat, 3)}"
    cached = _get_cache(key)
    if cached is not None:
        return {"address": cached.payload, "cached": True, "degraded": cached.degraded}
    try:
        data = get_client().regeo(lng, lat)
        comp = ((data.get("regeocode") or {}).get("addressComponent")) or {}
        address = {
            "formatted": (data.get("regeocode") or {}).get("formatted_address", ""),
            "province": comp.get("province", "") or "",
            "city": comp.get("city", "") or "",
            "district": comp.get("district", "") or "",
            "township": comp.get("township", "") or "",
            "adcode": comp.get("adcode", "") or "",
            "citycode": comp.get("citycode", "") or "",
            "business_areas": [b.get("name") for b in (comp.get("businessAreas") or []) if b.get("name")],
            "nearest": (comp.get("nearestPOI") or {}).get("name", "") if isinstance(comp.get("nearestPOI"), dict) else "",
        }
        _store_cache(key, lng, lat, 1000, "regeo", "regeo", address)
        return {"address": address, "cached": False, "degraded": False}
    except AmapError as exc:
        logger.warning("逆地理降级：%s", exc)
        return {
            "address": {"formatted": "", "province": "", "city": "", "district": "", "adcode": ""},
            "cached": False,
            "degraded": True,
            "message": f"逆地理不可用：{exc.infocode}",
        }


def locate_by_ip(ip: str | None = None) -> dict:
    """IP 定位（城市级），GPS 被拒时的兜底。"""
    try:
        data = get_client().ip(ip)
        rect = str(data.get("rectangle") or "")
        center = {"lng": 0.0, "lat": 0.0}
        if rect and ";" in rect:
            p1, _, p2 = rect.partition(";")
            try:
                lng1, lat1 = [float(x) for x in p1.split(",")]
                lng2, lat2 = [float(x) for x in p2.split(",")]
                center = {"lng": round((lng1 + lng2) / 2, 6), "lat": round((lat1 + lat2) / 2, 6)}
            except ValueError:
                pass
        return {
            "province": data.get("province", "") or "",
            "city": data.get("city", "") or "",
            "adcode": data.get("adcode", "") or "",
            "center": center,
            "degraded": False,
        }
    except AmapError as exc:
        return {"province": "", "city": "", "adcode": "", "center": {"lng": 0.0, "lat": 0.0},
                "degraded": True, "message": f"IP 定位不可用：{exc.infocode}"}


def tips(keywords: str, city: str = "", lng: float | None = None, lat: float | None = None) -> dict:
    """输入提示：搜索框联想。"""
    try:
        data = get_client().inputtips(keywords, city=city, lng=lng, lat=lat)
        tips_raw = data.get("tips") or []
        seen, out = set(), []
        for item in tips_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(
                {
                    "name": name,
                    "district": item.get("district", "") or "",
                    "address": item.get("address", "") or "",
                    "location": item.get("location", "") or "",
                    "typecode": item.get("typecode", "") or "",
                }
            )
            if len(out) >= 10:
                break
        return {"items": out, "degraded": False}
    except AmapError as exc:
        return {"items": [], "degraded": True, "message": f"联想不可用：{exc.infocode}"}


def place_detail(poi_id: str) -> dict:
    """POI 详情（营业时间/电话/评分）。"""
    try:
        data = get_client().detail(poi_id)
        pois = data.get("pois") or []
        if not pois:
            return {"place": None, "degraded": False}
        place = normalize_poi(pois[0])
        return {"place": place.to_dict() if place else None, "degraded": False}
    except AmapError as exc:
        return {"place": None, "degraded": True, "message": f"详情不可用：{exc.infocode}"}
