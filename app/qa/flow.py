"""问答编排器（应用层）：路由 → 改写 → 检索 → 提示 → 生成。

本模块只产出 :class:`AnswerEvent`（事件名与字段见 docs/架构与接口.md 第 4 节），
不关心 HTTP、NDJSON 编码与连接管理——那些留在 ``app/api/ask.py``。所有外部依赖
（生成客户端、检索器、发布指针）都通过构造函数注入，因此每个步骤都能用假对象
单独驱动：

    events = list(AnswerFlow(
        llm_getter=lambda: fake_llm,
        retriever_getter=lambda: fake_retriever,
        release_id_getter=lambda: 'rel-test',
    ).run(AnswerRequest(question='危化品怎么领用？')))

事件顺序：routing → query/vectors/keywords/evidence（检索阶段转发）→ prompt →
generating → answer（增量）→ sources → done；任何失败都以 error 事件收尾。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Iterator

from app import config
from app.llm import prompts
from app.llm.routing import route, rewrite

# 单次回答的字符上限，超过即截断并在 done 事件里标记 output_truncated。
MAX_ANSWER_CHARS = 8000
# 生成参数，与重构前保持一致。
STREAM_MAX_TOKENS = 1024
STREAM_TEMPERATURE = 0.2
# 检索线程事件队列的轮询间隔（秒）。
SEARCH_POLL_SECONDS = 0.5

MISSING_KNOWLEDGE_BASE = '知识库尚未建立，请先上传制度文件并发布后再提问。'
MODEL_HEALTH_MESSAGE = '生成模型不可用：健康检查未通过'
MODEL_CONFIG_HINT = '生成模型不可用：请检查模型服务配置'
EMPTY_ANSWER_MESSAGE = '生成模型未返回任何内容，请稍后重试。'
EMPTY_SEARCH_MESSAGE = '制度检索未返回结果，请稍后重试。'


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class AnswerRequest:
    """一次问答请求；与 HTTP 请求体解耦，便于非 HTTP 调用方复用。"""

    question: str = ''
    history: list[dict] = field(default_factory=list)
    run_tag: str | None = None


@dataclass(frozen=True)
class AnswerEvent:
    """契约事件：``stage`` + 该阶段的字段，序列化为 ``{"stage": ..., **payload}``。"""

    stage: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        event = {'stage': self.stage}
        event.update(self.payload)
        return event


@dataclass(frozen=True)
class RouteDecision:
    """路由结论：mode=chat 走闲聊直答，否则进入制度检索。"""

    mode: str = 'policy'
    search_query: str = ''
    reason: str = ''


@dataclass
class AnswerStats:
    """本次生成的字符数与是否被截断。"""

    chars: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class RetrievalStage:
    """检索器 ``on_stage`` 推来的阶段事件，原样转发给前端。"""

    stage: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalDone:
    """检索步骤的终态：要么 ``result``，要么中文 ``error`` 文案。"""

    result: dict | None = None
    error: str = ''
    stages: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# 事件字段整理（契约第 4 节）
# ---------------------------------------------------------------------------


def error_message(exc: BaseException) -> str:
    """把异常转成中文 error 事件文案；模型不可用的原文直接透出。"""
    text = str(exc).strip()
    if isinstance(exc, RuntimeError) and text.startswith('生成模型不可用：'):
        return text
    if not text:
        text = type(exc).__name__
    return f'问答失败：{text}'


def same_text(left: str, right: str) -> bool:
    """忽略标点和空白后比较两句话是否相同。"""

    def norm(text: str) -> str:
        return ''.join(str(text or '').split()).strip('?？。.!！,，;；:：')

    return norm(left) == norm(right)


def recent_questions(history: list[dict], limit: int = 2) -> list[str]:
    """按时间顺序取最近几条用户问题，用于改写失败时兜底拼接。"""
    questions: list[str] = []
    for message in history or []:
        if isinstance(message, dict) and str(message.get('role') or '') == 'user':
            text = str(message.get('content') or '').strip()
            if text:
                questions.append(text)
    return questions[-limit:]


def titles(result: dict, limit: int = 8) -> list[str]:
    """把检索命中整理成前端可读的标题列表。"""
    items: list[str] = []
    for row in (result.get('results') or [])[:limit]:
        if not isinstance(row, dict):
            continue
        citation = row.get('citation') or {}
        unit = row.get('unit') or {}
        title = str(citation.get('title') or unit.get('doc_title') or '').strip()
        if not title:
            continue
        article = str(citation.get('article') or unit.get('article') or '').strip()
        items.append(f'《{title}》{article}' if article else f'《{title}》')
    return items


def source_items(result: dict) -> list[dict]:
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


def done_payload(started: float, truncated: bool) -> dict:
    """done 事件字段：耗时与是否截断。"""
    return {'seconds': round(time.monotonic() - started, 2),
            'output_truncated': bool(truncated)}


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------


class RouteStep:
    """寒暄本地判断 + 意图路由：先本地识别小寒暄，再交给模型判断走向。"""

    def run(self, llm: Any, question: str, history: list[dict]) -> RouteDecision:
        decision = route(question, history, llm)
        return RouteDecision(
            mode=str(decision.get('mode') or 'policy'),
            search_query=str(decision.get('search_query') or question),
            reason=str(decision.get('reason') or ''),
        )


class RewriteStep:
    """多轮改写：指代消解与省略补全，失败或原样返回时用最近问题兜底。"""

    def run(self, llm: Any, question: str, history: list[dict], fallback: str = '') -> str:
        candidate = ''
        try:
            candidate = (rewrite(question, history, llm) or '').strip()
        except Exception:
            candidate = ''
        # 改写失败或模型原样返回时，用「最近两轮问题 + 本轮问题」兜底，
        # 避免「我一次想领10天的量可以吗」这类省略句丢掉上下文。
        if not candidate or same_text(candidate, question):
            topics = recent_questions(history, limit=2)
            merged = ' '.join(topics + [question]).strip()
            if merged and not same_text(merged, question):
                candidate = merged[:300]
            else:
                candidate = fallback or question
        return candidate


class RetrieveStep:
    """检索：在线程里跑检索，把阶段事件流式转发，终态给出结果或中文错误。"""

    def __init__(self, retriever_getter: Callable[[], Any], top_k: int | None = None,
                 poll_seconds: float = SEARCH_POLL_SECONDS) -> None:
        self._get_retriever = retriever_getter
        self._top_k = top_k
        self._poll = float(poll_seconds)

    def run(self, question: str, rewritten: str) -> Iterator[RetrievalStage | RetrievalDone]:
        try:
            retriever = self._get_retriever()
        except Exception as exc:
            yield RetrievalDone(error=f'知识库索引加载失败：{type(exc).__name__}: {exc}')
            return
        if retriever is None:
            yield RetrievalDone(error=MISSING_KNOWLEDGE_BASE)
            return
        top_k = config.TOP_K if self._top_k is None else int(self._top_k)
        # 检索器用改写后的问题检索，返回值与 query 事件里仍保留用户原问题。
        yield from self._stream(retriever, question, rewritten, top_k)

    def _stream(self, retriever: Any, question: str, rewritten: str,
                top_k: int) -> Iterator[RetrievalStage | RetrievalDone]:
        queued: queue.Queue = queue.Queue()
        finished = threading.Event()

        def on_stage(stage: str, payload: dict | None = None) -> None:
            queued.put(('stage', str(stage), dict(payload or {})))

        def worker() -> None:
            try:
                result = retriever.search(question, top_k=top_k,
                                          filters={'rewritten': rewritten},
                                          on_stage=on_stage)
                queued.put(('result', 'result', result))
            except Exception as exc:  # 交给编排器发 error 事件
                queued.put(('error', 'error', exc))
            finally:
                finished.set()

        thread = threading.Thread(target=worker, name='policy-search', daemon=True)
        thread.start()
        seen: set[str] = set()
        while True:
            try:
                kind, name, payload = queued.get(timeout=self._poll)
            except queue.Empty:
                if finished.is_set() and queued.empty():
                    failure = RuntimeError('检索线程异常退出，未返回结果')
                    yield RetrievalDone(error=f'制度检索失败：{failure}',
                                        stages=frozenset(seen))
                    return
                continue
            if kind == 'stage':
                seen.add(name)
                yield RetrievalStage(name, payload)
                continue
            if kind == 'error':
                failure = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
                yield RetrievalDone(error=f'制度检索失败：{failure}',
                                    stages=frozenset(seen))
                return
            yield RetrievalDone(result=payload if isinstance(payload, dict) else {},
                                stages=frozenset(seen))
            return


class PromptStep:
    """把问题、证据与历史组装成 messages（提示词模板在 app.llm.prompts）。"""

    def run(self, question: str, context: str, history: list[dict]) -> list[dict]:
        return prompts.build_messages(question, str(context or ''), history)

    def run_chat(self, question: str, history: list[dict]) -> list[dict]:
        return prompts.build_chat_messages(question, history)


class GenerateStep:
    """流式生成：把模型增量切成 answer 事件，超过上限即截断并停止拉取。"""

    def __init__(self, max_chars: int | None = None) -> None:
        self.max_chars = MAX_ANSWER_CHARS if max_chars is None else int(max_chars)

    def run(self, llm: Any, messages: list[dict]) -> Generator[AnswerEvent, None, AnswerStats | None]:
        """产出 answer 事件；失败或无内容时产出 error 事件并返回 None。"""
        stats = AnswerStats()
        try:
            for delta in llm.stream(messages, max_tokens=STREAM_MAX_TOKENS,
                                    temperature=STREAM_TEMPERATURE):
                if not delta:
                    continue
                remaining = self.max_chars - stats.chars
                if remaining <= 0:
                    stats.truncated = True
                    break
                text = delta if len(delta) <= remaining else delta[:remaining]
                stats.chars += len(text)
                stats.truncated = stats.chars >= self.max_chars
                yield AnswerEvent('answer', {'text': text})
                if stats.truncated:
                    break
        except Exception as exc:
            yield AnswerEvent('error', {'message': error_message(exc)})
            return None
        if stats.chars == 0 and not stats.truncated:
            yield AnswerEvent('error', {'message': EMPTY_ANSWER_MESSAGE})
            return None
        return stats


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


def _runtime_llm() -> Any:
    from app.wiring import runtime

    return runtime.llm()


def _runtime_retriever() -> Any:
    from app.wiring import runtime

    return runtime.retriever()


def _runtime_release_id() -> str | None:
    from app.wiring import runtime

    return runtime.release_id()


class AnswerFlow:
    """问答编排器：``run(request)`` 返回 ``AnswerEvent`` 生成器（同步）。

    步骤对象可替换、可单独调用；依赖全部来自构造函数，默认取运行时单例。
    """

    def __init__(self, llm_getter: Callable[[], Any] | None = None,
                 retriever_getter: Callable[[], Any] | None = None,
                 release_id_getter: Callable[[], str | None] | None = None,
                 *, top_k: int | None = None,
                 max_answer_chars: int | None = None) -> None:
        self._get_llm = llm_getter or _runtime_llm
        self._get_retriever = retriever_getter or _runtime_retriever
        self._get_release_id = release_id_getter or _runtime_release_id
        self.route_step = RouteStep()
        self.rewrite_step = RewriteStep()
        self.retrieve_step = RetrieveStep(self._get_retriever, top_k=top_k)
        self.prompt_step = PromptStep()
        self.generate_step = GenerateStep(max_chars=max_answer_chars)

    # ---- 对外入口 ----
    def run(self, request: AnswerRequest) -> Iterator[AnswerEvent]:
        started = time.monotonic()
        question = (request.question or '').strip()
        history = prompts.normalize_history(request.history)

        yield AnswerEvent('routing')

        llm, ready_error = self._prepare_llm()
        if llm is None:
            yield AnswerEvent('error', {'message': self._unavailable_message(ready_error)})
            return

        decision = self.route_step.run(llm, question, history)
        if decision.mode == 'chat':
            yield from self._chat(llm, question, history, started)
            return

        # 只有多轮对话才需要改写补全；首轮直接检索，省一次模型调用。
        rewritten = decision.search_query
        if history:
            rewritten = self.rewrite_step.run(llm, question, history, decision.search_query)
        yield from self._policy(llm, question, rewritten, history, started)

    # ---- 生成模型可用性 ----
    def _prepare_llm(self) -> tuple[Any | None, str]:
        """取生成客户端并做健康检查；不可用时返回 (None, 中文原因)。"""
        llm = None
        ready_error = ''
        try:
            llm = self._get_llm()
            if not llm.ready():
                ready_error = getattr(llm, 'last_error', '') or MODEL_HEALTH_MESSAGE
                llm = None
        except Exception as exc:
            ready_error = f'生成模型不可用：{type(exc).__name__}: {exc}'
            llm = None
        return llm, ready_error

    def _unavailable_message(self, ready_error: str) -> str:
        """模型不可用时的文案：没有发布版本时，缺知识库是更直接的原因。"""
        has_release = False
        try:
            has_release = self._get_release_id() is not None
        except Exception:
            has_release = False
        if not has_release:
            # 没有发布指针时 runtime.retriever() 会立即返回 None，不会加载索引。
            try:
                has_release = self._get_retriever() is not None
            except Exception:
                has_release = False
        if not has_release:
            return MISSING_KNOWLEDGE_BASE
        return ready_error or MODEL_CONFIG_HINT

    # ---- 两条主路径 ----
    def _chat(self, llm: Any, question: str, history: list[dict],
              started: float) -> Iterator[AnswerEvent]:
        messages = self.prompt_step.run_chat(question, history)
        yield AnswerEvent('prompt', {'messages': messages})
        yield AnswerEvent('generating')

        stats = yield from self.generate_step.run(llm, messages)
        if stats is None:
            return
        yield AnswerEvent('sources', {'items': []})
        yield AnswerEvent('done', done_payload(started, stats.truncated))

    def _policy(self, llm: Any, question: str, rewritten: str, history: list[dict],
                started: float) -> Iterator[AnswerEvent]:
        result: dict | None = None
        seen: frozenset[str] = frozenset()
        for signal in self.retrieve_step.run(question, rewritten):
            if isinstance(signal, RetrievalStage):
                yield AnswerEvent(signal.stage, signal.payload)
                continue
            seen = signal.stages
            if signal.error:
                yield AnswerEvent('error', {'message': signal.error})
                return
            result = signal.result

        if result is None:
            yield AnswerEvent('error', {'message': EMPTY_SEARCH_MESSAGE})
            return

        # 检索器可能没推 query/evidence（例如走缓存或提前返回），这里补齐事件。
        if 'query' not in seen:
            yield AnswerEvent('query', {
                'question': question,
                'rewritten': str(result.get('rewritten') or rewritten),
                'filters': result.get('filters') or {},
            })
        if 'evidence' not in seen:
            yield AnswerEvent('evidence', {
                'titles': titles(result),
                'notes': result.get('notes') or [],
            })

        messages = self.prompt_step.run(question, result.get('context') or '', history)
        yield AnswerEvent('prompt', {'messages': messages})
        yield AnswerEvent('generating')

        stats = yield from self.generate_step.run(llm, messages)
        if stats is None:
            return
        yield AnswerEvent('sources', {'items': source_items(result)})
        yield AnswerEvent('done', done_payload(started, stats.truncated))
