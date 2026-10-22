"""装配层（组合根）：把发布存储、检索器、生成客户端组装成运行时单例。

这是唯一允许「跨层拼装」的地方：上层（api/qa）通过它拿依赖，
下层模块之间不互相 import。发布变化时自动热重载。
"""
from __future__ import annotations

import threading

from app import config
from app.store import releases as release


class Runtime:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._retriever = None
        self._release_id: str | None = None
        self._llm = None

    # ---- 检索器 ----
    def retriever(self, force: bool = False):
        with self._lock:
            active = release.read_active()
            if active is None:
                return None
            if force or self._retriever is None or active['id'] != self._release_id:
                from app.retrieval.engine import PolicyRetriever
                self._retriever = PolicyRetriever(
                    release_dir=release.release_dir(active),
                    backend=config.VECTOR_BACKEND,
                    device=config.resolve_device(),
                    catalog_path=config.CATALOG_DB,
                )
                self._release_id = active['id']
            return self._retriever

    def release_id(self) -> str | None:
        active = release.read_active()
        return active['id'] if active else None

    # ---- 生成客户端 ----
    def llm(self):
        with self._lock:
            if self._llm is None:
                from app.llm.client import ChatClient
                self._llm = ChatClient(
                    base_url=config.LLM_BASE_URL,
                    model=config.LLM_MODEL,
                    api_key=config.LLM_API_KEY,
                    thinking=config.LLM_THINKING,
                )
            return self._llm

    def info(self) -> dict:
        active = release.read_active()
        retriever = None
        error = None
        try:
            retriever = self.retriever()
        except Exception as exc:  # 索引损坏时页面仍要能打开
            error = f'{type(exc).__name__}: {exc}'
        manifest = release.load_manifest(active) if active else {}
        return {
            'name': config.project_info().get('name', 'PolicyRAG'),
            'version': config.project_info().get('version', '0.0.0'),
            'release_id': active['id'] if active else None,
            'release_published_at': active.get('published_at') if active else None,
            'doc_count': manifest.get('doc_count', 0),
            'unit_count': manifest.get('unit_count', 0),
            'vector_backend': config.VECTOR_BACKEND,
            'embedding_model': config.EMBEDDING_DIR.name,
            'llm_model': config.LLM_MODEL,
            'device': retriever.device if retriever else config.resolve_device(),
            'cache': (retriever.cache.stats()
                      if retriever is not None and getattr(retriever, 'cache', None) is not None
                      else None),
            'error': error,
        }


runtime = Runtime()
