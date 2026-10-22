"""发布管理接口：列表、回滚（切换生效指针）、清理、删除。

发布目录是不可变产物；回滚只改 ``data/state/active_release.json``（原子写），
检索器在下一次请求时发现 release_id 变化会自动重新装载。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.store import releases as release_store

logger = logging.getLogger(__name__)
router = APIRouter()


class PruneRequest(BaseModel):
    keep: int = Field(default=5, ge=1, le=100, description='保留多少个最新发布（生效中的始终保留）')


@router.get('/releases')
def list_releases() -> dict:
    """发布历史（最新在前），并标出当前生效版本。"""
    active = release_store.read_active()
    active_id = str((active or {}).get('id') or '')
    items = []
    for record in release_store.list_releases():
        item = dict(record)
        item['active'] = str(item.get('id')) == active_id
        item.pop('dir', None)
        items.append(item)
    return {'active': active_id or None, 'releases': items}


@router.post('/releases/{release_id}/activate')
def activate_release(release_id: str) -> dict:
    """把生效指针切到指定发布（回滚/前滚），返回切换前后的编号。"""
    try:
        result = release_store.activate(release_id)
    except release_store.ReleaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info('切换生效发布：%s -> %s', result.get('previous'), release_id)
    return {'ok': True, **result}


@router.post('/releases/prune')
def prune_releases(payload: PruneRequest) -> dict:
    """只保留最近 keep 个发布，删除其余（生效中的始终保留）。"""
    result = release_store.prune(payload.keep)
    logger.info('清理发布：删除 %d 个，保留 %d 个', len(result['removed']), result['kept_total'])
    return {'ok': True, **result}


@router.delete('/releases/{release_id}')
def delete_release(release_id: str) -> dict:
    """删除一个非生效中的发布目录。"""
    try:
        result = release_store.delete_release(release_id)
    except release_store.ReleaseError as exc:
        code = 409 if '正在生效' in str(exc) else 404
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    logger.info('删除发布：%s', release_id)
    return {'ok': True, **result}
