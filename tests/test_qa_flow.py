"""问答编排器单测（app/qa/flow.py）：假 LLM + 假检索器，不依赖网络与真实模型。

覆盖：事件序列与字段、首轮跳过改写、多轮改写兜底、寒暄本地直答、低相关拒答、
错误事件映射与输出截断。断言的是 docs/架构与接口.md 第 4 节的事件契约。
"""
from __future__ import annotations

import json

from app import config
from app.qa.flow import (
    AnswerEvent,
    AnswerFlow,
    AnswerRequest,
    RewriteStep,
    RouteStep,
    titles,
)

# ---------------------------------------------------------------------------
# 假对象与样例数据
# ---------------------------------------------------------------------------

SAMPLE_UNIT = {
    'id': 'doc-3f2a9c1b7d40#u0007',
    'doc_id': 'doc-3f2a9c1b7d40',
    'doc_title': '实验室安全管理办法',
    'doc_no': '校发〔2024〕12号',
    'version_label': '2024修订版',
    'chapter': '第三章 危险化学品管理',
    'article': '第十五条',
    'text': '危险化学品领用须经实验室负责人审批，并在危化品台账登记。',
    'pages': [3],
    'source': {'doc_id': 'doc-3f2a9c1b7d40', 'file': '实验室安全管理办法.pdf',
               'page': 3, 'page_end': 3, 'parser': 'pymupdf'},
}

SAMPLE_ROW = {
    'rank': 1,
    'unit': SAMPLE_UNIT,
    'evidence': '《实验室安全管理办法》第三章 第十五条（第3页）\n'
                '危险化学品领用须经实验室负责人审批，并在危化品台账登记。',
    'citation': {'doc_id': 'doc-3f2a9c1b7d40', 'title': '实验室安全管理办法',
                 'doc_no': '校发〔2024〕12号', 'article': '第十五条',
                 'chapter': '第三章', 'page': 3, 'pages': [3],
                 'version_label': '2024修订版'},
    'channels': ['BGE', 'BM25'],
    'scores': {'dense': 0.81, 'bm25': 6.4, 'rrf': 0.03},
}

VERSION_NOTE = '旧版《实验室安全管理办法》相关内容已不适用'
NOT_FOUND_NOTE = '未找到与问题直接相关的制度条款，建议换一种问法或补充事项名称'

HISTORY = [
    {'role': 'user', 'content': '实验室安全管理办法说了什么'},
    {'role': 'assistant', 'content': '规定了危化品领用要求。[证据1]'},
]

REWRITTEN = '实验室危险化学品领用流程（多轮补全）'
ANSWER_PIECES = ['危险化学品领用须经', '实验室负责人审批', '，[证据1]。']


def sample_result(results=None, notes=None, context=None) -> dict:
    rows = [SAMPLE_ROW] if results is None else list(results)
    note_list = [VERSION_NOTE] if notes is None else list(notes)
    if context is None:
        context = ('[证据1] 《实验室安全管理办法》第三章 第十五条（第3页）\n'
                   '危险化学品领用须经实验室负责人审批，并在危化品台账登记。')
    return {
        'question': '危化品怎么领用？',
        'rewritten': '实验室危险化学品领用流程',
        'filters': {'department': None},
        'results': rows,
        'context': context if rows else '',
        'tokens': 120 if rows else 0,
        'notes': note_list,
        'seconds': 0.01,
    }


class FakeLLM:
    """同步假客户端：按系统提示词区分路由与改写，stream 逐段产出。"""

    def __init__(self, available: bool = True, mode: str = 'policy',
                 search_query: str = '实验室危险化学品领用流程',
                 rewritten: str = REWRITTEN, pieces=None,
                 stream_error: Exception | None = None) -> None:
        self.available = available
        self.last_error = '' if available else '生成模型不可用：模拟模型服务未启动'
        self.mode = mode
        self.search_query = search_query
        self.rewritten = rewritten
        self.pieces = list(ANSWER_PIECES if pieces is None else pieces)
        self.stream_error = stream_error
        self.complete_kinds: list[str] = []
        self.stream_calls: list[list[dict]] = []

    def ready(self) -> bool:
        return self.available

    def complete(self, messages, max_tokens=512, temperature=0.1) -> str:
        if not self.available:
            raise RuntimeError('生成模型不可用：模拟模型服务未启动')
        system = str(messages[0].get('content') or '') if messages else ''
        if '只输出 JSON' in system:  # 路由
            self.complete_kinds.append('route')
            return json.dumps({'mode': self.mode, 'search_query': self.search_query,
                               'reason': '测试路由'}, ensure_ascii=False)
        self.complete_kinds.append('rewrite')  # 改写
        return self.rewritten

    def stream(self, messages, max_tokens=1024, temperature=0.2):
        self.stream_calls.append(messages)
        if self.stream_error is not None:
            raise self.stream_error
        for piece in self.pieces:
            yield piece


class FakeRetriever:
    """按引擎的 on_stage 契约推阶段事件，再返回配置好的结果。"""

    device = 'cpu'

    def __init__(self, result: dict | None = None, error: Exception | None = None,
                 emit_stages: bool = True, fail_after_stages: bool = False) -> None:
        self.result = sample_result() if result is None else result
        self.error = error
        self.emit_stages = emit_stages
        self.fail_after_stages = fail_after_stages
        self.calls: list[dict] = []

    def search(self, question, top_k=5, filters=None, on_stage=None):
        rewritten = str((filters or {}).get('rewritten') or question)
        self.calls.append({'question': question, 'top_k': top_k, 'rewritten': rewritten})
        if self.error is not None and not self.fail_after_stages:
            raise self.error
        if on_stage is not None and self.emit_stages:
            on_stage('query', {'question': question, 'rewritten': rewritten,
                               'filters': {'department': None}})
            on_stage('vectors', {'passage_count': 12, 'titles': titles(self.result)})
            on_stage('keywords', {'passage_count': 5, 'titles': titles(self.result)})
            on_stage('evidence', {'titles': titles(self.result),
                                  'notes': self.result.get('notes') or []})
        if self.error is not None:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def make_flow(llm=None, retriever=None, *, release_id='rel-test',
              retriever_getter=None, max_answer_chars=None, top_k=None) -> AnswerFlow:
    return AnswerFlow(
        llm_getter=lambda: llm,
        retriever_getter=retriever_getter or (lambda: retriever),
        release_id_getter=lambda: release_id,
        top_k=top_k,
        max_answer_chars=max_answer_chars,
    )


def collect(flow: AnswerFlow, question: str, history=()) -> list[dict]:
    request = AnswerRequest(question=question, history=list(history))
    return [event.to_dict() for event in flow.run(request)]


def stages_of(events: list[dict]) -> list[str]:
    return [event['stage'] for event in events]


def first(events: list[dict], stage: str) -> dict:
    return next(event for event in events if event['stage'] == stage)


# ---------------------------------------------------------------------------
# 1. 制度问答主路径：事件序列与字段完全按契约
# ---------------------------------------------------------------------------


def test_policy_flow_streams_contract_events():
    llm = FakeLLM()
    retriever = FakeRetriever()
    events = collect(make_flow(llm, retriever), '危化品怎么领用？', HISTORY)

    assert stages_of(events) == [
        'routing', 'query', 'vectors', 'keywords', 'evidence',
        'prompt', 'generating', 'answer', 'answer', 'answer', 'sources', 'done',
    ]
    # 事件结构：stage + 该阶段字段，序列化后仍是同一个 dict
    assert AnswerEvent('done', {'seconds': 1.0}).to_dict() == {'stage': 'done', 'seconds': 1.0}

    # 检索器拿到用户原问题 + 改写后的问题；top_k 取配置
    assert retriever.calls == [{'question': '危化品怎么领用？', 'top_k': config.TOP_K,
                                'rewritten': REWRITTEN}]
    query = first(events, 'query')
    assert query['question'] == '危化品怎么领用？'  # query 事件保留原问题
    assert query['rewritten'] == REWRITTEN
    assert query['filters'] == {'department': None}

    # 提示词带系统提示词与证据
    prompt = first(events, 'prompt')['messages']
    assert prompt[0]['role'] == 'system'
    assert '[证据1]' in prompt[-1]['content']

    answer = ''.join(event['text'] for event in events if event['stage'] == 'answer')
    assert answer == '危险化学品领用须经实验室负责人审批，[证据1]。'

    # sources 字段严格按契约第 4 节
    items = first(events, 'sources')['items']
    assert len(items) == 1
    item = items[0]
    assert item == {
        'id': 1,
        'doc_id': 'doc-3f2a9c1b7d40',
        'file': '实验室安全管理办法.pdf',
        'title': '实验室安全管理办法',
        'pages': [3],
        'focus_page': 3,
        'text': '危险化学品领用须经实验室负责人审批，并在危化品台账登记。',
        'article': '第十五条',
        'chapter': '第三章',
        'doc_no': '校发〔2024〕12号',
        'version_label': '2024修订版',
    }

    done = events[-1]
    assert done['stage'] == 'done'
    assert isinstance(done['seconds'], float)
    assert done['output_truncated'] is False


# ---------------------------------------------------------------------------
# 2. 首轮没有历史：跳过改写，直接用路由给出的检索问句
# ---------------------------------------------------------------------------


def test_first_turn_skips_rewrite_and_uses_route_query():
    llm = FakeLLM(search_query='实验室危险化学品领用流程')
    retriever = FakeRetriever()
    events = collect(make_flow(llm, retriever), '危化品怎么领用？', [])

    assert llm.complete_kinds == ['route']  # 只有一次路由调用，没有改写
    assert retriever.calls[0]['rewritten'] == '实验室危险化学品领用流程'
    assert first(events, 'query')['rewritten'] == '实验室危险化学品领用流程'
    assert first(events, 'query')['question'] == '危化品怎么领用？'

    # RouteStep 可以脱离编排器单独使用：寒暄走本地判断，不调模型
    decision = RouteStep().run(llm, '你好', [])
    assert decision.mode == 'chat'
    assert decision.search_query == '你好'
    assert llm.complete_kinds == ['route']


# ---------------------------------------------------------------------------
# 3. 多轮改写：模型原样返回时用最近两轮问题兜底
# ---------------------------------------------------------------------------


def test_rewrite_step_completes_reference_and_falls_back():
    # 模型给出独立问句时直接采用
    llm = FakeLLM(rewritten=REWRITTEN)
    assert RewriteStep().run(llm, '危化品怎么领用？', HISTORY, 'fallback') == REWRITTEN

    # 模型原样返回（或改写失败）时，用「最近两轮问题 + 本轮问题」兜底
    history = [
        {'role': 'user', 'content': '实验室安全管理办法说了什么'},
        {'role': 'assistant', 'content': '规定了危化品领用要求。[证据1]'},
        {'role': 'user', 'content': '那第十五条说什么'},
        {'role': 'assistant', 'content': '第十五条规定了领用审批。[证据1]'},
    ]
    question = '我一次想领10天的量可以吗？'
    merged = '实验室安全管理办法说了什么 那第十五条说什么 我一次想领10天的量可以吗？'
    assert RewriteStep().run(FakeLLM(rewritten=question), question, history, 'x') == merged

    # 兜底结果真的成为检索问句
    retriever = FakeRetriever()
    events = collect(make_flow(FakeLLM(rewritten=question), retriever), question, history)
    assert retriever.calls[0]['rewritten'] == merged
    assert first(events, 'query')['rewritten'] == merged


# ---------------------------------------------------------------------------
# 4. 寒暄：本地判断直接回，不碰检索器，也不调路由模型
# ---------------------------------------------------------------------------


def test_greeting_is_answered_locally_without_retrieval():
    llm = FakeLLM(pieces=['您好，', '我可以回答制度与办事流程问题。'])

    def broken_retriever():
        raise AssertionError('寒暄分支不应该访问检索器')

    flow = AnswerFlow(llm_getter=lambda: llm, retriever_getter=broken_retriever,
                      release_id_getter=lambda: 'rel-test')
    events = collect(flow, '你好')

    assert stages_of(events) == [
        'routing', 'prompt', 'generating', 'answer', 'answer', 'sources', 'done',
    ]
    assert llm.complete_kinds == []  # 小寒暄不消耗路由模型调用
    assert first(events, 'prompt')['messages'][0]['content'].startswith(
        '你是高校政策与制度智能问答助手。当前问题与制度检索无关')
    assert first(events, 'sources')['items'] == []
    assert events[-1]['output_truncated'] is False


# ---------------------------------------------------------------------------
# 5. 低相关拒答：notes 透传、sources 为空、提示词标记“没有证据”
# ---------------------------------------------------------------------------


def test_low_relevance_refusal_keeps_notes_and_empty_sources():
    refusal = '未在现行有效制度中找到依据，建议咨询实验室与设备管理处。'
    llm = FakeLLM(pieces=[refusal])
    retriever = FakeRetriever(result=sample_result(results=[], notes=[NOT_FOUND_NOTE]))
    events = collect(make_flow(llm, retriever), '学校游泳池暑假开放吗？')

    evidence = first(events, 'evidence')
    assert evidence['titles'] == []
    assert evidence['notes'] == [NOT_FOUND_NOTE]
    prompt = first(events, 'prompt')['messages']
    assert '（本次没有检索到任何制度证据）' in prompt[-1]['content']
    assert first(events, 'sources')['items'] == []
    assert ''.join(event['text'] for event in events if event['stage'] == 'answer') == refusal
    assert events[-1]['stage'] == 'done'


# ---------------------------------------------------------------------------
# 6. 边界路径：错误事件映射与输出截断
# ---------------------------------------------------------------------------


def test_error_events_and_answer_truncation():
    # 模型不可用但知识库存在：透出健康检查错误
    flow = make_flow(FakeLLM(available=False), FakeRetriever())
    events = collect(flow, '实验室危化品怎么领用？')
    assert stages_of(events) == ['routing', 'error']
    assert events[-1]['message'] == '生成模型不可用：模拟模型服务未启动'

    # 模型不可用且没有发布版本：缺知识库是更直接的原因
    flow = make_flow(FakeLLM(available=False), None, release_id=None)
    events = collect(flow, '实验室危化品怎么领用？')
    assert stages_of(events) == ['routing', 'error']
    assert '知识库尚未建立' in events[-1]['message']

    # 索引加载失败：错误文案带异常类型
    def broken_retriever():
        raise RuntimeError('索引文件缺失')

    flow = make_flow(FakeLLM(), None, retriever_getter=broken_retriever)
    events = collect(flow, '实验室危化品怎么领用？')
    assert stages_of(events) == ['routing', 'error']
    assert events[-1]['message'] == '知识库索引加载失败：RuntimeError: 索引文件缺失'

    # 检索已推过阶段事件后失败：阶段事件保留，随后才是 error
    retriever = FakeRetriever(error=RuntimeError('向量索引损坏'), fail_after_stages=True)
    events = collect(make_flow(FakeLLM(), retriever), '危化品怎么领用？')
    assert stages_of(events)[:4] == ['routing', 'query', 'vectors', 'keywords']
    assert events[-1]['stage'] == 'error'
    assert events[-1]['message'] == '制度检索失败：向量索引损坏'

    # 生成中报错：错误文案透出模型不可用原文
    llm = FakeLLM(stream_error=RuntimeError('生成模型不可用：连接中断'))
    events = collect(make_flow(llm, FakeRetriever()), '危化品怎么领用？')
    assert events[-1]['stage'] == 'error'
    assert events[-1]['message'] == '生成模型不可用：连接中断'

    # 输出超过上限即截断，并在 done 里标记
    llm = FakeLLM(pieces=['1234567890'])
    events = collect(make_flow(llm, FakeRetriever(), max_answer_chars=4), '危化品怎么领用？')
    assert [event['text'] for event in events if event['stage'] == 'answer'] == ['1234']
    assert events[-1]['stage'] == 'done'
    assert events[-1]['output_truncated'] is True


def test_refusal_citation_conflict_is_sanitized():
    """拒答句 + 引用并存时：删除引用，通过 revision 事件下发修正稿。"""
    pieces = ['未在现行有效制度中找到依据，建议咨询相关归口部门 [证据1][证据9]。']
    events = collect(make_flow(FakeLLM(pieces=pieces), FakeRetriever()), '危化品怎么领用？', [])

    revision = first(events, 'revision')
    assert '[证据' not in revision['text']
    assert '未在现行' in revision['text']
    assert revision['removed_invalid'] == [9]
    assert revision['removed_for_refusal'] == 1

    done = events[-1]
    assert done['stage'] == 'done'
    assert done['sanitized']['removed_invalid'] == [9]


def test_grounded_answer_after_refusal_drops_refusal_sentence():
    """拒答句后跟着成段有依据内容：删掉拒答句，保留有依据的正文。"""
    pieces = ['未在现行有效制度中找到依据，建议咨询相关归口部门。',
              '现有证据列出了住宿费标准：一类地区650元/人·天[证据1]，',
              '二类地区550元/人·天[证据1]，但未说明北京属于哪一类地区。']
    events = collect(make_flow(FakeLLM(pieces=pieces), FakeRetriever()), '出差住宿费？', [])

    revision = first(events, 'revision')
    assert '未在现行' not in revision['text']
    assert '650元' in revision['text']
