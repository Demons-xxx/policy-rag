"""npz 向量后端：直接读发布目录的 ``vectors.npz``，零外部依赖回退方案。

``ids`` 与 passages 顺序一致，``vectors`` 已 L2 归一化，所以检索就是一次矩阵乘。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from app.retrieval.backends import VectorBackend


class NpzBackend(VectorBackend):
    def __init__(self, release_dir: Path) -> None:
        self.release_dir = Path(release_dir)
        self.path = self.release_dir / 'vectors.npz'
        if not self.path.is_file():
            raise FileNotFoundError(f'发布目录缺少 vectors.npz：{self.path}')
        with np.load(self.path, allow_pickle=False) as data:
            self._ids = [str(value) for value in data['ids'].tolist()]
            vectors = np.asarray(data['vectors'], dtype=np.float32)
        if vectors.ndim != 2:
            raise ValueError(f'vectors.npz 形状非法：{vectors.shape}')
        if len(self._ids) != vectors.shape[0]:
            raise ValueError(
                f'vectors.npz 的 ids 数量（{len(self._ids)}）与向量行数（{vectors.shape[0]}）不一致'
            )
        # 防御性再归一化：即使发布侧忘记归一化，矩阵乘也等价于余弦相似度
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        self._vectors = np.ascontiguousarray(vectors / norms, dtype=np.float32)
        self._index = {pid: row for row, pid in enumerate(self._ids)}
        self.dimension = int(self._vectors.shape[1]) if self._vectors.size else 0

    # -- 接口 ----------------------------------------------------------
    def add(self, ids: list[str], vectors: np.ndarray,
            metadatas: list[dict]) -> None:
        """npz 是只读后端，发布时整体重写，因此写入为空实现。"""

    def search(self, vector: np.ndarray, top_k: int,
               where: dict | None = None) -> list[tuple[str, float]]:
        total = len(self._ids)
        if total == 0:
            return []
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        if query.shape[0] != self.dimension:
            raise ValueError(
                f'查询向量维度 {query.shape[0]} 与索引维度 {self.dimension} 不一致'
            )
        scores = self._vectors @ query
        limit = max(1, min(int(top_k or 1), total))
        # argpartition 取 top-k，再按分数从高到低排序
        candidate = np.argpartition(-scores, limit - 1)[:limit]
        order = candidate[np.argsort(-scores[candidate], kind='stable')]
        # npz 无元数据，where 由检索层在拿到 passage/unit 后自行过滤
        return [(self._ids[int(row)], float(scores[int(row)])) for row in order]

    def vectors_for(self, ids: list[str]) -> dict[str, np.ndarray]:
        result: dict[str, np.ndarray] = {}
        for pid in ids or []:
            row = self._index.get(str(pid))
            if row is not None:
                result[str(pid)] = self._vectors[row].copy()
        return result

    def count(self) -> int:
        return len(self._ids)

    def reset(self) -> None:
        """只读后端，清空无意义。"""
