"""Stage 3 阶段化入库测试：解析器注册表、五个阶段、Pipeline 编排与旧函数一致性。

只使用 tests/fixtures 下自造的小样本，不依赖网络与真实大模型。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import make_samples  # noqa: E402

from app import config  # noqa: E402
from app.ingest import parser as parser_module  # noqa: E402
from app.ingest import registry  # noqa: E402
from app.ingest import stages as stage_module  # noqa: E402
from app.ingest.common import read_jsonl  # noqa: E402
from app.ingest.parser import parse_document  # noqa: E402
from app.ingest.pipeline import ingest_path, job_status  # noqa: E402
from app.ingest.quality import check_units  # noqa: E402
from app.ingest.structure import build_units  # noqa: E402
from app.store.catalog import Catalog  # noqa: E402

CUSTOM_EXT = ".stageprobe"
CUSTOM_PATH_SUFFIX = "probe.stageprobe"


# ------------------------------------------------------------------ 测试桩
class FakeTokenizer:
    """字符级假分词器，与 tests/test_ingest.py 中的实现一致。"""

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=False, **kwargs):
        offsets = [(index, index + 1) for index in range(len(text))]
        return {"input_ids": list(range(len(text))), "offset_mapping": offsets}


def _patch_project(monkeypatch, root: Path) -> Path:
    """把 app.config 的所有数据路径指向临时目录（与 test_ingest.py 的 project 夹具一致）。"""
    data = root / "data"
    paths = {
        "DATA_DIR": data,
        "STATE_DIR": data / "state",
        "RELEASES_DIR": data / "releases",
        "DOCS_DIR": data / "docs",
        "JOBS_DIR": data / "jobs",
        "LOGS_DIR": data / "logs",
        "UPLOADS_DIR": data / "uploads",
        "VECTOR_DIR": data / "vector_store",
        "ACTIVE_RELEASE": data / "state" / "active_release.json",
        "CATALOG_DB": data / "state" / "catalog.sqlite",
    }
    for name, value in paths.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "VECTOR_BACKEND", "npz")
    config.ensure_dirs()
    return root


@pytest.fixture()
def project(tmp_path, monkeypatch):
    return _patch_project(monkeypatch, tmp_path / "project")


@pytest.fixture()
def registry_guard():
    """注册表是模块级全局：测试结束后恢复，避免污染其它用例。"""
    parse_snapshot = dict(registry.PARSE_REGISTRY)
    format_snapshot = dict(registry.FORMAT_REGISTRY)
    yield
    registry.PARSE_REGISTRY.clear()
    registry.PARSE_REGISTRY.update(parse_snapshot)
    registry.FORMAT_REGISTRY.clear()
    registry.FORMAT_REGISTRY.update(format_snapshot)


def _record_template(path: Path, parsed: dict, meta: dict, doc_id: str, sha256: str) -> dict:
    return {
        "doc_id": doc_id,
        "title": meta.get("title") or "测试制度",
        "doc_no": meta.get("doc_no"),
        "version_label": meta.get("version_label"),
        "effective_from": meta.get("effective_from"),
        "effective_to": meta.get("effective_to"),
        "department": meta.get("department"),
        "status": meta.get("status") or "active",
        "supersedes": meta.get("supersedes"),
        "file_name": path.name,
        "format": parsed.get("format") or "",
        "pages": int(parsed.get("page_count") or len(parsed.get("pages") or []) or 1),
        "has_pdf": 1 if parsed.get("has_pdf") else 0,
        "sha256": sha256,
        "source_path": None,
        "imported_at": None,
    }


# ------------------------------------------------------------------ 注册表
def test_supported_extensions_cover_builtin_formats():
    extensions = registry.supported_extensions()
    assert extensions == sorted(extensions)
    for ext in (".pdf", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"):
        assert ext in extensions, ext
    for ext in extensions:
        parser_fn = registry.parser_for(Path(f"sample{ext}"))
        assert callable(parser_fn)
        assert registry.format_for(Path(f"sample{ext}"))


def test_register_parser_custom_extension(registry_guard):
    def custom_parser(path: Path) -> dict:
        return {
            "format": "stageprobe", "pages": [{"no": 1, "text": "自定义解析"}],
            "has_pdf": False, "page_count": 1, "tables": [], "blocks": [],
            "parser": "stageprobe",
        }

    returned = registry.register_parser(CUSTOM_EXT, custom_parser)
    assert returned is custom_parser
    # 扩展名大小写/前缀都会被规范化
    assert registry.parser_for("x.stageprobe") is custom_parser
    assert registry.parser_for("x.STAGEPROBE") is custom_parser
    assert CUSTOM_EXT in registry.supported_extensions()
    assert registry.format_for("x.stageprobe") == "stageprobe"

    # 注册后 parse_document / stages.parse 都走新解析器
    path = Path("任意文件.stageprobe")
    assert registry.parser_for(path)(path) == custom_parser(path)


def test_register_parser_validation(registry_guard):
    with pytest.raises(ValueError, match="扩展名不能为空"):
        registry.register_parser("", lambda path: {})
    with pytest.raises(TypeError, match="解析函数必须可调用"):
        registry.register_parser(CUSTOM_EXT, None)  # type: ignore[arg-type]


def test_unknown_extension_error_lists_supported(project, tmp_path):
    path = tmp_path / "表格.xlsx"
    path.write_bytes(b"not a document")

    with pytest.raises(RuntimeError) as excinfo:
        parser_module.detect_format(path)
    message = str(excinfo.value)
    assert message.startswith("不支持的文件格式")
    assert ".xlsx" in message
    for ext in registry.supported_extensions():
        assert ext in message

    with pytest.raises(RuntimeError, match="不支持的文件格式"):
        registry.parser_for(path)
    with pytest.raises(RuntimeError, match="不支持的文件格式"):
        registry.format_for(path)
    with pytest.raises(RuntimeError, match="不支持的文件格式"):
        parse_document(path)

    no_suffix = tmp_path / "无扩展名"
    no_suffix.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="（无扩展名）"):
        registry.parser_for(no_suffix)


def test_parse_document_dispatches_through_registry(registry_guard, tmp_path):
    marker = {"format": "stageprobe", "pages": [{"no": 1, "text": "注册表生效"}],
              "has_pdf": False, "page_count": 1, "parser": "stageprobe"}

    registry.register_parser(CUSTOM_EXT, lambda path: dict(marker))
    path = tmp_path / CUSTOM_PATH_SUFFIX
    path.write_text("内容", encoding="utf-8")
    assert parse_document(path) == marker
    assert stage_module.parse(path) == marker


def test_html_parser_extracts_text_and_table(project, tmp_path):
    path = tmp_path / "制度.html"
    path.write_text(
        "<html><head><title>标题</title><style>.a{}</style></head><body>"
        "<h1>第一章 总则</h1><p>第一条 为规范管理，制定本办法。</p>"
        "<table><tr><td>项目</td><td>标准</td></tr>"
        "<tr><td>差旅费</td><td>300元/天</td></tr></table>"
        "<script>var x = 1;</script></body></html>",
        encoding="utf-8",
    )
    parsed = stage_module.parse(path)
    assert parsed["format"] == "html"
    assert parsed["has_pdf"] is False
    assert parsed["page_count"] == 1
    text = parsed["pages"][0]["text"]
    assert "第一章 总则" in text
    assert "第一条 为规范管理" in text
    assert "项目 | 标准" in text
    assert "差旅费 | 300元/天" in text
    # 标签、脚本、样式与 head 里的标题都不应出现在正文
    assert "<" not in text and "var x" not in text and ".a{}" not in text
    assert "标题" not in text


# ------------------------------------------------------------------ 阶段函数与旧实现一致
def test_stage_parse_structure_quality_match_legacy(project, tmp_path):
    path = make_samples.build_txt(tmp_path)
    parsed = stage_module.parse(path)
    assert parsed == parse_document(path)

    meta = {
        "doc_id": "doc-legacy0001",
        "doc_title": "XX大学差旅费管理办法",
        "file_name": path.name,
        "status": "active",
    }
    units = stage_module.structure(parsed, meta)
    assert units == build_units(parsed, meta)

    issues = stage_module.quality(units, parsed)
    assert issues == check_units(units, parsed)


def test_chunk_stage_splits_units(project):
    unit = {"id": "doc-x#u0001", "doc_title": "测试制度", "article": "第一条",
            "text": "第一条 测试条款。" + "字" * 1000}
    passages = stage_module.chunk([unit], FakeTokenizer())
    assert len(passages) == 3
    assert passages[0]["unit_id"] == "doc-x#u0001"
    assert passages[0]["start"] == 0
    assert passages[1]["start"] == 448 - 64
    assert passages[-1]["end"] == len(unit["text"])
    assert all(set(item) == {"unit_id", "text", "embedding_text", "start", "end"}
               for item in passages)
    assert all(item["embedding_text"].startswith("《测试制度》第一条 ") for item in passages)

    # tokenizer 缺省时退化为按字符切分，短文本仍是一个片段
    short = stage_module.chunk([{"id": "doc-x#u0002", "text": "短文本"}])
    assert short == [{"unit_id": "doc-x#u0002", "text": "短文本",
                      "embedding_text": "短文本", "start": 0, "end": 3}]
    assert stage_module.chunk([{"id": "doc-x#u0003", "text": "   "}]) == []


def test_publish_stage_writes_artifacts_and_registers(project, tmp_path):
    path = make_samples.build_txt(tmp_path)
    parsed = stage_module.parse(path)
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    doc_id = stage_module.document_id(sha256)
    meta = stage_module.clean_meta({"title": "XX大学差旅费管理办法", "department": "财务处"})
    units = stage_module.structure(parsed, {
        **meta, "doc_id": doc_id, "doc_title": meta["title"], "file_name": path.name,
    })
    issues = stage_module.quality(units, parsed)
    record = _record_template(path, parsed, meta, doc_id, sha256)
    doc_dir = config.DOCS_DIR / doc_id

    catalog = Catalog()
    try:
        result = stage_module.publish(record, units, issues, parsed, doc_dir,
                                      source=path, catalog=catalog)
        stored = catalog.get_document(doc_id)
    finally:
        catalog.close()

    assert result["doc_id"] == doc_id
    assert result["unit_count"] == len(units) > 0
    assert result["issues"] == issues
    assert result["deduplicated"] is False
    assert result["source_path"].endswith("source.txt")
    assert result["imported_at"]
    assert stored is not None and stored["title"] == "XX大学差旅费管理办法"

    for name in ("source.txt", "meta.json", "units.jsonl", "issues.json"):
        assert (doc_dir / name).is_file(), name
    meta_payload = json.loads((doc_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_payload["unit_count"] == len(units)
    assert meta_payload["parser"] == "text"
    issues_payload = json.loads((doc_dir / "issues.json").read_text(encoding="utf-8"))
    assert issues_payload["doc_id"] == doc_id
    assert issues_payload["issues"] == issues
    assert read_jsonl(doc_dir / "units.jsonl") == units


def test_publish_stage_without_catalog_only_writes_files(project, tmp_path):
    path = make_samples.build_txt(tmp_path)
    parsed = stage_module.parse(path)
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    doc_id = stage_module.document_id(sha256)
    meta = stage_module.clean_meta({"title": "只落盘"})
    units = stage_module.structure(parsed, {
        **meta, "doc_id": doc_id, "doc_title": meta["title"], "file_name": path.name,
    })
    doc_dir = config.DOCS_DIR / doc_id
    stage_module.publish(_record_template(path, parsed, meta, doc_id, sha256),
                         units, [], parsed, doc_dir, source=None, catalog=None)
    assert (doc_dir / "units.jsonl").is_file()
    assert not (doc_dir / "source.txt").exists()
    catalog = Catalog()
    try:
        assert catalog.get_document(doc_id) is None
    finally:
        catalog.close()


# ------------------------------------------------------------------ Pipeline 编排
def test_pipeline_default_stage_order():
    assert stage_module.STAGE_ORDER == ("parse", "structure", "quality", "chunk", "publish")
    assert stage_module.Pipeline().stage_names == list(stage_module.STAGE_ORDER)
    assert stage_module.Pipeline(stage_module.DOCUMENT_STAGES).stage_names == [
        "parse", "structure", "quality", "publish",
    ]


def test_pipeline_rejects_unknown_stage():
    with pytest.raises(ValueError, match="未知阶段"):
        stage_module.Pipeline(["nope"])
    with pytest.raises(TypeError, match="无法识别的阶段定义"):
        stage_module.Pipeline([123])  # type: ignore[list-item]


def test_pipeline_runs_stages_in_order_and_prints(capsys):
    calls: list[str] = []
    progress: list[tuple[str, int, int]] = []
    hooks: list[tuple[str, str]] = []

    def step(name: str):
        def run(state):
            calls.append(name)
            return {**state, "trace": [*state.get("trace", []), name]}
        return run

    pipeline = stage_module.Pipeline(
        [("alpha", step("alpha")), ("beta", step("beta")), ("gamma", step("gamma"))],
        on_progress=lambda name, index, total, state: progress.append((name, index, total)),
        echo=True,
    )
    state = pipeline.run({"trace": []}, on_stage=lambda name, when, st: hooks.append((name, when)))

    assert calls == ["alpha", "beta", "gamma"]
    assert state["trace"] == ["alpha", "beta", "gamma"]
    assert pipeline.stage_names == ["alpha", "beta", "gamma"]
    assert progress == [("alpha", 1, 3), ("beta", 2, 3), ("gamma", 3, 3)]
    assert hooks == [("alpha", "before"), ("alpha", "after"),
                     ("beta", "before"), ("beta", "after"),
                     ("gamma", "before"), ("gamma", "after")]
    stdout = capsys.readouterr().out
    assert "[1/3] alpha" in stdout and "[3/3] gamma" in stdout

    # run_stages 是同一编排的一次性入口
    state2 = stage_module.run_stages({"trace": []}, [("alpha", step("alpha"))])
    assert state2["trace"] == ["alpha"]


def test_pipeline_default_stages_end_to_end(project, tmp_path):
    path = make_samples.build_txt(tmp_path)
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    doc_id = stage_module.document_id(sha256)
    state = {
        "path": path,
        "meta": stage_module.clean_meta({"title": "Pipeline 全阶段"}),
        "sha256": sha256,
        "fmt": registry.format_for(path),
        "doc_id": doc_id,
        "doc_dir": config.DOCS_DIR / doc_id,
        "catalog": None,
        "issues": [],
        "passages": [],
    }
    final = stage_module.Pipeline().run(state)

    assert final["result"]["doc_id"] == doc_id
    assert final["result"]["title"] == "Pipeline 全阶段"
    assert final["units"] and final["issues"] == []
    assert final["passages"], "chunk 阶段应生成检索片段"
    assert all(item["unit_id"].startswith(doc_id) for item in final["passages"])
    assert (final["doc_dir"] / "units.jsonl").is_file()
    assert read_jsonl(final["doc_dir"] / "units.jsonl") == final["units"]


# ------------------------------------------------------------------ 与旧函数结果一致
def test_run_document_matches_ingest_path(tmp_path, monkeypatch):
    path = make_samples.build_txt(tmp_path)
    meta = {
        "title": "XX大学差旅费管理办法",
        "doc_no": "校发〔2024〕1号",
        "version_label": "2024年版",
        "effective_from": "2024-05-01",
        "department": "财务处",
        "status": "active",
    }

    _patch_project(monkeypatch, tmp_path / "project-stages")
    staged = stage_module.run_document(path, meta)

    _patch_project(monkeypatch, tmp_path / "project-legacy")
    legacy = ingest_path(path, meta=meta)

    comparable = (
        "doc_id", "title", "doc_no", "version_label", "effective_from", "effective_to",
        "department", "status", "supersedes", "file_name", "format", "pages",
        "has_pdf", "sha256", "deduplicated", "unit_count", "issues",
    )
    for key in comparable:
        assert staged[key] == legacy[key], key

    # 单元文件逐字一致（结构规则未变）
    staged_units = read_jsonl(config.DOCS_DIR / staged["doc_id"] / "units.jsonl")
    legacy_units = read_jsonl(config.DOCS_DIR / legacy["doc_id"] / "units.jsonl")
    assert staged_units == legacy_units


def test_ingest_path_job_progress_messages(project, tmp_path):
    catalog = Catalog()
    try:
        catalog.create_job("job-stage-test", "upload", {"files": [], "meta": {}})
    finally:
        catalog.close()

    record = ingest_path(make_samples.build_txt(tmp_path), job_id="job-stage-test")
    job = job_status("job-stage-test")
    assert record["unit_count"] > 0
    assert job["progress"] == 90
    assert job["phase"] == "quality"
    assert job["message"] == f"入库完成：{record['title']}（{record['unit_count']} 个知识单元）"
