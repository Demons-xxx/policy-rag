"""发布指针：读写 data/state/active_release.json，所有发布都原子替换。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app import config


def read_active() -> dict | None:
    path = config.ACTIVE_RELEASE
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(record, dict) or not record.get('id') or not record.get('dir'):
        return None
    return record


def write_active(record: dict) -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = config.ACTIVE_RELEASE.with_name(config.ACTIVE_RELEASE.name + '.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, config.ACTIVE_RELEASE)


def release_dir(record: dict) -> Path:
    path = Path(record['dir'])
    return path if path.is_absolute() else config.ROOT / path


def load_manifest(record: dict) -> dict:
    path = release_dir(record) / 'manifest.json'
    return json.loads(path.read_text(encoding='utf-8'))


def collection_name(release_id: str) -> str:
    """Chroma 集合名只允许字母数字下划线连字符。"""
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in release_id)
    return 'policy_' + safe


def new_release_id() -> str:
    return 'rel-' + time.strftime('%Y%m%d-%H%M%S')
