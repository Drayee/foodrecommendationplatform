"""``rag`` 包单元测试。

运行方式
    ``python -m pytest -q rag/tests.py``                ← 首选（pytest.ini 已含 DJANGO_SETTINGS_MODULE）
    ``python manage.py test rag -v 2``                  ← Django 自带测试库隔离
    ``python -m unittest rag.tests -v``                 ← 无 pytest 时的兜底（本文件会自行 django.setup()）

覆盖点
    chunker   空文本 / 短文本 / 超长无标点 / overlap / 菜谱切分与步骤完整性
    embedder  形状 dtype / L2 归一化 / 空文本零向量 / 确定性 / 相似度排序 / n-gram 词元
              + Ollama backend：批量端点、老端点降级、熔断回退、工厂切换、（可选）真机连通
    bm25      排序正确性 / 空索引 / 空查询 / len / 序列化往返 / 中文分词
    store     构建-保存-加载往返 / 检索 / 缺失与损坏文件降级 / clear / fingerprint
    retriever 空库降级 / RRF 融合次序 / BM25 权重 / 标题路 / 异常吞掉 / 单例 / mtime 热重载
    build     TestCase 建 5 条 Recipe → build_index → search 命中预期菜名（含 RecipeEmbedding 落库）
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "RAGDjange.settings")

import django  # noqa: E402  (必须在 setup 之前 import)
from django.apps import apps as django_apps  # noqa: E402

if not django_apps.ready:  # pragma: no cover - manage.py test 时已就绪
    django.setup()

import numpy as np  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test import SimpleTestCase, TestCase  # noqa: E402

from rag.bm25 import BM25Index, tokenize  # noqa: E402
from rag.chunker import Chunk, chunk_recipe, split_text  # noqa: E402
from rag.embedder import (  # noqa: E402
    EmbedderUnavailable,
    FallbackEmbedder,
    HashingTfidfEmbedder,
    OllamaEmbedder,
    analyze,
    get_embedder,
    reset_embedder,
)
from rag.retriever import Hit, Retriever, get_retriever, reset_retriever  # noqa: E402
from rag.store import BM25_FILE, MANIFEST_FILE, META_FILE, VECTORS_FILE, VectorStore  # noqa: E402
import rag.embedder as embedder_module  # noqa: E402


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------
def _under_test_runner() -> bool:
    """判断当前是否跑在「独立测试库」上（manage.py test / pytest-django 会建临时库）。

    裸 ``python -m unittest rag.tests`` 没有测试库，会直接写开发库 ``db.sqlite3``；
    这类写库用例必须跳过，避免污染真实数据（残留 slug 冲突、脏菜谱等）。
    """
    try:
        from django.db import connection

        name = str(connection.settings_dict.get("NAME", ""))
    except Exception:  # pragma: no cover - 连接未就绪
        return False
    return name.startswith("test") or "memorydb" in name or name == ":memory:"


class TempIndexMixin:
    """把 ``settings.RAG["INDEX_DIR"]`` 指到临时目录，退出时还原并清理单例。"""

    index_dir: str

    def setUp(self) -> None:  # noqa: D401 - unittest 生命周期
        super().setUp()
        self.index_dir = tempfile.mkdtemp(prefix="rag-index-")
        self._old_index_dir = settings.RAG.get("INDEX_DIR")
        settings.RAG["INDEX_DIR"] = self.index_dir
        reset_retriever()
        self.addCleanup(self._cleanup_index_dir)

    def _cleanup_index_dir(self) -> None:
        reset_retriever()
        if self._old_index_dir is not None:
            settings.RAG["INDEX_DIR"] = self._old_index_dir
        shutil.rmtree(self.index_dir, ignore_errors=True)


def _fake_recipe(
    pk: int = 7,
    title: str = "番茄牛腩",
    summary: str = "酸甜开胃的经典家常炖菜",
    steps: list[str] | None = None,
    tips: str = "",
    tags: list[str] | None = None,
):
    """构造一个 chunk_recipe 能吃的轻量替身（不依赖数据库）。"""
    from types import SimpleNamespace

    step_texts = steps if steps is not None else ["牛腩切块焯水去腥", "番茄炒出汁后下牛腩慢炖四十分钟"]
    return SimpleNamespace(
        pk=pk,
        slug=f"recipe-{pk}",
        title=title,
        summary=summary,
        tips=tips,
        servings=2,
        calories=520.0,
        source="seed",
        status="published",
        cuisine=SimpleNamespace(name="川菜"),
        tags=[SimpleNamespace(name=name) for name in (tags or ["酸甜", "炖菜"])],
        ingredients=[
            SimpleNamespace(name_text="牛腩", amount="500", unit="克", ingredient=None),
            SimpleNamespace(name_text="番茄", amount="3", unit="个", ingredient=None),
        ],
        steps=[
            SimpleNamespace(index=i, text=text) for i, text in enumerate(step_texts, start=1)
        ],
        total_minutes=50,
        difficulty_label="普通",
    )


def _build_store(index_dir: str | Path, texts: list[str], embedder=None) -> VectorStore:
    """用真实编码器把若干文本写成一个可加载的索引。"""
    embedder = embedder or HashingTfidfEmbedder(dim=128)
    ids = [f"recipe:{i}:0" for i in range(len(texts))]
    vectors = embedder.encode(texts)
    payloads = [
        {"text": text, "title": text, "recipe_id": i, "model": embedder.name} for i, text in enumerate(texts)
    ]
    store = VectorStore(index_dir)
    store.build(ids, vectors, payloads)
    store.model = embedder.name
    store.save()
    return store


class _StubBM25:
    """BM25 替身：直接回放预设命中。"""

    def __init__(self, hits: list[tuple[str, float]]) -> None:
        self._hits = hits

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        return list(self._hits[:k])


class _StubStore:
    """VectorStore 替身：用于精确验证 RRF 融合与异常吞掉。"""

    def __init__(
        self,
        vector_hits: list[tuple[str, float]],
        bm25_hits: list[tuple[str, float]],
        *,
        titles: dict[str, str] | None = None,
        fail: bool = False,
    ) -> None:
        self.vector_hits = vector_hits
        self.bm25 = _StubBM25(bm25_hits)
        self.manifest: dict = {}
        self.fail = fail
        self.payloads = {
            chunk_id: {"text": f"text-{chunk_id}", "title": title, "recipe_id": 1}
            for chunk_id, title in (titles or {}).items()
        }

    def load(self) -> bool:
        return True

    def fingerprint(self) -> float:
        return 1.0

    def __len__(self) -> int:
        return max(len(self.vector_hits), len(self.bm25._hits), 1)

    def search(self, query_vec, k: int = 10):  # noqa: ANN001 - 替身签名
        if self.fail:
            raise RuntimeError("stub store boom")
        return list(self.vector_hits[:k])

    def get_payload(self, chunk_id: str) -> dict:
        if chunk_id in self.payloads:
            return dict(self.payloads[chunk_id])
        return {"text": f"text-{chunk_id}", "title": f"title-{chunk_id}", "recipe_id": 1}

    def stats(self) -> dict:
        return {"size": len(self), "dim": 8, "updated_at": "2026-01-01T00:00:00+00:00", "model": "stub"}


# ---------------------------------------------------------------------------
# chunker
# ---------------------------------------------------------------------------
class SplitTextTests(SimpleTestCase):
    """split_text 的边界行为。"""

    def test_short_text_returns_single_chunk(self) -> None:
        self.assertEqual(split_text("番茄牛腩", chunk_size=420, overlap=80), ["番茄牛腩"])

    def test_empty_text_returns_empty_list(self) -> None:
        for value in ("", "   ", "\n\n", None):
            with self.subTest(value=value):
                self.assertEqual(split_text(value), [])

    def test_long_text_is_split_and_bounded(self) -> None:
        text = "".join(f"第{i}步：把食材处理干净并切块备用。" for i in range(60))
        chunks = split_text(text, chunk_size=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks), [len(c) for c in chunks])
        self.assertTrue(chunks[0].startswith("第0步"))

    def test_adjacent_chunks_keep_overlap(self) -> None:
        text = "".join(f"步骤{i}需要小火慢炖并不断搅拌。" for i in range(40))
        chunks = split_text(text, chunk_size=120, overlap=30)
        overlaps = sum(1 for prev, nxt in zip(chunks, chunks[1:]) if prev[-10:] and prev[-10:] in nxt)
        self.assertGreaterEqual(overlaps, 1, chunks[:2])

    def test_single_long_sentence_without_punctuation(self) -> None:
        text = "牛" * 1000
        chunks = split_text(text, chunk_size=200, overlap=20)
        self.assertGreaterEqual(len(chunks), 4)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertTrue(all(set(chunk) == {"牛"} for chunk in chunks))

    def test_chunk_size_is_clamped_to_reasonable_minimum(self) -> None:
        chunks = split_text("番" * 200, chunk_size=1, overlap=0)
        self.assertTrue(all(len(chunk) == 20 for chunk in chunks))


class ChunkRecipeTests(SimpleTestCase):
    """chunk_recipe 的分块结构与元信息。"""

    def test_chunk_ids_and_meta(self) -> None:
        chunks = chunk_recipe(_fake_recipe())
        self.assertIsInstance(chunks[0], Chunk)
        self.assertTrue(chunks[0].chunk_id.startswith("recipe:7:"))
        self.assertEqual(chunks[0].doc_id, "recipe:7")
        self.assertEqual(chunks[0].recipe_id, 7)
        for key in ("title", "cuisine", "tags", "total_minutes", "difficulty_label"):
            self.assertIn(key, chunks[0].meta)
        self.assertEqual(chunks[0].meta["title"], "番茄牛腩")
        self.assertEqual(chunks[0].meta["cuisine"], "川菜")
        self.assertEqual(chunks[0].meta["total_minutes"], 50)
        self.assertEqual([c.chunk_id for c in chunks], [f"recipe:7:{i}" for i in range(len(chunks))])

    def test_every_chunk_carries_title(self) -> None:
        recipe = _fake_recipe(steps=[f"第{i}步：仔细处理牛腩并保持中小火。" for i in range(30)])
        chunks = chunk_recipe(recipe, chunk_size=160, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all("番茄牛腩" in chunk.text for chunk in chunks))

    def test_steps_are_not_cut(self) -> None:
        steps = ["牛腩切块冷水下锅焯水去腥", "番茄去皮切块炒出红汤", "加牛腩转小火炖四十分钟收汁"]
        chunks = chunk_recipe(_fake_recipe(steps=steps), chunk_size=200, overlap=20)
        joined = "\n".join(chunk.text for chunk in chunks)
        for text in steps:
            self.assertIn(text, joined)

    def test_recipe_with_only_title_still_yields_one_chunk(self) -> None:
        from types import SimpleNamespace

        bare = SimpleNamespace(pk=99, title="白粥", tags=[], steps=[], summary="", tips="")
        chunks = chunk_recipe(bare)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].chunk_id, "recipe:99:0")
        self.assertIn("白粥", chunks[0].text)


# ---------------------------------------------------------------------------
# embedder
# ---------------------------------------------------------------------------
class HashingEmbedderTests(SimpleTestCase):
    """HashingTfidfEmbedder 的数值性质。"""

    def setUp(self) -> None:
        super().setUp()
        self.embedder = HashingTfidfEmbedder(dim=256)

    def test_shape_and_dtype(self) -> None:
        matrix = self.embedder.encode(["番茄牛腩", "清蒸鲈鱼", ""])
        self.assertEqual(matrix.shape, (3, 256))
        self.assertEqual(matrix.dtype, np.float32)

    def test_rows_are_l2_normalized(self) -> None:
        matrix = self.embedder.encode(["番茄牛腩炖土豆", "麻婆豆腐的做法"])
        norms = np.linalg.norm(matrix, axis=1)
        np.testing.assert_allclose(norms, np.ones(2), atol=1e-5)

    def test_empty_text_gives_zero_vector_without_nan(self) -> None:
        matrix = self.embedder.encode(["", "   ", "！？。"])
        self.assertFalse(np.isnan(matrix).any())
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), np.zeros(3), atol=1e-6)

    def test_encoding_is_deterministic(self) -> None:
        first = self.embedder.encode(["番茄牛腩 beef stew"])
        second = HashingTfidfEmbedder(dim=256).encode(["番茄牛腩 beef stew"])
        np.testing.assert_array_equal(first, second)

    def test_similar_query_ranks_first(self) -> None:
        candidates = [
            "番茄牛腩的做法：牛腩焯水后与番茄一起慢炖五十分钟",
            "清蒸鲈鱼的做法：鱼身划刀加姜丝蒸八分钟",
            "蒜蓉西兰花：焯水后大火快炒",
        ]
        query = HashingTfidfEmbedder(dim=256).encode(["番茄牛腩怎么做好吃"])[0]
        scores = [float(np.dot(query, row)) for row in self.embedder.encode(candidates)]
        self.assertEqual(int(np.argmax(scores)), 0, scores)
        self.assertGreater(scores[0], scores[1])

    def test_analyze_emits_ngrams(self) -> None:
        table = analyze("番茄 beef stew")
        self.assertTrue(any(token.startswith("u:番") for token in table))
        self.assertIn("b:番茄", table)
        self.assertIn("w:beef", table)
        self.assertIn("wb:beef|stew", table)
        self.assertEqual(analyze(""), {})

    def test_factory_returns_singleton(self) -> None:
        self.assertIs(get_embedder(), get_embedder())
        self.assertEqual(get_embedder().dim, int(settings.RAG["EMBED_DIM"]))


# ---------------------------------------------------------------------------
# embedder · 可选本地 Ollama backend（不依赖服务在线：传输层全部打桩）
# ---------------------------------------------------------------------------
def _fake_transport(dim: int = 8, batch_ok: bool = True, legacy_ok: bool = True, calls: list | None = None):
    """构造 ``_post_json`` 替身：模拟 Ollama 的 /api/embed 与 /api/embeddings。"""

    def fake_post(url: str, payload: dict, timeout: float = 10.0) -> dict:
        if calls is not None:
            calls.append(url)
        if url.endswith("/api/embed"):
            if not batch_ok:
                raise EmbedderUnavailable("404 not found")
            return {"embeddings": [[0.5 + index] * dim for index, _ in enumerate(payload["input"])]}
        if url.endswith("/api/embeddings"):
            if not legacy_ok:
                raise EmbedderUnavailable("404 not found")
            return {"embedding": [0.25] * dim}
        raise AssertionError(f"意外的 URL：{url}")

    return fake_post


class OllamaEmbedderTests(SimpleTestCase):
    """Ollama backend 的传输、降级与工厂切换（不联网）。"""

    def test_batch_endpoint_returns_normalized_matrix(self) -> None:
        embedder = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434", dim=4)
        with mock.patch.object(embedder_module, "_post_json", _fake_transport(dim=8)):
            matrix = embedder.encode(["番茄牛腩", "清蒸鲈鱼"])
        self.assertEqual(matrix.shape, (2, 8))
        self.assertEqual(matrix.dtype, np.float32)
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), np.ones(2), atol=1e-6)
        self.assertEqual(embedder.dim, 8)  # 以实际维度为准（配置 4 → 实际 8）

    def test_base_url_strips_openai_v1_suffix(self) -> None:
        embedder = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434/v1")
        self.assertEqual(embedder.base_url, "http://127.0.0.1:11434")

    def test_falls_back_to_legacy_endpoint(self) -> None:
        embedder = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434", dim=8)
        calls: list[str] = []
        with mock.patch.object(
            embedder_module, "_post_json", _fake_transport(dim=8, batch_ok=False, calls=calls)
        ):
            matrix = embedder.encode(["番茄牛腩"])
        self.assertEqual(matrix.shape, (1, 8))
        self.assertFalse(embedder._batch_supported)
        self.assertTrue(any(url.endswith("/api/embeddings") for url in calls))

    def test_unreachable_service_raises_embedder_unavailable(self) -> None:
        embedder = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434", dim=8)

        def boom(url: str, payload: dict, timeout: float = 10.0) -> dict:
            raise EmbedderUnavailable("connection refused")

        with mock.patch.object(embedder_module, "_post_json", boom):
            with self.assertRaises(EmbedderUnavailable):
                embedder.encode(["番茄牛腩"])

    def test_empty_input_returns_empty_matrix(self) -> None:
        embedder = OllamaEmbedder("nomic-embed-text", dim=8)
        matrix = embedder.encode([])
        self.assertEqual(matrix.shape, (0, 8))

    def test_fallback_embedder_switches_on_failure(self) -> None:
        class _Broken:
            name = "broken"
            dim = 768

            def encode(self, texts):  # noqa: ANN001, D401 - 替身
                raise EmbedderUnavailable("down")

        hashing = HashingTfidfEmbedder(dim=64)
        embedder = FallbackEmbedder(_Broken(), hashing)
        self.assertEqual(embedder.active_backend, "primary")
        with self.assertLogs("rag.embedder", level="WARNING"):
            matrix = embedder.encode(["番茄牛腩"])
        self.assertEqual(embedder.active_backend, "fallback")
        self.assertEqual(embedder.name, hashing.name)
        self.assertEqual(embedder.dim, 64)
        self.assertEqual(matrix.shape, (1, 64))
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), np.ones(1), atol=1e-6)

    def test_fallback_embedder_keeps_primary_when_healthy(self) -> None:
        calls: list[str] = []
        primary = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434", dim=8)

        def fake(url: str, payload: dict, timeout: float = 10.0) -> dict:
            calls.append(url)
            return _fake_transport(dim=8)(url, payload, timeout)

        with mock.patch.object(embedder_module, "_post_json", fake):
            embedder = FallbackEmbedder(primary, HashingTfidfEmbedder(dim=64))
            matrix = embedder.encode(["番茄牛腩"])
        self.assertEqual(embedder.active_backend, "primary")
        self.assertEqual(matrix.shape, (1, 8))
        self.assertTrue(calls)

    def test_factory_switches_by_embed_backend_setting(self) -> None:
        old_backend = settings.RAG.get("EMBED_BACKEND")
        old_model = settings.RAG.get("EMBED_MODEL")
        settings.RAG["EMBED_BACKEND"] = "ollama"
        settings.RAG["EMBED_MODEL"] = "nomic-embed-text"
        reset_embedder()
        try:
            embedder = get_embedder()
            self.assertIsInstance(embedder, FallbackEmbedder)
            self.assertIsInstance(embedder.primary, OllamaEmbedder)
            self.assertEqual(embedder.primary.name, "nomic-embed-text")
            self.assertEqual(embedder.primary.base_url.rstrip("/"), "http://127.0.0.1:11434")
        finally:
            settings.RAG["EMBED_BACKEND"] = old_backend or "hashing"
            if old_model is not None:
                settings.RAG["EMBED_MODEL"] = old_model
            reset_embedder()

    def test_hashing_backend_name_is_self_consistent(self) -> None:
        """EMBED_MODEL 指向 ollama 模型时，hashing 后端不能冒用它的名字（否则索引标签撒谎）。"""
        old_backend = settings.RAG.get("EMBED_BACKEND")
        old_model = settings.RAG.get("EMBED_MODEL")
        settings.RAG["EMBED_BACKEND"] = "hashing"
        settings.RAG["EMBED_MODEL"] = "nomic-embed-text"
        reset_embedder()
        try:
            embedder = get_embedder()
            self.assertIsInstance(embedder, HashingTfidfEmbedder)
            self.assertTrue(embedder.name.startswith("hashing"), embedder.name)
        finally:
            settings.RAG["EMBED_BACKEND"] = old_backend or "hashing"
            if old_model is not None:
                settings.RAG["EMBED_MODEL"] = old_model
            reset_embedder()

    def test_live_ollama_embedding_if_available(self) -> None:
        """本机 Ollama 在线时做一次真机验证；离线自动跳过（CI / 他人机器不受影响）。"""
        embedder = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434", timeout=5)
        try:
            matrix = embedder.encode(["番茄牛腩怎么做", "清蒸鲈鱼的火候"])
        except EmbedderUnavailable as exc:
            self.skipTest(f"本机 Ollama 不可用：{exc}")
        self.assertEqual(matrix.shape[0], 2)
        self.assertGreaterEqual(matrix.shape[1], 64)
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), np.ones(2), atol=1e-4)


# ---------------------------------------------------------------------------
# bm25
# ---------------------------------------------------------------------------
class BM25Tests(SimpleTestCase):
    """BM25 排序与持久化。"""

    def setUp(self) -> None:
        super().setUp()
        self.index = BM25Index()
        self.index.add_many(
            [
                ("a", "番茄牛腩：牛腩切块与番茄同炖，酸甜开胃"),
                ("b", "清蒸鲈鱼：鱼身划刀，加姜丝大火蒸八分钟"),
                ("c", "番茄炒蛋：番茄去皮切块，鸡蛋打散炒熟"),
            ]
        )

    def test_tokenize_keeps_chinese_bigrams(self) -> None:
        tokens = tokenize("番茄牛腩 beef stew 300g")
        self.assertIn("番茄", tokens)
        self.assertIn("牛腩", tokens)
        self.assertIn("beef", tokens)
        self.assertIn("300g", tokens)
        self.assertIn("300", tokenize("300 克牛腩"))

    def test_search_ranks_relevant_document_first(self) -> None:
        hits = self.index.search("番茄牛腩怎么做", k=3)
        self.assertEqual(hits[0][0], "a")
        self.assertGreater(hits[0][1], hits[1][1])
        # 「番茄炒蛋」共享「番茄」词元 → 应被召回；「清蒸鲈鱼」无共同词元 → 不返回
        chunk_ids = [chunk_id for chunk_id, _ in hits]
        self.assertIn("c", chunk_ids)
        self.assertNotIn("b", chunk_ids)

    def test_single_char_query_still_matches(self) -> None:
        hits = self.index.search("鱼", k=2)
        self.assertEqual(hits[0][0], "b")

    def test_empty_index_returns_empty(self) -> None:
        self.assertEqual(BM25Index().search("番茄牛腩"), [])
        self.assertEqual(len(BM25Index()), 0)

    def test_empty_query_returns_empty(self) -> None:
        self.assertEqual(self.index.search(""), [])
        self.assertEqual(self.index.search("！？。、", k=3), [])

    def test_k_limits_results(self) -> None:
        self.assertLessEqual(len(self.index.search("番茄 牛腩 鲈鱼 鸡蛋", k=1)), 1)

    def test_serialization_roundtrip_preserves_ranking(self) -> None:
        restored = BM25Index.from_dict(self.index.to_dict())
        self.assertEqual(len(restored), len(self.index))
        self.assertEqual(restored.search("番茄牛腩", k=2), self.index.search("番茄牛腩", k=2))
        self.assertAlmostEqual(restored._avgdl, self.index._avgdl)

    def test_from_dict_rejects_broken_payload(self) -> None:
        with self.assertRaises(ValueError):
            BM25Index.from_dict({"doc_ids": ["a", "b"], "lens": [1]})
        with self.assertRaises(ValueError):
            BM25Index.from_dict({"doc_ids": "not-a-list", "lens": []})


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class VectorStoreTests(TempIndexMixin, SimpleTestCase):
    """落盘 / 加载 / 降级。"""

    def _make(self, texts: list[str]) -> VectorStore:
        embedder = HashingTfidfEmbedder(dim=64)
        return _build_store(self.index_dir, texts, embedder)

    def test_save_then_load_roundtrip(self) -> None:
        texts = ["番茄牛腩的做法", "清蒸鲈鱼的做法", "麻婆豆腐的做法"]
        store = self._make(texts)
        self.assertTrue((Path(self.index_dir) / VECTORS_FILE).is_file())
        self.assertTrue((Path(self.index_dir) / META_FILE).is_file())
        self.assertTrue((Path(self.index_dir) / BM25_FILE).is_file())
        self.assertTrue((Path(self.index_dir) / MANIFEST_FILE).is_file())

        loaded = VectorStore(self.index_dir)
        self.assertTrue(loaded.load())
        self.assertEqual(loaded.ids, store.ids)
        np.testing.assert_allclose(loaded.matrix, store.matrix, atol=1e-6)
        self.assertEqual(loaded.get_payload("recipe:1:0")["text"], "清蒸鲈鱼的做法")
        self.assertEqual(len(loaded.bm25), 3)
        self.assertEqual(loaded.model, store.model)

    def test_search_returns_semantic_top_hit(self) -> None:
        embedder = HashingTfidfEmbedder(dim=256)
        texts = ["番茄牛腩 酸甜 炖菜", "清蒸鲈鱼 清淡 蒸", "麻婆豆腐 麻辣 下饭"]
        _build_store(self.index_dir, texts, embedder)

        store = VectorStore(self.index_dir)
        self.assertTrue(store.load())
        query = embedder.encode(["番茄牛腩"])[0]
        hits = store.search(query, k=2)
        self.assertEqual(hits[0][0], "recipe:0:0")
        self.assertGreater(hits[0][1], hits[1][1])

    def test_search_with_mismatched_dim_returns_empty(self) -> None:
        store = self._make(["番茄牛腩"])
        self.assertEqual(store.search(np.ones(7, dtype=np.float32), k=3), [])

    def test_load_missing_dir_returns_false(self) -> None:
        store = VectorStore(Path(self.index_dir) / "not-exists")
        self.assertFalse(store.load())
        self.assertEqual(len(store), 0)
        self.assertEqual(store.search(np.ones(64, dtype=np.float32), k=3), [])

    def test_load_corrupt_meta_returns_false(self) -> None:
        self._make(["番茄牛腩"])
        (Path(self.index_dir) / META_FILE).write_text("{broken json", encoding="utf-8")
        store = VectorStore(self.index_dir)
        self.assertFalse(store.load())
        self.assertEqual(len(store), 0)

    def test_get_payload_missing_returns_empty_dict(self) -> None:
        store = self._make(["番茄牛腩"])
        self.assertEqual(store.get_payload("recipe:999:0"), {})

    def test_build_rejects_mismatched_lengths(self) -> None:
        store = VectorStore(self.index_dir)
        with self.assertRaises(ValueError):
            store.build(["a"], np.zeros((2, 4), dtype=np.float32), [{"text": "x"}, {"text": "y"}])

    def test_clear_removes_files(self) -> None:
        store = self._make(["番茄牛腩"])
        store.clear()
        self.assertEqual(len(store), 0)
        for name in (VECTORS_FILE, META_FILE, BM25_FILE, MANIFEST_FILE):
            self.assertFalse((Path(self.index_dir) / name).exists(), name)

    def test_fingerprint_moves_after_save(self) -> None:
        store = VectorStore(self.index_dir)
        self.assertEqual(store.fingerprint(), 0.0)
        _build_store(self.index_dir, ["番茄牛腩"])
        self.assertGreater(VectorStore(self.index_dir).fingerprint(), 0.0)

    def test_stats_contains_required_keys(self) -> None:
        store = self._make(["番茄牛腩", "清蒸鲈鱼"])
        stats = store.stats()
        for key in ("size", "dim", "updated_at", "model"):
            self.assertIn(key, stats)
        self.assertEqual(stats["size"], 2)
        self.assertEqual(stats["dim"], 64)


# ---------------------------------------------------------------------------
# retriever（无数据库）
# ---------------------------------------------------------------------------
class RetrieverBasicsTests(TempIndexMixin, SimpleTestCase):
    """空库降级、RRF 融合、异常吞掉、单例与热重载。"""

    def test_empty_index_search_returns_empty_and_stats_not_ready(self) -> None:
        retriever = get_retriever()
        self.assertEqual(retriever.search("番茄牛腩"), [])
        stats = retriever.stats()
        self.assertIs(stats["ready"], False)
        self.assertEqual(stats["size"], 0)
        for key in ("ready", "size", "dim", "model", "updated_at"):
            self.assertIn(key, stats)

    def test_blank_query_returns_empty(self) -> None:
        _build_store(self.index_dir, ["番茄牛腩的做法"])
        retriever = get_retriever()
        for query in ("", "   ", None):
            self.assertEqual(retriever.search(query), [])

    def test_search_hits_after_index_built(self) -> None:
        _build_store(self.index_dir, ["番茄牛腩 酸甜 炖菜", "清蒸鲈鱼 清淡 蒸制"], HashingTfidfEmbedder())
        retriever = get_retriever()
        hits = retriever.search("番茄牛腩怎么做", k=2)
        self.assertTrue(hits)
        self.assertIsInstance(hits[0], Hit)
        self.assertEqual(hits[0].chunk_id, "recipe:0:0")
        self.assertEqual(hits[0].payload["title"], "番茄牛腩 酸甜 炖菜")
        self.assertTrue(hits[0].text)

    def test_filters_can_drop_hits(self) -> None:
        _build_store(self.index_dir, ["番茄牛腩 酸甜 炖菜", "清蒸鲈鱼 清淡 蒸制"], HashingTfidfEmbedder())
        retriever = get_retriever()
        hits = retriever.search("番茄牛腩", k=3, filters={"recipe_id": 1})
        self.assertEqual([hit.recipe_id for hit in hits], [1] * len(hits))
        self.assertEqual(retriever.search("番茄牛腩", k=3, filters={"recipe_id": 999}), [])

    def test_rrf_fusion_reorders_single_path_winners(self) -> None:
        retriever = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=8))
        retriever.store = _StubStore(
            vector_hits=[("a", 0.9), ("b", 0.8), ("c", 0.7)],
            bm25_hits=[("c", 5.0), ("b", 4.0), ("d", 3.0)],
        )
        retriever._loaded = False
        ordered = [hit.chunk_id for hit in retriever.search("番茄牛腩", k=4)]
        # a 只有向量路（rank1），b/c 两路都命中 → RRF 后 b、c 反超 a
        self.assertEqual(ordered, ["b", "c", "a", "d"])
        self.assertGreater(len(ordered), 3)

    def test_rrf_with_zero_bm25_weight_uses_vector_order(self) -> None:
        old_weight = settings.RAG.get("HYBRID_BM25_WEIGHT")
        settings.RAG["HYBRID_BM25_WEIGHT"] = 0.0
        self.addCleanup(lambda: settings.RAG.__setitem__("HYBRID_BM25_WEIGHT", old_weight))
        retriever = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=8))
        retriever.store = _StubStore(
            vector_hits=[("a", 0.9), ("b", 0.8), ("c", 0.7)],
            bm25_hits=[("c", 5.0), ("b", 4.0)],
        )
        retriever._loaded = False
        ordered = [hit.chunk_id for hit in retriever.search("番茄牛腩", k=3)]
        self.assertEqual(ordered, ["a", "b", "c"])

    def test_title_channel_puts_exact_dish_first(self) -> None:
        base_hits = dict(
            vector_hits=[("a", 0.9), ("b", 0.8), ("c", 0.7)],
            bm25_hits=[("c", 5.0), ("b", 4.0), ("d", 3.0)],
        )
        titles = {"a": "红烧肉", "b": "番茄炖菜", "c": "番茄牛腩", "d": "盐水鸭"}

        # 只开两路：b（两路命中）反超 c
        two_path = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=8))
        two_path.store = _StubStore(**base_hits)
        two_path._loaded = False
        self.assertEqual([hit.chunk_id for hit in two_path.search("番茄牛腩怎么做", k=4)][0], "b")

        # 加上标题路：用户就是在点「番茄牛腩」，它必须排第一
        three_path = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=8))
        three_path.store = _StubStore(**base_hits, titles=titles)
        three_path._loaded = False
        ordered = [hit.chunk_id for hit in three_path.search("番茄牛腩怎么做", k=4)]
        self.assertEqual(ordered, ["c", "b", "a", "d"])

    def test_title_weight_zero_disables_title_channel(self) -> None:
        old_weight = settings.RAG.get("HYBRID_TITLE_WEIGHT")
        settings.RAG["HYBRID_TITLE_WEIGHT"] = 0.0
        self.addCleanup(self._restore_title_weight, old_weight)
        retriever = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=8))
        retriever.store = _StubStore(
            vector_hits=[("a", 0.9), ("b", 0.8), ("c", 0.7)],
            bm25_hits=[("c", 5.0), ("b", 4.0), ("d", 3.0)],
            titles={"a": "红烧肉", "b": "番茄炖菜", "c": "番茄牛腩", "d": "盐水鸭"},
        )
        retriever._loaded = False
        self.assertEqual([hit.chunk_id for hit in retriever.search("番茄牛腩怎么做", k=4)], ["b", "c", "a", "d"])

    @staticmethod
    def _restore_title_weight(old_weight) -> None:  # noqa: ANN001 - 测试工具
        if old_weight is None:
            settings.RAG.pop("HYBRID_TITLE_WEIGHT", None)
        else:
            settings.RAG["HYBRID_TITLE_WEIGHT"] = old_weight

    def test_search_swallows_store_exception(self) -> None:
        retriever = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=8))
        retriever.store = _StubStore([], [], fail=True)
        retriever._loaded = False
        with self.assertLogs("rag.retriever", level="WARNING"):
            self.assertEqual(retriever.search("番茄牛腩", k=3), [])

    def test_dim_mismatch_degrades_vector_path_only(self) -> None:
        """索引是 64 维、查询向量是 32 维（换过 backend 没重建）→ 向量路失效，词面路仍可召回。"""
        _build_store(self.index_dir, ["番茄牛腩 酸甜 炖菜", "清蒸鲈鱼 清淡 蒸制"], HashingTfidfEmbedder(dim=64))
        retriever = Retriever(index_dir=self.index_dir, embedder=HashingTfidfEmbedder(dim=32))
        hits = retriever.search("番茄牛腩", k=2)
        self.assertTrue(hits, "维度不匹配时不应整体返回空，BM25/标题路应兜住")
        self.assertEqual(hits[0].payload.get("title"), "番茄牛腩 酸甜 炖菜")

    def test_singleton_identity_and_reset(self) -> None:
        first = get_retriever()
        self.assertIs(first, get_retriever())
        reset_retriever()
        self.assertIsNot(first, get_retriever())

    def test_mtime_change_triggers_reload(self) -> None:
        embedder = HashingTfidfEmbedder()  # 与单例同维，保证向量路与 BM25 路都生效
        _build_store(self.index_dir, ["番茄牛腩的做法"], embedder)
        retriever = get_retriever()
        self.assertEqual(retriever.search("番茄牛腩", k=1)[0].chunk_id, "recipe:0:0")

        # 外部进程（新进程/管理命令）重建索引：写一份只有鲈鱼的新索引并前移 mtime
        shutil.rmtree(self.index_dir, ignore_errors=True)
        _build_store(self.index_dir, ["清蒸鲈鱼的做法"], embedder)
        future = 4_000_000_000.0
        for name in (VECTORS_FILE, META_FILE, BM25_FILE, MANIFEST_FILE):
            os.utime(Path(self.index_dir) / name, (future, future))

        hits = retriever.search("清蒸鲈鱼", k=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].payload["text"], "清蒸鲈鱼的做法")

    def test_invalidate_forces_reload(self) -> None:
        _build_store(self.index_dir, ["番茄牛腩的做法"])
        retriever = get_retriever()
        self.assertTrue(retriever.stats()["ready"])
        retriever.invalidate()
        self.assertEqual(len(retriever.store), 0)
        self.assertTrue(retriever.stats()["ready"])


# ---------------------------------------------------------------------------
# retriever + 数据库（5 条菜谱的小库）
# ---------------------------------------------------------------------------
class RetrieverIndexBuildTests(TempIndexMixin, TestCase):
    """build_index / search / RecipeEmbedding 落库的端到端行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not _under_test_runner():
            raise unittest.SkipTest("写库用例需要独立测试库：python manage.py test rag / python -m pytest rag/tests.py")
        super().setUpClass()

    @classmethod
    def setUpTestData(cls) -> None:
        from food.models import Cuisine, Recipe, RecipeIngredient, RecipeStep, Tag

        cuisine_cn = Cuisine.objects.create(name="川菜", region="四川")
        cuisine_cn2 = Cuisine.objects.create(name="粤菜", region="广东")
        tag_sour = Tag.objects.create(name="酸甜", kind=Tag.Kind.TASTE)
        tag_steam = Tag.objects.create(name="清蒸", kind=Tag.Kind.METHOD)

        fixtures = [
            (
                "番茄牛腩",
                cuisine_cn,
                [tag_sour],
                "酸甜开胃的经典家常炖菜，牛腩软烂番茄浓郁。",
                [("牛腩", "500", "克"), ("番茄", "3", "个")],
                ["牛腩切块冷水下锅焯水去腥", "番茄去皮切块炒出红汤", "加牛腩转小火炖四十分钟收汁"],
            ),
            (
                "清蒸鲈鱼",
                cuisine_cn2,
                [tag_steam],
                "鱼肉鲜嫩，只用姜丝与蒸鱼豉油提味。",
                [("鲈鱼", "1", "条"), ("姜", "20", "克")],
                ["鱼身划刀塞姜丝", "水开后大火蒸八分钟", "淋热油与蒸鱼豉油"],
            ),
            (
                "麻婆豆腐",
                cuisine_cn,
                [tag_sour],
                "麻辣鲜香的下饭菜，豆腐嫩滑。",
                [("豆腐", "400", "克"), ("牛肉末", "100", "克")],
                ["豆腐切块盐水浸泡", "炒香豆瓣酱与牛肉末", "下豆腐勾芡收汁"],
            ),
            (
                "白灼菜心",
                cuisine_cn2,
                [tag_steam],
                "清爽少油，突出蔬菜本味。",
                [("菜心", "300", "克"), ("蒜", "10", "克")],
                ["水开后下菜心焯三十秒", "捞出过冷水保持翠绿", "淋豉油撒蒜末"],
            ),
            (
                "蒜蓉西兰花",
                cuisine_cn2,
                [tag_steam],
                "低卡高纤的快手家常菜。",
                [("西兰花", "300", "克"), ("蒜", "20", "克")],
                ["西兰花掰小朵焯水", "蒜蓉爆香", "大火快炒一分钟出锅"],
            ),
        ]
        for title, cuisine, tags, summary, ingredients, steps in fixtures:
            recipe = Recipe.objects.create(
                title=title,
                cuisine=cuisine,
                summary=summary,
                difficulty=Recipe.Difficulty.NORMAL,
                prep_minutes=10,
                cook_minutes=30,
                servings=2,
                calories=400,
                status=Recipe.Status.PUBLISHED,
            )
            recipe.tags.set(tags)
            for order, (name, amount, unit) in enumerate(ingredients):
                RecipeIngredient.objects.create(
                    recipe=recipe, name_text=name, amount=amount, unit=unit, sort_order=order
                )
            for index, text in enumerate(steps, start=1):
                RecipeStep.objects.create(recipe=recipe, index=index, text=text)

    def test_build_index_returns_contract_shape(self) -> None:
        retriever = get_retriever()
        result = retriever.build_index(force=True)
        self.assertEqual(set(result), {"chunks", "recipes", "dim", "elapsed_ms", "model"})
        self.assertEqual(result["recipes"], 5)
        self.assertGreaterEqual(result["chunks"], 5)
        self.assertEqual(result["dim"], int(settings.RAG["EMBED_DIM"]))
        self.assertEqual(result["model"], get_embedder().name)
        self.assertGreater(result["elapsed_ms"], 0)

    def test_search_finds_expected_recipe_title(self) -> None:
        retriever = get_retriever()
        retriever.build_index(force=True)
        hits = retriever.search("番茄牛腩怎么做才软烂", k=3)
        self.assertTrue(hits, "检索应有结果")
        titles = [hit.payload.get("title") for hit in hits]
        self.assertIn("番茄牛腩", titles)
        self.assertEqual(titles[0], "番茄牛腩")
        self.assertGreater(hits[0].score, 0)

    def test_search_with_cuisine_filter(self) -> None:
        retriever = get_retriever()
        retriever.build_index(force=True)
        hits = retriever.search("清淡的鱼怎么做", k=5, filters={"cuisine": "粤菜"})
        self.assertTrue(hits)
        self.assertTrue(all(hit.payload.get("cuisine") == "粤菜" for hit in hits))

    def test_build_writes_recipe_embeddings(self) -> None:
        from food.models import Recipe, RecipeEmbedding

        retriever = get_retriever()
        retriever.build_index(force=True)
        embeddings = RecipeEmbedding.objects.all()
        self.assertEqual(embeddings.count(), Recipe.objects.published().count())
        row = embeddings.first()
        self.assertEqual(row.dim, int(settings.RAG["EMBED_DIM"]))
        self.assertEqual(len(bytes(row.vector)), row.dim * 4)
        self.assertEqual(row.model_name, get_embedder().name)
        # bulk_create 不触发 save()/信号，但 auto_now 由 ORM 的 pre_save 填值 —— PG/SQLite 都应有值
        self.assertIsNotNone(row.updated_at)
        # BinaryField 往返：字节取回后可还原成「已归一化的菜谱级向量」（PG 下为 bytea → memoryview）
        vector = np.frombuffer(bytes(row.vector), dtype=np.float32)
        self.assertEqual(vector.shape[0], row.dim)
        self.assertTrue(np.isfinite(vector).all())
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=3)

    def test_build_index_twice_updates_rows_instead_of_duplicating(self) -> None:
        from food.models import Recipe, RecipeEmbedding

        retriever = get_retriever()
        retriever.build_index(force=True)
        first = RecipeEmbedding.objects.count()
        retriever.build_index(force=True)  # 第二次走 bulk_update 分支
        self.assertEqual(RecipeEmbedding.objects.count(), first)
        self.assertEqual(RecipeEmbedding.objects.count(), Recipe.objects.published().count())
        self.assertTrue(all(row.updated_at is not None for row in RecipeEmbedding.objects.all()))

    def test_build_refreshes_search_doc(self) -> None:
        from food.models import Recipe

        recipe = Recipe.objects.get(title="番茄牛腩")
        Recipe.objects.filter(pk=recipe.pk).update(search_doc="")
        retriever = get_retriever()
        retriever.build_index(force=True)
        recipe.refresh_from_db()
        self.assertIn("番茄牛腩", recipe.search_doc)
        self.assertIn("食材", recipe.search_doc)
        self.assertGreater(recipe.content_version, 1)

    def test_second_build_reuses_index_when_unchanged(self) -> None:
        retriever = get_retriever()
        first = retriever.build_index(force=True)
        second = retriever.build_index(force=False)
        self.assertEqual(second["chunks"], first["chunks"])
        self.assertEqual(second["recipes"], first["recipes"])

    def test_stats_ready_after_build(self) -> None:
        retriever = get_retriever()
        retriever.build_index(force=True)
        stats = retriever.stats()
        self.assertIs(stats["ready"], True)
        self.assertGreater(stats["size"], 0)
        self.assertTrue(stats["updated_at"])
        self.assertEqual(stats["model"], get_embedder().name)

    def test_build_with_ollama_backend_if_available(self) -> None:
        """本机 Ollama 在线时验证：768 维索引能建、能查、维度写进 manifest / stats。"""
        ollama = OllamaEmbedder("nomic-embed-text", timeout=8)
        try:
            probe = ollama.encode(["连通性检查"])
        except EmbedderUnavailable as exc:
            self.skipTest(f"本机 Ollama 不可用：{exc}")

        retriever = get_retriever()
        retriever._embedder = ollama  # 注入，不改 settings（默认仍是 hashing）
        result = retriever.build_index(force=True)
        self.assertEqual(result["dim"], int(probe.shape[1]))
        self.assertEqual(result["model"], "nomic-embed-text")
        self.assertGreater(result["chunks"], 0)

        hits = retriever.search("番茄牛腩怎么做才软烂", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].payload.get("title"), "番茄牛腩")
        stats = retriever.stats()
        self.assertEqual(stats["dim"], int(probe.shape[1]))
        self.assertTrue(stats["ready"])
        self.assertEqual(
            int(retriever.store.manifest.get("dim", 0)), int(probe.shape[1]), "manifest 应记录实际维度"
        )

    def test_empty_library_degrades_gracefully(self) -> None:
        from food.models import Recipe

        Recipe.objects.all().delete()
        retriever = get_retriever()
        result = retriever.build_index(force=True)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["recipes"], 0)
        self.assertEqual(retriever.search("番茄牛腩"), [])
        self.assertIs(retriever.stats()["ready"], False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
