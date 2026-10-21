"""入库链路测试：解析、条款切分、质量检查、入库任务与发布。

全部使用 tests/fixtures 下自造的小样本；发布测试用假编码器/假 Chroma 后端，
不依赖网络与真实大模型。
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import numpy as np
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import make_samples  # noqa: E402

from app import config  # noqa: E402
from app.store import releases as release_module  # noqa: E402
from app.ingest import publish as publish_module  # noqa: E402
from app.ingest.common import read_jsonl  # noqa: E402
from app.ingest.parser import parse_document  # noqa: E402
from app.ingest.pipeline import ingest_path, job_status, submit_upload  # noqa: E402
from app.ingest.quality import check_units  # noqa: E402
from app.ingest.structure import build_units  # noqa: E402
from app.store.catalog import Catalog  # noqa: E402


# ------------------------------------------------------------------ 测试桩
class FakeTokenizer:
    """字符级假分词器，兼容 HF tokenizer 的 offset_mapping 调用方式。"""

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=False, **kwargs):
        offsets = [(index, index + 1) for index in range(len(text))]
        return {"input_ids": list(range(len(text))), "offset_mapping": offsets}

    def encode(self, text, add_special_tokens=False, **kwargs):
        return list(range(len(text)))


class FakeEncoder:
    device = "cpu"

    def __init__(self):
        self.tokenizer = FakeTokenizer()
        self.encode_calls = 0

    def encode(self, texts, query=False, batch_size=32):
        self.encode_calls += 1
        vectors = np.zeros((len(texts), 8), dtype=np.float32)
        for index, text in enumerate(texts):
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors[index] = np.frombuffer(digest[:32], dtype=np.uint8)[:8].astype(np.float32)
        return vectors


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """把 app.config 的所有数据路径指向临时目录。"""
    root = tmp_path / "project"
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


def _sample_units(path: Path, doc_id: str = "doc-test000001", **meta) -> tuple[list[dict], dict]:
    parsed = parse_document(path)
    base = {
        "doc_id": doc_id,
        "title": "测试制度",
        "file_name": Path(path).name,
        "department": "测试部门",
        "effective_from": "2024-05-01",
        "status": "active",
    }
    base.update(meta)
    return build_units(parsed, base), parsed


def _docx_units(tmp_path) -> list[dict]:
    units, _ = _sample_units(make_samples.build_docx(tmp_path))
    return units


# ------------------------------------------------------------------ 解析
def test_parse_txt(project, tmp_path):
    parsed = parse_document(make_samples.build_txt(tmp_path))
    assert parsed["format"] == "txt"
    assert parsed["has_pdf"] is False
    assert parsed["page_count"] == 1
    assert len(parsed["pages"]) == 1
    assert "第一章 总则" in parsed["pages"][0]["text"]
    assert "第一条" in parsed["pages"][0]["text"]


def test_parse_docx_with_table(project, tmp_path):
    path = make_samples.build_docx(tmp_path)
    parsed = parse_document(path)
    assert parsed["format"] == "docx"
    assert parsed["has_pdf"] is False
    assert parsed["page_count"] == 1
    text = parsed["pages"][0]["text"]
    assert "第一章 总则" in text
    assert "项目 | 标准 | 说明" in text
    assert len(parsed["tables"]) == 1
    table = parsed["tables"][0]
    assert table["headers"] == ["项目", "标准", "说明"]
    assert ["差旅费", "300元/天", "凭票据报销"] in table["rows"]
    # blocks 保留正文与表格的原始顺序
    kinds = [block["type"] for block in parsed["blocks"]]
    assert kinds.count("table") == 1


def test_parse_pdf_pages(project, tmp_path):
    parsed = parse_document(make_samples.build_pdf(tmp_path))
    assert parsed["format"] == "pdf"
    assert parsed["has_pdf"] is True
    assert parsed["page_count"] == 2
    assert "第一条" in parsed["pages"][0]["text"]
    assert "第三条" in parsed["pages"][1]["text"]


def test_parse_markdown(project, tmp_path):
    parsed = parse_document(make_samples.build_md(tmp_path))
    assert parsed["format"] == "md"
    assert "第一条" in parsed["pages"][0]["text"]


def test_parse_unsupported(project, tmp_path):
    path = tmp_path / "a.xlsx"
    path.write_bytes(b"not a document")
    with pytest.raises(RuntimeError, match="不支持的文件格式"):
        parse_document(path)


# ------------------------------------------------------------------ 结构化
def test_units_articles_and_items(project, tmp_path):
    units, _ = _sample_units(make_samples.build_txt(tmp_path))
    assert units, "应至少解析出一个单元"
    # id / order 连续且与契约一致
    for index, unit in enumerate(units, start=1):
        assert unit["order"] == index
        assert unit["id"] == f"doc-test000001#u{unit['order']:04d}"
    articles = [unit for unit in units if unit["kind"] == "article"]
    assert [unit["article"] for unit in articles] == [
        "第一条", "第二条", "第三条", "第四条", "第五条",
    ]
    by_article = {unit["article"]: unit for unit in articles}
    fourth = by_article["第四条"]
    # 条款正文包含后续款项，不拆碎
    for item in ("（一）", "（二）", "（三）", "300 元", "500 元", "审批"):
        assert item in fourth["text"]
    assert by_article["第三条"]["article_no"] == 3
    assert all(unit["doc_id"] == "doc-test000001" for unit in units)
    assert all(unit["doc_title"] == "测试制度" for unit in units)


def test_units_chapter_and_heading_path(project, tmp_path):
    units, _ = _sample_units(make_samples.build_txt(tmp_path))
    chapters = [unit for unit in units if unit["kind"] == "section"]
    assert chapters[0]["text"] == "第一章 总则"
    article = next(unit for unit in units if unit["kind"] == "article")
    assert article["chapter"] == "第一章 总则"
    assert article["heading_path"][0] == "第一章 总则"
    # 没有编号的标题行成为 paragraph
    assert units[0]["kind"] == "paragraph"
    assert units[0]["text"] == "XX大学差旅费管理办法"


def test_units_attachment(project, tmp_path):
    units, _ = _sample_units(make_samples.build_docx(tmp_path))
    attachments = [unit for unit in units if unit["kind"] == "attachment"]
    assert attachments
    assert attachments[0]["text"].startswith("附件1：")
    # 附件后续正文归入附件单元
    assert "项目负责人签字" in attachments[0]["text"]


def test_units_table_rows(project, tmp_path):
    units, _ = _sample_units(make_samples.build_docx(tmp_path))
    rows = [unit for unit in units if unit["kind"] == "table_row"]
    assert len(rows) == 3
    assert rows[0]["table"]["name"] == "科研经费报销标准表"
    assert rows[0]["table"]["headers"] == ["项目", "标准", "说明"]
    assert rows[0]["table"]["row"] == ["项目", "标准", "说明"]
    assert rows[1]["table"]["row"] == ["差旅费", "300元/天", "凭票据报销"]
    assert rows[1]["text"] == "差旅费 | 300元/天 | 凭票据报销"
    # 表格行不并入条款正文
    article = next(unit for unit in units if unit["article"] == "第三条")
    assert "差旅费 | 300元/天" not in article["text"]


def test_units_markdown_heading_and_table(project, tmp_path):
    units, _ = _sample_units(make_samples.build_md(tmp_path))
    sections = [unit["text"] for unit in units if unit["kind"] == "section"]
    assert "第一章 总则" in sections
    rows = [unit for unit in units if unit["kind"] == "table_row"]
    assert rows and rows[0]["table"]["headers"] == ["请假天数", "审批人", "备注"]
    articles = [unit["article"] for unit in units if unit["kind"] == "article"]
    assert "第三条" in articles


def test_units_pdf_page_mapping(project, tmp_path):
    units, _ = _sample_units(make_samples.build_pdf(tmp_path))
    first = next(unit for unit in units if unit["article"] == "第一条")
    second = next(unit for unit in units if unit["article"] == "第三条")
    assert first["pages"] == [1]
    assert second["pages"] == [2]
    assert second["source"]["page"] == 2
    assert first["source"]["parser"] == "pymupdf"


# ------------------------------------------------------------------ 质量
def test_quality_empty_pages_and_no_text(project):
    parsed = {
        "format": "pdf",
        "pages": [{"no": 1, "text": ""}, {"no": 2, "text": "   "}],
        "has_pdf": True,
        "page_count": 2,
    }
    issues = check_units([], parsed)
    codes = {issue["code"] for issue in issues}
    assert "empty_page" in codes
    assert "no_text" in codes
    no_text = next(issue for issue in issues if issue["code"] == "no_text")
    assert no_text["blocking"] is True
    assert no_text["level"] == "error"
    for issue in issues:
        assert set(issue) == {"level", "code", "message", "page", "blocking"}


def test_quality_garbled(project):
    text = "实验室安全管理制度正文内容。" * 10 + "\ufffd\ufffd\ufffd"
    parsed = {"format": "txt", "pages": [{"no": 1, "text": text}],
              "has_pdf": False, "page_count": 1}
    units = [{"order": 1, "kind": "article", "text": text, "pages": [1],
              "article": "第一条", "article_no": 1, "doc_id": "doc-x"}]
    issues = check_units(units, parsed)
    assert any(issue["code"] == "garbled" for issue in issues)
    assert not any(issue["blocking"] for issue in issues)


def test_quality_duplicate_and_missing_article(project):
    text = "重复的制度正文内容，用于验证完全重复检测。" * 2
    parsed = {"format": "txt", "pages": [{"no": 1, "text": text}],
              "has_pdf": False, "page_count": 1}
    units = [
        {"order": 1, "kind": "article", "text": text, "pages": [1],
         "article": "第一条", "article_no": 1, "doc_id": "doc-x"},
        {"order": 2, "kind": "article", "text": text, "pages": [1],
         "article": "第二条", "article_no": 2, "doc_id": "doc-x"},
        {"order": 3, "kind": "article", "text": "第五条 其他规定。", "pages": [1],
         "article": "第五条", "article_no": 5, "doc_id": "doc-x"},
    ]
    issues = check_units(units, parsed)
    codes = [issue["code"] for issue in issues]
    assert "duplicate" in codes
    missing = next(issue for issue in issues if issue["code"] == "missing_article")
    assert "第3条" in missing["message"] and "第4条" in missing["message"]
    assert missing["blocking"] is False


# ------------------------------------------------------------------ 入库
def test_ingest_path_and_dedupe(project, tmp_path):
    path = make_samples.build_docx(tmp_path)
    meta = {
        "title": "XX大学科研经费报销实施细则",
        "doc_no": "校发〔2024〕38号",
        "version_label": "2024年版",
        "effective_from": "2024-05-01",
        "department": "科研院、财务处",
        "status": "active",
    }
    first = ingest_path(path, meta=meta)
    expected = "doc-" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    assert first["doc_id"] == expected
    assert first["deduplicated"] is False
    assert first["unit_count"] > 0
    assert first["has_pdf"] == 0
    assert first["format"] == "docx"

    doc_dir = config.DOCS_DIR / first["doc_id"]
    for name in ("source.docx", "meta.json", "units.jsonl", "issues.json"):
        assert (doc_dir / name).is_file(), name
    assert read_jsonl(doc_dir / "units.jsonl")

    second = ingest_path(path)
    assert second["doc_id"] == first["doc_id"]
    assert second["deduplicated"] is True
    assert second["issues"] == first["issues"]

    catalog = Catalog()
    try:
        assert len(catalog.list_documents()) == 1
        record = catalog.get_document(first["doc_id"])
        assert record["title"] == meta["title"]
        assert record["status"] == "active"
    finally:
        catalog.close()


def test_ingest_title_fallback(project, tmp_path):
    record = ingest_path(make_samples.build_txt(tmp_path))
    assert record["title"] == "XX大学差旅费管理办法"


def test_submit_upload_job(project, tmp_path):
    data = make_samples.build_txt(tmp_path).read_bytes()
    job_id = submit_upload([("差旅费管理办法.txt", data)], meta={"title": "差旅费管理办法"})
    assert job_id.startswith("job-")

    deadline = time.time() + 20
    job = None
    while time.time() < deadline:
        job = job_status(job_id)
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job is not None
    assert job["status"] == "done", job
    assert job["progress"] == 100
    assert len(job["payload"]["docs"]) == 1
    assert job["payload"]["docs"][0]["title"] == "差旅费管理办法"
    saved = list((config.UPLOADS_DIR / job_id).iterdir())
    assert saved and saved[0].is_file()


def test_job_status_missing(project):
    with pytest.raises(KeyError, match="任务不存在"):
        job_status("job-not-exist")


# ------------------------------------------------------------------ 发布
def _ingest_samples(tmp_path):
    return [
        ingest_path(make_samples.build_txt(tmp_path), meta={
            "title": "XX大学差旅费管理办法", "department": "财务处",
            "effective_from": "2024-01-01", "status": "active",
        }),
        ingest_path(make_samples.build_docx(tmp_path), meta={
            "title": "XX大学科研经费报销实施细则", "department": "科研院、财务处",
            "effective_from": "2024-05-01", "status": "superseded",
        }),
    ]


def test_publish_npz_and_cache_reuse(project, monkeypatch):
    encoder = FakeEncoder()
    monkeypatch.setattr(publish_module, "_load_encoder", lambda device=None: encoder)
    docs = _ingest_samples(project / "samples")

    active = publish_module.publish_release()
    assert active["doc_count"] == 2
    assert active["unit_count"] > 0
    assert active["id"].startswith("rel-")
    assert active["collection"] == release_module.collection_name(active["id"])

    rel_dir = release_module.release_dir(active)
    for name in ("units.jsonl", "passages.jsonl", "vectors.npz", "manifest.json"):
        assert (rel_dir / name).is_file(), name

    manifest = release_module.load_manifest(active)
    assert manifest["doc_count"] == 2
    assert manifest["embedding"]["dimension"] == 8
    assert "科研院、财务处" in manifest["departments"]
    assert len(manifest["docs"]) == 2

    passages = read_jsonl(rel_dir / "passages.jsonl")
    assert len(passages) == manifest["passage_count"]
    assert [item["id"] for item in passages] == [
        f"p_{index:06d}" for index in range(1, len(passages) + 1)
    ]
    assert any(item["embedding_text"].startswith("《") for item in passages)
    assert all({"id", "unit_id", "text", "embedding_text", "start", "end"} <= set(item)
               for item in passages)

    with np.load(rel_dir / "vectors.npz") as data:
        ids = list(data["ids"])
        vectors = np.asarray(data["vectors"])
    assert ids == [item["id"] for item in passages]
    assert vectors.shape[0] == len(passages)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)

    active_record = release_module.read_active()
    assert active_record["id"] == active["id"]

    # 第二次发布：内容未变，单文档向量缓存命中，不再调用编码器
    cache_files = [config.DOCS_DIR / doc["doc_id"] / "vectors.npz" for doc in docs]
    mtimes = {path: path.stat().st_mtime_ns for path in cache_files}
    calls_before = encoder.encode_calls
    active2 = publish_module.publish_release()
    assert active2["id"] != active["id"]
    assert encoder.encode_calls == calls_before
    for path in cache_files:
        assert path.stat().st_mtime_ns == mtimes[path], "未变化文档应复用 vectors.npz"


def test_publish_chroma_backend(project, monkeypatch):
    monkeypatch.setattr(config, "VECTOR_BACKEND", "chroma")
    encoder = FakeEncoder()
    monkeypatch.setattr(publish_module, "_load_encoder", lambda device=None: encoder)
    ingest_path(make_samples.build_txt(project / "samples"))

    captured: dict = {}

    class FakeBackend:
        def add(self, ids, vectors, metadatas):
            captured["ids"] = ids
            captured["vectors"] = vectors
            captured["metadatas"] = metadatas

    def fake_create_backend(name, release_dir, collection):
        captured["name"] = name
        captured["release_dir"] = Path(release_dir)
        captured["collection"] = collection
        return FakeBackend()

    import app.retrieval.backends as backends

    monkeypatch.setattr(backends, "create_backend", fake_create_backend)
    active = publish_module.publish_release()

    assert captured["name"] == "chroma"
    assert captured["collection"] == active["collection"]
    assert captured["release_dir"] == release_module.release_dir(active)
    assert len(captured["ids"]) == len(captured["metadatas"]) > 0
    assert captured["vectors"].shape[0] == len(captured["ids"])
    metadata = captured["metadatas"][0]
    assert metadata["status"] == "active"
    assert metadata["doc_id"]
    assert metadata["unit_id"]


def test_publish_without_documents(project, monkeypatch):
    monkeypatch.setattr(publish_module, "_load_encoder", lambda device=None: FakeEncoder())
    with pytest.raises(RuntimeError, match="没有可发布的文档"):
        publish_module.publish_release()


def test_split_text_token_windows(project):
    tokenizer = FakeTokenizer()
    passages = publish_module.split_text("字" * 1000, tokenizer)
    assert len(passages) == 3
    assert passages[0]["start"] == 0
    assert passages[1]["start"] == 448 - 64
    assert passages[-1]["end"] == 1000
    assert all(448 >= len(item["text"]) for item in passages)


def test_split_text_short(project):
    passages = publish_module.split_text("短文本", FakeTokenizer())
    assert passages == [{"text": "短文本", "start": 0, "end": 3}]
