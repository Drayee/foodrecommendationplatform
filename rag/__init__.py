"""``rag`` —— 「寻味」美食推荐平台的 RAG 检索底座（纯本地，零外部依赖）。

模块职责
    chunker.py    菜谱结构化切分（split_text / chunk_recipe / iter_all_chunks）
    embedder.py   向量化：默认本地哈希 TF-IDF（1024 维），可选本机 Ollama（768 维，自动回退）
    bm25.py       自实现 BM25（k1=1.5 / b=0.75），中文单字 + char bigram + 英文数字 token
    store.py      向量库落盘（vectors.npy / meta.json / bm25.json / manifest.json）
    retriever.py  混合检索（向量 ⊕ BM25 ⊕ 标题，RRF 融合）+ 索引构建门面

约束
    - 默认 backend 恒为 ``hashing``：不下载模型、不联网；``ollama`` backend 只连本机回环服务，
      失败自动熔断回 hashing；
    - 不引入 scipy / sklearn / faiss / jieba 等硬依赖（可选导入一律 try/except）；
    - 任何检索异常都降级为 ``[]``，绝不把异常抛给上层（``food/services/assistant.py``）；
    - 换向量 backend / 换模型会改变维度（1024 ↔ 768），必须重建索引。

用法
    >>> from rag.retriever import get_retriever
    >>> get_retriever().build_index(force=True)       # 建索引
    >>> hits = get_retriever().search("番茄牛腩怎么做", k=3)
    >>> hits[0].payload["title"], round(hits[0].score, 4)

本包 __init__ 采用 PEP 562 延迟导入：``import rag`` 不会触发 Django 初始化，
只有真正取属性时才 import 子模块，避免 settings 未配置时的导入期错误。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "Chunk",
    "split_text",
    "chunk_recipe",
    "iter_all_chunks",
    "BaseEmbedder",
    "HashingTfidfEmbedder",
    "OllamaEmbedder",
    "FallbackEmbedder",
    "EmbedderUnavailable",
    "get_embedder",
    "reset_embedder",
    "BM25Index",
    "VectorStore",
    "Hit",
    "Retriever",
    "get_retriever",
    "reset_retriever",
]

__version__ = "1.1.0"

# 延迟导入映射：属性名 -> (子模块, 属性名)
_LAZY: dict[str, tuple[str, str]] = {
    "Chunk": ("rag.chunker", "Chunk"),
    "split_text": ("rag.chunker", "split_text"),
    "chunk_recipe": ("rag.chunker", "chunk_recipe"),
    "iter_all_chunks": ("rag.chunker", "iter_all_chunks"),
    "BaseEmbedder": ("rag.embedder", "BaseEmbedder"),
    "HashingTfidfEmbedder": ("rag.embedder", "HashingTfidfEmbedder"),
    "OllamaEmbedder": ("rag.embedder", "OllamaEmbedder"),
    "FallbackEmbedder": ("rag.embedder", "FallbackEmbedder"),
    "EmbedderUnavailable": ("rag.embedder", "EmbedderUnavailable"),
    "get_embedder": ("rag.embedder", "get_embedder"),
    "reset_embedder": ("rag.embedder", "reset_embedder"),
    "BM25Index": ("rag.bm25", "BM25Index"),
    "VectorStore": ("rag.store", "VectorStore"),
    "Hit": ("rag.retriever", "Hit"),
    "Retriever": ("rag.retriever", "Retriever"),
    "get_retriever": ("rag.retriever", "get_retriever"),
    "reset_retriever": ("rag.retriever", "reset_retriever"),
}


def __getattr__(name: str) -> Any:
    """按需导入子模块属性（PEP 562）。"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'rag' has no attribute {name!r}")
    module_name, attr_name = target
    from importlib import import_module

    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + __all__))
