"""菜谱采集入库：抓取 → 解析 → 去重 → 落库。

对外只提供 `import_recipe(url, user=None)`：

1. `source_url` 已存在同源菜谱时直接返回（`created=False`），避免重复入库；
2. 调用 `websearch.providers.fetch_recipe` 抓取并与解析（JSON-LD 优先，站点规则兜底）；
3. 解析失败抛 `ValueError`（`food/api.py` 会转 502），绝不静默创建空菜谱；
4. 自动关联 / 创建 `Cuisine`、`Tag`、`Ingredient`、`RecipeIngredient`、`RecipeStep`；
5. 页面没给热量时按食材重量与常见食材营养表做保守估算；
6. 最后 `recipe.refresh_search_doc()` 刷新 RAG 检索文本。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Any

from django.db import IntegrityError, connection, transaction

from food.models import (
    Cuisine,
    Ingredient,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    Tag,
)
from websearch.providers import fetch_recipe

logger = logging.getLogger("websearch")

# ---------------------------------------------------------------------------
# 单位与营养估算
# ---------------------------------------------------------------------------
#: 可换算成克数的单位
_UNIT_GRAMS: dict[str, float] = {
    "克": 1.0,
    "g": 1.0,
    "G": 1.0,
    "千克": 1000.0,
    "kg": 1000.0,
    "公斤": 1000.0,
    "斤": 500.0,
    "两": 50.0,
    "毫升": 1.0,
    "ml": 1.0,
    "ML": 1.0,
    "升": 1000.0,
    "汤匙": 15.0,
    "大匙": 15.0,
    "汤勺": 15.0,
    "茶匙": 5.0,
    "小匙": 5.0,
    "勺": 10.0,
    "碗": 250.0,
    "杯": 240.0,
}

#: 计数类单位
_PIECE_UNITS = {"个", "只", "根", "条", "片", "块", "瓣", "颗", "把", "段", "枚", "张"}

#: 计数单位对应的单件重量（克），按食材关键词匹配
_PIECE_WEIGHT: tuple[tuple[tuple[str, ...], float], ...] = (
    (("番茄", "西红柿"), 150.0),
    (("牛腩", "牛腱", "牛肉"), 400.0),
    (("鸡腿", "鸡翅", "整鸡"), 220.0),
    (("鸡蛋", "鸭蛋", "蛋"), 55.0),
    (("豆腐",), 400.0),
    (("土豆", "马铃薯"), 150.0),
    (("洋葱",), 180.0),
    (("大蒜", "蒜"), 5.0),
    (("姜",), 8.0),
    (("葱",), 3.0),
    (("辣椒", "小米椒", "干辣椒"), 5.0),
    (("鱼",), 500.0),
    (("虾",), 12.0),
    (("面条", "意面"), 100.0),
    (("米",), 150.0),
    (("西兰花", "青菜", "白菜", "菠菜", "生菜"), 300.0),
)

#: 每 100g 营养估算：(关键词, 热量, 蛋白质, 脂肪, 碳水, 分类)
_NUTRITION_RULES: tuple[tuple[tuple[str, ...], float, float, float, float, str], ...] = (
    (("牛腩", "牛腱", "牛肉"), 250.0, 17.0, 20.0, 0.0, "meat"),
    (("五花肉", "猪肉", "里脊", "肉末", "排骨"), 330.0, 15.0, 29.0, 0.0, "meat"),
    (("鸡胸", "鸡腿", "鸡肉", "鸡翅", "鸡"), 170.0, 19.0, 9.0, 0.0, "poultry"),
    (("鲈鱼", "鲫鱼", "草鱼", "鱼"), 105.0, 18.0, 3.0, 0.0, "seafood"),
    (("虾", "蟹", "蛤"), 95.0, 18.0, 1.5, 2.0, "seafood"),
    (("豆腐", "豆干", "腐竹", "豆皮"), 90.0, 8.5, 4.5, 3.0, "bean"),
    (("鸡蛋", "鸭蛋", "蛋"), 143.0, 13.0, 10.0, 1.0, "dairy_egg"),
    (("牛奶", "奶油", "芝士", "奶酪"), 250.0, 4.0, 20.0, 6.0, "dairy_egg"),
    (("米饭", "大米", "面条", "面粉", "粉丝", "年糕", "馒头"), 300.0, 7.0, 1.5, 68.0, "grain"),
    (("土豆", "马铃薯", "红薯", "山药"), 80.0, 2.0, 0.2, 17.0, "vegetable"),
    (("番茄", "西红柿", "黄瓜", "生菜", "白菜", "青菜", "菠菜", "西兰花", "茄子", "青椒", "豆角"), 30.0, 1.8, 0.3, 6.0, "vegetable"),
    (("洋葱", "胡萝卜", "萝卜"), 40.0, 1.1, 0.2, 9.0, "vegetable"),
    (("油", "黄油", "猪油"), 900.0, 0.0, 100.0, 0.0, "seasoning"),
    (("糖", "蜂蜜", "冰糖"), 400.0, 0.0, 0.0, 100.0, "seasoning"),
    (("蒜", "姜", "葱", "香菜", "辣椒", "花椒", "八角", "桂皮", "香叶"), 40.0, 1.5, 0.4, 8.0, "seasoning"),
    (("盐", "酱油", "生抽", "老抽", "醋", "料酒", "豆瓣酱", "蚝油", "味精", "鸡精", "淀粉", "胡椒"), 60.0, 3.0, 0.5, 8.0, "seasoning"),
)
_NUTRITION_DEFAULT = (120.0, 4.0, 3.0, 15.0, "other")
_PIECE_WEIGHT_DEFAULT = 80.0
_SEASONING_HINTS = ("盐", "糖", "酱", "醋", "油", "料酒", "淀粉", "胡椒", "花椒", "味精", "鸡精")

# ---------------------------------------------------------------------------
# 菜系 / 标签推断
# ---------------------------------------------------------------------------
_CUISINE_HINTS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("麻婆", "回锅", "宫保", "水煮", "鱼香", "口水", "辣子", "夫妻肺片", "蒜泥白肉"), "川菜", "四川·重庆", "🌶️"),
    (("清蒸", "白灼", "煲汤", "叉烧", "虾饺", "烧鹅", "豉油"), "粤菜", "广东", "🍲"),
    (("糖醋", "葱烧", "九转", "把子肉"), "鲁菜", "山东", "🥢"),
    (("剁椒", "小炒", "腊味", "毛氏"), "湘菜", "湖南", "🌶"),
    (("东坡肉", "西湖", "龙井", "叫花鸡"), "浙菜", "浙江", "🐟"),
    (("狮子头", "盐水鸭", "松鼠桂鱼"), "苏菜", "江苏", "🍜"),
    (("寿司", "味噌", "照烧", "天妇罗", "亲子丼"), "日式料理", "日本", "🍣"),
    (("咖喱", "冬阴功", "泰式"), "东南亚菜", "东南亚", "🍛"),
    (("意面", "披萨", "罗勒"), "西餐", "欧美", "🍝"),
    (("家常", "番茄", "红烧", "土豆丝", "西红柿"), "家常菜", "家常", "🏠"),
)

_NUTRITION_WORDS = ("高蛋白", "低卡", "低脂", "低糖", "高纤", "补钙", "减脂")
_DIET_WORDS = ("素食", "纯素", "全素", "斋", "无麸质")
_METHOD_WORDS = ("炒", "蒸", "炖", "煮", "炸", "烤", "拌", "焖", "烧", "煎", "卤", "汆", "焗", "烘焙", "凉拌", "烩")
_TASTE_WORDS = ("麻辣", "香辣", "微辣", "酸甜", "咸鲜", "清淡", "酱香", "蒜香", "五香", "椒盐", "孜然", "咖喱", "鲜香", "甜")


def _number(text: str) -> float:
    """从字符串里抠出第一个数字（支持 1/2 这种分数写法）。"""
    value = (text or "").strip()
    if not value:
        return 0.0
    fraction = re.match(r"^(\d+(?:\.\d+)?)\s*[/／]\s*(\d+(?:\.\d+)?)$", value)
    if fraction:
        denominator = float(fraction.group(2)) or 1.0
        return float(fraction.group(1)) / denominator
    digits = re.search(r"\d+(?:\.\d+)?", value)
    return float(digits.group()) if digits else 0.0


def _matches(name: str, keywords: tuple[str, ...]) -> bool:
    """食材名是否命中关键词。"""
    return any(keyword in name for keyword in keywords)


def guess_nutrition(name: str) -> tuple[float, float, float, float, str]:
    """按食材名估算 (热量, 蛋白质, 脂肪, 碳水, 分类)，单位均为每 100g。"""
    for keywords, calories, protein, fat, carbs, category in _NUTRITION_RULES:
        if _matches(name, keywords):
            return calories, protein, fat, carbs, category
    return _NUTRITION_DEFAULT


def _piece_weight(name: str) -> float:
    """估算“1 个 / 1 根 / 1 片”这类计数单位对应的克数。"""
    for keywords, grams in _PIECE_WEIGHT:
        if _matches(name, keywords):
            return grams
    return _PIECE_WEIGHT_DEFAULT


def estimate_grams(name: str, amount: str, unit: str) -> float:
    """把 "500" + "克" / "3" + "个" 估算成克数；"适量" 等模糊用量给小值。"""
    unit = (unit or "").strip()
    amount_value = _number(amount)
    if unit in _UNIT_GRAMS:
        return amount_value * _UNIT_GRAMS[unit]
    if unit in _PIECE_UNITS:
        return amount_value * _piece_weight(name)
    if not unit and amount_value:
        # 无单位时按克处理（中文菜谱常见 "牛腩 500"）
        return amount_value
    if not amount_value:
        # "适量 / 少许"：调味料按 5g，其他按 30g
        return 5.0 if _matches(name, _SEASONING_HINTS) else 30.0
    return amount_value * _piece_weight(name)


def estimate_nutrition(ingredients: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    """按食材克数估算整道菜的营养总量（热量 kcal / 蛋白质 / 脂肪 / 碳水）。"""
    calories = protein = fat = carbs = 0.0
    for item in ingredients:
        name = str(item.get("name") or "")
        grams = estimate_grams(name, str(item.get("amount") or ""), str(item.get("unit") or ""))
        if grams <= 0:
            continue
        per_100_cal, per_100_protein, per_100_fat, per_100_carbs, _ = guess_nutrition(name)
        ratio = grams / 100.0
        calories += per_100_cal * ratio
        protein += per_100_protein * ratio
        fat += per_100_fat * ratio
        carbs += per_100_carbs * ratio
    return round(calories, 1), round(protein, 1), round(fat, 1), round(carbs, 1)


def guess_cuisine(title: str, summary: str = "", hint: str = "") -> tuple[str, str, str]:
    """推断菜系，返回 (名称, 地域, 图标)；无法判断返回空名称。"""
    hint = (hint or "").strip()
    if hint:
        for _, name, region, icon in _CUISINE_HINTS:
            if name == hint:
                return name, region, icon
        return hint, "", "🍽️"
    text = f"{title} {summary}"
    for keywords, name, region, icon in _CUISINE_HINTS:
        if _matches(text, keywords):
            return name, region, icon
    return "", "", ""


def guess_tag_kind(name: str) -> str:
    """给标签猜一个 `Tag.Kind`（nutrition/diet/method/taste）。"""
    if _matches(name, _NUTRITION_WORDS):
        return Tag.Kind.NUTRITION
    if _matches(name, _DIET_WORDS):
        return Tag.Kind.DIET
    if _matches(name, _METHOD_WORDS):
        return Tag.Kind.METHOD
    if _matches(name, _TASTE_WORDS):
        return Tag.Kind.TASTE
    return Tag.Kind.TASTE


def guess_difficulty(step_count: int, total_minutes: int) -> int:
    """按步骤数与总耗时粗略评定难度（1~5）。"""
    if total_minutes >= 150 or step_count >= 12:
        return 4
    if total_minutes >= 90 or step_count >= 9:
        return 3
    if total_minutes >= 40 or step_count >= 5:
        return 2
    return 1


def _clamp_minutes(value: Any, fallback: int) -> int:
    """把分钟数收敛到 >=1 的合理区间。"""
    try:
        minutes = int(value or 0)
    except (TypeError, ValueError):
        minutes = 0
    return minutes if minutes > 0 else fallback


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def _get_or_create_ingredient(name: str) -> Ingredient:
    """按名称取食材主数据，缺失时按营养表创建。

    用 `get_or_create`（内部带 savepoint）而不是「先查后插」，避免 PostgreSQL 上
    两个导入并发时撞 `Ingredient.name` 唯一约束把外层事务打成 aborted。
    """
    cleaned = (name or "").strip()[:40]
    calories, protein, fat, carbs, category = guess_nutrition(cleaned)
    ingredient, _ = Ingredient.objects.get_or_create(
        name=cleaned,
        defaults={
            "category": category,
            "calories_per_100g": calories,
            "protein_per_100g": protein,
            "fat_per_100g": fat,
            "carbs_per_100g": carbs,
            "is_common": category != Ingredient.Category.SEASONING,
        },
    )
    return ingredient


def _resolve_cuisine(name: str, region: str, icon: str) -> Cuisine | None:
    """按名称取 / 建菜系。"""
    cleaned = (name or "").strip()[:40]
    if not cleaned:
        return None
    cuisine, _ = Cuisine.objects.get_or_create(
        name=cleaned,
        defaults={"region": region[:40], "icon": icon or "🍽️"},
    )
    return cuisine


def _attach_tags(recipe: Recipe, names: list[str]) -> None:
    """按关键词批量创建 / 关联标签。"""
    tags: list[Tag] = []
    for raw in names:
        name = (raw or "").strip()[:30]
        if not name:
            continue
        tag, _ = Tag.objects.get_or_create(name=name, kind=guess_tag_kind(name))
        tags.append(tag)
    if tags:
        recipe.tags.add(*tags)


def _summary_from(data: dict[str, Any], ingredients: list[dict[str, Any]]) -> str:
    """没有简介时用食材列表拼一句。"""
    summary = str(data.get("summary") or "").strip()
    if summary:
        return summary[:1000]
    names = [str(item.get("name") or "") for item in ingredients[:6] if item.get("name")]
    return ("主料：" + "、".join(names)) if names else ""


def _existing_payload(recipe: Recipe) -> dict:
    """已存在菜谱的返回结构（`created=False`）。"""
    return {
        "recipe_id": recipe.id,
        "title": recipe.title,
        "created": False,
        "ingredients": recipe.ingredients.count(),
        "steps": recipe.steps.count(),
    }


#: 同一 URL 的导入互斥锁（进程内）；PG 上另有事务级 advisory lock 兜跨进程并发
_IMPORT_LOCKS: dict[str, threading.Lock] = {}
_IMPORT_LOCKS_GUARD = threading.Lock()


def _import_lock(url: str) -> threading.Lock:
    """取某个 URL 的进程内互斥锁。"""
    with _IMPORT_LOCKS_GUARD:
        return _IMPORT_LOCKS.setdefault(url, threading.Lock())


def _acquire_db_lock(url: str) -> None:
    """在数据库层串行化同一 URL 的导入。

    PostgreSQL 用**事务级** advisory lock（随所在事务提交/回滚自动释放，
    不会泄漏），键由 md5 派生，避免依赖未文档化的 `hashtext()`；
    其他后端（SQLite）直接跳过，由进程内锁 + `transaction.atomic()` 兜底。
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(('x' || substr(md5(%s), 1, 16))::bit(64)::bigint)",
            [url[:200]],
        )


def _build_slug(title: str, url: str, attempt: int = 0) -> str:
    """生成稳定且基本唯一的 slug（中文标题 slugify 为空时用 "recipe-<短哈希>"）。"""
    digest = hashlib.md5(f"{title}|{url}|{attempt}".encode("utf-8")).hexdigest()[:8]
    ascii_part = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40]
    return f"{ascii_part or 'recipe'}-{digest}"[:140]


def _create_recipe(fields: dict[str, Any]) -> Recipe:
    """创建菜谱；slug 与既有菜谱撞车时换一个 slug 重试。

    同名菜谱（不同 source_url）在真实站点很常见，而 `Recipe.slug` 是唯一索引；
    每轮重试都包在 savepoint 里，确保 PostgreSQL 上的 IntegrityError 不会把
    外层事务打成 aborted 状态。
    """
    last_error: IntegrityError | None = None
    for attempt in range(4):
        try:
            with transaction.atomic():  # savepoint：只回滚这条 INSERT
                return Recipe.objects.create(
                    slug=_build_slug(fields["title"], fields["source_url"], attempt),
                    **fields,
                )
        except IntegrityError as exc:
            last_error = exc
            logger.warning("菜谱 slug 冲突，换 slug 重试（attempt=%d）：%s", attempt + 1, exc)
    raise last_error or ValueError("创建菜谱失败")


def import_recipe(url: str, user=None) -> dict:
    """抓取并入库一个网络菜谱。

    返回 `{"recipe_id","title","created","ingredients","steps"}`；
    同一 `source_url` 重复导入时 `created=False` 并返回已有菜谱 id。

    并发安全（PostgreSQL）
    - `Recipe.source_url` 在模型层**没有**唯一约束，所以这里用「进程内锁 +
      PG 事务级 advisory lock + 事务内二次确认」三重手段收窄竞态窗口；
      `Ingredient/Cuisine/Tag` 全部走 `get_or_create`（自带 savepoint）。
    - 若要彻底杜绝并发重复，需要在 `food/models.py` 加
      `UniqueConstraint(fields=["source_url"], condition=Q(source_url__gt=""))`
      —— 该文件不归本模块维护，已把建议同步给 Lead。
    """
    target = (url or "").strip()
    if not target.lower().startswith("http"):
        raise ValueError(f"非法菜谱链接：{url}")

    # 快速路径：已有同源菜谱（不抓网络、不加锁）
    existing = Recipe.objects.filter(source_url=target).first()
    if existing is not None:
        logger.info("菜谱已存在，跳过抓取：%s -> %s", target, existing.id)
        return _existing_payload(existing)

    data = fetch_recipe(target)  # 网络失败抛 WebSearchError，解析失败抛 ValueError
    title = str(data.get("title") or "").strip()[:120]
    ingredients_raw = list(data.get("ingredients") or [])
    steps_raw = list(data.get("steps") or [])
    if not title:
        raise ValueError(f"菜谱缺少标题：{target}")
    if not ingredients_raw and not steps_raw:
        raise ValueError(f"菜谱缺少食材与步骤：{target}")

    total_calories, total_protein, total_fat, total_carbs = estimate_nutrition(ingredients_raw)
    # 页面未提供热量时用食材估算（蛋白质/脂肪/碳水一律按食材估算）
    calories = float(data.get("calories") or 0) or total_calories

    prep_minutes = _clamp_minutes(data.get("prep_minutes"), 10)
    cook_minutes = _clamp_minutes(data.get("cook_minutes"), 20)
    servings = max(1, int(data.get("servings") or 2))
    cuisine_name, region, icon = guess_cuisine(title, str(data.get("summary") or ""), str(data.get("cuisine") or ""))
    difficulty = guess_difficulty(len(steps_raw), prep_minutes + cook_minutes)

    author = user if getattr(user, "is_authenticated", False) else None

    with _import_lock(target):
        with transaction.atomic():
            _acquire_db_lock(target)
            # 事务内二次确认：另一进程/线程可能刚刚插入同 source_url 的菜谱
            duplicate = Recipe.objects.filter(source_url=target).first()
            if duplicate is not None:
                logger.info("并发导入命中已有菜谱：%s -> %s", target, duplicate.id)
                return _existing_payload(duplicate)

            recipe = _create_recipe(
                {
                    "title": title,
                    "cuisine": _resolve_cuisine(cuisine_name, region, icon),
                    "author": author,
                    "summary": _summary_from(data, ingredients_raw),
                    "cover_url": str(data.get("cover_url") or "")[:500],
                    "difficulty": difficulty,
                    "prep_minutes": prep_minutes,
                    "cook_minutes": cook_minutes,
                    "servings": servings,
                    "tips": "",
                    "calories": round(calories, 1),
                    "protein": total_protein,
                    "fat": total_fat,
                    "carbs": total_carbs,
                    "status": Recipe.Status.PUBLISHED,
                    "source": Recipe.Source.WEB,
                    "source_url": target,
                    "source_site": str(data.get("source_site") or "")[:60],
                }
            )

            for order, item in enumerate(ingredients_raw):
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                RecipeIngredient.objects.create(
                    recipe=recipe,
                    ingredient=_get_or_create_ingredient(name),
                    name_text=name[:60],
                    amount=str(item.get("amount") or "")[:30],
                    unit=str(item.get("unit") or "")[:16],
                    is_main=order < 3,
                    sort_order=order,
                )

            for index, item in enumerate(steps_raw, start=1):
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                RecipeStep.objects.create(
                    recipe=recipe,
                    index=index,
                    text=text,
                    image_url=str(item.get("image_url") or "")[:500],
                    duration_min=0,
                )

            _attach_tags(recipe, list(data.get("tags") or []))
            recipe.refresh_search_doc()
            recipe.refresh_from_db(fields=["content_version", "search_doc"])

    ingredient_count = recipe.ingredients.count()
    step_count = recipe.steps.count()
    logger.info("菜谱入库成功 id=%s title=%s 食材=%d 步骤=%d 来源=%s", recipe.id, title, ingredient_count, step_count, target)

    return {
        "recipe_id": recipe.id,
        "title": recipe.title,
        "created": True,
        "ingredients": ingredient_count,
        "steps": step_count,
    }


__all__ = ["import_recipe", "estimate_nutrition", "estimate_grams", "guess_nutrition", "guess_cuisine"]
