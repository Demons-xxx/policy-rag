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
import shutil
import time
from pathlib import Path

from app import config


class ReleaseError(ValueError):
    """发布目录不存在、正在生效等可预期的操作错误。"""


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


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob('*'):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


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
            'published_at': data.get('published_at') or '',
            'doc_count': int(data.get('doc_count') or 0),
            'unit_count': int(data.get('unit_count') or 0),
            'passage_count': int(data.get('passage_count') or 0),
            'collection': data.get('collection') or '',
            'size_bytes': _dir_size(path),
        })
    records.sort(key=lambda item: item.get('published_at') or '', reverse=True)
    return records


def _release_path(release_id: str) -> Path:
    release_id = str(release_id or '').strip()
    if not release_id or release_id in ('.', '..') or '/' in release_id or '\\' in release_id:
        raise ReleaseError(f'发布编号不合法：{release_id!r}')
    path = Path(config.RELEASES_DIR) / release_id
    if not (path / 'manifest.json').is_file():
        raise ReleaseError(f'发布不存在：{release_id}')
    return path


def _record_for(path: Path) -> dict:
    data = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    return {
        'schema': 1,
        'id': path.name,
        'dir': str(path.resolve()),
        'collection': data.get('collection') or collection_name(path.name),
        'published_at': data.get('published_at') or '',
        'doc_count': int(data.get('doc_count') or 0),
        'unit_count': int(data.get('unit_count') or 0),
        'passage_count': int(data.get('passage_count') or 0),
    }


def activate(release_id: str) -> dict:
    """把生效指针切到指定发布（回滚/前滚都用它），原子写。"""
    path = _release_path(release_id)
    previous = read_active()
    record = _record_for(path)
    write_active(record)
    return {'active': record, 'previous': (previous or {}).get('id')}


def delete_release(release_id: str) -> dict:
    """删除一个非生效中的发布目录。"""
    path = _release_path(release_id)
    active = read_active()
    if active and str(active.get('id')) == path.name:
        raise ReleaseError('不能删除正在生效的发布；请先切换到其他发布')
    shutil.rmtree(path)
    return {'removed': path.name}


def prune(keep: int = 5) -> dict:
    """只保留最近 keep 个发布；正在生效的发布无论新旧都保留。"""
    keep = max(1, int(keep or 1))
    active = read_active()
    active_id = str((active or {}).get('id') or '')
    records = list_releases()
    kept: list[str] = []
    removed: list[str] = []
    for index, record in enumerate(records):
        release_id = str(record.get('id'))
        if release_id == active_id or index < keep:
            kept.append(release_id)
            continue
        path = Path(config.RELEASES_DIR) / release_id
        try:
            shutil.rmtree(path)
            removed.append(release_id)
        except OSError:
            kept.append(release_id)
    return {'removed': removed, 'kept': kept[:keep], 'kept_total': len(kept)}
