"""Office 文档转 PDF（Windows Word COM），用于 DOCX 原文页预览。

参考 RAG 项目的做法（PowerShell 调 Word.Application.ExportAsFixedFormat），
但不引入 pywin32 依赖：直接以 PowerShell 子进程调用 COM，失败时返回原因、
由入库流程记一条不阻断的提示，不影响 DOCX 文本入库。

非 Windows 或未安装 Word 时返回 False 并说明原因。
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

WORD_EXPORT_FORMAT_PDF = 17  # wdExportFormatPDF

#: 模块级开关便于测试替换（不直接改全局 os.name，避免污染其它模块）
_WINDOWS = os.name == 'nt'


def available() -> bool:
    return _WINDOWS


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def convert_docx_to_pdf(docx: Path, pdf: Path, timeout: float = 120.0) -> tuple[bool, str]:
    """把 DOCX 转成 PDF；成功返回 (True, '')，失败返回 (False, 中文原因)。"""
    docx = Path(docx)
    pdf = Path(pdf)
    if not docx.is_file():
        return False, f'源文件不存在：{docx}'
    if not _WINDOWS:
        return False, '仅 Windows + Word 环境支持 DOCX 页图生成'
    # Word 的导出路径相对它自己的工作目录，必须给绝对路径
    docx = docx.resolve()
    pdf = pdf.resolve()
    pdf.parent.mkdir(parents=True, exist_ok=True)
    if pdf.exists():
        try:
            pdf.unlink()
        except OSError:
            pass
    script = (
        '$ErrorActionPreference = "Stop";'
        '$word = New-Object -ComObject Word.Application;'
        '$word.Visible = $false;'
        '$word.DisplayAlerts = 0;'
        '$word.AutomationSecurity = 3;'
        'try {'
        f'$doc = $word.Documents.Open({_ps_quote(docx)}, $false, $true, $false);'
        'try {'
        f'$doc.ExportAsFixedFormat({_ps_quote(pdf)}, {WORD_EXPORT_FORMAT_PDF});'
        '} finally { $doc.Close(0) }'
        '} finally { $word.Quit() }'
    )
    try:
        completed = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-Command', script],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f'Word 转换超时（>{timeout:g}s）'
    except OSError as exc:
        return False, f'无法调用 PowerShell：{exc}'
    if not pdf.is_file() or pdf.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or '').strip().splitlines()[-3:]
        reason = '；'.join(line.strip() for line in detail if line.strip()) or '未生成 PDF'
        return False, f'Word 转换失败：{reason[:300]}'
    return True, ''


def pdf_pages(pdf: Path) -> int:
    """读取 PDF 页数；失败返回 0。"""
    try:
        import fitz

        with fitz.open(str(pdf)) as document:
            return int(document.page_count)
    except Exception:
        return 0


__all__ = ['available', 'convert_docx_to_pdf', 'pdf_pages']
