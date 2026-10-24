"""URL 总路由。

约定
- ``/``                    登录页
- ``/app/``                平台主界面（推荐流 / 搜索 / 详情 / 助手）
- ``/admin/``              Django 管理后台
- ``/api/v1/...``          REST 接口（见 food/api_urls.py）
"""
from django.contrib import admin
from django.urls import include, path

from . import views as legacy_views
from .views import health, index_html, login_html, logout_view

urlpatterns = [
    path("admin/", admin.site.urls),
    # 页面
    path("", login_html, name="login_page"),
    path("login/", login_html, name="login_page_alias"),
    path("app/", index_html, name="index_page"),
    path("logout/", logout_view, name="logout"),
    # 健康检查 / 运维
    path("health/", health, name="health"),
    # 业务 API
    path("api/v1/", include("food.api_urls")),
    path("api/v1/places/", include("places.urls")),
]

# ---------------------------------------------------------------------------
# 兼容原项目路由（RAGDjange 作业代码的入口全部保留，内部改指向新实现）
# ---------------------------------------------------------------------------
urlpatterns += [
    path("index/", legacy_views.index_html, name="legacy_index"),
    path("send_code/", legacy_views.send_code, name="legacy_send_code"),
    path("login_code/", legacy_views.login_code, name="legacy_login_code"),
    path("api/chat/", legacy_views.api_chat, name="legacy_api_chat"),
    path("api/email/", legacy_views.send_email_assis, name="legacy_send_email"),
]
