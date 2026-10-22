"""GET /api/metrics：问答请求的进程内指标（契约 4 扩展）。"""
from __future__ import annotations

from fastapi import APIRouter

from app import config
from app.api.info import _base_info
from app.observability import metrics

router = APIRouter()


@router.get('/metrics')
def api_metrics() -> dict:
    payload = metrics.snapshot()
    payload.update({
        'release_id': _base_info().get('release_id'),
        'vector_backend': config.VECTOR_BACKEND,
        'llm_model': config.LLM_MODEL,
    })
    return payload
