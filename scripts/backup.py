"""命令行备份：python scripts/backup.py [--out backups] [--keep 7] [--include-models]

恢复：python scripts/backup.py --restore backups/policyrag-XXXX.zip
（恢复前先停服务；会覆盖 data/docs、data/releases、data/state 与 .env）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ops import backup  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='PolicyRAG 备份/恢复')
    parser.add_argument('--out', default=str(ROOT / 'backups'), help='备份目录')
    parser.add_argument('--keep', type=int, default=7, help='保留最近 N 份（0 不清理）')
    parser.add_argument('--include-models', action='store_true', help='连同 models/ 一起打包（很大）')
    parser.add_argument('--restore', default=None, help='恢复指定 zip（覆盖当前数据）')
    args = parser.parse_args(argv)

    if args.restore:
        result = backup.restore(Path(args.restore))
        print(f"已恢复 {result['restored']} 个文件到 {result['root']}；请重启服务并检查 /api/info")
        return 0
    result = backup.make_backup(args.out, args.keep, args.include_models)
    print(f"备份完成：{result['path']}")
    print(f"  文件 {result['files']} 个，{result['bytes'] / 1024 / 1024:.1f} MB")
    if result['pruned']:
        print(f"  已清理旧备份：{', '.join(result['pruned'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
