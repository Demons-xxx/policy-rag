"""入库流水线：单文档入库 + 上传任务调度（薄包装层）。

Stage 3 起真正的阶段编排在 ``app.ingest.stages``（parse → structure → quality →
chunk → publish，可配置阶段列表）；本模块只保留原有公开函数，作为去重、
catalog 生命周期、任务调度与进度回调的薄包装：

- ``ingest_path``：sha256 去重 → ``stages.run_document`` → 返回文档记录；
- ``submit_upload`` / ``run_job`` / ``job_status``：上传任务登记与状态查询；
- 签名、返回结构、任务进度回调（phase/progress/message 取值）与旧实现一致。

契约见 docs/架构与接口.md 3.5。所有路径取自 app.config，写文件统一 UTF-8、
原子替换（``.tmp`` + ``os.replace``）。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

from app import config
from app.ingest import stages
from app.ingest.common import read_json, read_jsonl, sha256_file
from app.store.catalog import Catalog

#: 契约 2.1 的登记字段（定义已移到 stages，这里保留旧名以兼容调用方）
META_FIELDS = stages.META_FIELDS

#: 旧实现里的私有别名：外部若引用过仍可用
_clean_meta = stages.clean_meta
_guess_title = stages.guess_title

_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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


def _job_callback(job_id: str | None):
    """把阶段事件翻译成任务表更新；消息与进度值沿用旧实现（10/40/65/90）。"""

    def notify(name: str, when: str, state: dict) -> None:
        path = Path(state.get("path") or "")
        if when == "before":
            if name == "parse":
                _update_job(job_id, phase="parse", progress=10,
                            message=f"正在解析：{path.name}")
            elif name == "structure":
                _update_job(job_id, phase="structure", progress=40,
                            message=f"正在结构化：{path.name}")
            elif name == "quality":
                _update_job(job_id, phase="quality", progress=65,
                            message=f"正在检查质量：{path.name}")
        elif when == "after" and name == "publish":
            result = state.get("result") or {}
            _update_job(
                job_id, progress=90,
                message=f"入库完成：{result.get('title')}（{result.get('unit_count', 0)} 个知识单元）",
            )

    return notify


def ingest_path(path: Path, meta: dict | None = None, job_id: str | None = None,
                parser: str | None = None) -> dict:
    """完整入库单份文档：去重 → 复制原文 → 解析 → 结构化 → 质量检查 → 登记。

    薄包装：sha256 去重与 catalog 生命周期在这里，阶段串联交给
    ``stages.run_document``；签名、返回结构与任务进度回调与旧实现一致。
    """
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
        return stages.run_document(
            path, meta, sha256=sha256, catalog=catalog, job_id=job_id, parser=parser,
            on_stage=_job_callback(job_id),
        )
    finally:
        catalog.close()


def _new_job_id() -> str:
    return f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def meta_for_file(meta: dict | list | None, index: int) -> dict:
    """按文件序号取元数据：列表按顺序一一对应，字典全局共用。"""
    if isinstance(meta, list):
        item = meta[index] if index < len(meta) else {}
        return item if isinstance(item, dict) else {}
    return meta if isinstance(meta, dict) else {}


def submit_upload(files: list[tuple[str, bytes]], meta: dict | list | None = None) -> str:
    """保存上传文件到 data/uploads/<job_id>/，创建任务并后台逐个入库。

    ``meta`` 可以是全局对象（所有文件共用），也可以是与 ``files`` 顺序一致的
    对象列表（每份文件各自的标题、文号、生效日期等）。
    """
    config.ensure_dirs()
    if not files:
        raise ValueError("没有收到上传文件。")
    if isinstance(meta, list):
        cleaned_meta = [_clean_meta(item) for item in meta]
        if len(cleaned_meta) < len(files):
            cleaned_meta = cleaned_meta + [{}] * (len(files) - len(cleaned_meta))
    else:
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
                file_meta = meta_for_file(meta, index)
                parser_choice = file_meta.get("parser") if isinstance(file_meta, dict) else None
                result = ingest_path(Path(file_path), meta=file_meta, job_id=job_id,
                                     parser=parser_choice)
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
