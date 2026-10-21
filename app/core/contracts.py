"""跨层接口契约（Protocol + 轻量数据类）。

目的：让「检索/生成/解析/存储」这些部件可以互相替换。
业务代码依赖本文件的契约，而不是具体实现；测试里的假实现也实现同一组契约。

约定：内部数据仍以 dict 传递（与 units.jsonl / passages.jsonl 一致），
契约只约束「方法签名 + 必须存在的字段」，不强制 dataclass 落盘。
契约与真实实现保持一致，改实现时应同步改这里（tests/test_contracts.py 兜底）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, Sequence, runtime_checkable

import numpy as np

# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    """一次检索请求：问题 + 基准日期 + 可选的指定文号。"""

    text: str
    as_of: str = ''
    explicit_doc_key: str | None = None


@dataclass(frozen=True)
class Citation:
    """给用户看的引用条目（API `sources` 字段的结构）。"""

    unit_id: str
    doc_id: str
    doc_title: str = ''
    article_no: str = ''
    version_label: str = ''
    status: str = 'active'
    page_from: int | None = None
    page_to: int | None = None
    note: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            'unit_id': self.unit_id,
            'doc_id': self.doc_id,
            'doc_title': self.doc_title,
            'article_no': self.article_no,
            'version_label': self.version_label,
            'status': self.status,
        }
        if self.page_from:
            data['page'] = self.page_from
        if self.page_to and self.page_to != self.page_from:
            data['page_to'] = self.page_to
        if self.note:
            data['note'] = self.note
        if self.metadata:
            data['metadata'] = self.metadata
        return data


@dataclass
class RetrievalOutcome:
    """检索结果：证据文本 + 引用 + 诊断信息，供问答层与调试接口共用。"""

    results: list[dict] = field(default_factory=list)
    context: str = ''
    notes: list[str] = field(default_factory=list)
    stages: dict = field(default_factory=dict)
    max_dense: float = 0.0


# ---------------------------------------------------------------------------
# 部件契约（与真实实现逐项对应）
# ---------------------------------------------------------------------------


@runtime_checkable
class Encoder(Protocol):
    """文本 → 向量。实现：app/retrieval/encoder.py:BgeEncoder。"""

    dimension: int

    def encode(self, texts: list[str], query: bool = False,
               batch_size: int = 32) -> np.ndarray: ...

    def count_tokens(self, text: str) -> int: ...


@runtime_checkable
class VectorIndex(Protocol):
    """向量后端。实现：app/retrieval/backends/{chroma,npz}.py。"""

    def add(self, ids: list[str], vectors: np.ndarray, metadatas: list[dict]) -> None: ...

    def search(self, vector: np.ndarray, top_k: int,
               where: dict | None = None) -> list[tuple[str, float]]: ...

    def vectors_for(self, ids: list[str]) -> dict[str, np.ndarray]: ...

    def count(self) -> int: ...

    def reset(self) -> None: ...


@runtime_checkable
class LexicalIndex(Protocol):
    """词法索引。实现：app/retrieval/lexical.py:PostingBM25（分数由调用方排序）。"""

    def get_scores(self, tokens: list[str]) -> np.ndarray: ...

    def __len__(self) -> int: ...


@runtime_checkable
class Retriever(Protocol):
    """问答层唯一依赖的检索入口。实现：app/retrieval/engine.py:PolicyRetriever。"""

    def search(self, question: str, *, as_of: str = '',
               explicit_doc_key: str | None = None) -> RetrievalOutcome: ...


@runtime_checkable
class LLMClient(Protocol):
    """生成模型。实现：app/llm/client.py:ChatClient。"""

    def stream(self, messages: list[dict], max_tokens: int = 1024,
               temperature: float = 0.2) -> Iterator[str]: ...

    def complete(self, messages: list[dict], max_tokens: int = 512,
                 temperature: float = 0.1) -> str: ...


@runtime_checkable
class DocumentParser(Protocol):
    """文件 → 文本块。实现：app/ingest/parser.py（按扩展名注册）。"""

    def parse(self, path: Path) -> list[dict]: ...


@runtime_checkable
class CatalogStore(Protocol):
    """文档/任务/发布/审计的元数据表。实现：app/store/catalog.py:Catalog。"""

    def upsert_document(self, doc: dict) -> None: ...

    def list_documents(self) -> list[dict]: ...

    def get_document(self, doc_id: str) -> dict | None: ...


@runtime_checkable
class ReleaseStore(Protocol):
    """发布目录与 active 指针。实现：app/store/releases.py。"""

    def read_active(self) -> dict | None: ...

    def write_active(self, record: dict) -> None: ...

    def list_releases(self) -> list[dict]: ...


__all__ = [
    'Citation', 'CatalogStore', 'DocumentParser', 'Encoder', 'LLMClient',
    'LexicalIndex', 'Query', 'ReleaseStore', 'RetrievalOutcome', 'Retriever',
    'VectorIndex',
]
