"""GPU 并发保护（T13）：编码与重排共享一个信号量，避免并发请求把显存打爆。

- 默认并发 2（`RAG_GPU_CONCURRENCY`），超出的请求排队而不是同时上 GPU；
- 排队超过 0.2s 记一条 info 日志，便于发现容量不足；
- 只保护「真正占显存」的推理段，检索/生成/流式输出不占槽位。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from app import config

logger = logging.getLogger(__name__)

WAIT_LOG_SECONDS = 0.2

_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None


def _current() -> threading.BoundedSemaphore:
    global _semaphore
    with _lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(max(1, int(config.GPU_CONCURRENCY or 2)))
        return _semaphore


def reset(size: int | None = None) -> None:
    """测试或运行期改配置后重建信号量。"""
    global _semaphore
    with _lock:
        _semaphore = threading.BoundedSemaphore(max(1, int(size or config.GPU_CONCURRENCY or 2)))


@contextmanager
def gpu_slot(name: str = '推理', timeout: float | None = None):
    """占用一个 GPU 槽位；timeout=None 表示一直排队等待。"""
    semaphore = _current()
    started = time.perf_counter()
    acquired = semaphore.acquire(timeout=timeout) if timeout is not None else semaphore.acquire()
    waited = time.perf_counter() - started
    if not acquired:
        raise TimeoutError(f'等待 {name} 槽位超时（{timeout:g}s）')
    if waited >= WAIT_LOG_SECONDS:
        logger.info('等待 %s 槽位 %.2fs（并发上限 %s）', name, waited, getattr(semaphore, '_initial_value', '?'))
    try:
        yield waited
    finally:
        semaphore.release()


__all__ = ['gpu_slot', 'reset', 'WAIT_LOG_SECONDS']
