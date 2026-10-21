"""资料入库与发布接口（契约 4）。

上传 → 任务 → 查询 → 重试 / 删除 → 手动发布。真正的解析、结构化、入库和
发布逻辑由 ingest-dev 的 `app.ingest.pipeline` / `app.ingest.publish` 提供，
这里只做参数校验、任务登记与 HTTP 包装。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import threading
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from app import config

router = APIRouter()
log = logging.getLogger(__name__)

# 扩展名 → format 字段
ALLOWED_EXTENSIONS = {'.pdf': 'pdf', '.docx': 'docx', '.md': 'md', '.txt': 'txt'}
MAX_FILE_BYTES = 50 * 1024 * 1024
_SAFE_DOC_ID = re.compile(r'^doc-[A-Za-z0-9_-]+$')


def _pipeline():
    try:
        from app.ingest import pipeline
    except Exception as exc:  # 并行开发期模块可能还没落地
        raise HTTPException(status_code=503, detail=f'入库服务尚未就绪：{exc}') from exc
    return pipeline


def _publish_module():
    try:
        from app.ingest import publish
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f'发布服务尚未就绪：{exc}') from exc
    return publish


def _catalog():
    from app.store.catalog import Catalog
    return Catalog(config.CATALOG_DB)


def _close_catalog(catalog) -> None:
    closer = getattr(catalog, 'close', None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _task(job: dict) -> dict:
    """任务列表只保留前端需要的字段，避免把上传字节等内容带出去。"""
    fields = ('job_id', 'id', 'kind', 'status', 'phase', 'progress', 'message',
              'error', 'created_at', 'updated_at', 'payload')
    task = {key: job.get(key) for key in fields if key in job}
    task.setdefault('id', job.get('job_id'))
    return task


def _load_job(pipeline, job_id: str) -> dict:
    try:
        job = pipeline.job_status(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail='任务不存在') from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'读取任务失败：{exc}') from exc
    if not job:
        raise HTTPException(status_code=404, detail='任务不存在')
    return job


def _job_doc_ids(job: dict) -> list[str]:
    """从任务 payload 里找出本批新增的 doc_id（不同实现字段名可能是 docs/documents/doc_ids）。"""
    ids: list[str] = []

    def add(value) -> None:
        if isinstance(value, str) and value:
            ids.append(value)
        elif isinstance(value, dict):
            doc_id = value.get('doc_id') or value.get('id')
            if doc_id:
                ids.append(str(doc_id))

    payload = job.get('payload')
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}

    containers: list = []
    if isinstance(payload, dict):
        containers.extend(payload.get(key) for key in ('doc_ids', 'docs', 'documents'))
    containers.extend(job.get(key) for key in ('doc_ids', 'docs'))
    for container in containers:
        if isinstance(container, list):
            for item in container:
                add(item)
        elif isinstance(container, str):
            add(container)

    seen: set[str] = set()
    ordered: list[str] = []
    for doc_id in ids:
        if doc_id not in seen:
            seen.add(doc_id)
            ordered.append(doc_id)
    return ordered


def _remove_record(catalog, doc_id: str) -> bool:
    """删除登记行：优先用 Catalog 的 delete_document，没有就直接删表里的行。"""
    if catalog is not None:
        remover = getattr(catalog, 'delete_document', None)
        if callable(remover):
            try:
                remover(doc_id)
                return True
            except Exception as exc:
                log.warning('Catalog.delete_document 失败 %s: %s', doc_id, exc)
    if not Path(config.CATALOG_DB).is_file():
        return False
    try:
        with sqlite3.connect(config.CATALOG_DB) as connection:
            cursor = connection.execute('DELETE FROM documents WHERE doc_id = ?', (doc_id,))
            connection.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f'删除文档登记失败：{exc}') from exc


def _remove_documents(job: dict) -> tuple[list[str], list[str]]:
    doc_ids = _job_doc_ids(job)
    try:
        catalog = _catalog()
    except Exception as exc:
        log.warning('目录库不可用，仅删除文件：%s', exc)
        catalog = None

    removed: list[str] = []
    missing: list[str] = []
    try:
        for doc_id in doc_ids:
            if not _SAFE_DOC_ID.match(doc_id):
                missing.append(doc_id)
                continue
            directory = Path(config.DOCS_DIR) / doc_id
            deleted_file = False
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=False)
                deleted_file = True
            deleted_record = _remove_record(catalog, doc_id)
            if deleted_file or deleted_record:
                removed.append(doc_id)
            else:
                missing.append(doc_id)
    finally:
        _close_catalog(catalog)
    return removed, missing


@router.get('/ingest')
def api_ingest_list() -> dict:
    try:
        catalog = _catalog()
        try:
            jobs = catalog.list_jobs(limit=20) or []
        finally:
            _close_catalog(catalog)
    except Exception as exc:  # 目录库还没建好时不要让页面报错
        log.warning('读取入库任务失败：%s', exc)
        return {'tasks': []}
    return {'tasks': [_task(job) for job in jobs if isinstance(job, dict)]}


@router.post('/ingest')
async def api_ingest_upload(files: list[UploadFile] = File(...),
                            metadata: str | None = Form(None)) -> dict:
    uploads: list[tuple[str, bytes]] = []
    docs: list[dict] = []

    for upload in files:
        name = Path(upload.filename or '').name
        if not name:
            raise HTTPException(status_code=400, detail='上传文件缺少文件名')
        extension = Path(name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400,
                                detail=f'不支持的文件类型：{name}（仅支持 pdf/docx/md/txt）')
        size = getattr(upload, 'size', None)
        if isinstance(size, int) and size > MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail=f'文件过大：{name}（单文件上限 50MB）')
        data = await upload.read()
        await upload.close()
        if not data:
            raise HTTPException(status_code=400, detail=f'文件内容为空：{name}')
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail=f'文件过大：{name}（单文件上限 50MB）')
        uploads.append((name, data))
        docs.append({'name': name, 'size': len(data), 'format': ALLOWED_EXTENSIONS[extension]})

    if not uploads:
        raise HTTPException(status_code=400, detail='请至少上传一个文件')

    meta = None
    if metadata and metadata.strip():
        try:
            meta = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400,
                                detail=f'metadata 不是合法 JSON：{exc.msg}') from exc
        if not isinstance(meta, dict):
            raise HTTPException(status_code=400, detail='metadata 必须是 JSON 对象')

    pipeline = _pipeline()
    try:
        job_id = pipeline.submit_upload(uploads, meta)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f'提交入库任务失败：{type(exc).__name__}: {exc}') from exc
    if not job_id:
        raise HTTPException(status_code=500, detail='提交入库任务失败：未返回任务编号')
    return {'id': str(job_id), 'docs': docs}


@router.get('/ingest/{job_id}')
def api_ingest_detail(job_id: str) -> dict:
    return _load_job(_pipeline(), job_id)


@router.post('/ingest/{job_id}/retry')
def api_ingest_retry(job_id: str) -> dict:
    pipeline = _pipeline()
    job = _load_job(pipeline, job_id)
    if str(job.get('status') or '') == 'running':
        raise HTTPException(status_code=409, detail='任务正在执行，无需重试')
    try:
        threading.Thread(target=pipeline.run_job, args=(job_id,),
                         name=f'ingest-retry-{job_id}', daemon=True).start()
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f'重试任务失败：{type(exc).__name__}: {exc}') from exc
    return job


@router.post('/ingest/{job_id}/remove')
def api_ingest_remove(job_id: str) -> dict:
    pipeline = _pipeline()
    job = _load_job(pipeline, job_id)
    if str(job.get('status') or '') == 'running':
        raise HTTPException(status_code=409, detail='任务正在执行，无法删除本批资料')
    removed, missing = _remove_documents(job)
    try:
        catalog = _catalog()
        try:
            catalog.update_job(job_id, message=f'已删除本批 {len(removed)} 份资料')
        finally:
            _close_catalog(catalog)
    except Exception as exc:  # 任务备注更新失败不影响删除结果
        log.warning('更新任务备注失败 %s: %s', job_id, exc)
    return {'id': job_id, 'removed': removed, 'missing': missing}


@router.post('/publish')
def api_publish(force_full: bool = False, body: dict | None = Body(default=None)) -> dict:
    publish_module = _publish_module()
    publish_release = getattr(publish_module, 'publish_release', None)
    if not callable(publish_release):
        raise HTTPException(status_code=503, detail='发布服务尚未就绪：缺少 publish_release')
    if isinstance(body, dict) and 'force_full' in body:
        force_full = bool(body.get('force_full'))
    try:
        record = publish_release(force_full=bool(force_full))
    except Exception as exc:
        message = str(exc).strip()
        prefix = '发布失败：'
        if message.startswith(('没有可发布的', '没有可发布的知识单元')):
            # 知识库为空属于用户可修正的输入状态，返回 400 并给出中文原因
            raise HTTPException(status_code=400, detail=prefix + message) from exc
        raise HTTPException(status_code=500,
                            detail=f'{prefix}{type(exc).__name__}: {message}') from exc
    if not record:
        raise HTTPException(status_code=500, detail='发布失败：未返回有效发布记录')
    return record
