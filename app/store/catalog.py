"""SQLite 目录库：文档登记、入库任务、发布记录与审计。

契约见 docs/架构与接口.md 3.1。所有连接都使用 ``check_same_thread=False``
并开启 WAL，允许 FastAPI 工作线程、后台入库线程与命令行进程同时读写。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app import config

STATUSES = ("active", "superseded", "repealed")

#: documents 表字段，顺序与契约 2.1 一致
DOCUMENT_COLUMNS = (
    "doc_id", "title", "doc_no", "version_label", "effective_from", "effective_to",
    "department", "status", "supersedes", "file_name", "format", "pages", "has_pdf",
    "sha256", "source_path", "imported_at",
)

#: update_job 允许更新的字段（payload 用于记录任务明细）
JOB_FIELDS = ("status", "phase", "progress", "message", "error", "payload")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT PRIMARY KEY,
    title          TEXT,
    doc_no         TEXT,
    version_label  TEXT,
    effective_from TEXT,
    effective_to   TEXT,
    department     TEXT,
    status         TEXT DEFAULT 'active',
    supersedes     TEXT,
    file_name      TEXT,
    format         TEXT,
    pages          INTEGER DEFAULT 0,
    has_pdf        INTEGER DEFAULT 0,
    sha256         TEXT,
    source_path    TEXT,
    imported_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    kind       TEXT,
    payload    TEXT,
    status     TEXT DEFAULT 'queued',
    phase      TEXT,
    progress   REAL DEFAULT 0,
    message    TEXT,
    error      TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS releases (
    release_id   TEXT PRIMARY KEY,
    payload      TEXT,
    published_at TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT,
    action  TEXT,
    doc_id  TEXT,
    job_id  TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);
"""


def _now() -> str:
    """本地时区 ISO 时间，精确到秒。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Catalog:
    """目录库访问对象。每次实例化打开一条连接，可跨线程使用。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else config.CATALOG_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 生命周期 ----
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- 内部工具 ----
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _document_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        doc = {key: row[key] for key in row.keys()}
        doc["pages"] = int(doc.get("pages") or 0)
        doc["has_pdf"] = int(doc.get("has_pdf") or 0)
        doc["status"] = doc.get("status") or "active"
        return doc

    # ---- 文档 ----
    def upsert_document(self, doc: dict) -> None:
        """插入或整体更新一份文档记录（以 doc_id 为主键）。"""
        if not doc.get("doc_id"):
            raise ValueError("文档记录缺少 doc_id")
        data = {column: doc.get(column) for column in DOCUMENT_COLUMNS}
        data["status"] = data["status"] or "active"
        data["pages"] = int(data["pages"] or 0)
        data["has_pdf"] = 1 if data["has_pdf"] else 0
        if not data["imported_at"]:
            data["imported_at"] = _now()
        placeholders = ", ".join("?" for _ in DOCUMENT_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in DOCUMENT_COLUMNS if column != "doc_id"
        )
        sql = (
            f"INSERT INTO documents ({', '.join(DOCUMENT_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(doc_id) DO UPDATE SET {updates}"
        )
        self._execute(sql, tuple(data[column] for column in DOCUMENT_COLUMNS))

    def get_document(self, doc_id: str) -> dict | None:
        rows = self._query("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
        return self._document_row(rows[0]) if rows else None

    def find_by_sha(self, sha256: str) -> dict | None:
        if not sha256:
            return None
        rows = self._query(
            "SELECT * FROM documents WHERE sha256 = ? ORDER BY imported_at LIMIT 1", (sha256,)
        )
        return self._document_row(rows[0]) if rows else None

    def list_documents(self) -> list[dict]:
        rows = self._query("SELECT * FROM documents ORDER BY imported_at DESC, doc_id DESC")
        return [self._document_row(row) for row in rows]

    def delete_document(self, doc_id: str) -> bool:
        """删除文档登记（供入库接口的“删除本批资料”使用）。"""
        cursor = self._execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self.record_audit("document.delete", doc_id=doc_id)
        return deleted

    def set_status(self, doc_id: str, status: str, effective_to: str | None = None) -> None:
        """更新文档状态；effective_to 为空时保留原值。"""
        if status not in STATUSES:
            raise ValueError(f"非法文档状态：{status}（允许 {', '.join(STATUSES)}）")
        if effective_to is not None:
            self._execute(
                "UPDATE documents SET status = ?, effective_to = ? WHERE doc_id = ?",
                (status, effective_to, doc_id),
            )
        else:
            self._execute(
                "UPDATE documents SET status = ? WHERE doc_id = ?", (status, doc_id)
            )
        self.record_audit("document.status", doc_id=doc_id,
                          detail={"status": status, "effective_to": effective_to})

    # ---- 任务 ----
    def create_job(self, job_id: str, kind: str, payload: dict) -> None:
        if not job_id:
            raise ValueError("任务缺少 job_id")
        now = _now()
        self._execute(
            "INSERT INTO jobs (job_id, kind, payload, status, phase, progress, message, error, "
            "created_at, updated_at) VALUES (?, ?, ?, 'queued', NULL, 0, NULL, NULL, ?, ?)",
            (job_id, kind, _json(payload or {}), now, now),
        )

    def update_job(self, job_id: str, **fields) -> None:
        """按白名单更新任务字段：status/phase/progress/message/error/payload。"""
        unknown = [key for key in fields if key not in JOB_FIELDS]
        if unknown:
            raise ValueError(f"不支持的任务字段：{', '.join(unknown)}")
        if not fields:
            return
        values: list = []
        assignments: list[str] = []
        for key, value in fields.items():
            if key == "payload":
                value = _json(value if value is not None else {})
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(job_id)
        self._execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?", tuple(values))

    def get_job(self, job_id: str) -> dict | None:
        rows = self._query("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        return self._job_row(rows[0]) if rows else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        rows = self._query(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),)
        )
        return [self._job_row(row) for row in rows]

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict:
        job = {key: row[key] for key in row.keys()}
        try:
            job["payload"] = json.loads(job.get("payload") or "{}")
        except (TypeError, ValueError):
            job["payload"] = {}
        job["progress"] = int(job.get("progress") or 0)
        return job

    # ---- 发布 ----
    def record_release(self, release: dict) -> None:
        release_id = release.get("id") or release.get("release_id")
        if not release_id:
            raise ValueError("发布记录缺少 id")
        payload = dict(release)
        payload.setdefault("id", release_id)
        self._execute(
            "INSERT INTO releases (release_id, payload, published_at, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(release_id) DO UPDATE SET payload = excluded.payload, "
            "published_at = excluded.published_at",
            (release_id, _json(payload), release.get("published_at"), _now()),
        )

    def list_releases(self, limit: int = 10) -> list[dict]:
        rows = self._query(
            "SELECT * FROM releases ORDER BY published_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        )
        releases: list[dict] = []
        for row in rows:
            try:
                record = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                record = {}
            record.setdefault("id", row["release_id"])
            record["release_id"] = record["id"]
            releases.append(record)
        return releases

    # ---- 审计 ----
    def record_audit(self, action: str, doc_id: str | None = None,
                     job_id: str | None = None, detail: dict | None = None) -> None:
        self._execute(
            "INSERT INTO audit (at, action, doc_id, job_id, detail) VALUES (?, ?, ?, ?, ?)",
            (_now(), action, doc_id, job_id, _json(detail or {})),
        )

    def list_audit(self, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),)
        )
        result = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except (TypeError, ValueError):
                item["detail"] = {}
            result.append(item)
        return result
