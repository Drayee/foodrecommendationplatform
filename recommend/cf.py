"""item-based 协同过滤（纯 numpy / dict，无 scipy、无 sklearn）。

设计（docs/CONTRACTS.md §3.3）：

1. 用户-物品矩阵：``Rating``（score）与 ``InteractionLog`` 行为权重合成
   （``view=1 / click=1.5 / favorite=3 / cook_done=4 / rate=score``）；
2. 物品相似度：对矩阵列做 L2 归一化后 ``S = Xn.T @ Xn``，每列取 top-k（排除自身）；
3. 进程内缓存：矩阵与相似度都带 TTL 与数据指纹（计数 + 最新时间戳），
   数据变化或调用 ``invalidate_cache()`` 时失效；
4. ``cf_scores`` 只对给定候选集算分，返回 0~1 归一化结果。

性能：物品相似度矩阵是 O(n_items²) 的稠密点积，靠缓存摊薄；实测 1500 物品约 30ms，
缓存命中 < 1ms。候选集由上层（hybrid）截断到 ≤500。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

import numpy as np
from django.db.models import Count, Max

from food.models import InteractionLog, Rating, Recipe

logger = logging.getLogger("recommend")

# 行为权重（契约规定）
ACTION_WEIGHTS: dict[str, float] = {
    "view": 1.0,
    "click": 1.5,
    "favorite": 3.0,
    "cook_done": 4.0,
    "rate": 3.0,  # 若 extra 里带 score，用 score 覆盖
}
SKIP_ACTIONS = {"search"}
MAX_CELL_VALUE = 12.0   # 单格上限，避免刷行为把矩阵拉爆
DEFAULT_TOP_K = 20
CACHE_TTL = 300.0       # 秒

_LOCK = threading.RLock()
_CACHE: dict[str, object] = {
    "fingerprint": None,
    "built_at": 0.0,
    "user_ids": [],
    "item_ids": [],
    "matrix": None,
    "sims": {},          # {"{k}": {item_id: [(other_id, sim), ...]}}
    "user_index": {},
    "item_index": {},
}


# ---------------------------------------------------------------------------
# 矩阵
# ---------------------------------------------------------------------------
def _data_fingerprint() -> tuple:
    """数据指纹：表计数 + 最新时间戳，用于缓存失效判定。"""
    ratings = Rating.objects.aggregate(count=Count("id"), latest=Max("updated_at"))
    logs = InteractionLog.objects.aggregate(count=Count("id"), latest=Max("created_at"))
    return (
        ratings["count"] or 0,
        ratings["latest"].isoformat() if ratings["latest"] else "",
        logs["count"] or 0,
        logs["latest"].isoformat() if logs["latest"] else "",
        Recipe.objects.count(),
    )


def _interaction_value(action: str, weight: float, extra: dict | None) -> float:
    """把一条行为埋点换算成矩阵权重。"""
    if action == "rate":
        score = 0.0
        if isinstance(extra, dict):
            try:
                score = float(extra.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
        return score if score > 0 else float(ACTION_WEIGHTS["rate"])
    base = ACTION_WEIGHTS.get(action)
    if base is None:
        try:
            return float(weight or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return base


def build_user_item_matrix() -> tuple[list[int], list[int], np.ndarray]:
    """构建用户-物品稀疏矩阵。

    Returns:
        ``(user_ids, item_ids, matrix)``，``matrix`` 为 ``float32``，形状 ``(len(user_ids), len(item_ids))``。

    合并规则：同一 (user, recipe) 上 ``Rating.score`` 与行为权重求和，单格上限 12。
    匿名行为（``user_id`` 为空）不进入矩阵（无法定位行）。
    """
    cells: dict[tuple[int, int], float] = {}

    ratings = Rating.objects.values_list("user_id", "recipe_id", "score")
    for user_id, recipe_id, score in ratings:
        if not user_id or not recipe_id:
            continue
        cells[(int(user_id), int(recipe_id))] = cells.get((int(user_id), int(recipe_id)), 0.0) + float(score or 0)

    logs = (
        InteractionLog.objects.exclude(user__isnull=True)
        .exclude(recipe__isnull=True)
        .values_list("user_id", "recipe_id", "action", "weight", "extra")
    )
    for user_id, recipe_id, action, weight, extra in logs:
        if action in SKIP_ACTIONS or not user_id or not recipe_id:
            continue
        value = _interaction_value(action, weight, extra)
        if value <= 0:
            continue
        key = (int(user_id), int(recipe_id))
        cells[key] = cells.get(key, 0.0) + value

    if not cells:
        return [], [], np.zeros((0, 0), dtype=np.float32)

    user_ids = sorted({key[0] for key in cells})
    item_ids = sorted({key[1] for key in cells})
    user_index = {uid: i for i, uid in enumerate(user_ids)}
    item_index = {iid: i for i, iid in enumerate(item_ids)}

    matrix = np.zeros((len(user_ids), len(item_ids)), dtype=np.float32)
    for (uid, iid), value in cells.items():
        matrix[user_index[uid], item_index[iid]] = min(value, MAX_CELL_VALUE)
    return user_ids, item_ids, matrix


def _cache_valid(fingerprint: tuple, ttl: float = CACHE_TTL) -> bool:
    """判断缓存是否仍可用。"""
    return (
        _CACHE.get("fingerprint") == fingerprint
        and _CACHE.get("matrix") is not None
        and (time.time() - float(_CACHE.get("built_at") or 0.0)) < ttl
    )


def _matrix_and_index(force: bool = False) -> tuple[list[int], list[int], np.ndarray]:
    """取矩阵（带缓存）。"""
    fingerprint = _data_fingerprint()
    with _LOCK:
        if not force and _cache_valid(fingerprint):
            return _CACHE["user_ids"], _CACHE["item_ids"], _CACHE["matrix"]  # type: ignore[return-value]

        user_ids, item_ids, matrix = build_user_item_matrix()
        _CACHE.update(
            {
                "fingerprint": fingerprint,
                "built_at": time.time(),
                "user_ids": user_ids,
                "item_ids": item_ids,
                "matrix": matrix,
                "sims": {},
                "user_index": {uid: i for i, uid in enumerate(user_ids)},
                "item_index": {iid: i for i, iid in enumerate(item_ids)},
            }
        )
        return user_ids, item_ids, matrix


def invalidate_cache() -> None:
    """清空进程内缓存（行为数据写入后调用）。"""
    with _LOCK:
        _CACHE.update(
            {
                "fingerprint": None,
                "built_at": 0.0,
                "user_ids": [],
                "item_ids": [],
                "matrix": None,
                "sims": {},
                "user_index": {},
                "item_index": {},
            }
        )
    logger.info("CF 缓存已清空")


# ---------------------------------------------------------------------------
# 物品相似度
# ---------------------------------------------------------------------------
def _compute_similarity(matrix: np.ndarray, item_ids: list[int], k: int) -> dict[int, list[tuple[int, float]]]:
    """对矩阵列做余弦相似并取 top-k。"""
    if matrix.size == 0 or not item_ids:
        return {}
    norms = np.linalg.norm(matrix, axis=0)
    safe = np.where(norms > 0, norms, 1.0)
    normalized = matrix / safe
    sims = normalized.T @ normalized  # (n_items, n_items)
    np.fill_diagonal(sims, 0.0)

    k = max(0, min(k, len(item_ids) - 1))
    result: dict[int, list[tuple[int, float]]] = {}
    if k == 0:
        return {iid: [] for iid in item_ids}

    for row in range(len(item_ids)):
        row_values = sims[row]
        top_idx = np.argpartition(-row_values, k - 1)[:k]
        top_idx = top_idx[np.argsort(-row_values[top_idx])]
        result[item_ids[row]] = [
            (item_ids[int(col)], round(float(row_values[int(col)]), 4))
            for col in top_idx
            if row_values[int(col)] > 0
        ]
    return result


def item_similarity(k: int = DEFAULT_TOP_K) -> dict[int, list[tuple[int, float]]]:
    """物品-物品相似度（item-based，带进程内缓存）。

    Args:
        k: 每个物品保留的最近邻数量。

    Returns:
        ``{item_id: [(similar_item_id, cosine), ...]}``，按相似度降序。
    """
    k = max(int(k or 0), 1)
    _, item_ids, matrix = _matrix_and_index()
    if not item_ids:
        return {}
    with _LOCK:
        sims_cache: dict[str, dict[int, list[tuple[int, float]]]] = _CACHE["sims"]  # type: ignore[assignment]
        key = str(k)
        if key in sims_cache:
            return sims_cache[key]
    computed = _compute_similarity(matrix, item_ids, k)
    with _LOCK:
        sims_cache = _CACHE["sims"]  # type: ignore[assignment]
        sims_cache[str(k)] = computed
    return computed


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def cf_scores(user, candidate_ids: Iterable[int], k: int = DEFAULT_TOP_K) -> dict[int, float]:
    """给候选物品算协同过滤分（0~1）。

    Args:
        user: ``User`` 实例；``None`` / 匿名 / 无行为时返回 ``{}``。
        candidate_ids: 候选菜谱 id 集合（上游已截断到 ≤500）。
        k: 物品近邻数。

    Returns:
        ``{recipe_id: score}``，仅包含有正分的候选；无信号返回 ``{}``。
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return {}
    candidates = {int(cid) for cid in candidate_ids if cid}
    if not candidates:
        return {}

    user_ids, item_ids, matrix = _matrix_and_index()
    if not item_ids:
        return {}
    user_index: dict[int, int] = _CACHE["user_index"]  # type: ignore[assignment]
    row = user_index.get(int(getattr(user, "pk", 0) or 0))
    if row is None:
        return {}

    liked = matrix[row]
    known = {item_ids[i]: float(liked[i]) for i in range(len(item_ids)) if liked[i] > 0}
    if not known:
        return {}

    sims = item_similarity(k=k)
    raw: dict[int, float] = {}
    for source_id, weight in known.items():
        for other_id, sim in sims.get(source_id, []):
            if other_id in candidates and other_id != source_id:
                raw[other_id] = raw.get(other_id, 0.0) + weight * sim

    if not raw:
        return {}
    peak = max(raw.values()) or 1.0
    return {cid: round(min(value / peak, 1.0), 4) for cid, value in raw.items()}


def user_consumed_ids(user) -> set[int]:
    """用户已消费（评分 / 收藏 / 做过）的菜谱 id 集合，用于推荐去重。"""
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    from food.models import Favorite

    ids: set[int] = set()
    ids.update(Rating.objects.filter(user=user).values_list("recipe_id", flat=True))
    ids.update(Favorite.objects.filter(user=user).values_list("recipe_id", flat=True))
    ids.update(
        InteractionLog.objects.filter(user=user, action__in=["cook_done", "favorite"])
        .exclude(recipe__isnull=True)
        .values_list("recipe_id", flat=True)
    )
    return {int(i) for i in ids if i}


def stats() -> dict[str, object]:
    """CF 运行状态（调试 / 运维用）。"""
    user_ids, item_ids, matrix = _matrix_and_index()
    return {
        "users": len(user_ids),
        "items": len(item_ids),
        "cells": int(np.count_nonzero(matrix)) if matrix.size else 0,
        "density": round(float(np.count_nonzero(matrix)) / matrix.size, 4) if matrix.size else 0.0,
        "cached": _cache_valid(_CACHE.get("fingerprint") or ()),
        "sim_k": sorted(_CACHE.get("sims", {}).keys()),  # type: ignore[union-attr]
    }
