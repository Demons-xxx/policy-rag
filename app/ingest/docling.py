"""Docling 解析后端（可选）：通过外部带 docling 的 Python 子进程解析 PDF。

PolicyRAG 主环境不装 docling——它依赖多、升级会影响现有 torch/transformers。
需要更强版式与表格时，把 ``POLICYRAG_PARSER_PDF=docling``，并用
``POLICYRAG_DOCLING_PYTHON`` 指向已装 docling 的解释器（默认尝试同盘
``../RAG/local_service/runtime/parse/Scripts/python.exe``），模型目录默认
``../RAG/model/docling``，因此可以直接复用 RAG 项目已下载的权重。

失败原因（解释器不存在、模型加载失败、超时）都以中文 RuntimeError 抛给入库任务。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / 'scripts' / 'docling_worker.py'


def available() -> bool:
    """是否有可用的 docling 解释器与 worker 脚本。"""
    python = getattr(config, 'DOCLING_PYTHON', None)
    return bool(python and Path(python).is_file() and WORKER.is_file())


def parse_pdf(path: Path) -> dict:
    """调用外部 docling 解释器解析一份 PDF，返回统一的 pages 结构。"""
    python = getattr(config, 'DOCLING_PYTHON', None)
    if not python or not Path(python).is_file():
        raise RuntimeError(
            '已选择 docling 解析，但没有可用的 docling 解释器；'
            '请设置 POLICYRAG_DOCLING_PYTHON 指向已装 docling 的 python.exe')
    if not WORKER.is_file():
        raise RuntimeError(f'缺少 docling worker 脚本：{WORKER}')
    artifacts = getattr(config, 'DOCLING_ARTIFACTS', None)
    device = str(getattr(config, 'DOCLING_DEVICE', 'cuda') or 'cuda')
    timeout = float(getattr(config, 'DOCLING_TIMEOUT', 600) or 600)
    with tempfile.TemporaryDirectory(prefix='docling-') as folder:
        command = [str(python), str(WORKER), '--input', str(path),
                   '--output', '-', '--device', device,
                   '--timeout', str(timeout)]
        if artifacts:
            command.extend(['--artifacts', str(artifacts)])
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       encoding='utf-8', errors='replace',
                                       timeout=timeout + 60, cwd=str(ROOT))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'Docling 解析超时（>{timeout:g}s）：{path.name}') from exc
        marker = '__DOCLING_JSON__'
        text = completed.stdout or ''
        index = text.rfind(marker)
        if index < 0:
            detail = (completed.stderr or text or '').strip()[-500:]
            raise RuntimeError(f'Docling 子进程没有产出结果：{detail}')
        payload = json.loads(text[index + len(marker):])
    if payload.get('error'):
        raise RuntimeError(str(payload['error']))
    pages = payload.get('pages') or []
    if not pages:
        raise RuntimeError(f'Docling 未解析出任何页面文本：{path.name}')
    page_count = int(payload.get('page_count') or len(pages))
    return {
        'format': 'pdf',
        'pages': [{'no': int(page.get('no') or index + 1),
                   'text': str(page.get('text') or '')}
                  for index, page in enumerate(pages)],
        'has_pdf': True,
        'page_count': page_count,
        'tables': [],
        'parser': 'docling',
        'docling_tables': int(payload.get('tables') or 0),
        'docling_seconds': float(payload.get('seconds') or 0.0),
    }


__all__ = ['available', 'parse_pdf']
