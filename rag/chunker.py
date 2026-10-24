"""菜谱切分模块（chunker）。

职责
    ``split_text``      通用文本切分：按段落 / 句子边界聚合，超长片段硬切并保留 overlap；
    ``chunk_recipe``    单个菜谱 → 1~N 个 :class:`Chunk`，优先保证「一步 = 一段」不被截断；
    ``iter_all_chunks`` 批量遍历已发布菜谱并产出 Chunk，供索引构建使用。

设计取舍
    - 不依赖 jieba / 任何分词库：中文按 ``。！？；…`` 与换行切句，英文按空白切段；
    - 每个 chunk 都带菜名：只含「做法」的分块也能被「菜名 query」命中，提高召回；
    - ``meta`` 携带展示字段（title / cuisine / tags / total_minutes / difficulty_label），
      上层 ``food/services/assistant.py`` 与前端卡片直接可用；
    - 步骤文本作为不可分割的最小单元参与装箱，避免半句话被拦腰切断。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator

from django.conf import settings

logger = logging.getLogger("rag.chunker")

# 句子边界（含中文全角与英文半角）
_SENTENCE_ENDS = "。！？!?；;…\n"
# 单个菜谱最多产出多少块，防脏数据（超长正文）导致索引爆炸
_MAX_CHUNKS_PER_RECIPE = 48


@dataclass
class Chunk:
    """一个可检索的文本块。

    字段与 ``docs/CONTRACTS.md`` 第 2.1 节一致，不做增删。
    """

    chunk_id: str  # 全局唯一，形如 "recipe:12:0"
    doc_id: str  # "recipe:12"
    recipe_id: int | None
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------
def _rag_conf(key: str, default: Any) -> Any:
    """安全读取 ``settings.RAG[key]``（settings 未配置时返回默认值）。"""
    try:
        conf = getattr(settings, "RAG", None)
    except Exception:  # pragma: no cover - settings 未配置
        return default
    if isinstance(conf, dict):
        value = conf.get(key, default)
        return default if value is None else value
    return default


def _collect(relation: Any) -> list[Any]:
    """把 Django related manager 或普通 list/tuple 统一成 list。

    这样 ``chunk_recipe`` 既能吃真实 ``Recipe``（``tags.all()``），
    也能吃测试里的轻量替身对象（直接给 list），便于单测。
    """
    if relation is None:
        return []
    all_fn = getattr(relation, "all", None)
    if callable(all_fn):
        try:
            return list(all_fn())
        except Exception as exc:  # pragma: no cover - prefetch 缺失时
            logger.debug("读取关联对象失败：%s", exc)
            return []
    try:
        return list(relation)
    except TypeError:
        return []


# ---------------------------------------------------------------------------
# 通用文本切分
# ---------------------------------------------------------------------------
def _split_sentences(paragraph: str) -> list[str]:
    """按中文/英文句末标点把一段文本切成句子（保留标点）。"""
    sentences: list[str] = []
    buf: list[str] = []
    for ch in paragraph:
        buf.append(ch)
        if ch in _SENTENCE_ENDS:
            piece = "".join(buf).strip()
            if piece:
                sentences.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return sentences


def split_text(text: str, chunk_size: int = 420, overlap: int = 80) -> list[str]:
    """把长文本切成若干块，块长不超过 ``chunk_size``，相邻块尽量保留 ``overlap`` 字重叠。

    :param text: 原始文本（可以是 ``search_doc`` 这种多段落文本）
    :param chunk_size: 单块字符上限，小于 20 时按 20 处理
    :param overlap: 相邻块重叠字符数，自动裁剪到 ``chunk_size // 2`` 以内
    :return: 切分结果列表；空文本 / 纯空白返回 ``[]``
    """
    if text is None:
        return []
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []

    size = max(int(chunk_size or 420), 20)
    ov = max(0, min(int(overlap or 0), size // 2))
    stripped = normalized.strip()
    if len(stripped) <= size:
        return [stripped]

    # 1) 先按段落 → 句子拆成「尽量小但不被再切」的片段
    pieces: list[str] = []
    for paragraph in stripped.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        para_pieces = _split_sentences(paragraph)
        if not para_pieces:
            continue
        # 段落末尾补换行，保证拼接后仍保留段落边界
        para_pieces[-1] = para_pieces[-1] + "\n"
        pieces.extend(para_pieces)

    # 2) 贪心装箱：片段拼到当前块直到超出上限，再开新块并带上一块的尾巴
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        # 极端情况：单句超长 → 硬切（保留 overlap），循环保证推进
        while len(piece) > size:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.append(piece[:size].strip())
            piece = piece[size - ov :] if ov else piece[size:]
        if not piece:
            continue
        if not buf:
            buf = piece
        elif len(buf) + len(piece) <= size:
            buf += piece
        else:
            chunks.append(buf.strip())
            tail = buf[-ov:] if ov else ""
            # 重叠尾巴塞得下就带上，塞不下宁可丢重叠也不截断内容
            buf = tail + piece if len(tail) + len(piece) <= size else piece

    if buf.strip():
        chunks.append(buf.strip())
    return [chunk for chunk in chunks if chunk]


# ---------------------------------------------------------------------------
# 菜谱切分
# ---------------------------------------------------------------------------
def _recipe_header_units(recipe: Any, meta: dict[str, Any]) -> list[str]:
    """构造菜谱的头部单元（菜名 / 元信息行 / 简介句子）。"""
    units: list[str] = [meta["title"]]

    info_bits: list[str] = []
    if meta["cuisine"]:
        info_bits.append(f"菜系：{meta['cuisine']}")
    if meta["tags"]:
        info_bits.append("标签：" + "、".join(meta["tags"]))
    info_bits.append(f"难度：{meta['difficulty_label'] or '普通'}")
    info_bits.append(f"耗时：{meta['total_minutes']}分钟")
    if meta["servings"]:
        info_bits.append(f"份量：{meta['servings']}人份")
    if meta["calories"]:
        info_bits.append(f"热量：约{int(meta['calories'])}千卡")
    units.append("；".join(info_bits))

    summary = (getattr(recipe, "summary", "") or "").strip()
    if summary:
        sentences = _split_sentences(summary)
        units.extend(sentences or [summary])
    return units


def _ingredient_unit(recipe: Any) -> str:
    """把食材列表拼成一行「食材：A 100克、B 2个」。"""
    lines: list[str] = []
    for item in _collect(getattr(recipe, "ingredients", None)):
        name = getattr(item, "name_text", "") or ""
        if not name:
            ingredient = getattr(item, "ingredient", None)
            name = getattr(ingredient, "name", "") or ""
        if not name:
            continue
        amount = " ".join(x for x in [getattr(item, "amount", ""), getattr(item, "unit", "")] if x)
        lines.append(f"{name}{(' ' + amount) if amount else ''}")
    return "食材：" + "、".join(lines) if lines else ""


def _step_units(recipe: Any) -> list[str]:
    """步骤行；每一步都是不可分割的单元。"""
    units: list[str] = []
    for step in _collect(getattr(recipe, "steps", None)):
        text = (getattr(step, "text", "") or "").strip()
        if not text:
            continue
        index = getattr(step, "index", None)
        units.append(f"{index}. {text}" if index else text)
    return units


def chunk_recipe(recipe: Any, chunk_size: int | None = None, overlap: int | None = None) -> list[Chunk]:
    """把一个菜谱拆成 1~N 个 :class:`Chunk`。

    切分策略：先构造「头部（菜名 + 元信息 + 简介）」「食材行」「每一步」「小贴士」这些
    不可再分的单元，再贪心装箱到 ``chunk_size``；单个单元超长时才用 :func:`split_text`
    硬切（并保留 overlap）。每个块的正文都会带上菜名，保证步骤块也能被菜名 query 命中。

    :param recipe: ``food.models.Recipe`` 实例或等价替身对象
    :param chunk_size: 覆盖 ``settings.RAG["CHUNK_SIZE"]``
    :param overlap: 覆盖 ``settings.RAG["CHUNK_OVERLAP"]``
    :return: Chunk 列表（菜谱无任何内容时返回空列表）
    """
    recipe_pk = getattr(recipe, "pk", None)
    if recipe_pk is None:
        recipe_pk = getattr(recipe, "id", None)
    recipe_id = recipe_pk if isinstance(recipe_pk, int) else None
    doc_id = f"recipe:{recipe_pk if recipe_pk is not None else 0}"

    title = (getattr(recipe, "title", "") or "").strip() or "未命名菜谱"

    size = int(chunk_size or _rag_conf("CHUNK_SIZE", 420))
    size = max(size, 80)
    ov_conf = _rag_conf("CHUNK_OVERLAP", 80)
    ov = int(overlap if overlap is not None else ov_conf)
    ov = max(0, min(ov, size // 2))

    cuisine = ""
    cuisine_obj = getattr(recipe, "cuisine", None)
    if cuisine_obj is not None:
        cuisine = (getattr(cuisine_obj, "name", "") or "").strip()

    tag_names: list[str] = []
    for tag in _collect(getattr(recipe, "tags", None)):
        name = getattr(tag, "name", "") or (tag if isinstance(tag, str) else "")
        if name:
            tag_names.append(str(name))

    try:
        total_minutes = int(getattr(recipe, "total_minutes", 0) or 0)
    except Exception:  # pragma: no cover - 替身对象属性异常
        total_minutes = 0
    try:
        servings = int(getattr(recipe, "servings", 0) or 0)
    except Exception:  # pragma: no cover
        servings = 0
    try:
        calories = round(float(getattr(recipe, "calories", 0) or 0), 1)
    except Exception:  # pragma: no cover
        calories = 0.0

    meta: dict[str, Any] = {
        "title": title,
        "cuisine": cuisine,
        "tags": tag_names[:8],
        "total_minutes": total_minutes,
        "difficulty_label": str(getattr(recipe, "difficulty_label", "") or ""),
        "recipe_id": recipe_id,
        "slug": str(getattr(recipe, "slug", "") or ""),
        "servings": servings,
        "calories": calories,
        "source": str(getattr(recipe, "source", "") or ""),
        "status": str(getattr(recipe, "status", "") or ""),
    }

    units = _recipe_header_units(recipe, meta)
    ingredient_line = _ingredient_unit(recipe)
    if ingredient_line:
        units.append(ingredient_line)
    units.extend(_step_units(recipe))
    tips = (getattr(recipe, "tips", "") or "").strip()
    if tips:
        units.append("小贴士：" + tips)

    if not units:
        return []

    # 预留「菜名 + 换行」的预算，保证最终块长仍受 chunk_size 约束
    budget = max(size - len(title) - 1, 120)

    bodies: list[str] = []
    buf = ""
    for unit in units:
        if len(unit) > budget:
            if buf:
                bodies.append(buf)
                buf = ""
            bodies.extend(split_text(unit, budget, ov))
            continue
        if not buf:
            buf = unit
        elif len(buf) + 1 + len(unit) <= budget:
            buf = f"{buf}\n{unit}"
        else:
            bodies.append(buf)
            buf = unit
    if buf:
        bodies.append(buf)

    chunks: list[Chunk] = []
    for body in bodies:
        body = body.strip()
        if not body:
            continue
        text = body if title in body else f"{title}\n{body}"
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}:{len(chunks)}",
                doc_id=doc_id,
                recipe_id=recipe_id,
                text=text,
                meta={**meta, "chunk_index": len(chunks)},
            )
        )
        if len(chunks) >= _MAX_CHUNKS_PER_RECIPE:
            logger.warning("菜谱 %s 切分超过 %s 块，已截断", doc_id, _MAX_CHUNKS_PER_RECIPE)
            break
    return chunks


def iter_all_chunks(batch_size: int = 200) -> Iterator[Chunk]:
    """遍历全部已发布菜谱并逐个产出 Chunk。

    用 ``.iterator(chunk_size=...)`` 流式读取，配合 ``prefetch_related`` 避免 N+1 查询，
    单条菜谱切分异常只记 warning，不影响整体索引构建。
    """
    from food.models import Recipe

    batch = max(int(batch_size or 200), 1)
    queryset = (
        Recipe.objects.published()
        .prefetch_related("tags", "ingredients__ingredient", "steps")
        .order_by("pk")
    )
    for recipe in queryset.iterator(chunk_size=batch):
        try:
            yield from chunk_recipe(recipe)
        except Exception as exc:  # pragma: no cover - 脏数据兜底
            logger.warning("菜谱 %s 切分失败，已跳过：%s", getattr(recipe, "pk", "?"), exc)
