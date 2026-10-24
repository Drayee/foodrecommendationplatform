"""向量库落盘模块（store）。

职责
    把「块 id → 向量 + payload」与 BM25 倒排索引持久化到 ``settings.RAG["INDEX_DIR"]``：
        vectors.npy    float32 归一化矩阵 (n, dim)
        meta.json      ids / payloads / model / dim / size / updated_at
        bm25.json      BM25 倒排索引（:meth:`rag.bm25.BM25Index.to_dict`）
        manifest.json  构建清单（版本、签名、文件大小与 mtime），供热重载与排障
    :meth:`VectorStore.load` 失败只返回 ``False`` 并记 warning，绝不抛给调用方。

设计取舍
    - 全部写盘走「临时文件 + ``os.replace``」，避免热重载进程读到半个文件；
    - payload 里带 ``text`` 字段，BM25 直接用它建索引，省去再查一次数据库；
    - ``fingerprint()`` 汇总四个文件的 mtime，``Retriever`` 靠它判断是否需要重新加载；
    - 向量在 ``build`` 时再做一次 L2 归一化（防御性：外部传入未归一化向量也能余弦检索）。
"""
from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from rag.bm25 import BM25Index

logger = logging.getLogger("rag.store")

STORE_VERSION = 1
VECTORS_FILE = "vectors.npy"
META_FILE = "meta.json"
BM25_FILE = "bm25.json"
MANIFEST_FILE = "manifest.json"
_ALL_FILES = (VECTORS_FILE, META_FILE, BM25_FILE, MANIFEST_FILE)


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写：先写 ``*.tmp`` 再 ``os.replace``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON（UTF-8，保留中文）。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(path, raw)


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化；零向量行保持全 0。"""
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32, copy=False)


class VectorStore:
    """向量 + BM25 的落盘存储。

    >>> store = VectorStore("/tmp/index")
    >>> store.build(["recipe:1:0"], np.ones((1, 8), dtype=np.float32),
    ...             [{"text": "番茄牛腩", "title": "番茄牛腩", "recipe_id": 1}])
    >>> store.save()
    >>> VectorStore("/tmp/index").load()
    True
    """

    def __init__(self, index_dir: str | Path) -> None:
        self.index_dir = Path(index_dir)
        self.ids: list[str] = []
        self.matrix: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self.payloads: dict[str, dict[str, Any]] = {}
        self.bm25: BM25Index = BM25Index()
        self.model: str = ""
        self.updated_at: str = ""
        self.manifest: dict[str, Any] = {}
        # Retriever 写盘前可注入签名等清单信息
        self.manifest_extra: dict[str, Any] = {}

    # -- 构建 -------------------------------------------------------------
    def build(self, ids: list[str], vectors: "np.ndarray", payloads: list[dict]) -> None:
        """用内存数据构建索引（不落盘，需再调 :meth:`save`）。

        :param ids: 块 id 列表
        :param vectors: ``(n, dim)`` float32 矩阵
        :param payloads: 与 ids 对齐的 payload 列表，必须含 ``text`` 字段（BM25 用）
        """
        ids = [str(x) for x in (ids or [])]
        payloads = list(payloads or [])
        if len(ids) != len(payloads):
            raise ValueError(f"ids({len(ids)}) 与 payloads({len(payloads)}) 长度不一致")

        matrix = np.asarray(vectors if vectors is not None else [], dtype=np.float32)
        if matrix.ndim != 2:
            matrix = np.zeros((0, 0), dtype=np.float32)
        if matrix.shape[0] != len(ids):
            raise ValueError(f"vectors 行数({matrix.shape[0]}) 与 ids({len(ids)}) 不一致")
        matrix = _normalize_rows(matrix)

        self.ids = ids
        self.matrix = matrix
        self.payloads = {chunk_id: dict(payload) for chunk_id, payload in zip(ids, payloads)}
        self.bm25 = BM25Index()
        self.bm25.add_many(
            (chunk_id, str(self.payloads.get(chunk_id, {}).get("text", ""))) for chunk_id in ids
        )
        self.model = str(
            next((p.get("model") for p in payloads if p.get("model")), "")
            or self.manifest.get("model", "")
        )
        self.updated_at = _now_iso()

    # -- 落盘 -------------------------------------------------------------
    def save(self) -> None:
        """写 vectors.npy / meta.json / bm25.json / manifest.json（原子替换）。"""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        size = len(self.ids)
        dim = int(self.matrix.shape[1]) if self.matrix.ndim == 2 else 0
        self.updated_at = self.updated_at or _now_iso()

        buffer = io.BytesIO()
        np.save(buffer, np.ascontiguousarray(self.matrix, dtype=np.float32), allow_pickle=False)
        _atomic_write_bytes(self.index_dir / VECTORS_FILE, buffer.getvalue())

        _atomic_write_json(
            self.index_dir / META_FILE,
            {
                "store_version": STORE_VERSION,
                "ids": self.ids,
                "payloads": [self.payloads.get(chunk_id, {}) for chunk_id in self.ids],
                "model": self.model,
                "dim": dim,
                "size": size,
                "updated_at": self.updated_at,
            },
        )
        _atomic_write_json(self.index_dir / BM25_FILE, self.bm25.to_dict())

        files: dict[str, dict[str, Any]] = {}
        for name in (VECTORS_FILE, META_FILE, BM25_FILE):
            path = self.index_dir / name
            try:
                stat = path.stat()
                files[name] = {"bytes": stat.st_size, "mtime": round(stat.st_mtime, 3)}
            except OSError:  # pragma: no cover - 刚写完必然存在
                files[name] = {}
        manifest = {
            "store_version": STORE_VERSION,
            "kind": "rag-vector-store",
            "size": size,
            "dim": dim,
            "model": self.model,
            "updated_at": self.updated_at,
            "bm25_docs": len(self.bm25),
            "files": files,
            **self.manifest_extra,
        }
        _atomic_write_json(self.index_dir / MANIFEST_FILE, manifest)
        self.manifest = manifest
        logger.info("索引已保存：%s（%s 块 / %s 维 / %s）", self.index_dir, size, dim, self.model)

    # -- 加载 -------------------------------------------------------------
    def load(self) -> bool:
        """加载索引；任何问题（文件缺失、损坏、维度不符）都返回 ``False`` 并降级。"""
        try:
            meta_path = self.index_dir / META_FILE
            vectors_path = self.index_dir / VECTORS_FILE
            if not meta_path.is_file() or not vectors_path.is_file():
                logger.debug("索引文件不存在，跳过加载：%s", self.index_dir)
                return False

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ids = [str(x) for x in (meta.get("ids") or [])]
            payload_list = list(meta.get("payloads") or [])
            if len(ids) != len(payload_list):
                raise ValueError("meta.json 非法：ids 与 payloads 长度不一致")

            with open(vectors_path, "rb") as handle:
                matrix = np.load(handle, allow_pickle=False)
            matrix = np.asarray(matrix, dtype=np.float32)
            if matrix.ndim != 2:
                raise ValueError("vectors.npy 维度非法")
            if matrix.shape[0] != len(ids):
                raise ValueError("vectors.npy 行数与 ids 不一致")

            bm25_path = self.index_dir / BM25_FILE
            bm25 = BM25Index.from_dict(json.loads(bm25_path.read_text(encoding="utf-8"))) if bm25_path.is_file() else BM25Index()
            if len(bm25) != len(ids):
                # BM25 与向量不同步时重建（payload 里带 text，可自愈）
                logger.warning("bm25 文档数与向量数不一致（%s != %s），按 payload 重建", len(bm25), len(ids))
                bm25 = BM25Index()
                bm25.add_many(
                    (chunk_id, str(payload.get("text", ""))) for chunk_id, payload in zip(ids, payload_list)
                )

            manifest: dict[str, Any] = {}
            manifest_path = self.index_dir / MANIFEST_FILE
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    logger.warning("manifest.json 解析失败，忽略：%s", exc)

            self.ids = ids
            self.matrix = _normalize_rows(matrix)
            self.payloads = {chunk_id: dict(payload) for chunk_id, payload in zip(ids, payload_list)}
            self.bm25 = bm25
            self.model = str(meta.get("model") or manifest.get("model") or "")
            self.updated_at = str(meta.get("updated_at") or manifest.get("updated_at") or "")
            self.manifest = manifest
            logger.debug("索引已加载：%s（%s 块）", self.index_dir, len(ids))
            return True
        except Exception as exc:
            logger.warning("索引加载失败（%s），按空索引处理：%s", self.index_dir, exc)
            self._reset_state()
            return False

    def _reset_state(self) -> None:
        """清空内存状态（不动磁盘）。"""
        self.ids = []
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.payloads = {}
        self.bm25 = BM25Index()
        self.model = ""
        self.updated_at = ""
        self.manifest = {}

    # -- 查询 -------------------------------------------------------------
    def search(self, query_vec: "np.ndarray", k: int = 10) -> list[tuple[str, float]]:
        """余弦相似度 top-k；向量库为空或维度不符时返回 ``[]``。"""
        if not self.ids or self.matrix.size == 0:
            return []
        vector = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        dim = self.matrix.shape[1]
        if vector.shape[0] != dim:
            logger.warning("查询向量维度 %s 与索引维度 %s 不一致", vector.shape[0], dim)
            return []
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            return []
        vector = vector / norm
        try:
            k = max(int(k), 1)
        except (TypeError, ValueError):  # pragma: no cover
            k = 10
        k = min(k, len(self.ids))

        similarity = self.matrix @ vector
        if k < similarity.shape[0]:
            candidate = np.argpartition(-similarity, k - 1)[:k]
        else:
            candidate = np.arange(similarity.shape[0])
        order = candidate[np.argsort(-similarity[candidate], kind="stable")]
        return [(self.ids[int(idx)], round(float(similarity[int(idx)]), 6)) for idx in order]

    def get_payload(self, chunk_id: str) -> dict:
        """取某个块的 payload；不存在返回空 dict。"""
        payload = self.payloads.get(str(chunk_id))
        return dict(payload) if payload else {}

    # -- 元信息 -----------------------------------------------------------
    def stats(self) -> dict:
        """``{"size", "dim", "updated_at", "model"}``（+ 便于排障的附加字段）。"""
        dim = int(self.matrix.shape[1]) if self.matrix.ndim == 2 and self.matrix.size else int(
            self.manifest.get("dim", 0) or 0
        )
        return {
            "size": len(self.ids),
            "dim": dim,
            "updated_at": self.updated_at,
            "model": self.model,
            "bm25_docs": len(self.bm25),
            "index_dir": str(self.index_dir),
        }

    def fingerprint(self) -> float:
        """索引文件的最新 mtime；无文件时返回 ``0.0``。用于热重载判断。"""
        latest = 0.0
        for name in _ALL_FILES:
            try:
                latest = max(latest, (self.index_dir / name).stat().st_mtime)
            except OSError:
                continue
        return latest

    # -- 清理 -------------------------------------------------------------
    def clear(self) -> None:
        """删除磁盘索引文件并清空内存状态（幂等）。"""
        self._reset_state()
        for name in _ALL_FILES:
            path = self.index_dir / name
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:  # pragma: no cover - 权限/占用
                logger.warning("删除索引文件失败 %s：%s", path, exc)

    def __len__(self) -> int:
        """已索引的块数量。"""
        return len(self.ids)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<VectorStore dir={self.index_dir} size={len(self.ids)} model={self.model!r}>"
