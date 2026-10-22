# -*- coding: utf-8 -*-
"""本地 OCR（RapidOCR）测试：真实权重在 models/rapidocr，图片页端到端补文本。"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import fitz
import pytest

from app import config
from app.ingest import parser as parser_module
from app.ingest.ocr import LocalOcrClient

SAMPLE_PDF = Path(__file__).resolve().parents[1] / 'knowledge' / 'samples' / 'XX大学实验室安全管理办法（2023年版）.pdf'
MODEL_DIR = Path(config.OCR_MODEL_DIR)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec('rapidocr_onnxruntime') is None
    or not (MODEL_DIR / 'ch_PP-OCRv4_det_infer.onnx').is_file(),
    reason='本机没有 rapidocr 或 models/rapidocr 权重',
)


def _render_first_page(pdf: Path, out_png: Path, dpi: int = 200) -> None:
    with fitz.open(str(pdf)) as document:
        pixmap = document[0].get_pixmap(dpi=dpi)
        pixmap.save(str(out_png))


def test_local_ocr_client_reads_rendered_page(tmp_path):
    image = tmp_path / 'page.png'
    _render_first_page(SAMPLE_PDF, image)
    text, meta = LocalOcrClient(MODEL_DIR).recognize(image)
    assert meta['engine'] == 'rapidocr'
    assert meta['lines'] >= 5
    assert '实验室' in text


def test_parser_fills_scanned_page_with_local_ocr(tmp_path):
    image = tmp_path / 'page.png'
    _render_first_page(SAMPLE_PDF, image)
    # 反向构造「扫描件」：整页只有图片，没有文本层
    scanned = tmp_path / 'scanned.pdf'
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_image(page.rect, filename=str(image))
        document.save(str(scanned))

    parsed = parser_module.parse_document(scanned, parser='pymupdf')
    assert parsed.get('ocr_pages', 0) >= 1
    assert parsed['pages'][0].get('ocr') is True
    assert '实验室' in parsed['pages'][0]['text']
