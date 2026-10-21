"""发布：汇总知识单元 → 检索片段 → 向量 → release 目录 → Chroma 集合 → 激活指针。

契约见 docs/架构与接口.md 2.3 / 2.4 / 3.6。
- 片段按 448 token 切、64 token 重叠，token 由 BGE 分词器计算；
- 每份文档的向量缓存到 data/docs/<doc_id>/vectors.npz，sha/单元/编码参数不变则复用；
- 新建 Chroma 集合（config.VECTOR_BACKEND == "chroma" 时）后原子写 active_release。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from app import config
from app.store import releases as release_module
from app.ingest.common import (
    now_iso,
    read_json,
    read_jsonl,
    relative_path,
    sha256_file,
    write_json,
    write_jsonl,
)
from app.store.catalog import Catalog

PASSAGE_TOKENS = 448
PASSAGE_OVERLAP = 64
CACHE_SCHEMA = 1
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
EMBEDDING_TEXT_VERSION = 1


# ---------------------------------------------------------------- 编码器与切分
def _load_encoder(device: str | None = None):
    """按契约 3.7 创建 BGE 编码器；测试可 monkeypatch 本函数。"""
    from app.retrieval.encoder import BgeEncoder

    return BgeEncoder(model_dir=config.EMBEDDING_DIR, device=device)


def _embedding_model_name() -> str:
    name = config.EMBEDDING_DIR.name
    if name == "bge-base-zh-v1.5":
        return "BAAI/bge-base-zh-v1.5"
    return name


def _token_offsets(tokenizer, text: str) -> list[tuple[int, tuple[int, int]]]:
    """返回 [(token_id, (char_start, char_end))]；兼容 HF 与测试桩。"""
    try:
        encoded = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        ids = list(encoded["input_ids"])
        offsets = list(encoded["offset_mapping"])
        pairs = [
            (int(token), (int(span[0]), int(span[1])))
            for token, span in zip(ids, offsets)
            if span and int(span[1]) > int(span[0])
        ]
        if pairs:
            return pairs
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    # 退化方案：按字符切（真实 BGE 分词器不会走到这里）
    return [
        (index, (index, index + 1))
        for index, char in enumerate(text)
        if not char.isspace()
    ]


def split_text(text: str, tokenizer) -> list[dict]:
    """把一段原文切成 448/64 的片段，返回 [{text, start, end}]。"""
    text = text or ""
    if not text.strip():
        return []
    pairs = _token_offsets(tokenizer, text)
    if not pairs:
        return []
    total = len(pairs)
    if total <= PASSAGE_TOKENS:
        windows = [(0, len(text))]
    else:
        windows = []
        start = 0
        step = PASSAGE_TOKENS - PASSAGE_OVERLAP
        while start < total:
            end = min(start + PASSAGE_TOKENS, total)
            windows.append((pairs[start][1][0], pairs[end - 1][1][1]))
            if end >= total:
                break
            start += step
    passages: list[dict] = []
    for char_start, char_end in windows:
        raw = text[char_start:char_end]
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        passages.append({
            "text": stripped,
            "start": char_start + leading,
            "end": char_end - trailing,
        })
    return passages


def _embedding_text(unit: dict, text: str) -> str:
    title = unit.get("doc_title") or ""
    article = unit.get("article") or ""
    prefix = f"《{title}》" if title else ""
    if article:
        prefix += f"{article} "
    return f"{prefix}{text}".strip()


def embedding_text(unit: dict, text: str) -> str:
    """公开别名：片段的编码文本（``stages.chunk`` 与发布共用同一实现）。"""
    return _embedding_text(unit, text)


# ---------------------------------------------------------------- 向量
def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return np.ascontiguousarray(vectors / norms, dtype=np.float32)


def _atomic_savez(path: Path, ids: np.ndarray, vectors: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        np.savez(handle, ids=ids, vectors=vectors)
    os.replace(temporary, path)


def _cache_params(doc: dict, units_path: Path) -> dict:
    return {
        "schema": CACHE_SCHEMA,
        "doc_sha": doc.get("sha256") or "",
        "units_sha": sha256_file(units_path) if units_path.is_file() else "",
        "model": str(config.EMBEDDING_DIR.resolve()),
        "model_name": _embedding_model_name(),
        "chunk_tokens": PASSAGE_TOKENS,
        "overlap_tokens": PASSAGE_OVERLAP,
        "embedding_text_version": EMBEDDING_TEXT_VERSION,
    }


def _read_cached_vectors(doc_dir: Path, params: dict, passage_count: int):
    cache_npz = doc_dir / "vectors.npz"
    cache_meta_path = doc_dir / "vectors.meta.json"
    if not cache_npz.is_file():
        return None
    meta = read_json(cache_meta_path, default={}) or {}
    if meta.get("params") != params:
        return None
    if int(meta.get("passage_count") or -1) != passage_count:
        return None
    try:
        with np.load(cache_npz) as data:
            vectors = np.asarray(data["vectors"], dtype=np.float32)
    except Exception:
        return None
    if vectors.ndim != 2 or vectors.shape[0] != passage_count:
        return None
    return _l2_normalize(vectors)


def _document_vectors(doc: dict, passages: list[dict], encoder, force_full: bool) -> np.ndarray:
    """计算或复用单文档向量；返回 L2 归一化的 float32 矩阵。"""
    doc_dir = config.DOCS_DIR / doc["doc_id"]
    units_path = doc_dir / "units.jsonl"
    params = _cache_params(doc, units_path)
    if not force_full:
        cached = _read_cached_vectors(doc_dir, params, len(passages))
        if cached is not None:
            return cached

    texts = [passage["embedding_text"] for passage in passages]
    try:
        vectors = encoder.encode(texts, query=False, batch_size=32)
    except Exception as exc:
        raise RuntimeError(f"向量编码失败（{doc.get('title') or doc['doc_id']}）：{exc}") from exc
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    if vectors.ndim != 2 or vectors.shape[0] != len(passages):
        raise RuntimeError(
            f"编码器返回的向量形状异常：期望 {len(passages)} 行，实际 {getattr(vectors, 'shape', None)}"
        )
    vectors = _l2_normalize(vectors)

    ids = np.array(
        [f"{doc['doc_id']}#p{index:05d}" for index in range(len(passages))], dtype=str
    )
    _atomic_savez(doc_dir / "vectors.npz", ids, vectors)
    write_json(doc_dir / "vectors.meta.json", {
        "params": params,
        "passage_count": len(passages),
        "dimension": int(vectors.shape[1]),
        "updated_at": now_iso(),
    })
    return vectors


# ---------------------------------------------------------------- 发布
def _unique_release_id() -> str:
    base = release_module.new_release_id()
    candidate = base
    index = 2
    while (config.RELEASES_DIR / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _manifest_docs(docs: list[dict]) -> list[dict]:
    return [
        {
            "doc_id": doc["doc_id"],
            "title": doc.get("title"),
            "doc_no": doc.get("doc_no"),
            "version_label": doc.get("version_label"),
            "department": doc.get("department"),
            "status": doc.get("status"),
            "effective_from": doc.get("effective_from"),
            "effective_to": doc.get("effective_to"),
        }
        for doc in docs
    ]


def _unit_metadata(unit: dict) -> dict:
    def value(key: str) -> str:
        item = unit.get(key)
        return "" if item is None else str(item)

    pages = unit.get("pages") or []
    return {
        "unit_id": value("id"),
        "doc_id": value("doc_id"),
        "title": value("doc_title"),
        "doc_no": value("doc_no"),
        "version_label": value("version_label"),
        "article": value("article"),
        "chapter": value("chapter"),
        "department": value("department"),
        "status": value("status"),
        "effective_from": value("effective_from"),
        "effective_to": value("effective_to"),
        "kind": value("kind"),
        "page": int(pages[0]) if pages else 0,
    }


def publish_release(device: str | None = None, force_full: bool = False) -> dict:
    """汇总所有 active/superseded 文档，生成新 release 并切换激活指针。"""
    config.ensure_dirs()
    catalog = Catalog()
    try:
        documents = [
            doc for doc in catalog.list_documents()
            if (doc.get("status") or "active") in ("active", "superseded")
        ]
        if not documents:
            raise RuntimeError("没有可发布的文档，请先入库制度资料。")
        documents.sort(key=lambda item: item["doc_id"])

        encoder = _load_encoder(device)
        tokenizer = getattr(encoder, "tokenizer", None)
        if tokenizer is None:
            raise RuntimeError("编码器缺少 tokenizer，无法按 token 切分检索片段。")

        release_id = _unique_release_id()
        release_dir = config.RELEASES_DIR / release_id

        all_units: list[dict] = []
        all_passages: list[dict] = []
        doc_vector_blocks: list[np.ndarray] = []
        used_docs: list[dict] = []
        dimension: int | None = None

        for doc in documents:
            doc_id = doc["doc_id"]
            units_path = config.DOCS_DIR / doc_id / "units.jsonl"
            units = read_jsonl(units_path)
            if not units:
                continue
            # 以 catalog 的当前状态为准，修正 units.jsonl 里可能过期的状态
            for unit in units:
                unit["status"] = doc.get("status") or "active"

            local_passages: list[dict] = []
            for unit in units:
                for chunk in split_text(unit.get("text") or "", tokenizer):
                    local_passages.append({
                        "unit_id": unit.get("id"),
                        "text": chunk["text"],
                        "embedding_text": embedding_text(unit, chunk["text"]),
                        "start": chunk["start"],
                        "end": chunk["end"],
                    })
            if not local_passages:
                continue

            vectors = _document_vectors(doc, local_passages, encoder, force_full)
            if vectors.shape[1] != (dimension if dimension is not None else vectors.shape[1]):
                raise RuntimeError(
                    f"向量维度不一致：已有 {dimension} 维，文档 {doc_id} 为 {vectors.shape[1]} 维。"
                )
            dimension = int(vectors.shape[1])

            all_units.extend(units)
            for passage in local_passages:
                passage["id"] = f"p_{len(all_passages) + 1:06d}"
                all_passages.append(passage)
            doc_vector_blocks.append(vectors)
            used_docs.append(doc)

        if not used_docs or not all_passages:
            raise RuntimeError("没有可发布的知识单元，请检查文档是否解析成功。")

        vectors_all = _l2_normalize(np.vstack(doc_vector_blocks))
        passage_ids = np.array([passage["id"] for passage in all_passages], dtype=str)
        collection = release_module.collection_name(release_id)
        published_at = now_iso()

        release_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(release_dir / "units.jsonl", all_units)
        write_jsonl(release_dir / "passages.jsonl", all_passages)
        _atomic_savez(release_dir / "vectors.npz", passage_ids, vectors_all)

        departments = sorted({
            doc["department"] for doc in used_docs if doc.get("department")
        })
        manifest = {
            "schema": 1,
            "release_id": release_id,
            "published_at": published_at,
            "doc_count": len(used_docs),
            "unit_count": len(all_units),
            "passage_count": len(all_passages),
            "embedding": {
                "model": _embedding_model_name(),
                "dimension": int(vectors_all.shape[1]),
                "query_prefix": QUERY_PREFIX,
            },
            "docs": _manifest_docs(used_docs),
            "departments": departments,
            "collection": collection,
        }
        write_json(release_dir / "manifest.json", manifest)

        # Chroma 集合：新建集合写入向量；旧集合清理暂不实现（契约允许留空）
        if config.VECTOR_BACKEND == "chroma":
            unit_map = {unit.get("id"): unit for unit in all_units}
            try:
                from app.retrieval.backends import create_backend

                backend = create_backend("chroma", release_dir, collection)
                metadatas = [
                    _unit_metadata(unit_map.get(passage["unit_id"]) or {})
                    for passage in all_passages
                ]
                backend.add(passage_ids.tolist(), vectors_all, metadatas)
            except Exception as exc:
                raise RuntimeError(f"发布失败：写入向量集合 {collection} 出错：{exc}") from exc

        record = {
            "schema": 1,
            "id": release_id,
            "dir": relative_path(release_dir),
            "collection": collection,
            "published_at": published_at,
            "doc_count": len(used_docs),
            "unit_count": len(all_units),
            "passage_count": len(all_passages),
        }
        release_module.write_active(record)
        catalog.record_release(record)
        catalog.record_audit("publish", detail={
            "release_id": release_id,
            "doc_count": len(used_docs),
            "unit_count": len(all_units),
            "passage_count": len(all_passages),
        })
        return record
    finally:
        catalog.close()
