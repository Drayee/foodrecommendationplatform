"""检索门面模块（retriever）—— 向量检索 ⊕ BM25 ⊕ 标题 的 RRF 混合召回。

职责
    :class:`Retriever`  对外只暴露 4 个方法（契约 2.5）：
        ``search(query, k=5, filters=None) -> list[Hit]``
        ``build_index(force=False, progress=None) -> dict``
        ``stats() -> dict``（必须含 ``"ready"``）
        ``invalidate() -> None``
    :func:`get_retriever`  进程内单例；索引文件 mtime 变化时自动重新加载，
                           多进程 / 开发热重载场景下不会读到过期索引。

检索策略
    - 向量路：``HashingTfidfEmbedder`` 编码 query → 余弦 top-N；
    - 关键词路：``BM25Index`` 打分 top-N；
    - 标题路（契约外的小扩展，可关）：按「query 与菜名的词元亲和度」排序，
      解决 RRF 只看名次、无法分辨「用户就是要点这道菜」的问题
      （例：查「番茄炒蛋」时不能让同含「番茄」的「番茄牛腩」反超）；
    - 融合：RRF，``score = Σ w_r / (60 + rank_r)``，rank 从 1 开始；
      向量路权重 1.0，BM25 路取 ``settings.RAG["HYBRID_BM25_WEIGHT"]``（默认 0.5），
      标题路取 ``settings.RAG["HYBRID_TITLE_WEIGHT"]``（缺省 1.0，设 0 即关闭，
      关闭后行为与契约的「两路 RRF」完全一致）；
    - 任一路为空时自动退化；全部为空返回 ``[]``；
    - **任何异常都不外抛**：记 warning 日志 + 返回 ``[]``，保证助手上层不 500。

索引构建
    批量流式读取 ``Recipe.objects.published().prefetch_related(...)``（``.iterator()``），
    逐菜谱切分 → 编码 → 汇总成块级索引；同时把过期的 ``recipe.search_doc`` 刷新
    （``refresh_search_doc(save=True)``，只更新 search_doc / content_version / updated_at），
    并把「菜谱级向量」（块向量均值再归一化）批量写入 ``RecipeEmbedding``，供推荐 / 相似菜模块复用。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from django.conf import settings

from rag.bm25 import tokenize as bm25_tokenize
from rag.chunker import chunk_recipe
from rag.embedder import BaseEmbedder, get_embedder
from rag.store import VectorStore

logger = logging.getLogger("rag.retriever")

# RRF 平滑常数（契约固定 60）
RRF_K = 60
# 单路候选数下限（k 的倍数），保证融合时有足够候选
_CANDIDATE_FACTOR = 5
_MIN_CANDIDATES = 20
# 标题路默认权重（settings 里没配时使用）
_DEFAULT_TITLE_WEIGHT = 1.0


def _rag_conf(key: str, default: Any) -> Any:
    """安全读取 ``settings.RAG[key]``。"""
    try:
        conf = getattr(settings, "RAG", None)
    except Exception:  # pragma: no cover - settings 未配置
        return default
    if isinstance(conf, dict):
        value = conf.get(key, default)
        return default if value is None else value
    return default


def _weight_conf(key: str, default: float, upper: float = 5.0) -> float:
    """读取融合权重（settings 里常是字符串），统一转 float 并夹到 [0, upper]。"""
    raw = _rag_conf(key, default)
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        weight = float(default)
    if weight < 0:
        weight = 0.0
    if weight > upper:
        weight = upper
    return weight


def _bm25_weight() -> float:
    """BM25 融合权重。"""
    return _weight_conf("HYBRID_BM25_WEIGHT", 0.5)


def _title_weight() -> float:
    """标题路融合权重；设为 0 即退回契约的「纯两路 RRF」。"""
    return _weight_conf("HYBRID_TITLE_WEIGHT", _DEFAULT_TITLE_WEIGHT)


def _affinity_tokens(text: str) -> set[str]:
    """标题亲和度用词元：复用 BM25 的切分，只保留长度 ≥ 2 的 n-gram / 英文词。"""
    return {token for token in bm25_tokenize(text or "") if len(token) > 1}


def _title_affinity(query: str, title: str) -> float:
    """query 与菜名的词元亲和度（0~1）。

    - 互为子串（用户直接说了菜名）→ 1.0；
    - 否则用「query 词元被标题覆盖的比例」，避免长标题占便宜。

    >>> _title_affinity("番茄炒蛋怎么做", "番茄炒蛋")
    1.0
    >>> round(_title_affinity("番茄炒蛋怎么做", "番茄牛腩"), 3)
    0.143
    """
    if not query or not title:
        return 0.0
    if title in query or query in title:
        return 1.0
    query_tokens = _affinity_tokens(query)
    title_tokens = _affinity_tokens(title)
    if not query_tokens or not title_tokens:
        return 0.0
    return len(query_tokens & title_tokens) / len(query_tokens)


@dataclass
class Hit:
    """一条检索结果。

    字段与 ``docs/CONTRACTS.md`` 2.5 完全一致；``payload`` 里带展示字段与 ``text``。
    """

    chunk_id: str
    recipe_id: int | None
    score: float
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


class Retriever:
    """混合检索器：向量 top-N 与 BM25 top-N 用 RRF 融合。"""

    def __init__(
        self,
        index_dir: str | Path | None = None,
        embedder: BaseEmbedder | None = None,
    ) -> None:
        """``index_dir`` 缺省从 settings 读；``embedder`` 缺省用全局单例。"""
        resolved = index_dir if index_dir is not None else _rag_conf("INDEX_DIR", "data/index")
        self._index_dir = Path(str(resolved))
        # 显式传入目录时「钉住」，不再跟随 settings 变化（便于测试与多索引并存）
        self._dir_pinned = index_dir is not None
        self.store = VectorStore(self._index_dir)
        self._embedder = embedder
        self._loaded = False
        self._mtime = 0.0
        self._lock = threading.RLock()

    # -- 内部工具 ---------------------------------------------------------
    def _get_embedder(self) -> BaseEmbedder:
        """取编码器（构造时显式注入的优先）。"""
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    def _sync_dir(self) -> None:
        """让内存 store 与 ``settings.RAG["INDEX_DIR"]`` 保持一致（测试/换目录时用）。"""
        if self._dir_pinned:
            return
        configured = Path(str(_rag_conf("INDEX_DIR", self._index_dir)))
        if configured != self._index_dir:
            self._index_dir = configured
            self.store = VectorStore(configured)
            self._loaded = False
            self._mtime = 0.0

    def _ensure_loaded(self) -> bool:
        """按需加载索引；文件 mtime 变化（其他进程重建过）时自动重新加载。"""
        with self._lock:
            mtime = self.store.fingerprint()
            if not self._loaded or mtime != self._mtime:
                ok = self.store.load()
                self._loaded = bool(ok)
                self._mtime = self.store.fingerprint() if ok else 0.0
            return self._loaded

    @staticmethod
    def _notify(progress: Callable[..., None] | None, done: int, total: int) -> None:
        """安全调用进度回调（回调异常不影响索引构建）。"""
        if progress is None:
            return
        try:
            progress(done, total)
        except TypeError:  # 只接受一个参数的回调
            try:
                progress(done)
            except Exception as exc:  # pragma: no cover
                logger.debug("progress 回调失败：%s", exc)
        except Exception as exc:  # pragma: no cover
            logger.debug("progress 回调失败：%s", exc)

    @staticmethod
    def _dataset_signature(embedder: BaseEmbedder) -> str:
        """数据集 + 模型指纹：计数与 content_version 之和一变就说明需要重建。"""
        from django.db.models import Count, Sum

        from food.models import Recipe

        aggregate = Recipe.objects.published().aggregate(count=Count("id"), version_sum=Sum("content_version"))
        count = int(aggregate.get("count") or 0)
        version_sum = int(aggregate.get("version_sum") or 0)
        return f"v1|{getattr(embedder, 'name', '')}|{int(getattr(embedder, 'dim', 0) or 0)}|{count}|{version_sum}"

    @staticmethod
    def _refresh_search_doc(recipe: Any) -> None:
        """``search_doc`` 过期时刷新（只写 search_doc / content_version / updated_at）。"""
        try:
            fresh = recipe.build_search_doc()
            if (recipe.search_doc or "") != fresh:
                recipe.refresh_search_doc(save=True)
        except Exception as exc:  # pragma: no cover - 单条脏数据
            logger.warning("刷新 search_doc 失败 recipe=%s：%s", getattr(recipe, "pk", "?"), exc)

    @staticmethod
    def _passes(payload: dict[str, Any], filters: dict[str, Any] | None) -> bool:
        """payload 过滤：支持等值、集合交集（tags）与 list 值包含。"""
        if not filters:
            return True
        for key, expected in filters.items():
            if expected is None or expected == "" or expected == [] or expected == ():
                continue
            value = payload.get(key)
            if isinstance(expected, (list, tuple, set, frozenset)):
                got = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
                if not set(expected) & set(got):
                    return False
            elif isinstance(value, (list, tuple, set, frozenset)):
                if expected not in value:
                    return False
            elif str(value) != str(expected):
                return False
        return True

    def _fuse(self, channels: Iterable[tuple[Iterable[tuple[str, float]], float]]) -> list[tuple[str, float]]:
        """RRF 融合多路召回：``score = Σ w / (RRF_K + rank)``，rank 从 1 开始。

        :param channels: ``[(该路命中列表, 权重), ...]``；权重 ≤ 0 的路直接跳过
        """
        scores: dict[str, float] = {}
        for hits, weight in channels:
            if weight <= 0:
                continue
            for rank, (chunk_id, _score) in enumerate(hits, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

    def _title_hits(self, query: str, limit: int) -> list[tuple[str, float]]:
        """标题路：按 query 与菜名的词元亲和度排序（同一菜谱只取一个代表块）。"""
        payloads = getattr(self.store, "payloads", None) or {}
        if not isinstance(payloads, dict):
            return []
        ranked: list[tuple[str, float]] = []
        seen: set[tuple[Any, str]] = set()
        for chunk_id, payload in payloads.items():
            if not isinstance(payload, dict):
                continue
            title = str(payload.get("title") or "")
            affinity = _title_affinity(query, title)
            if affinity <= 0:
                continue
            # 同一菜谱的多个块共享标题，只让首个块（chunk_index 最小）代表它
            key = (payload.get("recipe_id"), title)
            if key in seen:
                continue
            seen.add(key)
            ranked.append((str(chunk_id), affinity))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked[: max(int(limit), 1)]

    # -- 检索 -------------------------------------------------------------
    def search(self, query: str, k: int = 5, filters: dict | None = None) -> list[Hit]:
        """混合检索，返回按融合分数降序的 :class:`Hit` 列表。

        :param query: 用户查询（空字符串直接返回 ``[]``）
        :param k: 返回条数
        :param filters: payload 过滤，如 ``{"cuisine": "川菜"}`` / ``{"tags": ["素菜"]}``
        :return: ``Hit`` 列表；索引未就绪或发生异常时返回 ``[]``（绝不抛异常）
        """
        query = (query or "").strip()
        if not query:
            return []
        try:
            k = max(int(k or 0), 1)
        except (TypeError, ValueError):
            k = 5

        started = time.perf_counter()
        try:
            self._sync_dir()
            if not self._ensure_loaded() or len(self.store) == 0:
                logger.info("检索跳过：索引未就绪（query=%r）", query[:40])
                return []

            candidate_k = max(k * _CANDIDATE_FACTOR, _MIN_CANDIDATES)
            candidate_k = min(candidate_k, len(self.store))

            embedder = self._get_embedder()
            query_vector = embedder.encode_query(query)
            vector_hits = self.store.search(query_vector, k=candidate_k)
            try:
                bm25_hits = self.store.bm25.search(query, k=candidate_k)
            except Exception as exc:  # BM25 单路失败不影响向量路
                logger.warning("BM25 检索失败，退化为纯向量检索：%s", exc)
                bm25_hits = []
            title_weight = _title_weight()
            try:
                title_hits = self._title_hits(query, candidate_k) if title_weight > 0 else []
            except Exception as exc:  # 标题路失败不影响两路
                logger.warning("标题路检索失败，忽略该路：%s", exc)
                title_hits = []

            fused = self._fuse(
                [(vector_hits, 1.0), (bm25_hits, _bm25_weight()), (title_hits, title_weight)]
            )
            hits: list[Hit] = []
            for chunk_id, score in fused:
                payload = self.store.get_payload(chunk_id)
                if not self._passes(payload, filters):
                    continue
                recipe_id = payload.get("recipe_id")
                hits.append(
                    Hit(
                        chunk_id=chunk_id,
                        recipe_id=recipe_id if isinstance(recipe_id, int) else None,
                        score=round(float(score), 6),
                        text=str(payload.get("text", "")),
                        payload=payload,
                    )
                )
                if len(hits) >= k:
                    break

            logger.debug(
                "检索完成 query=%r 向量=%s bm25=%s 标题=%s 返回=%s 耗时=%.1fms",
                query[:40], len(vector_hits), len(bm25_hits), len(title_hits), len(hits),
                (time.perf_counter() - started) * 1000,
            )
            return hits
        except Exception as exc:
            logger.warning("检索失败，降级为空结果（query=%r）：%s", query[:40], exc)
            return []

    # -- 建索引 -----------------------------------------------------------
    def build_index(self, force: bool = False, progress: Callable[..., None] | None = None) -> dict:
        """重建全量索引。

        流程：流式读取已发布菜谱 → 刷新过期 ``search_doc`` → 切块 → 编码 → 汇总
        → 落盘向量库 / BM25 → 批量写 ``RecipeEmbedding``。

        :param force: ``False`` 时若数据集指纹与清单一致，直接复用现有索引
        :param progress: 可选回调 ``progress(done, total)``
        :return: ``{"chunks", "recipes", "dim", "elapsed_ms", "model"}``
        """
        from food.models import Recipe, RecipeEmbedding

        started = time.perf_counter()
        embedder = self._get_embedder()
        # 维度先留空：以「第一次成功编码的实际宽度」为准。
        # 这样 Ollama 中途熔断回 hashing（768 → 1024）时，不会把两种维度的行混进同一个矩阵。
        dim = 0
        model = str(getattr(embedder, "name", "hashing-tfidf-zh"))
        self._sync_dir()

        signature = self._dataset_signature(embedder)
        if not force and self._ensure_loaded() and len(self.store) > 0:
            if self.store.manifest.get("signature") == signature:
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info("索引指纹未变，复用现有索引（%s 块）", len(self.store))
                return {
                    "chunks": int(self.store.manifest.get("chunks", len(self.store)) or len(self.store)),
                    "recipes": int(self.store.manifest.get("recipes", 0) or 0),
                    "dim": int(self.store.manifest.get("dim", 0) or 0),
                    "elapsed_ms": round(elapsed_ms, 1),
                    "model": str(self.store.manifest.get("model") or model),
                }

        total = int(Recipe.objects.published().count() or 0)
        batch_size = max(int(_rag_conf("INDEX_BATCH_SIZE", 200) or 200), 1)

        ids: list[str] = []
        vectors: list[np.ndarray] = []
        payloads: list[dict[str, Any]] = []
        recipe_vectors: dict[int, np.ndarray] = {}
        recipe_versions: dict[int, int] = {}
        done = 0

        queryset = (
            Recipe.objects.published()
            .prefetch_related("tags", "ingredients__ingredient", "steps")
            .order_by("pk")
        )
        for recipe in queryset.iterator(chunk_size=batch_size):
            done += 1
            try:
                self._refresh_search_doc(recipe)
                chunks = chunk_recipe(recipe)
                if chunks:
                    matrix = np.asarray(embedder.encode([chunk.text for chunk in chunks]), dtype=np.float32)
                    if matrix.ndim != 2 or matrix.shape[0] != len(chunks) or matrix.shape[1] == 0:
                        logger.warning("菜谱 %s 向量形状异常 %s，已跳过", recipe.pk, matrix.shape)
                        self._notify(progress, done, total)
                        continue
                    row_dim = int(matrix.shape[1])
                    if dim and row_dim != dim:
                        logger.warning(
                            "向量维度从 %s 变为 %s（后端降级？），跳过菜谱 %s —— 请用 --force 重建索引",
                            dim, row_dim, recipe.pk,
                        )
                        self._notify(progress, done, total)
                        continue
                    dim = row_dim
                    for row, chunk in enumerate(chunks):
                        ids.append(chunk.chunk_id)
                        vectors.append(matrix[row])
                        payloads.append(
                            {
                                **chunk.meta,
                                "text": chunk.text,
                                "chunk_id": chunk.chunk_id,
                                "doc_id": chunk.doc_id,
                                "model": model,
                            }
                        )
                    # 菜谱级向量：块向量均值再归一化，供 recommend / 相似菜复用
                    mean = matrix.mean(axis=0)
                    norm = float(np.linalg.norm(mean))
                    recipe_vectors[int(recipe.pk)] = mean / norm if norm > 0 else mean
                    recipe_versions[int(recipe.pk)] = int(recipe.content_version or 0)
            except Exception as exc:  # 单条菜谱失败不影响整体
                logger.warning("菜谱 %s 索引失败，已跳过：%s", getattr(recipe, "pk", "?"), exc)
            self._notify(progress, done, total)

        # 编码过程中后端可能熔断（ollama → hashing），最终生效的 name/dim 以此刻为准
        model = str(getattr(embedder, "name", model))
        if not dim:
            try:
                dim = int(getattr(embedder, "dim", 0) or 0)
            except (TypeError, ValueError):  # pragma: no cover
                dim = 0
        for payload in payloads:
            payload["model"] = model

        matrix_all = (
            np.vstack(vectors).astype(np.float32) if vectors else np.zeros((0, dim), dtype=np.float32)
        )
        self.store.build(ids, matrix_all, payloads)
        self.store.model = model
        # 刷新后 content_version 可能变化，落盘前重算指纹
        fresh_signature = self._dataset_signature(embedder)
        self.store.manifest_extra = {
            "signature": fresh_signature,
            "chunks": len(ids),
            "recipes": len(recipe_vectors),
            "dim": dim,
            "model": model,
        }
        self.store.model = model
        self.store.save()
        self._loaded = True
        self._mtime = self.store.fingerprint()

        saved = self._save_recipe_embeddings(recipe_vectors, recipe_versions, model, dim)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "索引构建完成：%s 块 / %s 菜谱 / %s 维 / %.1fms（RecipeEmbedding 写入 %s 条）",
            len(ids), len(recipe_vectors), dim, elapsed_ms, saved,
        )
        return {
            "chunks": len(ids),
            "recipes": len(recipe_vectors),
            "dim": dim,
            "elapsed_ms": round(elapsed_ms, 1),
            "model": model,
        }

    @staticmethod
    def _save_recipe_embeddings(
        recipe_vectors: dict[int, np.ndarray],
        recipe_versions: dict[int, int],
        model: str,
        dim: int,
    ) -> int:
        """批量写 ``RecipeEmbedding``（存在则 bulk_update，缺失则 bulk_create），返回写入条数。"""
        from food.models import RecipeEmbedding

        if not recipe_vectors:
            return 0
        recipe_ids = list(recipe_vectors.keys())
        existing = {
            row.recipe_id: row
            for row in RecipeEmbedding.objects.filter(recipe_id__in=recipe_ids)
        }
        to_create: list[RecipeEmbedding] = []
        to_update: list[RecipeEmbedding] = []
        for recipe_id, vector in recipe_vectors.items():
            blob = np.asarray(vector, dtype=np.float32).tobytes()
            version = int(recipe_versions.get(recipe_id, 0))
            row = existing.get(recipe_id)
            if row is None:
                to_create.append(
                    RecipeEmbedding(
                        recipe_id=recipe_id,
                        model_name=model,
                        dim=dim,
                        vector=blob,
                        content_version=version,
                    )
                )
                continue
            changed = (
                row.model_name != model
                or int(row.dim or 0) != dim
                or int(row.content_version or 0) != version
                or bytes(row.vector or b"") != blob
            )
            if changed:
                row.model_name = model
                row.dim = dim
                row.vector = blob
                row.content_version = version
                to_update.append(row)
        if to_create:
            RecipeEmbedding.objects.bulk_create(to_create, batch_size=200)
        if to_update:
            RecipeEmbedding.objects.bulk_update(
                to_update, ["model_name", "dim", "vector", "content_version"], batch_size=200
            )
        return len(to_create) + len(to_update)

    # -- 状态 -------------------------------------------------------------
    def stats(self) -> dict:
        """返回索引状态，必含 ``"ready": bool`` / ``size`` / ``dim`` / ``model`` / ``updated_at``。"""
        configured_model = str(_rag_conf("EMBED_MODEL", "hashing-tfidf-zh") or "hashing-tfidf-zh")
        try:
            configured_dim = int(_rag_conf("EMBED_DIM", 1024) or 1024)
        except (TypeError, ValueError):  # pragma: no cover
            configured_dim = 1024
        base = {
            "ready": False,
            "size": 0,
            "dim": configured_dim,
            "model": configured_model,
            "updated_at": "",
            "index_dir": "",
            "bm25_docs": 0,
        }
        try:
            self._sync_dir()
            loaded = self._ensure_loaded()
            store_stats = self.store.stats()
            size = int(store_stats.get("size", 0) or 0)
            base.update(
                {
                    "ready": bool(loaded and size > 0),
                    "size": size,
                    "dim": int(store_stats.get("dim", configured_dim) or configured_dim),
                    "model": str(store_stats.get("model") or configured_model),
                    "updated_at": str(store_stats.get("updated_at") or ""),
                    "index_dir": str(store_stats.get("index_dir") or self._index_dir),
                    "bm25_docs": int(store_stats.get("bm25_docs", 0) or 0),
                }
            )
        except Exception as exc:  # pragma: no cover - 极端情况
            logger.warning("读取索引状态失败：%s", exc)
        return base

    def invalidate(self) -> None:
        """丢弃内存索引（下次访问自动从磁盘重新加载，不删除文件）。"""
        with self._lock:
            self._loaded = False
            self._mtime = 0.0
            self.store = VectorStore(self._index_dir)
        logger.info("索引缓存已失效：%s", self._index_dir)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Retriever dir={self._index_dir} loaded={self._loaded} size={len(self.store)}>"


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------
_SINGLETON_LOCK = threading.Lock()
_RETRIEVER: Retriever | None = None


def get_retriever() -> Retriever:
    """返回进程内单例：文件 mtime 变化时 ``search`` / ``stats`` 自动重新加载索引。"""
    global _RETRIEVER
    with _SINGLETON_LOCK:
        if _RETRIEVER is None:
            _RETRIEVER = Retriever()
        return _RETRIEVER


def reset_retriever() -> None:
    """丢弃单例（测试或运行期切换 ``INDEX_DIR`` 后使用）。"""
    global _RETRIEVER
    with _SINGLETON_LOCK:
        _RETRIEVER = None
