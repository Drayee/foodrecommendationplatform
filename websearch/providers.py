"""网络搜索与菜谱解析底层能力。

职责
1. `http_get`：**唯一**的 HTTP 出口（UA 轮换、超时、单次重试、异常统一包装为 `WebSearchError`），
   上层与测试都只依赖它，所有联网行为因此可被 `unittest.mock.patch("websearch.providers.http_get")` 打桩。
2. `web_search`：按 duckduckgo(html) → bing(cn) → 360(so.com) 依次尝试，任一成功即返回并标记 provider，
   全部失败抛 `WebSearchError`。
3. `parse_recipe_jsonld`：解析 schema.org Recipe 结构化数据（支持 `@graph`、列表嵌套、`HowToStep` /
   `HowToSection` / 纯字符串 / 字符串列表四种 instructions 形态），坏 JSON 一律跳过并返回 `None`。
4. `fetch_recipe`：抓页面 → JSON-LD 优先 → 站点启发式兜底 → 归一化结构；彻底失败抛 `ValueError`。

设计原则：JSON-LD 比站点选择器稳定得多，因此站点适配只做兜底，且每个站点给出多组候选选择器。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("websearch")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_DEFAULT_CONFIG: dict[str, Any] = {
    "TIMEOUT": 8,
    "MAX_RESULTS": 8,
    "CACHE_TTL": 3600,
    "USER_AGENTS": [_DEFAULT_UA],
    "OFFLINE_FALLBACK": True,
    "FIXTURE_DIR": "",
}


def get_config() -> dict[str, Any]:
    """返回合并后的 WEBSEARCH 配置（Django 未初始化时回落到内置默认值）。"""
    config = dict(_DEFAULT_CONFIG)
    try:  # 仅供脱离 Django 的脚本 / 单测使用
        from django.conf import settings

        config.update(getattr(settings, "WEBSEARCH", {}) or {})
    except Exception:  # pragma: no cover - 极少数未配置场景
        pass
    if not config.get("USER_AGENTS"):
        config["USER_AGENTS"] = [_DEFAULT_UA]
    if not config.get("FIXTURE_DIR"):
        config["FIXTURE_DIR"] = str(Path(__file__).resolve().parent / "fixtures")
    return config


def fixture_dir() -> Path:
    """离线样例目录（`websearch/fixtures`）。"""
    return Path(str(get_config()["FIXTURE_DIR"]))


# ---------------------------------------------------------------------------
# 错误与数据结构
# ---------------------------------------------------------------------------
class WebSearchError(RuntimeError):
    """网络搜索 / 抓取失败（连接、超时、HTTP 错误、全部引擎无结果）。"""


@dataclass
class SearchResult:
    """一条搜索结果。"""

    title: str
    url: str
    snippet: str = ""
    source: str = ""  # duckduckgo / bing / so / fixture
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成契约要求的字典（不含 extra，除非原本就有内容）。"""
        data: dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }
        if self.extra:
            data["extra"] = self.extra
        return data


# ---------------------------------------------------------------------------
# HTTP 出口
# ---------------------------------------------------------------------------
def build_headers(user_agent: str | None = None) -> dict[str, str]:
    """构造带 UA 轮换与中文 Accept-Language 的请求头。"""
    agents = get_config().get("USER_AGENTS") or [_DEFAULT_UA]
    agent = user_agent or random.choice(agents)
    return {
        "User-Agent": agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }


def http_get(url: str, timeout: int | None = None, headers: dict | None = None) -> str:
    """GET 页面并返回 HTML 文本。

    参数
    ----
    url: 目标地址。
    timeout: 超时秒数，缺省取 `WEBSEARCH["TIMEOUT"]`。
    headers: 额外请求头（覆盖同名字段）。

    行为：UA 轮换 + `Accept-Language: zh-CN`，失败重试**一次**；任何异常都包装成
    `WebSearchError`，不把 requests 的异常类型泄漏给上层。
    """
    config = get_config()
    seconds = int(timeout or config["TIMEOUT"] or 8)
    merged = build_headers()
    merged.update(headers or {})

    last_error: WebSearchError | None = None
    # 连接阶段单独给一个更短的超时，避免不可达主机把整个搜索拖到 read timeout
    connect_timeout = min(5.0, float(seconds))
    for attempt in range(2):  # 首次 + 单次重试
        try:
            response = requests.get(
                url,
                headers=merged,
                timeout=(connect_timeout, seconds),
                allow_redirects=True,
            )
            if response.status_code >= 400:
                raise WebSearchError(f"HTTP {response.status_code} {url}")
            if not response.encoding or response.encoding.lower() in {"iso-8859-1", "ascii"}:
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except WebSearchError as exc:
            last_error = exc
            logger.debug("http_get 第 %d 次失败：%s", attempt + 1, exc)
        except requests.RequestException as exc:
            last_error = WebSearchError(f"请求失败 {url}: {exc}")
            logger.debug("http_get 第 %d 次请求异常：%s", attempt + 1, exc)
        except Exception as exc:  # pragma: no cover - 兜底
            last_error = WebSearchError(f"请求异常 {url}: {exc}")
            logger.debug("http_get 第 %d 次未知异常：%s", attempt + 1, exc)

    logger.warning("http_get 最终失败：%s", last_error)
    raise last_error or WebSearchError(f"请求失败 {url}")


# ---------------------------------------------------------------------------
# HTML / 文本工具
# ---------------------------------------------------------------------------
def _soup(html: str) -> BeautifulSoup:
    """用 lxml 解析，失败时退到内置 html.parser。"""
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:  # pragma: no cover - lxml 可用时走不到
        return BeautifulSoup(html or "", "html.parser")


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\xa0"), None)


def _clean(text: Any) -> str:
    """压缩空白、去掉零宽字符。"""
    if text is None:
        return ""
    value = str(text).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", value).strip()


def _text_of(value: Any) -> str:
    """把 JSON-LD 里可能是 str/dict/list 的文本字段取成纯文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, (int, float)):
        return _clean(value)
    if isinstance(value, dict):
        for key in ("text", "name", "@value", "headline", "description"):
            if key in value:
                text = _text_of(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _text_of(item)
            if text:
                return text
    return ""


def _as_list(value: Any) -> list[Any]:
    """把任意值规整成列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _site_of(url: str) -> str:
    """从 URL 提取站点标识（去掉 www.，保留主域）。"""
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# 多引擎搜索
# ---------------------------------------------------------------------------
PROVIDER_ORDER: tuple[str, ...] = ("duckduckgo", "bing", "so")

_PROVIDER_URLS: dict[str, tuple[str, ...]] = {
    "duckduckgo": ("https://html.duckduckgo.com/html/?q={query}",),
    # cn.bing.com 对无 Cookie 的请求常回落到首页（无结果），因此追加一个 www 入口重试
    "bing": (
        "https://cn.bing.com/search?q={query}",
        "https://www.bing.com/search?q={query}&setlang=zh-CN&cc=CN",
    ),
    "so": ("https://www.so.com/s?q={query}",),
}


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo 结果链接常是 `//duckduckgo.com/l/?uddg=<urlencoded>`，还原真实地址。"""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        query = parse_qs(urlparse(href).query)
        target = query.get("uddg") or query.get("u")
        if target:
            return unquote(target[0])
    return href


def _unwrap_bing(href: str) -> str:
    """Bing 会包一层 `bing.com/ck/a?...&u=a1<base64url>` 跳转链接。"""
    href = (href or "").strip()
    if not href:
        return ""
    if "bing.com/ck/a" in href and "u=" in href:
        query = parse_qs(urlparse(href).query)
        raw = (query.get("u") or [""])[0]
        if raw.startswith("a1"):
            payload = raw[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                return base64.urlsafe_b64decode(payload).decode("utf-8", "ignore")
            except Exception:  # pragma: no cover - 编码异常时保留原链接
                return href
    return href


def _parse_duckduckgo(html: str) -> list[SearchResult]:
    """解析 DuckDuckGo HTML 版结果页（`a.result__a`）。"""
    soup = _soup(html)
    results: list[SearchResult] = []
    blocks = soup.select("div.result, div.web-result, div.result--ad")
    for block in blocks:
        link = block.select_one("a.result__a") or block.select_one("h2 a") or block.select_one("a[href]")
        if link is None:
            continue
        snippet_node = block.select_one(".result__snippet") or block.select_one("a.result__snippet")
        results.append(
            SearchResult(
                title=_clean(link.get_text(" ", strip=True)),
                url=_unwrap_ddg(link.get("href", "")),
                snippet=_clean(snippet_node.get_text(" ", strip=True)) if snippet_node else "",
                source="duckduckgo",
            )
        )
    if not results:  # 结构变化时退回最宽松的选择器
        for link in soup.select("a.result__a"):
            results.append(
                SearchResult(
                    title=_clean(link.get_text(" ", strip=True)),
                    url=_unwrap_ddg(link.get("href", "")),
                    snippet="",
                    source="duckduckgo",
                )
            )
    return results


def _parse_bing(html: str) -> list[SearchResult]:
    """解析 Bing 结果页（`li.b_algo h2 a`）。"""
    soup = _soup(html)
    results: list[SearchResult] = []
    for block in soup.select("li.b_algo, li.b_ans"):
        link = block.select_one("h2 a") or block.select_one("a[href]")
        if link is None:
            continue
        snippet_node = block.select_one("p") or block.select_one(".b_caption p") or block.select_one(".b_snippet")
        results.append(
            SearchResult(
                title=_clean(link.get_text(" ", strip=True)),
                url=_unwrap_bing(link.get("href", "")),
                snippet=_clean(snippet_node.get_text(" ", strip=True)) if snippet_node else "",
                source="bing",
            )
        )
    return results


def _parse_so(html: str) -> list[SearchResult]:
    """解析 360 搜索（so.com）结果页（`li.res-list` / `h3 a`）。"""
    soup = _soup(html)
    results: list[SearchResult] = []
    for block in soup.select("li.res-list, div.result, li.result"):
        link = block.select_one("h3 a") or block.select_one("a[href]")
        if link is None:
            continue
        url = _clean(link.get("href", ""))
        # 360 的 AI 聚合页不是真实站点，入库无意义，直接丢弃
        if not url.lower().startswith("http") or "ai.so.com" in url:
            continue
        snippet_node = (
            block.select_one(".res-desc")
            or block.select_one(".res-comm-con")
            or block.select_one("p.res-desc")
            or block.select_one("p")
        )
        results.append(
            SearchResult(
                title=_clean(link.get_text(" ", strip=True)),
                url=url,
                snippet=_clean(snippet_node.get_text(" ", strip=True)) if snippet_node else "",
                source="so",
            )
        )
    return results


_PARSERS = {
    "duckduckgo": _parse_duckduckgo,
    "bing": _parse_bing,
    "so": _parse_so,
}


def _dedupe(results: Iterable[SearchResult]) -> list[SearchResult]:
    """按 URL 去重并过滤空链接；360 的跳转链接按标题去重（同一站点会返回多条 m=xxx 变体）。"""
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for item in results:
        url = (item.url or "").strip()
        if not url.lower().startswith("http"):
            continue
        key = f"so:{item.title}" if "so.com/link" in url else url
        if key in seen:
            continue
        seen.add(key)
        item.url = url
        unique.append(item)
    return unique


def web_search(query: str, limit: int = 8, providers: list[str] | None = None) -> list[SearchResult]:
    """多引擎搜索菜谱线索。

    依次尝试 `providers`（缺省 duckduckgo → bing → 360），**任一引擎成功即返回**，
    返回结果里的 `source` 标明实际提供方；全部失败抛 `WebSearchError`。
    """
    keyword = _clean(query)
    if not keyword:
        raise WebSearchError("搜索关键词为空")

    order = [name for name in (providers or list(PROVIDER_ORDER)) if name in _PARSERS]
    if not order:
        raise WebSearchError(f"不支持的搜索引擎：{providers}")

    try:
        limit = max(1, int(limit or 8))
    except (TypeError, ValueError):
        limit = int(get_config()["MAX_RESULTS"])

    failures: list[str] = []
    for name in order:
        parsed: list[SearchResult] = []
        for index, template in enumerate(_PROVIDER_URLS[name], start=1):
            url = template.format(query=quote_plus(keyword))
            try:
                html = http_get(url)
            except WebSearchError as exc:
                failures.append(f"{name}#{index}: {exc}")
                continue
            try:
                parsed = _dedupe(_PARSERS[name](html))
            except Exception as exc:  # pragma: no cover - 解析器内部异常不外泄
                failures.append(f"{name}#{index}: 解析异常 {exc}")
                continue
            if parsed:
                break
            failures.append(f"{name}#{index}: 无结果")
        if parsed:
            logger.info("搜索成功 provider=%s query=%s hits=%d", name, keyword, len(parsed))
            return parsed[:limit]

    message = "全部搜索引擎失败：" + " | ".join(failures)
    logger.warning(message)
    raise WebSearchError(message)


# ---------------------------------------------------------------------------
# 通用解析工具：时长 / 份量 / 图片 / 食材行
# ---------------------------------------------------------------------------
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
_CN_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*(?:小时|个小时|时|hours?|hrs?|h))?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*(?:分钟|分|minutes?|mins?|m))?",
    re.IGNORECASE,
)


def parse_duration(value: Any) -> int:
    """把 ISO8601 时长（`PT1H30M`）或中文/数字时长转成分钟数；无法识别返回 0。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(round(float(value))))
    text = _clean(value)
    if not text:
        return 0

    match = _ISO_DURATION.match(text)
    if match and any(match.groupdict().values()):
        parts = match.groupdict()
        minutes = (
            float(parts.get("days") or 0) * 1440
            + float(parts.get("hours") or 0) * 60
            + float(parts.get("minutes") or 0)
            + float(parts.get("seconds") or 0) / 60
        )
        return max(0, int(round(minutes)))

    match = _CN_DURATION.search(text)
    if match and any(match.groups()):
        minutes = float(match.group(1) or 0) * 60 + float(match.group(2) or 0)
        return max(0, int(round(minutes)))

    digits = re.search(r"\d+(?:\.\d+)?", text)
    return max(0, int(round(float(digits.group())))) if digits else 0


def parse_yield(value: Any) -> int:
    """解析 `recipeYield`（"2 人份" / 4 / ["2 servings"]）成人数。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    for item in _as_list(value):
        text = _clean(item)
        digits = re.search(r"\d+", text)
        if digits:
            return max(0, int(digits.group()))
    return 0


def _parse_image(value: Any) -> str:
    """解析 `image`（str / dict{url} / list），返回首个可用地址。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "@id", "thumbnailUrl"):
            url = _text_of(value.get(key))
            if url:
                return url
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            url = _parse_image(item)
            if url:
                return url
    return ""


_ING_FIRST_NAME = [
    re.compile(r"^(?P<name>.+?)[\s:：]+(?P<amount>\d+(?:\.\d+)?(?:\s*[-~—]\s*\d+(?:\.\d+)?)?)\s*(?P<unit>[^\d\s]*)$"),
    re.compile(
        r"^(?P<name>.+?)[\s:：]+(?P<amount>适量|少许|少量|若干|一把|半[个只片块颗瓣]?|数[个片颗]|随意)(?P<unit>.*)$"
    ),
]

#: 常见计量单位（长的放前面，避免 "汤匙" 被 "匙" 抢先匹配）
_UNIT_WORDS: tuple[str, ...] = (
    "千克", "公斤", "毫升", "汤匙", "大匙", "汤勺", "茶匙", "小匙", "克", "kg", "ml", "g",
    "斤", "两", "勺", "碗", "杯", "盒", "包", "袋", "罐", "滴", "片", "个", "只", "根",
    "条", "块", "瓣", "颗", "把", "段", "匙", "张", "枚",
)
_UNIT_PATTERN = "|".join(_UNIT_WORDS)

#: 中文站点常见写法：数字 + 单位 + 名称，且中间没有空格（"600克牛腩" / "2个番茄"）
_ING_AMOUNT_NOSPACE = re.compile(
    rf"^(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})\s*(?P<name>.+)$"
)
#: "半根大葱" / "半个洋葱"
_ING_HALF = re.compile(rf"^(?P<amount>半)\s*(?P<unit>{_UNIT_PATTERN})\s*(?P<name>.+)$")
#: 名称直接跟数字："北豆腐400克"
_ING_NAME_NOSPACE = re.compile(
    rf"^(?P<name>[^\d\s]+?)(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})?$"
)

_ING_FIRST_AMOUNT = [
    re.compile(r"^(?P<amount>\d+(?:\.\d+)?(?:\s*[-~—]\s*\d+(?:\.\d+)?)?)\s*(?P<unit>[^\d\s]{0,4}?)\s+(?P<name>.+)$"),
    re.compile(r"^(?P<amount>适量|少许|少量|若干)(?P<unit>)\s+(?P<name>.+)$"),
]


def split_ingredient_line(raw: Any) -> dict[str, str]:
    """把 "牛腩 500克" / "600克牛腩" / "1汤匙 豆瓣酱" / "盐 适量" 拆成 name/amount/unit。"""
    text = _clean(raw)
    text = re.sub(r"[（(][^）)]*[）)]", "", text).strip()
    if not text:
        return {"name": "", "amount": "", "unit": ""}

    for pattern in _ING_FIRST_NAME:
        match = pattern.match(text)
        if match:
            return {
                "name": _clean(match.group("name")).strip("：: "),
                "amount": _clean(match.group("amount")),
                "unit": _clean(match.groupdict().get("unit") or ""),
            }
    for pattern in (_ING_HALF, _ING_AMOUNT_NOSPACE, _ING_NAME_NOSPACE):
        match = pattern.match(text)
        if match:
            name = _clean(match.group("name")).strip("：: ")
            if not name:
                continue
            return {
                "name": name,
                "amount": _clean(match.group("amount")),
                "unit": _clean(match.groupdict().get("unit") or ""),
            }
    for pattern in _ING_FIRST_AMOUNT:
        match = pattern.match(text)
        if match:
            return {
                "name": _clean(match.group("name")).strip("：: "),
                "amount": _clean(match.group("amount")),
                "unit": _clean(match.groupdict().get("unit") or ""),
            }
    return {"name": text, "amount": "", "unit": ""}


def _strip_step_noise(text: str) -> str:
    """去掉步骤前的 "1." / "步骤一：" 之类编号前缀。"""
    value = _clean(text)
    value = re.sub(r"^(?:step\s*\d+|步骤\s*\d+|第\s*\d+\s*步|做法\s*\d+)\s*[.、:：)）]?\s*", "", value, flags=re.I)
    value = re.sub(r"^\d+\s*[.、:：)）]\s*", "", value)
    return value


#: 单串多步文本里的编号标记（排除 "3.5克" 这类小数）
_STEP_MARKER = re.compile(r"[,，。;；!！?？\s]*(\d{1,2})\s*[.、)）](?!\d)")
#: 这些前缀是栏目名而非步骤内容，拆分时丢弃
_STEP_JUNK_PREFIX = {"食材", "食材集合", "材料", "用料", "做法", "步骤", "调料", "主料", "辅料", "配料"}


def split_step_blob(text: Any) -> list[str]:
    """把 "1.切块 2.焯水 3.下锅" 这类挤在一个字符串里的多步文本拆成多条步骤。

    只在检测到 **两个及以上** 编号标记时才拆分，避免误伤普通长句。
    """
    value = _clean(text)
    if not value:
        return []
    markers = list(_STEP_MARKER.finditer(value))
    if len(markers) < 2:
        return [value]

    pieces: list[str] = []
    prefix = value[: markers[0].start()].strip(" ,，。;；")
    if len(prefix) >= 4 and prefix not in _STEP_JUNK_PREFIX:
        pieces.append(prefix)
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(value)
        piece = value[marker.end() : end].strip(" ,，。;；")
        if piece:
            pieces.append(piece)
    return pieces or [value]


# ---------------------------------------------------------------------------
# JSON-LD 菜谱解析
# ---------------------------------------------------------------------------
def _iter_jsonld_nodes(data: Any, depth: int = 0) -> Iterator[dict]:
    """递归展开 JSON-LD：兼容 dict / list / `@graph` / 嵌套实体。"""
    if depth > 8:
        return
    if isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "hasPart", "itemListElement", "itemList"):
            child = data.get(key)
            if child is not None:
                yield from _iter_jsonld_nodes(child, depth + 1)
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from _iter_jsonld_nodes(item, depth + 1)


def _is_recipe_node(node: dict) -> bool:
    """判断节点是不是 Recipe（含 `@type: Recipe` 或直接带 recipeIngredient/recipeInstructions）。"""
    raw_type = node.get("@type") or node.get("type")
    types = [str(item).lower() for item in _as_list(raw_type)]
    if any(item == "recipe" or item.endswith(":recipe") or item.endswith("/recipe") for item in types):
        return True
    return bool(node.get("recipeIngredient") or node.get("recipeInstructions"))


def _flatten_instructions(value: Any, depth: int = 0) -> list[dict[str, str]]:
    """把 recipeInstructions 的所有形态拍平成 [{"text","image_url"}]。

    支持：纯字符串、字符串列表、HowToStep、HowToSection（含 itemListElement / steps）、
    以及任意层级的嵌套列表。
    """
    steps: list[dict[str, str]] = []
    if value is None or depth > 8:
        return steps

    if isinstance(value, str):
        for piece in split_step_blob(_strip_step_noise(value)):
            steps.append({"text": piece, "image_url": ""})
        return steps

    if isinstance(value, (list, tuple)):
        for item in value:
            steps.extend(_flatten_instructions(item, depth + 1))
        return steps

    if isinstance(value, dict):
        nested = value.get("itemListElement") or value.get("steps")
        if nested is not None:
            # HowToSection：容器本身不是步骤，只递归其子步骤
            steps.extend(_flatten_instructions(nested, depth + 1))
            return steps
        text = _strip_step_noise(_text_of(value.get("text")) or _text_of(value.get("name")))
        if text:
            image = _parse_image(value.get("image"))
            for index, piece in enumerate(split_step_blob(text)):
                steps.append({"text": piece, "image_url": image if index == 0 else ""})
        return steps

    text = _strip_step_noise(_text_of(value))
    if text:
        steps.append({"text": text, "image_url": ""})
    return steps


def _parse_nutrition(value: Any) -> float:
    """从 `nutrition.calories`（"380 kcal" / 380 / {"@value": 380}）解析千卡数。"""
    if value is None:
        return 0.0
    if isinstance(value, dict):
        return _parse_nutrition(value.get("calories") or value.get("@value") or value.get("value"))
    if isinstance(value, (int, float)):
        return round(float(value), 1)
    digits = re.search(r"\d+(?:\.\d+)?", _clean(value))
    return round(float(digits.group()), 1) if digits else 0.0


def _keywords_to_list(value: Any) -> list[str]:
    """把 keywords / recipeCategory / recipeCuisine 归整成去重字符串列表。"""
    items: list[str] = []
    for raw in _as_list(value):
        text = _text_of(raw)
        if not text:
            continue
        for piece in re.split(r"[,，、/|]+", text):
            piece = _clean(piece)
            if piece and piece not in items:
                items.append(piece)
    return items


def parse_recipe_jsonld(html: str, url: str = "") -> dict | None:
    """解析页面中的 schema.org Recipe（JSON-LD）。

    返回 `{"title","summary","cover_url","ingredients","steps","servings","cook_minutes",
    "prep_minutes","source_site","source_url"}` 以及可选的 `calories / cuisine / tags`；
    没有 Recipe 或 JSON 全部损坏时返回 `None`（**不抛异常**）。
    """
    if not html:
        return None

    soup = _soup(html)
    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})
    if not scripts:
        return None

    nodes: list[dict] = []
    for script in scripts:
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        # 有些站点在 JSON 前后拼接注释/跟踪代码，截取最外层大括号再试一次
        candidates = [raw]
        if not raw.startswith("{") and not raw.startswith("["):
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                candidates.append(raw[start : end + 1])
        parsed = None
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                break
            except (json.JSONDecodeError, ValueError):
                continue
        if parsed is None:
            logger.debug("JSON-LD 解析失败，已跳过：%s", raw[:80].replace("\n", " "))
            continue
        nodes.extend(_iter_jsonld_nodes(parsed))

    recipe = next((node for node in nodes if _is_recipe_node(node)), None)
    if recipe is None:
        return None

    title = _text_of(recipe.get("name")) or _text_of(recipe.get("headline"))
    if not title:
        logger.debug("JSON-LD Recipe 缺少 name 字段：%s", url)
        return None

    ingredients = [split_ingredient_line(item) for item in _as_list(recipe.get("recipeIngredient"))]
    ingredients = [item for item in ingredients if item["name"]]

    steps = _flatten_instructions(recipe.get("recipeInstructions"))

    data: dict[str, Any] = {
        "title": title,
        "summary": _text_of(recipe.get("description")),
        "cover_url": _parse_image(recipe.get("image")),
        "ingredients": ingredients,
        "steps": steps,
        "servings": parse_yield(recipe.get("recipeYield")),
        "cook_minutes": parse_duration(recipe.get("cookTime")),
        "prep_minutes": parse_duration(recipe.get("prepTime")),
        "source_site": _site_of(url),
        "source_url": url,
        "calories": _parse_nutrition(recipe.get("nutrition")),
        "cuisine": (_keywords_to_list(recipe.get("recipeCuisine")) or [""])[0],
        "tags": _keywords_to_list(recipe.get("keywords")) + _keywords_to_list(recipe.get("recipeCategory")),
    }
    logger.debug("JSON-LD 解析成功：%s（食材 %d / 步骤 %d）", title, len(ingredients), len(steps))
    return data


# ---------------------------------------------------------------------------
# 站点启发式兜底（JSON-LD 缺失时）
# ---------------------------------------------------------------------------
_SITE_RULES: dict[str, dict[str, list[str]]] = {
    "xiachufang": {
        "title": ["h1.page-title", "h1.recipe-title", "div.title h1", "h1"],
        "ingredients": [
            "div.ings tr",
            "div.ings table tr",
            "div.ingredients tr",
            ".ings .name",
            "div.recipe-ingredients tr",
        ],
        "steps": [".steps li", "div.steps li", ".steps .text", "div.recipe-steps li", ".step"],
    },
    "meishij": {
        "title": ["h1.recipe_De_title", "h1.title", "div.recipe_title h1", "h1"],
        "ingredients": [
            "div.materials_box li",
            "div.materials li",
            "fieldset.materials li",
            "#materials li",
            "div.recipe_material li",
        ],
        "steps": ["div.step_list li", "div.recipe_step li", "div.steps li", "#step_list li"],
    },
    "douguo": {
        "title": ["h1.recipe-name", "h1.title", "div.recipe-title h1", "h1"],
        "ingredients": [
            "div.ingredients li",
            "ul.ingredients li",
            "div.recipe-ingredients li",
            "div.ingredient-list li",
        ],
        "steps": ["div.steps li", "ol.steps li", "div.recipe-step li", "div.step-list li"],
    },
    "cookpad": {
        "title": ["#recipe-title", "h1.recipe-title", "h1 span", "h1"],
        "ingredients": ["#ingredients li", "div#ingredients li", "ul.ingredients__list li"],
        "steps": ["#steps li", "div#steps li", "ol.steps li"],
    },
}

_GENERIC_RULES = {
    "title": ["h1[itemprop='name']", "h1.recipe-title", "h1"],
    "ingredients": ["[itemprop='recipeIngredient']", "ul.ingredients li", "div.ingredients li"],
    "steps": ["[itemprop='recipeInstructions'] li", "[itemprop='recipeInstructions'] p", "ol.steps li"],
}


def _match_rules(url: str, html: str) -> dict[str, list[str]] | None:
    """按域名挑站点规则；未知名站点返回 None（交给通用规则）。"""
    site = _site_of(url)
    for key, rules in _SITE_RULES.items():
        if key in site:
            return rules
    return None


def _select_texts(soup: BeautifulSoup, selectors: list[str]) -> list[str]:
    """按候选选择器取文本列表（命中第一个非空选择器即返回）。"""
    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception:  # pragma: no cover - 非法选择器
            continue
        texts = [_clean(node.get_text(" ", strip=True)) for node in nodes]
        texts = [text for text in texts if text]
        if texts:
            return texts
    return []


def _parse_with_rules(html: str, url: str, rules: dict[str, list[str]]) -> dict | None:
    """按选择器规则解析出统一结构；信息不足返回 None。"""
    soup = _soup(html)

    title = ""
    for selector in rules.get("title", []):
        node = soup.select_one(selector)
        if node:
            title = _clean(node.get_text(" ", strip=True))
            if title:
                break
    if not title:
        meta = soup.find("meta", attrs={"property": "og:title"})
        title = _clean(meta.get("content")) if meta else ""
    if not title:
        return None

    ingredient_lines = _select_texts(soup, rules.get("ingredients", []))
    ingredients = [split_ingredient_line(line) for line in ingredient_lines]
    ingredients = [item for item in ingredients if item["name"]]

    step_lines = _select_texts(soup, rules.get("steps", []))
    steps = [{"text": text, "image_url": ""} for text in (_strip_step_noise(line) for line in step_lines) if text]

    if not ingredients and not steps:
        return None

    return {
        "title": title,
        "summary": "",
        "cover_url": "",
        "ingredients": ingredients,
        "steps": steps,
        "servings": 0,
        "cook_minutes": 0,
        "prep_minutes": 0,
        "source_site": _site_of(url),
        "source_url": url,
        "calories": 0.0,
        "cuisine": "",
        "tags": [],
    }


def parse_recipe_site(html: str, url: str = "") -> dict | None:
    """JSON-LD 缺失时的站点启发式解析：站点规则 → 通用 itemprop 规则。"""
    if not html:
        return None
    rules = _match_rules(url, html)
    if rules:
        data = _parse_with_rules(html, url, rules)
        if data:
            logger.debug("站点规则解析成功：%s（%s）", data["title"], _site_of(url))
            return data
    data = _parse_with_rules(html, url, _GENERIC_RULES)
    if data:
        logger.debug("通用规则解析成功：%s", data["title"])
    return data


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def _normalize_recipe(data: dict, url: str) -> dict:
    """补全并裁剪字段，保证下游拿到的结构与契约一致。"""
    title = _clean(data.get("title"))[:120]
    ingredients = []
    for item in data.get("ingredients") or []:
        name = _clean(item.get("name"))[:60]
        if not name:
            continue
        ingredients.append(
            {
                "name": name,
                "amount": _clean(item.get("amount"))[:30],
                "unit": _clean(item.get("unit"))[:16],
            }
        )
    steps = []
    for item in data.get("steps") or []:
        text = _clean(item.get("text"))
        if not text:
            continue
        steps.append({"text": text, "image_url": _clean(item.get("image_url"))[:500]})

    return {
        "title": title,
        "summary": _clean(data.get("summary"))[:1000],
        "cover_url": _clean(data.get("cover_url"))[:500],
        "ingredients": ingredients,
        "steps": steps,
        "servings": max(0, int(data.get("servings") or 0)),
        "cook_minutes": max(0, int(data.get("cook_minutes") or 0)),
        "prep_minutes": max(0, int(data.get("prep_minutes") or 0)),
        "source_site": _clean(data.get("source_site"))[:60] or _site_of(url),
        "source_url": url,
        "calories": round(float(data.get("calories") or 0), 1),
        "cuisine": _clean(data.get("cuisine"))[:40],
        "tags": [_clean(tag)[:30] for tag in (data.get("tags") or []) if _clean(tag)][:8],
    }


def fetch_recipe(url: str) -> dict:
    """抓取并解析一个菜谱页面，返回统一结构。

    优先级：JSON-LD（最稳）→ 站点启发式规则 → 通用 itemprop 规则。
    网络失败的 `WebSearchError` 原样上抛；页面结构无法识别时抛 `ValueError`。
    """
    target = (url or "").strip()
    if not target.lower().startswith("http"):
        raise ValueError(f"非法菜谱链接：{url}")

    html = http_get(target)
    data = parse_recipe_jsonld(html, target)
    if data and not (data.get("ingredients") or data.get("steps")):
        logger.debug("JSON-LD 缺少食材与步骤，转站点兜底：%s", target)
        data = None
    if not data:
        data = parse_recipe_site(html, target)
    if not data:
        raise ValueError(f"无法解析菜谱页面（无 JSON-LD 且站点规则未命中）：{target}")

    return _normalize_recipe(data, target)


__all__ = [
    "SearchResult",
    "WebSearchError",
    "PROVIDER_ORDER",
    "build_headers",
    "fetch_recipe",
    "get_config",
    "http_get",
    "parse_duration",
    "parse_recipe_jsonld",
    "parse_recipe_site",
    "parse_yield",
    "split_ingredient_line",
    "web_search",
]
