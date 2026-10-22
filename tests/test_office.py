"""DOCX 页图（office.py）测试：不依赖本机 Word，用假 subprocess 校验脚本与失败路径。"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.ingest import office


def _fake_run(write_pdf: bool, returncode: int = 0, stderr: str = ''):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        script = cmd[-1]
        if write_pdf:
            pdf = Path(script.split("ExportAsFixedFormat('")[1].split("'")[0])
            pdf.write_bytes(b'%PDF-1.7 fake')
        return SimpleNamespace(returncode=returncode, stdout='', stderr=stderr)

    return run, calls


def test_missing_source_file_is_reported(tmp_path):
    ok, reason = office.convert_docx_to_pdf(tmp_path / 'missing.docx', tmp_path / 'out.pdf')
    assert ok is False
    assert '源文件不存在' in reason


def test_non_windows_is_reported(tmp_path, monkeypatch):
    source = tmp_path / 'a.docx'
    source.write_bytes(b'PK')
    monkeypatch.setattr(office, '_WINDOWS', False)
    ok, reason = office.convert_docx_to_pdf(source, tmp_path / 'out.pdf')
    assert ok is False
    assert '仅 Windows' in reason


def test_success_uses_absolute_paths_and_reports_pages(tmp_path, monkeypatch):
    source = tmp_path / 'a.docx'
    source.write_bytes(b'PK')
    out = tmp_path / 'source.pdf'
    run, calls = _fake_run(write_pdf=True)
    monkeypatch.setattr(office, '_WINDOWS', True)
    monkeypatch.setattr(office.subprocess, 'run', run)
    monkeypatch.setattr(office, 'pdf_pages', lambda path: 3)

    ok, reason = office.convert_docx_to_pdf(source, out)

    assert ok is True and reason == ''
    script = calls[0][-1]
    assert str(source.resolve()) in script
    assert str(out.resolve()) in script
    assert 'Word.Application' in script
    assert 'ExportAsFixedFormat' in script


def test_failure_reason_contains_ps_detail(tmp_path, monkeypatch):
    source = tmp_path / 'a.docx'
    source.write_bytes(b'PK')
    run, _ = _fake_run(write_pdf=False, returncode=1, stderr='COMException: 拒绝访问')
    monkeypatch.setattr(office, '_WINDOWS', True)
    monkeypatch.setattr(office.subprocess, 'run', run)

    ok, reason = office.convert_docx_to_pdf(source, tmp_path / 'out.pdf')

    assert ok is False
    assert 'Word 转换失败' in reason
    assert 'COMException' in reason


def test_timeout_is_reported(tmp_path, monkeypatch):
    source = tmp_path / 'a.docx'
    source.write_bytes(b'PK')

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get('timeout', 1))

    monkeypatch.setattr(office, '_WINDOWS', True)
    monkeypatch.setattr(office.subprocess, 'run', boom)

    ok, reason = office.convert_docx_to_pdf(source, tmp_path / 'out.pdf', timeout=3)
    assert ok is False
    assert '超时' in reason


def test_pdf_pages_returns_zero_for_unknown_file(tmp_path):
    assert office.pdf_pages(tmp_path / 'nope.pdf') == 0
