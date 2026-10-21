"""GET /api/info：前端首屏需要的服务与知识库状态。"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter

from app import config
from app.core.runtime import runtime

router = APIRouter()

READY_CACHE_SECONDS = 10.0

_ready_lock = threading.Lock()
_ready_cache: dict = {'key': None, 'at': 0.0, 'ok': False, 'error': None}


def reset_ready_cache() -> None:
    """清空 model_ready 缓存（测试和服务状态变化时调用）。"""
    with _ready_lock:
        _ready_cache.update(key=None, at=0.0, ok=False, error=None)


def _llm_key(llm) -> str:
    return f"{getattr(llm, 'base_url', '')}|{getattr(llm, 'model', '')}"


def _model_ready(llm) -> tuple[bool, str | None]:
    """10 秒内复用 ready() 结果，避免每次打开页面都等健康检查。"""
    key = _llm_key(llm)
    now = time.monotonic()
    with _ready_lock:
        if _ready_cache['key'] == key and now - float(_ready_cache['at']) < READY_CACHE_SECONDS:
            return bool(_ready_cache['ok']), _ready_cache['error']

    try:
        ok = bool(llm.ready())
        if ok:
            error = None
        else:
            error = getattr(llm, 'last_error', '') or f'生成模型不可用：{key} 未响应健康检查'
    except Exception as exc:  # ready() 约定不抛，但别让首屏因它 500
        ok = False
        error = f'生成模型不可用：{type(exc).__name__}: {exc}'

    with _ready_lock:
        _ready_cache.update(key=key, at=time.monotonic(), ok=ok, error=error)
    return ok, error


def _base_info() -> dict:
    try:
        payload = runtime.info()
    except Exception as exc:  # 发布清单损坏时仍要能打开页面
        active = None
        try:
            from app.core import release
            active = release.read_active()
        except Exception:
            active = None
        payload = {
            'name': config.project_info().get('name', 'PolicyRAG'),
            'version': config.project_info().get('version', '0.0.0'),
            'release_id': active['id'] if active else None,
            'release_published_at': active.get('published_at') if active else None,
            'doc_count': 0,
            'unit_count': 0,
            'vector_backend': config.VECTOR_BACKEND,
            'embedding_model': config.EMBEDDING_DIR.name,
            'llm_model': config.LLM_MODEL,
            'device': config.resolve_device(),
            'error': f'{type(exc).__name__}: {exc}',
        }
    return payload


@router.get('/info')
def api_info() -> dict:
    payload = _base_info()

    ready = False
    llm_error: str | None = None
    try:
        llm = runtime.llm()
    except Exception as exc:
        llm_error = f'生成模型不可用：{type(exc).__name__}: {exc}'
    else:
        ready, llm_error = _model_ready(llm)

    errors = [value for value in (payload.get('error'), llm_error) if value]
    return {
        'name': payload.get('name', 'PolicyRAG'),
        'version': payload.get('version', '0.0.0'),
        'release_id': payload.get('release_id'),
        'release_published_at': payload.get('release_published_at'),
        'doc_count': payload.get('doc_count', 0),
        'unit_count': payload.get('unit_count', 0),
        'vector_backend': payload.get('vector_backend', config.VECTOR_BACKEND),
        'embedding_model': payload.get('embedding_model', config.EMBEDDING_DIR.name),
        'llm_model': payload.get('llm_model', config.LLM_MODEL),
        'device': payload.get('device', config.resolve_device()),
        'model_ready': ready,
        'history_messages': config.MAX_HISTORY_MESSAGES,
        'automatic_entry': False,
        'questions': [],
        'error': '；'.join(errors) if errors else None,
    }
