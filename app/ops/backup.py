"""数据备份与恢复辅助（T22）。

备份内容（默认）：
- ``data/docs``：原文、解析结果、单元/问题清单；
- ``data/releases``：发布快照（manifest/units/passages/vectors）；
- ``data/state``：生效指针、文档目录库、入库任务；
- 根目录 ``.env``（存在时）与 ``app/project.json``、``data/state/active_release.json`` 所在层级已含于 state。

默认**不备份** models/（体积大、可重新下载）。备份为单个 zip，写完后做
zip 完整性校验；按 ``--keep`` 保留最近 N 份。恢复 = 停服务 → 解压覆盖 → 重启。
"""
from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path

from app import config


def _zip_tree(archive: zipfile.ZipFile, folder: Path, arc_root: str) -> int:
    count = 0
    if not folder.is_dir():
        return 0
    for path in sorted(folder.rglob('*')):
        if not path.is_file():
            continue
        archive.write(path, f'{arc_root}/{path.relative_to(folder).as_posix()}')
        count += 1
    return count


def make_backup(out_dir: Path | str | None = None, keep: int = 7,
                include_models: bool = False) -> dict:
    """打一个备份 zip，返回 {"path", "files", "bytes", "pruned"}。"""
    root = Path(config.ROOT)
    out = Path(out_dir) if out_dir else root / 'backups'
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    target = out / f'policyrag-{stamp}.zip'
    tmp = target.with_suffix('.tmp')
    files = 0
    folders = [('data/docs', config.DOCS_DIR), ('data/releases', config.RELEASES_DIR),
               ('data/state', config.STATE_DIR)]
    if include_models:
        folders.append(('models', root / 'models'))
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for arc_root, folder in folders:
            files += _zip_tree(archive, Path(folder), arc_root)
        for name in ('app/project.json',):
            path = root / name
            if path.is_file():
                archive.write(path, name)
                files += 1
        env_file = root / '.env'
        if env_file.is_file():
            archive.write(env_file, '.env')
            files += 1
        meta = {'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'release_id': _active_release(), 'files': files}
        archive.writestr('backup-meta.json', json.dumps(meta, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(tmp) as archive:
        bad = archive.testzip()
        if bad:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f'备份校验失败：{bad} 损坏')
    tmp.replace(target)
    pruned = prune(out, keep)
    return {'path': str(target), 'files': files, 'bytes': target.stat().st_size, 'pruned': pruned}


def _active_release() -> str:
    path = Path(config.ACTIVE_RELEASE)
    if not path.is_file():
        return ''
    try:
        return str(json.loads(path.read_text(encoding='utf-8')).get('release_id') or '')
    except (OSError, ValueError):
        return ''


def prune(out_dir: Path | str, keep: int) -> list[str]:
    """只保留最近 ``keep`` 份备份，返回被删除的文件名。"""
    if keep <= 0:
        return []
    out = Path(out_dir)
    backups = sorted(out.glob('policyrag-*.zip'), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for path in backups[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    return removed


def restore(archive: Path | str, target_root: Path | str | None = None) -> dict:
    """解压覆盖到项目根（危险操作，调用方需先停服务并自行确认）。"""
    src = Path(archive)
    root = Path(target_root) if target_root else Path(config.ROOT)
    if not src.is_file():
        raise RuntimeError(f'备份不存在：{src}')
    restored = 0
    with zipfile.ZipFile(src) as zf:
        names = [name for name in zf.namelist() if not name.endswith('/')]
        for name in names:
            zf.extract(name, root)
            restored += 1
    return {'restored': restored, 'root': str(root)}


__all__ = ['make_backup', 'prune', 'restore']
