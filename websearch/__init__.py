"""`websearch` 包：网络搜索与菜谱采集。

模块职责
- `providers`：多引擎搜索（duckduckgo → bing → 360）+ JSON-LD 菜谱解析 + 站点启发式兜底，
  是所有 HTTP 访问的唯一出口（`http_get`），便于测试打桩。
- `service`：对上层提供 `search_recipes_online`，负责 `WebSearchCache` 缓存与离线 fixture 降级。
- `ingest`：`import_recipe` 抓取 → 解析 → 去重 → 落库（Recipe / 食材 / 步骤 / 菜系 / 标签）。

对外契约见 `docs/CONTRACTS.md` 第 4 节。
"""

from __future__ import annotations

__all__ = ["providers", "service", "ingest"]
