"""种子数据与 ``seed_data`` 命令的测试。

覆盖三类约束
------------
1. **JSON 结构**：必填字段、枚举合法、菜系覆盖、食材可解析、步骤数与营养值合理；
2. **命令幂等**：``seed_data`` 连续执行两次，业务表数量不变；
3. **派生字段**：``Recipe.recompute_rating()`` 的均值/人数聚合正确，
   灌数据后 ``search_doc`` 非空、``hot_score`` 已回写。
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from food.models import (
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

SEED_DIR = Path(settings.BASE_DIR) / "data" / "seed"
ALLOWED_UNITS = {"克", "毫升", "汤匙", "茶匙", "个", "根", "瓣", "片", "只", "碗"}
EXPECTED_CUISINES = {
    "川菜",
    "粤菜",
    "鲁菜",
    "苏菜",
    "浙菜",
    "闽菜",
    "湘菜",
    "徽菜",
    "东北菜",
    "西北菜",
    "家常菜",
    "西餐",
}
INGREDIENT_CATEGORIES = {c.value for c in Ingredient.Category}
TAG_KINDS = {k.value for k in Tag.Kind}


def load_seed(name: str) -> list[dict]:
    """读取种子 JSON。"""
    with (SEED_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


class SeedJsonStructureTest(SimpleTestCase):
    """直接校验 data/seed/*.json 的结构与业务约束（不依赖数据库）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cuisines = load_seed("cuisines.json")
        cls.tags = load_seed("tags.json")
        cls.ingredients = load_seed("ingredients.json")
        cls.recipes = load_seed("recipes.json")
        cls.users = load_seed("users.json")
        # 食材名 + 别名 → 规范名
        cls.ingredient_index: dict[str, str] = {}
        for item in cls.ingredients:
            cls.ingredient_index[item["name"]] = item["name"]
            for alias in str(item.get("alias", "")).split(","):
                alias = alias.strip()
                if alias:
                    cls.ingredient_index[alias] = item["name"]

    # -- 菜系 --------------------------------------------------------------
    def test_cuisine_count_and_fields(self):
        self.assertEqual(len(self.cuisines), 12)
        self.assertEqual({c["name"] for c in self.cuisines}, EXPECTED_CUISINES)
        for c in self.cuisines:
            for key in ("name", "region", "description", "icon", "sort_order"):
                self.assertIn(key, c)
            # 简介 30~60 字
            self.assertGreaterEqual(len(c["description"]), 30)
            self.assertLessEqual(len(c["description"]), 60)
            self.assertTrue(c["icon"])
            self.assertIsInstance(c["sort_order"], int)

    # -- 标签 --------------------------------------------------------------
    def test_tag_count_kind_and_color(self):
        self.assertGreaterEqual(len(self.tags), 30)
        kinds = {t["kind"] for t in self.tags}
        self.assertEqual(kinds, TAG_KINDS)
        for t in self.tags:
            self.assertIn(t["kind"], TAG_KINDS)
            self.assertRegex(t["color"], r"^#[0-9a-fA-F]{6}$")
        # (name, kind) 不能重复
        pairs = [(t["name"], t["kind"]) for t in self.tags]
        self.assertEqual(len(pairs), len(set(pairs)))

    # -- 食材 --------------------------------------------------------------
    def test_ingredient_fields_and_nutrition(self):
        self.assertGreaterEqual(len(self.ingredients), 60)
        names = [i["name"] for i in self.ingredients]
        self.assertEqual(len(names), len(set(names)))
        for item in self.ingredients:
            for key in (
                "name",
                "alias",
                "category",
                "unit",
                "calories_per_100g",
                "protein_per_100g",
                "fat_per_100g",
                "carbs_per_100g",
                "is_common",
            ):
                self.assertIn(key, item)
            self.assertIn(item["category"], INGREDIENT_CATEGORIES)
            self.assertGreaterEqual(item["calories_per_100g"], 0)
            for macro in ("protein_per_100g", "fat_per_100g", "carbs_per_100g"):
                self.assertGreaterEqual(item[macro], 0)
            # 每 100g 三大营养素供能不应超过 900kcal（纯油脂上限）
            energy = 4 * item["protein_per_100g"] + 9 * item["fat_per_100g"] + 4 * item["carbs_per_100g"]
            self.assertLessEqual(energy, 950, f"{item['name']} 营养数据异常：{energy:.0f}kcal")

    # -- 菜谱 --------------------------------------------------------------
    def test_recipe_count_and_cuisine_coverage(self):
        self.assertGreaterEqual(len(self.recipes), 60)
        counter: dict[str, int] = {}
        for r in self.recipes:
            counter[r["cuisine"]] = counter.get(r["cuisine"], 0) + 1
        self.assertEqual(set(counter), EXPECTED_CUISINES, "菜系未全部覆盖")
        for cuisine, n in counter.items():
            self.assertGreaterEqual(n, 3, f"{cuisine} 只有 {n} 道菜")

    def test_recipe_required_fields(self):
        required = {
            "title",
            "cuisine",
            "tags",
            "summary",
            "difficulty",
            "prep_minutes",
            "cook_minutes",
            "servings",
            "calories",
            "protein",
            "fat",
            "carbs",
            "ingredients",
            "steps",
            "tips",
        }
        titles = [r["title"] for r in self.recipes]
        self.assertEqual(len(titles), len(set(titles)), "菜名重复")
        for r in self.recipes:
            missing = required - set(r)
            self.assertFalse(missing, f"{r.get('title')} 缺字段 {missing}")
            self.assertIn(r["cuisine"], EXPECTED_CUISINES)
            self.assertTrue(r["summary"].strip())
            self.assertGreaterEqual(len(r["summary"]), 30, f"{r['title']} summary 太短")
            self.assertLessEqual(len(r["summary"]), 70, f"{r['title']} summary 太长")
            self.assertTrue(r["tips"].strip())

    def test_recipe_enum_and_numbers(self):
        for r in self.recipes:
            self.assertGreaterEqual(r["difficulty"], 1)
            self.assertGreaterEqual(r["prep_minutes"], 0)
            # 凉拌类不需要开火，cook_minutes 允许为 0；但整道菜的总耗时必须合理
            self.assertGreaterEqual(r["cook_minutes"], 0)
            self.assertGreaterEqual(r["prep_minutes"] + r["cook_minutes"], 5)
            self.assertGreater(r["servings"], 0)
            for key in ("calories", "protein", "fat", "carbs"):
                self.assertGreater(r[key], 0, f"{r['title']} 的 {key} 必须为正")
            # 热量应与三大营养素供能大致自洽（容差 12% 或 60kcal）
            calc = 4 * r["protein"] + 9 * r["fat"] + 4 * r["carbs"]
            tolerance = max(60, r["calories"] * 0.12)
            self.assertLessEqual(
                abs(calc - r["calories"]),
                tolerance,
                f"{r['title']} 热量 {r['calories']} 与 4P+9F+4C={calc:.0f} 偏差过大",
            )

    def test_recipe_tags_exist(self):
        tag_names = {t["name"] for t in self.tags}
        for r in self.recipes:
            self.assertTrue(r["tags"], f"{r['title']} 缺少标签")
            for tag in r["tags"]:
                self.assertIn(tag, tag_names, f"{r['title']} 使用了未定义标签 {tag}")

    def test_recipe_ingredients_resolvable(self):
        for r in self.recipes:
            self.assertTrue(r["ingredients"], f"{r['title']} 没有食材")
            main_count = 0
            for item in r["ingredients"]:
                for key in ("name", "amount", "unit", "is_main"):
                    self.assertIn(key, item, f"{r['title']} 食材缺字段 {key}")
                self.assertIn(
                    item["name"],
                    self.ingredient_index,
                    f"{r['title']} 的食材「{item['name']}」不在 ingredients.json 中",
                )
                self.assertIn(item["unit"], ALLOWED_UNITS, f"{r['title']} 单位非法 {item['unit']}")
                main_count += 1 if item["is_main"] else 0
            self.assertGreaterEqual(main_count, 1, f"{r['title']} 至少要有一样主料")

    def test_recipe_steps_detail(self):
        for r in self.recipes:
            self.assertGreaterEqual(len(r["steps"]), 4, f"{r['title']} 步骤太少")
            self.assertLessEqual(len(r["steps"]), 8, f"{r['title']} 步骤太多")
            for step in r["steps"]:
                self.assertGreaterEqual(len(step.strip()), 12, f"{r['title']} 有用废话步骤：{step}")
            # 准备 + 烹饪时间应与实际工序匹配（至少大于 0 且不至于离谱）
            self.assertLessEqual(r["prep_minutes"] + r["cook_minutes"], 240)

    # -- 用户 --------------------------------------------------------------
    def test_users_shape_and_signal(self):
        self.assertGreaterEqual(len(self.users), 8)
        titles = {r["title"] for r in self.recipes}
        emails = [u["email"] for u in self.users]
        self.assertEqual(len(emails), len(set(emails)))
        for user in self.users:
            for key in (
                "email",
                "nickname",
                "spicy_level",
                "max_cook_minutes",
                "diet_flags",
                "preferred_cuisines",
                "preferred_tags",
                "disliked_ingredients",
                "ratings",
                "favorites",
            ):
                self.assertIn(key, user, f"{user.get('nickname')} 缺字段 {key}")
            self.assertTrue(0 <= user["spicy_level"] <= 5)
            self.assertGreater(user["max_cook_minutes"], 0)
            self.assertGreaterEqual(len(user["ratings"]), 12)
            self.assertLessEqual(len(user["ratings"]), 25)
            for item in user["ratings"]:
                self.assertIn(item["recipe_title"], titles, f"评分菜谱不存在 {item['recipe_title']}")
                self.assertTrue(1 <= item["score"] <= 5)
            for title in user["favorites"]:
                self.assertIn(title, titles, f"收藏菜谱不存在 {title}")
            for cuisine in user["preferred_cuisines"]:
                self.assertIn(cuisine, EXPECTED_CUISINES)
            for tag in user["preferred_tags"]:
                self.assertIn(tag, {t["name"] for t in self.tags})

    def test_users_have_distinct_taste(self):
        """协同过滤需要明显口味差异：嗜辣用户与清淡用户不应是同一份画像。"""
        spicy = next(u for u in self.users if u["spicy_level"] >= 4)
        mild = next(u for u in self.users if u["spicy_level"] <= 1)
        self.assertNotEqual(set(spicy["preferred_cuisines"]), set(mild["preferred_cuisines"]))
        self.assertGreater(spicy["spicy_level"], mild["spicy_level"])
        # 至少有一道菜被两个以上用户评分，协同过滤才有共现信号
        counts: dict[str, int] = {}
        for user in self.users:
            for item in user["ratings"]:
                counts[item["recipe_title"]] = counts.get(item["recipe_title"], 0) + 1
        self.assertGreaterEqual(max(counts.values()), 2)


class SeedDataCommandTest(TestCase):
    """``seed_data`` 命令的落库结果与幂等性。"""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_data", verbosity=0)

    def _snapshot(self) -> dict[str, int]:
        return {
            "recipe": Recipe.objects.count(),
            "cuisine": Cuisine.objects.count(),
            "tag": Tag.objects.count(),
            "ingredient": Ingredient.objects.count(),
            "recipe_ingredient": RecipeIngredient.objects.count(),
            "step": RecipeStep.objects.count(),
            "user": User.objects.filter(username__contains="@demo.xunwei.local").count(),
            "profile": UserProfile.objects.count(),
            "rating": Rating.objects.count(),
            "favorite": Favorite.objects.count(),
            "interaction": InteractionLog.objects.filter(session_key="seed").count(),
        }

    def test_counts_matched_json(self):
        snap = self._snapshot()
        self.assertEqual(snap["recipe"], len(load_seed("recipes.json")))
        self.assertEqual(snap["cuisine"], 12)
        self.assertEqual(snap["tag"], len(load_seed("tags.json")))
        self.assertEqual(snap["ingredient"], len(load_seed("ingredients.json")))
        self.assertEqual(snap["user"], 8)
        self.assertEqual(snap["profile"], 8)
        self.assertEqual(snap["rating"], sum(len(u["ratings"]) for u in load_seed("users.json")))
        self.assertEqual(snap["favorite"], sum(len(u["favorites"]) for u in load_seed("users.json")))
        self.assertEqual(snap["interaction"], 208)

    def test_all_ingredients_resolved(self):
        self.assertEqual(RecipeIngredient.objects.filter(ingredient__isnull=True).count(), 0)

    def test_search_doc_and_hot_score(self):
        self.assertEqual(Recipe.objects.filter(search_doc="").count(), 0)
        sample = Recipe.objects.get(title="麻婆豆腐")
        self.assertIn("麻婆豆腐", sample.search_doc)
        self.assertIn("食材", sample.search_doc)
        self.assertGreaterEqual(sample.hot_score, 0)
        self.assertGreater(sample.content_version, 1)

    def test_demo_users_have_unusable_password(self):
        for user in User.objects.filter(username__contains="@demo.xunwei.local"):
            self.assertFalse(user.has_usable_password())
            self.assertTrue(user.profile.onboarded)

    def test_rerun_is_idempotent(self):
        before = self._snapshot()
        call_command("seed_data", verbosity=0)
        after = self._snapshot()
        self.assertEqual(before, after, "重复执行 seed_data 后数据量发生变化")

    def test_skip_users(self):
        call_command("seed_data", "--skip-users", verbosity=0)
        # 主数据仍在，用户不会被清掉
        self.assertEqual(Recipe.objects.count(), len(load_seed("recipes.json")))
        self.assertEqual(UserProfile.objects.count(), 8)

    def test_reset_then_reload(self):
        call_command("seed_data", "--reset", verbosity=0)
        self.assertEqual(Recipe.objects.count(), len(load_seed("recipes.json")))
        self.assertEqual(Rating.objects.count(), sum(len(u["ratings"]) for u in load_seed("users.json")))

    def test_interactions_spread_in_last_30_days(self):
        newest = InteractionLog.objects.order_by("-created_at").first()
        oldest = InteractionLog.objects.order_by("created_at").first()
        self.assertIsNotNone(newest)
        span_days = (newest.created_at - oldest.created_at).days
        self.assertGreaterEqual(span_days, 20, "行为时间应散布在近 30 天")
        actions = set(InteractionLog.objects.values_list("action", flat=True))
        self.assertTrue({"view", "favorite"} <= actions)


class RecipeRatingAggregateTest(TestCase):
    """``Recipe.recompute_rating()`` 的聚合正确性（不依赖种子命令）。"""

    def setUp(self):
        self.cuisine = Cuisine.objects.create(name="测试菜系", region="测试", description="仅用于测试")
        self.recipe = Recipe.objects.create(
            title="测试菜", cuisine=self.cuisine, summary="测试用菜谱", difficulty=1
        )

    def _rate(self, username: str, score: int) -> None:
        user = User.objects.create_user(username=username, password="x")
        Rating.objects.create(user=user, recipe=self.recipe, score=score)

    def test_recompute_rating_average(self):
        for idx, score in enumerate([5, 4, 3], start=1):
            self._rate(f"u{idx}", score)
        self.recipe.recompute_rating()
        self.recipe.refresh_from_db()
        self.assertAlmostEqual(self.recipe.rating_avg, 4.0, places=2)
        self.assertEqual(self.recipe.rating_count, 3)

    def test_recompute_rating_without_ratings(self):
        self.recipe.recompute_rating()
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.rating_avg, 0)
        self.assertEqual(self.recipe.rating_count, 0)

    def test_update_or_create_keeps_single_rating(self):
        user = User.objects.create_user(username="dup", password="x")
        Rating.objects.update_or_create(user=user, recipe=self.recipe, defaults={"score": 2})
        Rating.objects.update_or_create(user=user, recipe=self.recipe, defaults={"score": 5})
        self.assertEqual(Rating.objects.filter(user=user, recipe=self.recipe).count(), 1)
        self.recipe.recompute_rating()
        self.recipe.refresh_from_db()
        self.assertAlmostEqual(self.recipe.rating_avg, 5.0, places=2)
