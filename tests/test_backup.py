# -*- coding: utf-8 -*-
"""备份/恢复：打包 data 三类目录、校验、按 keep 清理、可解压恢复。"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from app import config
from app.ops import backup


def _prepare(tmp_path: Path) -> dict:
    root = tmp_path / 'project'
    (root / 'data' / 'docs' / 'doc-1').mkdir(parents=True)
    (root / 'data' / 'docs' / 'doc-1' / 'meta.json').write_text('{"doc_id":"doc-1"}', encoding='utf-8')
    (root / 'data' / 'releases' / 'rel-1').mkdir(parents=True)
    (root / 'data' / 'releases' / 'rel-1' / 'manifest.json').write_text('{}', encoding='utf-8')
    (root / 'data' / 'state').mkdir(parents=True)
    (root / 'data' / 'state' / 'active_release.json').write_text('{"release_id":"rel-1"}', encoding='utf-8')
    (root / 'app').mkdir(parents=True)
    (root / 'app' / 'project.json').write_text('{"version":"0.2.1"}', encoding='utf-8')
    (root / '.env').write_text('POLICYRAG_PORT=7880\n', encoding='utf-8')
    return {'root': root, 'docs': root / 'data' / 'docs',
            'releases': root / 'data' / 'releases', 'state': root / 'data' / 'state',
            'active': root / 'data' / 'state' / 'active_release.json'}


def _patch_config(monkeypatch, paths: dict) -> None:
    monkeypatch.setattr(config, 'ROOT', paths['root'])
    monkeypatch.setattr(config, 'DOCS_DIR', paths['docs'])
    monkeypatch.setattr(config, 'RELEASES_DIR', paths['releases'])
    monkeypatch.setattr(config, 'STATE_DIR', paths['state'])
    monkeypatch.setattr(config, 'ACTIVE_RELEASE', paths['active'])


def test_backup_archives_data_and_prunes(tmp_path, monkeypatch):
    paths = _prepare(tmp_path)
    _patch_config(monkeypatch, paths)
    out = tmp_path / 'backups'
    first = backup.make_backup(out, keep=5)
    time.sleep(1.1)  # 保证文件名时间戳不同
    second = backup.make_backup(out, keep=1)

    assert Path(second['path']).is_file()
    assert second['pruned'] == [Path(first['path']).name]
    assert len(list(out.glob('policyrag-*.zip'))) == 1
    with zipfile.ZipFile(second['path']) as zf:
        names = set(zf.namelist())
        assert 'data/docs/doc-1/meta.json' in names
        assert 'data/releases/rel-1/manifest.json' in names
        assert 'data/state/active_release.json' in names
        assert '.env' in names
        meta = json.loads(zf.read('backup-meta.json'))
        assert meta['release_id'] == 'rel-1'


def test_restore_extracts_into_target(tmp_path, monkeypatch):
    paths = _prepare(tmp_path)
    _patch_config(monkeypatch, paths)
    archive = backup.make_backup(tmp_path / 'backups', keep=0)['path']
    restore_root = tmp_path / 'restored'
    result = backup.restore(archive, restore_root)
    assert result['restored'] >= 4
    assert (restore_root / 'data' / 'state' / 'active_release.json').is_file()
    assert (restore_root / '.env').read_text(encoding='utf-8').startswith('POLICYRAG_PORT')
