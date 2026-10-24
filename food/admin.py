"""Django 管理后台：菜谱运营、用户行为、检索与搜索日志。"""
from __future__ import annotations

from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html

from food.models import (
    ChatMessage,
    ChatSession,
    Comment,
    Cuisine,
    Favorite,
    Ingredient,
    InteractionLog,
    Rating,
    Recipe,
    RecipeEmbedding,
    RecipeIngredient,
    RecipeStep,
    SearchLog,
    Tag,
    UserProfile,
    WebSearchCache,
)


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    extra = 1
    autocomplete_fields = ["ingredient"]


class RecipeStepInline(admin.TabularInline):
    model = RecipeStep
    extra = 1


@admin.register(Cuisine)
class CuisineAdmin(admin.ModelAdmin):
    list_display = ("name", "region", "icon", "recipe_count", "sort_order")
    search_fields = ("name", "region")
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_n=Count("recipes"))

    @admin.display(description="菜谱数", ordering="_n")
    def recipe_count(self, obj):
        return obj._n


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "color_chip", "slug")
    list_filter = ("kind",)
    search_fields = ("name",)

    @admin.display(description="颜色")
    def color_chip(self, obj):
        if not obj.color:
            return "-"
        return format_html('<span style="background:{};padding:2px 10px;border-radius:8px">{}</span>', obj.color, obj.color)


@admin.register(Ingredient)
class IngredientAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "calories_per_100g", "protein_per_100g", "is_common")
    list_filter = ("category", "is_common")
    search_fields = ("name", "alias")
    list_editable = ("is_common",)


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "cuisine",
        "difficulty",
        "total_minutes",
        "calories_display",
        "rating_avg",
        "favorite_count",
        "hot_score",
        "status",
        "source",
    )
    list_filter = ("status", "source", "cuisine", "difficulty", "tags")
    search_fields = ("title", "summary", "search_doc")
    inlines = [RecipeIngredientInline, RecipeStepInline]
    prepopulated_fields = {"slug": ("title",)}
    filter_horizontal = ("tags",)
    readonly_fields = ("rating_avg", "rating_count", "favorite_count", "view_count", "hot_score", "search_doc", "content_version")
    date_hierarchy = "created_at"
    actions = ["rebuild_search_doc", "recompute_rating_action", "publish", "archive"]

    fieldsets = (
        ("基础信息", {"fields": ("title", "slug", "cuisine", "tags", "author", "summary", "cover_url")}),
        ("工艺", {"fields": ("difficulty", "prep_minutes", "cook_minutes", "servings", "tips")}),
        ("营养（整道菜估算）", {"fields": ("calories", "protein", "fat", "carbs")}),
        ("来源与状态", {"fields": ("status", "source", "source_url", "source_site")}),
        ("统计（只读）", {"fields": ("rating_avg", "rating_count", "favorite_count", "view_count", "hot_score", "content_version")}),
        ("检索文本（只读）", {"fields": ("search_doc",), "classes": ("collapse",)}),
    )

    @admin.display(description="耗时")
    def total_minutes(self, obj):
        return f"{obj.total_minutes} 分钟"

    @admin.display(description="热量")
    def calories_display(self, obj):
        return f"{int(obj.calories)} kcal"

    @admin.action(description="重建检索文本（search_doc）")
    def rebuild_search_doc(self, request, queryset):
        for recipe in queryset:
            recipe.refresh_search_doc()
        self.message_user(request, f"已重建 {queryset.count()} 条检索文本")

    @admin.action(description="重算评分聚合")
    def recompute_rating_action(self, request, queryset):
        for recipe in queryset:
            recipe.recompute_rating()
        self.message_user(request, f"已重算 {queryset.count()} 条评分")

    @admin.action(description="发布")
    def publish(self, request, queryset):
        updated = queryset.update(status=Recipe.Status.PUBLISHED)
        self.message_user(request, f"已发布 {updated} 条")

    @admin.action(description="下架")
    def archive(self, request, queryset):
        updated = queryset.update(status=Recipe.Status.ARCHIVED)
        self.message_user(request, f"已下架 {updated} 条")


@admin.register(RecipeEmbedding)
class RecipeEmbeddingAdmin(admin.ModelAdmin):
    list_display = ("recipe", "model_name", "dim", "content_version", "updated_at")
    search_fields = ("recipe__title",)
    readonly_fields = ("recipe", "model_name", "dim", "vector", "content_version", "updated_at")


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ("user", "recipe", "score", "created_at")
    list_filter = ("score",)
    search_fields = ("user__username", "recipe__title")
    autocomplete_fields = ("recipe",)


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ("user", "recipe", "created_at")
    search_fields = ("user__username", "recipe__title")
    autocomplete_fields = ("recipe",)


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("user", "recipe", "short_content", "likes", "is_hidden", "created_at")
    list_filter = ("is_hidden",)
    search_fields = ("user__username", "recipe__title", "content")
    actions = ["hide_comments"]

    @admin.display(description="内容")
    def short_content(self, obj):
        return obj.content[:40]

    @admin.action(description="隐藏选中评论")
    def hide_comments(self, request, queryset):
        self.message_user(request, f"已隐藏 {queryset.update(is_hidden=True)} 条评论")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "nickname", "spicy_level", "max_cook_minutes", "onboarded")
    search_fields = ("user__username", "user__email", "nickname")
    filter_horizontal = ("preferred_cuisines", "preferred_tags")


@admin.register(InteractionLog)
class InteractionLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "recipe", "weight", "created_at")
    list_filter = ("action",)
    search_fields = ("user__username", "recipe__title")
    date_hierarchy = "created_at"


@admin.register(SearchLog)
class SearchLogAdmin(admin.ModelAdmin):
    list_display = ("query", "mode", "result_count", "latency_ms", "from_cache", "created_at")
    list_filter = ("mode", "from_cache")
    search_fields = ("query",)
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in SearchLog._meta.fields]


@admin.register(WebSearchCache)
class WebSearchCacheAdmin(admin.ModelAdmin):
    list_display = ("query", "provider", "degraded", "hit_count", "fetched_at", "expires_at")
    list_filter = ("provider", "degraded")
    search_fields = ("query",)


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("role", "content", "citations", "latency_ms", "created_at")
    can_delete = False


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "session_key", "message_count", "updated_at")
    search_fields = ("title", "session_key", "user__username")
    inlines = [ChatMessageInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_n=Count("messages"))

    @admin.display(description="消息数", ordering="_n")
    def message_count(self, obj):
        return obj._n


admin.site.site_header = "寻味 · 美食推荐平台"
admin.site.site_title = "寻味后台"
admin.site.index_title = "运营管理"
