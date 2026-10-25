"""REST API 实现。

约定
- 所有响应 ``{"code": 0, "msg": "ok", "data": ...}``，HTTP 状态码同步语义；
- 读接口允许匿名，写接口要求登录；
- 外部能力（推荐引擎 / 网络搜索 / RAG 索引）全部延迟导入并降级，
  任一子系统未就绪只影响该功能，不会让整个接口 500。
"""
from __future__ import annotations

import json
import logging
import time
from functools import wraps

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, Q
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from food.models import (
    Comment,
    Cuisine,
    Favorite,
    InteractionLog,
    Rating,
    Recipe,
    SearchLog,
    Tag,
    UserProfile,
)
from food.services import assistant
from food.services import auth as auth_service

logger = logging.getLogger("food.api")


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
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
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def api_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return _err("请先登录", status=401, code=401)
        return view(request, *args, **kwargs)

    return wrapper


def _profile(user) -> UserProfile:
    profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"nickname": user.username})
    return profile


def _session_key(request) -> str:
    if not request.session.session_key:
        request.session.save()
    return request.session.session_key or "anonymous"


def _track(request, action: str, recipe: Recipe | None = None, weight: float = 1.0, **extra):
    try:
        InteractionLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            session_key=_session_key(request),
            recipe=recipe,
            action=action,
            weight=weight,
            extra=extra or {},
        )
    except Exception as exc:  # pragma: no cover - 埋点绝不能影响主流程
        logger.debug("埋点失败：%s", exc)


# ---------------------------------------------------------------------------
# 元数据 / 健康检查
# ---------------------------------------------------------------------------
@require_GET
def meta(request):
    data = {
        "cuisines": list(Cuisine.objects.values("id", "name", "icon", "slug", "region")),
        "tags": list(Tag.objects.values("id", "name", "kind")),
        "difficulties": [{"value": v, "label": l} for v, l in Recipe.Difficulty.choices],
        "scenes": ["早餐", "午餐", "晚餐", "夜宵", "减脂", "下饭", "宴客", "一人食"],
        "sorts": ["recommend", "hot", "new", "rating", "quick"],
        "stats": {
            "recipes": Recipe.objects.published().count(),
            "cuisines": Cuisine.objects.count(),
            "ratings": Rating.objects.count(),
            "users": User.objects.count(),
        },
        "llm": assistant.llm_status(),
        "index": assistant.index_status(),
    }
    return _ok(data)


@require_GET
def health(request):
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pragma: no cover
        db_ok = False
        logger.warning("数据库检查失败：%s", exc)
    payload = {
        "database": db_ok,
        "llm": assistant.llm_status(),
        "index": assistant.index_status(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _ok(payload, status=200 if db_ok else 503)


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def send_code(request):
    return auth_service.send_code(_body(request).get("email"))


@csrf_exempt
@require_POST
def login_with_code(request):
    data = _body(request)
    return auth_service.login_code(request, data.get("email"), data.get("code"))


@csrf_exempt
@require_POST
def logout(request):
    django_logout(request)
    return _ok(None, "已退出登录")


def _me_payload(request) -> dict:
    """当前用户的完整档案（`/auth/me/` 与保存偏好后的回显共用）。"""
    user = request.user
    profile = _profile(user)
    return {
        "authenticated": True,
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "nickname": profile.display_name,
        "avatar": profile.avatar,
        "onboarded": profile.onboarded,
        "spicy_level": profile.spicy_level,
        "max_cook_minutes": profile.max_cook_minutes,
        "preferred_cuisines": list(profile.preferred_cuisines.values("id", "name")),
        "preferred_tags": list(profile.preferred_tags.values("id", "name")),
        "disliked_ingredients": profile.disliked_ingredients,
        "diet_flags": profile.diet_flags,
        "counts": {
            "favorites": Favorite.objects.filter(user=user).count(),
            "ratings": Rating.objects.filter(user=user).count(),
        },
    }


@require_GET
def me(request):
    if not request.user.is_authenticated:
        return _ok({"authenticated": False})
    return _ok(_me_payload(request))


# ---------------------------------------------------------------------------
# 菜谱
# ---------------------------------------------------------------------------
def _apply_sort(qs, sort: str):
    if sort == "hot":
        return qs.order_by("-hot_score", "-view_count")
    if sort == "new":
        return qs.order_by("-created_at")
    if sort == "rating":
        return qs.order_by("-rating_avg", "-rating_count")
    if sort == "quick":
        return qs.order_by("prep_minutes", "cook_minutes")
    return qs.order_by("-hot_score", "-rating_avg")


@require_GET
def recipe_list(request):
    params = request.GET
    qs = Recipe.objects.published().with_relations()
    keyword = (params.get("q") or "").strip()
    if keyword:
        qs = qs.search(keyword)
    if params.get("cuisine"):
        qs = qs.filter(Q(cuisine__slug=params["cuisine"]) | Q(cuisine__name=params["cuisine"]))
    if params.get("tag"):
        qs = qs.filter(tags__name=params["tag"])
    if params.get("difficulty"):
        qs = qs.filter(difficulty=_int(params["difficulty"], 2))
    if params.get("max_minutes"):
        qs = qs.filter(prep_minutes__lte=_int(params["max_minutes"], 999))
    if params.get("diet"):
        qs = qs.filter(tags__name__in=[params["diet"]])
    qs = _apply_sort(qs.distinct(), params.get("sort", "recommend"))

    size = min(max(_int(params.get("size"), 12), 1), 60)
    page = Paginator(qs, size).get_page(_int(params.get("page"), 1))
    return _ok(
        {
            "items": [r.to_card() for r in page.object_list],
            "total": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "has_next": page.has_next(),
        }
    )


@require_GET
def recipe_detail(request, pk: int):
    recipe = Recipe.objects.published().with_relations().filter(pk=pk).first()
    if recipe is None:
        return _err("菜谱不存在", status=404, code=404)
    Recipe.objects.filter(pk=pk).update(view_count=recipe.view_count + 1)
    _track(request, "view", recipe, weight=1.0)

    data = recipe.to_detail()
    data["view_count"] = recipe.view_count + 1
    if request.user.is_authenticated:
        data["my_rating"] = (
            Rating.objects.filter(user=request.user, recipe=recipe).values_list("score", flat=True).first()
        )
        data["is_favorite"] = Favorite.objects.filter(user=request.user, recipe=recipe).exists()
    else:
        data["my_rating"] = None
        data["is_favorite"] = False
    data["comments"] = [
        {
            "id": c.id,
            "user": c.user.username,
            "content": c.content,
            "likes": c.likes,
            "created_at": c.created_at.isoformat(),
        }
        for c in recipe.comments.filter(is_hidden=False, parent__isnull=True).select_related("user")[:10]
    ]
    return _ok(data)


@csrf_exempt
@require_POST
@api_login_required
def recipe_rate(request, pk: int):
    score = _int(_body(request).get("score"), 0)
    if score < 1 or score > 5:
        return _err("评分需在 1~5 之间")
    recipe = Recipe.objects.filter(pk=pk, status=Recipe.Status.PUBLISHED).first()
    if recipe is None:
        return _err("菜谱不存在", status=404, code=404)
    Rating.objects.update_or_create(user=request.user, recipe=recipe, defaults={"score": score})
    recipe.recompute_rating()
    _track(request, "rate", recipe, weight=score)
    return _ok({"rating_avg": recipe.rating_avg, "rating_count": recipe.rating_count}, "评分成功")


@csrf_exempt
@require_POST
@api_login_required
def recipe_favorite(request, pk: int):
    recipe = Recipe.objects.filter(pk=pk, status=Recipe.Status.PUBLISHED).first()
    if recipe is None:
        return _err("菜谱不存在", status=404, code=404)
    fav, created = Favorite.objects.get_or_create(user=request.user, recipe=recipe)
    if not created:
        fav.delete()
    count = Favorite.objects.filter(recipe=recipe).count()
    Recipe.objects.filter(pk=pk).update(favorite_count=count)
    if created:
        _track(request, "favorite", recipe, weight=3.0)
    return _ok({"is_favorite": created, "favorite_count": count}, "已收藏" if created else "已取消收藏")


@csrf_exempt
@require_http_methods(["GET", "POST"])
def recipe_comments(request, pk: int):
    recipe = Recipe.objects.filter(pk=pk).first()
    if recipe is None:
        return _err("菜谱不存在", status=404, code=404)
    if request.method == "GET":
        items = (
            recipe.comments.filter(is_hidden=False)
            .select_related("user")
            .order_by("-created_at")[:50]
        )
        return _ok(
            [
                {
                    "id": c.id,
                    "user": c.user.username,
                    "parent_id": c.parent_id,
                    "content": c.content,
                    "likes": c.likes,
                    "created_at": c.created_at.isoformat(),
                }
                for c in items
            ]
        )

    if not request.user.is_authenticated:
        return _err("请先登录", status=401, code=401)
    data = _body(request)
    content = (data.get("content") or "").strip()
    if not content:
        return _err("评论内容不能为空")
    if len(content) > 1000:
        return _err("评论过长（最多 1000 字）")
    comment = Comment.objects.create(
        user=request.user,
        recipe=recipe,
        content=content,
        parent_id=_int(data.get("parent_id"), 0) or None,
    )
    _track(request, "comment", recipe, weight=2.0)
    return _ok(
        {
            "id": comment.id,
            "user": comment.user.username,
            "content": comment.content,
            "created_at": comment.created_at.isoformat(),
        },
        "评论成功",
    )


@require_GET
def recipe_similar(request, pk: int):
    recipe = Recipe.objects.filter(pk=pk).first()
    if recipe is None:
        return _err("菜谱不存在", status=404, code=404)
    try:
        from recommend.content import similar_recipes

        pairs = similar_recipes(recipe, k=min(max(_int(request.GET.get("k"), 6), 1), 20))
        items = [{"score": round(score, 4), **obj.to_card(), "reason": "口味/食材相近"} for obj, score in pairs]
    except Exception as exc:  # pragma: no cover - 推荐引擎未就绪时兜底
        logger.warning("相似菜谱降级：%s", exc)
        qs = (
            Recipe.objects.published()
            .exclude(pk=pk)
            .filter(Q(cuisine=recipe.cuisine) | Q(tags__in=recipe.tags.all()))
            .distinct()
            .order_by("-hot_score")[:6]
        )
        items = [{"score": 0.0, **r.to_card(), "reason": "同菜系/同标签"} for r in qs]
    return _ok(items)


# ---------------------------------------------------------------------------
# 检索 / 推荐
# ---------------------------------------------------------------------------
@require_GET
def search(request):
    started = time.perf_counter()
    keyword = (request.GET.get("q") or "").strip()
    if not keyword:
        return _err("请输入搜索关键词")
    qs = Recipe.objects.published().with_relations().search(keyword)
    qs = _apply_sort(qs.distinct(), request.GET.get("sort", "recommend"))
    size = min(max(_int(request.GET.get("size"), 12), 1), 40)
    page = Paginator(qs, size).get_page(_int(request.GET.get("page"), 1))
    latency = (time.perf_counter() - started) * 1000
    SearchLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        query=keyword[:300],
        mode=SearchLog.Mode.KEYWORD,
        result_count=page.paginator.count,
        latency_ms=round(latency, 1),
    )
    return _ok(
        {
            "items": [r.to_card() for r in page.object_list],
            "total": page.paginator.count,
            "latency_ms": round(latency, 1),
            "mode": "keyword",
        }
    )


@require_GET
def semantic_search(request):
    started = time.perf_counter()
    query = (request.GET.get("q") or "").strip()
    if not query:
        return _err("请输入搜索内容")
    k = min(max(_int(request.GET.get("k"), 8), 1), 30)
    hits = assistant.retrieve(query, k=k)
    top = max([float(getattr(h, "score", 0.0) or 0.0) for h in hits] + [1e-9])
    recipes, seen = [], set()
    for hit in hits:
        payload = getattr(hit, "payload", {}) or {}
        rid = getattr(hit, "recipe_id", None) or payload.get("recipe_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        obj = Recipe.objects.published().with_relations().filter(pk=rid).first()
        if obj:
            raw = float(getattr(hit, "score", 0.0) or 0.0)
            recipes.append(
                {
                    "score": round(raw / top, 4),
                    "raw_score": round(raw, 5),
                    "snippet": getattr(hit, "text", "")[:200],
                    **obj.to_card(),
                }
            )
    latency = (time.perf_counter() - started) * 1000
    SearchLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        query=query[:300],
        mode=SearchLog.Mode.SEMANTIC,
        result_count=len(recipes),
        latency_ms=round(latency, 1),
    )
    return _ok(
        {
            "items": recipes,
            "total": len(recipes),
            "latency_ms": round(latency, 1),
            "mode": "semantic",
            "index": assistant.index_status(),
        }
    )


@require_GET
def feed(request):
    user = request.user if request.user.is_authenticated else None
    k = min(max(_int(request.GET.get("k"), 0), 0), 30) or int((getattr(settings, "RECOMMEND", {}) or {}).get("FEED_SIZE", 12))
    scene = request.GET.get("scene") or None
    try:
        from recommend.hybrid import recommend_for_user

        results = recommend_for_user(user, k=k, scene=scene)
        items = [
            {
                "score": round(r.score, 4),
                "reason": r.reason,
                **r.recipe.to_card(),
            }
            for r in results
        ]
        mode = "personalized" if user else "cold_start"
    except Exception as exc:  # pragma: no cover - 推荐引擎未就绪时按热度兜底
        logger.warning("推荐降级为热度排序：%s", exc)
        items = [{"score": round(r.hot_score, 3), "reason": "热门推荐", **r.to_card()} for r in Recipe.objects.hot(k)]
        mode = "fallback_hot"
    return _ok({"items": items, "mode": mode, "scene": scene or ""})


# ---------------------------------------------------------------------------
# AI 助手
# ---------------------------------------------------------------------------
def _sse_stream(stream: assistant.AnswerStream):
    """把 AnswerStream 包装为 SSE：文本分片 + 引用 + 结束事件。"""
    try:
        yield f"data: {json.dumps({'type': 'meta', 'degraded': not assistant.llm_ready()}, ensure_ascii=False)}\n\n"
        for chunk in stream:
            yield f"data: {json.dumps({'type': 'token', 'text': chunk}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'citations', 'items': stream.citations}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'latency_ms': round(stream.latency_ms, 1), 'tool_calls': len(stream.tool_calls)}, ensure_ascii=False)}\n\n"
    except Exception as exc:  # pragma: no cover
        logger.exception("SSE 流式输出失败")
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@csrf_exempt
@require_POST
def assistant_chat(request):
    data = _body(request)
    question = (data.get("question") or "").strip()
    if not question:
        return _err("问题不能为空")
    session_key = (data.get("session_id") or "").strip() or _session_key(request)
    location = {
        "lng": _float(data.get("lng"), 0.0),
        "lat": _float(data.get("lat"), 0.0),
    }
    stream = assistant.stream_answer(
        question=question,
        session_key=session_key,
        user=request.user if request.user.is_authenticated else None,
        use_tools=bool(data.get("use_tools", True)),
        location=location,
    )
    response = StreamingHttpResponse(_sse_stream(stream), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@csrf_exempt
@require_POST
def assistant_chat_sync(request):
    data = _body(request)
    question = (data.get("question") or "").strip()
    if not question:
        return _err("问题不能为空")
    session_key = (data.get("session_id") or "").strip() or _session_key(request)
    location = {"lng": _float(data.get("lng"), 0.0), "lat": _float(data.get("lat"), 0.0)}
    return _ok(
        assistant.answer_once(
            question,
            session_key=session_key,
            user=request.user if request.user.is_authenticated else None,
            use_tools=bool(data.get("use_tools", False)),
            location=location,
        )
    )


@require_GET
def assistant_history(request):
    from food.models import ChatSession

    session_key = (request.GET.get("session_id") or "").strip() or _session_key(request)
    session = ChatSession.objects.filter(session_key=session_key).first()
    if not session:
        return _ok({"session_id": session_key, "messages": []})
    messages = [
        {
            "role": m.role,
            "content": m.content,
            "citations": m.citations,
            "created_at": m.created_at.isoformat(),
        }
        for m in session.messages.order_by("created_at")[:100]
    ]
    return _ok({"session_id": session_key, "title": session.title, "messages": messages})


@require_GET
def assistant_debug(request):
    question = (request.GET.get("q") or "").strip()
    if not question:
        return _err("请输入调试问题")
    return _ok(assistant.debug_retrieval(question, k=min(max(_int(request.GET.get("k"), 5), 1), 20)))


# ---------------------------------------------------------------------------
# 网络搜索
# ---------------------------------------------------------------------------
@require_GET
def websearch(request):
    query = (request.GET.get("q") or "").strip()
    if not query:
        return _err("请输入搜索关键词")
    limit = min(max(_int(request.GET.get("limit"), 5), 1), 10)
    try:
        from websearch.service import search_recipes_online

        result = search_recipes_online(query, limit=limit)
    except Exception as exc:  # pragma: no cover
        logger.warning("网络搜索失败：%s", exc)
        return _err(f"网络搜索暂不可用：{exc}", status=503, code=503)
    SearchLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        query=query[:300],
        mode=SearchLog.Mode.WEB,
        result_count=len(result.get("results", [])),
        latency_ms=_float(result.get("latency_ms"), 0),
        from_cache=bool(result.get("cached")),
    )
    return _ok(result)


@csrf_exempt
@require_POST
def websearch_import(request):
    url = (_body(request).get("url") or "").strip()
    if not url.startswith("http"):
        return _err("请提供合法的菜谱链接")
    try:
        from websearch.ingest import import_recipe

        result = import_recipe(url, user=request.user if request.user.is_authenticated else None)
    except Exception as exc:  # pragma: no cover
        logger.exception("菜谱导入失败")
        return _err(f"导入失败：{exc}", status=502, code=502)
    return _ok(result, "导入成功")


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
@csrf_exempt
@require_http_methods(["GET", "PATCH", "POST"])
def profile(request):
    if not request.user.is_authenticated:
        return _err("请先登录", status=401, code=401)
    profile_obj = _profile(request.user)
    if request.method == "GET":
        return _ok(_me_payload(request))

    data = _body(request)
    if "nickname" in data:
        profile_obj.nickname = (data.get("nickname") or "")[:40]
    if "bio" in data:
        profile_obj.bio = (data.get("bio") or "")[:200]
    if "spicy_level" in data:
        profile_obj.spicy_level = max(0, min(_int(data["spicy_level"], 2), 5))
    if "max_cook_minutes" in data:
        profile_obj.max_cook_minutes = max(5, min(_int(data["max_cook_minutes"], 60), 600))
    if "diet_flags" in data and isinstance(data["diet_flags"], list):
        profile_obj.diet_flags = [str(x)[:20] for x in data["diet_flags"]][:10]
    if "disliked_ingredients" in data and isinstance(data["disliked_ingredients"], list):
        profile_obj.disliked_ingredients = [str(x)[:20] for x in data["disliked_ingredients"]][:20]
    profile_obj.onboarded = True
    profile_obj.save()

    if isinstance(data.get("preferred_cuisines"), list):
        ids = [_int(x) for x in data["preferred_cuisines"] if _int(x)]
        profile_obj.preferred_cuisines.set(Cuisine.objects.filter(id__in=ids))
    if isinstance(data.get("preferred_tags"), list):
        ids = [_int(x) for x in data["preferred_tags"] if _int(x)]
        profile_obj.preferred_tags.set(Tag.objects.filter(id__in=ids))
    _track(request, "profile_update", None, weight=0.5)
    return _ok(_me_payload(request))


@require_GET
@api_login_required
def my_favorites(request):
    favs = Favorite.objects.filter(user=request.user).select_related("recipe").order_by("-created_at")[:100]
    return _ok(
        [
            {"favorited_at": f.created_at.isoformat(), **f.recipe.to_card()}
            for f in favs
            if f.recipe.status == Recipe.Status.PUBLISHED
        ]
    )


@csrf_exempt
@require_POST
def track_interaction(request):
    data = _body(request)
    action = (data.get("action") or "").strip()[:20]
    if action not in {"view", "click", "search", "rate", "favorite", "cook_done", "share"}:
        return _err("不支持的行为类型")
    recipe = Recipe.objects.filter(pk=_int(data.get("recipe_id"), 0)).first()
    _track(request, action, recipe, weight=_float(data.get("weight"), 1.0))
    return _ok(None, "已记录")


# ---------------------------------------------------------------------------
# 运维
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def rebuild_index(request):
    if not (request.user.is_authenticated and request.user.is_staff):
        return _err("需要管理员权限", status=403, code=403)
    try:
        from rag.retriever import get_retriever

        result = get_retriever().build_index(force=True)
    except Exception as exc:  # pragma: no cover
        logger.exception("索引重建失败")
        return _err(f"索引重建失败：{exc}", status=500, code=500)
    return _ok(result, "索引已重建")
