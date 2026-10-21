"""发布存储：把「一次发布」当成一个带版本目录的不可变产物。

文件布局（相对项目根）：
    data/releases/<release_id>/units.jsonl|passages.jsonl|vectors.npz|manifest.json
    data/state/active_release.json        # 指向当前发布目录

上层（runtime / api / publish）只通过本模块读写发布，不直接拼路径，
这样将来换成对象存储或数据库时只改这里。兼容门面已移除，统一从这里导入。
"""
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
    """原子替换 active_release.json：先写临时文件再 rename。"""
    target = Path(config.ACTIVE_RELEASE)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, target)


def release_dir(record: dict) -> Path:
    path = Path(record['dir'])
    return path if path.is_absolute() else config.ROOT / path


def load_manifest(record: dict) -> dict:
    path = release_dir(record) / 'manifest.json'
    return json.loads(path.read_text(encoding='utf-8'))


def collection_name(release_id: str) -> str:
    """Chroma 集合名只允许字母数字下划线连字符，其他字符替换为下划线。"""
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in release_id)
    return 'policy_' + safe


def new_release_id() -> str:
    return 'rel-' + time.strftime('%Y%m%d-%H%M%S')


def list_releases() -> list[dict]:
    """按目录列出所有发布，最新在前（只读 manifest，不加载向量）。"""
    records: list[dict] = []
    root = Path(config.RELEASES_DIR)
    if not root.is_dir():
        return records
    for path in root.iterdir():
        manifest = path / 'manifest.json'
        if not path.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding='utf-8'))
        except Exception:
            continue
        records.append({
            'id': path.name,
            'dir': str(path),
            'created_at': data.get('created_at', ''),
            'document_count': data.get('document_count', 0),
            'unit_count': data.get('unit_count', 0),
            'passage_count': data.get('passage_count', 0),
        })
    records.sort(key=lambda item: item.get('created_at') or '', reverse=True)
    return records
