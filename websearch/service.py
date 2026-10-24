"""网络搜索服务层：带缓存的菜谱线索搜索。

对上层（`food/api.py` 的 `websearch` 视图、`RAGDjange/tools/tools.py` 的 `web_search_recipes`）
只暴露 `search_recipes_online`：

- 先查 `WebSearchCache`（`query_hash = md5(f"{provider}:{query}")`，TTL 取 `WEBSEARCH["CACHE_TTL"]`），
  命中则 `cached=True` 并 `hit_count + 1`；
- 未命中则走 `providers.web_search` 多引擎搜索，成功后写入缓存；
- 搜索全部失败且 `WEBSEARCH["OFFLINE_FALLBACK"]` 为真时，读取 `websearch/fixtures/*.json`
  返回 `degraded=True`（离线演示 / 断网可用），否则把 `WebSearchError` 抛给上层。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.db import IntegrityError
from django.utils import timezone

from websearch import providers
from websearch.providers import WebSearchError

logger = logging.getLogger("websearch")

#: 缓存查找顺序：三个真实引擎 + 离线 fixture
_CACHE_PROVIDERS: tuple[str, ...] = tuple(providers.PROVIDER_ORDER) + ("fixture",)

#: 降级结果只缓存 5 分钟，避免网络恢复后长时间返回旧数据
_DEGRADED_TTL = 300


def _elapsed_ms(started: float) -> float:
    """计算耗时（毫秒，保留两位）。"""
    return round((time.perf_counter() - started) * 1000, 2)


def query_hash(provider: str, query: str) -> str:
    """按契约生成缓存键：`md5(f"{provider}:{query}")`。"""
    return hashlib.md5(f"{provider}:{query}".encode("utf-8")).hexdigest()


def _to_result_dicts(items: list[Any], limit: int) -> list[dict[str, str]]:
    """把 SearchResult（或已序列化的 dict）规整成契约要求的列表。"""
    results: list[dict[str, str]] = []
    for item in items[:limit]:
        if isinstance(item, dict):
            results.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("snippet") or ""),
                    "source": str(item.get("source") or ""),
                }
            )
        else:
            results.append(
                {
                    "title": getattr(item, "title", "") or "",
                    "url": getattr(item, "url", "") or "",
                    "snippet": getattr(item, "snippet", "") or "",
                    "source": getattr(item, "source", "") or "",
                }
            )
    return results


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _find_cache(query: str):
    """按 provider 顺序查未过期缓存行；命中返回 `WebSearchCache`，否则 None。

    时区说明（PostgreSQL）：`USE_TZ=True` 下 `timezone.now()` 是 aware datetime，
    `expires_at` 在 PG 里是 `timestamptz`，比较由数据库按 UTC 归一化完成，
    不存在 SQLite 上「str 与 datetime 混比」的隐患；`expires_at__gt=now` 就是
    正确写法，无需手动换时区。
    """
    from food.models import WebSearchCache

    now = timezone.now()
    hashes = {query_hash(provider, query): provider for provider in _CACHE_PROVIDERS}
    try:
        row = (
            WebSearchCache.objects.filter(query_hash__in=list(hashes), expires_at__gt=now)
            .order_by("-fetched_at")
            .first()
        )
    except Exception as exc:  # pragma: no cover - 数据库不可用时不影响搜索主流程
        logger.warning("读取搜索缓存失败：%s", exc)
        return None
    return row


def _store_cache(
    query: str,
    provider: str,
    payload: dict,
    degraded: bool,
    ttl: int | None = None,
) -> None:
    """写入（或刷新）缓存行；失败只记日志，不打断搜索返回。

    并发说明：`query_hash` 是唯一索引。Django 的 `update_or_create` 内部已经
    用 savepoint 包住 INSERT，撞唯一约束时会捕获 `IntegrityError` 并回退成
    UPDATE，所以**不需要**再显式开事务；PostgreSQL 上偶发冲突时这里再兜一次
    重试，保证缓存写失败不升级成 500。
    """
    from food.models import WebSearchCache

    config = providers.get_config()
    ttl = int(ttl if ttl is not None else config.get("CACHE_TTL") or 3600)
    expires_at = timezone.now() + timedelta(seconds=max(60, ttl))

    for attempt in range(2):
        try:
            WebSearchCache.objects.update_or_create(
                query_hash=query_hash(provider, query),
                defaults={
                    "query": query[:300],
                    "provider": provider,
                    "payload": payload,
                    "degraded": degraded,
                    "expires_at": expires_at,
                },
            )
            return
        except IntegrityError as exc:  # 唯一约束冲突：重试一次即走 update 分支
            logger.warning("缓存写入唯一约束冲突，重试一次（attempt=%d）：%s", attempt + 1, exc)
            continue
        except Exception as exc:  # pragma: no cover - 缓存写失败不影响结果
            logger.warning("写入搜索缓存失败：%s", exc)
            return
    logger.warning("写入搜索缓存失败：连续冲突，已跳过本次缓存")


# ---------------------------------------------------------------------------
# 离线 fixture
# ---------------------------------------------------------------------------
def _fixture_files() -> list[Path]:
    """列出 fixtures 目录下的 json 样例（按名称排序，保证可复现）。"""
    folder = providers.fixture_dir()
    try:
        return sorted(path for path in folder.glob("*.json") if path.is_file())
    except OSError as exc:  # pragma: no cover - 目录不存在
        logger.warning("离线样例目录不可用（%s）：%s", folder, exc)
        return []


def load_fixture_results(query: str, limit: int = 5) -> list[dict[str, str]]:
    """从 `websearch/fixtures/*.json` 读取离线搜索结果。

    命名约定：文件名包含查询关键词的样例优先（如 `duckduckgo_番茄牛腩.json`），
    没有匹配时回落到第一个结构合法的 json（便于任意关键词离线演示）。
    """
    files = _fixture_files()
    if not files:
        return []

    keyword = (query or "").strip()
    preferred = [path for path in files if keyword and keyword in path.stem]
    ordered = preferred + [path for path in files if path not in preferred]

    for path in ordered:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("离线样例读取失败 %s：%s", path.name, exc)
            continue
        raw_items = data.get("results") if isinstance(data, dict) else data
        if not isinstance(raw_items, list):
            continue

        results: list[dict[str, str]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            results.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": url,
                    "snippet": str(item.get("snippet") or "").strip(),
                    "source": str(item.get("source") or "fixture"),
                }
            )
        if results:
            logger.info("离线降级命中样例 %s（%d 条）", path.name, len(results))
            return results[: max(1, limit)]
    return []


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def search_recipes_online(query: str, limit: int = 5) -> dict:
    """搜索网络菜谱线索，返回契约结构。

    返回值::

        {"query","results":[{"title","url","snippet","source"}],
         "cached","degraded","provider","latency_ms","expires_at"}
    """
    started = time.perf_counter()
    keyword = (query or "").strip()
    config = providers.get_config()
    try:
        max_results = int(config.get("MAX_RESULTS") or 8)
        limit = max(1, min(int(limit or 5), max_results))
    except (TypeError, ValueError):  # pragma: no cover - 配置异常兜底
        limit = 5

    if not keyword:
        raise WebSearchError("搜索关键词为空")

    cached = _find_cache(keyword)
    if cached is not None:
        cached.touch()
        logger.info("搜索缓存命中 provider=%s query=%s", cached.provider, keyword)
        payload = cached.payload or {}
        return {
            "query": keyword,
            "results": _to_result_dicts(list(payload.get("results") or []), limit),
            "cached": True,
            "degraded": bool(cached.degraded),
            "provider": cached.provider or str(payload.get("provider") or ""),
            "latency_ms": _elapsed_ms(started),
            "expires_at": cached.expires_at.isoformat() if cached.expires_at else "",
        }

    try:
        results = providers.web_search(keyword, limit=limit)
    except WebSearchError as exc:
        logger.warning("在线搜索失败（%s），尝试离线降级", exc)
        if not config.get("OFFLINE_FALLBACK", True):
            raise
        fixture_results = load_fixture_results(keyword, limit=limit)
        if not fixture_results:
            logger.warning("离线样例无可用结果，继续上抛错误")
            raise
        payload = {"query": keyword, "results": fixture_results, "provider": "fixture"}
        ttl = min(int(config.get("CACHE_TTL") or 3600), _DEGRADED_TTL)
        _store_cache(keyword, "fixture", payload, degraded=True, ttl=ttl)
        return {
            "query": keyword,
            "results": fixture_results,
            "cached": False,
            "degraded": True,
            "provider": "fixture",
            "latency_ms": _elapsed_ms(started),
            "expires_at": (timezone.now() + timedelta(seconds=ttl)).isoformat(),
        }

    provider = results[0].source if results else ""
    result_dicts = _to_result_dicts(list(results), limit)
    # 缓存里存未截断的完整结果，命中时再按 limit 裁剪
    payload = {"query": keyword, "results": result_dicts, "provider": provider}
    _store_cache(keyword, provider or "unknown", payload, degraded=False)
    expires_at = timezone.now() + timedelta(seconds=int(config.get("CACHE_TTL") or 3600))
    logger.info("在线搜索成功 provider=%s query=%s hits=%d", provider, keyword, len(result_dicts))

    return {
        "query": keyword,
        "results": result_dicts,
        "cached": False,
        "degraded": False,
        "provider": provider,
        "latency_ms": _elapsed_ms(started),
        "expires_at": expires_at.isoformat(),
    }


__all__ = ["search_recipes_online", "query_hash", "load_fixture_results"]
