"""`websearch` 单元测试（**全程不联网**）。

所有网络访问都通过 `websearch.providers.http_get` 打桩：
`with patch("websearch.providers.http_get", ...)` / `patch(..., side_effect=...)`。

运行方式
- `python -m pytest -q websearch/tests.py`
- 或 `python manage.py test websearch`
"""

from __future__ import annotations

import json
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from food.models import Recipe, RecipeStep, Tag, WebSearchCache
from websearch import providers, service
from websearch.ingest import estimate_nutrition, import_recipe
from websearch.providers import (
    SearchResult,
    WebSearchError,
    parse_duration,
    parse_recipe_jsonld,
    parse_recipe_site,
    split_ingredient_line,
    web_search,
)

# ---------------------------------------------------------------------------
# 测试用 HTML 片段（结构对齐真实站点，避免联网）
# ---------------------------------------------------------------------------
DDG_HTML = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.xiachufang.com%2Frecipe%2F100563982%2F">
        番茄牛腩的做法_下厨房
      </a>
    </h2>
    <a class="result__snippet" href="#">牛腩焯水后与番茄同炖，酸甜浓郁。</a>
  </div>
</div>
<div class="result results_links web-result">
  <h2 class="result__title"><a class="result__a" href="https://www.meishij.net/zuofa/fanqieniunan_18.html">番茄牛腩家常做法_美食杰</a></h2>
  <a class="result__snippet" href="#">番茄分两次下锅，一半炒沙一半留果肉。</a>
</div>
</body></html>
"""

BING_HTML = """
<html><body><ol id="b_results">
<li class="b_algo">
  <h2><a href="https://www.meishij.net/zuofa/fanqieniunan_18.html">番茄牛腩的家常做法 - 美食杰</a></h2>
  <div class="b_caption"><p>牛腩软烂、汤汁浓稠，拌饭一绝。</p></div>
</li>
<li class="b_algo">
  <h2><a href="https://www.douguo.com/cookbook/2086944.html">番茄炖牛腩_豆果美食</a></h2>
  <p>3.2 万人收藏的做法。</p>
</li>
</ol></body></html>
"""

SO_HTML = """
<html><body><ul class="result">
<li class="res-list">
  <h3><a href="https://www.xiachufang.com/recipe/100563982/">番茄牛腩做法_下厨房</a></h3>
  <p class="res-desc">酸甜开胃，牛腩软烂不柴。</p>
</li>
</ul></body></html>
"""

#: 无 JSON-LD 的下厨房风格页面（走站点启发式兜底）
XIACHUFANG_HTML = """
<html><head><title>麻婆豆腐的做法_下厨房</title></head>
<body>
  <h1 class="page-title">麻婆豆腐</h1>
  <div class="ings">
    <table>
      <tr><td class="name">北豆腐 400克</td></tr>
      <tr><td class="name">牛肉末 80克</td></tr>
      <tr><td class="name">郫县豆瓣酱 1汤匙</td></tr>
    </table>
  </div>
  <div class="steps">
    <ol>
      <li>1. 豆腐切块，入盐水焯 1 分钟。</li>
      <li>2. 炒香牛肉末与豆瓣酱，出红油。</li>
      <li>3. 下豆腐烧 3 分钟，勾芡后撒花椒粉。</li>
    </ol>
  </div>
</body></html>
"""


def _fixture_text(name: str) -> str:
    """读取 fixtures 下的真实样例文件。"""
    path = providers.fixture_dir() / name
    return path.read_text(encoding="utf-8")


class JSONLDParsingTests(TestCase):
    """JSON-LD 解析（核心能力）。"""

    def test_parse_jsonld_single_object(self):
        """单对象 Recipe：标题 / 食材拆分 / 步骤 / 时长 / 图片 / 营养 / 关键词。"""
        data = parse_recipe_jsonld(_fixture_text("recipe_jsonld_麻婆豆腐.html"), "https://www.xiachufang.com/recipe/100563982/")

        self.assertIsNotNone(data)
        self.assertEqual(data["title"], "麻婆豆腐")
        self.assertIn("麻辣鲜香", data["summary"])
        self.assertEqual(data["cover_url"], "https://i2.chuimg.com/mapo_tofu_1200x800.jpg")
        self.assertEqual(len(data["ingredients"]), 10)
        self.assertEqual(data["ingredients"][0], {"name": "北豆腐", "amount": "400", "unit": "克"})
        self.assertEqual(data["ingredients"][6]["name"], "花椒粉")
        self.assertEqual(data["ingredients"][6]["amount"], "适量")
        self.assertEqual(len(data["steps"]), 4)
        self.assertEqual(data["steps"][0]["image_url"], "https://i2.chuimg.com/mapo_step1_800x600.jpg")
        self.assertEqual(data["servings"], 2)
        self.assertEqual(data["prep_minutes"], 10)
        self.assertEqual(data["cook_minutes"], 15)
        self.assertEqual(data["calories"], 380.0)
        self.assertEqual(data["cuisine"], "中国菜")
        self.assertIn("麻辣", data["tags"])
        self.assertIn("川菜", data["tags"])
        self.assertEqual(data["source_site"], "xiachufang.com")

    def test_parse_jsonld_graph(self):
        """@graph 形式：跳过面包屑节点，HowToSection 内的步骤要拍平。"""
        data = parse_recipe_jsonld(
            _fixture_text("recipe_jsonld_番茄牛腩.html"), "https://www.meishij.net/zuofa/fanqieniunan_18.html"
        )

        self.assertIsNotNone(data)
        self.assertEqual(data["title"], "番茄牛腩")
        # image 是列表 → 取第一张
        self.assertEqual(data["cover_url"], "https://i.meishij.net/fanqieniunan_1.jpg")
        self.assertEqual(len(data["ingredients"]), 9)
        self.assertEqual(data["ingredients"][1], {"name": "番茄", "amount": "3", "unit": "个"})
        # 2 + 3 个 HowToStep，分组名不能混进步骤
        self.assertEqual(len(data["steps"]), 5)
        self.assertTrue(all("准备" != step["text"] for step in data["steps"]))
        self.assertIn("牛腩切 3 厘米块", data["steps"][0]["text"])
        self.assertEqual(data["servings"], 3)
        self.assertEqual(data["prep_minutes"], 15)
        self.assertEqual(data["cook_minutes"], 90)
        self.assertEqual(data["calories"], 520.0)

    def test_instructions_three_shapes(self):
        """instructions 三种形态：纯字符串 / 字符串列表 / HowToStep 列表。"""
        template = """
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org",
          "@type": "Recipe",
          "name": "测试菜谱",
          "recipeIngredient": ["盐 2克"],
          "recipeInstructions": {body}
        }}
        </script>
        """
        cases = [
            ('"先把水烧开，再下食材。"', 1),
            ('["步骤一：备料", "步骤二：下锅", "步骤三：出锅"]', 3),
            ('[{"@type": "HowToStep", "text": "A"}, {"@type": "HowToStep", "text": "B"}]', 2),
        ]
        for body, expected in cases:
            html = template.format(body=body)
            data = parse_recipe_jsonld(html, "https://example.com/recipe/1")
            self.assertIsNotNone(data, body)
            self.assertEqual(len(data["steps"]), expected, body)

    def test_parse_jsonld_bad_json_returns_none(self):
        """全部 JSON 损坏时返回 None，不抛异常。"""
        html = """
        <html><head>
          <script type="application/ld+json">{ "name": "坏的 JSON", </script>
          <script type="application/ld+json">not json at all</script>
        </head><body></body></html>
        """
        self.assertIsNone(parse_recipe_jsonld(html, "https://example.com/broken"))

    def test_parse_jsonld_mixed_bad_and_good(self):
        """坏 JSON 与前后的杂讯不影响同页面里合法的 Recipe 块。"""
        html = """
        <script type="application/ld+json">{ broken </script>
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Recipe", "name": "番茄炒蛋",
         "recipeIngredient": ["番茄 2个"], "recipeInstructions": "炒熟即可"}
        </script>
        """
        data = parse_recipe_jsonld(html, "https://example.com/ok")
        self.assertIsNotNone(data)
        self.assertEqual(data["title"], "番茄炒蛋")
        self.assertEqual(len(data["steps"]), 1)

    def test_parse_duration_iso8601(self):
        """ISO8601 → 分钟：PT1H30M=90、PT45M=45、P1DT2H=1560、中文时长也能识别。"""
        self.assertEqual(parse_duration("PT1H30M"), 90)
        self.assertEqual(parse_duration("PT45M"), 45)
        self.assertEqual(parse_duration("P1DT2H"), 1560)
        self.assertEqual(parse_duration("PT20S"), 0)
        self.assertEqual(parse_duration("1小时30分钟"), 90)
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration(None), 0)
        self.assertEqual(parse_duration(""), 0)

    def test_split_ingredient_line(self):
        """食材行拆分为 name / amount / unit（含中文站点常见的无空格写法）。"""
        self.assertEqual(split_ingredient_line("牛腩 500克"), {"name": "牛腩", "amount": "500", "unit": "克"})
        self.assertEqual(split_ingredient_line("1汤匙 豆瓣酱"), {"name": "豆瓣酱", "amount": "1", "unit": "汤匙"})
        self.assertEqual(split_ingredient_line("盐 适量"), {"name": "盐", "amount": "适量", "unit": ""})
        self.assertEqual(split_ingredient_line("清水"), {"name": "清水", "amount": "", "unit": ""})
        # 下厨房移动端写法：数字 + 单位 + 名称，中间没有空格
        self.assertEqual(split_ingredient_line("600克牛腩"), {"name": "牛腩", "amount": "600", "unit": "克"})
        self.assertEqual(split_ingredient_line("2个番茄"), {"name": "番茄", "amount": "2", "unit": "个"})
        self.assertEqual(split_ingredient_line("半个洋葱"), {"name": "洋葱", "amount": "半", "unit": "个"})
        self.assertEqual(split_ingredient_line("北豆腐400克"), {"name": "北豆腐", "amount": "400", "unit": "克"})

    def test_single_string_with_numbered_steps(self):
        """一整串 "1.…2.…3.…" 的 instructions 要被拆成多条步骤（真实站点常见）。"""
        html = """
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Recipe", "name": "番茄炖牛腩",
         "recipeIngredient": ["600克牛腩", "2个番茄"],
         "recipeInstructions": "食材集合,1.牛腩切大粒，冷水浸泡30分钟。,2.番茄去皮切块。,3.小火炖90分钟，加盐调味。"}
        </script>
        """
        data = parse_recipe_jsonld(html, "https://m.xiachufang.com/recipe/104167300/")
        self.assertIsNotNone(data)
        self.assertEqual(len(data["steps"]), 3)
        self.assertTrue(data["steps"][0]["text"].startswith("牛腩切大粒"))
        self.assertTrue(data["steps"][2]["text"].startswith("小火炖90分钟"))
        # "食材集合" 这类栏目名前缀要丢掉
        self.assertNotIn("食材集合", " ".join(step["text"] for step in data["steps"]))
        self.assertEqual(data["ingredients"][0], {"name": "牛腩", "amount": "600", "unit": "克"})

    def test_single_sentence_step_not_split(self):
        """没有编号标记的普通长句不能被误拆。"""
        html = """
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Recipe", "name": "测试",
         "recipeIngredient": ["盐 2克"],
         "recipeInstructions": "把水烧开后下入食材，煮 3.5 分钟后调味即可。"}
        </script>
        """
        data = parse_recipe_jsonld(html, "https://example.com/x")
        self.assertEqual(len(data["steps"]), 1)
        self.assertIn("3.5 分钟", data["steps"][0]["text"])


class SiteFallbackTests(TestCase):
    """JSON-LD 缺失时的站点启发式兜底。"""

    def test_xiachufang_heuristic_parse(self):
        """无 JSON-LD 时用 .page-title / .ings / .steps 选择器兜底。"""
        data = parse_recipe_site(XIACHUFANG_HTML, "https://www.xiachufang.com/recipe/1001/")
        self.assertIsNotNone(data)
        self.assertEqual(data["title"], "麻婆豆腐")
        self.assertEqual(len(data["ingredients"]), 3)
        self.assertEqual(data["ingredients"][0]["name"], "北豆腐")
        self.assertEqual(len(data["steps"]), 3)
        # 步骤前的编号要被清掉
        self.assertFalse(data["steps"][0]["text"].startswith("1."))

    def test_fetch_recipe_prefers_jsonld_then_fallback(self):
        """fetch_recipe：有 JSON-LD 用 JSON-LD；没有则走站点规则。"""
        with patch("websearch.providers.http_get", return_value=_fixture_text("recipe_jsonld_麻婆豆腐.html")):
            from_jsonld = providers.fetch_recipe("https://www.xiachufang.com/recipe/100563982/")
        self.assertEqual(from_jsonld["title"], "麻婆豆腐")
        self.assertEqual(from_jsonld["cook_minutes"], 15)

        with patch("websearch.providers.http_get", return_value=XIACHUFANG_HTML):
            from_rules = providers.fetch_recipe("https://www.xiachufang.com/recipe/1001/")
        self.assertEqual(from_rules["title"], "麻婆豆腐")
        self.assertEqual(len(from_rules["steps"]), 3)

    def test_fetch_recipe_raises_value_error(self):
        """页面里既无 JSON-LD 也没有可用结构 → ValueError。"""
        with patch("websearch.providers.http_get", return_value="<html><body><h1>404</h1></body></html>"):
            with self.assertRaises(ValueError):
                providers.fetch_recipe("https://www.douguo.com/cookbook/404.html")


class SearchProviderTests(TestCase):
    """多引擎搜索与失败传播。"""

    def test_provider_fallback_order(self):
        """duckduckgo 失败时自动切到 bing，并标记 source。"""
        def fake_get(url, timeout=None, headers=None):
            if "duckduckgo" in url:
                raise WebSearchError("duckduckgo 超时")
            return BING_HTML

        with patch("websearch.providers.http_get", side_effect=fake_get):
            results = web_search("番茄牛腩", limit=5)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "bing")
        self.assertEqual(results[0].url, "https://www.meishij.net/zuofa/fanqieniunan_18.html")

    def test_duckduckgo_redirect_unwrap(self):
        """DDG 的 /l/?uddg= 跳转链接要还原成真实地址。"""
        with patch("websearch.providers.http_get", return_value=DDG_HTML):
            results = web_search("番茄牛腩", limit=5)
        self.assertEqual(results[0].source, "duckduckgo")
        self.assertEqual(results[0].url, "https://www.xiachufang.com/recipe/100563982/")

    def test_so_results_dedupe_and_filter_ai_links(self):
        """360 同一标题的多个 link 变体只保留一条；AI 聚合页直接丢弃。"""
        html = """
        <html><body><ul class="result">
        <li class="res-list"><h3><a href="https://www.so.com/link?m=aaa">番茄牛腩做法_豆果美食</a></h3><p class="res-desc">用料：牛腩 500 克</p></li>
        <li class="res-list"><h3><a href="https://www.so.com/link?m=bbb">番茄牛腩做法_豆果美食</a></h3><p class="res-desc">用料：牛腩 500 克</p></li>
        <li class="res-list"><h3><a href="https://ai.so.com/search?search=番茄牛腩">360 AI 搜索结果</a></h3><p class="res-desc">AI 聚合页</p></li>
        </ul></body></html>
        """
        with patch("websearch.providers.http_get", return_value=html):
            results = web_search("番茄牛腩", limit=5, providers=["so"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "so")
        self.assertIn("so.com/link", results[0].url)

    def test_all_providers_failed_raises(self):
        """三个引擎全挂 → WebSearchError；且离线开关关闭时 service 直接上抛。"""
        with patch("websearch.providers.http_get", side_effect=WebSearchError("网络不可用")):
            with self.assertRaises(WebSearchError):
                web_search("番茄牛腩")

        disabled = {**settings.WEBSEARCH, "OFFLINE_FALLBACK": False}
        with override_settings(WEBSEARCH=disabled):
            with patch("websearch.providers.http_get", side_effect=WebSearchError("网络不可用")):
                with self.assertRaises(WebSearchError):
                    service.search_recipes_online("番茄牛腩", limit=3)


class SearchServiceTests(TestCase):
    """缓存与离线降级。"""

    def test_cache_hit_and_hit_count(self):
        """第二次同 query 命中缓存：cached=True、命中计数 +1、不再发起请求。"""
        with patch("websearch.providers.http_get", return_value=DDG_HTML) as mocked:
            first = service.search_recipes_online("番茄牛腩", limit=5)
            self.assertFalse(first["cached"])
            self.assertFalse(first["degraded"])
            self.assertEqual(first["provider"], "duckduckgo")
            self.assertTrue(first["expires_at"])
            self.assertGreaterEqual(first["latency_ms"], 0)
            self.assertEqual(mocked.call_count, 1)

            second = service.search_recipes_online("番茄牛腩", limit=5)
            self.assertTrue(second["cached"])
            self.assertEqual(mocked.call_count, 1)

        row = WebSearchCache.objects.get(query="番茄牛腩")
        self.assertEqual(row.hit_count, 1)
        self.assertEqual(row.provider, "duckduckgo")
        self.assertEqual(len(row.payload["results"]), 2)

    def test_cache_key_and_expiry(self):
        """缓存键 = md5(f"{provider}:{query}")，TTL 取配置 CACHE_TTL。"""
        expected_hash = service.query_hash("duckduckgo", "番茄牛腩")
        with patch("websearch.providers.http_get", return_value=DDG_HTML):
            result = service.search_recipes_online("番茄牛腩", limit=2)

        row = WebSearchCache.objects.get(query_hash=expected_hash)
        self.assertEqual(row.query, "番茄牛腩")
        self.assertFalse(row.degraded)
        ttl = int(settings.WEBSEARCH["CACHE_TTL"])
        remaining = (row.expires_at - timezone.now()).total_seconds()
        self.assertGreater(remaining, ttl - 60)
        self.assertLessEqual(remaining, ttl + 5)
        self.assertEqual(len(result["results"]), 2)

    def test_expired_cache_is_refetched(self):
        """过期缓存不再命中，会重新走网络。"""
        with patch("websearch.providers.http_get", return_value=DDG_HTML):
            service.search_recipes_online("番茄牛腩", limit=2)
        WebSearchCache.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        with patch("websearch.providers.http_get", return_value=DDG_HTML) as mocked:
            again = service.search_recipes_online("番茄牛腩", limit=2)
        self.assertFalse(again["cached"])
        self.assertEqual(mocked.call_count, 1)

    def test_offline_fixture_degraded(self):
        """断网时读 websearch/fixtures/*.json，标记 degraded=True。"""
        with patch("websearch.providers.http_get", side_effect=WebSearchError("connection timeout")):
            result = service.search_recipes_online("番茄牛腩", limit=3)

        self.assertTrue(result["degraded"])
        self.assertFalse(result["cached"])
        self.assertEqual(result["provider"], "fixture")
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(result["results"][0]["source"], "xiachufang.com")
        self.assertIn("番茄牛腩", result["results"][0]["title"])
        # 降级结果也会落缓存，但标记 degraded
        row = WebSearchCache.objects.get(query="番茄牛腩")
        self.assertTrue(row.degraded)


class IngestTests(TestCase):
    """抓取入库与去重。"""

    def setUp(self):
        self.url = "https://www.meishij.net/zuofa/fanqieniunan_18.html"
        self.html = _fixture_text("recipe_jsonld_番茄牛腩.html")

    def test_import_recipe_persists_fields(self):
        """落库字段：标题 / 状态 / 来源 / 时长 / 热量 / 食材 / 步骤 / 菜系 / 标签 / 检索文本。"""
        with patch("websearch.providers.http_get", return_value=self.html):
            result = import_recipe(self.url)

        self.assertTrue(result["created"])
        self.assertEqual(result["title"], "番茄牛腩")
        self.assertEqual(result["ingredients"], 9)
        self.assertEqual(result["steps"], 5)

        recipe = Recipe.objects.get(pk=result["recipe_id"])
        self.assertEqual(recipe.status, Recipe.Status.PUBLISHED)
        self.assertEqual(recipe.source, Recipe.Source.WEB)
        self.assertEqual(recipe.source_url, self.url)
        self.assertEqual(recipe.source_site, "meishij.net")
        self.assertEqual(recipe.prep_minutes, 15)
        self.assertEqual(recipe.cook_minutes, 90)
        self.assertEqual(recipe.servings, 3)
        self.assertEqual(recipe.calories, 520.0)
        # 菜系来自 recipeCuisine
        self.assertIsNotNone(recipe.cuisine)
        self.assertEqual(recipe.cuisine.name, "家常菜")
        # 标签来自 keywords / recipeCategory
        tag_names = set(recipe.tags.values_list("name", flat=True))
        self.assertTrue({"酸甜", "秋冬暖身", "高蛋白"}.issubset(tag_names))
        self.assertEqual(Tag.objects.get(name="高蛋白").kind, Tag.Kind.NUTRITION)
        # "炖菜" 命中烹饪方式词 → method
        self.assertEqual(Tag.objects.get(name="炖菜").kind, Tag.Kind.METHOD)
        # 食材关联主数据 + 用量
        first_ing = recipe.ingredients.first()
        self.assertEqual(first_ing.name_text, "牛腩")
        self.assertEqual(first_ing.amount, "500")
        self.assertEqual(first_ing.unit, "克")
        self.assertIsNotNone(first_ing.ingredient)
        self.assertGreater(first_ing.ingredient.calories_per_100g, 0)
        self.assertTrue(first_ing.is_main)
        # 步骤序号从 1 连续
        self.assertEqual(list(recipe.steps.values_list("index", flat=True)), [1, 2, 3, 4, 5])
        # RAG 检索文本已刷新
        self.assertIn("番茄牛腩", recipe.search_doc)
        self.assertIn("食材：", recipe.search_doc)
        self.assertGreaterEqual(recipe.content_version, 2)

    def test_import_recipe_dedupes_by_source_url(self):
        """同一 source_url 二次导入不再建新菜谱，返回 created=False。"""
        with patch("websearch.providers.http_get", return_value=self.html) as mocked:
            first = import_recipe(self.url)
            second = import_recipe(self.url)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["recipe_id"], first["recipe_id"])
        self.assertEqual(Recipe.objects.count(), 1)
        self.assertEqual(RecipeStep.objects.filter(recipe_id=first["recipe_id"]).count(), 5)
        self.assertEqual(mocked.call_count, 1)  # 去重后不再抓取

    def test_import_recipe_requires_parseable_page(self):
        """解析失败必须抛 ValueError（API 层转 502），不得建空菜谱。"""
        with patch("websearch.providers.http_get", return_value="<html><body>空页面</body></html>"):
            with self.assertRaises(ValueError):
                import_recipe("https://www.douguo.com/cookbook/empty.html")
        self.assertEqual(Recipe.objects.count(), 0)

    def test_import_recipe_rejects_bad_url(self):
        """非 http 链接直接 ValueError。"""
        with self.assertRaises(ValueError):
            import_recipe("ftp://example.com/recipe")

    def test_nutrition_estimate_from_ingredients(self):
        """页面没给热量时按食材克数估算热量与三大营养素。"""
        calories, protein, fat, carbs = estimate_nutrition(
            [
                {"name": "牛腩", "amount": "500", "unit": "克"},
                {"name": "番茄", "amount": "3", "unit": "个"},
                {"name": "盐", "amount": "适量", "unit": ""},
            ]
        )
        self.assertGreater(calories, 1000)  # 500g 牛腩 + 3 个番茄
        self.assertGreater(protein, 50)
        self.assertGreater(fat, 50)
        self.assertGreaterEqual(carbs, 0)

    def test_import_recipe_estimates_calories_when_missing(self):
        """无 nutrition 字段的页面走估算路径，热量不为 0。"""
        html = """
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Recipe", "name": "番茄炒蛋",
         "recipeIngredient": ["番茄 300克", "鸡蛋 3个"],
         "recipeInstructions": ["番茄切块", "鸡蛋打散炒熟后混合"]}
        </script>
        """
        url = "https://www.xiachufang.com/recipe/1002/"
        with patch("websearch.providers.http_get", return_value=html):
            result = import_recipe(url)
        recipe = Recipe.objects.get(pk=result["recipe_id"])
        self.assertGreater(recipe.calories, 0)
        self.assertEqual(recipe.servings, 2)  # 缺省 2 人份
        self.assertEqual(recipe.cuisine.name, "家常菜")  # 标题命中家常菜关键词


class ConfigAndUtilityTests(TestCase):
    """配置读取 / 请求头 / fixture 目录等零散能力。"""

    def test_headers_rotate_user_agent(self):
        """请求头包含轮换 UA 与中文 Accept-Language。"""
        headers = providers.build_headers()
        self.assertIn("Mozilla", headers["User-Agent"])
        self.assertIn("zh-CN", headers["Accept-Language"])

    def test_fixture_dir_points_to_package(self):
        """FIXTURE_DIR 指向 websearch/fixtures 且样例齐全。"""
        folder = providers.fixture_dir()
        self.assertEqual(folder.name, "fixtures")
        names = {path.name for path in folder.glob("*")}
        self.assertIn("duckduckgo_番茄牛腩.json", names)
        self.assertIn("recipe_jsonld_麻婆豆腐.html", names)
        self.assertIn("recipe_jsonld_番茄牛腩.html", names)
        payload = json.loads((folder / "duckduckgo_番茄牛腩.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["results"]), 5)

    def test_search_result_to_dict(self):
        """SearchResult 序列化字段与契约一致。"""
        item = SearchResult(title="番茄牛腩", url="https://a.com/1", snippet="酸甜", source="bing")
        self.assertEqual(
            item.to_dict(),
            {"title": "番茄牛腩", "url": "https://a.com/1", "snippet": "酸甜", "source": "bing"},
        )


#: 同名菜谱（不同 source_url）用于验证 slug 唯一索引不会炸
SAME_TITLE_HTML = """
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Recipe", "name": "番茄牛腩",
 "recipeIngredient": ["牛腩 500克", "番茄 3个"],
 "recipeInstructions": ["牛腩焯水", "番茄炒沙后同炖 90 分钟"]}
</script>
"""


class PostgresTimeZoneTests(TestCase):
    """PostgreSQL（`USE_TZ=True` + `timestamptz`）下的时间语义与 slug 唯一索引。"""

    def test_expires_at_is_aware_and_filterable(self):
        """`expires_at` 必须是 aware datetime，`expires_at__gt=timezone.now()` 过滤正确。"""
        with patch("websearch.providers.http_get", return_value=DDG_HTML):
            service.search_recipes_online("番茄牛腩", limit=2)

        row = WebSearchCache.objects.get(query="番茄牛腩")
        self.assertTrue(timezone.is_aware(row.expires_at))
        self.assertFalse(row.is_expired())
        self.assertEqual(WebSearchCache.objects.filter(expires_at__gt=timezone.now()).count(), 1)

        WebSearchCache.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(WebSearchCache.objects.filter(expires_at__gt=timezone.now()).count(), 0)

    def test_expires_at_column_is_timestamptz_on_postgres(self):
        """PG 上 `expires_at` 落到 `timestamp with time zone`，不是无时区时间戳。"""
        if connection.vendor != "postgresql":
            self.skipTest("仅 PostgreSQL 需要校验列类型")
        table = next(name for name in connection.introspection.table_names() if name.endswith("websearchcache"))
        with connection.cursor() as cursor:
            cursor.execute(
                "select data_type from information_schema.columns "
                "where table_name = %s and column_name = 'expires_at'",
                [table],
            )
            self.assertEqual(cursor.fetchone()[0], "timestamp with time zone")

    def test_same_title_different_url_gets_distinct_slug(self):
        """同名但不同来源的菜谱都要能入库（slug 唯一索引冲突要自动换 slug 重试）。"""
        with patch("websearch.providers.http_get", return_value=SAME_TITLE_HTML):
            first = import_recipe("https://www.meishij.net/zuofa/fanqieniunan_18.html")
            second = import_recipe("https://www.xiachufang.com/recipe/100563982/")

        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertNotEqual(first["recipe_id"], second["recipe_id"])
        slugs = set(Recipe.objects.values_list("slug", flat=True))
        self.assertEqual(len(slugs), 2)
        self.assertEqual(Recipe.objects.count(), 2)


class SlugConflictTests(TestCase):
    """`Recipe.slug` 唯一索引冲突时用 savepoint 重试，不能把外层事务打成 aborted（PG 关键）。"""

    def test_slug_conflict_retries_without_poisoning_transaction(self):
        url_a = "https://www.meishij.net/zuofa/a.html"
        url_b = "https://www.meishij.net/zuofa/b.html"
        with patch("websearch.providers.http_get", return_value=SAME_TITLE_HTML):
            first = import_recipe(url_a)
            taken = Recipe.objects.get(pk=first["recipe_id"]).slug
            # 第一次返回已被占用的 slug（触发 IntegrityError），第二次返回可用 slug
            with patch("websearch.ingest._build_slug", side_effect=[taken, "recipe-freshslug"]):
                second = import_recipe(url_b)

        self.assertTrue(second["created"])
        self.assertEqual(Recipe.objects.count(), 2)
        self.assertEqual(Recipe.objects.get(pk=second["recipe_id"]).slug, "recipe-freshslug")
        # 冲突后外层事务仍然可用：菜谱、食材、步骤、检索文本都完整落库
        recipe = Recipe.objects.get(pk=second["recipe_id"])
        self.assertEqual(recipe.ingredients.count(), 2)
        self.assertEqual(recipe.steps.count(), 2)
        self.assertIn("食材：", recipe.search_doc)


def _run_concurrently(worker, count: int) -> list[BaseException]:
    """在多个线程里并发执行 `worker(index)`，返回捕获到的异常列表。"""
    errors: list[BaseException] = []

    def runner(index: int) -> None:
        try:
            worker(index)
        except BaseException as exc:  # pragma: no cover - 只有出 bug 才会走到
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=runner, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


class ConcurrencyTests(TransactionTestCase):
    """并发写：缓存 `query_hash` 唯一约束的 upsert、导入去重（真实提交，非套娃事务）。"""

    def test_cache_upsert_concurrent_writes_single_row(self):
        """6 个线程同时写同一 query 的缓存 → 只有 1 行，且不抛 IntegrityError。"""
        payload = {"query": "并发麻婆豆腐", "results": [], "provider": "duckduckgo"}
        errors = _run_concurrently(
            lambda index: service._store_cache("并发麻婆豆腐", "duckduckgo", payload, False), 6
        )
        self.assertEqual(errors, [])
        rows = WebSearchCache.objects.filter(query="并发麻婆豆腐")
        self.assertEqual(rows.count(), 1)
        self.assertFalse(rows.first().degraded)

    def test_import_recipe_concurrent_dedupe(self):
        """4 个线程同时导入同一 URL → 只落 1 条菜谱，其余返回 created=False 的同一个 id。"""
        url = "https://www.meishij.net/zuofa/fanqieniunan_18.html"
        html = _fixture_text("recipe_jsonld_番茄牛腩.html")
        results: list[dict] = []

        with patch("websearch.providers.http_get", return_value=html):
            errors = _run_concurrently(lambda index: results.append(import_recipe(url)), 4)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(Recipe.objects.filter(source_url=url).count(), 1)
        self.assertEqual(len({item["recipe_id"] for item in results}), 1)
        self.assertEqual(sum(1 for item in results if item["created"]), 1)


class PortabilityTests(TestCase):
    """可移植性守卫：`websearch/` 源码里不允许出现 SQLite 专有 SQL / 硬编码连接串。"""

    FORBIDDEN_SQL = ("datetime('now')", 'datetime("now")', "strftime", "julianday", "PRAGMA", "sqlite_master")
    FORBIDDEN_DSN = ("sqlite3", "db.sqlite3", "5432", "postgres://", "postgresql://", "password=")

    def _sources(self):
        folder = Path(providers.__file__).resolve().parent
        for path in sorted(folder.glob("*.py")):
            if path.name == "tests.py":  # 本文件自带禁词清单，跳过自检
                continue
            yield path, path.read_text(encoding="utf-8")

    def test_no_sqlite_specific_sql(self):
        for path, source in self._sources():
            for pattern in self.FORBIDDEN_SQL:
                self.assertNotIn(pattern, source, f"{path.name} 含 SQLite 专有写法：{pattern}")

    def test_no_hardcoded_database_dsn(self):
        """数据库连接信息只能来自 settings/.env，源码里不得出现主机端口或密码。"""
        for path, source in self._sources():
            for pattern in self.FORBIDDEN_DSN:
                self.assertNotIn(pattern, source, f"{path.name} 出现疑似硬编码连接串：{pattern}")

    def test_only_providers_module_touches_network(self):
        """HTTP 访问集中在 providers.http_get；service / ingest 不得直接 import requests。"""
        folder = Path(providers.__file__).resolve().parent
        for name in ("service.py", "ingest.py"):
            source = (folder / name).read_text(encoding="utf-8")
            self.assertNotIn("import requests", source, f"{name} 不应直接使用 requests")
            self.assertNotIn("urlopen", source, f"{name} 不应直接打开网络连接")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
