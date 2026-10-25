"""页面视图。

平台是「单页壳 + 服务端注入首屏上下文」的形态：所有页面共用 templates/index.html，
由 ``page`` 变量决定前端进入哪个视图，避免多份重复模板。
"""
from __future__ import annotations

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render

from food.models import Cuisine, Recipe, Tag


def _base_context(request, page: str, **extra) -> dict:
    user = request.user
    context = {
        "page": page,
        "is_authenticated": user.is_authenticated,
        "user_name": user.get_username() if user.is_authenticated else "",
        "user_id": user.id if user.is_authenticated else 0,
        "cuisines": list(Cuisine.objects.values("id", "name", "icon", "slug")),
        "tags": list(Tag.objects.values("id", "name", "kind")[:40]),
        "llm_ready": _llm_ready(),
        "demo_mode": getattr(settings, "DEMO_VERIFY_CODE", False),
        "static_version": getattr(settings, "STATIC_VERSION", ""),
    }
    context.update(extra)
    return context


def _llm_ready() -> bool:
    from food.services.assistant import llm_ready

    return llm_ready()


def home(request):
    return render(request, "index.html", _base_context(request, "home"))


def recipe_detail_page(request, pk: int):
    recipe = Recipe.objects.published().with_relations().filter(pk=pk).first()
    if recipe is None:
        raise Http404("菜谱不存在")
    return render(
        request,
        "index.html",
        _base_context(request, "recipe", recipe_id=pk, recipe_title=recipe.title),
    )


def search_page(request):
    keyword = (request.GET.get("q") or "").strip()
    return render(request, "index.html", _base_context(request, "search", keyword=keyword))


def assistant_page(request):
    return render(request, "index.html", _base_context(request, "assistant"))


def profile_page(request):
    if not request.user.is_authenticated:
        return redirect("login_page")
    return render(request, "index.html", _base_context(request, "profile"))
