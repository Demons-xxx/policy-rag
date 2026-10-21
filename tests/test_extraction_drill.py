"""抽包演练的回归守卫：内核必须能脱离本项目单独跑起来。

脚本会把 docs/架构.md §6 的清单复制到临时目录，用假编码器 + 假模型完成
「造发布 → 检索 → 证据 → 问答事件流」，任何跨模块隐式依赖都会让它失败。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_extraction_drill_runs_standalone():
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'drill_extract.py')],
        cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'DRILL OK' in completed.stdout, completed.stdout
