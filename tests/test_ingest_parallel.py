# -*- coding: utf-8 -*-
"""多文件入库并发：4 份文件并行解析，进度回写、结果顺序稳定。"""
from __future__ import annotations

import time
from pathlib import Path

from app.ingest import pipeline

FILES = [{'name': '甲.pdf', 'saved_name': 'a.pdf', 'path': f'D:/tmp/a{i}.pdf'} for i in range(4)]


class FakeCatalog:
    def __init__(self, *args, **kwargs):
        self.updates = []

    def get_job(self, job_id):
        return {'payload': {'files': FILES, 'meta': {}}}

    def update_job(self, job_id, **fields):
        self.updates.append(fields)

    def record_audit(self, *args, **kwargs):
        return None

    def close(self):
        return None


def test_run_job_parses_files_concurrently(monkeypatch):
    catalog = FakeCatalog()
    monkeypatch.setattr(pipeline, 'Catalog', lambda *a, **k: catalog)
    monkeypatch.setattr(pipeline.config, 'INGEST_CONCURRENCY', 4)

    def fake_ingest(path, meta=None, job_id=None, parser=None):
        time.sleep(0.25)
        name = Path(str(path)).name
        return {'doc_id': 'doc-' + name, 'title': name, 'unit_count': 1,
                'deduplicated': False, 'issues': []}

    monkeypatch.setattr(pipeline, 'ingest_path', fake_ingest)
    started = time.perf_counter()
    pipeline.run_job('job-parallel')
    elapsed = time.perf_counter() - started

    final = catalog.updates[-1]
    assert final['status'] == 'done'
    assert final['progress'] == 100
    titles = [doc['title'] for doc in final['payload']['docs']]
    assert titles == ['a0.pdf', 'a1.pdf', 'a2.pdf', 'a3.pdf']  # 顺序稳定
    assert elapsed < 0.8  # 串行需要约 1.0s
    assert any(u.get('progress') == 100 for u in catalog.updates)


def test_run_job_keeps_single_file_errors(monkeypatch):
    catalog = FakeCatalog()
    monkeypatch.setattr(pipeline, 'Catalog', lambda *a, **k: catalog)
    monkeypatch.setattr(pipeline.config, 'INGEST_CONCURRENCY', 4)

    def fake_ingest(path, meta=None, job_id=None, parser=None):
        name = Path(str(path)).name
        if name.endswith('2.pdf'):
            raise RuntimeError('第 2 页 OCR 失败：端点超时')
        return {'doc_id': 'doc-' + name, 'title': name, 'unit_count': 1,
                'deduplicated': False, 'issues': []}

    monkeypatch.setattr(pipeline, 'ingest_path', fake_ingest)
    pipeline.run_job('job-error')

    final = catalog.updates[-1]
    assert final['status'] == 'failed'
    assert len(final['payload']['docs']) == 3
    assert any('OCR 失败' in err for err in final['payload']['errors'])
