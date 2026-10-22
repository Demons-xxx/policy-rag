# -*- coding: utf-8 -*-
"""测试公共夹具：把解析器固定为 pymupdf。

本机 .env 可能把 ``POLICYRAG_PARSER_PDF`` 设成 docling（本地部署偏好），
但入库/OCR/页映射相关测试断言的是 pymupdf 的解析行为。为保证测试结果与
本机配置无关，默认强制 pymupdf；需要 docling 的测试显式传 ``parser="docling"``。
"""
from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def _force_pymupdf_default(monkeypatch):
    monkeypatch.setattr(config, "PARSER_PDF", "pymupdf")
