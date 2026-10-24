"""热度分计算（时间衰减的互动量）。

公式（契约 docs/CONTRACTS.md §3.1）::

    engagement = rating_avg * rating_count * 1.2
               + favorite_count * 3
               + view_count     * 0.5
               + comment_count  * 2
    hot_score  = engagement / (hours + 2) ** 1.5

设计要点：
- 纯函数 ``hot_score`` 便于单测，不写库；
- ``recompute_hot_scores`` 走 ``values()`` 一次取全量统计 + 一次 ``bulk_update`` 批量回写，
  避免 N+1（评论数用 ``annotate(Count)`` 聚合）；
- 评论只统计未隐藏的（``is_hidden=False``）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from django.db.models import Count, Q
from django.utils import timezone

from food.models import Recipe

logger = logging.getLogger("recommend")

# 互动权重（与契约一致，勿随意改动）
W_RATING = 1.2
W_FAVORITE = 3.0
W_VIEW = 0.5
W_COMMENT = 2.0

DECAY_POWER = 1.5
DECAY_OFFSET = 2.0


def _comment_count(recipe: Any) -> int:
    """取评论数：优先用 ``annotate(comment_count=...)`` 的注解值，避免每行一次查询。"""
    counted = getattr(recipe, "comment_count", None)
    if counted is not None:
        return int(counted)
    prefetched = getattr(recipe, "_prefetched_objects_cache", {}) or {}
    comments = prefetched.get("comments")
    if comments is not None:
        return sum(1 for c in comments if not getattr(c, "is_hidden", False))
    if getattr(recipe, "pk", None) is None:
        return 0
    try:
        return recipe.comments.filter(is_hidden=False).count()
    except Exception:  # pragma: no cover - 数据异常时不影响热度计算
        logger.debug("评论数统计失败 recipe=%s", getattr(recipe, "pk", None), exc_info=True)
        return 0


def engagement_score(recipe: Any) -> float:
    """计算互动量（不含时间衰减）。

    Args:
        recipe: ``Recipe`` 实例，或带同名属性的轻量对象。

    Returns:
        加权互动量，非负浮点。
    """
    rating_avg = float(getattr(recipe, "rating_avg", 0) or 0)
    rating_count = int(getattr(recipe, "rating_count", 0) or 0)
    favorite_count = int(getattr(recipe, "favorite_count", 0) or 0)
    view_count = int(getattr(recipe, "view_count", 0) or 0)
    comment_count = _comment_count(recipe)
    return (
        rating_avg * rating_count * W_RATING
        + favorite_count * W_FAVORITE
        + view_count * W_VIEW
        + comment_count * W_COMMENT
    )


def hot_score(recipe: Any, now: datetime | None = None) -> float:
    """单条菜谱热度分（含时间衰减）。

    Args:
        recipe: ``Recipe`` 实例（需 ``created_at``）。
        now: 参照时间，默认当前时区时间；显式传入便于测试。

    Returns:
        ``engagement / (hours + 2) ** 1.5``；``created_at`` 缺失时按 0 小时处理。
    """
    now = now or timezone.now()
    created = getattr(recipe, "created_at", None) or now
    hours = max((now - created).total_seconds() / 3600.0, 0.0)
    return float(engagement_score(recipe) / ((hours + DECAY_OFFSET) ** DECAY_POWER))


def _hot_from_values(row: dict[str, Any], now: datetime) -> float:
    """由 ``values()`` 行数据直接算热度（不构造模型实例）。"""
    engagement = (
        float(row.get("rating_avg") or 0) * int(row.get("rating_count") or 0) * W_RATING
        + int(row.get("favorite_count") or 0) * W_FAVORITE
        + int(row.get("view_count") or 0) * W_VIEW
        + int(row.get("comment_count") or 0) * W_COMMENT
    )
    created = row.get("created_at") or now
    hours = max((now - created).total_seconds() / 3600.0, 0.0)
    return float(engagement / ((hours + DECAY_OFFSET) ** DECAY_POWER))


def _meaningfully_changed(old: float, new: float) -> bool:
    """分值是否有实质变化（时间衰减会让每次重算都带极小的自然漂移）。

    阈值取「绝对 1e-6」与「相对当前值 1e-4」的较大者：真实变化不会被漏掉，
    短时间内重复调用 ``recompute_hot_scores`` 也不会产生无意义的 UPDATE。
    """
    delta = abs(new - old)
    if delta <= 1e-9:
        return False
    return delta > max(abs(old) * 1e-4, 1e-6)


def recompute_hot_scores(limit: int | None = None) -> int:
    """批量重算并回写 ``Recipe.hot_score``。

    Args:
        limit: 只处理最近的 N 条（按创建时间倒序）；``None`` 表示全量。

    Returns:
        实际更新条数。

    实现：一次 ``values()`` 拉全量统计（评论数用 ``annotate(Count(filter=...))``，
    PostgreSQL 生成 ``COUNT(...) FILTER (WHERE ...)``，SQLite 亦支持；``values()``
    的分组字段包含 ``created_at``，满足 PG 的 GROUP BY 严格性），算出新分值后只挑
    有实质变化的行做 ``bulk_update(batch_size=500)``：500 行 × 1 字段约 1000 个绑定
    参数，远低于 PostgreSQL 的 65535 上限。
    """
    now = timezone.now()
    qs = Recipe.objects.all().annotate(
        comment_count=Count("comments", filter=Q(comments__is_hidden=False))
    ).values(
        "id",
        "rating_avg",
        "rating_count",
        "favorite_count",
        "view_count",
        "created_at",
        "comment_count",
    )
    if limit is not None:
        qs = qs.order_by("-created_at")[: max(int(limit), 0)]

    pending: list[Recipe] = []
    for row in qs.iterator(chunk_size=1000):
        new_value = _hot_from_values(row, now)
        pending.append(Recipe(id=row["id"], hot_score=new_value))

    if not pending:
        return 0

    # 只回写真正变化的行：先取旧值（一次查询），避免无意义 UPDATE
    old_map = dict(Recipe.objects.filter(id__in=[r.id for r in pending]).values_list("id", "hot_score"))
    changed = [
        r for r in pending if _meaningfully_changed(float(old_map.get(r.id, 0.0) or 0.0), r.hot_score)
    ]
    if not changed:
        return 0
    Recipe.objects.bulk_update(changed, ["hot_score"], batch_size=500)
    logger.info("热度回写完成：候选 %d 条，实际更新 %d 条", len(pending), len(changed))
    return len(changed)


def hot_scores_for(recipes: Iterable[Any], now: datetime | None = None) -> dict[int, float]:
    """批量计算若干菜谱的热度分，返回 ``{recipe_id: hot}``（供混合层归一化使用）。"""
    now = now or timezone.now()
    return {obj.pk: hot_score(obj, now=now) for obj in recipes if getattr(obj, "pk", None)}
