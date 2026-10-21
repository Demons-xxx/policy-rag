"""Chroma 向量后端：持久化在 ``data/vector_store/``，按发布 ID 建集合。

约定见 docs/架构与接口.md 3.9：集合元数据固定 cosine，写入按 500 条分批，
``search`` 返回 ``(id, 1-distance)``，支持 ``where={"doc_id": {...}}`` 过滤。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from app import config
from app.retrieval.backends import VectorBackend

# 单批写入上限，避免一次性提交过大
_BATCH_SIZE = 500


def _clean_metadata(metadata: dict | None) -> dict:
    """Chroma metadata 只接受 str/int/float/bool，这里做一次清洗。"""
    cleaned: dict = {}
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            cleaned[str(key)] = ','.join(str(item) for item in value)
        elif hasattr(value, 'item'):  # numpy 标量
            cleaned[str(key)] = value.item()
        elif isinstance(value, (str, int, float, bool)):
            cleaned[str(key)] = value
        else:
            cleaned[str(key)] = str(value)
    return cleaned


class ChromaBackend(VectorBackend):
    def __init__(self, collection: str, path: Path | None = None) -> None:
        # 延迟导入：npz 回退和单测不需要加载 chromadb 的重依赖
        import chromadb
        from chromadb.config import Settings

        self.path = Path(path) if path else Path(config.VECTOR_DIR)
        self.path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self.name = str(collection)
        self._collection = self.client.get_or_create_collection(
            name=self.name,
            metadata={'hnsw:space': 'cosine'},
        )

    # -- 接口 ----------------------------------------------------------
    def add(self, ids: list[str], vectors: np.ndarray,
            metadatas: list[dict] | None = None) -> None:
        ids = [str(item) for item in (ids or [])]
        if not ids:
            return
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            raise ValueError('向量矩阵形状与 ids 数量不一致')
        metadata_list = list(metadatas or [])
        for start in range(0, len(ids), _BATCH_SIZE):
            end = min(start + _BATCH_SIZE, len(ids))
            chunk_meta = None
            if metadata_list:
                chunk_meta = [_clean_metadata(item) for item in metadata_list[start:end]]
            # upsert 保证同一 release 重复发布时幂等
            self._collection.upsert(
                ids=ids[start:end],
                embeddings=matrix[start:end].tolist(),
                metadatas=chunk_meta,
            )

    def search(self, vector: np.ndarray, top_k: int,
               where: dict | None = None) -> list[tuple[str, float]]:
        total = self.count()
        if total == 0:
            return []
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        limit = max(1, min(int(top_k or 1), total))
        result = self._collection.query(
            query_embeddings=[query.tolist()],
            n_results=limit,
            where=where or None,
            include=['distances'],
        )
        ids = (result.get('ids') or [[]])[0]
        distances = (result.get('distances') or [[]])[0]
        # cosine 空间下 distance = 1 - cosine similarity
        return [(str(pid), float(1.0 - dist)) for pid, dist in zip(ids, distances)]

    def vectors_for(self, ids: list[str]) -> dict[str, np.ndarray]:
        wanted = [str(item) for item in (ids or [])]
        if not wanted:
            return {}
        result = self._collection.get(ids=wanted, include=['embeddings'])
        got_ids = result.get('ids') or []
        embeddings = result.get('embeddings')
        if embeddings is None:
            return {}
        return {
            str(pid): np.asarray(vec, dtype=np.float32)
            for pid, vec in zip(got_ids, embeddings)
        }

    def count(self) -> int:
        return int(self._collection.count())

    def reset(self) -> None:
        """删集合后立刻重建空集合，后续 add 可直接使用。"""
        try:
            self.client.delete_collection(name=self.name)
        except Exception:
            pass
        self._collection = self.client.get_or_create_collection(
            name=self.name,
            metadata={'hnsw:space': 'cosine'},
        )
