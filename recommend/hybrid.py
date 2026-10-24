"""混合推荐排序（内容 / 协同 / 热度 / 新鲜度 + MMR 去同质化 + 场景过滤）。

职责（docs/CONTRACTS.md §3.4）：
- ``recommend_for_user``：加权求和的多路召回排序，冷启动自动切「热度 + 菜系/口味多样性」；
- ``recommend_similar``：相似菜谱（内容相似度直出）。

工程约束：
- 权重从 ``settings.RECOMMEND`` 读取（值为字符串，需 ``float()`` 转换）；
- 候选集一次 ``values()`` + 标签 / 食材 / 菜系三张映射表，杜绝 N+1；
- 任何异常都在本层兜底为「热度 top-k」，绝不向 API 层抛出。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from django.conf import settings
from django.utils import timezone

from food.models import Cuisine, Recipe, RecipeIngredient

from . import cf, content
from .content import ContentProfile, RecipeFeatures

logger = logging.getLogger("recommend")

CANDIDATE_LIMIT = 500     # 候选集上限（契约）
MMR_POOL = 80             # 参与 MMR 的候选池
DEFAULT_FEED_SIZE = 12

DEFAULT_WEIGHTS: dict[str, float] = {
    "content": 0.45,
    "cf": 0.30,
    "hot": 0.15,
    "fresh": 0.10,
}
DEFAULT_MMR_LAMBDA = 0.7

# 场景规则：标签命中 或 硬约束满足 即算匹配
SCENE_RULES: dict[str, dict[str, object]] = {
    "早餐": {"tags": ("早餐", "早点", "粥", "面点", "豆浆", "包子", "馒头", "油条", "煎饼"), "max_minutes": 25},
    "午餐": {"tags": ("午餐", "便当", "家常", "下饭", "快手"), "max_minutes": 45},
    "晚餐": {"tags": ("晚餐", "家常", "下饭", "硬菜", "汤"), "max_minutes": 90},
    "夜宵": {"tags": ("夜宵", "宵夜", "下酒", "烧烤", "小吃"), "max_minutes": 35},
    "减脂": {"tags": ("减脂", "低卡", "低脂", "轻食", "沙拉", "健身"), "max_calories": 400},
    "下饭": {"tags": ("下饭", "红烧", "香辣", "家常", "重口"), "max_minutes": 60},
    "宴客": {"tags": ("宴客", "待客", "硬菜", "大菜", "面子菜"), "min_difficulty": 3},
    "一人食": {"tags": ("一人食", "单人", "简餐", "快手"), "max_servings": 1},
    "快手": {"tags": ("快手", "懒人", "简单", "10分钟"), "max_minutes": 25},
}

# 场景别名，兼容前端/工具层传入的近似词
SCENE_ALIASES: dict[str, str] = {
    "早饭": "早餐", "早点": "早餐", "早饭吃什么": "早餐",
    "中饭": "午餐", "午饭": "午餐",
    "晚饭": "晚餐",
    "宵夜": "夜宵", "深夜": "夜宵",
    "减肥": "减脂", "瘦身": "减脂", "低卡": "减脂",
    "下饭菜": "下饭", "配饭": "下饭",
    "请客": "宴客", "待客": "宴客",
    "单人": "一人食", "独食": "一人食",
    "快速": "快手", "懒人": "快手",
}


@dataclass
class ScoredRecipe:
    """推荐结果项。

    Attributes:
        recipe: 菜谱实例（已带 ``tags`` / ``cuisine``，可直接 ``to_card()``）。
        score: 混合后的最终分（0~1）。
        reason: 中文推荐理由，前端直接展示。
    """

    recipe: Recipe
    score: float
    reason: str


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _as_float(raw: object, default: float) -> float:
    """把 settings 里的字符串权重安全转成 float。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def recommend_settings() -> dict[str, object]:
    """读取并规整 ``settings.RECOMMEND``。

    Returns:
        ``{"weights": {...}, "mmr_lambda": float, "feed_size": int, "decay_hours": float}``；
        权重按总和归一化，总和非法时退回默认值。
    """
    cfg = getattr(settings, "RECOMMEND", {}) or {}
    weights = {
        "content": _as_float(cfg.get("WEIGHT_CONTENT"), DEFAULT_WEIGHTS["content"]),
        "cf": _as_float(cfg.get("WEIGHT_CF"), DEFAULT_WEIGHTS["cf"]),
        "hot": _as_float(cfg.get("WEIGHT_HOT"), DEFAULT_WEIGHTS["hot"]),
        "fresh": _as_float(cfg.get("WEIGHT_FRESH"), DEFAULT_WEIGHTS["fresh"]),
    }
    weights = {k: max(v, 0.0) for k, v in weights.items()}
    total = sum(weights.values())
    if total <= 0:
        weights = dict(DEFAULT_WEIGHTS)
        total = sum(weights.values())
    weights = {k: v / total for k, v in weights.items()}

    mmr_lambda = _as_float(cfg.get("MMR_LAMBDA"), DEFAULT_MMR_LAMBDA)
    mmr_lambda = min(max(mmr_lambda, 0.0), 1.0)
    feed_size = int(_as_float(cfg.get("FEED_SIZE"), DEFAULT_FEED_SIZE) or DEFAULT_FEED_SIZE)
    decay_hours = _as_float(cfg.get("HOT_DECAY_HOURS"), 72.0) or 72.0
    return {
        "weights": weights,
        "mmr_lambda": mmr_lambda,
        "feed_size": max(feed_size, 1),
        "decay_hours": max(decay_hours, 1.0),
    }


# ---------------------------------------------------------------------------
# 候选集
# ---------------------------------------------------------------------------
_CANDIDATE_FIELDS = (
    "id",
    "title",
    "cuisine_id",
    "prep_minutes",
    "cook_minutes",
    "difficulty",
    "calories",
    "protein",
    "servings",
    "rating_avg",
    "rating_count",
    "hot_score",
    "created_at",
)


def _load_candidates(excluded: set[int], now) -> list[RecipeFeatures]:
    """一次查询加载候选菜谱特征（含标签 / 食材 / 菜系三张映射表，共 4 条 SQL）。"""
    base = Recipe.objects.published()
    if excluded:
        base = base.exclude(id__in=list(excluded))
    rows = list(
        base.order_by("-hot_score", "-rating_avg", "-id").values(*_CANDIDATE_FIELDS)[:CANDIDATE_LIMIT]
    )
    if not rows:
        return []

    ids = [row["id"] for row in rows]
    tag_map: dict[int, list[tuple[str, str]]] = {}
    for recipe_id, name, kind in Recipe.tags.through.objects.filter(recipe_id__in=ids).values_list(
        "recipe_id", "tag__name", "tag__kind"
    ):
        tag_map.setdefault(recipe_id, []).append((name, kind))

    ing_map: dict[int, list[tuple[str, bool]]] = {}
    for recipe_id, name_text, is_main in RecipeIngredient.objects.filter(recipe_id__in=ids).values_list(
        "recipe_id", "name_text", "is_main"
    ):
        ing_map.setdefault(recipe_id, []).append((name_text, bool(is_main)))

    cuisine_ids = {row["cuisine_id"] for row in rows if row["cuisine_id"]}
    cuisine_map = (
        dict(Cuisine.objects.filter(id__in=list(cuisine_ids)).values_list("id", "name")) if cuisine_ids else {}
    )

    features: list[RecipeFeatures] = []
    for row in rows:
        features.append(
            content.features_from_parts(
                recipe_id=row["id"],
                title=row["title"],
                cuisine=cuisine_map.get(row["cuisine_id"], ""),
                tags=tag_map.get(row["id"], []),
                ingredients=ing_map.get(row["id"], []),
                prep_minutes=row["prep_minutes"],
                cook_minutes=row["cook_minutes"],
                difficulty=row["difficulty"],
                calories=row["calories"],
                protein=row["protein"],
                servings=row["servings"],
                rating_avg=row["rating_avg"],
                rating_count=row["rating_count"],
                hot_score=row["hot_score"],
                created_at=row["created_at"],
                now=now,
            )
        )
    return features


# ---------------------------------------------------------------------------
# 场景过滤
# ---------------------------------------------------------------------------
def normalize_scene(scene: str | None) -> str | None:
    """场景词归一化（别名 → 标准场景名），未知场景返回 ``None``（不过滤）。"""
    if not scene:
        return None
    text = str(scene).strip()
    if not text:
        return None
    text = SCENE_ALIASES.get(text, text)
    return text if text in SCENE_RULES else None


def scene_match(feat: RecipeFeatures, scene: str | None) -> bool:
    """判断菜谱是否属于给定场景。

    Args:
        feat: 菜谱特征。
        scene: 场景名（应已归一化）。

    Returns:
        ``True`` 表示命中；场景未知时一律 ``True``（不过滤）。
    """
    rule = SCENE_RULES.get(scene or "")
    if not rule:
        return True
    if feat.has_any_tag(rule.get("tags", ())):  # type: ignore[arg-type]
        return True
    max_minutes = rule.get("max_minutes")
    if max_minutes is not None and 0 < feat.total_minutes <= float(max_minutes):  # type: ignore[arg-type]
        return True
    max_calories = rule.get("max_calories")
    if max_calories is not None and 0 < feat.calories_per_serving <= float(max_calories):  # type: ignore[arg-type]
        return True
    min_difficulty = rule.get("min_difficulty")
    if min_difficulty is not None and feat.difficulty >= int(min_difficulty):  # type: ignore[arg-type]
        return True
    max_servings = rule.get("max_servings")
    if max_servings is not None and 0 < feat.servings <= int(max_servings):  # type: ignore[arg-type]
        return True
    return False


def select_scene(features: Sequence[RecipeFeatures], scene: str | None) -> tuple[set[int], str]:
    """按场景挑选候选（标签优先，标签全无时退化为耗时/热量启发式）。

    Args:
        features: 候选特征列表。
        scene: 已归一化的场景名。

    Returns:
        ``(命中的 recipe id 集合, 策略)``，策略为 ``"tag"`` / ``"heuristic"`` / ``"none"``。

    说明：真实数据里场景标签（早餐 2 道、快手 11 道……）比纯耗时启发式准确得多，
    因此只要候选池里存在场景标签命中，就只按标签筛；一道标签都没有时才退回启发式，
    避免「25 分钟以内的菜全被算成早餐」。
    """
    if not scene or scene not in SCENE_RULES:
        return set(), "none"
    rule = SCENE_RULES[scene]
    tags = rule.get("tags", ())
    tagged = [feat for feat in features if feat.has_any_tag(tags)]  # type: ignore[arg-type]
    if tagged:
        return {feat.id for feat in tagged}, "tag"
    return {feat.id for feat in features if scene_match(feat, scene)}, "heuristic"


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def _freshness(hours: float, decay_hours: float) -> float:
    """新鲜度：以 ``decay_hours`` 为时间常数做指数衰减。"""
    return math.exp(-max(hours, 0.0) / max(decay_hours, 1.0))


def _feature_similarity(left: RecipeFeatures, right: RecipeFeatures) -> float:
    """两个菜谱的同质化相似度（标签 Jaccard 0.6 + 食材 0.3 + 同菜系 0.1）。"""
    def jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        union = a | b
        return len(a & b) / len(union) if union else 0.0

    same_cuisine = 1.0 if left.cuisine and left.cuisine == right.cuisine else 0.0
    return 0.6 * jaccard(left.tags, right.tags) + 0.3 * jaccard(
        set(left.ingredients), set(right.ingredients)
    ) + 0.1 * same_cuisine


def _mmr_select(
    pool: Sequence[tuple[RecipeFeatures, float]],
    k: int,
    lam: float,
) -> list[tuple[RecipeFeatures, float]]:
    """MMR 贪心选择：``lam * 相关性 - (1 - lam) * 与已选最大相似度``。"""
    remaining = list(pool)
    selected: list[tuple[RecipeFeatures, float]] = []
    while remaining and len(selected) < k:
        best_index = 0
        best_value = float("-inf")
        for index, (feat, score) in enumerate(remaining):
            if selected:
                penalty = max(_feature_similarity(feat, chosen[0]) for chosen in selected)
            else:
                penalty = 0.0
            value = lam * score - (1.0 - lam) * penalty
            if value > best_value:
                best_value = value
                best_index = index
        selected.append(remaining.pop(best_index))
    return selected


def _compose_reason(
    content_reasons: list[str],
    feat: RecipeFeatures,
    profile: ContentProfile,
    cf_score: float,
    is_cold: bool,
    scene: str | None = None,
) -> str:
    """拼装最终展示理由（场景命中时加前缀）。"""
    if content_reasons:
        head = "；".join(content_reasons[:2])
        if cf_score >= 0.5 and not is_cold:
            head = f"{head}；和你口味相近的人也在做"
        return head
    if is_cold:
        base = f"{feat.cuisine}人气榜" if feat.cuisine else "平台热门精选"
        return f"{scene}推荐 · {base}" if scene else base
    if cf_score >= 0.5:
        return "和你口味相近的人都喜欢"
    base = f"{feat.cuisine}新发现" if feat.cuisine else "换换口味试试"
    return f"{scene}推荐 · {base}" if scene else base


def _score_candidate(
    feat: RecipeFeatures,
    profile: ContentProfile,
    content_score: float,
    cf_score: float,
    hot_norm: float,
    fresh_norm: float,
    weights: dict[str, float],
) -> float:
    """按激活权重归一化的加权求和。"""
    active: dict[str, float] = {"hot": weights["hot"], "fresh": weights["fresh"]}
    if profile.has_signals:
        active["content"] = weights["content"]
    if cf_score > 0:
        active["cf"] = weights["cf"]
    total = sum(active.values()) or 1.0
    value = (
        active.get("content", 0.0) * content_score
        + active.get("cf", 0.0) * cf_score
        + active["hot"] * hot_norm
        + active["fresh"] * fresh_norm
    )
    return max(0.0, min(value / total, 1.0))


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def _fetch_recipes(ids: Sequence[int]) -> dict[int, Recipe]:
    """批量取菜谱对象并保持 prefetch（1 次主查询 + 2 次关系查询）。"""
    if not ids:
        return {}
    objs = (
        Recipe.objects.filter(id__in=list(ids))
        .select_related("cuisine", "author")
        .prefetch_related("tags", "ingredients")
    )
    return {obj.id: obj for obj in objs}


def _fallback_hot(
    k: int,
    excluded: set[int],
    scene: str | None = None,
    reason: str = "热门推荐",
) -> list[ScoredRecipe]:
    """兜底：直接按 ``hot_score`` 取 top-k（不依赖任何画像 / CF 数据）。"""
    qs = Recipe.objects.published().exclude(id__in=list(excluded)) if excluded else Recipe.objects.published()
    objs = list(qs.with_relations().order_by("-hot_score", "-rating_avg")[: max(k * 3, k)])
    picked: list[ScoredRecipe] = []
    now = timezone.now()
    for obj in objs:
        if len(picked) >= k:
            break
        if scene:
            try:
                if not scene_match(content.features_from_recipe(obj, now=now), scene):
                    continue
            except Exception:  # pragma: no cover
                pass
        picked.append(ScoredRecipe(recipe=obj, score=round(min(obj.hot_score / 100.0, 1.0), 4), reason=reason))
    if len(picked) < k:  # 场景过滤太严时放宽，保证有结果
        picked_ids = {item.recipe.id for item in picked}
        for obj in objs:
            if len(picked) >= k:
                break
            if obj.id in picked_ids:
                continue
            picked.append(ScoredRecipe(recipe=obj, score=0.05, reason=reason))
    return picked


def recommend_for_user(
    user,
    k: int = 12,
    scene: str | None = None,
    exclude_ids: Iterable[int] | None = None,
    diversity: bool = True,
) -> list[ScoredRecipe]:
    """给用户生成个性化推荐。

    Args:
        user: ``User`` 实例；``None`` / 匿名用户走冷启动。
        k: 返回条数（会被夹到 1~50）。
        scene: 场景词（早餐/午餐/晚餐/夜宵/减脂/下饭/宴客/一人食/快手，含别名）。
        exclude_ids: 额外排除的菜谱 id。
        diversity: 是否用 MMR 去同质化（lambda 取 ``settings.RECOMMEND["MMR_LAMBDA"]``）。

    Returns:
        ``list[ScoredRecipe]``，按最终分降序；异常时退化为热度 top-k。
    """
    cfg = recommend_settings()
    k = max(1, min(int(k or 0) or int(cfg["feed_size"]), 50))
    scene_norm = normalize_scene(scene)
    excluded = {int(i) for i in (exclude_ids or []) if i}

    try:
        seen = cf.user_consumed_ids(user)
        excluded |= seen
        now = timezone.now()
        profile = content.build_profile(user)
        features = _load_candidates(excluded, now)

        if scene_norm:
            in_scene, strategy = select_scene(features, scene_norm)
            if not in_scene:
                logger.info("场景 %s 无命中候选（策略 %s），已放宽为全量候选", scene_norm, strategy)
            else:
                logger.debug("场景 %s 命中 %d 条（策略 %s）", scene_norm, len(in_scene), strategy)
        else:
            in_scene = set()
        if not features:
            return _fallback_hot(k, excluded, scene_norm)

        content_scores = {
            feat.id: content.score_features(profile, feat) for feat in features
        }
        cf_map = cf.cf_scores(user, [feat.id for feat in features]) if profile.has_signals or user else {}
        hot_peak = max((feat.hot_score for feat in features), default=0.0) or 0.0
        decay_hours = float(cfg["decay_hours"])
        weights: dict[str, float] = cfg["weights"]  # type: ignore[assignment]
        is_cold = not profile.has_signals and not cf_map

        scored: list[tuple[RecipeFeatures, float]] = []
        for feat in features:
            score_value, reasons = content_scores.get(feat.id, (0.0, []))
            cf_value = float(cf_map.get(feat.id, 0.0))
            if hot_peak > 0:
                hot_norm = max(min(feat.hot_score / hot_peak, 1.0), 0.0)
            else:  # 热度未回写时用评分兜底
                hot_norm = max(min(feat.rating_avg / 5.0, 1.0), 0.0)
            fresh_norm = _freshness(feat.created_hours, decay_hours)
            final = _score_candidate(feat, profile, score_value, cf_value, hot_norm, fresh_norm, weights)
            final = round(final, 4)
            scored.append((feat, final))

        # 场景命中的排前面：命中不足 k 条时用其余候选补齐（feed 不会变短）
        scored.sort(key=lambda pair: (pair[0].id in in_scene, pair[1]), reverse=True)
        pool_all = scored[: max(MMR_POOL, k)]
        if in_scene:
            scene_pool = [pair for pair in scored if pair[0].id in in_scene][: max(MMR_POOL, k)]
            other_pool = [pair for pair in scored if pair[0].id not in in_scene][: max(MMR_POOL, k)]
        else:
            scene_pool, other_pool = [], pool_all

        lam = float(cfg["mmr_lambda"])
        if diversity:
            if in_scene:
                chosen = _mmr_select(scene_pool, min(k, len(scene_pool)), lam)
                if len(chosen) < k:
                    chosen += _mmr_select(other_pool, k - len(chosen), lam)
            else:
                chosen = _mmr_select(pool_all, k, lam)
        else:
            chosen = (scene_pool + other_pool)[:k] if in_scene else pool_all[:k]

        recipes = _fetch_recipes([feat.id for feat, _ in chosen])
        results: list[ScoredRecipe] = []
        for feat, score_value in chosen:
            obj = recipes.get(feat.id)
            if obj is None:  # 并发下线等边界
                continue
            _, reasons = content_scores.get(feat.id, (0.0, []))
            results.append(
                ScoredRecipe(
                    recipe=obj,
                    score=round(float(score_value), 4),
                    reason=_compose_reason(
                        reasons, feat, profile, float(cf_map.get(feat.id, 0.0)), is_cold, scene_norm
                    ),
                )
            )
        if not results:
            return _fallback_hot(k, excluded, scene_norm)
        return results
    except Exception as exc:  # 兜底：任何异常都不让 feed 接口 500
        logger.warning("推荐主流程异常，降级为热度排序：%s", exc, exc_info=True)
        try:
            return _fallback_hot(k, excluded, scene_norm)
        except Exception:  # pragma: no cover - 数据库不可用时返回空列表
            logger.error("热度兜底同样失败", exc_info=True)
            return []


def recommend_similar(recipe_id: int, k: int = 6) -> list[ScoredRecipe]:
    """相似菜谱推荐。

    Args:
        recipe_id: 基准菜谱 id。
        k: 返回条数。

    Returns:
        ``list[ScoredRecipe]``；基准菜谱不存在时返回 ``[]``。
    """
    k = max(1, min(int(k or 0) or 6, 20))
    try:
        recipe = (
            Recipe.objects.filter(pk=recipe_id)
            .select_related("cuisine")
            .prefetch_related("tags", "ingredients")
            .first()
        )
        if recipe is None:
            return []
        pairs = content.similar_recipes(recipe, k=k)
        results: list[ScoredRecipe] = []
        for obj, score_value in pairs:
            reason = "口味/食材相近"
            if obj.cuisine_id and obj.cuisine_id == recipe.cuisine_id:
                reason = f"同是{getattr(obj.cuisine, 'name', '')}，口味相近"
            results.append(ScoredRecipe(recipe=obj, score=round(float(score_value), 4), reason=reason))
        return results
    except Exception as exc:
        logger.warning("相似菜谱推荐异常：%s", exc, exc_info=True)
        return []
