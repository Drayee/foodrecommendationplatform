"""文本向量化模块（embedder）—— 默认纯本地哈希 TF-IDF，可选本地 Ollama。

职责
    :class:`HashingTfidfEmbedder`  默认实现：中文 char 1~3 gram + 英文/数字词 + 词级 bigram，
                                  用 hashing trick 映射到 ``settings.RAG["EMBED_DIM"]``（默认 1024），
                                  TF 取 log 饱和、带稳定符号位、最后 L2 归一化，输出 float32；
    :class:`OllamaEmbedder`        可选：调本机 Ollama（``/api/embed`` / ``/api/embeddings``，
                                  如 ``nomic-embed-text``，768 维），只连回环地址，不下载模型；
    :class:`FallbackEmbedder`      给 Ollama 套的降级壳：任一请求失败即熔断回 hashing，永不抛给上层；
    :class:`BaseEmbedder`          抽象基类，方便将来替换为真正的语义模型；
    :func:`get_embedder`           进程内单例工厂（按 backend/name/dim 缓存）。

设计取舍
    - 用 ``zlib.crc32`` 做哈希：确定性、跨进程稳定、比 hashlib 快一个量级；
    - 中文单字权重低（0.6）、高频虚词更低（0.35），bigram 权重 1.0、trigram 0.85，
      英文词 1.0、词级 bigram 0.9 —— 这是「固定平滑 IDF」的近似替代，无需语料统计；
    - 不做倒排、不做语料级 idf，换来「零依赖 + 即时可用 + 完全可复现」；
    - 默认 backend 恒为 ``hashing``；只有 ``RAG_EMBED_BACKEND=ollama`` 才走上层本地服务，
      且失败自动熔断回 hashing（``FallbackEmbedder``），日志留痕；
    - **换 backend / 换模型会改变向量维度（hashing 1024 vs nomic-embed-text 768），
      必须重建索引**（``python manage.py build_index --force``），否则查询向量与索引维度不符，
      向量路会降级为空（检索只剩 BM25 + 标题路）；
    - ``sentence_transformers`` 只在显式指定 backend 且本地已有模型时 try/except 启用，
      默认路径绝不触发网络下载。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import urllib.error
import urllib.request
import zlib
from typing import Any, Sequence

import numpy as np
from django.conf import settings

logger = logging.getLogger("rag.embedder")

# 中文（含扩展 A / 兼容区）连续片段
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
# 英文 / 数字 / 版本号类 token
_ASCII_WORD = re.compile(r"[a-z0-9]+(?:[._\-][a-z0-9]+)*")

# 中文高频虚词：权重压到最低（近似停用词，但不删，保留一点语境）
_LOW_WEIGHT_CHARS = set("的了和是在有就不人都一上也很到说要你去会着看好自己这那他她它们个吗呢吧啊之与及其为以于而则把被让给从对向再又更最太")
# 英文停用词（哈希维度有限，直接丢弃收益更高）
_ASCII_STOP = {
    "a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with", "is", "are",
    "be", "as", "at", "by", "it", "this", "that", "from", "how", "what", "do", "does",
}

# token -> 基础权重
_W_CHAR = 0.6
_W_CHAR_LOW = 0.35
_W_BIGRAM = 1.0
_W_TRIGRAM = 0.85
_W_WORD = 1.0
_W_WORD_BIGRAM = 0.9

_SIGN_SEED = 0x5BD1E995


def _rag_conf(key: str, default):
    """安全读取 ``settings.RAG[key]``。"""
    try:
        conf = getattr(settings, "RAG", None)
    except Exception:  # pragma: no cover - settings 未配置
        return default
    if isinstance(conf, dict):
        value = conf.get(key, default)
        return default if value is None else value
    return default


def _bump(table: dict[str, float], token: str, weight: float) -> None:
    """累积 token 权重（重复出现即 TF）。"""
    table[token] = table.get(token, 0.0) + weight


def analyze(text: str) -> dict[str, float]:
    """把文本拆成 ``{token: 累积权重}``。

    token 前缀区分来源：``u:`` 单字、``b:`` 双字、``t:`` 三字、``w:`` 英文词、``wb:`` 词级 bigram。
    这样不同长度的 n-gram 在同一哈希空间里互不混淆。
    """
    if not text:
        return {}
    lowered = str(text).lower()
    table: dict[str, float] = {}

    for run in _CJK_RUN.findall(lowered):
        for ch in run:
            _bump(table, "u:" + ch, _W_CHAR_LOW if ch in _LOW_WEIGHT_CHARS else _W_CHAR)
        for i in range(len(run) - 1):
            _bump(table, "b:" + run[i : i + 2], _W_BIGRAM)
        for i in range(len(run) - 2):
            _bump(table, "t:" + run[i : i + 3], _W_TRIGRAM)

    words = [w for w in _ASCII_WORD.findall(lowered) if w not in _ASCII_STOP]
    for word in words:
        _bump(table, "w:" + word, _W_WORD)
    for left, right in zip(words, words[1:]):
        _bump(table, "wb:" + left + "|" + right, _W_WORD_BIGRAM)
    return table


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------
class BaseEmbedder:
    """向量编码器基类：子类实现 :meth:`encode` 即可。"""

    name: str = "base"
    dim: int = 0

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """把一批文本编码成 ``(n, dim)`` 的 float32 且已 L2 归一化的矩阵。"""
        raise NotImplementedError

    def encode_query(self, text: str) -> np.ndarray:
        """编码单条查询，返回 ``(dim,)`` 向量。"""
        matrix = self.encode([text])
        return matrix[0] if len(matrix) else np.zeros(self.dim, dtype=np.float32)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r} dim={self.dim}>"


# ---------------------------------------------------------------------------
# 默认实现：哈希 TF-IDF
# ---------------------------------------------------------------------------
class HashingTfidfEmbedder(BaseEmbedder):
    """中文友好的哈希 TF-IDF 向量化器（纯本地、确定性、零模型下载）。

    >>> emb = HashingTfidfEmbedder(dim=1024)
    >>> mat = emb.encode(["番茄牛腩", "清蒸鲈鱼"])
    >>> mat.shape, mat.dtype, round(float(np.linalg.norm(mat[0])), 6)
    ((2, 1024), dtype('float32'), 1.0)
    """

    def __init__(self, dim: int | None = None, name: str | None = None) -> None:
        raw_dim = dim if dim is not None else _rag_conf("EMBED_DIM", 1024)
        try:
            self.dim = int(raw_dim)
        except (TypeError, ValueError):  # pragma: no cover - 脏配置
            self.dim = 1024
        if self.dim <= 0:
            self.dim = 1024
        raw_name = name if name is not None else _rag_conf("EMBED_MODEL", "hashing-tfidf-zh")
        resolved = str(raw_name or "hashing-tfidf-zh")
        # EMBED_MODEL 指向可选后端（如 nomic-embed-text）时，hashing 不能冒用它的名字，
        # 否则 manifest / RecipeEmbedding.model_name 会把 hashing 索引标成 ollama 模型。
        if not resolved.lower().startswith("hashing"):
            resolved = "hashing-tfidf-zh"
        self.name = resolved

    # -- 内部：单条文本 -> 归一化向量 -------------------------------------
    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        table = analyze(text)
        if not table:
            return vector
        for token, weight in table.items():
            if weight <= 0:
                continue
            raw = token.encode("utf-8")
            index = zlib.crc32(raw) % self.dim
            # 稳定的 ±1 符号位：抵消哈希碰撞带来的系统性正偏差
            sign = 1.0 if (zlib.crc32(raw, _SIGN_SEED) & 1) == 0 else -1.0
            # TF 饱和：重复出现增长变慢（log 饱和）
            tf = 1.0 + math.log(weight) if weight > 1.0 else weight
            vector[index] += np.float32(sign * tf)
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= np.float32(norm)
        return vector

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """批量编码，返回 ``(len(texts), dim)`` float32 矩阵（行已 L2 归一化）。"""
        if texts is None:
            return np.zeros((0, self.dim), dtype=np.float32)
        items = list(texts)
        matrix = np.zeros((len(items), self.dim), dtype=np.float32)
        for row, text in enumerate(items):
            matrix[row] = self._vector("" if text is None else str(text))
        return matrix


# ---------------------------------------------------------------------------
# 可选实现：本地 sentence-transformers（默认不启用，绝不联网）
# ---------------------------------------------------------------------------
class SentenceTransformerEmbedder(BaseEmbedder):  # pragma: no cover - 可选路径
    """本地已有 sentence-transformers 模型时才可用；构造失败由工厂回退哈希实现。"""

    def __init__(self, model_name: str, dim: int | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # 可选依赖，缺失即抛 ImportError

        self.name = model_name
        self._model = SentenceTransformer(model_name, local_files_only=True)
        probe = self._model.encode(["测试"], normalize_embeddings=True)
        self.dim = int(dim or probe.shape[1])

    def encode(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        items = [("" if t is None else str(t)) for t in (texts or [])]
        if not items:
            return np.zeros((0, self.dim), dtype=np.float32)
        matrix = self._model.encode(items, normalize_embeddings=True)
        return np.asarray(matrix, dtype=np.float32)


# ---------------------------------------------------------------------------
# 可选实现：本机 Ollama 向量服务（只连回环地址，不下载模型）
# ---------------------------------------------------------------------------
class EmbedderUnavailable(RuntimeError):
    """本地向量服务不可用（连接失败 / 模型缺失 / 返回结构异常）。"""


def _llm_base_url() -> str:
    """从 ``settings.LLM["BASE_URL"]`` 推断 Ollama 根地址（去掉 ``/v1``）。"""
    try:
        conf = getattr(settings, "LLM", None) or {}
    except Exception:  # pragma: no cover
        return ""
    if isinstance(conf, dict):
        url = str(conf.get("BASE_URL") or "").strip()
        if url:
            return url.rstrip("/").removesuffix("/v1")
    return ""


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    """POST JSON 到本地服务（绕过系统代理，避免回环请求被代理劫持）。

    独立成模块级函数，便于测试用 monkeypatch 替换掉真实网络调用。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:  # 连接拒绝 / 超时
        raise EmbedderUnavailable(f"{url} 不可达：{exc}") from exc
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise EmbedderUnavailable(f"{url} 返回非 JSON：{body[:120]}") from exc
    if not isinstance(parsed, dict):
        raise EmbedderUnavailable(f"{url} 返回结构异常：{type(parsed).__name__}")
    return parsed


class OllamaEmbedder(BaseEmbedder):
    """调本机 Ollama 做向量化（默认 ``nomic-embed-text``，768 维）。

    只访问 ``127.0.0.1`` / ``localhost`` 上的模型服务，不联网、不下载模型；
    优先用批量接口 ``/api/embed``，不可用时退回逐条 ``/api/embeddings``。
    任何失败都抛 :class:`EmbedderUnavailable`，由 :class:`FallbackEmbedder` 熔断回 hashing。

    >>> emb = OllamaEmbedder("nomic-embed-text", "http://127.0.0.1:11434")  # doctest: +SKIP
    >>> emb.encode(["番茄牛腩"]).shape                                       # doctest: +SKIP
    (1, 768)
    """

    def __init__(
        self,
        model_name: str | None = None,
        base_url: str | None = None,
        dim: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.name = str(model_name or _rag_conf("EMBED_MODEL", "nomic-embed-text") or "nomic-embed-text")
        raw_base = str(
            base_url or _rag_conf("EMBED_BASE_URL", "") or _llm_base_url() or "http://127.0.0.1:11434"
        ).strip()
        self.base_url = raw_base.rstrip("/").removesuffix("/v1")
        try:
            self.dim = int(dim or _rag_conf("EMBED_DIM", 768) or 768)
        except (TypeError, ValueError):  # pragma: no cover
            self.dim = 768
        try:
            self.timeout = float(timeout or _rag_conf("EMBED_TIMEOUT", 10) or 10)
        except (TypeError, ValueError):  # pragma: no cover
            self.timeout = 10.0
        self._batch_supported = True
        self._dim_warned = False

    # -- 传输层 -----------------------------------------------------------
    def _single(self, text: str) -> list[float]:
        """逐条编码（``/api/embeddings``，兼容老版本 Ollama）。"""
        data = _post_json(
            f"{self.base_url}/api/embeddings",
            {"model": self.name, "prompt": text},
            self.timeout,
        )
        embedding = data.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise EmbedderUnavailable("embedding 字段为空或格式异常")
        return embedding

    def _batch(self, items: list[str]) -> list[list[float]]:
        """批量编码（``/api/embed``）；接口不可用（老版本 404 / 结构异常）时自动降级为逐条。

        连接失败也一样先试逐条：逐条再失败就由 :class:`EmbedderUnavailable` 冒泡，
        交给 :class:`FallbackEmbedder` 熔断回 hashing。
        """
        try:
            data = _post_json(
                f"{self.base_url}/api/embed",
                {"model": self.name, "input": items},
                self.timeout,
            )
            embeddings = data.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(items):
                raise ValueError("embeddings 数量与输入不一致")
            return embeddings
        except Exception as exc:
            logger.info("/api/embed 批量接口不可用（%s），改用 /api/embeddings 逐条", exc)
            self._batch_supported = False
            return [self._single(text) for text in items]

    # -- 编码 -------------------------------------------------------------
    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """批量编码；行已 L2 归一化（Ollama 不保证归一化，这里统一处理）。"""
        items = [("" if text is None else str(text)) for text in (texts or [])]
        if not items:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = self._batch(items) if self._batch_supported else [self._single(t) for t in items]
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(items) or matrix.shape[1] == 0:
            raise EmbedderUnavailable(f"向量矩阵形状异常：{matrix.shape}")
        if int(matrix.shape[1]) != self.dim:
            if not self._dim_warned:
                logger.info(
                    "Ollama 实际向量维度 %s 与配置 EMBED_DIM=%s 不同，以实际维度为准（需重建索引）",
                    matrix.shape[1], self.dim,
                )
                self._dim_warned = True
            self.dim = int(matrix.shape[1])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (matrix / norms).astype(np.float32, copy=False)


class FallbackEmbedder(BaseEmbedder):
    """给「可能不可靠的本地服务」套一层熔断：失败一次即永久回退主备实现。

    ``name`` / ``dim`` 反映**当前生效**的后端；熔断后上游会看到后面备实现的维度，
    这也是「换后端必须重建索引」的原因。
    """

    def __init__(self, primary: BaseEmbedder, fallback: BaseEmbedder) -> None:
        self.primary = primary
        self.fallback = fallback
        self._disabled = False

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.fallback.name if self._disabled else self.primary.name

    @property
    def dim(self) -> int:  # type: ignore[override]
        return int(self.fallback.dim if self._disabled else self.primary.dim)

    @property
    def active_backend(self) -> str:
        """当前生效的后端名（primary / fallback）。"""
        return "fallback" if self._disabled else "primary"

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """优先走主实现，失败则熔断并改用备实现（记 warning，不向上抛）。"""
        if not self._disabled:
            try:
                return self.primary.encode(texts)
            except Exception as exc:
                self._disabled = True
                logger.warning(
                    "%r 不可用（%s），已熔断并回退 %r —— 注意：维度可能变化，需重建索引",
                    getattr(self.primary, "name", self.primary), exc, self.fallback.name,
                )
        return self.fallback.encode(texts)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FallbackEmbedder active={self.active_backend} name={self.name!r} dim={self.dim}>"


# ---------------------------------------------------------------------------
# 工厂（进程内单例）
# ---------------------------------------------------------------------------
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str, int], BaseEmbedder] = {}

# 会走「本地向量服务」的 backend 别名
_OLLAMA_BACKENDS = {"ollama", "local-ollama", "llama-ollama"}


def _build_embedder(backend: str, name: str, dim: int) -> BaseEmbedder:
    """按 backend 配置构造编码器；可选后端失败一律回退哈希实现。"""
    if backend in _OLLAMA_BACKENDS:
        hashing = HashingTfidfEmbedder(dim=dim, name="hashing-tfidf-zh")
        try:
            primary = OllamaEmbedder(model_name=name if name else None, dim=None)
            logger.info("embedder 使用本地 Ollama：%s @ %s", primary.name, primary.base_url)
            return FallbackEmbedder(primary, hashing)
        except Exception as exc:  # pragma: no cover - 构造异常（如配置非法）
            logger.warning("Ollama embedder 构造失败（%s），回退 hashing-tfidf", exc)
            return hashing
    if backend in {"sentence-transformers", "sentence_transformers", "st"}:
        try:
            return SentenceTransformerEmbedder(name, dim)
        except Exception as exc:  # pragma: no cover - 依赖缺失 / 模型不存在
            logger.warning("sentence-transformers 不可用（%s），回退 hashing-tfidf", exc)
    # 默认 hashing：名字必须自洽 —— EMBED_MODEL 可能指向 ollama 模型（如 nomic-embed-text），
    # 不能拿它给 hashing 索引打标，否则 manifest / RecipeEmbedding.model_name 会撒谎。
    hashing_name = name if str(name).lower().startswith("hashing") else "hashing-tfidf-zh"
    return HashingTfidfEmbedder(dim=dim, name=hashing_name)


def get_embedder() -> BaseEmbedder:
    """返回进程内单例编码器（按 ``EMBED_BACKEND`` / ``EMBED_MODEL`` / ``EMBED_DIM`` 缓存）。"""
    backend = str(_rag_conf("EMBED_BACKEND", "hashing") or "hashing").strip().lower()
    name = str(_rag_conf("EMBED_MODEL", "hashing-tfidf-zh") or "hashing-tfidf-zh")
    try:
        dim = int(_rag_conf("EMBED_DIM", 1024) or 1024)
    except (TypeError, ValueError):  # pragma: no cover
        dim = 1024
    if dim <= 0:
        dim = 1024
    key = (backend, name, dim)
    with _CACHE_LOCK:
        embedder = _CACHE.get(key)
        if embedder is None:
            embedder = _build_embedder(backend, name, dim)
            _CACHE[key] = embedder
            logger.info("embedder 就绪：%r", embedder)
        return embedder


def reset_embedder() -> None:
    """清空单例缓存（测试或运行期改配置后使用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()
