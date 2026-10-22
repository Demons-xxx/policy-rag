"""可观测性（T12）：分段耗时日志与 /api/metrics。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import config
from app.api import ask as ask_api
from app.main import app
from app.observability import Metrics, log_request, metrics
from app.qa.flow import AnswerEvent


def test_metrics_snapshot_percentiles_and_error_rate():
    recorder = Metrics()
    for value in range(1, 101):
        recorder.record({'total_ms': value, 'retrieval_ms': value / 2,
                         'first_token_ms': value * 2, 'error': 'boom' if value > 90 else None})
    snap = recorder.snapshot()
    assert snap['requests'] == 100
    assert snap['errors'] == 10
    assert snap['error_rate'] == 0.1
    assert 50 <= snap['total_ms']['p50'] <= 51
    assert 95 <= snap['total_ms']['p95'] <= 96
    assert snap['retrieval_ms']['p95'] < snap['total_ms']['p95']


def test_log_request_appends_json_line(tmp_path):
    path = tmp_path / 'app.log'
    log_request({'request_id': 'abc', 'total_ms': 12.3}, path=path)
    log_request({'request_id': 'def', 'total_ms': 4.5}, path=path)
    lines = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert [item['request_id'] for item in lines] == ['abc', 'def']
    assert lines[1]['total_ms'] == 4.5


def test_log_request_does_not_raise_on_bad_path(tmp_path):
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('x', encoding='utf-8')
    log_request({'request_id': 'x'}, path=blocker / 'app.log')  # 只记 warning，不抛


def test_ask_writes_request_log_and_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'LOGS_DIR', tmp_path)
    metrics.reset()

    class FakeFlow:
        def run(self, request):
            yield AnswerEvent('routing')
            yield AnswerEvent('query', {'rewritten': 'q'})
            yield AnswerEvent('evidence', {'titles': ['t']})
            yield AnswerEvent('prompt')
            yield AnswerEvent('generating')
            yield AnswerEvent('answer', {'text': '答'})
            yield AnswerEvent('sources', {'items': [{'id': 1}, {'id': 2}]})
            yield AnswerEvent('done', {})

    monkeypatch.setattr(ask_api, 'AnswerFlow', FakeFlow)
    client = TestClient(app)
    response = client.post('/api/ask', json={'question': '测试问题', 'history': []})
    assert response.status_code == 200
    assert response.text.count('\n') >= 8

    snap = metrics.snapshot()
    assert snap['requests'] == 1 and snap['errors'] == 0
    assert snap['total_ms']['p50'] >= 0

    lines = (tmp_path / 'app.log').read_text(encoding='utf-8').splitlines()
    record = json.loads(lines[-1])
    assert record['question'] == '测试问题'
    assert record['route'] == 'retrieve'
    assert record['sources'] == 2
    assert record['error'] is None
    assert record['request_id'] and len(record['request_id']) == 8


def test_metrics_endpoint_shape():
    metrics.reset()
    metrics.record({'total_ms': 30.0, 'retrieval_ms': 10.0, 'first_token_ms': 5.0})
    client = TestClient(app)
    payload = client.get('/api/metrics').json()
    assert payload['requests'] == 1
    assert payload['errors'] == 0
    assert payload['total_ms']['p50'] == 30.0
    assert {'release_id', 'vector_backend', 'llm_model'} <= set(payload)
