"""POST /api/ask：NDJSON 流式问答（契约 4）。

事件顺序：routing → query/vectors/keywords/evidence（检索阶段转发）→ prompt →
generating → answer（增量）→ sources → done；任何失败都发 error 事件，
HTTP 状态保持 200，避免前端解析到裸断的连接。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import config
from app.core.runtime import runtime
from app.llm import prompts
from app.llm.routing import route, rewrite

router = APIRouter()

# 单次回答的字符上限，超过即截断并在 done 事件里标记 output_truncated。
MAX_ANSWER_CHARS = 8000


class AskRequest(BaseModel):
    question: str = ''
    history: list[dict] = Field(default_factory=list)
    run_tag: str | None = None


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + '\n'


def _error_message(exc: BaseException) -> str:
    text = str(exc).strip()
    if isinstance(exc, RuntimeError) and text.startswith('生成模型不可用：'):
        return text
    if not text:
        text = type(exc).__name__
    return f'问答失败：{text}'


def _titles(result: dict, limit: int = 8) -> list[str]:
    """把检索命中整理成前端可读的标题列表。"""
    titles: list[str] = []
    for row in (result.get('results') or [])[:limit]:
        if not isinstance(row, dict):
            continue
        citation = row.get('citation') or {}
        unit = row.get('unit') or {}
        title = str(citation.get('title') or unit.get('doc_title') or '').strip()
        if not title:
            continue
        article = str(citation.get('article') or unit.get('article') or '').strip()
        titles.append(f'《{title}》{article}' if article else f'《{title}》')
    return titles


def _source_items(result: dict) -> list[dict]:
    """sources 事件条目，字段严格按契约第 4 节。"""
    items: list[dict] = []
    for index, row in enumerate(result.get('results') or [], start=1):
        if not isinstance(row, dict):
            continue
        unit = row.get('unit') or {}
        citation = row.get('citation') or {}
        source = unit.get('source') or {}
        pages = unit.get('pages') or citation.get('pages') or source.get('pages') or []
        pages = [int(page) for page in pages if isinstance(page, (int, float))]
        focus_page = pages[0] if pages else citation.get('page') or source.get('page') or 1
        try:
            focus_page = int(focus_page)
        except (TypeError, ValueError):
            focus_page = 1
        items.append({
            'id': index,
            'doc_id': str(unit.get('doc_id') or citation.get('doc_id') or ''),
            'file': str(source.get('file') or citation.get('file') or ''),
            'title': str(unit.get('doc_title') or citation.get('title') or ''),
            'pages': pages,
            'focus_page': focus_page,
            'text': str(unit.get('text') or row.get('evidence') or ''),
            'article': str(citation.get('article') or unit.get('article') or ''),
            'chapter': str(citation.get('chapter') or unit.get('chapter') or ''),
            'doc_no': str(unit.get('doc_no') or citation.get('doc_no') or ''),
            'version_label': str(unit.get('version_label') or citation.get('version_label') or ''),
        })
    return items


class _AnswerStats:
    """记录本次生成的字符数与是否被截断。"""

    def __init__(self) -> None:
        self.chars = 0
        self.truncated = False


def _answer_lines(llm, messages: list[dict], stats: _AnswerStats) -> Iterator[str]:
    """把模型增量转换成 answer 事件，超过上限即停止生成。"""
    for delta in llm.stream(messages, max_tokens=1024, temperature=0.2):
        if not delta:
            continue
        remaining = MAX_ANSWER_CHARS - stats.chars
        if remaining <= 0:
            stats.truncated = True
            return
        text = delta if len(delta) <= remaining else delta[:remaining]
        stats.chars += len(text)
        stats.truncated = stats.chars >= MAX_ANSWER_CHARS
        yield _line({'stage': 'answer', 'text': text})
        if stats.truncated:
            return


def _done(started: float, truncated: bool) -> dict:
    return {'stage': 'done', 'seconds': round(time.monotonic() - started, 2),
            'output_truncated': bool(truncated)}


def _search(retriever, question: str, rewritten: str) -> Iterator[tuple[str, str, object]]:
    """在线程里跑检索，把 on_stage 事件通过队列转发出来。

    产出 ("stage", 阶段名, payload) / ("result", "result", 检索结果) / ("error", ...)。
    检索器用改写后的问题检索，返回值与 query 事件里仍保留用户原问题。
    """
    events: queue.Queue = queue.Queue()
    finished = threading.Event()

    def on_stage(stage: str, payload: dict | None = None) -> None:
        events.put(('stage', str(stage), dict(payload or {})))

    def worker() -> None:
        try:
            result = retriever.search(question, top_k=config.TOP_K,
                                      filters={'rewritten': rewritten},
                                      on_stage=on_stage)
            events.put(('result', 'result', result))
        except Exception as exc:  # 交给主生成器发 error 事件
            events.put(('error', 'error', exc))
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name='policy-search', daemon=True)
    thread.start()
    while True:
        try:
            kind, name, payload = events.get(timeout=0.5)
        except queue.Empty:
            if finished.is_set() and events.empty():
                yield ('error', 'error', RuntimeError('检索线程异常退出，未返回结果'))
                return
            continue
        yield (kind, name, payload)
        if kind != 'stage':
            return


def _chat_flow(llm, question: str, history: list[dict], started: float) -> Iterator[str]:
    messages = prompts.build_chat_messages(question, history)
    yield _line({'stage': 'prompt', 'messages': messages})
    yield _line({'stage': 'generating'})

    stats = _AnswerStats()
    try:
        for line in _answer_lines(llm, messages, stats):
            yield line
    except Exception as exc:
        yield _line({'stage': 'error', 'message': _error_message(exc)})
        return
    if stats.chars == 0 and not stats.truncated:
        yield _line({'stage': 'error', 'message': '生成模型未返回任何内容，请稍后重试。'})
        return
    yield _line({'stage': 'sources', 'items': []})
    yield _line(_done(started, stats.truncated))


def _policy_flow(llm, question: str, rewritten: str, history: list[dict],
                 started: float) -> Iterator[str]:
    # ---- 检索器（无发布版本时立即给出明确错误）----
    try:
        retriever = runtime.retriever()
    except Exception as exc:
        yield _line({'stage': 'error',
                     'message': f'知识库索引加载失败：{type(exc).__name__}: {exc}'})
        return
    if retriever is None:
        yield _line({'stage': 'error',
                     'message': '知识库尚未建立，请先上传制度文件并发布后再提问。'})
        return

    result: dict | None = None
    seen: set[str] = set()
    failure: BaseException | None = None
    for kind, stage, payload in _search(retriever, question, rewritten):
        if kind == 'stage':
            seen.add(stage)
            event = {'stage': stage}
            if isinstance(payload, dict):
                event.update(payload)
            yield _line(event)
        elif kind == 'result':
            result = payload if isinstance(payload, dict) else {}
        else:
            failure = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
            break

    if failure is not None:
        yield _line({'stage': 'error', 'message': f'制度检索失败：{failure}'})
        return
    if result is None:
        yield _line({'stage': 'error', 'message': '制度检索未返回结果，请稍后重试。'})
        return

    if 'query' not in seen:
        yield _line({'stage': 'query', 'question': question,
                     'rewritten': str(result.get('rewritten') or rewritten),
                     'filters': result.get('filters') or {}})
    if 'evidence' not in seen:
        yield _line({'stage': 'evidence', 'titles': _titles(result),
                     'notes': result.get('notes') or []})

    # ---- 提示词与生成 ----
    messages = prompts.build_messages(question, str(result.get('context') or ''), history)
    yield _line({'stage': 'prompt', 'messages': messages})
    yield _line({'stage': 'generating'})

    stats = _AnswerStats()
    try:
        for line in _answer_lines(llm, messages, stats):
            yield line
    except Exception as exc:
        yield _line({'stage': 'error', 'message': _error_message(exc)})
        return
    if stats.chars == 0 and not stats.truncated:
        yield _line({'stage': 'error', 'message': '生成模型未返回任何内容，请稍后重试。'})
        return

    yield _line({'stage': 'sources', 'items': _source_items(result)})
    yield _line(_done(started, stats.truncated))


def _events(request: AskRequest) -> Iterator[str]:
    started = time.monotonic()
    question = (request.question or '').strip()
    history = prompts.normalize_history(request.history)

    yield _line({'stage': 'routing'})

    llm = None
    ready_error = ''
    try:
        llm = runtime.llm()
        if not llm.ready():
            ready_error = getattr(llm, 'last_error', '') or '生成模型不可用：健康检查未通过'
            llm = None
    except Exception as exc:
        ready_error = f'生成模型不可用：{type(exc).__name__}: {exc}'
        llm = None

    if llm is None:
        # 模型不可用时先确认知识库状态：没有发布版本时，缺知识库才是更直接的原因。
        has_release = False
        try:
            has_release = runtime.release_id() is not None
        except Exception:
            has_release = False
        if not has_release:
            # 没有发布指针时 runtime.retriever() 会立即返回 None，不会加载索引。
            try:
                has_release = runtime.retriever() is not None
            except Exception:
                has_release = False
        if not has_release:
            yield _line({'stage': 'error',
                         'message': '知识库尚未建立，请先上传制度文件并发布后再提问。'})
            return
        yield _line({'stage': 'error',
                     'message': ready_error or '生成模型不可用：请检查模型服务配置'})
        return

    decision = route(question, history, llm)
    mode = str(decision.get('mode') or 'policy')
    rewritten = str(decision.get('search_query') or question)

    if mode == 'chat':
        yield from _chat_flow(llm, question, history, started)
        return

    # 只有多轮对话才需要改写补全；首轮直接检索，省一次模型调用。
    if history:
        try:
            rewritten = rewrite(question, history, llm) or rewritten
        except Exception:
            rewritten = rewritten or question
    yield from _policy_flow(llm, question, rewritten, history, started)


def _generate(request: AskRequest) -> Iterator[str]:
    try:
        yield from _events(request)
    except Exception as exc:  # 兜底：任何未预期异常都转成 error 事件
        yield _line({'stage': 'error', 'message': _error_message(exc)})


@router.post('/ask')
def api_ask(payload: AskRequest) -> StreamingResponse:
    question = (payload.question or '').strip()
    if not question:
        raise HTTPException(status_code=400, detail='问题不能为空')
    return StreamingResponse(
        _generate(payload),
        media_type='application/x-ndjson',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
