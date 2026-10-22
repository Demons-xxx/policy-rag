# -*- coding: utf-8 -*-
"""PDF 解析器选择（T16）：全局默认 + 单次覆盖、非法值报中文错误。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import config
from app.ingest import parser as parser_module
from app.ingest import stages

SAMPLE_PDF = Path(__file__).resolve().parents[1] / 'knowledge' / 'samples' / 'XX大学实验室安全管理办法（2023年版）.pdf'


def _copy_sample(tmp_path: Path) -> Path:
    target = tmp_path / 'sample.pdf'
    shutil.copy2(SAMPLE_PDF, target)
    return target


def test_parse_document_pymupdf_override(tmp_path):
    path = _copy_sample(tmp_path)
    parsed = parser_module.parse_document(path, parser='pymupdf')
    assert parsed['parser'] == 'pymupdf'
    assert parsed['page_count'] >= 1
    assert parsed['pages'][0]['text'].strip()


def test_parse_document_rejects_unknown_parser(tmp_path):
    path = _copy_sample(tmp_path)
    with pytest.raises(RuntimeError, match='不支持的 PDF 解析器'):
        parser_module.parse_document(path, parser='mineru')


def test_run_document_rejects_unknown_parser_before_parse(tmp_path):
    path = _copy_sample(tmp_path)
    with pytest.raises(ValueError, match='不支持的 PDF 解析器'):
        stages.run_document(path, parser='mineru')


@pytest.mark.skipif(
    not (Path(config.DOCLING_PYTHON).is_file() and Path(config.DOCLING_ARTIFACTS).is_dir()),
    reason='本机没有 docling 运行时或模型',
)
def test_parse_document_docling_override(tmp_path):
    path = _copy_sample(tmp_path)
    parsed = parser_module.parse_document(path, parser='docling')
    assert parsed['parser'] == 'docling'
    assert parsed['page_count'] >= 1
    assert parsed['pages'][0]['text'].strip()
