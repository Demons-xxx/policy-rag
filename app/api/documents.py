"""文档目录与原文预览接口（契约 4）。

- GET /api/documents：SQLite 登记 + `data/docs/<doc_id>/meta.json` 合并后输出；
- GET /api/documents/{doc_id}/pdf：返回原 PDF；
- GET /api/documents/{doc_id}/pages/{page}/image：用 pymupdf 渲染 150dpi PNG。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, Response

from app import config
from app.store.catalog import STATUSES

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
        'supersedes': _pick(record, meta, 'supersedes'),
        'superseded_by': _pick(record, meta, 'superseded_by'),
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


def _pdf_path(record: dict) -> Path | None:
    """定位可预览的 PDF：DOCX 的页图是另存的 source.pdf，优先于 source.docx。"""
    record = record or {}
    doc_id = str(record.get('doc_id') or '')
    candidates: list[Path] = []
    raw = record.get('source_path')
    if raw:
        path = Path(raw)
        path = path if path.is_absolute() else config.ROOT / path
        if path.suffix.lower() == '.pdf':
            candidates.append(path)
        else:
            candidates.append(path.with_name('source.pdf'))
    if doc_id:
        candidates.append(Path(config.DOCS_DIR) / doc_id / 'source.pdf')
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


@router.post('/documents/{doc_id}/version-link')
def api_document_version_link(doc_id: str, payload: dict = Body(...)) -> dict:
    """手工覆盖版本链（推断误判时用）：只改登记，不重新发布。"""
    allowed = {'status', 'supersedes', 'superseded_by'}
    updates = {key: payload.get(key) for key in allowed if key in payload}
    if not updates:
        raise HTTPException(status_code=400, detail='没有可更新的字段（status/supersedes/superseded_by）')
    if 'status' in updates and updates['status'] not in STATUSES:
        raise HTTPException(status_code=400, detail=f"状态不合法：{updates['status']}（可选 {'/'.join(STATUSES)}）")
    catalog = _catalog()
    try:
        record = catalog.get_document(doc_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f'文档不存在：{doc_id}')
        merged = dict(record)
        merged.update(updates)
        catalog.upsert_document(merged)
        catalog.record_audit('document_version_link', doc_id=doc_id, detail=updates)
        return _public(merged)
    finally:
        _close_catalog(catalog)


@router.get('/documents/{doc_id}/pdf')
def api_document_pdf(doc_id: str) -> FileResponse:
    record = _document(doc_id)
    path = _pdf_path(record)
    if path is None:
        raise HTTPException(status_code=404, detail='该文档没有可预览的 PDF 原件')
    return FileResponse(
        path,
        media_type='application/pdf',
        filename=str(record.get('file_name') or path.name),
        headers={'Cache-Control': 'public, max-age=3600'},
    )


@router.get('/documents/{doc_id}/units')
def api_document_units(doc_id: str) -> dict:
    """文档的单元清单（工作台原文/对比共用），只保留前端需要的字段。"""
    record = _document(doc_id)
    public = _public(record)
    path = Path(config.DOCS_DIR) / doc_id / 'units.jsonl'
    units: list[dict] = []
    if path.is_file():
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or not item.get('text'):
                    continue
                units.append({
                    'id': item.get('id'),
                    'kind': item.get('kind') or 'paragraph',
                    'chapter': item.get('chapter') or '',
                    'section': item.get('section') or '',
                    'article': item.get('article') or item.get('article_no') or '',
                    'pages': item.get('pages') or [],
                    'text': item.get('text') or '',
                })
        except OSError as exc:
            log.warning('读取单元失败 %s: %s', path, exc)
            raise HTTPException(status_code=503, detail='文档单元暂时无法读取') from exc
    return {
        'doc_id': doc_id,
        'title': public.get('title') or '',
        'doc_no': public.get('doc_no') or '',
        'version_label': public.get('version_label') or '',
        'status': public.get('status') or '',
        'units': units,
    }


@router.get('/documents/{doc_id}/pages/{page}/image')
def api_document_page_image(doc_id: str, page: str) -> Response:
    try:
        page_no = int(str(page).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail='页码必须是整数') from None
    if page_no < 1:
        raise HTTPException(status_code=404, detail='页码从 1 开始')

    record = _document(doc_id)
    path = _pdf_path(record)
    if path is None:
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


@router.delete('/documents/{doc_id}')
def api_delete_document(doc_id: str, publish: bool = True) -> dict:
    """停用文档：状态置为 repealed，之后的新发布不再包含它。

    默认直接发布新版本（旧发布仍在 `data/releases` 里，可回滚）；
    ``?publish=false`` 只改状态，由调用方稍后统一发布。
    """
    try:
        catalog = _catalog()
        try:
            record = catalog.get_document(doc_id)
            if not record:
                raise HTTPException(status_code=404, detail='文档不存在')
            if record.get('status') == 'repealed':
                return {'ok': True, 'doc_id': doc_id, 'already': True}
            catalog.set_status(doc_id, 'repealed')
            catalog.record_audit('document_remove', detail={
                'doc_id': doc_id, 'file': record.get('file_name'),
            })
            title = record.get('title') or record.get('file_name') or doc_id
        finally:
            _close_catalog(catalog)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning('停用文档失败 %s: %s', doc_id, exc)
        raise HTTPException(status_code=503, detail=f'停用文档失败：{exc}') from exc

    if not publish:
        return {'ok': True, 'doc_id': doc_id, 'title': title, 'status': 'repealed',
                'note': '状态已置为 repealed，调用 POST /api/publish 后生效'}
    try:
        from app.ingest.publish import publish_release

        release = publish_release()
    except Exception as exc:
        log.warning('停用后发布失败 %s: %s', doc_id, exc)
        raise HTTPException(
            status_code=503,
            detail=f'文档已停用，但发布新版本失败：{exc}。请稍后调用 POST /api/publish 重试',
        ) from exc
    return {'ok': True, 'doc_id': doc_id, 'title': title, 'status': 'repealed',
            'release_id': release.get('id'), 'doc_count': release.get('doc_count')}


@router.post('/documents/{doc_id}/restore')
def api_restore_document(doc_id: str, publish: bool = False) -> dict:
    """恢复被停用的文档（状态回到 active）；默认只改状态，发布由调用方决定。"""
    try:
        catalog = _catalog()
        try:
            record = catalog.get_document(doc_id)
            if not record:
                raise HTTPException(status_code=404, detail='文档不存在')
            if record.get('status') != 'repealed':
                return {'ok': True, 'doc_id': doc_id, 'status': record.get('status'),
                        'note': '文档未被停用，无需恢复'}
            catalog.set_status(doc_id, 'active')
            catalog.record_audit('document_restore', detail={'doc_id': doc_id})
        finally:
            _close_catalog(catalog)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning('恢复文档失败 %s: %s', doc_id, exc)
        raise HTTPException(status_code=503, detail=f'恢复文档失败：{exc}') from exc

    if not publish:
        return {'ok': True, 'doc_id': doc_id, 'status': 'active',
                'note': '状态已恢复为 active，调用 POST /api/publish 后生效'}
    from app.ingest.publish import publish_release

    release = publish_release()
    return {'ok': True, 'doc_id': doc_id, 'status': 'active',
            'release_id': release.get('id'), 'doc_count': release.get('doc_count')}
