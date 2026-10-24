"""内容画像与单菜谱打分。

职责（docs/CONTRACTS.md §3.2）：
- ``build_profile(user)``：把 ``UserProfile`` 的显式偏好 + 历史行为（评分 ≥4 / 收藏 / 做过）
  聚合成 ``ContentProfile``；
- ``score_recipe(profile, recipe)``：输出 0~1 分数与中文推荐理由；忌口命中直接 0 分；
- ``similar_recipes(recipe, k)``：基于标签 / 食材 / 菜系 / 标题的稀疏余弦相似度。

性能约定：所有候选菜谱的标签、食材、菜系必须由调用方一次 prefetch；本模块
``features_from_recipe`` 只读已 prefetch 的关系，不额外发查询（未 prefetch 时才回落到查询）。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from django.db.models import Q

from food.models import Recipe, UserProfile

logger = logging.getLogger("recommend")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
HISTORY_LIMIT = 80            # 参与画像的历史行为条数上限（性能护栏）
POSITIVE_RATING = 4           # 视为正向反馈的最低分
MAX_CANDIDATES = 500          # 相似菜谱候选上限

CUISINE_WEIGHT_PREF = 3.0     # 用户显式选择的菜系
TAG_WEIGHT_PREF = 2.0         # 用户显式选择的标签
FAVORITE_WEIGHT = 1.5
COOK_DONE_WEIGHT = 2.0

MEAT_WORDS: tuple[str, ...] = (
    "猪肉", "牛肉", "羊肉", "鸡肉", "鸭肉", "五花", "排骨", "牛腩", "里脊", "培根",
    "火腿", "香肠", "腊肉", "肉末", "肉丝", "肉片", "鸡翅", "鸡腿", "鸡胸", "虾",
    "鱼", "蟹", "贝", "鱿", "墨鱼", "蛤", "蚝", "海参", "牛", "羊", "猪", "鸡", "鸭",
)

SPICY_TAG_LEVEL: dict[str, int] = {
    "微辣": 2, "小辣": 2, "中辣": 3, "香辣": 3, "麻辣": 4, "特辣": 5, "重辣": 5, "变态辣": 5,
}
SPICY_WORDS: tuple[str, ...] = ("辣椒", "干辣椒", "花椒", "豆瓣酱", "剁椒", "小米辣", "朝天椒")

DIET_ALIASES: dict[str, str] = {
    "vegetarian": "vegetarian", "素食": "vegetarian", "vegan": "vegetarian", "纯素": "vegetarian",
    "low_cal": "low_cal", "低卡": "low_cal", "低脂": "low_cal", "减脂": "low_cal",
    "high_protein": "high_protein", "高蛋白": "high_protein", "增肌": "high_protein",
}

# 打分权重（未激活的信号会被剔除后重新归一化）
SIGNAL_WEIGHTS: dict[str, float] = {
    "quality": 0.12,
    "cuisine": 0.22,
    "tag": 0.20,
    "ingredient": 0.10,
    "spicy": 0.10,
    "time": 0.12,
    "diet": 0.14,
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ContentProfile:
    """用户内容画像。

    Attributes:
        cuisine_weights: ``{菜系名: 权重}``（未归一化，越大越偏好）。
        tag_weights: ``{标签名: 权重}``。
        ingredient_names: 用户常做/常吃的食材名集合（小写归一化前的原始中文）。
        disliked: 忌口食材集合。
        avg_minutes: 历史偏好耗时（分钟）；无历史时退化为 ``max_cook_minutes``。
        spicy_level: 吃辣等级 0~5；``-1`` 表示未知（不参与扣分）。
        diet_flags: 归一化后的饮食标识集合（vegetarian/low_cal/high_protein）。
    """

    cuisine_weights: dict[str, float] = field(default_factory=dict)
    tag_weights: dict[str, float] = field(default_factory=dict)
    ingredient_names: set[str] = field(default_factory=set)
    disliked: set[str] = field(default_factory=set)
    avg_minutes: float = 0.0
    spicy_level: int = -1
    diet_flags: set[str] = field(default_factory=set)

    @property
    def has_signals(self) -> bool:
        """是否有任何可用于个性化的信号（冷启动判定）。"""
        return bool(
            self.cuisine_weights or self.tag_weights or self.ingredient_names or self.diet_flags
        )


@dataclass
class RecipeFeatures:
    """菜谱的轻量特征（一次 prefetch 后不再访问数据库）。"""

    id: int
    title: str
    cuisine: str
    tag_names: tuple[str, ...]
    tag_kinds: dict[str, tuple[str, ...]]
    ingredients: tuple[str, ...]
    main_ingredients: tuple[str, ...]
    total_minutes: int
    difficulty: int
    calories_per_serving: float
    protein_per_serving: float
    servings: int
    rating_avg: float
    rating_count: int
    hot_score: float
    created_hours: float
    spicy_level: int

    @property
    def tags(self) -> set[str]:
        """标签名集合（MMR 相似度用）。"""
        return set(self.tag_names)

    def has_any_tag(self, names: Iterable[str]) -> bool:
        """是否命中给定标签名中的任意一个。"""
        wanted = set(names)
        return bool(wanted & set(self.tag_names))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """把数值夹到 ``[low, high]``。"""
    return max(low, min(high, value))


def _normalize_name(name: str) -> str:
    """食材/标签名归一化：去空白、去括号内容。"""
    text = (name or "").strip().lower()
    for bracket in ("（", "(", "【", "["):
        if bracket in text:
            text = text.split(bracket, 1)[0]
    return text.strip()


# ---------------------------------------------------------------------------
# 特征抽取
# ---------------------------------------------------------------------------
def features_from_parts(
    *,
    recipe_id: int,
    title: str,
    cuisine: str,
    tags: Sequence[tuple[str, str]],
    ingredients: Sequence[tuple[str, bool]],
    prep_minutes: int,
    cook_minutes: int,
    difficulty: int,
    calories: float,
    protein: float,
    servings: int,
    rating_avg: float,
    rating_count: int,
    hot_score: float,
    created_at,
    now=None,
) -> RecipeFeatures:
    """由原始字段拼接 ``RecipeFeatures``（唯一构造入口，供批量查询路径复用）。

    Args:
        recipe_id: 菜谱 id。
        title: 菜名。
        cuisine: 菜系名（无则为空串）。
        tags: ``[(标签名, 标签类型)]``。
        ingredients: ``[(食材名, 是否主料)]``。
        prep_minutes / cook_minutes: 准备 / 烹饪耗时。
        difficulty: 难度 1~5。
        calories / protein: 整份营养值。
        servings: 份量。
        rating_avg / rating_count / hot_score: 统计字段。
        created_at: 创建时间（用于新鲜度）。
        now: 参照时间。

    Returns:
        ``RecipeFeatures``。
    """
    from django.utils import timezone

    now = now or timezone.now()
    tag_kinds: dict[str, list[str]] = {}
    tag_names: list[str] = []
    for name, kind in tags:
        if not name:
            continue
        tag_names.append(name)
        tag_kinds.setdefault(kind or "taste", []).append(name)

    normalized_ings = tuple(_normalize_name(name) for name, _ in ingredients if name)
    mains = tuple(_normalize_name(name) for name, is_main in ingredients if name and is_main)
    if not mains:
        mains = normalized_ings[:4]

    created = created_at or now
    hours = max((now - created).total_seconds() / 3600.0, 0.0)
    servings = max(int(servings or 1), 1)

    dish_spicy = 0
    for name in tag_kinds.get("taste", []):
        dish_spicy = max(dish_spicy, SPICY_TAG_LEVEL.get(name, 0))
    if any(word in ing for ing in normalized_ings for word in SPICY_WORDS):
        dish_spicy = max(dish_spicy, 2)

    return RecipeFeatures(
        id=int(recipe_id),
        title=title or "",
        cuisine=cuisine or "",
        tag_names=tuple(tag_names),
        tag_kinds={kind: tuple(names) for kind, names in tag_kinds.items()},
        ingredients=normalized_ings,
        main_ingredients=mains,
        total_minutes=int((prep_minutes or 0) + (cook_minutes or 0)),
        difficulty=int(difficulty or 2),
        calories_per_serving=round(float(calories or 0) / servings, 1),
        protein_per_serving=round(float(protein or 0) / servings, 1),
        servings=servings,
        rating_avg=float(rating_avg or 0),
        rating_count=int(rating_count or 0),
        hot_score=float(hot_score or 0),
        created_hours=hours,
        spicy_level=dish_spicy,
    )


def features_from_recipe(recipe: Recipe, now=None) -> RecipeFeatures:
    """把 ``Recipe`` 转成 ``RecipeFeatures``（只读已 prefetch 的关系，不额外发查询）。"""
    tag_pairs = [(tag.name, tag.kind) for tag in recipe.tags.all()]
    ing_pairs = [(item.name_text, bool(getattr(item, "is_main", False))) for item in recipe.ingredients.all()]
    cuisine = ""
    if recipe.cuisine_id:
        cuisine = getattr(getattr(recipe, "cuisine", None), "name", "") or ""
    return features_from_parts(
        recipe_id=int(recipe.pk),
        title=recipe.title or "",
        cuisine=cuisine,
        tags=tag_pairs,
        ingredients=ing_pairs,
        prep_minutes=int(getattr(recipe, "prep_minutes", 0) or 0),
        cook_minutes=int(getattr(recipe, "cook_minutes", 0) or 0),
        difficulty=int(getattr(recipe, "difficulty", 2) or 2),
        calories=float(getattr(recipe, "calories", 0) or 0),
        protein=float(getattr(recipe, "protein", 0) or 0),
        servings=int(getattr(recipe, "servings", 1) or 1),
        rating_avg=float(getattr(recipe, "rating_avg", 0) or 0),
        rating_count=int(getattr(recipe, "rating_count", 0) or 0),
        hot_score=float(getattr(recipe, "hot_score", 0) or 0),
        created_at=getattr(recipe, "created_at", None),
        now=now,
    )


# ---------------------------------------------------------------------------
# 画像构建
# ---------------------------------------------------------------------------
def _empty_profile() -> ContentProfile:
    """空画像（冷启动 / 匿名用户）。"""
    return ContentProfile()


def _positive_history(user) -> dict[int, float]:
    """收集用户的正向历史：``{recipe_id: 权重}``（评分≥4 / 收藏 / 做过）。"""
    from food.models import Favorite, InteractionLog, Rating

    weights: dict[int, float] = {}
    ratings = (
        Rating.objects.filter(user=user, score__gte=POSITIVE_RATING)
        .values_list("recipe_id", "score")[:HISTORY_LIMIT]
    )
    for recipe_id, score in ratings:
        weights[recipe_id] = weights.get(recipe_id, 0.0) + (float(score) - 3.0)

    favorites = Favorite.objects.filter(user=user).values_list("recipe_id", flat=True)[:HISTORY_LIMIT]
    for recipe_id in favorites:
        weights[recipe_id] = weights.get(recipe_id, 0.0) + FAVORITE_WEIGHT

    logs = (
        InteractionLog.objects.filter(user=user, action__in=["cook_done", "favorite", "rate"])
        .exclude(recipe__isnull=True)
        .values_list("recipe_id", "action", "extra")[:HISTORY_LIMIT]
    )
    for recipe_id, action, extra in logs:
        if action == "cook_done":
            increment = COOK_DONE_WEIGHT
        elif action == "favorite":
            increment = FAVORITE_WEIGHT
        else:  # rate
            score = float((extra or {}).get("score") or 0) if isinstance(extra, dict) else 0.0
            increment = max(score - 3.0, 0.0) if score else 1.0
        weights[recipe_id] = weights.get(recipe_id, 0.0) + increment
    return weights


def build_profile(user) -> ContentProfile:
    """构建用户内容画像。

    Args:
        user: ``User`` 实例；``None`` / 匿名用户返回空画像（冷启动）。

    Returns:
        ``ContentProfile``。异常（如未建 profile、脏 JSON）内部吞掉并降级，不向上抛。
    """
    profile = _empty_profile()
    if user is None or not getattr(user, "is_authenticated", False):
        return profile

    try:
        user_profile = UserProfile.objects.filter(user=user).first()
    except Exception:  # pragma: no cover - 极端脏数据
        logger.warning("读取 UserProfile 失败 user=%s", getattr(user, "pk", None), exc_info=True)
        user_profile = None

    if user_profile is not None:
        for cuisine in user_profile.preferred_cuisines.all():
            profile.cuisine_weights[cuisine.name] = (
                profile.cuisine_weights.get(cuisine.name, 0.0) + CUISINE_WEIGHT_PREF
            )
        for tag in user_profile.preferred_tags.all():
            profile.tag_weights[tag.name] = profile.tag_weights.get(tag.name, 0.0) + TAG_WEIGHT_PREF
        for raw in user_profile.disliked_ingredients or []:
            name = _normalize_name(str(raw))
            if name:
                profile.disliked.add(name)
        for raw in user_profile.diet_flags or []:
            flag = DIET_ALIASES.get(str(raw).strip().lower()) or DIET_ALIASES.get(str(raw).strip())
            if flag:
                profile.diet_flags.add(flag)
        profile.spicy_level = int(user_profile.spicy_level if user_profile.spicy_level is not None else -1)
        profile.avg_minutes = float(user_profile.max_cook_minutes or 0)

    # ---- 历史行为聚合 -------------------------------------------------
    try:
        history = _positive_history(user)
    except Exception:  # pragma: no cover - 历史表异常不影响主流程
        logger.warning("读取历史行为失败 user=%s", getattr(user, "pk", None), exc_info=True)
        history = {}

    if history:
        rows = (
            Recipe.objects.filter(id__in=list(history.keys()))
            .select_related("cuisine")
            .prefetch_related("tags", "ingredients")
        )
        total_weight = 0.0
        weighted_minutes = 0.0
        ingredient_counter: dict[str, float] = {}
        inferred_low_cal = 0.0
        for recipe in rows:
            weight = float(history.get(recipe.pk, 0.0))
            if weight <= 0:
                continue
            total_weight += weight
            if recipe.cuisine_id and recipe.cuisine:
                name = recipe.cuisine.name
                profile.cuisine_weights[name] = profile.cuisine_weights.get(name, 0.0) + weight
            for tag in recipe.tags.all():
                profile.tag_weights[tag.name] = profile.tag_weights.get(tag.name, 0.0) + weight * 0.6
            minutes = (recipe.prep_minutes or 0) + (recipe.cook_minutes or 0)
            if minutes:
                weighted_minutes += minutes * weight
            servings = max(recipe.servings or 1, 1)
            if (recipe.calories or 0) / servings <= 400:
                inferred_low_cal += weight
            for item in recipe.ingredients.all():
                key = _normalize_name(item.name_text)
                if key:
                    ingredient_counter[key] = ingredient_counter.get(key, 0.0) + weight

        if total_weight > 0 and weighted_minutes > 0:
            profile.avg_minutes = round(weighted_minutes / total_weight, 1)
        top_ingredients = sorted(ingredient_counter.items(), key=lambda kv: -kv[1])[:20]
        profile.ingredient_names = {name for name, _ in top_ingredients}
        # 历史里多数低卡 → 推断低卡偏好（仅在用户没显式标注时补充）
        if inferred_low_cal > total_weight * 0.6:
            profile.diet_flags.add("low_cal")

    if profile.avg_minutes <= 0:
        profile.avg_minutes = 45.0
    return profile


# ---------------------------------------------------------------------------
# 单菜谱打分
# ---------------------------------------------------------------------------
def _diet_scores(profile: ContentProfile, feat: RecipeFeatures) -> tuple[float, list[str]]:
    """饮食标识匹配：返回 (子分, 理由)。"""
    if not profile.diet_flags:
        return 0.0, []
    matched: list[str] = []
    scores: list[float] = []

    if "vegetarian" in profile.diet_flags:
        is_veg = not any(word in ing for ing in feat.ingredients for word in MEAT_WORDS)
        scores.append(1.0 if is_veg else 0.0)
        if is_veg:
            matched.append("素食友好")

    if "low_cal" in profile.diet_flags:
        cal = feat.calories_per_serving
        if cal <= 0:
            scores.append(0.5)
        else:
            scores.append(_clamp((450.0 - cal) / 250.0))
        if 0 < cal <= 400:
            matched.append("符合你的低卡偏好")

    if "high_protein" in profile.diet_flags:
        protein = feat.protein_per_serving
        scores.append(_clamp(protein / 25.0))
        if protein >= 18:
            matched.append("高蛋白管饱")

    if not scores:
        return 0.0, []
    return sum(scores) / len(scores), matched


def _spicy_score(profile: ContentProfile, feat: RecipeFeatures) -> float:
    """辣度匹配：未知返回 0.6（中性），比用户承受度高则降分。"""
    if profile.spicy_level < 0:
        return 0.6
    if feat.spicy_level <= 0:
        return 0.9 if profile.spicy_level >= 2 else 1.0
    diff = feat.spicy_level - profile.spicy_level
    if diff <= 0:
        return 1.0
    if diff == 1:
        return 0.6
    return 0.15


def _time_score(profile: ContentProfile, feat: RecipeFeatures) -> float:
    """耗时匹配：菜谱耗时相对用户可接受上限的线性衰减。"""
    budget = profile.avg_minutes or 45.0
    total = feat.total_minutes
    if total <= 0:
        return 0.5
    ratio = total / budget
    if ratio <= 0.7:
        return 1.0
    if ratio >= 1.6:
        return 0.0
    return _clamp((1.6 - ratio) / 0.9)


def _dislike_hit(profile: ContentProfile, feat: RecipeFeatures) -> str:
    """忌口命中检测，返回命中的食材名（空串表示未命中）。

    双向子串匹配：忌口「花生」命中「花生米」，忌口「花生米」也命中「花生」。
    """
    if not profile.disliked:
        return ""
    for disliked in profile.disliked:
        for ing in feat.ingredients:
            if not ing or not disliked:
                continue
            if disliked in ing or ing in disliked:
                return disliked
    return ""


def score_features(profile: ContentProfile, feat: RecipeFeatures) -> tuple[float, list[str]]:
    """给已抽取特征的菜谱打分（0~1）并生成中文理由。

    Args:
        profile: 用户内容画像。
        feat: 菜谱特征。

    Returns:
        ``(score, reasons)``；忌口命中时 ``score == 0.0`` 且理由为提示文案。
    """
    hit = _dislike_hit(profile, feat)
    if hit:
        return 0.0, [f"含你忌口的{hit}"]

    reasons: list[str] = []
    signals: dict[str, tuple[float, float]] = {}  # name -> (子分, 权重)

    # 1) 质量基线：评分 + 热度（始终可用）
    quality = _clamp(feat.rating_avg / 5.0) if feat.rating_count else 0.35
    signals["quality"] = (quality, SIGNAL_WEIGHTS["quality"])
    if feat.rating_avg >= 4.5 and feat.rating_count >= 5:
        reasons.append(f"评分{feat.rating_avg:.1f}分好评")

    # 2) 菜系偏好
    if profile.cuisine_weights:
        max_w = max(profile.cuisine_weights.values()) or 1.0
        ratio = profile.cuisine_weights.get(feat.cuisine, 0.0) / max_w
        signals["cuisine"] = (_clamp(ratio), SIGNAL_WEIGHTS["cuisine"])
        if ratio >= 0.5 and feat.cuisine:
            reasons.append(f"你常吃{feat.cuisine}")

    # 3) 标签偏好
    if profile.tag_weights:
        max_w = max(profile.tag_weights.values()) or 1.0
        matched_tags = [t for t in feat.tag_names if t in profile.tag_weights]
        matched_w = sum(profile.tag_weights[t] for t in matched_tags)
        ratio = _clamp(matched_w / (max_w * 1.5))
        signals["tag"] = (ratio if matched_tags else 0.0, SIGNAL_WEIGHTS["tag"])
        if matched_tags:
            reasons.append("符合你的口味：" + "、".join(matched_tags[:2]))

    # 4) 食材亲和度
    if profile.ingredient_names:
        overlap = set(feat.main_ingredients or feat.ingredients) & profile.ingredient_names
        denom = max(min(len(feat.main_ingredients or feat.ingredients), 3), 1)
        signals["ingredient"] = (_clamp(len(overlap) / denom), SIGNAL_WEIGHTS["ingredient"])
        if overlap:
            reasons.append("常吃的食材：" + "、".join(list(overlap)[:2]))

    # 5) 辣度
    signals["spicy"] = (_spicy_score(profile, feat), SIGNAL_WEIGHTS["spicy"])
    if profile.spicy_level >= 3 and feat.spicy_level >= 3:
        reasons.append("够辣够过瘾")
    elif profile.spicy_level == 0 and feat.spicy_level >= 3:
        reasons.append("偏辣，建议减辣")

    # 6) 耗时
    signals["time"] = (_time_score(profile, feat), SIGNAL_WEIGHTS["time"])
    if feat.total_minutes and feat.total_minutes <= (profile.avg_minutes or 45.0) * 0.7:
        reasons.append(f"{feat.total_minutes} 分钟快手菜")

    # 7) 饮食标识
    diet_score, diet_reasons = _diet_scores(profile, feat)
    if diet_reasons or profile.diet_flags:
        signals["diet"] = (diet_score, SIGNAL_WEIGHTS["diet"])
        reasons.extend(diet_reasons)

    total = sum(score * weight for score, weight in signals.values())
    applied = sum(weight for _, weight in signals.values()) or 1.0
    final = _clamp(total / applied)

    # 去重且最多 3 条理由
    unique: list[str] = []
    for reason in reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return round(final, 4), unique[:3]


def score_recipe(profile: ContentProfile, recipe: Recipe) -> tuple[float, list[str]]:
    """给单条 ``Recipe`` 打内容分。

    Args:
        profile: ``build_profile`` 的产物。
        recipe: 菜谱实例（建议已 prefetch ``tags`` / ``ingredients`` / ``cuisine``）。

    Returns:
        ``(0~1 分数, 中文理由列表)``。
    """
    try:
        return score_features(profile, features_from_recipe(recipe))
    except Exception:  # pragma: no cover - 脏数据兜底
        logger.warning("内容打分失败 recipe=%s", getattr(recipe, "pk", None), exc_info=True)
        return 0.0, []


# ---------------------------------------------------------------------------
# 相似菜谱
# ---------------------------------------------------------------------------
def _sparse_vector(feat: RecipeFeatures) -> dict[str, float]:
    """把特征转成稀疏向量（维度 = 标签 / 菜系 / 食材 / 标题 char bigram）。"""
    vector: dict[str, float] = {}
    for name in feat.tag_names:
        vector[f"tag:{name}"] = 1.0
    if feat.cuisine:
        vector[f"cuisine:{feat.cuisine}"] = 1.0
    for name in feat.main_ingredients:
        vector[f"ing:{name}"] = 0.8
    for name in feat.ingredients:
        vector.setdefault(f"ing:{name}", 0.4)
    title = (feat.title or "").strip()
    for i in range(len(title) - 1):
        gram = title[i : i + 2]
        vector[f"ch:{gram}"] = 0.5
    return vector


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """稀疏余弦相似度。"""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(weight * b.get(key, 0.0) for key, weight in a.items())
    if dot <= 0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a * norm_b)


def similar_recipes(recipe: Recipe, k: int = 6) -> list[tuple[Recipe, float]]:
    """找与给定菜谱内容相似的菜谱（标签 / 食材 / 菜系 / 标题文本）。

    Args:
        recipe: 基准菜谱（需 ``pk``；建议已 prefetch 关系）。
        k: 返回条数。

    Returns:
        ``[(Recipe, 相似度 0~1)]``，按相似度降序，已排除自身。
    """
    if recipe is None or getattr(recipe, "pk", None) is None:
        return []
    k = max(int(k or 0), 0)
    if k == 0:
        return []
    base_feat = features_from_recipe(recipe)
    base_vec = _sparse_vector(base_feat)

    tag_ids = list(recipe.tags.values_list("id", flat=True))
    qs = Recipe.objects.published().exclude(pk=recipe.pk)
    if tag_ids or recipe.cuisine_id:
        condition = Q(pk__in=[])
        if tag_ids:
            condition |= Q(tags__in=tag_ids)
        if recipe.cuisine_id:
            condition |= Q(cuisine_id=recipe.cuisine_id)
        qs = qs.filter(condition).distinct()
    candidates = list(
        qs.select_related("cuisine").prefetch_related("tags", "ingredients").order_by("-hot_score")[
            :MAX_CANDIDATES
        ]
    )

    scored: list[tuple[Recipe, float]] = []
    for obj in candidates:
        sim = _cosine(base_vec, _sparse_vector(features_from_recipe(obj)))
        if sim > 0:
            scored.append((obj, round(sim, 4)))

    # 不足 k 条时用热门补齐（保证接口总能给出结果）
    if len(scored) < k:
        seen = {recipe.pk} | {obj.pk for obj, _ in scored}
        filler = (
            Recipe.objects.published()
            .exclude(pk__in=seen)
            .select_related("cuisine")
            .prefetch_related("tags", "ingredients")
            .order_by("-hot_score")[: k - len(scored)]
        )
        scored.extend((obj, 0.05) for obj in filler)

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]


def profile_summary(profile: ContentProfile) -> dict[str, object]:
    """画像的可序列化摘要（调试 / 运营后台展示用）。"""
    top_cuisines = sorted(profile.cuisine_weights.items(), key=lambda kv: -kv[1])[:3]
    top_tags = sorted(profile.tag_weights.items(), key=lambda kv: -kv[1])[:5]
    return {
        "cuisines": [{"name": n, "weight": round(w, 2)} for n, w in top_cuisines],
        "tags": [{"name": n, "weight": round(w, 2)} for n, w in top_tags],
        "ingredients": sorted(profile.ingredient_names)[:10],
        "disliked": sorted(profile.disliked),
        "avg_minutes": profile.avg_minutes,
        "spicy_level": profile.spicy_level,
        "diet_flags": sorted(profile.diet_flags),
        "has_signals": profile.has_signals,
    }


def sequence_keywords(items: Sequence[str], limit: int = 3) -> str:
    """把标签序列拼成中文短语（理由文案复用）。"""
    return "、".join([i for i in items if i][:limit])
