"""pytest 公共夹具。

约定：所有测试默认使用临时索引目录，避免污染真实 `data/index`；
需要真实索引的用例自己显式构建。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "RAGDjange.settings")
os.environ.setdefault("LLM_ENABLED", "0")          # 测试绝不调用真实模型
os.environ.setdefault("WEBSEARCH_OFFLINE_FALLBACK", "1")


@pytest.fixture
def tmp_index_dir(tmp_path, settings):
    """把 RAG 索引目录指向临时目录。"""
    settings.RAG = {**settings.RAG, "INDEX_DIR": str(tmp_path / "index")}
    return Path(settings.RAG["INDEX_DIR"])


@pytest.fixture
def sample_recipes(db):
    """最小可用菜谱集：4 道，覆盖不同菜系/口味，供推荐与检索测试复用。"""
    from food.models import Cuisine, Recipe, RecipeIngredient, RecipeStep

    data = [
        {
            "title": "麻婆豆腐",
            "cuisine": "川菜",
            "summary": "麻辣鲜香，豆腐嫩滑，下饭一绝。",
            "ingredients": ["北豆腐 400克", "牛肉末 80克", "豆瓣酱 1汤匙"],
            "steps": ["豆腐切块焯水", "炒香豆瓣酱与肉末", "下豆腐烧 3 分钟勾芡"],
            "calories": 380,
        },
        {
            "title": "清蒸鲈鱼",
            "cuisine": "粤菜",
            "summary": "鲜嫩清淡，突出鱼本味。",
            "ingredients": ["鲈鱼 1条", "姜 10克", "蒸鱼豉油 2汤匙"],
            "steps": ["鱼身划刀铺姜", "大火蒸 8 分钟", "淋豉油浇热油"],
            "calories": 260,
        },
        {
            "title": "番茄牛腩",
            "cuisine": "家常菜",
            "summary": "酸甜浓郁，牛腩软烂。",
            "ingredients": ["牛腩 500克", "番茄 3个", "洋葱 半个"],
            "steps": ["牛腩焯水", "番茄炒出沙", "小火炖 90 分钟"],
            "calories": 520,
        },
        {
            "title": "蒜蓉西兰花",
            "cuisine": "家常菜",
            "summary": "低卡快手，清脆爽口。",
            "ingredients": ["西兰花 300克", "大蒜 4瓣"],
            "steps": ["西兰花焯水 1 分钟", "爆香蒜末", "大火快炒调味"],
            "calories": 120,
        },
    ]
    cuisines = {}
    recipes = []
    for idx, item in enumerate(data):
        cuisine, _ = Cuisine.objects.get_or_create(name=item["cuisine"])
        cuisines[item["cuisine"]] = cuisine
        recipe = Recipe.objects.create(
            title=item["title"],
            cuisine=cuisine,
            summary=item["summary"],
            calories=item["calories"],
            prep_minutes=10 + idx,
            cook_minutes=10 + idx * 2,
            servings=2,
            rating_avg=4.5 - idx * 0.2,
            rating_count=10,
            favorite_count=5,
            view_count=100,
            status=Recipe.Status.PUBLISHED,
        )
        for order, text in enumerate(item["ingredients"]):
            name, _, amount = text.partition(" ")
            RecipeIngredient.objects.create(
                recipe=recipe, name_text=name, amount=amount, unit="", sort_order=order
            )
        for order, text in enumerate(item["steps"], start=1):
            RecipeStep.objects.create(recipe=recipe, index=order, text=text)
        recipe.refresh_search_doc()
        recipes.append(recipe)
    return recipes
