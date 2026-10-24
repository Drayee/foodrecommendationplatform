"""``recommend`` 包单元测试（12 个用例）。

覆盖面：
1. 热度：时间衰减 / 公式精确值 / 批量回写
2. 内容画像：空用户冷启动、忌口一票否决、评分偏好命中
3. 协同过滤：物品相似度排序、候选打分、无行为用户降级
4. 混合层：冷启动条数与去重、scene 过滤、MMR 多样性对比、已消费排除、理由非空
5. 相似菜谱：排除自身 + 排序可用
6. 性能：单用户端到端耗时测量

运行方式（两种都支持）：
    python -m pytest -q recommend/tests.py
    python -m unittest recommend.tests -v

说明：pytest 未安装时用 unittest；模块自带测试库初始化（``setup_databases``），
因此不需要 pytest-django，也不会碰 ``db.sqlite3``。
"""
from __future__ import annotations

import os
import time
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "RAGDjange.settings")
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import TestCase, override_settings  # noqa: E402
from django.test.utils import (  # noqa: E402
    setup_databases,
    setup_test_environment,
    teardown_databases,
)
from django.utils import timezone  # noqa: E402

from food.models import (  # noqa: E402
    Cuisine,
    Favorite,
    Recipe,
    RecipeIngredient,
    Tag,
    UserProfile,
)
from recommend import cf, content, hot  # noqa: E402
from recommend.content import build_profile, score_recipe, similar_recipes  # noqa: E402
from recommend.hybrid import (  # noqa: E402
    normalize_scene,
    recommend_for_user,
    recommend_similar,
    scene_match,
)

_DB_STATE: dict[str, object] = {}


# ---------------------------------------------------------------------------
# 测试库初始化（兼容 pytest / unittest / manage.py test）
# ---------------------------------------------------------------------------
def _runner_managed() -> bool:
    """测试环境 / 测试库是否已由外部 runner（pytest-django、``manage.py test``）备好。"""
    try:
        from django.test.utils import _TestState  # Django 内部状态标记
    except Exception:  # pragma: no cover - 版本差异
        return False
    return hasattr(_TestState, "saved_data")


def _prepare_test_db() -> None:
    """确保跑在测试库上；已由外部 runner 托管（含 manage.py test / pytest-django）时跳过。"""
    if _DB_STATE.get("config") is not None:
        return
    name = str(connection.settings_dict.get("NAME") or "")
    if "memory" in name or name.startswith("file:memorydb") or _runner_managed():
        return
    setup_test_environment()
    _DB_STATE["config"] = setup_databases(verbosity=0, interactive=False)


def _teardown_test_db() -> None:
    """销毁测试库。"""
    config = _DB_STATE.pop("config", None)
    if config is not None:
        teardown_databases(config, verbosity=0)


def setUpModule() -> None:  # unittest
    """unittest 钩子。"""
    _prepare_test_db()


def setup_module(module=None) -> None:  # pytest (xunit style)
    """pytest 钩子。"""
    _prepare_test_db()


def tearDownModule() -> None:  # unittest
    """unittest 钩子。"""
    _teardown_test_db()


def teardown_module(module=None) -> None:  # pytest (xunit style)
    """pytest 钩子。"""
    _teardown_test_db()


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------
def make_recipe(
    title: str,
    *,
    cuisine: Cuisine | None = None,
    tags: tuple[tuple[str, str], ...] = (),
    ingredients: tuple[tuple[str, bool], ...] = (),
    minutes: int = 20,
    difficulty: int = 2,
    calories: float = 300.0,
    protein: float = 20.0,
    servings: int = 2,
    hot_score: float = 0.0,
    rating_avg: float = 0.0,
    rating_count: int = 0,
    favorite_count: int = 0,
    view_count: int = 0,
    hours_ago: float = 0.0,
) -> Recipe:
    """快速造一条已发布菜谱（含标签 / 食材 / 统计字段）。"""
    recipe = Recipe.objects.create(
        title=title,
        cuisine=cuisine,
        prep_minutes=0,
        cook_minutes=minutes,
        difficulty=difficulty,
        calories=calories,
        protein=protein,
        servings=servings,
        hot_score=hot_score,
        rating_avg=rating_avg,
        rating_count=rating_count,
        favorite_count=favorite_count,
        view_count=view_count,
        status=Recipe.Status.PUBLISHED,
    )
    for name, kind in tags:
        tag, _ = Tag.objects.get_or_create(name=name, kind=kind)
        recipe.tags.add(tag)
    for name, is_main in ingredients:
        RecipeIngredient.objects.create(recipe=recipe, name_text=name, is_main=is_main)
    if hours_ago:
        Recipe.objects.filter(pk=recipe.pk).update(created_at=timezone.now() - timedelta(hours=hours_ago))
        recipe.refresh_from_db()
    return recipe


def rec_settings(**overrides: str) -> dict[str, str]:
    """构造完整的 ``RECOMMEND`` 配置（``override_settings`` 需要整块替换）。"""
    base = {
        "FEED_SIZE": "12",
        "WEIGHT_CONTENT": "0.45",
        "WEIGHT_CF": "0.30",
        "WEIGHT_HOT": "0.15",
        "WEIGHT_FRESH": "0.10",
        "HOT_DECAY_HOURS": "72",
        "MMR_LAMBDA": "0.7",
    }
    base.update(overrides)
    return base


class RecommendTestBase(TestCase):
    """公共 setUp：清 CF 缓存、建基础菜系。"""

    def setUp(self) -> None:
        cf.invalidate_cache()
        self.sichuan = Cuisine.objects.create(name="川菜")
        self.cantonese = Cuisine.objects.create(name="粤菜")
        self.user = User.objects.create_user("tester", password="pwd12345")


# ---------------------------------------------------------------------------
# 1. 热度
# ---------------------------------------------------------------------------
class HotScoreTests(RecommendTestBase):
    def test_new_recipe_beats_old_with_same_engagement(self) -> None:
        """同互动量下，新菜热度必须高于老菜（时间衰减单调）。"""
        now = timezone.now()
        fresh = make_recipe("新出锅红烧肉", hours_ago=1, rating_avg=4.5, rating_count=10, favorite_count=5, view_count=100)
        old = make_recipe("上周的红烧肉", hours_ago=200, rating_avg=4.5, rating_count=10, favorite_count=5, view_count=100)

        fresh_score = hot.hot_score(fresh, now=now)
        old_score = hot.hot_score(old, now=now)
        self.assertGreater(fresh_score, old_score)
        self.assertGreater(old_score, 0.0)

        expected = (4.5 * 10 * 1.2 + 5 * 3 + 100 * 0.5) / ((1 + 2) ** 1.5)
        self.assertAlmostEqual(fresh_score, expected, places=4)

    def test_recompute_hot_scores_batch_writes(self) -> None:
        """批量回写：返回更新条数，且库里的 hot_score 与公式一致。"""
        now = timezone.now()
        a = make_recipe("番茄牛腩", hours_ago=2, rating_avg=4.0, rating_count=3, favorite_count=2, view_count=50)
        make_recipe("清蒸鲈鱼", hours_ago=5, rating_avg=5.0, rating_count=1)
        updated = hot.recompute_hot_scores()
        self.assertEqual(updated, 2)

        a.refresh_from_db()
        expected = hot.hot_score(a, now=now)
        self.assertAlmostEqual(a.hot_score, expected, places=2)
        self.assertGreater(a.hot_score, 0.0)

    def test_recompute_is_idempotent_within_tolerance(self) -> None:
        """连续两次重算：第二次不再产生无意义 UPDATE（时间衰减漂移小于阈值）。"""
        make_recipe("宫保鸡丁", hours_ago=3, rating_avg=4.5, rating_count=6, favorite_count=4, view_count=90)
        make_recipe("鱼香肉丝", hours_ago=8, rating_avg=4.0, rating_count=3, favorite_count=2, view_count=40)
        first = hot.recompute_hot_scores()
        second = hot.recompute_hot_scores()
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)

    def test_hot_monotonic_in_views(self) -> None:
        """浏览数越多热度越高（同创建时间）。"""
        now = timezone.now()
        low = make_recipe("冷门菜", hours_ago=3, view_count=10)
        high = make_recipe("热门菜", hours_ago=3, view_count=1000)
        self.assertGreater(hot.hot_score(high, now=now), hot.hot_score(low, now=now))


# ---------------------------------------------------------------------------
# 2. 内容画像
# ---------------------------------------------------------------------------
class ContentProfileTests(RecommendTestBase):
    def test_empty_profile_for_cold_start(self) -> None:
        """空用户 / 无偏好用户返回空画像，打分仍在 0~1 且不抛异常。"""
        anonymous = build_profile(None)
        self.assertFalse(anonymous.has_signals)
        self.assertEqual(anonymous.cuisine_weights, {})
        self.assertEqual(anonymous.tag_weights, {})
        self.assertEqual(anonymous.disliked, set())

        plain_user = build_profile(self.user)  # 没有 UserProfile、没有行为
        self.assertFalse(plain_user.has_signals)

        recipe = make_recipe("清炒时蔬", cuisine=self.cantonese, tags=(("清淡", "taste"),))
        score, reasons = score_recipe(plain_user, recipe)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        self.assertIsInstance(reasons, list)

    def test_disliked_ingredient_zeroes_score(self) -> None:
        """忌口命中直接 0 分，并给出中文提示理由。"""
        profile_obj = UserProfile.objects.create(user=self.user, disliked_ingredients=["花生"])
        profile_obj.preferred_cuisines.add(self.sichuan)
        profile = build_profile(self.user)
        self.assertIn("花生", profile.disliked)

        hit_recipe = make_recipe("宫保鸡丁", cuisine=self.sichuan, ingredients=(("花生米", True), ("鸡胸肉", True)))
        safe_recipe = make_recipe("麻婆豆腐", cuisine=self.sichuan, ingredients=(("豆腐", True),))

        hit_score, hit_reasons = score_recipe(profile, hit_recipe)
        safe_score, _ = score_recipe(profile, safe_recipe)
        self.assertEqual(hit_score, 0.0)
        self.assertTrue(any("忌口" in r for r in hit_reasons))
        self.assertGreater(safe_score, 0.0)

    def test_rating_lifts_same_cuisine(self) -> None:
        """评分 ≥4 的川菜历史 → 川菜候选得分高于粤菜候选。"""
        from food.models import Rating

        liked = make_recipe("水煮牛肉", cuisine=self.sichuan, tags=(("麻辣", "taste"),), ingredients=(("牛肉", True),))
        Rating.objects.create(user=self.user, recipe=liked, score=5)

        profile = build_profile(self.user)
        self.assertGreater(profile.cuisine_weights.get("川菜", 0.0), 0.0)

        sichuan_candidate = make_recipe("回锅肉", cuisine=self.sichuan, tags=(("香辣", "taste"),))
        cantonese_candidate = make_recipe("白切鸡", cuisine=self.cantonese, tags=(("咸鲜", "taste"),))
        sichuan_score, sichuan_reasons = score_recipe(profile, sichuan_candidate)
        cantonese_score, _ = score_recipe(profile, cantonese_candidate)
        self.assertGreater(sichuan_score, cantonese_score)
        self.assertTrue(any("川菜" in r for r in sichuan_reasons))


# ---------------------------------------------------------------------------
# 3. 协同过滤
# ---------------------------------------------------------------------------
class CollaborativeFilteringTests(RecommendTestBase):
    def test_item_similarity_ranking(self) -> None:
        """共现用户越多的两个物品，相似度越高；无关物品不进近邻。"""
        from food.models import Rating

        a = make_recipe("A菜")
        b = make_recipe("B菜")
        c = make_recipe("C菜")
        u2 = User.objects.create_user("u2", password="pwd12345")
        u3 = User.objects.create_user("u3", password="pwd12345")
        u4 = User.objects.create_user("u4", password="pwd12345")

        Rating.objects.create(user=self.user, recipe=a, score=5)
        Rating.objects.create(user=self.user, recipe=b, score=4)
        Rating.objects.create(user=u2, recipe=a, score=4)
        Rating.objects.create(user=u2, recipe=b, score=5)
        Rating.objects.create(user=u3, recipe=c, score=5)
        Rating.objects.create(user=u4, recipe=c, score=4)

        sims = cf.item_similarity(k=5)
        self.assertIn(a.id, sims)
        neighbours = dict(sims[a.id])
        self.assertIn(b.id, neighbours)
        self.assertGreater(neighbours[b.id], 0.5)
        self.assertLess(neighbours.get(c.id, 0.0), neighbours[b.id])

    def test_cf_scores_only_known_neighbours(self) -> None:
        """cf_scores 只给有共现邻居的候选打分，无信号候选不出现。"""
        from food.models import Rating

        a = make_recipe("金牌卤肉饭")
        b = make_recipe("台式卤肉饭")
        c = make_recipe("凉拌黄瓜")
        u2 = User.objects.create_user("cf_u2", password="pwd12345")
        Rating.objects.create(user=self.user, recipe=a, score=5)
        Rating.objects.create(user=u2, recipe=a, score=5)
        Rating.objects.create(user=u2, recipe=b, score=4)

        scores = cf.cf_scores(self.user, [b.id, c.id])
        self.assertIn(b.id, scores)
        self.assertGreater(scores[b.id], 0.0)
        self.assertNotIn(c.id, scores)

    def test_anonymous_user_returns_no_cf(self) -> None:
        """匿名用户 / 无行为用户不产生 CF 分。"""
        make_recipe("任意菜")
        self.assertEqual(cf.cf_scores(None, [1, 2, 3]), {})


# ---------------------------------------------------------------------------
# 4. 混合层
# ---------------------------------------------------------------------------
class HybridTests(RecommendTestBase):
    def _seed_varied(self, count: int = 6) -> list[Recipe]:
        """造一批热度递减、口味各异的菜谱。"""
        tag_pool = (("下饭", "scene"), ("清淡", "taste"), ("香辣", "taste"), ("快手", "method"))
        recipes = []
        for index in range(count):
            recipes.append(
                make_recipe(
                    f"测试菜{index}",
                    cuisine=self.sichuan if index % 2 == 0 else self.cantonese,
                    tags=(tag_pool[index % len(tag_pool)],),
                    minutes=15 + index,
                    hot_score=float(100 - index * 10),
                    rating_avg=4.0,
                    rating_count=5,
                    hours_ago=index,
                )
            )
        return recipes

    def test_cold_start_returns_k_unique(self) -> None:
        """冷启动返回 k 条且 id 不重复。"""
        self._seed_varied(6)
        results = recommend_for_user(None, k=4)
        self.assertEqual(len(results), 4)
        ids = [item.recipe.id for item in results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item.reason for item in results))

    def test_scene_filter_applies(self) -> None:
        """scene="快手" 时只保留 25 分钟内的菜谱。"""
        for index in range(3):
            make_recipe(f"快手小炒{index}", minutes=15, hot_score=90 - index)
        for index in range(3):
            make_recipe(f"慢炖大菜{index}", minutes=90, difficulty=4, hot_score=95 - index)

        results = recommend_for_user(None, k=3, scene="快手")
        self.assertEqual(len(results), 3)
        for item in results:
            self.assertLessEqual(item.recipe.total_minutes, 25)

        # 场景词归一化
        self.assertEqual(normalize_scene("快速"), "快手")
        self.assertIsNone(normalize_scene("不存在的场景"))

    def test_mmr_reduces_homogeneity(self) -> None:
        """开启 MMR（低 lambda）后，结果标签种类数不低于关闭时，且确有提升。"""
        # 6 道高度同质的「香辣川菜」（热度最高）
        for index in range(6):
            make_recipe(
                f"香辣同质菜{index}",
                cuisine=self.sichuan,
                tags=(("香辣", "taste"),),
                ingredients=(("干辣椒", True), ("花椒", True)),
                hot_score=200 - index,
                rating_avg=4.5,
                rating_count=8,
                hours_ago=index,
            )
        # 3 道口味不同、热度略低的菜
        for index, tag in enumerate((("清淡", "taste"), ("酸甜", "taste"), ("咸鲜", "taste"))):
            make_recipe(f"多样菜{index}", cuisine=self.cantonese, tags=(tag,), hot_score=60 - index, hours_ago=index)

        plain = recommend_for_user(None, k=4, diversity=False)
        plain_tags = {tag for item in plain for tag in (t.name for t in item.recipe.tags.all())}

        with override_settings(RECOMMEND=rec_settings(MMR_LAMBDA="0.05")):
            diversified = recommend_for_user(None, k=4, diversity=True)
        diverse_tags = {tag for item in diversified for tag in (t.name for t in item.recipe.tags.all())}

        self.assertEqual(len(plain), 4)
        self.assertEqual(len(diversified), 4)
        self.assertGreaterEqual(len(diverse_tags), len(plain_tags))
        self.assertGreater(len(diverse_tags), len(plain_tags))

    def test_consumed_recipes_are_excluded(self) -> None:
        """已收藏 / 已评分的菜谱不再出现在推荐里。"""
        consumed = make_recipe("吃过的菜", hot_score=999, cuisine=self.sichuan, rating_avg=5, rating_count=9)
        Favorite.objects.create(user=self.user, recipe=consumed)
        for index in range(5):
            make_recipe(f"没吃过的菜{index}", hot_score=100 - index)

        results = recommend_for_user(self.user, k=3)
        self.assertTrue(results)
        self.assertNotIn(consumed.id, [item.recipe.id for item in results])

    def test_reason_is_chinese_text(self) -> None:
        """每条结果都有中文理由，并带 0~1 分值。"""
        self._seed_varied(5)
        results = recommend_for_user(self.user, k=3)
        for item in results:
            self.assertTrue(item.reason)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in item.reason))
            self.assertGreaterEqual(item.score, 0.0)
            self.assertLessEqual(item.score, 1.0)

    def test_scene_short_feed_is_padded(self) -> None:
        """场景命中不足 k 条时用其余候选补齐，且命中项仍排在最前。"""
        quick = make_recipe("唯一快手菜", minutes=10, hot_score=10)
        for index in range(4):
            make_recipe(f"慢炖大菜补{index}", minutes=120, difficulty=4, hot_score=99 - index)

        results = recommend_for_user(None, k=3, scene="快手")
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].recipe.id, quick.id)

    def test_scene_prefers_tags_over_time_heuristic(self) -> None:
        """候选池里有场景标签时按标签筛，纯耗时短路不再生效。"""
        breakfast = make_recipe("皮蛋瘦肉粥", tags=(("早餐", "scene"),), minutes=50, hot_score=80)
        quick = make_recipe("快手炒饭", tags=(("快手", "method"),), minutes=10, hot_score=90)

        results = recommend_for_user(None, k=3, scene="早餐")
        self.assertEqual(results[0].recipe.id, breakfast.id)

    def test_scene_match_helper(self) -> None:
        """场景判定：标签命中或硬约束满足即通过。"""
        quick = content.features_from_recipe(make_recipe("葱油拌面", tags=(("快手", "method"),), minutes=10))
        slow = content.features_from_recipe(make_recipe("佛跳墙", minutes=180, difficulty=5, servings=6))
        self.assertTrue(scene_match(quick, "快手"))
        self.assertFalse(scene_match(slow, "快手"))
        self.assertTrue(scene_match(slow, "宴客"))
        self.assertTrue(scene_match(slow, None))


# ---------------------------------------------------------------------------
# 5. 相似菜谱
# ---------------------------------------------------------------------------
class SimilarRecipeTests(RecommendTestBase):
    def test_similar_recipes_excludes_self(self) -> None:
        """相似菜谱排除自身，且同菜系同标签的排第一。"""
        base = make_recipe(
            "麻辣香锅",
            cuisine=self.sichuan,
            tags=(("麻辣", "taste"),),
            ingredients=(("藕片", True), ("午餐肉", True)),
        )
        close = make_recipe(
            "干锅花菜",
            cuisine=self.sichuan,
            tags=(("麻辣", "taste"),),
            ingredients=(("花菜", True), ("午餐肉", True)),
        )
        far = make_recipe("白灼菜心", cuisine=self.cantonese, tags=(("清淡", "taste"),))

        pairs = similar_recipes(base, k=2)
        ids = [obj.id for obj, _ in pairs]
        self.assertNotIn(base.id, ids)
        self.assertEqual(ids[0], close.id)
        self.assertGreater(pairs[0][1], 0.0)
        self.assertGreater(pairs[0][1], dict(pairs).get(far.id, 0.0))

    def test_recommend_similar_wrapper(self) -> None:
        """hybrid.recommend_similar 返回 ScoredRecipe 且理由中文。"""
        base = make_recipe("可乐鸡翅", cuisine=self.cantonese, tags=(("咸鲜", "taste"),))
        make_recipe("蜜汁鸡翅", cuisine=self.cantonese, tags=(("咸鲜", "taste"),))
        results = recommend_similar(base.id, k=1)
        self.assertEqual(len(results), 1)
        self.assertNotEqual(results[0].recipe.id, base.id)
        self.assertTrue(results[0].reason)


# ---------------------------------------------------------------------------
# 6. 性能
# ---------------------------------------------------------------------------
class PerformanceTests(RecommendTestBase):
    def test_single_user_latency_under_budget(self) -> None:
        """单用户端到端耗时：120 道菜、冷启动与个性化两条路径都测量。"""
        for index in range(120):
            make_recipe(
                f"压测菜谱{index}",
                cuisine=self.sichuan if index % 3 else self.cantonese,
                tags=(("下饭", "scene"), ("香辣", "taste")),
                ingredients=(("土豆", True), ("青椒", False)),
                minutes=10 + index % 40,
                hot_score=float(500 - index),
                rating_avg=4.2,
                rating_count=6,
                hours_ago=index % 48,
            )
        from food.models import Rating

        Rating.objects.create(user=self.user, recipe=Recipe.objects.first(), score=5)
        cf.invalidate_cache()

        warm = None
        for label in ("冷启动", "个性化"):
            started = time.perf_counter()
            results = recommend_for_user(self.user if label == "个性化" else None, k=12)
            elapsed_ms = (time.perf_counter() - started) * 1000
            warm = elapsed_ms
            self.assertEqual(len(results), 12)
            self.assertLess(elapsed_ms, 1000.0, f"{label} 路径耗时 {elapsed_ms:.1f}ms 超出预算")
            print(f"[性能] {label} 单用户端到端：{elapsed_ms:.1f}ms")
        self.assertIsNotNone(warm)
