"""上传元数据：按文件分别指定（列表）或全局共用（对象）。"""
import json
import types

from fastapi.testclient import TestClient

from app.api import ingest as ingest_api
from app.ingest.pipeline import meta_for_file
from app.main import app

client = TestClient(app)


def test_meta_for_file_aligns_list_with_files():
    metas = [{'title': 'A制度'}, {'title': 'B制度'}]
    assert meta_for_file(metas, 0) == {'title': 'A制度'}
    assert meta_for_file(metas, 1) == {'title': 'B制度'}
    assert meta_for_file(metas, 5) == {}
    assert meta_for_file({'title': '全局'}, 3) == {'title': '全局'}
    assert meta_for_file(None, 0) == {}


def test_upload_accepts_per_file_metadata(monkeypatch):
    captured: dict = {}

    def fake_submit(files, meta=None):
        captured['files'] = files
        captured['meta'] = meta
        return 'job-test-1'

    monkeypatch.setattr(ingest_api, '_pipeline',
                        lambda: types.SimpleNamespace(submit_upload=fake_submit))
    response = client.post(
        '/api/ingest',
        files=[('files', ('甲.txt', '第一章 总则'.encode(), 'text/plain')),
               ('files', ('乙.txt', '第二章 细则'.encode(), 'text/plain'))],
        data={'metadata': json.dumps([{'title': '甲制度'}, {'title': '乙制度'}])},
    )
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 'job-test-1'
    assert captured['meta'] == [{'title': '甲制度'}, {'title': '乙制度'}]
    assert [name for name, _ in captured['files']] == ['甲.txt', '乙.txt']


def test_upload_rejects_metadata_list_longer_than_files(monkeypatch):
    monkeypatch.setattr(ingest_api, '_pipeline',
                        lambda: types.SimpleNamespace(submit_upload=lambda *a, **k: 'job-x'))
    response = client.post(
        '/api/ingest',
        files=[('files', ('甲.txt', b'x', 'text/plain'))],
        data={'metadata': json.dumps([{'title': 'A'}, {'title': 'B'}])},
    )
    assert response.status_code == 400
    assert '不能超过文件数' in response.json()['detail']


def test_upload_rejects_non_object_metadata_item(monkeypatch):
    monkeypatch.setattr(ingest_api, '_pipeline',
                        lambda: types.SimpleNamespace(submit_upload=lambda *a, **k: 'job-x'))
    response = client.post(
        '/api/ingest',
        files=[('files', ('甲.txt', b'x', 'text/plain'))],
        data={'metadata': json.dumps(['不是对象'])},
    )
    assert response.status_code == 400
    assert '必须是 JSON 对象' in response.json()['detail']


def test_run_job_passes_each_files_own_metadata(monkeypatch):
    from app.ingest import pipeline

    captured: list[tuple[str, dict]] = []

    def fake_ingest(path, meta=None, job_id=None, parser=None):
        captured.append((str(path), dict(meta or {})))
        return {'doc_id': 'doc-' + str(path), 'title': (meta or {}).get('title'),
                'unit_count': 1, 'deduplicated': False, 'issues': []}

    class FakeCatalog:
        def __init__(self, *a, **k):
            self.payload = {
                'files': [{'name': '甲.txt', 'path': '甲.txt'},
                          {'name': '乙.txt', 'path': '乙.txt'}],
                'meta': [{'title': '甲制度'}, {'title': '乙制度'}],
            }

        def get_job(self, job_id):
            return {'job_id': job_id, 'payload': self.payload}

        def update_job(self, *a, **k):
            return None

        def record_audit(self, *a, **k):
            return None

        def close(self):
            return None

    monkeypatch.setattr(pipeline, 'ingest_path', fake_ingest)
    monkeypatch.setattr(pipeline, 'Catalog', FakeCatalog)
    pipeline.run_job('job-test')
    assert [meta['title'] for _, meta in captured] == ['甲制度', '乙制度']
