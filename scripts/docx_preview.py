"""DOCX 页图补全脚本：把已入库的 DOCX 文档转成 source.pdf（Word COM）。

入库流程会自动做这件事（config.DOCX_PREVIEW），本脚本用于：
- 功能上线前已入库、还没有 source.pdf 的历史文档；
- 转换失败后重试（--force 强制重转）。

用法（项目根目录）：
    python scripts/docx_preview.py            # 只补缺失的
    python scripts/docx_preview.py --force    # 全部重转
    python scripts/docx_preview.py --doc doc-xxx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.ingest import office  # noqa: E402
from app.ingest.common import write_json  # noqa: E402
from app.store.catalog import Catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='补全 DOCX 文档的 PDF 页图')
    parser.add_argument('--doc', default=None, help='只处理指定 doc_id')
    parser.add_argument('--force', action='store_true', help='已有 source.pdf 也重新转换')
    parser.add_argument('--timeout', type=float, default=float(config.DOCX_PREVIEW_TIMEOUT), help='单篇转换超时秒数')
    args = parser.parse_args(argv)

    catalog = Catalog(config.CATALOG_DB)
    try:
        records = catalog.list_documents()
        docs = [r for r in records if str(r.get('format') or '').lower() == 'docx']
        if args.doc:
            docs = [r for r in docs if r['doc_id'] == args.doc]
        if not docs:
            print('没有需要处理的 DOCX 文档')
            return 0
        done = skipped = failed = 0
        for record in docs:
            doc_id = record['doc_id']
            doc_dir = Path(config.DOCS_DIR) / doc_id
            source = doc_dir / 'source.docx'
            if not source.is_file():
                print(f'跳过 {doc_id}：缺少 source.docx')
                skipped += 1
                continue
            preview = doc_dir / 'source.pdf'
            if preview.is_file() and not args.force:
                pages = office.pdf_pages(preview) or int(record.get('pages') or 1)
                _save(catalog, record, pages)
                skipped += 1
                continue
            ok, reason = office.convert_docx_to_pdf(source, preview, timeout=args.timeout)
            if not ok:
                print(f'失败 {doc_id}（{record.get("title")}）：{reason}')
                failed += 1
                continue
            pages = office.pdf_pages(preview) or int(record.get('pages') or 1)
            _save(catalog, record, pages)
            print(f'完成 {doc_id}（{record.get("title")}）：{pages} 页')
            done += 1
        print(f'汇总：转换 {done}，跳过 {skipped}，失败 {failed}')
        return 1 if failed else 0
    finally:
        catalog.close()


def _save(catalog: Catalog, record: dict, pages: int) -> None:
    """同步 catalog 与 meta.json，便于 /api/documents 返回 has_pdf 与页数。"""
    record = dict(record)
    record['has_pdf'] = 1
    record['pages'] = int(pages or 1)
    catalog.upsert_document(record)
    doc_dir = Path(config.DOCS_DIR) / record['doc_id']
    meta_path = doc_dir / 'meta.json'
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            meta = {}
    meta['has_pdf'] = 1
    meta['pages'] = int(pages or 1)
    write_json(meta_path, meta)


if __name__ == '__main__':
    raise SystemExit(main())
