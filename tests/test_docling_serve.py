# -*- coding: utf-8 -*-
"""常驻 docling worker：同一进程复用，第二次解析不再冷启动。"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from app import config
from app.ingest import docling

SAMPLE_PDF = Path(__file__).resolve().parents[1] / 'knowledge' / 'samples' / 'XX大学实验室安全管理办法（2023年版）.pdf'

pytestmark = pytest.mark.skipif(
    not Path(str(config.DOCLING_PYTHON or '')).is_file() or not docling.available(),
    reason='本机没有 docling 运行时',
)


def test_serve_mode_reuses_worker_process(tmp_path):
    source = tmp_path / 'sample.pdf'
    shutil.copy2(SAMPLE_PDF, source)
    docling.reset_client()
    try:
        t0 = time.perf_counter()
        first = docling.parse_pdf(source)
        first_secs = time.perf_counter() - t0
        t0 = time.perf_counter()
        second = docling.parse_pdf(source)
        second_secs = time.perf_counter() - t0
        assert first['page_count'] == second['page_count'] >= 1
        assert first['pages'] == second['pages']
        # 常驻模式下第二份不再加载模型，应显著更快（本机 0.4s vs 9s+）
        assert second_secs < first_secs / 2
        assert docling._client is not None and docling._client._proc.poll() is None
    finally:
        docling.reset_client()
