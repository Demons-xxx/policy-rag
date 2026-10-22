"""POST /api/ask：NDJSON 流式问答（契约 4）。

接口层只做四件事：解析入参 → 取运行时 → 由 ``app.qa.flow.AnswerFlow`` 产出事件 →
编码成 NDJSON。事件名、字段与顺序都在编排器里定义，本文件不再关心问答流程。

事件顺序：routing → query/vectors/keywords/evidence（检索阶段转发）→ prompt →
generating → answer（增量）→ sources → done；任何失败都发 error 事件，
HTTP 状态保持 200，避免前端解析到裸断的连接。
"""
from __future__ import annotations

import json
import time
from typing import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import observability
from app.qa.flow import AnswerEvent, AnswerFlow, AnswerRequest, error_message

router = APIRouter()

NDJSON_MEDIA_TYPE = 'application/x-ndjson'


class AskRequest(BaseModel):
    question: str = ''
    history: list[dict] = Field(default_factory=list)
    run_tag: str | None = None


def encode(event: AnswerEvent) -> str:
    """一行 NDJSON；JSON 一律 UTF-8 原文，不做 ensure_ascii 转义。"""
    return json.dumps(event.to_dict(), ensure_ascii=False) + '\n'


def _generate(payload: AskRequest) -> Iterator[str]:
    """把编排器的事件流编码成 NDJSON 行。

    客户端断开时 StreamingResponse 会关闭本生成器（GeneratorExit），
    因此不需要额外的心跳或探测：停止迭代即停止向后端继续拉取。
    """
    request = AnswerRequest(question=payload.question, history=payload.history,
                            run_tag=payload.run_tag)
    request_id = observability.new_request_id()
    started = time.perf_counter()
    marks: dict[str, float] = {}
    error: str | None = None
    sources = 0
    try:
        for event in AnswerFlow().run(request):
            stage = event.stage
            now = time.perf_counter()
            if stage not in marks:
                marks[stage] = now
            data = event.payload or {}
            if stage == 'error':
                error = str(data.get('message') or '未知错误')
            elif stage == 'sources':
                sources = len(data.get('items') or [])
            yield encode(event)
    except Exception as exc:  # 兜底：编排器未预期的异常也转成 error 事件
        error = error_message(exc)
        yield encode(AnswerEvent('error', {'message': error}))
    finally:
        def delta(first: str, second: str) -> float | None:
            if first in marks and second in marks and marks[second] >= marks[first]:
                return round((marks[second] - marks[first]) * 1000, 1)
            return None

        record = {
            'request_id': request_id,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'question': (payload.question or '')[:200],
            'run_tag': payload.run_tag,
            'route': 'retrieve' if 'query' in marks or 'evidence' in marks else 'chat',
            'routing_ms': delta('routing', 'query'),
            'retrieval_ms': delta('query', 'evidence'),
            'first_token_ms': delta('generating', 'answer'),
            'total_ms': round((time.perf_counter() - started) * 1000, 1),
            'sources': sources,
            'error': error,
        }
        observability.metrics.record(record)
        observability.log_request(record)


@router.post('/ask')
def api_ask(payload: AskRequest) -> StreamingResponse:
    question = (payload.question or '').strip()
    if not question:
        raise HTTPException(status_code=400, detail='问题不能为空')
    return StreamingResponse(
        _generate(payload),
        media_type=NDJSON_MEDIA_TYPE,
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
