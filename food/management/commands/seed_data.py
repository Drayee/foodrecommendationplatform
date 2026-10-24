"""种子数据导入命令：`python manage.py seed_data`。

职责
----
1. 从 ``data/seed/*.json`` 读取菜系 / 标签 / 食材 / 菜谱 / 演示用户；
2. 幂等落库（菜系按 name、标签按 (name, kind)、食材按 name、菜谱按 title 做 ``update_or_create``）；
3. 回填菜谱检索文本（``refresh_search_doc``）与热度分；
4. 用固定随机种子生成演示行为（评分 / 收藏 / 行为埋点），时间散布在近 30 天，
   保证协同过滤与热度榜有信号，且重复执行不叠加数据。

用法::

    python manage.py seed_data                  # 幂等增量灌数据
    python manage.py seed_data --reset          # 清空业务数据后重灌
    python manage.py seed_data --skip-users     # 只灌菜谱主数据
    python manage.py seed_data --no-demo-behavior   # 不生成演示行为

设计约束
--------
- 不修改 ``food/models.py``；所有字段名严格对齐模型定义；
- ``recommend.hot`` 可能尚未就绪：导入失败时退化为本地简易热度公式，命令绝不因此崩溃；
- 所有时间字段（``created_at`` 为 auto_now_add）在 ``bulk_create`` 之后手动 UPDATE 回填。
"""
from __future__ import annotations

import json
import logging
import random
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from food.models import (
    Comment,
    Cuisine,
    Favorite,
    Ingredient,
    InteractionLog,
    Rating,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    Tag,
    UserProfile,
)

logger = logging.getLogger(__name__)

# 允许的用量单位（与数据规格保持一致）
ALLOWED_UNITS = {"克", "毫升", "汤匙", "茶匙", "个", "根", "瓣", "片", "只", "碗"}
# 演示行为固定随机种子：保证多次运行分布一致，便于测试与演示复现
BEHAVIOR_SEED = 20240501
# 目标行为条数（不足时用 click / cook_done 补齐，超出时截断）
TARGET_INTERACTIONS = 208
# 行为权重（与 recommend 契约保持一致）
ACTION_WEIGHTS = {"view": 1.0, "click": 1.5, "favorite": 3.0, "cook_done": 4.0}


def _load_json(data_dir: Path, filename: str) -> list[dict[str, Any]]:
    """读取种子 JSON；文件缺失或格式错误直接报错，避免静默灌入半套数据。"""
    path = data_dir / filename
    if not path.exists():
        raise CommandError(f"种子文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:  # noqa: TRY003
        raise CommandError(f"JSON 解析失败 {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise CommandError(f"{path} 顶层必须是数组")
    return payload


def _split_alias(alias: str) -> Iterable[str]:
    """拆分别名：支持中英文逗号 / 顿号 / 斜杠 / 分号。"""
    for part in re.split(r"[,，、/;；]", alias or ""):
        part = part.strip()
        if part:
            yield part


def build_ingredient_index() -> dict[str, Ingredient]:
    """建立「食材名 / 别名 → Ingredient」索引，供菜谱用量解析。"""
    index: dict[str, Ingredient] = {}
    for ingredient in Ingredient.objects.all():
        index[ingredient.name] = ingredient
        for alias in _split_alias(ingredient.alias):
            index.setdefault(alias, ingredient)
    return index


# ---------------------------------------------------------------------------
# 主数据
# ---------------------------------------------------------------------------
def seed_cuisines(rows: list[dict[str, Any]]) -> int:
    """写入菜系，返回处理条数。"""
    for row in rows:
        Cuisine.objects.update_or_create(
            name=row["name"],
            defaults={
                "region": row.get("region", ""),
                "description": row.get("description", ""),
                "icon": row.get("icon", "🍽️"),
                "sort_order": int(row.get("sort_order", 0)),
            },
        )
    return len(rows)


def seed_tags(rows: list[dict[str, Any]]) -> int:
    """写入标签（按 name + kind 唯一）。"""
    for row in rows:
        Tag.objects.update_or_create(
            name=row["name"],
            kind=row.get("kind", Tag.Kind.TASTE),
            defaults={"color": row.get("color", "")},
        )
    return len(rows)


def seed_ingredients(rows: list[dict[str, Any]]) -> int:
    """写入食材主数据（含每 100g 营养）。"""
    for row in rows:
        Ingredient.objects.update_or_create(
            name=row["name"],
            defaults={
                "alias": row.get("alias", ""),
                "category": row.get("category", Ingredient.Category.OTHER),
                "unit": row.get("unit", "克"),
                "calories_per_100g": float(row.get("calories_per_100g", 0) or 0),
                "protein_per_100g": float(row.get("protein_per_100g", 0) or 0),
                "fat_per_100g": float(row.get("fat_per_100g", 0) or 0),
                "carbs_per_100g": float(row.get("carbs_per_100g", 0) or 0),
                "is_common": bool(row.get("is_common", False)),
            },
        )
    return len(rows)


def seed_recipes(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """写入菜谱及其用量、步骤。

    返回 ``(菜谱数, 未解析食材数)``；未解析食材保留 ``name_text`` 与 ``ingredient=None``，
    便于后续人工补录，不阻断灌数据。
    """
    cuisines = {c.name: c for c in Cuisine.objects.all()}
    tags = {(t.name, t.kind): t for t in Tag.objects.all()}
    tag_by_name: dict[str, Tag] = {}
    for (name, _kind), tag in tags.items():
        tag_by_name.setdefault(name, tag)
    ingredients = build_ingredient_index()

    unresolved = 0
    for row in rows:
        title = row["title"]
        cuisine = cuisines.get(row.get("cuisine", ""))
        if cuisine is None and row.get("cuisine"):
            logger.warning("菜系缺失：%s（菜品 %s）", row.get("cuisine"), title)

        recipe, _created = Recipe.objects.update_or_create(
            title=title,
            defaults={
                "cuisine": cuisine,
                "summary": row.get("summary", ""),
                "difficulty": int(row.get("difficulty", 2)),
                "prep_minutes": int(row.get("prep_minutes", 10)),
                "cook_minutes": int(row.get("cook_minutes", 20)),
                "servings": int(row.get("servings", 2)),
                "calories": float(row.get("calories", 0) or 0),
                "protein": float(row.get("protein", 0) or 0),
                "fat": float(row.get("fat", 0) or 0),
                "carbs": float(row.get("carbs", 0) or 0),
                "tips": row.get("tips", ""),
                "status": Recipe.Status.PUBLISHED,
                "source": Recipe.Source.SEED,
            },
        )

        # 标签：只绑定已存在的标签，缺失项记录 warning
        bound_tags = []
        for tag_name in row.get("tags", []):
            tag = tag_by_name.get(tag_name)
            if tag is None:
                logger.warning("标签缺失：%s（菜品 %s）", tag_name, title)
                continue
            bound_tags.append(tag)
        recipe.tags.set(bound_tags)

        # 用量：先清空再重建，保证重复执行不产生重复行
        recipe.ingredients.all().delete()
        payload = []
        for order, item in enumerate(row.get("ingredients", [])):
            name = item["name"]
            unit = item.get("unit", "克")
            if unit and unit not in ALLOWED_UNITS:
                logger.warning("非常规单位：%s（菜品 %s）", unit, title)
            matched = ingredients.get(name)
            if matched is None:
                unresolved += 1
                logger.warning("食材未收录：%s（菜品 %s）", name, title)
            payload.append(
                RecipeIngredient(
                    recipe=recipe,
                    ingredient=matched,
                    name_text=name,
                    amount=str(item.get("amount", "")),
                    unit=unit,
                    note=item.get("note", ""),
                    is_main=bool(item.get("is_main", False)),
                    sort_order=order,
                )
            )
        if payload:
            RecipeIngredient.objects.bulk_create(payload)

        # 步骤：先清空再重建；duration_min 按烹饪时长均摊作为估算值
        recipe.steps.all().delete()
        step_texts = [str(s).strip() for s in row.get("steps", []) if str(s).strip()]
        if step_texts:
            cook = max(int(row.get("cook_minutes", 20)), 1)
            per_step = round(cook / len(step_texts), 1)
            RecipeStep.objects.bulk_create(
                [
                    RecipeStep(recipe=recipe, index=i, text=text, duration_min=per_step)
                    for i, text in enumerate(step_texts, start=1)
                ]
            )
    return len(rows), unresolved


# ---------------------------------------------------------------------------
# 演示用户与行为
# ---------------------------------------------------------------------------
def seed_users(rows: list[dict[str, Any]]) -> dict[str, int]:
    """写入演示用户、偏好画像、评分与收藏。

    返回 ``{"users": n, "ratings": n, "favorites": n}``。
    """
    recipes = {r.title: r for r in Recipe.objects.all()}
    cuisines = {c.name: c for c in Cuisine.objects.all()}
    tag_by_name: dict[str, Tag] = {}
    for tag in Tag.objects.all():
        tag_by_name.setdefault(tag.name, tag)

    stat = {"users": 0, "ratings": 0, "favorites": 0}
    for row in rows:
        email = row["email"]
        user, _ = User.objects.get_or_create(
            username=email,
            defaults={"email": email, "first_name": row.get("nickname", "")[:30]},
        )
        # 演示账号不设可用密码，避免被当成真实登录入口
        if user.has_usable_password():
            user.set_unusable_password()
        user.email = email
        user.is_active = True
        user.save(update_fields=["email", "is_active", "password"])

        profile, _ = UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "nickname": row.get("nickname", ""),
                "bio": row.get("bio", ""),
                "spicy_level": int(row.get("spicy_level", 2)),
                "max_cook_minutes": int(row.get("max_cook_minutes", 60)),
                "diet_flags": list(row.get("diet_flags", [])),
                "disliked_ingredients": list(row.get("disliked_ingredients", [])),
                "onboarded": True,
            },
        )
        profile.preferred_cuisines.set(
            [cuisines[n] for n in row.get("preferred_cuisines", []) if n in cuisines]
        )
        profile.preferred_tags.set(
            [tag_by_name[n] for n in row.get("preferred_tags", []) if n in tag_by_name]
        )
        stat["users"] += 1

        for item in row.get("ratings", []):
            recipe = recipes.get(item["recipe_title"])
            if recipe is None:
                logger.warning("评分目标菜谱不存在：%s", item["recipe_title"])
                continue
            Rating.objects.update_or_create(
                user=user,
                recipe=recipe,
                defaults={"score": max(1, min(5, int(item["score"])))},
            )
            stat["ratings"] += 1

        for title in row.get("favorites", []):
            recipe = recipes.get(title)
            if recipe is None:
                logger.warning("收藏目标菜谱不存在：%s", title)
                continue
            Favorite.objects.get_or_create(user=user, recipe=recipe)
            stat["favorites"] += 1
    return stat


def seed_interactions(rows: list[dict[str, Any]]) -> int:
    """生成演示行为埋点。

    构造规则（确定性、可复现）：
    - 每条评分 → 一次 ``view``；
    - 每条收藏 → 一次 ``favorite``；
    - 评分 ≥4 的菜谱补 ``click``，评分 =5 的补 ``cook_done``，直到凑满 ``TARGET_INTERACTIONS``；
    - 时间在近 30 天内随机散布，并通过 UPDATE 回填 ``created_at``。
    """
    rng = random.Random(BEHAVIOR_SEED)
    recipes = {r.title: r for r in Recipe.objects.all()}
    now = timezone.now()

    base: list[tuple[str, int, int, str]] = []  # (email, user_id, recipe_id, action)
    extras: list[tuple[str, int, int, str]] = []
    for row in rows:
        email = row["email"]
        user = User.objects.filter(username=email).first()
        if user is None:
            continue
        user_id = user.id
        for item in row.get("ratings", []):
            recipe = recipes.get(item["recipe_title"])
            if recipe is None:
                continue
            base.append((email, user_id, recipe.id, "view"))
            score = int(item["score"])
            if score >= 4:
                extras.append((email, user_id, recipe.id, "click"))
            if score == 5:
                extras.append((email, user_id, recipe.id, "cook_done"))
        for title in row.get("favorites", []):
            recipe = recipes.get(title)
            if recipe is None:
                continue
            base.append((email, user_id, recipe.id, "favorite"))

    # 用固定种子打散补齐池，避免所有 click 集中在同一用户；
    # cook_done（权重 4）比 click（权重 1.5）信号更强，补齐时优先使用，
    # 让演示数据里同时存在「做过这道菜」的正反馈。
    rng.shuffle(extras)
    extras.sort(key=lambda event: 0 if event[3] == "cook_done" else 1)
    events = base[:TARGET_INTERACTIONS]
    for event in extras:
        if len(events) >= TARGET_INTERACTIONS:
            break
        events.append(event)

    # 先清掉本批演示用户的历史行为，保证幂等
    if events:
        InteractionLog.objects.filter(
            session_key="seed", user_id__in={e[1] for e in events}
        ).delete()

    objects: list[InteractionLog] = []
    stamps: list[Any] = []
    for email, user_id, recipe_id, action in events:
        # 时间：近 30 天内随机散布
        created_at = now - timedelta(minutes=rng.randint(5, 30 * 24 * 60))
        objects.append(
            InteractionLog(
                user_id=user_id,
                session_key="seed",
                recipe_id=recipe_id,
                action=action,
                weight=ACTION_WEIGHTS.get(action, 1.0),
                extra={"source": "seed"},
            )
        )
        stamps.append(created_at)

    created = InteractionLog.objects.bulk_create(objects)
    # bulk_create 会先按 auto_now_add 写入「当前时间」，这里把真实发生时间回填。
    # 用 bulk_update 生成标准的 CASE WHEN 语句（PostgreSQL / MySQL / SQLite 通用，
    # 不含 strftime()、datetime() 等 SQLite 专有函数）；万一后端不支持或行数对不上，
    # 再退化成逐条 QuerySet.update()，只更新 created_at 单列，仍未触碰其他字段。
    for obj, created_at in zip(created, stamps, strict=False):
        obj.created_at = created_at
    try:
        InteractionLog.objects.bulk_update(created, ["created_at"], batch_size=200)
    except Exception as exc:  # noqa: BLE001 - 回填失败必须降级，不能让命令崩
        logger.warning("bulk_update 回填 created_at 失败（%s），退化为逐条 UPDATE", exc)
        for obj in created:
            InteractionLog.objects.filter(pk=obj.pk).update(created_at=obj.created_at)
    return len(created)


# ---------------------------------------------------------------------------
# 派生字段：检索文本 / 热度 / 评分聚合
# ---------------------------------------------------------------------------
def refresh_search_docs(batch_size: int = 100) -> int:
    """为所有已发布菜谱重建检索文本，返回处理条数。"""
    count = 0
    queryset = Recipe.objects.published().prefetch_related("tags", "ingredients", "steps", "cuisine")
    for recipe in queryset.iterator(chunk_size=batch_size):
        recipe.refresh_search_doc(save=True)
        count += 1
    return count


def recompute_hot_scores_fallback() -> int:
    """本地简易热度：engagement / (小时数 + 2)^1.5，时间衰减与 recommend 契约同形。"""
    now = timezone.now()
    updated = 0
    for recipe in Recipe.objects.published():
        age_hours = max((now - recipe.created_at).total_seconds() / 3600.0, 0.0)
        engagement = (
            recipe.view_count
            + 2.0 * recipe.favorite_count
            + 3.0 * recipe.rating_count
            + max(recipe.rating_avg - 3.0, 0.0) * 5.0
        )
        recipe.hot_score = round(engagement / ((age_hours + 2.0) ** 1.5), 6)
        recipe.save(update_fields=["hot_score", "updated_at"])
        updated += 1
    return updated


def refresh_hot_scores() -> tuple[int, str]:
    """优先调用 ``recommend.hot.recompute_hot_scores``，不可用时退化本地实现。"""
    try:  # 推荐模块由其他同学并行开发，可能还不存在
        from recommend.hot import recompute_hot_scores  # noqa: PLC0415

        return int(recompute_hot_scores()), "recommend.hot"
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须降级，命令不能崩
        logger.warning("recommend.hot 不可用（%s），改用本地热度公式", exc)
        return recompute_hot_scores_fallback(), "fallback"


def refresh_rating_stats() -> int:
    """重算所有菜谱的 rating_avg / rating_count 与收藏数。"""
    count = 0
    for recipe in Recipe.objects.published():
        recipe.recompute_rating()
        favorite_count = recipe.favorites.count()
        if favorite_count != recipe.favorite_count:
            recipe.favorite_count = favorite_count
            recipe.save(update_fields=["favorite_count", "updated_at"])
        count += 1
    return count


# ---------------------------------------------------------------------------
# 清库
# ---------------------------------------------------------------------------
def reset_business_data(demo_emails: Iterable[str]) -> None:
    """清空业务数据（主数据 + 演示用户及其行为），保留超级用户与检索日志。

    删除顺序严格按「依赖方 → 被依赖方」排列，PostgreSQL 的外键约束（含
    ``NO ACTION`` 立即检查）不会被触发；M2M 中间表（``food_recipe_tags``、
    ``food_userprofile_preferred_cuisines`` 等）由 Django Collector 自动清理，
    不依赖 ``TRUNCATE ... CASCADE`` 这类 PG 专有语法，SQLite / MySQL 同样可跑。
    """
    with transaction.atomic():
        # 1) 行为与统计：只依赖 User / Recipe
        InteractionLog.objects.all().delete()
        Favorite.objects.all().delete()
        Rating.objects.all().delete()
        Comment.objects.all().delete()
        # 2) 菜谱子表 → 菜谱本体（向量表随 Recipe 级联删除）
        RecipeStep.objects.all().delete()
        RecipeIngredient.objects.all().delete()
        Recipe.objects.all().delete()
        # 3) 主数据：Tag / Cuisine 的 M2M 中间行由 Collector 一并删除
        Tag.objects.all().delete()
        Cuisine.objects.all().delete()
        Ingredient.objects.all().delete()
        # 4) 演示用户（UserProfile / ChatSession 级联删除），不碰超级用户
        emails = list(demo_emails)
        if emails:
            User.objects.filter(username__in=emails, is_superuser=False).delete()


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
class Command(BaseCommand):
    help = "导入种子数据（菜系 / 标签 / 食材 / 菜谱 / 演示用户与行为）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reset", action="store_true", help="清空业务数据后重新灌入")
        parser.add_argument("--skip-users", action="store_true", help="跳过演示用户与行为数据")
        parser.add_argument(
            "--with-demo-behavior",
            dest="demo_behavior",
            action="store_true",
            default=True,
            help="生成演示评分 / 收藏 / 行为埋点（默认开启）",
        )
        parser.add_argument(
            "--no-demo-behavior",
            dest="demo_behavior",
            action="store_false",
            help="只建演示用户与偏好，不生成评分 / 收藏 / 行为",
        )
        parser.add_argument("--data-dir", default="", help="种子数据目录，默认 data/seed")

    def handle(self, *args, **options) -> None:  # noqa: C901 - 流程线性，集中在一个入口便于阅读
        data_dir = Path(options["data_dir"]) if options["data_dir"] else Path(settings.BASE_DIR) / "data" / "seed"
        rows_cuisine = _load_json(data_dir, "cuisines.json")
        rows_tag = _load_json(data_dir, "tags.json")
        rows_ingredient = _load_json(data_dir, "ingredients.json")
        rows_recipe = _load_json(data_dir, "recipes.json")
        # --skip-users 时允许 users.json 暂缺（便于先灌主数据、后补演示用户）
        if options["skip_users"] and not (data_dir / "users.json").exists():
            rows_user = []
        else:
            rows_user = _load_json(data_dir, "users.json")

        demo_emails = [row["email"] for row in rows_user]
        with transaction.atomic():
            if options["reset"]:
                reset_business_data(demo_emails)
                self.stdout.write(self.style.WARNING("已清空业务数据（--reset）"))

            n_cuisine = seed_cuisines(rows_cuisine)
            n_tag = seed_tags(rows_tag)
            n_ingredient = seed_ingredients(rows_ingredient)
            n_recipe, unresolved = seed_recipes(rows_recipe)

            n_users = n_ratings = n_favorites = n_interactions = 0
            if options["skip_users"]:
                self.stdout.write(self.style.WARNING("已跳过演示用户（--skip-users）"))
            else:
                user_stat = seed_users(rows_user)
                n_users = user_stat["users"]
                n_ratings = user_stat["ratings"]
                n_favorites = user_stat["favorites"]
                if options["demo_behavior"]:
                    n_interactions = seed_interactions(rows_user)
                else:
                    self.stdout.write(
                        self.style.WARNING("已跳过演示行为生成（--no-demo-behavior），历史行为保留不动")
                    )

            n_doc = refresh_search_docs()
            refresh_rating_stats()
            n_hot, hot_source = refresh_hot_scores()

        if unresolved:
            self.stdout.write(
                self.style.WARNING(f"有 {unresolved} 条用量未能匹配食材主数据（已保留名称文本）")
            )
        self.stdout.write(f"检索文本已重建：{n_doc} 条；热度来源：{hot_source}")
        self.stdout.write(
            "菜品 {recipe} / 菜系 {cuisine} / 标签 {tag} / 食材 {ingredient} / 用户 {user} / "
            "评分 {rating} / 收藏 {favorite} / 行为 {interaction}".format(
                recipe=n_recipe,
                cuisine=n_cuisine,
                tag=n_tag,
                ingredient=n_ingredient,
                user=n_users,
                rating=n_ratings,
                favorite=n_favorites,
                interaction=n_interactions,
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"种子数据导入完成：Recipe={Recipe.objects.count()} 条，"
                f"hot_score 更新 {n_hot} 条"
            )
        )
