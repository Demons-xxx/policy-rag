#!/usr/bin/env python
"""检索链路命令行调试工具。

用法::

    python scripts/search.py "实验室危化品怎么领用？"
    python scripts/search.py "第十二条" --release data/releases/rel-20261021-153000 --backend npz
    python scripts/search.py "差旅标准" --top-k 3 --json

默认读取 data/state/active_release.json 指向的现行发布；``--release`` 既可以给
发布目录，也可以给 data/releases 下的 release id。
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
from app.store import releases as release_core  # noqa: E402
from app.retrieval.engine import PolicyRetriever  # noqa: E402


def _resolve_release(value: str | None) -> tuple[Path, str]:
    """把 --release 参数解析成（发布目录，release id）。"""
    if not value:
        active = release_core.read_active()
        if not active:
            raise SystemExit('没有可用的发布：先执行入库与发布，或用 --release 指定目录')
        return release_core.release_dir(active), str(active.get('id') or '')
    path = Path(value)
    if path.is_dir():
        return path.resolve(), path.name
    candidate = config.RELEASES_DIR / value
    if candidate.is_dir():
        return candidate.resolve(), value
    raise SystemExit(f'发布目录不存在：{value}')


def _print_result(result: dict) -> None:
    print(f'问题：{result["question"]}')
    print(f'改写：{result["rewritten"]}')
    filters = result.get('filters') or {}
    print(f'过滤：department={filters.get("department")} as_of={filters.get("as_of")}')
    print('-' * 78)
    for item in result.get('results') or []:
        citation = item.get('citation') or {}
        scores = item.get('scores') or {}
        page = citation.get('page')
        page_text = f'（第{page}页）' if page else ''
        print(
            f'[{item["rank"]}] 《{citation.get("title")}》'
            f' {citation.get("chapter") or ""} {citation.get("article") or ""}'
            f'{page_text}'
        )
        print(
            '    通道：' + '/'.join(item.get('channels') or [])
            + f' | dense={scores.get("dense")} bm25={scores.get("bm25")} rrf={scores.get("rrf")}'
        )
        print('    证据：' + (item.get('evidence') or '').replace('\n', '\n          '))
        print()
    notes = result.get('notes') or []
    if notes:
        print('提示：')
        for note in notes:
            print(f'  - {note}')
        print()
    print(f'上下文（tokens={result.get("tokens")}，{result.get("seconds")}s）：')
    print(result.get('context') or '（无）')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='PolicyRAG 检索调试')
    parser.add_argument('question', help='要检索的问题')
    parser.add_argument('--release', default=None,
                        help='发布目录或 data/releases 下的 release id，默认用现行发布')
    parser.add_argument('--backend', choices=['npz', 'chroma'],
                        default=config.VECTOR_BACKEND, help='向量后端')
    parser.add_argument('--top-k', type=int, default=config.TOP_K, help='返回条数')
    parser.add_argument('--device', default=None, help='cpu / cuda，默认自动')
    parser.add_argument('--as-of', default=None, help='时效判断基准日 YYYY-MM-DD')
    parser.add_argument('--department', default=None, help='按归口部门过滤')
    parser.add_argument('--json', action='store_true', help='输出完整 JSON')
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK，显式切 UTF-8 避免中文乱码
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    release_dir, release_id = _resolve_release(args.release)
    filters = {'as_of': args.as_of, 'department': args.department}
    filters = {key: value for key, value in filters.items() if value}

    def on_stage(stage: str, payload: dict) -> None:
        if stage in ('query', 'vectors', 'keywords', 'evidence'):
            print(f'[stage:{stage}] {json.dumps(payload, ensure_ascii=False)}', file=sys.stderr)

    try:
        retriever = PolicyRetriever(
            release_dir=release_dir,
            backend=args.backend,
            device=args.device,
        )
        result = retriever.search(
            args.question,
            top_k=args.top_k,
            filters=filters,
            on_stage=on_stage,
        )
    except Exception as exc:
        raise SystemExit(f'检索失败：{type(exc).__name__}: {exc}') from exc

    info = retriever.info()
    print(f'发布：{release_id} | 目录：{release_dir}')
    print(f'后端：{info["backend"]} | 设备：{info["device"]} | '
          f'单元 {info["unit_count"]} / 片段 {info["passage_count"]}')
    print()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_result(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
