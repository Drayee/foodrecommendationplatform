"""平台工具集（供 AI 助手 / 后台任务调用）。

与原项目 ``tools.py`` 的差异：
- 数据库访问走 Django ORM（原版硬编码 MySQLdb 连接串，部署即失效）；
- SQL 工具改为「只读 + 白名单」安全查询，禁止任意写操作；
- 邮件工具的参数全部来自 settings，不再硬编码邮箱与授权码；
- 新增美食场景工具：菜谱检索、个性化推荐、网络菜谱搜索、平台统计。

工具注册表 ``TOOL_REGISTRY`` 同时供 function-calling 的 schema 生成与手动派发使用。
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
from email.mime.text import MIMEText
from typing import Any

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger("food.tools")


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------
def send_email(to: str, subject: str, content: str) -> str:
    """发送 HTML 邮件（配置缺失时降级为控制台输出，返回值说明真实状态）。"""
    to = (to or "").strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
        return f"邮件发送失败：收件人地址不合法（{to}）"
    try:
        send_mail(
            subject=subject or "美食推荐平台通知",
            message=re.sub(r"<[^>]+>", "", content or ""),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to],
            html_message=content or "",
            fail_silently=False,
        )
        return f"邮件已成功发送给 {to}"
    except Exception as exc:  # pragma: no cover - 依赖外部 SMTP
        logger.warning("邮件发送失败：%s", exc)
        return f"邮件发送失败：{exc}"


def send_email_smtp(to: str, subject: str, content: str) -> str:
    """保留原项目的 SMTP_SSL 直发实现，便于某些不支持 STARTTLS 的邮箱。"""
    cfg = getattr(settings, "MAIL", {})
    if not cfg.get("USER") or not cfg.get("PASSWORD"):
        return "未配置 SMTP 账号，无法直发邮件"
    try:
        text = MIMEText(content, "html", "utf-8")
        text["to"] = to
        text["from"] = cfg["USER"]
        text["subject"] = subject
        smtp = smtplib.SMTP_SSL(host=cfg["HOST"], port=cfg["PORT"], timeout=cfg.get("TIMEOUT", 10))
        smtp.login(cfg["USER"], cfg["PASSWORD"])
        smtp.sendmail(cfg["USER"], [to], text.as_string())
        smtp.quit()
        return f"邮件已通过 SMTP 发送给 {to}"
    except Exception as exc:
        return f"邮件发送失败：{exc}"


# ---------------------------------------------------------------------------
# 菜谱工具
# ---------------------------------------------------------------------------
def search_recipes(keyword: str = "", limit: int = 5, cuisine: str = "", max_minutes: int = 0) -> str:
    """按关键词 / 菜系 / 耗时在平台菜谱库里检索，返回 JSON 字符串。"""
    from food.models import Recipe

    qs = Recipe.objects.published().with_relations()
    if keyword:
        qs = qs.search(keyword)
    if cuisine:
        qs = qs.filter(cuisine__name__icontains=cuisine)
    if max_minutes:
        qs = qs.filter(prep_minutes__lte=max_minutes)
    items = [r.to_card() for r in qs.order_by("-hot_score", "-rating_avg")[: max(1, min(int(limit), 20))]]
    return json.dumps({"count": len(items), "items": items}, ensure_ascii=False)


def get_recipe_detail(recipe_id: int = 0, title: str = "") -> str:
    """按 id 或菜名获取菜谱完整做法。"""
    from food.models import Recipe

    qs = Recipe.objects.published().with_relations()
    recipe = None
    if recipe_id:
        recipe = qs.filter(id=recipe_id).first()
    if recipe is None and title:
        recipe = qs.filter(title__icontains=title).first()
    if recipe is None:
        return json.dumps({"error": "未找到该菜谱"}, ensure_ascii=False)
    return json.dumps(recipe.to_detail(), ensure_ascii=False)


def recommend_recipes(user_id: int = 0, limit: int = 5, scene: str = "") -> str:
    """给出个性化推荐（user_id 为 0 时按热度冷启动）。"""
    from django.contrib.auth.models import User

    from recommend.hybrid import recommend_for_user

    user = User.objects.filter(id=user_id).first() if user_id else None
    results = recommend_for_user(user, k=max(1, min(int(limit), 20)), scene=scene or None)
    payload = [{"recipe": r.recipe.to_card(), "score": round(r.score, 4), "reason": r.reason} for r in results]
    return json.dumps({"count": len(payload), "items": payload}, ensure_ascii=False)


def web_search_recipes(query: str = "", limit: int = 5) -> str:
    """联网搜索菜谱线索（外部站点），返回标题 / 链接 / 摘要。"""
    from websearch.service import search_recipes_online

    result = search_recipes_online(query, limit=max(1, min(int(limit), 10)))
    return json.dumps(result, ensure_ascii=False)


def platform_stats() -> str:
    """平台运营数据概览。"""
    from django.db.models import Avg, Count, Sum

    from food.models import Comment, Favorite, Rating, Recipe, SearchLog

    agg = Recipe.objects.published().aggregate(
        total=Count("id"), views=Sum("view_count"), avg=Avg("rating_avg")
    )
    data = {
        "recipes": agg["total"] or 0,
        "views": agg["views"] or 0,
        "avg_rating": round(agg["avg"] or 0, 2),
        "favorites": Favorite.objects.count(),
        "ratings": Rating.objects.count(),
        "comments": Comment.objects.count(),
        "searches": SearchLog.objects.count(),
    }
    return json.dumps(data, ensure_ascii=False)



# ---------------------------------------------------------------------------
# 只读 SQL（保守白名单，仅用于运营人员手工排查）
# ---------------------------------------------------------------------------
_ALLOWED_SQL = re.compile(r"^\s*select\s", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|grant|attach|pragma)\b", re.IGNORECASE
)


def sql_query(sql: str) -> str:
    """执行只读 SELECT 查询（最多返回 50 行）。"""
    from django.db import connection

    if not _ALLOWED_SQL.match(sql or "") or _FORBIDDEN.search(sql or ""):
        return "仅允许只读 SELECT 查询"
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(50)
        return json.dumps({"columns": columns, "rows": [list(map(str, r)) for r in rows]}, ensure_ascii=False)
    except Exception as exc:
        return f"数据库查询失败，失败原因：{exc}"


# ---------------------------------------------------------------------------
# 工具注册表（schema 供 function calling 使用）
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "search_recipes": {
        "func": search_recipes,
        "schema": {
            "type": "function",
            "function": {
                "name": "search_recipes",
                "description": "在平台菜谱库中按关键词、菜系、耗时上限检索菜谱",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "关键词，如 番茄、下饭、低卡"},
                        "limit": {"type": "integer", "description": "返回条数，默认 5"},
                        "cuisine": {"type": "string", "description": "菜系，如 川菜"},
                        "max_minutes": {"type": "integer", "description": "最长烹饪时间（分钟）"},
                    },
                },
            },
        },
    },
    "get_recipe_detail": {
        "func": get_recipe_detail,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_recipe_detail",
                "description": "获取某个菜谱的完整食材与步骤",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "recipe_id": {"type": "integer", "description": "菜谱 id"},
                        "title": {"type": "string", "description": "菜名（id 未知时使用）"},
                    },
                },
            },
        },
    },
    "recommend_recipes": {
        "func": recommend_recipes,
        "schema": {
            "type": "function",
            "function": {
                "name": "recommend_recipes",
                "description": "根据用户画像与行为给出个性化菜谱推荐",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer", "description": "用户 id，0 表示游客"},
                        "limit": {"type": "integer", "description": "推荐条数"},
                        "scene": {"type": "string", "description": "场景：早餐/午餐/晚餐/夜宵/减脂"},
                    },
                },
            },
        },
    },
    "web_search_recipes": {
        "func": web_search_recipes,
        "schema": {
            "type": "function",
            "function": {
                "name": "web_search_recipes",
                "description": "联网搜索站外菜谱线索，用于补充平台没有的菜式",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词"},
                        "limit": {"type": "integer", "description": "条数"},
                    },
                    "required": ["query"],
                },
            },
        },
    },
    "platform_stats": {
        "func": platform_stats,
        "schema": {
            "type": "function",
            "function": {
                "name": "platform_stats",
                "description": "查询平台菜谱、评分、收藏等运营统计",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "send_email": {
        "func": send_email,
        "schema": {
            "type": "function",
            "function": {
                "name": "send_email",
                "description": "给用户发送邮件（菜谱清单、通知等）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "收件人邮箱"},
                        "subject": {"type": "string", "description": "邮件主题"},
                        "content": {"type": "string", "description": "HTML 正文"},
                    },
                    "required": ["to", "subject", "content"],
                },
            },
        },
    },
}


def tool_schemas(names: list[str] | None = None) -> list[dict]:
    names = names or list(TOOL_REGISTRY)
    return [TOOL_REGISTRY[n]["schema"] for n in names if n in TOOL_REGISTRY]


def call_tool(name: str, arguments: dict | str | None = None) -> str:
    """按名字派发工具调用，永远返回字符串（失败也不抛异常）。"""
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        return f"未知工具：{name}"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
    try:
        return str(entry["func"](**(arguments or {})))
    except TypeError as exc:
        return f"工具 {name} 参数错误：{exc}"
    except Exception as exc:  # pragma: no cover
        logger.exception("工具执行失败 %s", name)
        return f"工具 {name} 执行失败：{exc}"
