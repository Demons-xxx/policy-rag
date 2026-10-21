#!/usr/bin/env python
"""命令行入库：``python scripts/ingest.py <路径或目录> [--meta meta.json] [--no-publish]``。

- 目录会递归收集 PDF/DOCX/MD/TXT；
- 目录内若存在 metadata.json（结构同 knowledge/samples），自动按文件名套用登记信息，
  并用「实际文件 sha256 前 12 位」重新计算 doc_id，把 supersedes 里的预计算 doc_id
  映射到实际 doc_id；
- 默认入库成功后立即发布，``--no-publish`` 跳过发布。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.core import release as release_module  # noqa: E402
from app.ingest.common import sha256_file  # noqa: E402
from app.ingest.parser import FORMAT_BY_SUFFIX  # noqa: E402
from app.ingest.pipeline import ingest_path  # noqa: E402
from app.ingest.publish import publish_release  # noqa: E402


def collect_files(target: Path) -> list[Path]:
    if target.is_dir():
        files = [
            path for path in target.rglob("*")
            if path.is_file() and path.suffix.lower() in FORMAT_BY_SUFFIX
        ]
    else:
        files = [target]
    return sorted(files, key=lambda item: str(item))


def load_metadata(path: Path) -> dict:
    """读取登记表：支持 documents 数组、文件名映射、单文件全局 meta。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"读取 meta 文件失败：{path}：{exc}") from exc

    if isinstance(payload, list):
        return {
            str(item.get("file")): item
            for item in payload
            if isinstance(item, dict) and item.get("file")
        }
    if isinstance(payload, dict):
        if isinstance(payload.get("documents"), list):
            return {
                str(item.get("file")): item
                for item in payload["documents"]
                if isinstance(item, dict) and item.get("file")
            }
        if any(key in payload for key in (
            "title", "doc_no", "version_label", "effective_from", "department", "status",
        )):
            return {"*": payload}
        return {
            str(key): value for key, value in payload.items() if isinstance(value, dict)
        }
    return {}


def find_metadata(target: Path, explicit: str | None) -> dict:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise RuntimeError(f"meta 文件不存在：{explicit}")
        return load_metadata(path)
    candidates = []
    if target.is_dir():
        candidates.append(target / "metadata.json")
    else:
        candidates.append(target.parent / "metadata.json")
    for candidate in candidates:
        if candidate.is_file():
            print(f"发现登记表：{candidate}")
            return load_metadata(candidate)
    return {}


def build_supersedes_map(files: list[Path], metadata: dict) -> dict[str, str]:
    """外部（预计算）doc_id → 实际 doc_id 的映射。"""
    mapping: dict[str, str] = {}
    for path in files:
        entry = metadata.get(path.name)
        if not entry:
            continue
        external = str(entry.get("doc_id") or "").strip()
        if not external:
            continue
        actual = "doc-" + sha256_file(path)[:12]
        mapping[external] = actual
    return mapping


def describe(record: dict) -> str:
    title = record.get("title") or record.get("doc_id")
    state = "重复跳过" if record.get("deduplicated") else "新增"
    return (
        f"{state}：{title} -> {record.get('doc_id')}，"
        f"单元 {record.get('unit_count', 0)} 个，问题 {len(record.get('issues') or [])} 个"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PolicyRAG 制度文档入库")
    parser.add_argument("path", help="文件或目录路径")
    parser.add_argument("--meta", help="登记信息 JSON 文件（可选）")
    parser.add_argument("--no-publish", action="store_true", help="只入库，不发布")
    parser.add_argument("--publish", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    config.ensure_dirs()
    target = Path(args.path)
    if not target.exists():
        print(f"路径不存在：{args.path}", file=sys.stderr)
        return 1

    try:
        metadata = find_metadata(target, args.meta)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    files = collect_files(target)
    if not files:
        print("未找到可入库文件（支持 PDF/DOCX/MD/TXT）。", file=sys.stderr)
        return 1

    supersedes_map = build_supersedes_map(files, metadata)
    failures = 0
    for path in files:
        entry = metadata.get(path.name) or metadata.get("*") or {}
        meta = dict(entry) if entry else None
        if meta and meta.get("supersedes"):
            external = str(meta["supersedes"])
            meta["supersedes"] = supersedes_map.get(external, external)
        try:
            record = ingest_path(path, meta=meta)
            print(describe(record))
            issues = record.get("issues") or []
            for issue in issues:
                flag = "阻断" if issue.get("blocking") else issue.get("level", "warn")
                print(f"  [{flag}] {issue.get('code')}: {issue.get('message')}")
        except Exception as exc:
            failures += 1
            print(f"入库失败：{path.name}：{exc}", file=sys.stderr)

    if args.no_publish:
        print(f"共 {len(files)} 份文件，失败 {failures} 份；已跳过发布。")
        return 0 if failures == 0 else 1
    if failures == len(files):
        print("全部文件入库失败，跳过发布。", file=sys.stderr)
        return 1

    try:
        active = publish_release()
    except Exception as exc:
        print(f"发布失败：{exc}", file=sys.stderr)
        return 1

    manifest = {}
    try:
        manifest = release_module.load_manifest(active)
    except Exception:
        pass
    print(
        f"发布完成：{active['id']}，文档 {active.get('doc_count', 0)} 份，"
        f"单元 {active.get('unit_count', 0)} 个，片段 {manifest.get('passage_count', 0)} 条，"
        f"集合 {active.get('collection')}"
    )
    print(f"共 {len(files)} 份文件，失败 {failures} 份。")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
