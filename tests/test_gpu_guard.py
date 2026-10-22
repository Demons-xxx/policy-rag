"""GPU 并发保护（T13）：信号量限流、等待日志、超时失败。"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from app.retrieval import gpu_guard


def test_semaphore_limits_concurrency_to_two():
    gpu_guard.reset(2)
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker() -> None:
        nonlocal active, peak
        barrier.wait()
        with gpu_guard.gpu_slot('编码'):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 2


def test_waiting_is_logged(caplog):
    gpu_guard.reset(1)
    started = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with gpu_guard.gpu_slot('重排'):
            started.set()
            release.wait(2.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(1.0)
    caplog.set_level(logging.INFO)
    with gpu_guard.gpu_slot('编码', timeout=3.0):
        pass
    thread.join()
    release.set()
    assert any('等待 编码 槽位' in record.message for record in caplog.records)


def test_timeout_raises_without_occupying_slot():
    gpu_guard.reset(1)
    started = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with gpu_guard.gpu_slot('重排'):
            started.set()
            release.wait(2.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(1.0)
    with pytest.raises(TimeoutError):
        with gpu_guard.gpu_slot('编码', timeout=0.05):
            pass
    release.set()
    thread.join()


def test_reset_uses_config_default(monkeypatch):
    import app.retrieval.gpu_guard as guard

    monkeypatch.setattr(guard, '_semaphore', None)
    monkeypatch.setattr('app.config.GPU_CONCURRENCY', 3)
    semaphore = guard._current()
    assert semaphore._initial_value == 3
