"""REST API 路由（/api/v1/）。

统一返回结构：{"code": 0, "msg": "ok", "data": ...}；出错时 code != 0 且带 HTTP 状态码。
"""
from django.urls import path

from food import api

app_name = "api"

urlpatterns = [
    # 元数据
    path("meta/", api.meta, name="meta"),
    path("health/", api.health, name="health"),
    # 认证
    path("auth/send_code/", api.send_code, name="send_code"),
    path("auth/login/", api.login_with_code, name="login"),
    path("auth/logout/", api.logout, name="logout"),
    path("auth/me/", api.me, name="me"),
    # 菜谱
    path("recipes/", api.recipe_list, name="recipe_list"),
    path("recipes/<int:pk>/", api.recipe_detail, name="recipe_detail"),
    path("recipes/<int:pk>/rate/", api.recipe_rate, name="recipe_rate"),
    path("recipes/<int:pk>/favorite/", api.recipe_favorite, name="recipe_favorite"),
    path("recipes/<int:pk>/comments/", api.recipe_comments, name="recipe_comments"),
    path("recipes/<int:pk>/similar/", api.recipe_similar, name="recipe_similar"),
    # 检索 / 推荐
    path("search/", api.search, name="search"),
    path("search/semantic/", api.semantic_search, name="semantic_search"),
    path("feed/", api.feed, name="feed"),
    # 助手
    path("assistant/chat/", api.assistant_chat, name="assistant_chat"),
    path("assistant/chat/sync/", api.assistant_chat_sync, name="assistant_chat_sync"),
    path("assistant/history/", api.assistant_history, name="assistant_history"),
    path("assistant/debug/", api.assistant_debug, name="assistant_debug"),
    # 网络搜索
    path("websearch/", api.websearch, name="websearch"),
    path("websearch/import/", api.websearch_import, name="websearch_import"),
    # 用户
    path("profile/", api.profile, name="profile"),
    path("favorites/", api.my_favorites, name="favorites"),
    path("interactions/", api.track_interaction, name="interactions"),
    # 运维
    path("admin/rebuild_index/", api.rebuild_index, name="rebuild_index"),
]
