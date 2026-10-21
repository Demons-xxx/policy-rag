"""发布指针（兼容门面）：实现已迁移到 app/store/releases.py。"""
from __future__ import annotations

from app.store.releases import (
    collection_name,
    list_releases,
    load_manifest,
    new_release_id,
    read_active,
    release_dir,
    write_active,
)

__all__ = [
    'collection_name', 'list_releases', 'load_manifest', 'new_release_id',
    'read_active', 'release_dir', 'write_active',
]
