"""向量后端工厂与统一接口。

约定见 docs/架构与接口.md 3.9。两种实现（chroma / npz）都满足同一套方法签名，
检索层只依赖这里的抽象接口。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np


class VectorBackend(ABC):
    """向量后端抽象接口。"""

    @abstractmethod
    def add(self, ids: list[str], vectors: np.ndarray,
            metadatas: list[dict]) -> None:
        """批量写入向量（重复 id 应覆盖或忽略，由实现决定）。"""

    @abstractmethod
    def search(self, vector: np.ndarray, top_k: int,
               where: dict | None = None) -> list[tuple[str, float]]:
        """按余弦相似度返回 ``[(id, score), ...]``，score 越大越相似。"""

    @abstractmethod
    def vectors_for(self, ids: list[str]) -> dict[str, np.ndarray]:
        """按 id 取回向量；不存在的 id 不返回。"""

    @abstractmethod
    def count(self) -> int:
        """向量条数。"""

    @abstractmethod
    def reset(self) -> None:
        """清空后端（发布时重建集合用）。"""


def create_backend(name: str, release_dir: Path, collection: str) -> VectorBackend:
    """按名字创建后端：``npz`` 读发布目录，``chroma`` 连持久化集合。"""
    key = str(name or '').strip().lower()
    if key == 'npz':
        from app.retrieval.backends.npz import NpzBackend
        return NpzBackend(Path(release_dir))
    if key == 'chroma':
        from app.retrieval.backends.chroma import ChromaBackend
        return ChromaBackend(str(collection))
    raise ValueError(f'不支持的向量后端：{name}（可选 chroma / npz）')


__all__ = ['VectorBackend', 'create_backend']
