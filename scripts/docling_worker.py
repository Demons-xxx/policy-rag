"""Docling 解析 worker（由外部带 docling 的 Python 解释器执行）。

PolicyRAG 主环境不安装 docling（体积大、依赖重），需要 Docling 版式/表格解析时
用 ``POLICYRAG_DOCLING_PYTHON`` 指向一个已装 docling 的解释器，由本脚本以子进程
方式跑完整份 PDF，把结果写成与主解析器一致的 pages 结构：

    {"pages": [{"no": 1, "text": "..."}], "page_count": 3,
     "tables": 2, "parser": "docling", "seconds": 1.23}

表格用 Docling 的 markdown 导出（pipe 表格），主项目 structure 模块会把它切成
``table_row`` 单元；其余文本按页顺序拼回，保持条款行原样。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _page_of(item) -> int:
    prov = getattr(item, 'prov', None) or []
    for entry in prov:
        page = getattr(entry, 'page_no', None)
        if isinstance(page, int) and page > 0:
            return page
    return 0


def convert(pdf: Path, device: str, artifacts: str | None, timeout: float) -> dict:
    os.environ.setdefault('USE_TF', '0')
    os.environ.setdefault('USE_FLAX', '0')
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (AcceleratorOptions, PdfPipelineOptions,
                                                    TableFormerMode)
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
        enable_remote_services=False,
        document_timeout=timeout,
        accelerator_options=AcceleratorOptions(device=device, num_threads=4),
    )
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    if artifacts:
        options.artifacts_path = Path(artifacts)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

    started = time.perf_counter()
    result = converter.convert(pdf)
    document = result.document

    pages: dict[int, list[str]] = {}
    table_count = 0
    current = 1
    for item, _level in document.iterate_items():
        page = _page_of(item) or current
        current = page
        label = str(getattr(item, 'label', '') or '')
        if label == 'table':
            try:
                text = item.export_to_markdown(document)
            except Exception:
                text = ''
            table_count += 1
        else:
            text = str(getattr(item, 'text', '') or '')
        if text.strip():
            pages.setdefault(page, []).append(text.strip())

    page_items = [{'no': number, 'text': '\n'.join(pages[number])}
                  for number in sorted(pages)]
    return {
        'pages': page_items,
        'page_count': max((item['no'] for item in page_items), default=0),
        'tables': table_count,
        'parser': 'docling',
        'seconds': round(time.perf_counter() - started, 3),
        'status': str(result.status),
    }


MARKER = '__DOCLING_JSON__'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Docling PDF 解析 worker')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--device', default='cuda')
    parser.add_argument('--artifacts', default=None)
    parser.add_argument('--timeout', type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        payload = convert(Path(args.input), args.device, args.artifacts, args.timeout)
        code = 0
    except Exception as exc:  # 子进程失败时也要给主进程可读的中文原因
        payload = {'error': f'Docling 解析失败：{type(exc).__name__}: {exc}'}
        code = 1
    text = json.dumps(payload, ensure_ascii=False)
    if args.output == '-':
        # 子进程运行在受限 token 下时可能写不了父进程创建的临时目录，直接走 stdout
        sys.stdout.write(MARKER + text + '\n')
        sys.stdout.flush()
    else:
        Path(args.output).write_text(text, encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
