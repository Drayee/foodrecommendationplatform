"""页面视图 + 兼容旧作业路由。

页面：登录页、平台主界面
兼容：原 RAGDjange 作业暴露的 send_code / login_code / api_chat / api/email 接口全部保留
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from food.services import assistant
from food.services import auth as auth_service

logger = logging.getLogger("food.views")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def login_html(request):
    if request.user.is_authenticated:
        return redirect("index_page")
    return render(
        request,
        "login.html",
        {
            "demo_mode": getattr(settings, "DEMO_VERIFY_CODE", False),
            "static_version": getattr(settings, "STATIC_VERSION", ""),
        },
    )


def index_html(request):
    from food.models import Cuisine, Tag

    context = {
        "user_name": request.user.get_username() if request.user.is_authenticated else "",
        "is_authenticated": request.user.is_authenticated,
        "cuisines": list(Cuisine.objects.all().values("id", "name", "icon")),
        "tastes": list(Tag.objects.filter(kind="taste").values("id", "name")[:12]),
        "llm_ready": assistant.llm_ready(),
        "demo_mode": getattr(settings, "DEMO_VERIFY_CODE", False),
        "static_version": getattr(settings, "STATIC_VERSION", ""),
    }
    return render(request, "index.html", context)


def logout_view(request):
    logout(request)
    return redirect("login_page")


def health(request):
    """健康检查：数据库 / 索引 / LLM 三块状态。"""
    from django.db import connection

    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pragma: no cover
        db_ok = False
        logger.warning("数据库健康检查失败：%s", exc)
    return JsonResponse(
        {
            "status": "ok" if db_ok else "degraded",
            "database": db_ok,
            "llm": assistant.llm_status(),
            "index": assistant.index_status(),
        },
        json_dumps_params={"ensure_ascii": False},
    )


# ---------------------------------------------------------------------------
# 兼容旧接口
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def send_code(request):
    data = json.loads(request.body or "{}")
    return auth_service.send_code(data.get("email"))


@csrf_exempt
@require_POST
def login_code(request):
    data = json.loads(request.body or "{}")
    return auth_service.login_code(request, data.get("email"), data.get("code"))


def _sse(generator):
    """把纯文本生成器包装成 SSE。"""
    try:
        for chunk in generator:
            if chunk:
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
    except Exception as exc:  # pragma: no cover
        logger.exception("SSE 输出异常")
        yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
    finally:
        yield "data: [DONE]\n\n"


@csrf_exempt
@require_POST
def api_chat(request):
    """兼容原 /api/chat/：走 RAG 助手的流式回答。"""
    data = json.loads(request.body or "{}")
    question = (data.get("question") or "").strip()
    session_id = data.get("sessionId") or "legacy"
    if not question:
        return JsonResponse({"code": 400, "msg": "问题不能为空"}, json_dumps_params={"ensure_ascii": False})
    stream = assistant.stream_answer(
        question=question,
        session_key=f"legacy:{session_id}",
        user=request.user if request.user.is_authenticated else None,
        use_tools=True,
    )
    return StreamingHttpResponse(_sse(stream), content_type="text/event-stream")


@csrf_exempt
@require_POST
def send_email_assis(request):
    """兼容原 /api/email/：菜谱清单邮件助手（流式）。"""
    data = json.loads(request.body or "{}")
    question = (data.get("question") or "").strip()
    session_id = data.get("sessionId") or "legacy-mail"
    stream = assistant.stream_answer(
        question=question,
        session_key=f"mail:{session_id}",
        user=request.user if request.user.is_authenticated else None,
        use_tools=True,
        mode="email",
    )
    return StreamingHttpResponse(_sse(stream), content_type="text/event-stream")
