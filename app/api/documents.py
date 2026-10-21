"""文档目录与原文预览接口（契约 4）。

- GET /api/documents：SQLite 登记 + `data/docs/<doc_id>/meta.json` 合并后输出；
- GET /api/documents/{doc_id}/pdf：返回原 PDF；
- GET /api/documents/{doc_id}/pages/{page}/image：用 pymupdf 渲染 150dpi PNG。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app import config

router = APIRouter()
log = logging.getLogger(__name__)

RENDER_DPI = 150


def _catalog():
    """Catalog 由 ingest-dev 提供，延迟导入避免并行开发期启动失败。"""
    from app.store.catalog import Catalog
    return Catalog(config.CATALOG_DB)


def _meta(doc_id: str) -> dict:
    path = Path(config.DOCS_DIR) / doc_id / 'meta.json'
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning('读取 meta.json 失败 %s: %s', path, exc)
        return {}


def _pick(record: dict, meta: dict, key: str, default=None):
    value = record.get(key)
    if value is None or value == '':
        value = meta.get(key)
    return default if value is None or value == '' else value


def _public(record: dict) -> dict:
    """把登记记录 + meta.json 归一成前端字段（契约第 4 节）。"""
    record = record or {}
    doc_id = str(record.get('doc_id') or '')
    meta = _meta(doc_id) if doc_id else {}
    fmt = str(_pick(record, meta, 'format', '') or '').lower()

    has_pdf = record.get('has_pdf')
    if has_pdf is None:
        has_pdf = meta.get('has_pdf')
    has_pdf = bool(has_pdf) if has_pdf is not None else fmt == 'pdf'

    pages = _pick(record, meta, 'pages', 0)
    try:
        pages = int(pages or 0)
    except (TypeError, ValueError):
        pages = 0

    return {
        'id': doc_id,
        'name': str(_pick(record, meta, 'file_name', '') or ''),
        'title': str(_pick(record, meta, 'title', '') or ''),
        'doc_no': _pick(record, meta, 'doc_no'),
        'version_label': _pick(record, meta, 'version_label'),
        'department': _pick(record, meta, 'department'),
        'status': str(_pick(record, meta, 'status', 'active') or 'active'),
        'pages': pages,
        'has_pdf': has_pdf,
        'effective_from': _pick(record, meta, 'effective_from'),
        'effective_to': _pick(record, meta, 'effective_to'),
    }


def _source_path(record: dict) -> Path | None:
    """定位原文文件：优先登记的 source_path，其次 data/docs/<doc_id>/source.<ext>。"""
    record = record or {}
    candidates: list[Path] = []
    raw = record.get('source_path')
    if raw:
        path = Path(raw)
        candidates.append(path if path.is_absolute() else config.ROOT / path)
    doc_id = str(record.get('doc_id') or '')
    if doc_id:
        fmt = str(record.get('format') or '').lower()
        extensions = ([fmt] if fmt else []) + ['pdf']
        for extension in extensions:
            candidates.append(Path(config.DOCS_DIR) / doc_id / f'source.{extension}')
    for path in candidates:
        if path.is_file():
            return path
    return None


def _close_catalog(catalog) -> None:
    closer = getattr(catalog, 'close', None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _document(doc_id: str) -> dict:
    try:
        catalog = _catalog()
    except Exception as exc:
        log.warning('打开文档目录失败 %s: %s', doc_id, exc)
        raise HTTPException(status_code=503, detail='文档目录服务尚未就绪，请稍后再试') from exc
    try:
        record = catalog.get_document(doc_id)
    except Exception as exc:
        log.warning('读取文档登记失败 %s: %s', doc_id, exc)
        raise HTTPException(status_code=503, detail='文档目录服务尚未就绪，请稍后再试') from exc
    finally:
        _close_catalog(catalog)
    if not record:
        raise HTTPException(status_code=404, detail='文档不存在')
    return record


@router.get('/documents')
def api_documents() -> dict:
    try:
        catalog = _catalog()
        try:
            records = catalog.list_documents() or []
        finally:
            _close_catalog(catalog)
    except Exception as exc:  # 目录库还没建好时首屏仍要能打开
        log.warning('读取文档列表失败：%s', exc)
        return {'documents': []}
    documents = [_public(record) for record in records if isinstance(record, dict)]
    documents.sort(key=lambda item: (item['status'] != 'active', item['title'] or item['name']))
    return {'documents': documents}


@router.get('/documents/{doc_id}/pdf')
def api_document_pdf(doc_id: str) -> FileResponse:
    record = _document(doc_id)
    path = _source_path(record)
    if path is None or path.suffix.lower() != '.pdf':
        raise HTTPException(status_code=404, detail='该文档没有可预览的 PDF 原件')
    return FileResponse(
        path,
        media_type='application/pdf',
        filename=str(record.get('file_name') or path.name),
        headers={'Cache-Control': 'public, max-age=3600'},
    )


@router.get('/documents/{doc_id}/pages/{page}/image')
def api_document_page_image(doc_id: str, page: str) -> Response:
    try:
        page_no = int(str(page).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail='页码必须是整数') from None
    if page_no < 1:
        raise HTTPException(status_code=404, detail='页码从 1 开始')

    record = _document(doc_id)
    path = _source_path(record)
    if path is None or path.suffix.lower() != '.pdf':
        raise HTTPException(status_code=404, detail='该文档没有可渲染的 PDF 原件')

    try:
        import fitz  # pymupdf
    except Exception as exc:  # pragma: no cover - 依赖已装，仅防环境异常
        raise HTTPException(status_code=500, detail=f'PDF 渲染组件不可用：{exc}') from exc

    try:
        with fitz.open(path) as pdf:
            if page_no > pdf.page_count:
                raise HTTPException(
                    status_code=404,
                    detail=f'页码超出范围（1-{pdf.page_count}）',
                )
            target = pdf.load_page(page_no - 1)
            matrix = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
            pixmap = target.get_pixmap(matrix=matrix, alpha=False)
            content = pixmap.tobytes('png')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'页面渲染失败：{exc}') from exc

    return Response(content=content, media_type='image/png',
                    headers={'Cache-Control': 'public, max-age=3600'})
