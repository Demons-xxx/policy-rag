"""入库阶段：parse → structure → quality → chunk → publish。

每个阶段都是「显式输入 → 输出」的函数，可以单独调用与单测：

=================  =================================  =========================================
阶段                输入                               输出
=================  =================================  =========================================
``parse``           ``path: Path``                     parsed dict（契约 3.2，解析器查注册表）
``structure``       ``parsed, meta``                   units（契约 2.2，规则仍在 structure.py）
``quality``         ``units, parsed``                  issues（契约 3.4，规则仍在 quality.py）
``chunk``           ``units, tokenizer=None``          passages（448/64 切分）
``publish``         record / units / issues / doc_dir  落盘 + 登记，返回文档记录
=================  =================================  =========================================

``Pipeline`` 按配置的阶段列表依次执行（可打印进度），``run_document`` 是单文档入库
的编排（parse → structure → quality → publish），供 ``pipeline.ingest_path`` 做薄包装。
``chunk`` 不在单文档入库路径里（片段只在发布时生成），但可作为独立阶段接入，
例如离线预切分或发布前的 dry-run。

结构化 / 质检的判定规则仍分别由 ``structure.py`` / ``quality.py`` 实现，本模块只做
阶段编排，不改任何判定结果。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Sequence

from app import config
from app.ingest import parser as parser_module
from app.ingest import publish as publish_module
from app.ingest import registry
from app.ingest.common import now_iso, relative_path, sha256_file, write_json, write_jsonl
from app.ingest.quality import check_units
from app.ingest.structure import build_units
from app.store.catalog import STATUSES

#: 契约定义的文档登记字段（顺序即写盘顺序）
META_FIELDS = (
    "title", "doc_no", "version_label", "effective_from", "effective_to",
    "department", "status", "supersedes",
)

#: 猜标题时跳过的行首（章号、附件、目录、Markdown 标题等）
_TITLE_SKIP_PREFIX = ("第", "附件", "目录", "目 录", "附录", "#")


# ------------------------------------------------------------------ 公共小工具
def document_id(sha256: str) -> str:
    """契约 2.1：``doc-<sha256 前12位>``，同一内容稳定复现。"""
    return f"doc-{str(sha256 or '')[:12]}"


def clean_meta(meta: dict | None) -> dict:
    """只保留契约字段，空字符串统一成 None；非法状态抛中文 ``ValueError``。"""
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


def guess_title(parsed: dict, path: Path) -> str:
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
    return Path(path).stem


# ------------------------------------------------------------------ 五个阶段
def parse(path: Path | str) -> dict:
    """阶段 1：文件 → parsed dict；解析器由 ``registry`` 按扩展名提供。"""
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"文件不存在：{path}")
    return registry.parser_for(path)(path)


def structure(parsed: dict, meta: dict) -> list[dict]:
    """阶段 2：parsed + 登记信息 → units（规则仍由 structure.build_units 决定）。"""
    return build_units(parsed, meta)


def quality(units: list[dict], parsed: dict) -> list[dict]:
    """阶段 3：units + parsed → issues（规则仍由 quality.check_units 决定）。"""
    return check_units(units, parsed)


def chunk(units: list[dict], tokenizer=None) -> list[dict]:
    """阶段 4：units → 检索片段（每个片段带 unit_id / text / embedding_text / start / end）。

    ``tokenizer`` 为 None 时退化为按字符切分（与 publish.split_text 的兜底一致），
    便于单测与离线 dry-run；正式发布由 ``publish.publish_release`` 传入 BGE 分词器。
    片段 id 由发布阶段统一编号，这里不生成。
    """
    passages: list[dict] = []
    for unit in units or []:
        text = unit.get("text") or ""
        for part in publish_module.split_text(text, tokenizer):
            passages.append({
                "unit_id": unit.get("id"),
                "text": part["text"],
                "embedding_text": publish_module.embedding_text(unit, part["text"]),
                "start": part["start"],
                "end": part["end"],
            })
    return passages


def publish(record: dict, units: list[dict], issues: list[dict], parsed: dict,
            doc_dir: Path, *, source: Path | str | None = None,
            catalog=None, job_id: str | None = None) -> dict:
    """阶段 5：把单元 / 问题 / 元数据落盘，并在 catalog 登记文档。

    - 写 ``data/docs/<doc_id>/`` 下的 ``meta.json`` / ``units.jsonl`` / ``issues.json``；
    - ``source`` 非空时把原文复制成 ``source.<ext>``；JSON/JSONL 落盘走 common 的原子写；
    - ``catalog`` 为 None 时只落盘不登记（便于单独调用 / dry-run）；
    - 返回文档记录 + ``issues`` / ``unit_count`` / ``deduplicated=False``，
      字段与旧 ``pipeline.ingest_path`` 完全一致。
    """
    doc_dir = Path(doc_dir)
    doc_dir.mkdir(parents=True, exist_ok=True)

    source_path: Path | None = None
    if source is not None:
        source = Path(source)
        suffix = parser_module.source_suffix(str(record.get("format") or ""))
        if suffix == ".bin":
            # 领域包注册的新格式：沿用原文件扩展名，避免 source.bin 丢信息
            suffix = source.suffix.lower() or ".bin"
        source_path = doc_dir / f"source{suffix}"
        try:
            shutil.copy2(source, source_path)
        except OSError as exc:
            raise RuntimeError(f"复制原文失败：{exc}") from exc

    record = dict(record)
    record["source_path"] = relative_path(source_path) if source_path is not None else None
    record["imported_at"] = now_iso()

    units = list(units or [])
    issues = list(issues or [])
    meta_payload = dict(record)
    meta_payload["unit_count"] = len(units)
    meta_payload["parser"] = (parsed or {}).get("parser")
    write_json(doc_dir / "meta.json", meta_payload)
    write_jsonl(doc_dir / "units.jsonl", units)
    write_json(doc_dir / "issues.json", {
        "doc_id": record.get("doc_id"),
        "generated_at": record["imported_at"],
        "blocking": any(issue.get("blocking") for issue in issues),
        "issues": issues,
    })

    if catalog is not None:
        catalog.upsert_document(record)
        catalog.record_audit("ingest", doc_id=record.get("doc_id"), job_id=job_id, detail={
            "file": record.get("file_name"),
            "format": record.get("format"),
            "unit_count": len(units),
            "issue_count": len(issues),
            "sha256": record.get("sha256") or "",
        })

    return {
        **record,
        "issues": issues,
        "unit_count": len(units),
        "deduplicated": False,
    }


# ------------------------------------------------------------------ 状态适配器
def stage_parse(state: dict) -> dict:
    """Pipeline 适配：``state["parsed"] = parse(state["path"])``。"""
    state = dict(state)
    state["parsed"] = parse(state["path"])
    return state


def stage_structure(state: dict) -> dict:
    """Pipeline 适配：猜标题、拼 unit_meta，再 ``state["units"] = structure(...)``。"""
    state = dict(state)
    path = Path(state["path"])
    meta = state.get("meta") or {}
    parsed = state.get("parsed") or {}
    title = meta.get("title") or guess_title(parsed, path)
    unit_meta = {
        **meta,
        "doc_id": state.get("doc_id") or "",
        "doc_title": title,
        "file_name": path.name,
    }
    state["title"] = title
    state["unit_meta"] = unit_meta
    state["units"] = structure(parsed, unit_meta)
    return state


def stage_quality(state: dict) -> dict:
    """Pipeline 适配：``state["issues"] = quality(units, parsed)``。"""
    state = dict(state)
    state["issues"] = quality(state.get("units") or [], state.get("parsed") or {})
    return state


def stage_chunk(state: dict) -> dict:
    """Pipeline 适配：``state["passages"] = chunk(units, tokenizer)``。"""
    state = dict(state)
    state["passages"] = chunk(state.get("units") or [], state.get("tokenizer"))
    return state


def stage_publish(state: dict) -> dict:
    """Pipeline 适配：拼文档记录并落盘登记，结果写入 ``state["result"]``。"""
    state = dict(state)
    path = Path(state["path"])
    parsed = state.get("parsed") or {}
    meta = state.get("meta") or {}
    title = state.get("title") or meta.get("title") or guess_title(parsed, path)
    doc_id = state.get("doc_id") or document_id(state.get("sha256") or "")
    record = {
        "doc_id": doc_id,
        "title": title,
        "doc_no": meta.get("doc_no"),
        "version_label": meta.get("version_label"),
        "effective_from": meta.get("effective_from"),
        "effective_to": meta.get("effective_to"),
        "department": meta.get("department"),
        "status": meta.get("status") or "active",
        "supersedes": meta.get("supersedes"),
        "file_name": path.name,
        "format": state.get("fmt") or parsed.get("format") or "",
        "pages": int(parsed.get("page_count") or len(parsed.get("pages") or []) or 1),
        "has_pdf": 1 if parsed.get("has_pdf") else 0,
        "sha256": state.get("sha256") or "",
        "source_path": None,
        "imported_at": None,
    }
    doc_dir = state.get("doc_dir") or (config.DOCS_DIR / doc_id)
    result = publish(
        record,
        state.get("units") or [],
        state.get("issues") or [],
        parsed,
        doc_dir,
        source=path,
        catalog=state.get("catalog"),
        job_id=state.get("job_id"),
    )
    state["record"] = dict(result)
    state["result"] = result
    return state


#: 阶段名 → 状态适配器
STAGE_FUNCS: dict[str, Callable[[dict], dict]] = {
    "parse": stage_parse,
    "structure": stage_structure,
    "quality": stage_quality,
    "chunk": stage_chunk,
    "publish": stage_publish,
}

#: 完整阶段顺序（spec 的五个阶段）；chunk 只在需要片段时接入
STAGE_ORDER = ("parse", "structure", "quality", "chunk", "publish")

#: 单文档入库实际使用的阶段（片段在发布时生成，入库不预切分）
DOCUMENT_STAGES = ("parse", "structure", "quality", "publish")

ProgressHook = Callable[[str, int, int, dict], None]
StageHook = Callable[[str, str, dict], None]


# ------------------------------------------------------------------ 编排
def _normalize_stages(stages: Sequence | None) -> list[tuple[str, Callable[[dict], dict]]]:
    """把阶段列表统一成 ``[(name, fn)]``。

    支持三种写法：阶段名字符串（如 ``"parse"``）、``(name, fn)`` 二元组、
    以及裸可调用对象（用 ``__name__`` 当阶段名）。
    """
    if stages is None:
        return [(name, STAGE_FUNCS[name]) for name in STAGE_ORDER]

    normalized: list[tuple[str, Callable[[dict], dict]]] = []
    for item in stages:
        if isinstance(item, str):
            if item not in STAGE_FUNCS:
                raise ValueError(
                    f"未知阶段：{item}（可选：{', '.join(STAGE_ORDER)}）"
                )
            normalized.append((item, STAGE_FUNCS[item]))
        elif isinstance(item, tuple) and len(item) == 2:
            name, func = item
            if not callable(func):
                raise TypeError(f"阶段 {name!r} 的函数必须可调用：{func!r}")
            normalized.append((str(name), func))
        elif callable(item):
            normalized.append((getattr(item, "__name__", "stage"), item))
        else:
            raise TypeError(f"无法识别的阶段定义：{item!r}")
    return normalized


class Pipeline:
    """按配置的阶段列表依次执行一个 state dict（每阶段输入输出都是 state）。

    >>> Pipeline()                       # 默认五个阶段：parse/structure/quality/chunk/publish
    >>> Pipeline(["parse", "structure"]) # 子集
    >>> Pipeline([("parse", my_parse)])  # 替换某个阶段的实现
    >>> Pipeline(DOCUMENT_STAGES, echo=True)  # 打印进度
    """

    def __init__(self, stages: Sequence | None = None,
                 on_progress: ProgressHook | None = None,
                 echo: bool = False) -> None:
        self.stages = _normalize_stages(stages)
        self.on_progress = on_progress
        self.echo = bool(echo)

    @property
    def stage_names(self) -> list[str]:
        """当前阶段顺序（便于测试与日志）。"""
        return [name for name, _ in self.stages]

    def run(self, state: dict, on_stage: StageHook | None = None) -> dict:
        """依次执行阶段；``on_stage(name, "before"|"after", state)`` 可选钩子。"""
        total = len(self.stages)
        for index, (name, func) in enumerate(self.stages, start=1):
            if on_stage is not None:
                on_stage(name, "before", state)
            state = func(state)
            if self.echo:
                print(f"[{index}/{total}] {name}")
            if self.on_progress is not None:
                self.on_progress(name, index, total, state)
            if on_stage is not None:
                on_stage(name, "after", state)
        return state


def run_stages(state: dict, stages: Sequence | None = None,
               on_progress: ProgressHook | None = None, echo: bool = False,
               on_stage: StageHook | None = None) -> dict:
    """一次性编排入口：等价于 ``Pipeline(stages, ...).run(state)``。"""
    return Pipeline(stages, on_progress=on_progress, echo=echo).run(state, on_stage=on_stage)


def run_document(path: Path | str, meta: dict | None = None, *,
                 sha256: str | None = None, doc_id: str | None = None,
                 catalog=None, job_id: str | None = None,
                 on_stage: StageHook | None = None,
                 on_progress: ProgressHook | None = None,
                 echo: bool = False) -> dict:
    """单文档入库编排：parse → structure → quality → publish，返回文档记录。

    这是 ``pipeline.ingest_path`` 的薄包装目标：去重、catalog 生命周期由调用方负责，
    这里只负责阶段串联与返回结构。
    """
    config.ensure_dirs()
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"文件不存在：{path}")

    sha256 = sha256 or sha256_file(path)
    state = {
        "path": path,
        "meta": clean_meta(meta),
        "sha256": sha256,
        "fmt": registry.format_for(path),
        "doc_id": doc_id or document_id(sha256),
        "doc_dir": None,
        "catalog": catalog,
        "job_id": job_id,
        "tokenizer": None,
        "issues": [],
        "passages": [],
    }
    state["doc_dir"] = config.DOCS_DIR / state["doc_id"]

    pipeline = Pipeline(DOCUMENT_STAGES, on_progress=on_progress, echo=echo)
    state = pipeline.run(state, on_stage=on_stage)
    return state["result"]


__all__ = [
    "DOCUMENT_STAGES",
    "META_FIELDS",
    "Pipeline",
    "STAGE_FUNCS",
    "STAGE_ORDER",
    "chunk",
    "clean_meta",
    "document_id",
    "guess_title",
    "parse",
    "publish",
    "quality",
    "run_document",
    "run_stages",
    "stage_chunk",
    "stage_parse",
    "stage_publish",
    "stage_quality",
    "stage_structure",
    "structure",
]
