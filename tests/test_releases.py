"""发布管理（列表/回滚/清理/删除）的存储层与接口测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.store import releases


def _make_release(root: Path, release_id: str, published_at: str, docs: int = 2) -> Path:
    folder = root / release_id
    folder.mkdir(parents=True)
    (folder / 'units.jsonl').write_text('', encoding='utf-8')
    (folder / 'passages.jsonl').write_text('', encoding='utf-8')
    (folder / 'vectors.npz').write_bytes(b'fake-vectors')
    manifest = {
        'schema': 1, 'release_id': release_id, 'published_at': published_at,
        'doc_count': docs, 'unit_count': docs * 3, 'passage_count': docs * 3,
        'collection': releases.collection_name(release_id),
    }
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False),
                                          encoding='utf-8')
    return folder


@pytest.fixture()
def release_dirs(tmp_path, monkeypatch):
    root = tmp_path / 'releases'
    root.mkdir()
    active_file = tmp_path / 'state' / 'active_release.json'
    monkeypatch.setattr(config, 'RELEASES_DIR', root)
    monkeypatch.setattr(config, 'ACTIVE_RELEASE', active_file)
    _make_release(root, 'rel-a', '2026-01-01T00:00:00+08:00')
    _make_release(root, 'rel-b', '2026-02-01T00:00:00+08:00')
    _make_release(root, 'rel-c', '2026-03-01T00:00:00+08:00')
    releases.write_active({'id': 'rel-b', 'dir': str(root / 'rel-b')})
    return root


def test_list_releases_reports_counts_and_size(release_dirs):
    items = releases.list_releases()
    assert [item['id'] for item in items] == ['rel-c', 'rel-b', 'rel-a']
    assert items[0]['doc_count'] == 2
    assert items[0]['unit_count'] == 6
    assert items[0]['size_bytes'] > 0


def test_activate_switches_pointer(release_dirs):
    result = releases.activate('rel-c')
    assert result['previous'] == 'rel-b'
    assert releases.read_active()['id'] == 'rel-c'
    with pytest.raises(releases.ReleaseError):
        releases.activate('rel-missing')


def test_delete_refuses_active_release(release_dirs):
    with pytest.raises(releases.ReleaseError, match='正在生效'):
        releases.delete_release('rel-b')
    releases.delete_release('rel-a')
    assert not (release_dirs / 'rel-a').exists()


def test_prune_keeps_latest_and_active(release_dirs):
    releases.activate('rel-a')  # 最新的是 rel-c，但 rel-a 生效中必须保留
    result = releases.prune(keep=1)
    remaining = {item['id'] for item in releases.list_releases()}
    assert remaining == {'rel-c', 'rel-a'}
    assert set(result['removed']) == {'rel-b'}


def test_release_api_endpoints(release_dirs):
    client = TestClient(app)
    listed = client.get('/api/releases').json()
    assert listed['active'] == 'rel-b'
    assert {item['id'] for item in listed['releases']} == {'rel-a', 'rel-b', 'rel-c'}

    switched = client.post('/api/releases/rel-c/activate')
    assert switched.status_code == 200
    assert switched.json()['active']['id'] == 'rel-c'

    refused = client.delete('/api/releases/rel-c')
    assert refused.status_code == 409

    deleted = client.delete('/api/releases/rel-a')
    assert deleted.status_code == 200

    pruned = client.post('/api/releases/prune', json={'keep': 1})
    assert pruned.status_code == 200
    assert pruned.json()['ok'] is True
