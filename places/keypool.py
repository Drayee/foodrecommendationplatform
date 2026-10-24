"""高德 Key 池：轮询 + 配额冷却 + 故障转移。

为什么需要池
- 高德 Web 服务 Key 有两层限制：**QPS**（10021 触发）与**日调用量**（10003 / 10029 触发）；
- 单个 Key 被限流后，整个「附近美食」功能就不可用；池化后可以自动换 Key 继续服务；
- 配额类错误不能靠重试解决，只能**把该 Key 冷却到次日**，用剩下的 Key 顶住。

策略
1. `round_robin`（默认）：按「使用次数升序 + 上次使用时间升序」取用，天然分散压力；
2. 失败分类：配额类 → 长冷却（默认 6h）；QPS 类 → 短冷却（默认 5s）；其他 → 连续失败 3 次进 30s 冷却；
3. 全部 Key 都在冷却 → `acquire()` 返回 `None`，上层直接走离线降级（fixture），不报 500；
4. Key 一律脱敏后再出现在日志 / 统计接口里（`9b4e9d...147a`），避免泄露到前端或日志文件。

线程安全：`acquire` / `report_*` 全部在 `threading.Lock` 内更新，Django 多线程 dev server 可用。
跨进程不做共享（gunicorn 多 worker 时各持一份，配额粒度仍由高德侧约束，够用且无额外依赖）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger("places.keypool")

# 冷却时长（秒）
COOLDOWN_QUOTA = 6 * 3600   # 日配额 / 权限类：长时间冷却
COOLDOWN_QPS = 5            # 频率超限：几秒后回来
COOLDOWN_OTHER = 30         # 未知错误：短暂避让
FAILURES_BEFORE_COOLDOWN = 3

# 高德 infocode 分类
QUOTA_INFOCODES = {
    "10003",  # 访问已超出日访问量
    "10004",  # 单位时间内访问过于频繁（部分场景返回此码）
    "10009",  # 当前 Key 权限不足 / 配额不足
    "10010",  # IP 访问超限
    "10029",  # 日调用量超限
    "10030",  # 服务不存在或已删除
    "10044",  # 账号维度日调用量超限
    "10045",  # 账号维度 QPS 超限
}
QPS_INFOCODES = {
    "10021",  # QPS 超出限制
    "10019",  # 请求过于频繁
    "10020",  # 请求过于频繁（旧码）
}


def mask_key(key: str) -> str:
    """Key 脱敏：保留前 6 位与后 4 位。"""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "***"
    return f"{key[:6]}...{key[-4:]}"


@dataclass
class KeyState:
    key: str
    label: str = ""
    uses: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_used_at: float = 0.0
    total_latency_ms: float = 0.0

    @property
    def cooling(self) -> bool:
        return self.cooldown_until > time.time()

    @property
    def cooldown_remaining(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def to_public(self) -> dict:
        return {
            "label": self.label,
            "masked": mask_key(self.key),
            "uses": self.uses,
            "successes": self.successes,
            "failures": self.failures,
            "status": "cooling" if self.cooling else "healthy",
            "cooldown_remaining": self.cooldown_remaining,
            "last_error": self.last_error,
            "avg_latency_ms": round(self.total_latency_ms / self.successes, 1) if self.successes else 0,
        }


class KeyPool:
    """一组 Key 的调度器。构造后通过 `acquire()` 取 Key、`report_*` 回写结果。"""

    def __init__(self, keys: Iterable[str], scope: str = "amap", cooldown_quota: int = COOLDOWN_QUOTA):
        cleaned: list[str] = []
        for raw in keys or []:
            key = (raw or "").strip()
            if key and key not in cleaned:
                cleaned.append(key)
        self.scope = scope
        self.cooldown_quota = cooldown_quota
        self._lock = threading.Lock()
        self._states: list[KeyState] = [
            KeyState(key=k, label=f"{scope}#{idx + 1}") for idx, k in enumerate(cleaned)
        ]
        logger.info("Key 池[%s]初始化：%d 个 Key", scope, len(self._states))

    # -- 查询 -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._states)

    @property
    def has_keys(self) -> bool:
        return bool(self._states)

    @property
    def keys(self) -> list[str]:
        return [s.key for s in self._states]

    def available(self) -> int:
        with self._lock:
            return sum(1 for s in self._states if not s.cooling)

    # -- 调度 -------------------------------------------------------------
    def acquire(self, exclude: set[str] | None = None) -> KeyState | None:
        """取一个可用 Key；全部冷却/被排除时返回 None。"""
        exclude = exclude or set()
        now = time.time()
        with self._lock:
            candidates = [s for s in self._states if not s.cooling and s.key not in exclude]
            if not candidates:
                return None
            # 轮询：优先用得少的，其次最久没用的
            state = min(candidates, key=lambda s: (s.uses, s.last_used_at or 0.0, s.label))
            state.uses += 1
            state.last_used_at = now
            return state

    def report_success(self, state: KeyState | None, latency_ms: float = 0.0) -> None:
        if state is None:
            return
        with self._lock:
            state.successes += 1
            state.consecutive_failures = 0
            state.total_latency_ms += max(0.0, latency_ms)

    def report_failure(self, state: KeyState | None, infocode: str = "", message: str = "") -> int:
        """登记失败并按错误类型设置冷却，返回冷却秒数。"""
        if state is None:
            return 0
        infocode = str(infocode or "")
        duration = 0
        if infocode in QUOTA_INFOCODES:
            duration = self.cooldown_quota
        elif infocode in QPS_INFOCODES:
            duration = COOLDOWN_QPS
        with self._lock:
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error = f"{infocode} {message}".strip()
            if duration:
                state.cooldown_until = max(state.cooldown_until, time.time() + duration)
                logger.warning(
                    "Key %s 进入冷却 %ss（infocode=%s %s）",
                    mask_key(state.key), duration, infocode, message,
                )
            elif state.consecutive_failures >= FAILURES_BEFORE_COOLDOWN:
                duration = COOLDOWN_OTHER
                state.cooldown_until = max(state.cooldown_until, time.time() + duration)
                state.consecutive_failures = 0
                logger.warning(
                    "Key %s 连续失败 %d 次，冷却 %ss（%s）",
                    mask_key(state.key), FAILURES_BEFORE_COOLDOWN, duration, message,
                )
        return duration

    # -- 运维 -------------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            states = [s.to_public() for s in self._states]
        return {
            "scope": self.scope,
            "total": len(states),
            "available": sum(1 for s in states if s["status"] == "healthy"),
            "cooling": sum(1 for s in states if s["status"] == "cooling"),
            "keys": states,
        }

    def reset(self) -> None:
        """清空冷却与计数（运维 / 测试用）。"""
        with self._lock:
            for s in self._states:
                s.cooldown_until = 0.0
                s.failures = 0
                s.consecutive_failures = 0
                s.last_error = ""
        logger.info("Key 池[%s]已重置冷却状态", self.scope)


# ---------------------------------------------------------------------------
# 进程内单例（按 scope）
# ---------------------------------------------------------------------------
_pools: dict[str, KeyPool] = {}
_pools_lock = threading.Lock()


def get_pool(scope: str = "amap") -> KeyPool:
    """按 scope 取池；Key 列表来自 settings.AMAP，延迟创建以便测试覆盖 settings。"""
    with _pools_lock:
        pool = _pools.get(scope)
        if pool is None:
            from django.conf import settings

            cfg = getattr(settings, "AMAP", {}) or {}
            if scope == "js":
                keys = cfg.get("JS_KEYS") or ([cfg["JS_KEY"]] if cfg.get("JS_KEY") else [])
            else:
                keys = cfg.get("WEB_KEYS") or ([cfg["WEB_KEY"]] if cfg.get("WEB_KEY") else [])
            cooldown = int(cfg.get("QUOTA_COOLDOWN", COOLDOWN_QUOTA))
            pool = KeyPool(keys, scope=scope, cooldown_quota=cooldown)
            _pools[scope] = pool
        return pool


def reset_pools() -> None:
    """丢弃全部池（配置变更 / 测试隔离用）。"""
    with _pools_lock:
        _pools.clear()


def pool_stats() -> dict:
    return {"web": get_pool("amap").stats(), "js": get_pool("js").stats()}
