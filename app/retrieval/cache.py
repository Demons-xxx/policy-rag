"""检索结果 LRU 缓存（T9）。

缓存键 = release_id + 规范化问题 + 过滤条件 + 基准日 + top_k，因此：
- 同问、同参数第二次直接命中，跳过编码与融合；
- 换 release、换过滤条件、换基准日自然失效；
- 容量默认 256（`RAG_CACHE_SIZE`），超出按最久未用淘汰。

返回的是缓存深拷贝，调用方修改结果不会污染缓存。
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from typing import Any


def normalize_key_text(text: str) -> str:
    """缓存键的文本归一化：去首尾空白、压缩空白、忽略大小写。"""
    return ' '.join(str(text or '').split()).lower()


class ResultCache:
    """带命中统计的 LRU 缓存。"""

    def __init__(self, size: int = 256) -> None:
        self.size = max(1, int(size or 1))
        self._data: OrderedDict[tuple, dict] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> dict | None:
        if key not in self._data:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return copy.deepcopy(self._data[key])

    def put(self, key: tuple, value: dict) -> None:
        self._data[key] = copy.deepcopy(value)
        self._data.move_to_end(key)
        while len(self._data) > self.size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            'size': len(self._data),
            'capacity': self.size,
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': round(self.hits / total, 4) if total else 0.0,
        }


def make_key(release_id: str, question: str, filters: dict | None,
             as_of: str, top_k: int) -> tuple:
    """构造缓存键：过滤条件按 key 排序，保证顺序无关。"""
    items = tuple(sorted(
        (str(name), str(value))
        for name, value in (filters or {}).items()
        if value not in (None, '', [], {})
    ))
    return (str(release_id), normalize_key_text(question), items, str(as_of), int(top_k))


__all__ = ['ResultCache', 'make_key', 'normalize_key_text']
