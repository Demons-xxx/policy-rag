"""Docling 解析 worker（由外部带 docling 的 Python 解释器执行）。

两种运行方式：

1. 一次性：``python docling_worker.py --input a.pdf --output -``，解析一份后退出；
2. 常驻（推荐）：``python docling_worker.py --serve``，从 stdin 逐行读请求
   ``{"input": "...", "device": "cuda", "artifacts": "...", "timeout": 600}``，
   每行回一个 ``__DOCLING_JSON__{...}``。模型只加载一次，连续解析省掉每次
   6–9 秒的冷启动（主进程 ``app/ingest/docling.py`` 默认复用常驻模式）。

输出结构与主解析器一致：

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

MARKER = '__DOCLING_JSON__'


def _page_of(item) -> int:
    prov = getattr(item, 'prov', None) or []
    for entry in prov:
        page = getattr(entry, 'page_no', None)
        if isinstance(page, int) and page > 0:
            return page
    return 0


def build_converter(device: str, artifacts: str | None, timeout: float):
    """按设备/模型目录构造 DocumentConverter；serve 模式按 key 缓存复用。"""
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
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def convert(pdf: Path, device: str, artifacts: str | None, timeout: float,
            converter=None) -> dict:
    if converter is None:
        converter = build_converter(device, artifacts, timeout)
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


def _emit(payload: dict, output: str) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if output == '-':
        # 子进程运行在受限 token 下时可能写不了父进程创建的临时目录，直接走 stdout
        sys.stdout.write(MARKER + text + '\n')
        sys.stdout.flush()
    else:
        Path(output).write_text(text, encoding='utf-8')


def serve() -> int:
    """常驻模式：逐行请求/响应，模型按 (device, artifacts, timeout) 缓存。"""
    cache: dict[tuple, object] = {}
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        request_id = ''
        try:
            request = json.loads(line)
            request_id = str(request.get('id') or '')
            device = str(request.get('device') or 'cuda')
            artifacts = request.get('artifacts') or None
            timeout = float(request.get('timeout') or 600.0)
            key = (device, str(artifacts), timeout)
            converter = cache.get(key)
            if converter is None:
                converter = build_converter(device, artifacts, timeout)
                cache[key] = converter
            payload = convert(Path(request['input']), device, artifacts, timeout, converter=converter)
        except Exception as exc:  # 单份失败不退出常驻进程
            payload = {'error': f'Docling 解析失败：{type(exc).__name__}: {exc}'}
        if request_id:
            payload['id'] = request_id
        try:
            _emit(payload, '-')
        except (BrokenPipeError, OSError):
            return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Docling PDF 解析 worker')
    parser.add_argument('--serve', action='store_true', help='常驻模式：stdin 逐行请求')
    parser.add_argument('--input', default=None)
    parser.add_argument('--output', default='-')
    parser.add_argument('--device', default='cuda')
    parser.add_argument('--artifacts', default=None)
    parser.add_argument('--timeout', type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.serve:
        return serve()
    if not args.input:
        parser.error('一次性模式需要 --input')
    try:
        payload = convert(Path(args.input), args.device, args.artifacts, args.timeout)
        code = 0
    except Exception as exc:  # 子进程失败时也要给主进程可读的中文原因
        payload = {'error': f'Docling 解析失败：{type(exc).__name__}: {exc}'}
        code = 1
    _emit(payload, args.output)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
