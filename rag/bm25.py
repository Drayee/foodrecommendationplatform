"""BM25 检索模块（自实现，不依赖 rank_bm25 / jieba）。

职责
    :class:`BM25Index` 提供 ``add_many`` / ``search`` / ``to_dict`` / ``from_dict``，
    统计 df / idf / avgdl，参数默认 ``k1=1.5`` ``b=0.75``（见契约 2.3）。

分词策略（无 jieba 依赖）
    中文连续片段 → 单字 + 双字（char bigram）；英文 / 数字 → 词 token；
    单字兜底是为了让「辣」「鱼」这类单字查询仍能命中（否则 bigram 分词无 token 可用）。

设计取舍
    - 倒排表用 ``{token: [(doc_index, tf), ...]}``，查询时只扫命中文档，复杂度与命中量相关；
    - idf 用经典 BM25 平滑式 ``log(1 + (N - df + 0.5) / (df + 0.5))``，df 越大权重越低；
    - ``to_dict`` 直接输出 JSON 友好的结构（元组转列表），``from_dict`` 重建倒排表，
      避免持久化后再分词（既快又保证与建索引时完全一致）。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from typing import Any, Iterable, Sequence

logger = logging.getLogger("rag.bm25")

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+(?:[._\-][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """BM25 用的分词：中文单字 + char bigram，英文/数字按词。

    >>> tokenize("番茄牛腩 beef stew")[:6]
    ['番', '茄', '牛', '腩', '番茄', '茄牛']
    """
    if not text:
        return []
    lowered = str(text).lower()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    tokens.extend(_ASCII_WORD.findall(lowered))
    return tokens


class BM25Index:
    """轻量 BM25 倒排索引。

    >>> index = BM25Index()
    >>> index.add_many([("a", "番茄牛腩炖土豆"), ("b", "清蒸鲈鱼")])
    >>> index.search("番茄牛腩", k=1)[0][0]
    'a'
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = float(k1)
        self.b = float(b)
        self.doc_ids: list[str] = []
        self._lens: list[int] = []
        self._postings: dict[str, list[list[int]]] = {}
        self._avgdl: float = 0.0

    # -- 构建 -------------------------------------------------------------
    def add_many(self, items: Iterable[tuple[str, str]]) -> None:
        """批量加入 ``(chunk_id, text)``。重复的 chunk_id 不做去重（调用方负责）。"""
        for chunk_id, text in items:
            tokens = tokenize(text or "")
            doc_index = len(self.doc_ids)
            self.doc_ids.append(str(chunk_id))
            self._lens.append(len(tokens))
            for token, tf in Counter(tokens).items():
                self._postings.setdefault(token, []).append([doc_index, tf])
        self._recompute_avgdl()

    def _recompute_avgdl(self) -> None:
        """重算平均文档长度（用 token 数近似词数）。"""
        total = sum(self._lens)
        self._avgdl = (total / len(self._lens)) if self._lens else 0.0

    # -- 查询 -------------------------------------------------------------
    def _idf(self, token: str) -> float:
        """单个 token 的 idf 权重。"""
        n = len(self.doc_ids)
        if n == 0:
            return 0.0
        df = len(self._postings.get(token, ()))
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        """返回 ``[(chunk_id, score)]``，按分数降序、只保留分数 > 0 的文档。"""
        n = len(self.doc_ids)
        if n == 0:
            return []
        tokens = tokenize(query or "")
        if not tokens:
            return []
        try:
            k = max(int(k), 1)
        except (TypeError, ValueError):  # pragma: no cover
            k = 10

        avgdl = self._avgdl or 1.0
        scores: dict[int, float] = {}
        for token in set(tokens):
            postings = self._postings.get(token)
            if not postings:
                continue
            idf = self._idf(token)
            if idf <= 0:
                continue
            for doc_index, tf in postings:
                doc_len = self._lens[doc_index] or 1
                denominator = tf + self.k1 * (1.0 - self.b + self.b * doc_len / avgdl)
                if denominator <= 0:  # pragma: no cover - 理论不可达
                    continue
                scores[doc_index] = scores.get(doc_index, 0.0) + idf * tf * (self.k1 + 1.0) / denominator

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
        return [(self.doc_ids[idx], round(score, 6)) for idx, score in ranked]

    # -- 持久化 -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 友好结构（postings 的元组已转 list）。"""
        return {
            "k1": self.k1,
            "b": self.b,
            "doc_ids": list(self.doc_ids),
            "lens": list(self._lens),
            "avgdl": self._avgdl,
            "postings": {token: postings for token, postings in self._postings.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> "BM25Index":
        """从 :meth:`to_dict` 结果（或 JSON 字符串）重建索引。"""
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("bm25 数据格式非法")
        index = cls(k1=float(data.get("k1", 1.5)), b=float(data.get("b", 0.75)))
        doc_ids: Sequence[Any] = data.get("doc_ids") or []
        lens: Sequence[Any] = data.get("lens") or []
        postings: dict[str, Any] = data.get("postings") or {}
        index.doc_ids = [str(x) for x in doc_ids]
        index._lens = [int(x) for x in lens]
        if len(index._lens) != len(index.doc_ids):
            raise ValueError("bm25 数据不一致：doc_ids 与 lens 长度不同")
        index._postings = {
            str(token): [[int(row[0]), int(row[1])] for row in rows]
            for token, rows in postings.items()
        }
        index._avgdl = float(data.get("avgdl") or 0.0)
        if index._avgdl <= 0:
            index._recompute_avgdl()
        return index

    # -- 杂项 -------------------------------------------------------------
    def __len__(self) -> int:
        """已收录的文档数。"""
        return len(self.doc_ids)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<BM25Index docs={len(self)} avgdl={self._avgdl:.1f} k1={self.k1} b={self.b}>"
