"""美食推荐平台领域模型。

分层说明
- 主数据：Cuisine（菜系）/ Tag（标签）/ Ingredient（食材）/ Recipe（菜谱）
- 关系数据：RecipeIngredient（用量）/ RecipeStep（步骤）/ RecipeEmbedding（向量）
- 用户行为：Rating（评分）/ Favorite（收藏）/ Comment（评论）/ InteractionLog（行为埋点）
- 检索与运营：SearchLog（检索日志）/ WebSearchCache（网络搜索缓存）
- 对话：ChatSession / ChatMessage（RAG 助手会话历史，带引用）
"""
from __future__ import annotations

import hashlib
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify


def _make_slug(title: str, fallback: str = "recipe") -> str:
    """中文标题 slugify 常常为空，这里兜底为 拼音不可得时用短哈希。"""
    base = slugify(title, allow_unicode=False)
    if not base:
        base = fallback
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# 主数据
# ---------------------------------------------------------------------------
class Cuisine(TimeStampedModel):
    """菜系：川菜、粤菜、鲁菜、家常菜……"""

    name = models.CharField("菜系名称", max_length=40, unique=True)
    slug = models.SlugField("标识", max_length=60, unique=True, blank=True)
    region = models.CharField("地域", max_length=40, blank=True, default="")
    description = models.TextField("简介", blank=True, default="")
    icon = models.CharField("图标", max_length=16, blank=True, default="🍽️")
    sort_order = models.IntegerField("排序", default=0)

    class Meta:
        verbose_name = "菜系"
        verbose_name_plural = "菜系"
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or _make_slug(self.name, "cuisine")
        super().save(*args, **kwargs)


class Tag(TimeStampedModel):
    """标签：口味 / 烹饪方式 / 场景 / 人群饮食 / 营养。"""

    class Kind(models.TextChoices):
        TASTE = "taste", "口味"
        METHOD = "method", "烹饪方式"
        SCENE = "scene", "场景"
        DIET = "diet", "饮食偏好"
        NUTRITION = "nutrition", "营养"

    name = models.CharField("标签名", max_length=30)
    slug = models.SlugField("标识", max_length=60, blank=True)
    kind = models.CharField("类型", max_length=16, choices=Kind.choices, default=Kind.TASTE)
    color = models.CharField("颜色", max_length=16, blank=True, default="")

    class Meta:
        verbose_name = "标签"
        verbose_name_plural = "标签"
        unique_together = [("name", "kind")]
        ordering = ["kind", "name"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}·{self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or _make_slug(self.name, "tag")
        super().save(*args, **kwargs)


class Ingredient(TimeStampedModel):
    """食材主数据，含每 100g 营养估算，用于热量与营养画像。"""

    class Category(models.TextChoices):
        VEGETABLE = "vegetable", "蔬菜"
        MEAT = "meat", "肉类"
        POULTRY = "poultry", "禽类"
        SEAFOOD = "seafood", "水产"
        GRAIN = "grain", "主食谷物"
        DAIRY_EGG = "dairy_egg", "蛋奶"
        BEAN = "bean", "豆制品"
        SEASONING = "seasoning", "调味料"
        FRUIT = "fruit", "水果"
        OTHER = "other", "其他"

    name = models.CharField("食材名", max_length=40, db_index=True)
    alias = models.CharField("别名", max_length=120, blank=True, default="")
    category = models.CharField("分类", max_length=16, choices=Category.choices, default=Category.OTHER)
    unit = models.CharField("常用单位", max_length=8, default="克")
    calories_per_100g = models.FloatField("每100g热量(kcal)", default=0)
    protein_per_100g = models.FloatField("蛋白质(g)", default=0)
    fat_per_100g = models.FloatField("脂肪(g)", default=0)
    carbs_per_100g = models.FloatField("碳水(g)", default=0)
    is_common = models.BooleanField("常用食材", default=False)

    class Meta:
        verbose_name = "食材"
        verbose_name_plural = "食材"
        unique_together = [("name",)]
        ordering = ["category", "name"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# 菜谱
# ---------------------------------------------------------------------------
class RecipeQuerySet(models.QuerySet):
    def published(self):
        return self.filter(status=Recipe.Status.PUBLISHED)

    def with_relations(self):
        return self.select_related("cuisine", "author").prefetch_related(
            "tags", "ingredients__ingredient", "steps"
        )

    def hot(self, limit: int = 12):
        return self.published().order_by("-hot_score", "-rating_avg")[:limit]

    def search(self, keyword: str):
        if not keyword:
            return self
        return self.filter(
            Q(title__icontains=keyword)
            | Q(summary__icontains=keyword)
            | Q(search_doc__icontains=keyword)
            | Q(tags__name__icontains=keyword)
            | Q(ingredients__name_text__icontains=keyword)
        ).distinct()


class Recipe(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        ARCHIVED = "archived", "已下架"

    class Difficulty(models.IntegerChoices):
        EASY = 1, "简单"
        NORMAL = 2, "普通"
        MEDIUM = 3, "中等"
        HARD = 4, "较难"
        CHEF = 5, "大师"

    class Source(models.TextChoices):
        SEED = "seed", "内置数据"
        WEB = "web", "网络采集"
        USER = "user", "用户上传"
        AI = "ai", "AI 生成"

    title = models.CharField("菜名", max_length=120, db_index=True)
    slug = models.SlugField("标识", max_length=140, unique=True, blank=True)
    cuisine = models.ForeignKey(
        Cuisine, verbose_name="菜系", on_delete=models.SET_NULL, null=True, blank=True, related_name="recipes"
    )
    tags = models.ManyToManyField(Tag, verbose_name="标签", blank=True, related_name="recipes")
    author = models.ForeignKey(
        User, verbose_name="作者", on_delete=models.SET_NULL, null=True, blank=True, related_name="recipes"
    )

    summary = models.TextField("简介", blank=True, default="")
    cover_url = models.URLField("封面图", max_length=500, blank=True, default="")
    difficulty = models.IntegerField("难度", choices=Difficulty.choices, default=Difficulty.NORMAL)
    prep_minutes = models.PositiveIntegerField("准备时间(分钟)", default=10)
    cook_minutes = models.PositiveIntegerField("烹饪时间(分钟)", default=20)
    servings = models.PositiveIntegerField("份量(人)", default=2)
    tips = models.TextField("小贴士", blank=True, default="")

    # 营养（每份估算）
    calories = models.FloatField("热量(kcal)", default=0)
    protein = models.FloatField("蛋白质(g)", default=0)
    fat = models.FloatField("脂肪(g)", default=0)
    carbs = models.FloatField("碳水(g)", default=0)

    # 统计
    rating_avg = models.FloatField("平均分", default=0, db_index=True)
    rating_count = models.IntegerField("评分人数", default=0)
    favorite_count = models.IntegerField("收藏数", default=0)
    view_count = models.IntegerField("浏览数", default=0)
    hot_score = models.FloatField("热度分", default=0, db_index=True)

    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.PUBLISHED)
    source = models.CharField("来源", max_length=16, choices=Source.choices, default=Source.SEED)
    source_url = models.URLField("来源链接", max_length=500, blank=True, default="")
    source_site = models.CharField("来源站点", max_length=60, blank=True, default="")

    # 向量索引版本号：内容变化后 +1，索引构建时只处理变化项
    content_version = models.IntegerField("内容版本", default=1)
    search_doc = models.TextField("检索文本", blank=True, default="", editable=False)

    objects = RecipeQuerySet.as_manager()

    class Meta:
        verbose_name = "菜谱"
        verbose_name_plural = "菜谱"
        ordering = ["-hot_score", "-created_at"]
        indexes = [
            models.Index(fields=["status", "-hot_score"]),
            models.Index(fields=["cuisine", "status"]),
            models.Index(fields=["-created_at"]),
        ]
        constraints = [
            # 网络采集的菜谱按来源去重：非空 source_url 必须唯一（部分唯一索引，SQLite/PG 均支持）
            models.UniqueConstraint(
                fields=["source_url"],
                condition=~Q(source_url=""),
                name="uniq_recipe_source_url_nonempty",
            )
        ]

    def __str__(self) -> str:
        return self.title

    # -- 派生字段 ---------------------------------------------------------
    @property
    def total_minutes(self) -> int:
        return (self.prep_minutes or 0) + (self.cook_minutes or 0)

    @property
    def difficulty_label(self) -> str:
        return self.get_difficulty_display()

    @property
    def calories_per_serving(self) -> float:
        return round(self.calories / max(self.servings or 2, 1), 1)

    def build_search_doc(self, web_snippets: list[str] | None = None) -> str:
        """拼接用于 RAG 检索的文本块（标题 / 简介 / 食材 / 步骤 / 标签）。"""
        parts: list[str] = [self.title]
        if self.cuisine_id:
            parts.append(f"菜系：{self.cuisine.name}")
        if self.summary:
            parts.append(self.summary)
        tag_names = [t.name for t in self.tags.all()]
        if tag_names:
            parts.append("标签：" + "、".join(tag_names))
        ing_lines = []
        for item in self.ingredients.all():
            amount = " ".join(x for x in [item.amount, item.unit] if x)
            ing_lines.append(f"{item.name_text}{(' ' + amount) if amount else ''}")
        if ing_lines:
            parts.append("食材：" + "、".join(ing_lines))
        step_lines = [f"{step.index}. {step.text}" for step in self.steps.all()]
        if step_lines:
            parts.append("做法：" + " ".join(step_lines))
        if self.tips:
            parts.append("小贴士：" + self.tips)
        for snippet in web_snippets or []:
            if snippet:
                parts.append("网络资料：" + snippet)
        parts.append(
            f"难度：{self.get_difficulty_display()}；耗时：{self.total_minutes}分钟；"
            f"热量：约{int(self.calories)}千卡/{self.servings}人份"
        )
        return "\n".join(parts)

    def refresh_search_doc(self, save: bool = True, web_snippets: list[str] | None = None) -> str:
        self.search_doc = self.build_search_doc(web_snippets)
        self.content_version = (self.content_version or 0) + 1
        if save:
            super(Recipe, self).save(update_fields=["search_doc", "content_version", "updated_at"])
        return self.search_doc

    def recompute_rating(self) -> None:
        agg = self.ratings.aggregate(avg=models.Avg("score"), cnt=models.Count("id"))
        self.rating_avg = round(agg["avg"] or 0, 2)
        self.rating_count = agg["cnt"] or 0
        super(Recipe, self).save(update_fields=["rating_avg", "rating_count", "updated_at"])

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _make_slug(self.title or "recipe")
        super().save(*args, **kwargs)

    # -- 序列化 -----------------------------------------------------------
    def to_card(self) -> dict:
        """列表卡片（轻量）。"""
        tags = [t.name for t in self.tags.all()][:4]
        return {
            "id": self.id,
            "title": self.title,
            "slug": self.slug,
            "cover_url": self.cover_url,
            "summary": (self.summary or "")[:120],
            "cuisine": self.cuisine.name if self.cuisine_id else "",
            "tags": tags,
            "difficulty": self.difficulty,
            "difficulty_label": self.get_difficulty_display(),
            "total_minutes": self.total_minutes,
            "servings": self.servings,
            "calories": round(self.calories_per_serving, 1),
            "rating_avg": self.rating_avg,
            "rating_count": self.rating_count,
            "favorite_count": self.favorite_count,
            "hot_score": round(self.hot_score, 3),
        }

    def to_detail(self) -> dict:
        data = self.to_card()
        data.update(
            {
                "summary": self.summary,
                "tips": self.tips,
                "prep_minutes": self.prep_minutes,
                "cook_minutes": self.cook_minutes,
                "nutrition": {
                    "calories": self.calories,
                    "protein": self.protein,
                    "fat": self.fat,
                    "carbs": self.carbs,
                    "per_serving": round(self.calories / max(self.servings or 2, 1), 1),
                },
                "ingredients": [
                    {
                        "name": item.name_text,
                        "amount": item.amount,
                        "unit": item.unit,
                        "note": item.note,
                        "is_main": item.is_main,
                        "ingredient_id": item.ingredient_id,
                    }
                    for item in self.ingredients.all()
                ],
                "steps": [
                    {
                        "index": step.index,
                        "text": step.text,
                        "image_url": step.image_url,
                        "duration_min": step.duration_min,
                    }
                    for step in self.steps.all()
                ],
                "source": self.source,
                "source_url": self.source_url,
                "source_site": self.source_site,
                "author": getattr(self.author, "username", "") if self.author_id else "",
                "created_at": self.created_at.isoformat() if self.created_at else "",
            }
        )
        return data


class RecipeIngredient(models.Model):
    recipe = models.ForeignKey(Recipe, verbose_name="菜谱", on_delete=models.CASCADE, related_name="ingredients")
    ingredient = models.ForeignKey(
        Ingredient, verbose_name="食材", on_delete=models.SET_NULL, null=True, blank=True, related_name="usages"
    )
    name_text = models.CharField("食材名", max_length=60)
    amount = models.CharField("用量", max_length=30, blank=True, default="")
    unit = models.CharField("单位", max_length=16, blank=True, default="")
    note = models.CharField("备注", max_length=60, blank=True, default="")
    is_main = models.BooleanField("主料", default=True)
    sort_order = models.IntegerField("排序", default=0)

    class Meta:
        verbose_name = "菜谱食材"
        verbose_name_plural = "菜谱食材"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.name_text} {self.amount}{self.unit}"


class RecipeStep(models.Model):
    recipe = models.ForeignKey(Recipe, verbose_name="菜谱", on_delete=models.CASCADE, related_name="steps")
    index = models.PositiveIntegerField("步骤序号", default=1)
    text = models.TextField("步骤说明")
    image_url = models.URLField("示意图", max_length=500, blank=True, default="")
    duration_min = models.FloatField("耗时(分钟)", default=0)

    class Meta:
        verbose_name = "菜谱步骤"
        verbose_name_plural = "菜谱步骤"
        ordering = ["index", "id"]
        unique_together = [("recipe", "index")]

    def __str__(self) -> str:
        return f"{self.recipe_id}#{self.index}"


class RecipeEmbedding(models.Model):
    """菜谱向量（与 RAG 索引一一对应），vector 用 float32 原始字节存储。"""

    recipe = models.OneToOneField(Recipe, verbose_name="菜谱", on_delete=models.CASCADE, related_name="embedding")
    model_name = models.CharField("向量模型", max_length=60)
    dim = models.IntegerField("维度", default=0)
    vector = models.BinaryField("向量")
    content_version = models.IntegerField("内容版本", default=0)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "菜谱向量"
        verbose_name_plural = "菜谱向量"

    def __str__(self) -> str:
        return f"embedding<{self.recipe_id}>"


# ---------------------------------------------------------------------------
# 用户与行为
# ---------------------------------------------------------------------------
class UserProfile(TimeStampedModel):
    user = models.OneToOneField(User, verbose_name="用户", on_delete=models.CASCADE, related_name="profile")
    nickname = models.CharField("昵称", max_length=40, blank=True, default="")
    avatar = models.CharField("头像", max_length=300, blank=True, default="")
    bio = models.CharField("简介", max_length=200, blank=True, default="")

    preferred_cuisines = models.ManyToManyField(Cuisine, verbose_name="偏好菜系", blank=True, related_name="fans")
    preferred_tags = models.ManyToManyField(Tag, verbose_name="偏好标签", blank=True, related_name="fans")
    disliked_ingredients = models.JSONField("忌口食材", default=list, blank=True)
    diet_flags = models.JSONField("饮食标识", default=list, blank=True)  # vegetarian / low_cal / high_protein
    spicy_level = models.IntegerField("吃辣等级", default=2)  # 0-5
    max_cook_minutes = models.IntegerField("可接受耗时(分钟)", default=60)
    onboarded = models.BooleanField("已完成偏好引导", default=False)

    class Meta:
        verbose_name = "用户资料"
        verbose_name_plural = "用户资料"

    def __str__(self) -> str:
        return self.nickname or getattr(self.user, "username", f"user-{self.user_id}")

    @property
    def display_name(self) -> str:
        return self.nickname or getattr(self.user, "username", "")

    def ensure_onboarded(self) -> None:
        if not self.onboarded:
            self.onboarded = True
            self.save(update_fields=["onboarded", "updated_at"])


class Rating(TimeStampedModel):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="ratings")
    recipe = models.ForeignKey(Recipe, verbose_name="菜谱", on_delete=models.CASCADE, related_name="ratings")
    score = models.PositiveSmallIntegerField("评分", default=5)

    class Meta:
        verbose_name = "评分"
        verbose_name_plural = "评分"
        unique_together = [("user", "recipe")]
        indexes = [models.Index(fields=["recipe", "-score"])]

    def __str__(self) -> str:
        return f"{self.user_id}->{self.recipe_id}:{self.score}"


class Favorite(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="favorites")
    recipe = models.ForeignKey(Recipe, verbose_name="菜谱", on_delete=models.CASCADE, related_name="favorites")
    created_at = models.DateTimeField("收藏时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "收藏"
        verbose_name_plural = "收藏"
        unique_together = [("user", "recipe")]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user_id}♥{self.recipe_id}"


class Comment(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="comments")
    recipe = models.ForeignKey(Recipe, verbose_name="菜谱", on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey(
        "self", verbose_name="父评论", on_delete=models.CASCADE, null=True, blank=True, related_name="replies"
    )
    content = models.TextField("内容", max_length=1000)
    likes = models.IntegerField("点赞数", default=0)
    is_hidden = models.BooleanField("已隐藏", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "评论"
        verbose_name_plural = "评论"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipe", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.recipe_id}: {self.content[:20]}"


class InteractionLog(models.Model):
    """行为埋点：view / click / search / rate / favorite / cook_done。"""

    user = models.ForeignKey(
        User, verbose_name="用户", on_delete=models.SET_NULL, null=True, blank=True, related_name="interactions"
    )
    session_key = models.CharField("会话", max_length=64, blank=True, default="", db_index=True)
    recipe = models.ForeignKey(
        Recipe, verbose_name="菜谱", on_delete=models.CASCADE, null=True, blank=True, related_name="interactions"
    )
    action = models.CharField("行为", max_length=20, db_index=True)
    weight = models.FloatField("权重", default=1.0)
    extra = models.JSONField("附加信息", default=dict, blank=True)
    created_at = models.DateTimeField("发生时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "用户行为"
        verbose_name_plural = "用户行为"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"]), models.Index(fields=["action", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action}:{self.user_id}->{self.recipe_id}"


# ---------------------------------------------------------------------------
# 检索 / 搜索 / 对话
# ---------------------------------------------------------------------------
class SearchLog(models.Model):
    class Mode(models.TextChoices):
        KEYWORD = "keyword", "关键词"
        SEMANTIC = "semantic", "语义检索"
        ASSISTANT = "assistant", "AI 助手"
        WEB = "web", "网络搜索"

    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.SET_NULL, null=True, blank=True)
    query = models.CharField("查询", max_length=300, db_index=True)
    mode = models.CharField("模式", max_length=16, choices=Mode.choices, default=Mode.KEYWORD)
    result_count = models.IntegerField("结果数", default=0)
    latency_ms = models.FloatField("耗时(ms)", default=0)
    from_cache = models.BooleanField("命中缓存", default=False)
    created_at = models.DateTimeField("时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "检索日志"
        verbose_name_plural = "检索日志"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.mode}:{self.query}"


class WebSearchCache(models.Model):
    """网络搜索 / 抓取结果缓存，避免频繁请求外部站点。"""

    query_hash = models.CharField("查询哈希", max_length=64, unique=True)
    query = models.CharField("查询", max_length=300, db_index=True)
    provider = models.CharField("提供方", max_length=40, default="")
    payload = models.JSONField("结果", default=dict, blank=True)
    degraded = models.BooleanField("降级返回", default=False)
    hit_count = models.IntegerField("命中次数", default=0)
    fetched_at = models.DateTimeField("抓取时间", auto_now_add=True)
    expires_at = models.DateTimeField("过期时间", db_index=True)

    class Meta:
        verbose_name = "网络搜索缓存"
        verbose_name_plural = "网络搜索缓存"
        ordering = ["-fetched_at"]

    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def touch(self) -> None:
        type(self).objects.filter(pk=self.pk).update(hit_count=F("hit_count") + 1)

    def __str__(self) -> str:
        return f"{self.provider}:{self.query}"


class ChatSession(models.Model):
    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, null=True, blank=True)
    session_key = models.CharField("会话标识", max_length=64, unique=True)
    title = models.CharField("标题", max_length=120, blank=True, default="新的美食对话")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后活跃", auto_now=True, db_index=True)

    class Meta:
        verbose_name = "对话会话"
        verbose_name_plural = "对话会话"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title

    @classmethod
    def get_or_create_by_key(cls, session_key: str, user=None) -> "ChatSession":
        obj, _ = cls.objects.get_or_create(
            session_key=session_key or uuid.uuid4().hex, defaults={"user": user}
        )
        return obj


class ChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "用户"
        ASSISTANT = "assistant", "助手"
        SYSTEM = "system", "系统"

    session = models.ForeignKey(ChatSession, verbose_name="会话", on_delete=models.CASCADE, related_name="messages")
    role = models.CharField("角色", max_length=16, choices=Role.choices)
    content = models.TextField("内容")
    citations = models.JSONField("引用来源", default=list, blank=True)
    latency_ms = models.FloatField("耗时(ms)", default=0)
    token_usage = models.JSONField("token 用量", default=dict, blank=True)
    created_at = models.DateTimeField("时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "对话消息"
        verbose_name_plural = "对话消息"
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.role}:{self.content[:24]}"
