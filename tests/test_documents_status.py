"""文档停用/恢复接口测试（用临时目录库，不发布、不碰真实知识库）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.store.catalog import Catalog


def _doc(doc_id: str) -> dict:
    return {
        'doc_id': doc_id, 'title': '测试制度', 'doc_no': '测试〔2026〕1号',
        'version_label': '2026版', 'effective_from': '2026-01-01', 'effective_to': None,
        'department': '测试处', 'status': 'active', 'supersedes': None,
        'file_name': 'test.pdf', 'format': 'pdf', 'pages': 1, 'has_pdf': 1,
        'sha256': 'sha-' + doc_id, 'source_path': None, 'imported_at': '2026-01-02',
    }


def test_disable_and_restore_document(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CATALOG_DB', tmp_path / 'catalog.sqlite')
    catalog = Catalog(config.CATALOG_DB)
    catalog.upsert_document(_doc('doc-1'))
    catalog.close()

    client = TestClient(app)
    marked = client.delete('/api/documents/doc-1?publish=false')
    assert marked.status_code == 200
    assert marked.json()['status'] == 'repealed'

    again = client.delete('/api/documents/doc-1?publish=false')
    assert again.json()['already'] is True

    listed = {item['id']: item for item in client.get('/api/documents').json()['documents']}
    assert listed['doc-1']['status'] == 'repealed'

    restored = client.post('/api/documents/doc-1/restore?publish=false')
    assert restored.status_code == 200
    assert restored.json()['status'] == 'active'

    catalog = Catalog(config.CATALOG_DB)
    try:
        assert catalog.get_document('doc-1')['status'] == 'active'
    finally:
        catalog.close()

    missing = client.delete('/api/documents/doc-404?publish=false')
    assert missing.status_code == 404


def test_version_link_override(tmp_path, monkeypatch):
    """手工覆盖版本链：推断误判时用 API 修正，只改登记不重新发布。"""
    monkeypatch.setattr(config, 'CATALOG_DB', tmp_path / 'catalog.sqlite')
    catalog = Catalog(config.CATALOG_DB)
    catalog.upsert_document(_doc('doc-old'))
    catalog.upsert_document(_doc('doc-new'))
    catalog.close()

    client = TestClient(app)
    response = client.post('/api/documents/doc-old/version-link', json={
        'status': 'superseded', 'superseded_by': 'doc-new'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'superseded'
    assert payload['superseded_by'] == 'doc-new'

    catalog = Catalog(config.CATALOG_DB)
    try:
        row = catalog.get_document('doc-old')
        assert row['status'] == 'superseded' and row['superseded_by'] == 'doc-new'
        actions = [item['action'] for item in catalog.list_audit(10)]
        assert 'document_version_link' in actions
    finally:
        catalog.close()

    bad = client.post('/api/documents/doc-old/version-link', json={'status': 'bogus'})
    assert bad.status_code == 400
    empty = client.post('/api/documents/doc-old/version-link', json={})
    assert empty.status_code == 400
    missing = client.post('/api/documents/doc-404/version-link', json={'status': 'active'})
    assert missing.status_code == 404
