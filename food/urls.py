"""页面路由（HTML 页面，数据全部走 /api/v1）。"""
from django.urls import path

from food import views

app_name = "food"

urlpatterns = [
    path("", views.home, name="home"),
    path("recipe/<int:pk>/", views.recipe_detail_page, name="recipe_detail"),
    path("search/", views.search_page, name="search"),
    path("assistant/", views.assistant_page, name="assistant"),
    path("profile/", views.profile_page, name="profile"),
]
