#!/usr/bin/env python
"""构建/切换检索索引：``python scripts/build_index.py [--device cpu|cuda] [--force]``。

等价于调用 app.ingest.publish.publish_release：汇总全部文档 → 片段 → 向量 →
release 目录 →（chroma 后端时）Chroma 集合 → 原子切换 active_release。
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
from app.store import releases as release_module  # noqa: E402
from app.ingest.publish import publish_release  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PolicyRAG 构建检索索引")
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None,
                        help="编码设备，默认由 config.resolve_device() 决定")
    parser.add_argument("--force", action="store_true",
                        help="忽略单文档向量缓存，全部重新编码")
    args = parser.parse_args(argv)

    config.ensure_dirs()
    try:
        active = publish_release(device=args.device, force_full=args.force)
    except Exception as exc:
        print(f"构建索引失败：{exc}", file=sys.stderr)
        return 1

    manifest = {}
    try:
        manifest = release_module.load_manifest(active)
    except Exception as exc:
        print(f"警告：读取 manifest 失败：{exc}", file=sys.stderr)

    print(json.dumps({
        "release_id": active.get("id"),
        "dir": active.get("dir"),
        "collection": active.get("collection"),
        "vector_backend": config.VECTOR_BACKEND,
        "doc_count": active.get("doc_count"),
        "unit_count": active.get("unit_count"),
        "passage_count": manifest.get("passage_count"),
        "embedding": manifest.get("embedding"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
