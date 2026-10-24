"""高德 Web 服务客户端（REST v3）。

设计要点
1. **唯一 HTTP 出口**：所有高德请求都走 `AmapClient._request`，便于测试打桩与统一降级；
2. **Key 池**：每次请求从 `KeyPool` 取一个可用 Key，失败按 infocode 分类冷却并自动换 Key；
3. **绕系统代理**：本机装过全局代理，requests 默认会读注册表代理导致连接失败，这里显式
   `Session.trust_env = False`；
4. **不抛裸异常**：统一 `AmapError`，带 `infocode` 与 `retryable`，上层据此决定降级还是提示。

用到的接口
- `/v3/place/around`     周边搜索（核心）
- `/v3/place/text`       关键词搜索（异地找店、搜索框）
- `/v3/place/detail`     POI 详情（营业时间、电话、评分）
- `/v3/geocode/regeo`    逆地理编码（定位 → 省市区/商圈）
- `/v3/ip`               IP 定位（GPS 被拒时兜底）
- `/v3/assistant/inputtips` 输入提示
"""
from __future__ import annotations

import logging
import threading
import time

import requests
from django.conf import settings

from places.keypool import KeyPool, get_pool

logger = logging.getLogger("places.amap")


class AmapError(RuntimeError):
    """高德接口错误。`infocode` 为高德原始错误码，`retryable` 表示换 Key 后可能成功。"""

    def __init__(self, message: str, infocode: str = "", *, retryable: bool = False, http_status: int = 0):
        super().__init__(message)
        self.infocode = str(infocode or "")
        self.retryable = retryable
        self.http_status = http_status

    @property
    def is_quota_error(self) -> bool:
        return self.infocode in {"10003", "10004", "10009", "10010", "10029", "10030", "10044", "10045"}

    @property
    def is_no_key(self) -> bool:
        return self.infocode in {"NO_KEY", "DISABLED"}


def _amap_cfg() -> dict:
    return getattr(settings, "AMAP", {}) or {}


class AmapClient:
    BASE_URL = "https://restapi.amap.com"
    USER_AGENT = "XunweiFood/1.0 (nearby-food; +https://github.com/Drayee/foodrecommendationplatform)"

    def __init__(self, pool: KeyPool | None = None, timeout: int | None = None):
        self._pool = pool
        cfg = _amap_cfg()
        self.timeout = int(timeout or cfg.get("TIMEOUT", 8))
        self._session: requests.Session | None = None
        self._lock = threading.Lock()

    # -- 基础 -------------------------------------------------------------
    @property
    def pool(self) -> KeyPool:
        if self._pool is None:
            self._pool = get_pool("amap")
        return self._pool

    @property
    def configured(self) -> bool:
        cfg = _amap_cfg()
        return bool(cfg.get("ENABLED", True)) and self.pool.has_keys

    def _get_session(self) -> requests.Session:
        with self._lock:
            if self._session is None:
                session = requests.Session()
                session.trust_env = False  # 不走系统代理
                session.headers.update({"User-Agent": self.USER_AGENT, "Accept": "application/json"})
                self._session = session
            return self._session

    # -- 核心请求 ---------------------------------------------------------
    def _request(self, path: str, params: dict) -> dict:
        cfg = _amap_cfg()
        if not cfg.get("ENABLED", True):
            raise AmapError("高德服务已禁用（AMAP_ENABLED=0）", "DISABLED")
        if not self.pool.has_keys:
            raise AmapError("未配置高德 Web 服务 Key（AMAP_WEB_KEY / AMAP_WEB_KEYS）", "NO_KEY")

        tried: set[str] = set()
        last_error: AmapError | None = None
        attempts = max(1, len(self.pool))

        for _ in range(attempts):
            state = self.pool.acquire(exclude=tried)
            if state is None:
                break
            tried.add(state.key)
            query = {**params, "key": state.key}
            started = time.perf_counter()
            try:
                resp = self._get_session().get(
                    f"{self.BASE_URL}{path}", params=query, timeout=self.timeout
                )
            except requests.RequestException as exc:
                self.pool.report_failure(state, "", f"network: {exc}")
                last_error = AmapError(f"高德网络请求失败：{exc}", "NETWORK", retryable=True)
                continue

            latency_ms = (time.perf_counter() - started) * 1000
            if resp.status_code != 200:
                self.pool.report_failure(state, "", f"http {resp.status_code}")
                last_error = AmapError(
                    f"高德返回 HTTP {resp.status_code}", f"HTTP_{resp.status_code}",
                    retryable=True, http_status=resp.status_code,
                )
                continue

            try:
                data = resp.json()
            except ValueError:
                self.pool.report_failure(state, "", "bad json")
                last_error = AmapError("高德返回非 JSON 内容", "BAD_JSON", retryable=True)
                continue

            if str(data.get("status", "")) == "1":
                self.pool.report_success(state, latency_ms)
                data["_key_label"] = state.label
                data["_latency_ms"] = round(latency_ms, 1)
                return data

            infocode = str(data.get("infocode", ""))
            info = str(data.get("info", "") or "高德接口调用失败")
            self.pool.report_failure(state, infocode, info)
            last_error = AmapError(info, infocode, retryable=True)

        raise last_error or AmapError("所有高德 Key 均不可用", "ALL_EXHAUSTED")

    # -- 业务接口 ---------------------------------------------------------
    def around(
        self,
        lng: float,
        lat: float,
        radius: int | None = None,
        keywords: str = "",
        types: str | None = None,
        page: int = 1,
        offset: int = 25,
        sortrule: str = "distance",
        extensions: str = "all",
    ) -> dict:
        """周边搜索：以 (lng,lat) 为圆心搜餐饮 POI。"""
        cfg = _amap_cfg()
        max_radius = int(cfg.get("MAX_RADIUS", 50000))
        radius = int(radius or cfg.get("DEFAULT_RADIUS", 3000))
        radius = max(1, min(radius, max_radius))
        params = {
            "location": f"{float(lng):.6f},{float(lat):.6f}",
            "radius": radius,
            "types": types or cfg.get("DEFAULT_TYPES", "050000"),
            "offset": max(1, min(int(offset), 25)),
            "page": max(1, min(int(page), 100)),
            "sortrule": sortrule if sortrule in {"distance", "weight"} else "distance",
            "extensions": "all" if extensions == "all" else "base",
        }
        if keywords:
            params["keywords"] = keywords[:60]
        return self._request("/v3/place/around", params)

    def text(
        self,
        keywords: str,
        city: str = "",
        page: int = 1,
        offset: int = 25,
        types: str | None = None,
        extensions: str = "all",
    ) -> dict:
        """关键词搜索（找特定店名 / 异地城市搜索）。"""
        params = {
            "keywords": (keywords or "")[:60],
            "offset": max(1, min(int(offset), 25)),
            "page": max(1, min(int(page), 100)),
            "extensions": "all" if extensions == "all" else "base",
            "types": types or _amap_cfg().get("DEFAULT_TYPES", "050000"),
        }
        if city:
            params["city"] = city[:20]
            params["citylimit"] = "false"
        return self._request("/v3/place/text", params)

    def detail(self, poi_id: str) -> dict:
        if not poi_id:
            raise AmapError("缺少 POI id", "BAD_REQUEST")
        return self._request("/v3/place/detail", {"id": poi_id, "extensions": "all"})

    def regeo(self, lng: float, lat: float, extensions: str = "base") -> dict:
        return self._request(
            "/v3/geocode/regeo",
            {
                "location": f"{float(lng):.6f},{float(lat):.6f}",
                "extensions": "all" if extensions == "all" else "base",
                "radius": 1000,
            },
        )

    def ip(self, ip: str | None = None) -> dict:
        params = {"ip": ip} if ip else {}
        return self._request("/v3/ip", params)

    def inputtips(self, keywords: str, city: str = "", lng: float | None = None, lat: float | None = None) -> dict:
        params = {"keywords": (keywords or "")[:60], "datatype": "poi"}
        if city:
            params["city"] = city[:20]
        if lng is not None and lat is not None:
            params["location"] = f"{float(lng):.6f},{float(lat):.6f}"
        return self._request("/v3/assistant/inputtips", params)


_client: AmapClient | None = None
_client_lock = threading.Lock()


def get_client() -> AmapClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AmapClient()
        return _client


def reset_client() -> None:
    """配置变更 / 测试隔离时丢弃单例。"""
    global _client
    with _client_lock:
        _client = None
