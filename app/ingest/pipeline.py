"""入库流水线：单文档入库 + 上传任务调度。

契约见 docs/架构与接口.md 3.5。所有路径取自 app.config，写文件统一 UTF-8、
原子替换（``.tmp`` + ``os.replace``）。
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from app import config
from app.ingest import parser as parser_module
from app.ingest.common import (
    now_iso,
    read_json,
    read_jsonl,
    relative_path,
    sha256_file,
    write_json,
    write_jsonl,
)
from app.ingest.quality import check_units
from app.ingest.structure import build_units
from app.store.catalog import STATUSES, Catalog

META_FIELDS = (
    "title", "doc_no", "version_label", "effective_from", "effective_to",
    "department", "status", "supersedes",
)

_TITLE_SKIP_PREFIX = ("第", "附件", "目录", "目 录", "附录", "#")
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _clean_meta(meta: dict | None) -> dict:
    """只保留契约字段，空字符串统一成 None。"""
    meta = meta or {}
    cleaned: dict = {}
    for key in META_FIELDS:
        value = meta.get(key)
        if value is None:
            cleaned[key] = None
        else:
            text = str(value).strip()
            cleaned[key] = text or None
    status = cleaned.get("status") or "active"
    if status not in STATUSES:
        raise ValueError(f"非法文档状态：{status}（允许 {', '.join(STATUSES)}）")
    cleaned["status"] = status
    return cleaned


def _guess_title(parsed: dict, path: Path) -> str:
    """从首页首个像标题的行猜制度名称，失败时用文件名。"""
    for page in (parsed.get("pages") or [])[:1]:
        for line in (page.get("text") or "").splitlines():
            text = line.strip()
            if len(text) < 3 or len(text) > 120:
                continue
            if text.startswith(_TITLE_SKIP_PREFIX):
                continue
            if text.startswith(("（", "(")):
                continue
            if text.endswith("。") and len(text) > 30:
                continue
            return text
    return path.stem


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE_NAME_RE.sub("_", Path(name or "upload").name).strip(" .")
    return cleaned or "upload"


def _update_job(job_id: str | None, **fields) -> None:
    if not job_id:
        return
    try:
        catalog = Catalog()
        try:
            catalog.update_job(job_id, **fields)
        finally:
            catalog.close()
    except Exception:
        # 任务表异常不应打断入库本身
        pass


def _read_issues(doc_id: str) -> list[dict]:
    issues = read_json(config.DOCS_DIR / doc_id / "issues.json", default={})
    if isinstance(issues, dict):
        return list(issues.get("issues") or [])
    if isinstance(issues, list):
        return issues
    return []


def _existing_result(catalog: Catalog, record: dict, sha256: str, job_id: str | None) -> dict:
    doc_id = record["doc_id"]
    units = read_jsonl(config.DOCS_DIR / doc_id / "units.jsonl")
    result = dict(record)
    result["issues"] = _read_issues(doc_id)
    result["unit_count"] = len(units)
    result["deduplicated"] = True
    try:
        catalog.record_audit("ingest.skip", doc_id=doc_id, job_id=job_id,
                             detail={"sha256": sha256, "reason": "sha256 已存在"})
    except Exception:
        pass
    _update_job(job_id, message=f"已存在相同内容文档「{record.get('title') or doc_id}」，跳过重复入库")
    return result


def ingest_path(path: Path, meta: dict | None = None, job_id: str | None = None) -> dict:
    """完整入库单份文档：复制原文 → 解析 → 结构化 → 质量检查 → 登记。"""
    config.ensure_dirs()
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"文件不存在：{path}")

    sha256 = sha256_file(path)
    catalog = Catalog()
    try:
        existing = catalog.find_by_sha(sha256)
        if existing is not None:
            return _existing_result(catalog, existing, sha256, job_id)

        cleaned = _clean_meta(meta)
        fmt = parser_module.detect_format(path)
        doc_id = f"doc-{sha256[:12]}"
        doc_dir = config.DOCS_DIR / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        # 1) 复制原文
        suffix = parser_module.source_suffix(fmt)
        source_path = doc_dir / f"source{suffix}"
        try:
            shutil.copy2(path, source_path)
        except OSError as exc:
            raise RuntimeError(f"复制原文失败：{exc}") from exc

        # 2) 解析
        _update_job(job_id, phase="parse", progress=10, message=f"正在解析：{path.name}")
        parsed = parser_module.parse_document(path)

        # 3) 结构化
        _update_job(job_id, phase="structure", progress=40, message=f"正在结构化：{path.name}")
        title = cleaned.get("title") or _guess_title(parsed, path)
        unit_meta = {
            **cleaned,
            "doc_id": doc_id,
            "doc_title": title,
            "file_name": path.name,
        }
        units = build_units(parsed, unit_meta)

        # 4) 质量检查
        _update_job(job_id, phase="quality", progress=65, message=f"正在检查质量：{path.name}")
        issues = check_units(units, parsed)

        # 5) 落盘登记
        imported_at = now_iso()
        record = {
            "doc_id": doc_id,
            "title": title,
            "doc_no": cleaned.get("doc_no"),
            "version_label": cleaned.get("version_label"),
            "effective_from": cleaned.get("effective_from"),
            "effective_to": cleaned.get("effective_to"),
            "department": cleaned.get("department"),
            "status": cleaned.get("status") or "active",
            "supersedes": cleaned.get("supersedes"),
            "file_name": path.name,
            "format": fmt,
            "pages": int(parsed.get("page_count") or len(parsed.get("pages") or []) or 1),
            "has_pdf": 1 if parsed.get("has_pdf") else 0,
            "sha256": sha256,
            "source_path": relative_path(source_path),
            "imported_at": imported_at,
        }
        meta_payload = dict(record)
        meta_payload["unit_count"] = len(units)
        meta_payload["parser"] = parsed.get("parser")
        write_json(doc_dir / "meta.json", meta_payload)
        write_jsonl(doc_dir / "units.jsonl", units)
        write_json(doc_dir / "issues.json", {
            "doc_id": doc_id,
            "generated_at": imported_at,
            "blocking": any(issue.get("blocking") for issue in issues),
            "issues": issues,
        })
        catalog.upsert_document(record)
        catalog.record_audit("ingest", doc_id=doc_id, job_id=job_id, detail={
            "file": path.name, "format": fmt, "unit_count": len(units),
            "issue_count": len(issues), "sha256": sha256,
        })
        _update_job(job_id, progress=90,
                    message=f"入库完成：{title}（{len(units)} 个知识单元）")

        result = dict(record)
        result["issues"] = issues
        result["unit_count"] = len(units)
        result["deduplicated"] = False
        return result
    finally:
        catalog.close()


def _new_job_id() -> str:
    return f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def submit_upload(files: list[tuple[str, bytes]], meta: dict | None = None) -> str:
    """保存上传文件到 data/uploads/<job_id>/，创建任务并后台逐个入库。"""
    config.ensure_dirs()
    if not files:
        raise ValueError("没有收到上传文件。")
    cleaned_meta = _clean_meta(meta)
    job_id = _new_job_id()
    upload_dir = config.UPLOADS_DIR / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    used_names: set[str] = set()
    for index, (name, data) in enumerate(files):
        safe_name = _safe_name(name)
        if safe_name in used_names:
            suffix = Path(safe_name).suffix
            safe_name = f"{Path(safe_name).stem}-{index + 1}{suffix}"
        used_names.add(safe_name)
        target = upload_dir / safe_name
        target.write_bytes(data)
        entries.append({"name": name, "saved_name": safe_name,
                        "path": str(target), "size": len(data)})

    catalog = Catalog()
    try:
        catalog.create_job(job_id, "upload", {"files": entries, "meta": cleaned_meta})
    finally:
        catalog.close()

    thread = threading.Thread(target=_background_run, args=(job_id,), daemon=True,
                              name=f"ingest-{job_id}")
    thread.start()
    return job_id


def _background_run(job_id: str) -> None:
    """后台线程入口：异常已写回任务表，这里只负责兜底不打印堆栈。"""
    try:
        run_job(job_id)
    except Exception:
        pass


def run_job(job_id: str) -> None:
    """后台任务：逐个入库 payload 里的文件，进度写回 catalog。"""
    catalog = Catalog()
    try:
        job = catalog.get_job(job_id)
        if job is None:
            raise KeyError(f"任务不存在：{job_id}")
        payload = job.get("payload") or {}
        files = payload.get("files") or []
        meta = payload.get("meta") or {}
        catalog.update_job(
            job_id, status="running", phase="parse", progress=0, error=None,
            message=f"开始入库，共 {len(files)} 份文件",
        )

        docs: list[dict] = []
        errors: list[str] = []
        for index, item in enumerate(files):
            name = item.get("name") or item.get("saved_name") or "文件"
            file_path = item.get("path")
            try:
                if not file_path:
                    raise RuntimeError("上传文件路径缺失")
                result = ingest_path(Path(file_path), meta=meta, job_id=job_id)
                docs.append({
                    "doc_id": result["doc_id"],
                    "title": result.get("title"),
                    "unit_count": result.get("unit_count", 0),
                    "deduplicated": bool(result.get("deduplicated")),
                    "issues": result.get("issues") or [],
                })
            except Exception as exc:
                errors.append(f"{name}：{exc}")
            progress = int(round((index + 1) / max(len(files), 1) * 100))
            catalog.update_job(
                job_id, progress=progress,
                message=f"已处理 {index + 1}/{len(files)} 份文件",
            )

        final_payload = dict(payload)
        final_payload["docs"] = docs
        final_payload["errors"] = errors
        if errors:
            catalog.update_job(
                job_id, status="failed", progress=100, payload=final_payload,
                message=f"入库结束：成功 {len(docs)} 份，失败 {len(errors)} 份",
                error="；".join(errors),
            )
        else:
            catalog.update_job(
                job_id, status="done", phase="quality", progress=100,
                payload=final_payload, message=f"入库完成，共 {len(docs)} 份文件",
                error=None,
            )
        catalog.record_audit("job.finish", job_id=job_id, detail={
            "status": "failed" if errors else "done",
            "doc_count": len(docs), "error_count": len(errors),
        })
    except Exception as exc:
        try:
            catalog.update_job(job_id, status="failed", error=f"任务执行失败：{exc}",
                               message="任务执行失败")
        except Exception:
            pass
    finally:
        catalog.close()


def job_status(job_id: str) -> dict:
    """读取任务状态；任务不存在时抛出中文 KeyError。"""
    catalog = Catalog()
    try:
        job = catalog.get_job(job_id)
        if job is None:
            raise KeyError(f"任务不存在：{job_id}")
        return job
    finally:
        catalog.close()
