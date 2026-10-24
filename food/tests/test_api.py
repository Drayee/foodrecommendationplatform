"""REST API 集成测试。

覆盖：元数据/健康检查、菜谱列表与筛选、详情与浏览量、登录流程、评分/收藏/评论的鉴权与写入、
关键词与语义检索、推荐流、AI 助手（无模型时的降级路径）、偏好保存、运维接口权限。

原则：不依赖真实网络、不依赖真实大模型、不依赖 `data/index` 现成产物。
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.django_db


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def login(client, email="tester@xunwei.local"):
    """走真实验证码流程登录（演示模式下验证码会回显）。"""
    resp = post_json(client, "/api/v1/auth/send_code/", {"email": email})
    body = resp.json()
    assert body["code"] == 0, body
    code = (body.get("data") or {}).get("demo_code")
    assert code, "演示模式下应回显 demo_code"
    resp = post_json(client, "/api/v1/auth/login/", {"email": email, "code": code})
    assert resp.json()["code"] == 0, resp.json()
    return email


# ---------------------------------------------------------------------------
# 元数据 / 健康
# ---------------------------------------------------------------------------
class TestMeta:
    def test_meta_returns_dictionary(self, client, sample_recipes):
        body = client.get("/api/v1/meta/").json()
        assert body["code"] == 0
        data = body["data"]
        assert data["stats"]["recipes"] >= 4
        assert isinstance(data["cuisines"], list)
        assert "llm" in data and "index" in data
        assert len(data["difficulties"]) == 5

    def test_health_reports_subsystems(self, client):
        body = client.get("/api/v1/health/").json()
        assert body["code"] == 0
        data = body["data"]
        assert data["database"] is True
        assert "ready" in data["llm"]
        assert "ready" in data["index"]


# ---------------------------------------------------------------------------
# 菜谱列表 / 详情
# ---------------------------------------------------------------------------
class TestRecipeApi:
    def test_list_default(self, client, sample_recipes):
        body = client.get("/api/v1/recipes/").json()["data"]
        assert body["total"] == 4
        assert len(body["items"]) == 4
        card = body["items"][0]
        for key in ("id", "title", "cuisine", "total_minutes", "rating_avg", "calories"):
            assert key in card

    def test_list_filter_by_cuisine_and_sort(self, client, sample_recipes):
        body = client.get("/api/v1/recipes/", {"cuisine": "川菜"}).json()["data"]
        assert body["total"] == 1
        assert body["items"][0]["title"] == "麻婆豆腐"

        quick = client.get("/api/v1/recipes/", {"sort": "quick", "size": 2}).json()["data"]
        assert len(quick["items"]) == 2
        assert quick["items"][0]["total_minutes"] <= quick["items"][1]["total_minutes"]

    def test_list_keyword_search(self, client, sample_recipes):
        body = client.get("/api/v1/recipes/", {"q": "番茄"}).json()["data"]
        assert body["total"] >= 1
        assert any("番茄" in item["title"] for item in body["items"])

    def test_detail_and_view_increment(self, client, sample_recipes):
        recipe = sample_recipes[0]
        before = recipe.view_count
        body = client.get(f"/api/v1/recipes/{recipe.id}/").json()["data"]
        assert body["title"] == recipe.title
        assert body["ingredients"] and body["steps"]
        assert body["view_count"] == before + 1
        assert body["is_favorite"] is False
        assert body["my_rating"] is None

    def test_detail_404(self, client, sample_recipes):
        resp = client.get("/api/v1/recipes/999999/")
        assert resp.status_code == 404
        assert resp.json()["code"] == 404

    def test_similar_returns_others(self, client, sample_recipes):
        recipe = sample_recipes[0]
        body = client.get(f"/api/v1/recipes/{recipe.id}/similar/", {"k": 3}).json()["data"]
        assert isinstance(body, list)
        assert all(item["id"] != recipe.id for item in body)


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
class TestAuth:
    def test_send_code_rejects_bad_email(self, client):
        body = post_json(client, "/api/v1/auth/send_code/", {"email": "not-an-email"}).json()
        assert body["code"] == 400

    def test_login_creates_profile(self, client):
        login(client, "newbie@xunwei.local")
        body = client.get("/api/v1/auth/me/").json()["data"]
        assert body["authenticated"] is True
        assert body["email"] == "newbie@xunwei.local"
        assert body["nickname"]

    def test_wrong_code_rejected(self, client):
        post_json(client, "/api/v1/auth/send_code/", {"email": "wrong@xunwei.local"})
        body = post_json(
            client, "/api/v1/auth/login/", {"email": "wrong@xunwei.local", "code": "000000"}
        ).json()
        assert body["code"] == 400

    def test_logout(self, client):
        login(client)
        post_json(client, "/api/v1/auth/logout/", {})
        assert client.get("/api/v1/auth/me/").json()["data"]["authenticated"] is False

    def test_me_anonymous(self, client):
        assert client.get("/api/v1/auth/me/").json()["data"] == {"authenticated": False}


# ---------------------------------------------------------------------------
# 交互：评分 / 收藏 / 评论
# ---------------------------------------------------------------------------
class TestInteraction:
    def test_rate_requires_login(self, client, sample_recipes):
        resp = post_json(client, f"/api/v1/recipes/{sample_recipes[0].id}/rate/", {"score": 5})
        assert resp.status_code == 401

    def test_rate_updates_aggregate(self, client, sample_recipes):
        login(client)
        recipe = sample_recipes[0]
        body = post_json(client, f"/api/v1/recipes/{recipe.id}/rate/", {"score": 4}).json()
        assert body["code"] == 0
        assert body["data"]["rating_count"] == 1
        assert body["data"]["rating_avg"] == 4.0

        # 重复评分应更新而不是新增
        body = post_json(client, f"/api/v1/recipes/{recipe.id}/rate/", {"score": 2}).json()
        assert body["data"]["rating_count"] == 1
        assert body["data"]["rating_avg"] == 2.0

    def test_rate_out_of_range(self, client, sample_recipes):
        login(client)
        body = post_json(client, f"/api/v1/recipes/{sample_recipes[0].id}/rate/", {"score": 9}).json()
        assert body["code"] == 1

    def test_favorite_toggle(self, client, sample_recipes):
        login(client)
        recipe = sample_recipes[1]
        first = post_json(client, f"/api/v1/recipes/{recipe.id}/favorite/", {}).json()["data"]
        assert first["is_favorite"] is True and first["favorite_count"] == 1
        second = post_json(client, f"/api/v1/recipes/{recipe.id}/favorite/", {}).json()["data"]
        assert second["is_favorite"] is False and second["favorite_count"] == 0

    def test_comment_create_and_list(self, client, sample_recipes):
        login(client)
        recipe = sample_recipes[2]
        created = post_json(
            client, f"/api/v1/recipes/{recipe.id}/comments/", {"content": "汤很浓，牛腩够烂"}
        ).json()
        assert created["code"] == 0
        items = client.get(f"/api/v1/recipes/{recipe.id}/comments/").json()["data"]
        assert items and items[0]["content"] == "汤很浓，牛腩够烂"

    def test_comment_requires_login(self, client, sample_recipes):
        resp = post_json(client, f"/api/v1/recipes/{sample_recipes[0].id}/comments/", {"content": "x"})
        assert resp.status_code == 401

    def test_empty_comment_rejected(self, client, sample_recipes):
        login(client)
        body = post_json(client, f"/api/v1/recipes/{sample_recipes[0].id}/comments/", {"content": "   "}).json()
        assert body["code"] == 1


# ---------------------------------------------------------------------------
# 检索 / 推荐
# ---------------------------------------------------------------------------
class TestSearchFeed:
    def test_keyword_search_records_log(self, client, sample_recipes):
        from food.models import SearchLog

        body = client.get("/api/v1/search/", {"q": "豆腐"}).json()["data"]
        assert body["total"] >= 1
        assert body["mode"] == "keyword"
        assert SearchLog.objects.filter(query="豆腐", mode="keyword").exists()

    def test_search_empty_query(self, client):
        assert client.get("/api/v1/search/").json()["code"] == 1

    def test_semantic_search_degrades_without_index(self, client, sample_recipes, settings, tmp_path):
        settings.RAG = {**settings.RAG, "INDEX_DIR": str(tmp_path / "noidx")}
        body = client.get("/api/v1/search/semantic/", {"q": "番茄怎么做"}).json()
        assert body["code"] == 0
        assert body["data"]["total"] == 0
        assert "index" in body["data"]

    def test_feed_guest_returns_items(self, client, sample_recipes):
        body = client.get("/api/v1/feed/", {"k": 3}).json()["data"]
        assert len(body["items"]) == 3
        assert body["mode"] in {"cold_start", "fallback_hot", "personalized"}
        assert all(item.get("id") for item in body["items"])

    def test_feed_after_behavior(self, client, sample_recipes):
        login(client)
        recipe = sample_recipes[0]
        post_json(client, f"/api/v1/recipes/{recipe.id}/favorite/", {})
        body = client.get("/api/v1/feed/", {"k": 4}).json()["data"]
        assert len(body["items"]) >= 3
        assert body["mode"] == "personalized"


# ---------------------------------------------------------------------------
# AI 助手（无 Key 降级）
# ---------------------------------------------------------------------------
class TestAssistant:
    @pytest.fixture(autouse=True)
    def _no_llm(self, monkeypatch):
        """整类测试都关掉 LLM：本机可能接着 Ollama，真实推理既慢又不可断言。"""
        from food.services import assistant

        monkeypatch.setattr(assistant, "llm_ready", lambda: False)

    def test_sync_answer_without_llm(self, client, sample_recipes):
        body = post_json(
            client, "/api/v1/assistant/chat/sync/", {"question": "番茄牛腩怎么做？"}
        ).json()
        assert body["code"] == 0
        data = body["data"]
        assert data["degraded"] is True
        assert data["answer"]
        assert data["latency_ms"] >= 0

    def test_sync_answer_empty_question(self, client):
        assert post_json(client, "/api/v1/assistant/chat/sync/", {"question": " "}).json()["code"] == 1

    def test_history_persisted(self, client, sample_recipes):
        session = "pytest-session-1"
        post_json(
            client,
            "/api/v1/assistant/chat/sync/",
            {"question": "推荐一道快手菜", "session_id": session},
        )
        body = client.get("/api/v1/assistant/history/", {"session_id": session}).json()["data"]
        assert len(body["messages"]) == 2
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][1]["role"] == "assistant"

    def test_debug_retrieval_shape(self, client, sample_recipes):
        body = client.get("/api/v1/assistant/debug/", {"q": "麻婆豆腐"}).json()["data"]
        assert body["question"] == "麻婆豆腐"
        assert isinstance(body["hits"], list)


# ---------------------------------------------------------------------------
# 用户中心
# ---------------------------------------------------------------------------
class TestProfile:
    def test_profile_requires_login(self, client):
        assert client.get("/api/v1/profile/").status_code == 401

    def test_save_preferences(self, client, sample_recipes):
        login(client)
        body = post_json(
            client,
            "/api/v1/profile/",
            {
                "spicy_level": 4,
                "max_cook_minutes": 45,
                "diet_flags": ["low_cal"],
                "disliked_ingredients": ["香菜", "花生"],
                "preferred_cuisines": [sample_recipes[0].cuisine_id],
            },
        ).json()
        assert body["code"] == 0
        data = body["data"]
        assert data["spicy_level"] == 4
        assert data["max_cook_minutes"] == 45
        assert data["diet_flags"] == ["low_cal"]
        assert data["disliked_ingredients"] == ["香菜", "花生"]
        assert len(data["preferred_cuisines"]) == 1

    def test_favorites_list(self, client, sample_recipes):
        login(client)
        recipe = sample_recipes[0]
        post_json(client, f"/api/v1/recipes/{recipe.id}/favorite/", {})
        items = client.get("/api/v1/favorites/").json()["data"]
        assert len(items) == 1 and items[0]["id"] == recipe.id

    def test_track_interaction(self, client, sample_recipes):
        body = post_json(
            client,
            "/api/v1/interactions/",
            {"action": "click", "recipe_id": sample_recipes[0].id, "weight": 1.5},
        ).json()
        assert body["code"] == 0
        bad = post_json(client, "/api/v1/interactions/", {"action": "hack"}).json()
        assert bad["code"] == 1


# ---------------------------------------------------------------------------
# 运维接口
# ---------------------------------------------------------------------------
class TestOps:
    def test_rebuild_index_requires_staff(self, client, sample_recipes):
        resp = post_json(client, "/api/v1/admin/rebuild_index/", {})
        assert resp.status_code == 403

    def test_rebuild_index_as_staff(self, client, sample_recipes, settings, tmp_path):
        from django.contrib.auth.models import User

        settings.RAG = {**settings.RAG, "INDEX_DIR": str(tmp_path / "idx")}
        user = User.objects.create_user("staff", password="x", is_staff=True)
        client.force_login(user)
        body = post_json(client, "/api/v1/admin/rebuild_index/", {}).json()
        assert body["code"] == 0
        assert body["data"]["chunks"] > 0
        assert body["data"]["recipes"] >= 4


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------
class TestPages:
    def test_login_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "寻味" in resp.content.decode("utf-8")

    def test_app_page_renders_boot(self, client, sample_recipes):
        resp = client.get("/app/")
        assert resp.status_code == 200
        html = resp.content.decode("utf-8")
        assert "window.__BOOT__" in html
        assert "寻味" in html
