"""Docling 解析后端（可选）：通过外部带 docling 的 Python 子进程解析 PDF。

PolicyRAG 主环境不装 docling——它依赖多、升级会影响现有 torch/transformers。
需要更强版式与表格时，把 ``POLICYRAG_PARSER_PDF=docling``，并用
``POLICYRAG_DOCLING_PYTHON`` 指向已装 docling 的解释器（默认项目内
``.venv-docling``，其次同盘 RAG 的 parse 运行时），模型目录默认 ``models/docling``。

默认用**常驻 worker**（``--serve``）：模型只加载一次，连续解析省掉 6–9 秒冷启动；
常驻进程异常退出时自动重启一次，再失败回退一次性子进程。
失败原因（解释器不存在、模型加载失败、超时）都以中文 RuntimeError 抛给入库任务。
"""
from __future__ import annotations

import atexit
import json
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / 'scripts' / 'docling_worker.py'
MARKER = '__DOCLING_JSON__'


def _runtime() -> tuple[str, Path, str | None, str, float]:
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
    return str(python), WORKER, artifacts, device, timeout


def available() -> bool:
    """是否有可用的 docling 解释器与 worker 脚本。"""
    python = getattr(config, 'DOCLING_PYTHON', None)
    return bool(python and Path(python).is_file() and WORKER.is_file())


def _payload_to_parsed(payload: dict, path: Path) -> dict:
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


def _parse_once(path: Path) -> dict:
    """一次性子进程（常驻模式不可用时的兜底）。"""
    python, worker, artifacts, device, timeout = _runtime()
    command = [str(python), str(worker), '--input', str(path),
               '--output', '-', '--device', device, '--timeout', str(timeout)]
    if artifacts:
        command.extend(['--artifacts', str(artifacts)])
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace',
                                   timeout=timeout + 60, cwd=str(ROOT))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'Docling 解析超时（>{timeout:g}s）：{path.name}') from exc
    text = completed.stdout or ''
    index = text.rfind(MARKER)
    if index < 0:
        detail = (completed.stderr or text or '').strip()[-500:]
        raise RuntimeError(f'Docling 子进程没有产出结果：{detail}')
    return _payload_to_parsed(json.loads(text[index + len(MARKER):]), path)


class _ServeClient:
    """常驻 docling worker 的客户端：单进程 + 串行请求 + 超时读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stderr: deque[str] = deque(maxlen=200)
        self._drainer: threading.Thread | None = None

    # -- 进程管理 ---------------------------------------------------------
    def _start(self) -> None:
        python, worker, artifacts, device, _timeout = _runtime()
        command = [str(python), str(worker), '--serve']
        if artifacts:
            command.extend(['--artifacts', str(artifacts)])
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(ROOT),
            text=True, encoding='utf-8', errors='replace', bufsize=1,
        )
        self._stderr.clear()
        self._drainer = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drainer.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.strip()
            if line:
                self._stderr.append(line)

    def _ensure(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None
        return self._proc

    def restart(self) -> None:
        self.close()
        with self._lock:
            self._ensure()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - 退出清理失败不报
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # -- 请求 / 响应 ------------------------------------------------------
    def request(self, path: Path) -> dict:
        with self._lock:
            proc = self._ensure()
            python, worker, artifacts, device, timeout = _runtime()
            payload = {
                'input': str(path), 'device': device,
                'artifacts': str(artifacts) if artifacts else None,
                'timeout': timeout,
            }
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
                proc.stdin.flush()
            except OSError as exc:
                raise RuntimeError(f'Docling 常驻进程写入失败：{exc}') from exc
            assert proc.stdout is not None
            result: queue.Queue = queue.Queue(maxsize=1)

            def _read() -> None:
                try:
                    result.put(proc.stdout.readline())
                except Exception as exc:  # noqa: BLE001
                    result.put(exc)

            reader = threading.Thread(target=_read, daemon=True)
            reader.start()
            try:
                line = result.get(timeout=timeout + 60)
            except queue.Empty as exc:
                self.close()
                raise RuntimeError(f'Docling 解析超时（>{timeout:g}s）：{path.name}') from exc
            if isinstance(line, Exception):
                raise RuntimeError(f'Docling 常驻进程读取失败：{line}')
            line = (line or '').strip()
            if not line:
                detail = '；'.join(list(self._stderr)[-5:])
                self.close()
                raise RuntimeError(f'Docling 常驻进程意外退出：{detail or "无输出"}')
            index = line.rfind(MARKER)
            if index < 0:
                raise RuntimeError(f'Docling 常驻进程返回异常：{line[:300]}')
            return json.loads(line[index + len(MARKER):])


_client: _ServeClient | None = None


def _get_client() -> _ServeClient:
    global _client
    if _client is None:
        _client = _ServeClient()
        atexit.register(_client.close)
    return _client


def reset_client() -> None:
    """测试/运维用：关闭常驻进程，下次调用重新拉起。"""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def parse_pdf(path: Path) -> dict:
    """解析一份 PDF：默认走常驻 worker，失败重启一次，再失败回退一次性子进程。"""
    path = Path(path)
    use_serve = bool(getattr(config, 'DOCLING_SERVE', True))
    if not use_serve:
        return _parse_once(path)
    client = _get_client()
    try:
        payload = client.request(path)
    except RuntimeError:
        # 常驻进程可能被外部杀掉/显存被占：重启一次再试
        try:
            client.restart()
            payload = client.request(path)
        except RuntimeError:
            return _parse_once(path)
    return _payload_to_parsed(payload, path)


__all__ = ['available', 'parse_pdf', 'reset_client']
