"""AI 美食助手：RAG 检索 + 工具调用 + 流式生成 + 无模型降级。

调用链
    question
      → Retriever 混合检索（向量 + BM25）得到 context
      → 拼装 system / history / context
      → 有 LLM：两阶段（function-calling 决策 → 流式生成）
        无 LLM：抽取式总结（把检索到的菜谱要点组织成结构化回答）
      → 写 ChatMessage（含 citations）

对外接口
    stream_answer(...) -> AnswerStream   可迭代出文本分片，消费后读 .citations
    answer_once(...)   -> dict           非流式，便于测试与后台任务
    llm_ready() / llm_status() / index_status()

首字延迟
    小显存机器上模型首 token 可能要 60~160s。`AnswerStream` 在 LLM 决策前先 yield 一句过程提示
    （`正在查附近的店…` / `正在翻菜谱库…`），分片类型为 `PreambleChunk(str)`，正文同步写入
    `.preamble`，不污染 `.answer`（落库的对话内容仍然是纯正文）。
    `settings.LLM["PREAMBLE"] = False` 可整体关掉，行为回到原样。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Iterable, Iterator

from django.conf import settings

from RAGDjange.llm import LLMUnavailable, get_client
from RAGDjange.tools.tools import TOOL_REGISTRY, call_tool, tool_schemas

logger = logging.getLogger("rag.assistant")

SYSTEM_PROMPT = """你是「寻味」美食推荐平台的 AI 美食助手，名字叫小味。

你的能力边界：
- 你只能依据【检索到的菜谱资料】和【工具返回结果】回答，不能编造菜名、用量和步骤；
- 资料不足时，明确说明「平台菜谱库暂时没有这道菜的完整做法」，然后给出相近菜式或建议联网检索；
- 用户问营养、热量、时间等问题时，优先引用资料里的数值，并说明是估算值。

回答风格：
- 中文，简洁，先给结论再给细节；
- 推荐菜谱时用「- 菜名（菜系｜耗时｜热量）」列表，并附一句推荐理由；
- 涉及步骤时用有序列表，把「火候/时间/关键动作」写清楚；
- 末尾用「参考：」列出你实际用到的菜谱名，不要编造来源。

你可以调用的工具：按关键词检索菜谱、查菜谱详情、个性化推荐、**查询附近餐厅**（nearby_foods）、联网搜索菜谱线索、查平台统计、发邮件。

当用户问「附近」「周边」「走路能到」「附近有什么好吃的」「下班吃点什么」这类问题时：
- 优先调用 nearby_foods 拿真实店铺数据，再据此回答，不要凭空推荐店名或编造距离；
- 若当前会话没有位置坐标，直接告诉用户「点一下『附近』页的定位，我就能看到你在哪」；
- 推荐店铺时给出「店名（品类｜距离｜评分｜人均）」，并说明推荐理由与营业时间。

只有当站内资料确实不够时才联网搜索；只有在用户明确要求时才发送邮件。
"""

EMAIL_PROMPT = """你是「寻味」平台的美食邮件助手，负责把菜谱清单整理成适合邮件阅读的 HTML。

规则：
1. 先用工具拿到菜谱（search_recipes / get_recipe_detail / recommend_recipes），不要凭空写；
2. 输出一个完整的 HTML 片段（不要 markdown 代码块），包含菜名、食材用量、简要步骤；
3. 正文控制在 600 字以内，配色暖色系，标题用 <h2>；
4. 只有用户明确说「发送」「可以」时，才调用 send_email；否则只输出 HTML 预览并询问收件邮箱。
"""

# ---------------------------------------------------------------------------
# 首字延迟兜底：LLM 决策前的过程提示
# ---------------------------------------------------------------------------
# 小显存机器上 qwen3:14b 首 token 可能要 60~160s，先吐一句过程提示，用户不至于面对空白。
# 关键词只做粗分类（用户措辞千变万化，猜太细反而误判），真正的意图仍由模型 + 工具决定。
NEARBY_HINTS = ("附近", "周边", "周围", "走路", "下班", "吃什么", "最近", "这边")
PREAMBLE_NEARBY = "正在查附近的店…"
PREAMBLE_DEFAULT = "正在翻菜谱库…"


class PreambleChunk(str):
    """过程提示分片。

    仍是 `str`（SSE 序列化、前端展示与普通分片一致），但 `isinstance(chunk, PreambleChunk)`
    为真、`chunk.kind == "preamble"`，包装层想区分时不必再看内容。
    """

    kind = "preamble"


def _preamble_enabled() -> bool:
    """`settings.LLM["PREAMBLE"]` 默认开；显式关掉后行为回到「一句不说，直接等模型」。"""
    cfg = getattr(settings, "LLM", {}) or {}
    value = cfg.get("PREAMBLE", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _has_location(location: dict | None) -> bool:
    try:
        lng = float((location or {}).get("lng") or 0)
        lat = float((location or {}).get("lat") or 0)
    except (TypeError, ValueError):
        return False
    if lng == 0 and lat == 0:
        return False
    return -180 <= lng <= 180 and -90 <= lat <= 90


def nearby_intent(question: str) -> bool:
    """粗判「附近意图」：命中关键词集合即算，配合坐标才提示「查附近的店」。"""
    text = (question or "").strip()
    return bool(text) and any(word in text for word in NEARBY_HINTS)


def build_preamble(question: str, location: dict | None = None) -> str:
    """返回过程提示文案；关掉开关时返回空串。"""
    if not _preamble_enabled():
        return ""
    if nearby_intent(question) and _has_location(location):
        return PREAMBLE_NEARBY
    return PREAMBLE_DEFAULT


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def llm_ready() -> bool:
    try:
        return get_client().available()
    except Exception:  # pragma: no cover
        return False


def llm_status() -> dict:
    try:
        return get_client().status()
    except Exception as exc:  # pragma: no cover
        return {"ready": False, "error": str(exc)}


def index_status() -> dict:
    try:
        from rag.retriever import get_retriever

        return get_retriever().stats()
    except Exception as exc:  # pragma: no cover - 索引尚未构建
        return {"ready": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def retrieve(question: str, k: int | None = None) -> list:
    """混合检索；索引缺失时返回空列表，绝不让助手整体失败。"""
    k = k or int((getattr(settings, "RAG", {}) or {}).get("TOP_K", 5))
    try:
        from rag.retriever import get_retriever

        return get_retriever().search(question, k=k)
    except Exception as exc:
        logger.warning("检索失败，降级为空上下文：%s", exc)
        return []


def _hits_to_context(hits: Iterable) -> str:
    """把检索结果拼成上下文。RRF 原始分很小（0.00x~0.0x），展示时按本轮最高分归一化。"""
    hits = list(hits)
    top = max([float(getattr(h, "score", 0.0) or 0.0) for h in hits] + [1e-9])
    lines = []
    for idx, hit in enumerate(hits, start=1):
        payload = getattr(hit, "payload", {}) or {}
        title = payload.get("title") or f"资料{idx}"
        relevance = float(getattr(hit, "score", 0.0) or 0.0) / top
        text = (getattr(hit, "text", "") or "").strip()
        lines.append(f"[{idx}] 《{title}》（相关度 {relevance:.0%}）\n{text}")
    return "\n\n".join(lines)


def _citations_from_hits(hits: Iterable) -> list[dict]:
    hits = list(hits)
    top = max([float(getattr(h, "score", 0.0) or 0.0) for h in hits] + [1e-9])
    out, seen = [], set()
    for hit in hits:
        payload = getattr(hit, "payload", {}) or {}
        rid = getattr(hit, "recipe_id", None) or payload.get("recipe_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        raw = float(getattr(hit, "score", 0.0) or 0.0)
        out.append(
            {
                "recipe_id": rid,
                "title": payload.get("title", ""),
                "score": round(raw, 5),
                "relevance": round(raw / top, 3),
                "snippet": (getattr(hit, "text", "") or "")[:180],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------
def _history_messages(session_key: str, limit: int = 6) -> list[dict]:
    from food.models import ChatSession

    session = ChatSession.objects.filter(session_key=session_key).first()
    if not session:
        return []
    msgs = list(session.messages.order_by("-created_at")[:limit])[::-1]
    return [{"role": m.role, "content": m.content} for m in msgs if m.role in {"user", "assistant"}]


def _persist(session_key: str, user, question: str, answer: str, citations: list[dict], latency_ms: float):
    from food.models import ChatMessage, ChatSession

    session = ChatSession.get_or_create_by_key(session_key, user=user if getattr(user, "is_authenticated", False) else None)
    if not session.title or session.title == "新的美食对话":
        session.title = (question or "")[:40] or "新的美食对话"
        session.save(update_fields=["title", "updated_at"])
    ChatMessage.objects.create(session=session, role="user", content=question)
    ChatMessage.objects.create(
        session=session, role="assistant", content=answer, citations=citations, latency_ms=latency_ms
    )
    return session


# ---------------------------------------------------------------------------
# 降级：无 LLM 时的抽取式回答
# ---------------------------------------------------------------------------
def _extractive_answer(question: str, hits: list) -> str:
    if not hits:
        return (
            "平台菜谱库里没有检索到与这个问题直接相关的内容。\n\n"
            "可以试试：\n"
            "- 换一个更具体的食材或菜名，例如「番茄牛腩」「低卡鸡胸」；\n"
            "- 在搜索栏用「菜系 + 主料」的组合，例如「川菜 豆腐」；\n"
            "- 或者在管理后台执行 `python manage.py crawl_recipes` 补充菜谱数据。\n\n"
            "（提示：当前未配置大模型 API Key，回答由本地检索结果直接组织，配置 `.env` 中的 "
            "`LLM_API_KEY` 后可获得完整生成式回答。）"
        )

    blocks = ["根据平台菜谱库，与你问题最相关的是："]
    for idx, hit in enumerate(hits[:4], start=1):
        payload = getattr(hit, "payload", {}) or {}
        title = payload.get("title", f"资料{idx}")
        meta = "｜".join(
            x for x in [payload.get("cuisine", ""), payload.get("difficulty_label", ""),
                        f'{payload.get("total_minutes", "")}分钟' if payload.get("total_minutes") else ""] if x
        )
        snippet = (getattr(hit, "text", "") or "").replace("\n", " ")
        blocks.append(f"{idx}. **{title}**" + (f"（{meta}）" if meta else "") + f"\n   {snippet[:220]}")
    blocks.append(
        "\n参考：" + "、".join((getattr(h, "payload", {}) or {}).get("title", "") for h in hits[:4])
    )
    blocks.append(
        "\n（当前为本地检索模式：未检测到可用的大模型 Key，以上内容直接由检索结果组织；"
        "配置 `LLM_API_KEY` 后可获得完整自然语言回答。）"
    )
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 工具调用（两阶段）
# ---------------------------------------------------------------------------
def _tool_round(messages: list[dict], max_rounds: int = 2) -> tuple[list[dict], list[dict]]:
    """让模型决定是否调用工具；返回 (更新后的 messages, 调用记录)。"""
    client = get_client()
    calls: list[dict] = []
    for _ in range(max_rounds):
        try:
            resp = client._get_client().chat.completions.create(
                model=client.model_name,
                messages=messages,
                tools=tool_schemas(),
                tool_choice="auto",
                temperature=0.2,
                # 这一阶段只需要模型吐出 tool_calls，不需要长文本；限流能显著缩短首字延迟
                max_tokens=300,
            )
        except Exception as exc:
            logger.info("工具调用阶段跳过：%s", exc)
            return messages, calls
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            break
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            name = tc.function.name
            result = call_tool(name, tc.function.arguments)
            calls.append({"name": name, "arguments": tc.function.arguments, "result": result[:500]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:4000]})
    return messages, calls


def _build_messages(question: str, context: str, history: list[dict], mode: str, location: dict | None = None) -> list[dict]:
    system = EMAIL_PROMPT if mode == "email" else SYSTEM_PROMPT
    if context:
        system = system + "\n\n【检索到的菜谱资料】\n" + context
    if location and location.get("lng") and location.get("lat"):
        system += (
            f"\n\n【用户当前位置】经度 {location['lng']:.6f}，纬度 {location['lat']:.6f}（GCJ-02 高德坐标系）。"
            "可以直接用这组坐标调用 nearby_foods 查询附近餐厅。"
        )
    else:
        system += "\n\n【用户当前位置】未知。若用户询问附近餐厅，先请其在「附近」页完成定位。"
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history[-6:])
    msgs.append({"role": "user", "content": question})
    return msgs


# ---------------------------------------------------------------------------
# 对外：流式
# ---------------------------------------------------------------------------
class AnswerStream:
    """可迭代文本分片；消费结束后 ``citations`` / ``tool_calls`` / ``degraded`` 可用。"""

    def __init__(
        self,
        question: str,
        session_key: str,
        user,
        use_tools: bool = True,
        mode: str = "chat",
        location: dict | None = None,
    ):
        self.question = question
        self.session_key = session_key
        self.user = user
        self.use_tools = use_tools
        self.mode = mode
        self.location = location or {}
        self.citations: list[dict] = []
        self.tool_calls: list[dict] = []
        self.degraded = False
        self.answer = ""
        # 过程提示（首个分片）：不进 self.answer，落库的对话正文因此保持干净
        self.preamble = ""
        self.preamble_ms = 0.0
        self.latency_ms = 0.0
        self._started = time.perf_counter()

    def __iter__(self) -> Iterator[str]:
        started = time.perf_counter()
        try:
            yield from self._run()
        except Exception as exc:  # pragma: no cover - 兜底，保证 SSE 正常收尾
            logger.exception("助手生成失败")
            fallback = f"助手暂时不可用（{exc}）。你可以稍后重试，或在搜索栏按关键词找菜谱。"
            self.answer += fallback
            yield fallback
        finally:
            self.latency_ms = (time.perf_counter() - started) * 1000
            try:
                _persist(self.session_key, self.user, self.question, self.answer, self.citations, self.latency_ms)
            except Exception as exc:  # pragma: no cover
                logger.warning("对话落库失败：%s", exc)

    # -- 内部种子 ---------------------------------------------------------
    def _run(self) -> Iterator[str]:
        # 先吐过程提示：LLM 决策（含工具轮）之前，避免前端长时间空白
        hint = build_preamble(self.question, self.location)
        if hint:
            self.preamble = hint
            self.preamble_ms = round((time.perf_counter() - self._started) * 1000, 1)
            yield PreambleChunk(hint)

        hits = retrieve(self.question)
        self.citations = _citations_from_hits(hits)
        context = _hits_to_context(hits)
        history = _history_messages(self.session_key)

        if not llm_ready():
            self.degraded = True
            text = _extractive_answer(self.question, hits)
            self.answer = text
            yield text
            return

        client = get_client()
        messages = _build_messages(self.question, context, history, self.mode, self.location)

        if self.use_tools:
            messages, calls = _tool_round(messages)
            self.tool_calls = calls

        try:
            for delta in client.chat_stream(messages):
                self.answer += delta
                yield delta
        except LLMUnavailable:
            self.degraded = True
            text = _extractive_answer(self.question, hits)
            self.answer = text
            yield text


def stream_answer(
    question: str,
    session_key: str = "anonymous",
    user=None,
    use_tools: bool = True,
    mode: str = "chat",
    location: dict | None = None,
) -> AnswerStream:
    """`location` 为 GCJ-02 坐标（{"lng":..., "lat":...}），用于「附近吃什么」类问题。"""
    return AnswerStream(
        question=question,
        session_key=session_key,
        user=user,
        use_tools=use_tools,
        mode=mode,
        location=location,
    )


# ---------------------------------------------------------------------------
# 对外：同步
# ---------------------------------------------------------------------------
def answer_once(
    question: str,
    session_key: str = "sync",
    user=None,
    use_tools: bool = False,
    location: dict | None = None,
) -> dict:
    stream = stream_answer(
        question, session_key=session_key, user=user, use_tools=use_tools, location=location
    )
    text = "".join(stream)
    return {
        # 过程提示单独回传，answer 只放正文（同步接口没有「空白等待」问题）
        "answer": stream.answer or text,
        "preamble": stream.preamble,
        "citations": stream.citations,
        "tool_calls": stream.tool_calls,
        "degraded": stream.degraded,
        "latency_ms": round(stream.latency_ms, 1),
    }


def debug_retrieval(question: str, k: int = 5) -> dict:
    hits = retrieve(question, k=k)
    return {
        "question": question,
        "hits": [
            {
                "chunk_id": getattr(h, "chunk_id", ""),
                "recipe_id": getattr(h, "recipe_id", None),
                "score": round(float(getattr(h, "score", 0.0)), 4),
                "title": (getattr(h, "payload", {}) or {}).get("title", ""),
                "text": (getattr(h, "text", "") or "")[:300],
            }
            for h in hits
        ],
    }
