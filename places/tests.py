"""places 模块测试。

覆盖：坐标转换、Key 池调度与冷却、POI 归一化、缓存与离线降级、排序策略、REST 接口。
所有高德调用都被 mock，测试全程不联网。
"""
from __future__ import annotations

import json
import math
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from places import coord, keypool, service
from places.amap import AmapError
from places.models import PlaceCache, PlaceFavorite, PlaceInteraction, PlaceReview

pytestmark = pytest.mark.django_db

YAAN = (103.0012, 29.9875)  # GCJ-02


# ---------------------------------------------------------------------------
# 坐标
# ---------------------------------------------------------------------------
class TestCoord:
    def test_wgs84_to_gcj02_shifts_inside_china(self):
        lng, lat = coord.wgs84_to_gcj02(103.0, 29.98)
        assert (lng, lat) != (103.0, 29.98)
        # 中国境内偏移量级：数百米以内
        shift = coord.haversine_m(103.0, 29.98, lng, lat)
        assert 50 < shift < 800

    def test_outside_china_untouched(self):
        assert coord.wgs84_to_gcj02(2.3522, 48.8566) == (2.3522, 48.8566)

    def test_roundtrip_is_close(self):
        wlng, wlat = coord.gcj02_to_wgs84(103.0012, 29.9875)
        back = coord.wgs84_to_gcj02(wlng, wlat)
        assert coord.haversine_m(back[0], back[1], 103.0012, 29.9875) < 5

    def test_haversine_known_distance(self):
        # 天安门 → 故宫约 1km 量级
        d = coord.haversine_m(116.397428, 39.90923, 116.397128, 39.916527)
        assert 700 < d < 900

    def test_format_distance(self):
        assert coord.format_distance(320) == "320m"
        assert coord.format_distance(1500) == "1.5km"

    def test_grid_key_precision(self):
        key = coord.grid_key(103.0012, 29.9875, 3)
        assert key.startswith("103.001:")
        assert len(key.split(":")[1].split(".")[1]) == 3


# ---------------------------------------------------------------------------
# Key 池
# ---------------------------------------------------------------------------
class TestKeyPool:
    def test_round_robin_spreads_load(self):
        pool = keypool.KeyPool(["k1", "k2", "k3"])
        picked = [pool.acquire().label for _ in range(6)]
        assert set(picked) == {"amap#1", "amap#2", "amap#3"}
        assert picked.count("amap#1") == 2

    def test_quota_error_cools_key(self):
        pool = keypool.KeyPool(["k1", "k2"], cooldown_quota=3600)
        state = pool.acquire()
        pool.report_failure(state, "10003", "日配额超限")
        assert state.cooling
        assert state.cooldown_remaining > 3000
        # 冷却中的不会被再取出
        for _ in range(5):
            assert pool.acquire().key != state.key

    def test_qps_error_short_cooldown(self):
        pool = keypool.KeyPool(["k1"])
        state = pool.acquire()
        duration = pool.report_failure(state, "10021", "QPS 超限")
        assert duration == keypool.COOLDOWN_QPS

    def test_all_cooling_returns_none(self):
        pool = keypool.KeyPool(["k1"])
        state = pool.acquire()
        pool.report_failure(state, "10029", "日调用量超限")
        assert pool.acquire() is None

    def test_consecutive_failures_trigger_cooldown(self):
        pool = keypool.KeyPool(["k1"])
        state = pool.acquire()
        for _ in range(keypool.FAILURES_BEFORE_COOLDOWN):
            pool.report_failure(state, "99999", "unknown")
        assert state.cooling

    def test_mask_key_hides_middle(self):
        raw = "demo1234abcd5678efgh9012"
        masked = keypool.mask_key(raw)
        assert masked.startswith("demo12")
        assert masked.endswith("9012")
        assert "abcd5678" not in masked

    def test_stats_and_reset(self):
        pool = keypool.KeyPool(["k1", "k2"])
        pool.report_failure(pool.acquire(), "10003", "quota")
        stats = pool.stats()
        assert stats["total"] == 2
        assert stats["cooling"] == 1
        pool.reset()
        assert pool.stats()["cooling"] == 0

    def test_empty_pool(self):
        pool = keypool.KeyPool([])
        assert pool.has_keys is False
        assert pool.acquire() is None


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
class TestNormalize:
    RAW = {
        "id": "B0FFTEST",
        "name": "老码头火锅",
        "type": "餐饮服务;中餐厅;火锅店",
        "typecode": "050117",
        "address": "西大街 88 号",
        "location": "103.002410,29.988320",
        "tel": "0835-2222688",
        "distance": "180",
        "biz_ext": {"rating": "4.6", "cost": "72.00", "open_time": "11:00-23:00"},
        "photos": [{"url": "https://example.com/1.jpg"}],
        "pname": "四川省",
        "cityname": "雅安市",
        "adname": "雨城区",
    }

    def test_full_poi(self):
        place = service.normalize_poi(self.RAW)
        assert place is not None
        assert place.poi_id == "B0FFTEST"
        assert place.type_label == "火锅店"
        assert place.rating == 4.6
        assert place.cost == 72.0
        assert place.distance_m == 180
        assert place.photos == ["https://example.com/1.jpg"]
        assert place.city == "雅安市"

    def test_missing_location_dropped(self):
        raw = dict(self.RAW)
        raw.pop("location")
        assert service.normalize_poi(raw) is None

    def test_bad_location_dropped(self):
        raw = dict(self.RAW, location="not-a-coord")
        assert service.normalize_poi(raw) is None

    def test_distance_computed_from_origin(self):
        raw = dict(self.RAW)
        raw.pop("distance")
        place = service.normalize_poi(raw, origin=YAAN)
        assert 0 < place.distance_m < 1000

    def test_missing_biz_ext_defaults(self):
        raw = dict(self.RAW, biz_ext={})
        place = service.normalize_poi(raw)
        assert place.rating == 0 and place.cost == 0

    def test_type_label_falls_back(self):
        raw = dict(self.RAW, type="餐饮服务")
        assert service.normalize_poi(raw).type_label == "餐饮服务"

    def test_to_dict_has_distance_text(self):
        data = service.normalize_poi(self.RAW).to_dict()
        assert data["distance_text"] == "180m"
        assert "score" in data


# ---------------------------------------------------------------------------
# 服务层：缓存 / 降级 / 排序
# ---------------------------------------------------------------------------
class FakeClient:
    """高德客户端替身：记录调用参数，便于断言「传给高德的是什么」。"""

    def __init__(self, pois=None, error: AmapError | None = None):
        self.pois = pois or []
        self.error = error
        self.calls = 0
        self.positions: list[tuple] = []
        self.last_kwargs: dict = {}

    def around(self, *args, **kwargs):
        self.calls += 1
        self.positions.append(tuple(args[:2]))
        self.last_kwargs = dict(kwargs)
        if self.error:
            raise self.error
        return {"pois": self.pois, "_key_label": "amap#1", "_latency_ms": 12.0}


def _poi(pid: str, name: str, lng: float, lat: float, rating: str = "4.5", cost: str = "50", distance: str = ""):
    return {
        "id": pid,
        "name": name,
        "type": "餐饮服务;中餐厅;川菜",
        "typecode": "050111",
        "address": "测试路 1 号",
        "location": f"{lng},{lat}",
        "distance": distance,
        "biz_ext": {"rating": rating, "cost": cost, "open_time": "10:00-21:00"},
        "cityname": "雅安市",
        "adname": "雨城区",
    }


class TestService:
    def test_nearby_uses_client_and_caches(self, monkeypatch):
        pois = [_poi("P1", "近店", 103.0013, 29.9876), _poi("P2", "远店", 103.0045, 29.9876)]
        fake = FakeClient(pois)
        monkeypatch.setattr(service, "get_client", lambda: fake)

        first = service.nearby(*YAAN, radius=3000, use_cache=True)
        assert first["provider"] == "amap"
        assert first["degraded"] is False
        assert first["cached"] is False
        assert len(first["items"]) == 2
        assert fake.calls == 1
        assert PlaceCache.objects.count() == 1

        second = service.nearby(*YAAN, radius=3000, use_cache=True)
        assert second["cached"] is True
        assert fake.calls == 1  # 命中缓存，未再请求

    def test_nearby_degrades_on_quota_error(self, monkeypatch):
        fake = FakeClient(error=AmapError("日配额超限", "10029", retryable=True))
        monkeypatch.setattr(service, "get_client", lambda: fake)

        result = service.nearby(*YAAN, radius=5000)
        assert result["degraded"] is True
        assert result["provider"] == "fixture"
        assert result["items"], "降级仍应返回 fixture 样例"
        assert all(item["source"] == "fixture" for item in result["items"])
        assert "10029" in result["message"]

    def test_nearby_degrades_without_key(self, monkeypatch):
        monkeypatch.setattr(
            service, "get_client", lambda: FakeClient(error=AmapError("未配置 Key", "NO_KEY"))
        )
        result = service.nearby(*YAAN, radius=3000)
        assert result["degraded"] is True
        assert PlaceCache.objects.filter(degraded=True).exists()

    def test_sort_distance(self):
        items = [
            service.normalize_poi(_poi("A", "远", 103.02, 29.99, distance="2000"), YAAN),
            service.normalize_poi(_poi("B", "近", 103.0013, 29.9876, distance="100"), YAAN),
        ]
        ranked = service.rank_places(items, radius=3000, sort="distance")
        assert [p.name for p in ranked] == ["近", "远"]

    def test_sort_rating(self):
        items = [
            service.normalize_poi(_poi("A", "低分", 103.0013, 29.9876, rating="3.5"), YAAN),
            service.normalize_poi(_poi("B", "高分", 103.0013, 29.9876, rating="4.9"), YAAN),
        ]
        ranked = service.rank_places(items, radius=3000, sort="rating")
        assert [p.name for p in ranked] == ["高分", "低分"]

    def test_smart_sort_prefers_near_and_good(self):
        near_good = service.normalize_poi(_poi("A", "近且好", 103.0013, 29.9876, rating="4.8", cost="50"), YAAN)
        far_bad = service.normalize_poi(_poi("B", "远且差", 103.03, 30.0, rating="3.0", cost="300"), YAAN)
        ranked = service.rank_places([far_bad, near_good], radius=3000, sort="smart", budget=60)
        assert ranked[0].name == "近且好"
        assert ranked[0].score > ranked[-1].score

    def test_heat_boosts_popular_poi(self):
        a = service.normalize_poi(_poi("HOT", "热门店", 103.0013, 29.9876, rating="4.0"), YAAN)
        b = service.normalize_poi(_poi("COLD", "冷门店", 103.0013, 29.9876, rating="4.0"), YAAN)
        for _ in range(5):
            PlaceInteraction.objects.create(poi_id="HOT", place_name="热门店", action="view")
        ranked = service.rank_places([b, a], radius=3000, sort="smart")
        assert ranked[0].poi_id == "HOT"

    def test_cache_expiry_refetches(self, monkeypatch):
        fake = FakeClient([_poi("P1", "近店", 103.0013, 29.9876)])
        monkeypatch.setattr(service, "get_client", lambda: fake)
        service.nearby(*YAAN, radius=3000)
        row = PlaceCache.objects.first()
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])

        service.nearby(*YAAN, radius=3000)
        assert fake.calls == 2

    def test_regeo_degrades(self, monkeypatch):
        class BadClient:
            def regeo(self, *a, **k):
                raise AmapError("配额", "10029")

        monkeypatch.setattr(service, "get_client", lambda: BadClient())
        result = service.regeo(*YAAN)
        assert result["degraded"] is True
        assert result["address"]["city"] == ""

    def test_tips_degrades(self, monkeypatch):
        class BadClient:
            def inputtips(self, *a, **k):
                raise AmapError("x", "NO_KEY")

        monkeypatch.setattr(service, "get_client", lambda: BadClient())
        result = service.tips("火锅")
        assert result["degraded"] is True
        assert result["items"] == []


# ---------------------------------------------------------------------------
# REST 接口
# ---------------------------------------------------------------------------
class TestApi:
    def test_config_exposes_js_key_and_lists(self, client, settings):
        settings.AMAP = {
            **settings.AMAP,
            "JS_KEY": "js-test-key",
            "JS_KEYS": ["js-test-key"],
            "JS_PAIRS": [],
            "SECURITY_CODE": "js-test-code",
        }
        keypool.reset_pools()
        body = client.get("/api/v1/places/config/").json()
        assert body["code"] == 0
        data = body["data"]
        assert data["js_key"] == "js-test-key"
        assert data["security_code"] == "js-test-code"
        assert data["default_radius"] > 0
        assert len(data["sorts"]) == 5
        keypool.reset_pools()

    def test_config_returns_js_pairs_with_security_code(self, client, settings):
        pairs = [
            {"key": "k-alpha", "security_code": "code-alpha"},
            {"key": "k-beta", "security_code": "code-beta"},
        ]
        settings.AMAP = {**settings.AMAP, "JS_PAIRS": pairs, "JS_KEYS": ["k-alpha", "k-beta"]}
        keypool.reset_pools()
        data = client.get("/api/v1/places/config/").json()["data"]
        assert data["js_pairs"] == pairs
        assert data["js_key"] == "k-alpha"
        assert data["security_code"] == "code-alpha"
        assert data["key_pool"]["js_total"] == 2
        keypool.reset_pools()

    def test_nearby_requires_coordinates(self, client):
        assert client.get("/api/v1/places/nearby/").json()["code"] == 1

    def test_nearby_returns_items(self, client, monkeypatch):
        fake = FakeClient([_poi("P1", "近店", 103.0013, 29.9876)])
        monkeypatch.setattr(service, "get_client", lambda: fake)
        body = client.get("/api/v1/places/nearby/", {"lng": 103.0, "lat": 29.98, "radius": 3000}).json()
        assert body["code"] == 0
        data = body["data"]
        assert data["items"] and data["items"][0]["name"] == "近店"
        assert "center" in data and "center_wgs84" in data
        assert data["key_pool"]["total"] >= 1

    def test_nearby_rejects_bad_coordinates(self, client):
        assert client.get("/api/v1/places/nearby/", {"lng": 999, "lat": 29}).json()["code"] == 1

    def test_favorite_requires_login(self, client):
        resp = client.post("/api/v1/places/P1/favorite/", data=json.dumps({"place": {"name": "店"}}),
                           content_type="application/json")
        assert resp.status_code == 401

    def test_favorite_toggle_with_snapshot(self, client):
        user = User.objects.create_user("foodie", password="x")
        client.force_login(user)
        payload = {"place": {"name": "老码头火锅", "address": "西大街", "lng": 103.0, "lat": 29.98,
                             "rating": 4.6, "cost": 72, "typecode": "050117", "type_label": "火锅店"}}
        first = client.post("/api/v1/places/P1/favorite/", data=json.dumps(payload),
                            content_type="application/json").json()
        assert first["data"]["is_favorite"] is True
        assert PlaceFavorite.objects.filter(user=user, poi_id="P1").exists()

        second = client.post("/api/v1/places/P1/favorite/", data=json.dumps(payload),
                             content_type="application/json").json()
        assert second["data"]["is_favorite"] is False
        assert not PlaceFavorite.objects.filter(user=user, poi_id="P1").exists()

    def test_my_favorites(self, client):
        user = User.objects.create_user("fan", password="x")
        PlaceFavorite.objects.create(user=user, poi_id="P9", name="好吃店")
        client.force_login(user)
        body = client.get("/api/v1/places/favorites/").json()
        assert body["code"] == 0 and len(body["data"]) == 1

    def test_my_reviews_lists_own(self, client):
        user = User.objects.create_user("checker", password="x")
        client.force_login(user)
        PlaceReview.objects.create(user=user, poi_id="P7", place_name="砂锅店", rating=5, content="很香")
        body = client.get("/api/v1/places/reviews/mine/").json()
        assert body["code"] == 0
        assert len(body["data"]) == 1
        assert body["data"][0]["place_name"] == "砂锅店"

    def test_my_reviews_requires_login(self, client):
        assert client.get("/api/v1/places/reviews/mine/").status_code == 401

    def test_review_post_and_get(self, client):
        user = User.objects.create_user("reviewer", password="x")
        client.force_login(user)
        created = client.post(
            "/api/v1/places/P2/reviews/",
            data=json.dumps({"rating": 5, "content": "砂锅很香", "place_name": "砂锅店", "cost": 45,
                             "dishes": ["砂锅酥肉"]}),
            content_type="application/json",
        ).json()
        assert created["code"] == 0
        assert PlaceReview.objects.filter(poi_id="P2").count() == 1

        body = client.get("/api/v1/places/P2/reviews/").json()["data"]
        assert body["count"] == 1
        assert body["avg_rating"] == 5.0
        assert body["items"][0]["content"] == "砂锅很香"

    def test_review_rating_validated(self, client):
        user = User.objects.create_user("bad", password="x")
        client.force_login(user)
        body = client.post("/api/v1/places/P3/reviews/", data=json.dumps({"rating": 9}),
                           content_type="application/json").json()
        assert body["code"] == 1

    def test_review_requires_login(self, client):
        resp = client.post("/api/v1/places/P3/reviews/", data=json.dumps({"rating": 5}),
                           content_type="application/json")
        assert resp.status_code == 401

    def test_key_status_masks_for_anonymous(self, client, settings):
        settings.AMAP = {**settings.AMAP, "WEB_KEYS": ["abcdef1234567890"], "WEB_KEY": "abcdef1234567890"}
        keypool.reset_pools()
        body = client.get("/api/v1/places/keys/").json()["data"]
        assert body["web"]["total"] == 1
        assert "keys" not in body["web"]
        keypool.reset_pools()

    def test_key_reset_requires_staff(self, client):
        resp = client.post("/api/v1/places/keys/reset/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 坐标：边界纬度与往返精度
# ---------------------------------------------------------------------------
class TestCoordBoundary:
    @pytest.mark.parametrize("lat", [0.8293, 0.83, 0.9, 55.8, 55.8271])
    def test_wgs84_to_gcj02_survives_boundary_latitudes(self, lat):
        """境内框的南北边界上必须仍然能算（cos/sqrt 不炸）且真的偏移。"""
        lng, out_lat = coord.wgs84_to_gcj02(103.0, lat)
        assert math.isfinite(lng) and math.isfinite(out_lat)
        assert -180 <= lng <= 180 and -90 <= out_lat <= 90
        assert (lng, out_lat) != (103.0, lat), "境内边界纬度不应退化成恒等变换"

    def test_boundary_outside_china_is_identity(self):
        assert coord.out_of_china(103.0, 55.9) is True
        assert coord.out_of_china(103.0, 55.8) is False
        assert coord.wgs84_to_gcj02(103.0, 55.9) == (103.0, 55.9)

    @pytest.mark.parametrize(
        "point",
        [
            (103.0012, 29.9875),      # 雅安
            (116.397428, 39.90923),   # 北京
            (120.0, 55.8),            # 北边界附近
            (113.264385, 23.129112),  # 广州
            (87.6, 43.8),             # 乌鲁木齐
        ],
    )
    def test_gcj02_to_wgs84_roundtrip_within_10m(self, point):
        wlng, wlat = coord.gcj02_to_wgs84(*point)
        back = coord.wgs84_to_gcj02(wlng, wlat)
        assert coord.haversine_m(back[0], back[1], point[0], point[1]) < 10

    def test_southern_boundary_roundtrip_bug_is_pinned(self):
        """已知缺陷（钉住现状）：紧贴南边界（lat ≲ 0.8347）时逆解落到境内框外，
        回程 `wgs84_to_gcj02` 走境外恒等分支，往返误差约 150m。

        这里断言「当前就错这么多」，一旦被修好本用例会失败并提示更新。
        """
        point = (103.0, 0.83)
        back = coord.wgs84_to_gcj02(*coord.gcj02_to_wgs84(*point))
        assert 100 < coord.haversine_m(back[0], back[1], point[0], point[1]) < 300


# ---------------------------------------------------------------------------
# 高德错误码分类（amap_check / Key 池共用逻辑）
# ---------------------------------------------------------------------------
class TestAmapErrorClassification:
    @pytest.mark.parametrize("infocode", sorted(keypool.QUOTA_INFOCODES))
    def test_quota_infocodes_flagged(self, infocode):
        assert AmapError("配额", infocode).is_quota_error is True
        assert AmapError("配额", infocode).is_no_key is False

    def test_qps_and_no_key_codes(self):
        assert AmapError("QPS 超限", "10021").is_quota_error is False
        assert AmapError("未配置 Key", "NO_KEY").is_no_key is True
        assert AmapError("服务已禁用", "DISABLED").is_no_key is True
        assert AmapError("网络失败", "NETWORK").is_no_key is False

    def test_keypool_codes_agree_with_error_classification(self):
        """Key 池的冷却分类必须与 AmapError 的判断保持一致，否则会长期误伤 Key。"""
        quota_by_pool = keypool.QUOTA_INFOCODES
        quota_by_error = {c for c in quota_by_pool | keypool.QPS_INFOCODES if AmapError("x", c).is_quota_error}
        assert quota_by_error == set(quota_by_pool)
        assert not any(AmapError("x", c).is_quota_error for c in keypool.QPS_INFOCODES)

    def test_amap_check_hints_cover_common_quota_codes(self):
        """`manage.py amap_check` 的关键错误码必须有中文释义：运维靠它判断「换 Key」还是「等次日」。

        注：10019 / 10020 / 10030 目前没有释义（已在测试报告里作为缺陷记录），
        这里只钉住最常撞到的日配额与 QPS 码，避免测试把「缺释义」当成合法状态固化。
        """
        from places.management.commands.amap_check import INFOCODE_HINTS

        assert {"10003", "10029", "10021"} <= set(INFOCODE_HINTS)
        assert all(isinstance(v, str) and v.strip() for v in INFOCODE_HINTS.values())
        # 释义表里出现的高德码必须能被池子的分类逻辑识别（否则会漏冷却）
        unknown = set(INFOCODE_HINTS) - keypool.QUOTA_INFOCODES - keypool.QPS_INFOCODES
        assert unknown <= {"10000", "10001", "10005", "10006", "10007", "10008", "10012"}


# ---------------------------------------------------------------------------
# 接口：附近搜索的缓存与入参钳制
# ---------------------------------------------------------------------------
class TestNearbyParams:
    PARAMS = {"lng": 103.0012, "lat": 29.9875, "coord": "gcj02"}

    def _fake(self, monkeypatch, pois=None):
        fake = FakeClient(pois if pois is not None else [_poi("P1", "近店", 103.0013, 29.9876)])
        monkeypatch.setattr(service, "get_client", lambda: fake)
        return fake

    def test_second_request_hits_cache(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        params = {**self.PARAMS, "radius": 3000}
        first = client.get("/api/v1/places/nearby/", params).json()["data"]
        second = client.get("/api/v1/places/nearby/", params).json()["data"]
        assert first["cached"] is False
        assert second["cached"] is True
        assert fake.calls == 1, "命中缓存时不应再请求高德"
        assert PlaceCache.objects.count() == 1
        assert PlaceCache.objects.first().hit_count >= 1
        assert [i["name"] for i in second["items"]] == [i["name"] for i in first["items"]]

    def test_expired_cache_refetches(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        params = {**self.PARAMS, "radius": 3000}
        assert client.get("/api/v1/places/nearby/", params).json()["data"]["cached"] is False
        PlaceCache.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        data = client.get("/api/v1/places/nearby/", params).json()["data"]
        assert data["cached"] is False
        assert fake.calls == 2

    def test_radius_clamped_to_max(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        data = client.get("/api/v1/places/nearby/", {**self.PARAMS, "radius": 999999}).json()["data"]
        assert data["radius"] == 50000
        assert fake.last_kwargs["radius"] == 50000, "钳制必须发生在请求高德之前"

    def test_radius_floor_and_default(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        low = client.get("/api/v1/places/nearby/", {**self.PARAMS, "radius": 10}).json()["data"]
        assert low["radius"] == 100
        default = client.get("/api/v1/places/nearby/", dict(self.PARAMS)).json()["data"]
        assert default["radius"] == 3000

    def test_page_clamped_to_bounds(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        high = client.get("/api/v1/places/nearby/", {**self.PARAMS, "page": 900}).json()["data"]
        assert high["page"] == 100
        assert fake.last_kwargs["page"] == 100
        zero = client.get("/api/v1/places/nearby/", {**self.PARAMS, "page": 0}).json()["data"]
        assert zero["page"] == 1

    def test_coord_gcj02_skips_conversion(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        data = client.get("/api/v1/places/nearby/", self.PARAMS).json()["data"]
        assert fake.positions[-1] == pytest.approx((103.0012, 29.9875))
        assert data["center"] == pytest.approx({"lng": 103.0012, "lat": 29.9875})

    def test_coord_defaults_to_wgs84_and_converts(self, client, monkeypatch):
        fake = self._fake(monkeypatch)
        data = client.get("/api/v1/places/nearby/", {"lng": 103.0, "lat": 29.98}).json()["data"]
        sent = fake.positions[-1]
        assert sent != pytest.approx((103.0, 29.98))
        assert 50 < coord.haversine_m(sent[0], sent[1], 103.0, 29.98) < 800
        assert data["center"] == pytest.approx({"lng": sent[0], "lat": sent[1]})


# ---------------------------------------------------------------------------
# 接口：输入提示
# ---------------------------------------------------------------------------
class FakeTipsClient:
    def __init__(self, tips):
        self.tips = tips
        self.calls: list[tuple] = []

    def inputtips(self, keywords, city="", lng=None, lat=None):
        self.calls.append((keywords, city, lng, lat))
        return {"tips": self.tips}


class TestTipsApi:
    def test_tips_parses_dedups_and_forwards_params(self, client, monkeypatch):
        fake = FakeTipsClient(
            [
                {"name": "老码头火锅", "district": "雨城区", "address": "西大街 88 号",
                 "location": "103.002410,29.988320", "typecode": "050117"},
                {"name": "老码头火锅", "district": "雨城区"},   # 重名去重
                {"name": "   "},                              # 空名丢弃
                {"name": "砂锅居"},
                "bad-item",                                   # 非 dict 忽略
            ]
        )
        monkeypatch.setattr(service, "get_client", lambda: fake)
        body = client.get(
            "/api/v1/places/tips/", {"q": "火锅", "city": "雅安", "lng": 103.0012, "lat": 29.9875}
        ).json()
        assert body["code"] == 0
        data = body["data"]
        assert data["degraded"] is False
        assert [i["name"] for i in data["items"]] == ["老码头火锅", "砂锅居"]
        assert data["items"][0]["typecode"] == "050117"
        assert data["items"][1]["district"] == ""
        assert fake.calls == [("火锅", "雅安", 103.0012, 29.9875)]

    def test_tips_requires_keyword(self, client):
        for params in ({}, {"q": "   "}, {"keywords": ""}):
            body = client.get("/api/v1/places/tips/", params).json()
            assert body["code"] == 1
            assert "关键词" in body["msg"]


# ---------------------------------------------------------------------------
# 接口：详情页评分分布
# ---------------------------------------------------------------------------
class FakeDetailClient:
    def __init__(self, poi: dict | None):
        self.poi = poi
        self.calls = 0

    def detail(self, poi_id: str):
        self.calls += 1
        if not self.poi:
            return {"pois": []}
        return {"pois": [dict(self.poi, id=poi_id)]}


class TestDetailApi:
    def test_review_distribution_counts_each_star(self, client, monkeypatch):
        monkeypatch.setattr(
            service, "get_client",
            lambda: FakeDetailClient(_poi("P-D", "砂锅居", 103.0013, 29.9876, rating="4.2", cost="45")),
        )
        for idx, rating in enumerate((5, 4, 4)):
            user = User.objects.create_user(f"diner{idx}", password="x")
            PlaceReview.objects.create(user=user, poi_id="P-D", place_name="砂锅居", rating=rating, content="好吃")

        body = client.get("/api/v1/places/P-D/").json()
        assert body["code"] == 0
        data = body["data"]
        assert data["place"]["name"] == "砂锅居"
        stats = data["review_stats"]
        assert stats["count"] == 3
        assert stats["avg_rating"] == 4.33
        assert stats["distribution"] == {"1": 0, "2": 0, "3": 0, "4": 2, "5": 1}
        assert len(data["reviews"]) == 3
        assert stats["favorited"] == 0

    def test_hidden_reviews_excluded_and_view_tracked(self, client, monkeypatch):
        monkeypatch.setattr(
            service, "get_client",
            lambda: FakeDetailClient(_poi("P-D2", "面馆", 103.0013, 29.9876)),
        )
        user = User.objects.create_user("picker", password="x")
        PlaceReview.objects.create(user=user, poi_id="P-D2", place_name="面馆", rating=5, is_hidden=True)
        PlaceReview.objects.create(user=user, poi_id="P-D2", place_name="面馆", rating=3)
        PlaceFavorite.objects.create(user=user, poi_id="P-D2", name="面馆")
        client.force_login(user)

        data = client.get("/api/v1/places/P-D2/").json()["data"]
        stats = data["review_stats"]
        assert stats["count"] == 1
        assert stats["distribution"] == {"1": 0, "2": 0, "3": 1, "4": 0, "5": 0}
        assert stats["avg_rating"] == 3.0
        assert stats["favorited"] == 1
        assert data["is_favorite"] is True
        assert PlaceInteraction.objects.filter(poi_id="P-D2", action="view").exists()


# ---------------------------------------------------------------------------
# 接口：打卡 / 埋点
# ---------------------------------------------------------------------------
class TestCheckinApi:
    def test_checkin_requires_login(self, client):
        resp = client.post(
            "/api/v1/places/P-CK/checkin/",
            data=json.dumps({"place_name": "砂锅居"}),
            content_type="application/json",
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == 401
        assert PlaceReview.objects.count() == 0

    def test_checkin_writes_review_and_track(self, client):
        user = User.objects.create_user("checker", password="x")
        client.force_login(user)
        body = client.post(
            "/api/v1/places/P-CK/checkin/",
            data=json.dumps({"place_name": "砂锅居", "rating": 4, "cost": 45}),
            content_type="application/json",
        ).json()
        assert body["code"] == 0
        assert body["data"] == {"checked_in": True}

        review = PlaceReview.objects.get(user=user, poi_id="P-CK")
        assert review.place_name == "砂锅居"
        assert review.rating == 4
        assert review.content == ""      # 打卡不写评价正文
        assert review.cost == 45

        track = PlaceInteraction.objects.get(poi_id="P-CK", action="checkin")
        assert track.weight == 2.5
        assert track.place_name == "砂锅居"
        assert track.user_id == user.id

    def test_checkin_defaults_when_body_empty(self, client):
        user = User.objects.create_user("checker2", password="x")
        client.force_login(user)
        body = client.post(
            "/api/v1/places/P-CK2/checkin/", data=json.dumps({}), content_type="application/json"
        ).json()
        assert body["code"] == 0
        review = PlaceReview.objects.get(poi_id="P-CK2")
        assert review.rating == 5            # 缺省 5 星
        assert review.place_name == "这家店"  # 缺省店名兜底


class TestTrackApi:
    def test_supported_actions_recorded_with_weights(self, client):
        for action in ("view", "route", "click"):
            body = client.post(
                "/api/v1/places/track/",
                data=json.dumps({"action": action, "poi_id": "P-TR", "name": "小面馆"}),
                content_type="application/json",
            ).json()
            assert body["code"] == 0
            assert body["msg"] == "已记录"

        rows = list(PlaceInteraction.objects.filter(poi_id="P-TR"))
        assert len(rows) == 3
        assert {r.action: r.weight for r in rows} == {"view": 1.0, "route": 2.0, "click": 1.0}
        assert all(r.session_key for r in rows), "匿名埋点也要写会话键，便于热度归因"

    def test_unknown_or_missing_action_rejected(self, client):
        for payload in (
            {"action": "share", "poi_id": "P-TR"},
            {"action": "", "poi_id": "P-TR"},
            {"poi_id": "P-TR"},
        ):
            body = client.post(
                "/api/v1/places/track/", data=json.dumps(payload), content_type="application/json"
            ).json()
            assert body["code"] == 1
            assert "行为" in body["msg"]
        assert PlaceInteraction.objects.filter(poi_id="P-TR").count() == 0

    def test_track_rejects_get(self, client):
        assert client.get("/api/v1/places/track/").status_code == 405
