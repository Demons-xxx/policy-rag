"""问答链与 HTTP 接口测试（契约 3.12-3.14、第 4 节）。

全部使用假 LLM / 假检索器 / 假目录库，不依赖网络、大模型和真实发布版本。
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import documents
from app.api import info as info_api
from app.wiring import runtime
from app.llm import prompts, routing
from app.llm.client import ChatClient
from app.main import app

# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------

SAMPLE_UNIT = {
    'id': 'doc-3f2a9c1b7d40#u0007',
    'doc_id': 'doc-3f2a9c1b7d40',
    'doc_title': '实验室安全管理办法',
    'doc_no': '校发〔2024〕12号',
    'version_label': '2024修订版',
    'effective_from': '2024-07-01',
    'effective_to': None,
    'department': '实验室与设备管理处',
    'status': 'active',
    'chapter': '第三章 危险化学品管理',
    'section': None,
    'article': '第十五条',
    'article_no': 15,
    'kind': 'article',
    'heading_path': ['第三章 危险化学品管理', '第十五条'],
    'text': '危险化学品领用须经实验室负责人审批，并在危化品台账登记。',
    'pages': [3],
    'table': None,
    'order': 7,
    'source': {'doc_id': 'doc-3f2a9c1b7d40', 'file': '实验室安全管理办法.pdf',
               'page': 3, 'page_end': 3, 'parser': 'pymupdf'},
}

TITLES = ['《实验室安全管理办法》第十五条']


class FakeLLM:
    base_url = 'http://fake.local/v1'
    model = 'fake-qwen'
    last_error = ''

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.complete_calls: list[list[dict]] = []
        self.stream_calls: list[list[dict]] = []

    def ready(self) -> bool:
        if self.available:
            self.last_error = ''
            return True
        self.last_error = '生成模型不可用：模拟模型服务未启动'
        return False

    def complete(self, messages, max_tokens=512, temperature=0.1):
        self.complete_calls.append(messages)
        if not self.available:
            raise RuntimeError('生成模型不可用：模拟模型服务未启动')
        system = str(messages[0].get('content') or '') if messages else ''
        if '只输出 JSON' in system:  # 路由
            return json.dumps({'mode': 'policy',
                               'search_query': '实验室危险化学品领用流程',
                               'reason': '制度咨询'}, ensure_ascii=False)
        return '实验室危险化学品领用流程（多轮补全）'

    def stream(self, messages, max_tokens=1024, temperature=0.2):
        self.stream_calls.append(messages)
        if not self.available:
            raise RuntimeError('生成模型不可用：模拟模型服务未启动')
        for piece in ['危险化学品领用须经', '实验室负责人审批', '，[证据1]。']:
            yield piece


class FakeRetriever:
    device = 'cpu'

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def search(self, question, top_k=5, filters=None, on_stage=None):
        rewritten = str((filters or {}).get('rewritten') or question)
        self.calls.append((question, rewritten))
        if on_stage is not None:
            on_stage('query', {'question': question, 'rewritten': rewritten,
                               'filters': {'department': None}})
            on_stage('vectors', {'passage_count': 12, 'titles': TITLES})
            on_stage('keywords', {'passage_count': 5, 'titles': TITLES})
            on_stage('evidence', {'titles': TITLES,
                                  'notes': ['旧版《实验室安全管理办法》相关内容已不适用']})
        return {
            'question': question,
            'rewritten': question,
            'filters': {'department': None},
            'results': [{
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
            }],
            'context': '[证据1] 《实验室安全管理办法》第三章 第十五条（第3页）\n'
                       '危险化学品领用须经实验室负责人审批，并在危化品台账登记。',
            'tokens': 120,
            'notes': ['旧版《实验室安全管理办法》相关内容已不适用'],
            'seconds': 0.01,
        }

    def info(self) -> dict:
        return {'release_id': 'rel-fake', 'doc_count': 1, 'unit_count': 1,
                'backend': 'npz', 'embedding_model': 'bge', 'device': 'cpu'}


class FakeCatalog:
    def __init__(self, records: list[dict]) -> None:
        self.records = records

    def list_documents(self) -> list[dict]:
        return list(self.records)

    def get_document(self, doc_id: str):
        for record in self.records:
            if record.get('doc_id') == doc_id:
                return dict(record)
        return None


def ndjson(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def first_stages(events: list[dict]) -> list[str]:
    ordered: list[str] = []
    for event in events:
        stage = event.get('stage')
        if stage and stage not in ordered:
            ordered.append(stage)
    return ordered


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    """每个用例都从“没有发布版本、没有目录库”的干净状态开始。"""
    monkeypatch.setattr(config, 'ACTIVE_RELEASE', tmp_path / 'active_release.json')
    monkeypatch.setattr(config, 'CATALOG_DB', tmp_path / 'catalog.sqlite')
    info_api.reset_ready_cache()
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_llm(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(runtime, 'llm', lambda: llm)
    return llm


@pytest.fixture
def fake_retriever(monkeypatch):
    retriever = FakeRetriever()
    monkeypatch.setattr(runtime, 'retriever', lambda force=False: retriever)
    return retriever


# ---------------------------------------------------------------------------
# ChatClient（契约 3.12）
# ---------------------------------------------------------------------------


def test_chat_client_stream_parses_sse():
    chunks = [
        {'choices': [{'delta': {'content': '危险化学品'}}]},
        {'choices': [{'delta': {'content': '须审批'}}]},
        {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
    ]
    body = ''.join('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n' for chunk in chunks)
    body += 'data: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == '/v1/chat/completions'
        payload = json.loads(request.content.decode('utf-8'))
        assert payload['stream'] is True
        assert payload['model'] == 'fake-qwen'
        return httpx.Response(200, content=body.encode('utf-8'),
                              headers={'content-type': 'text/event-stream'})

    client = ChatClient('http://mock.local/v1', 'fake-qwen')
    fake = httpx.Client(transport=httpx.MockTransport(handler))
    client._client = fake
    client._stream_client = fake  # 流式请求走独立客户端（T11 首 token 超时）
    assert ''.join(client.stream([{'role': 'user', 'content': 'hi'}])) == '危险化学品须审批'


def test_chat_client_complete_and_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'fake-qwen'}]})
        if request.url.path == '/v1/chat/completions':
            return httpx.Response(200, json={'choices': [{'message': {'content': '好的'}}]})
        return httpx.Response(404)

    client = ChatClient('http://mock.local/v1', 'fake-qwen')
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.ready() is True
    assert client.complete([{'role': 'user', 'content': 'hi'}]) == '好的'


def test_chat_client_errors_are_chinese_runtime_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('connection refused', request=request)

    client = ChatClient('http://mock.local/v1', 'fake-qwen')
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.ready() is False
    assert client.last_error.startswith('生成模型不可用：')
    with pytest.raises(RuntimeError) as caught:
        list(client.stream([{'role': 'user', 'content': 'hi'}]))
    assert str(caught.value).startswith('生成模型不可用：')


# ---------------------------------------------------------------------------
# prompts / routing（契约 3.13、3.14）
# ---------------------------------------------------------------------------


def test_system_prompt_has_citation_and_refusal_rules():
    for required in ('[证据n]', '未在现行有效制度中找到依据', '[证据1]',
                     '禁止编造条款号', '以现行有效版本', '不要输出思考过程'):
        assert required in prompts.SYSTEM_PROMPT


def test_build_messages_trims_history():
    history = []
    for index in range(20):
        role = 'user' if index % 2 == 0 else 'assistant'
        history.append({'role': role, 'content': f'第{index}条历史' + '内容' * 400})
    messages = prompts.build_messages('实验室危化品怎么领用？', '[证据1] 原文', history)
    assert messages[0]['role'] == 'system'
    assert '[证据1] 原文' in messages[-1]['content']
    assert messages[-1]['content'].startswith('【检索到的制度证据】')

    kept = messages[1:-1]
    assert len(kept) <= 12
    assert kept[0]['role'] == 'user'
    assert sum(len(item['content']) for item in kept) <= config.MAX_HISTORY_CHARS


def test_route_parses_json_and_rewrite_completes_reference():
    llm = FakeLLM()
    decision = routing.route('危化品怎么领用', [], llm)
    assert decision['mode'] == 'policy'
    assert decision['search_query'] == '实验室危险化学品领用流程'

    history = [{'role': 'user', 'content': '实验室安全管理办法说了什么'},
               {'role': 'assistant', 'content': '规定了危化品领用等要求。[证据1]'}]
    assert routing.rewrite('那第十五条说什么', history, llm) == '实验室危险化学品领用流程（多轮补全）'


def test_route_and_rewrite_degrade_without_llm():
    class BrokenLLM:
        last_error = ''

        def complete(self, *args, **kwargs):
            raise RuntimeError('生成模型不可用：模拟服务未启动')

    question = '那第十五条说什么？'
    decision = routing.route(question, [], BrokenLLM())
    assert decision['mode'] == 'policy'
    assert decision['search_query'] == question
    assert routing.rewrite(question, [{'role': 'user', 'content': '实验室安全管理办法'}],
                           BrokenLLM()) == question

    class GarbageLLM:
        def complete(self, *args, **kwargs):
            return '这不是 JSON'

    assert routing.route('实验室安全有什么要求', [], GarbageLLM())['mode'] == 'policy'
    # 寒暄走本地判断，不依赖模型，也不会被历史带成制度检索
    assert routing.route('你好', [], GarbageLLM())['mode'] == 'chat'
    assert routing.route('你好，实验室怎么领用危化品', [], GarbageLLM())['mode'] == 'policy'


# ---------------------------------------------------------------------------
# /api/info / /api/ask（契约第 4 节）
# ---------------------------------------------------------------------------


def test_api_info_fields(client, fake_llm, fake_retriever):
    response = client.get('/api/info')
    assert response.status_code == 200
    payload = response.json()
    expected = {'name', 'version', 'release_id', 'release_published_at', 'doc_count',
                'unit_count', 'vector_backend', 'embedding_model', 'llm_model', 'device',
                'model_ready', 'history_messages', 'automatic_entry', 'questions', 'error'}
    assert expected <= set(payload)
    assert payload['release_id'] is None
    assert payload['doc_count'] == 0
    assert payload['model_ready'] is True
    assert payload['error'] is None
    assert payload['history_messages'] == config.MAX_HISTORY_MESSAGES
    assert payload['automatic_entry'] is False
    assert payload['questions'] == []
    assert payload['device'] == 'cpu'


def test_api_info_reports_model_error(client, monkeypatch, fake_retriever):
    monkeypatch.setattr(runtime, 'llm', lambda: FakeLLM(available=False))
    response = client.get('/api/info')
    assert response.status_code == 200
    payload = response.json()
    assert payload['model_ready'] is False
    assert '生成模型不可用' in payload['error']


def test_api_ask_without_release_returns_error_event(client, fake_llm, monkeypatch):
    monkeypatch.setattr(runtime, 'retriever', lambda force=False: None)
    response = client.post('/api/ask', json={'question': '实验室危化品怎么领用？', 'history': []})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/x-ndjson')
    events = ndjson(response.text)
    assert events[0]['stage'] == 'routing'
    assert events[-1]['stage'] == 'error'
    assert '知识库尚未建立' in events[-1]['message']
    assert 'answer' not in [event.get('stage') for event in events]


def test_api_ask_streams_full_event_sequence(client, fake_llm, fake_retriever):
    history = [{'role': 'user', 'content': '实验室安全管理办法说了什么'},
               {'role': 'assistant', 'content': '规定了危化品领用要求。[证据1]'}]
    response = client.post('/api/ask', json={'question': '危化品怎么领用？',
                                             'history': history,
                                             'run_tag': 'test-run'})
    assert response.status_code == 200
    events = ndjson(response.text)

    assert first_stages(events) == [
        'routing', 'query', 'vectors', 'keywords', 'evidence',
        'prompt', 'generating', 'answer', 'sources', 'done',
    ]
    assert events[-1]['stage'] == 'done'
    assert isinstance(events[-1]['seconds'], float)
    assert events[-1]['output_truncated'] is False

    answer = ''.join(event.get('text') or '' for event in events if event['stage'] == 'answer')
    assert '危险化学品领用须经' in answer

    sources = [event for event in events if event['stage'] == 'sources'][0]['items']
    assert len(sources) == 1
    item = sources[0]
    assert item['id'] == 1
    assert item['doc_id'] == 'doc-3f2a9c1b7d40'
    assert item['file'] == '实验室安全管理办法.pdf'
    assert item['title'] == '实验室安全管理办法'
    assert item['pages'] == [3]
    assert item['focus_page'] == 3
    assert item['article'] == '第十五条'
    assert item['chapter'] == '第三章'
    assert item['doc_no'] == '校发〔2024〕12号'
    assert item['version_label'] == '2024修订版'
    assert '危险化学品领用须经' in item['text']

    # 检索器拿到用户原问题 + 改写后的问题（filters.rewritten），query 事件保留原问题
    assert fake_retriever.calls == [('危化品怎么领用？', '实验室危险化学品领用流程（多轮补全）')]
    # prompt 里带证据和系统提示词
    prompt_messages = [event for event in events if event['stage'] == 'prompt'][0]['messages']
    assert prompt_messages[0]['role'] == 'system'
    assert '[证据1]' in prompt_messages[-1]['content']


def test_api_ask_degrades_when_llm_unavailable(client, monkeypatch, fake_retriever):
    monkeypatch.setattr(runtime, 'llm', lambda: FakeLLM(available=False))
    response = client.post('/api/ask', json={'question': '实验室危化品怎么领用？', 'history': []})
    assert response.status_code == 200
    events = ndjson(response.text)
    assert events[0]['stage'] == 'routing'
    assert events[-1]['stage'] == 'error'
    assert '生成模型不可用' in events[-1]['message']


def test_api_ask_reports_missing_knowledge_base_when_llm_down(client, monkeypatch):
    monkeypatch.setattr(runtime, 'llm', lambda: FakeLLM(available=False))
    monkeypatch.setattr(runtime, 'retriever', lambda force=False: None)
    response = client.post('/api/ask', json={'question': '实验室危化品怎么领用？', 'history': []})
    assert response.status_code == 200
    events = ndjson(response.text)
    assert events[-1]['stage'] == 'error'
    assert '知识库尚未建立' in events[-1]['message']


def test_api_ask_rejects_empty_question(client, fake_llm):
    response = client.post('/api/ask', json={'question': '   ', 'history': []})
    assert response.status_code == 400
    assert response.json()['detail'] == '问题不能为空'


# ---------------------------------------------------------------------------
# /api/documents（契约第 4 节）
# ---------------------------------------------------------------------------


DOCUMENT_RECORD = {
    'doc_id': 'doc-abc123def456',
    'title': '实验室安全管理办法',
    'doc_no': '校发〔2024〕12号',
    'version_label': '2024修订版',
    'department': None,
    'status': 'active',
    'file_name': '实验室安全管理办法.pdf',
    'format': 'pdf',
    'pages': 12,
    'has_pdf': 1,
    'sha256': 'abc',
    'source_path': 'data/docs/doc-abc123def456/source.pdf',
    'effective_from': '2024-07-01',
    'effective_to': None,
}


def test_api_documents_structure(client, monkeypatch, tmp_path):
    doc_dir = tmp_path / 'docs' / 'doc-abc123def456'
    doc_dir.mkdir(parents=True)
    (doc_dir / 'meta.json').write_text(json.dumps(
        {'department': '实验室与设备管理处', 'has_pdf': True, 'pages': 12},
        ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(config, 'DOCS_DIR', tmp_path / 'docs')
    monkeypatch.setattr(documents, '_catalog', lambda: FakeCatalog([DOCUMENT_RECORD]))

    response = client.get('/api/documents')
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {'documents'}
    assert len(payload['documents']) == 1
    document = payload['documents'][0]
    assert set(document) == {'id', 'name', 'title', 'doc_no', 'version_label',
                             'department', 'status', 'supersedes', 'superseded_by',
                             'pages', 'has_pdf', 'effective_from', 'effective_to'}
    assert document['id'] == 'doc-abc123def456'
    assert document['name'] == '实验室安全管理办法.pdf'
    assert document['title'] == '实验室安全管理办法'
    assert document['department'] == '实验室与设备管理处'  # meta.json 补齐
    assert document['pages'] == 12
    assert document['has_pdf'] is True
    assert document['status'] == 'active'


def test_api_documents_pdf_404_is_chinese(client, monkeypatch):
    record = dict(DOCUMENT_RECORD, format='docx', has_pdf=0,
                  source_path='data/docs/doc-abc123def456/source.docx')
    monkeypatch.setattr(documents, '_catalog', lambda: FakeCatalog([record]))

    missing = client.get('/api/documents/doc-not-exist/pdf')
    assert missing.status_code == 404
    assert missing.json()['detail'] == '文档不存在'

    no_pdf = client.get('/api/documents/doc-abc123def456/pdf')
    assert no_pdf.status_code == 404
    assert no_pdf.json()['detail'] == '该文档没有可预览的 PDF 原件'


def test_api_documents_page_image_renders_png(client, monkeypatch, tmp_path):
    import fitz

    pdf_path = tmp_path / 'docs' / 'doc-abc123def456' / 'source.pdf'
    pdf_path.parent.mkdir(parents=True)
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)  # A4
    page.insert_text((72, 72), 'PolicyRAG preview test')
    pdf.save(pdf_path)
    pdf.close()

    record = dict(DOCUMENT_RECORD, source_path=str(pdf_path))
    monkeypatch.setattr(documents, '_catalog', lambda: FakeCatalog([record]))

    response = client.get('/api/documents/doc-abc123def456/pages/1/image')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/png'
    content = response.content
    assert content[:8] == b'\x89PNG\r\n\x1a\n'
    # 150dpi 渲染的 A4 页面，像素尺寸约 1240x1754
    width = int.from_bytes(content[16:20], 'big')
    height = int.from_bytes(content[20:24], 'big')
    assert 1200 <= width <= 1300
    assert 1700 <= height <= 1800

    out_of_range = client.get('/api/documents/doc-abc123def456/pages/9/image')
    assert out_of_range.status_code == 404
    assert '页码超出范围' in out_of_range.json()['detail']


def test_api_documents_list_degrades_without_catalog(client, monkeypatch):
    def broken():
        raise ImportError('app.store.catalog 尚未实现')

    monkeypatch.setattr(documents, '_catalog', broken)
    response = client.get('/api/documents')
    assert response.status_code == 200
    assert response.json() == {'documents': []}


# ---------------------------------------------------------------------------
# /api/ingest 参数校验
# ---------------------------------------------------------------------------


def test_api_ingest_rejects_unsupported_extension(client):
    response = client.post('/api/ingest',
                           files={'files': ('病毒.exe', b'MZ', 'application/octet-stream')})
    assert response.status_code == 400
    assert '不支持的文件类型' in response.json()['detail']


def test_api_ingest_rejects_bad_metadata(client):
    response = client.post(
        '/api/ingest',
        files={'files': ('制度.txt', '第一章 总则'.encode('utf-8'), 'text/plain')},
        data={'metadata': '{不是 JSON'},
    )
    assert response.status_code == 400
    assert 'metadata 不是合法 JSON' in response.json()['detail']
