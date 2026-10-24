"""places 路由（挂载在 /api/v1/places/）。"""
from django.urls import path

from places import api

app_name = "places"

urlpatterns = [
    # 配置与运维
    path("config/", api.config, name="config"),
    path("keys/", api.key_status, name="key_status"),
    path("keys/reset/", api.key_reset, name="key_reset"),
    # 核心：附近搜索
    path("nearby/", api.nearby, name="nearby"),
    path("regeo/", api.regeo, name="regeo"),
    path("locate/", api.locate, name="locate"),
    path("tips/", api.tips, name="tips"),
    # 用户资产（放在 <poi_id> 之前，避免被吞）
    path("favorites/", api.my_favorites, name="my_favorites"),
    path("track/", api.track, name="track"),
    # POI 详情与互动
    path("<str:poi_id>/", api.detail, name="detail"),
    path("<str:poi_id>/favorite/", api.favorite, name="favorite"),
    path("<str:poi_id>/reviews/", api.reviews, name="reviews"),
    path("<str:poi_id>/checkin/", api.checkin, name="checkin"),
]
