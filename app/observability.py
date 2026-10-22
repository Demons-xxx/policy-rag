"""可观测性（T12）：问答请求的分段耗时、JSON 行日志与进程内指标。

- 每次问答写一行 JSON 到 `log/app.log`（request_id、问题、路径、分段耗时、引用数、错误）；
- 进程内保留最近 500 次耗时用于 P50/P95，`/api/metrics` 只读内存，不扫日志文件；
- 日志写入失败（磁盘满/权限）不影响问答，只记一条 warning。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from app import config

logger = logging.getLogger(__name__)

MAX_SAMPLES = 500


def new_request_id() -> str:
    return os.urandom(4).hex()


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 1)


class Metrics:
    """进程内指标：请求数、错误数、分段耗时百分位。"""

    def __init__(self, max_samples: int = MAX_SAMPLES) -> None:
        self._lock = threading.Lock()
        self._samples: deque[dict] = deque(maxlen=max_samples)
        self.requests = 0
        self.errors = 0
        self.started_at = time.strftime('%Y-%m-%dT%H:%M:%S%z')

    def record(self, item: dict) -> None:
        with self._lock:
            self.requests += 1
            if item.get('error'):
                self.errors += 1
            self._samples.append(dict(item))

    def snapshot(self) -> dict:
        with self._lock:
            samples = list(self._samples)
            requests = self.requests
            errors = self.errors
        totals = [float(item.get('total_ms') or 0) for item in samples]
        retrievals = [float(item.get('retrieval_ms') or 0) for item in samples if item.get('retrieval_ms') is not None]
        first_tokens = [float(item.get('first_token_ms') or 0) for item in samples if item.get('first_token_ms') is not None]
        return {
            'requests': requests,
            'errors': errors,
            'error_rate': round(errors / requests, 4) if requests else 0.0,
            'window': len(samples),
            'started_at': self.started_at,
            'total_ms': {
                'avg': round(sum(totals) / len(totals), 1) if totals else 0.0,
                'p50': _percentile(totals, 0.5),
                'p95': _percentile(totals, 0.95),
            },
            'retrieval_ms': {'p50': _percentile(retrievals, 0.5), 'p95': _percentile(retrievals, 0.95)},
            'first_token_ms': {'p50': _percentile(first_tokens, 0.5), 'p95': _percentile(first_tokens, 0.95)},
        }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self.requests = 0
            self.errors = 0


metrics = Metrics()

_log_lock = threading.Lock()


def log_request(item: dict, path: Path | None = None) -> None:
    """追加一行 JSON 到 `log/app.log`；失败不影响问答。"""
    target = Path(path) if path is not None else config.LOGS_DIR / 'app.log'
    line = json.dumps(item, ensure_ascii=False) + '\n'
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with target.open('a', encoding='utf-8') as handle:
                handle.write(line)
    except OSError as exc:
        logger.warning('写请求日志失败：%s', exc)


__all__ = ['Metrics', 'metrics', 'log_request', 'new_request_id']
