"""混合检索编排：BGE 向量 + BM25 + 条款/文号/章节精确通道 → RRF → 过滤 → 排序 → 证据。

接口见 docs/架构与接口.md 3.11，返回结构见 2.5。检索结果只来自发布目录的
units/passages，不做任何改写，保证每条证据都能在 units.jsonl 中找到。

Stage 2 起本文件只保留编排与缓存，各阶段实现分在：
    channels.py  候选召回（dense / BM25 / 文号 / 条款号 / 章节 / 标题）
    fusion.py    单元级候选记录与加权 RRF
    filters.py   状态、时效、表头、章节导语、相关性门控
    ranking.py   降权排序、跨文档多样性、条数控制、notes 合并
    evidence.py  证据文本、表格挂接、token 预算、版本提示
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Callable

from app import config
from app.domain import versions as versioning
from app.retrieval import channels, fusion, ranking
from app.retrieval import evidence as evidence_builder
from app.retrieval import filters as stage_filters
from app.retrieval.backends import create_backend
from app.retrieval.channels import DENSE_TOP, KEYWORD_TOP
from app.retrieval.evidence import EVIDENCE_MAX_TOKENS
from app.retrieval.fusion import EXACT_CHANNELS, RRF_K
from app.retrieval.lexical import PostingBM25, words
from app.retrieval.query import (
    doc_no_key,
    normalize,
    parse_article,
    parse_query,
)

__all__ = [
    'PolicyRetriever', 'DENSE_TOP', 'KEYWORD_TOP', 'RRF_K',
    'EVIDENCE_MAX_TOKENS', 'EXACT_CHANNELS',
]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


class PolicyRetriever:
    """发布目录上的混合检索器。"""

    def __init__(self, release_dir: Path, backend: str = 'chroma',
                 device: str | None = None,
                 catalog_path: Path | None = None,
                 encoder=None) -> None:
        self.release_dir = Path(release_dir)
        self.backend_name = str(backend or config.VECTOR_BACKEND or 'chroma').lower()
        self.device = str(device or config.resolve_device())
        self.catalog_path = Path(catalog_path) if catalog_path else None
        # 允许测试注入假编码器，避免单测加载真实模型
        self._encoder = encoder
        self.backend_note: str | None = None

        self.manifest = self._load_manifest()
        units_path = self.release_dir / 'units.jsonl'
        if not units_path.is_file():
            raise RuntimeError(f'发布目录缺少 units.jsonl：{units_path}')

        self.units = _read_jsonl(units_path)
        self.passages = _read_jsonl(self.release_dir / 'passages.jsonl')
        self.unit_map: dict[str, dict] = {
            str(unit['id']): unit for unit in self.units if unit.get('id')
        }
        self.passage_map: dict[str, dict] = {
            str(passage['id']): passage for passage in self.passages if passage.get('id')
        }

        # unit -> 按位置排序的 passages，用于长单元取「命中片段 ±1 段」
        self.unit_passages: dict[str, list[dict]] = {}
        for passage in self.passages:
            unit_id = str(passage.get('unit_id') or '')
            if unit_id in self.unit_map:
                self.unit_passages.setdefault(unit_id, []).append(passage)
        for items in self.unit_passages.values():
            items.sort(key=lambda item: (int(item.get('start') or 0), str(item.get('id'))))

        # 文档 -> 单元样本，用于版本/时效判断
        self._doc_units: dict[str, list[dict]] = {}
        for unit in self.units:
            self._doc_units.setdefault(str(unit.get('doc_id') or ''), []).append(unit)
        # 按文档原始顺序排列，便于把紧跟在条款后的表格并进条款证据
        for items in self._doc_units.values():
            items.sort(key=lambda item: (int(item.get('order') or 0), str(item.get('id') or '')))
        self._doc_samples: dict[str, dict] = {
            doc_id: items[0] for doc_id, items in self._doc_units.items() if items
        }

        # BM25 索引建在 passage 文本上
        self._passage_ids = [str(passage.get('id')) for passage in self.passages]
        token_lists = [words(str(passage.get('text') or '')) for passage in self.passages]
        self.bm25 = PostingBM25(self._passage_ids, token_lists)

        self.collection = self._collection_name()
        self._backend = self._open_backend()
        # 同一基准日的现行版本映射可复用，避免每次检索都全量扫描 units
        self._current_cache: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # 初始化辅助
    # ------------------------------------------------------------------
    def _load_manifest(self) -> dict:
        path = self.release_dir / 'manifest.json'
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _collection_name(self) -> str:
        name = self.manifest.get('collection')
        if name:
            return str(name)
        from app.core.release import collection_name
        release_id = str(self.manifest.get('release_id') or self.release_dir.name)
        return collection_name(release_id)

    def _open_backend(self):
        wanted = self.backend_name
        npz_available = (self.release_dir / 'vectors.npz').is_file()
        try:
            backend = create_backend(wanted, self.release_dir, self.collection)
            if wanted == 'chroma' and backend.count() == 0 and npz_available:
                self.backend_name = 'npz'
                self.backend_note = 'Chroma 集合为空，已回退 npz 后端'
                return create_backend('npz', self.release_dir, self.collection)
            return backend
        except Exception as exc:
            if wanted != 'npz' and npz_available:
                self.backend_name = 'npz'
                self.backend_note = f'{wanted} 后端不可用（{exc}），已回退 npz 后端'
                return create_backend('npz', self.release_dir, self.collection)
            raise RuntimeError(f'向量后端初始化失败：{exc}') from exc

    @property
    def encoder(self):
        """真实编码器懒加载：只做 BM25 检索或单测时不需要加载模型。"""
        if self._encoder is None:
            from app.retrieval.encoder import BgeEncoder
            self._encoder = BgeEncoder(device=self.device)
            self.device = self._encoder.device
        return self._encoder

    # ------------------------------------------------------------------
    # 信息
    # ------------------------------------------------------------------
    def info(self) -> dict:
        embedding = self.manifest.get('embedding') or {}
        return {
            'release_id': self.manifest.get('release_id') or self.release_dir.name,
            'release_dir': str(self.release_dir),
            'backend': self.backend_name,
            'backend_note': self.backend_note,
            'collection': self.collection,
            'device': self.device,
            'embedding_model': embedding.get('model') or config.EMBEDDING_DIR.name,
            'dimension': embedding.get('dimension') or getattr(self._backend, 'dimension', None),
            'doc_count': len(self._doc_units),
            'unit_count': len(self.units),
            'passage_count': len(self.passages),
        }

    # ------------------------------------------------------------------
    # 主检索（编排）
    # ------------------------------------------------------------------
    def search(self, question: str, top_k: int = 5,
               filters: dict | None = None,
               on_stage: Callable | None = None) -> dict:
        started = time.perf_counter()
        filters = dict(filters or {})
        top_k = max(1, int(top_k or config.TOP_K or 5))
        as_of = str(filters.get('as_of') or date.today().isoformat())
        department_filter = normalize(str(filters.get('department') or '')) or None
        doc_filter = normalize(str(filters.get('doc_id') or '')) or None

        # 支持服务层传入改写后的问题（多轮补全），检索用它，返回值仍保留原问题
        base = normalize(str(filters.get('rewritten') or filters.get('query') or question or ''))
        parsed = parse_query(base or question or '')
        if filters.get('doc_no'):
            parsed['doc_no'] = normalize(str(filters['doc_no']))
        if filters.get('article'):
            article, article_no = parse_article(str(filters['article']))
            parsed['article'] = article
            parsed['article_no'] = article_no
        rewritten = parsed.get('text') or base or normalize(question)
        notes: list[str] = []
        if self.backend_note:
            notes.append(self.backend_note)

        self._emit(on_stage, 'query', {
            'question': question,
            'rewritten': rewritten,
            'filters': {'department': department_filter, 'as_of': as_of},
            'doc_no': parsed.get('doc_no'),
            'article': parsed.get('article'),
            'is_followup': parsed.get('is_followup'),
        })

        # ---- 通道 1~2：BGE 向量 / BM25 ----
        dense_hits, dense_note = channels.dense_candidates(
            self.encoder, self._backend, rewritten, doc_filter,
        )
        if dense_note:
            notes.append(dense_note)
        dense_ids = [pid for pid, _ in dense_hits]
        self._emit(on_stage, 'vectors', {
            'passage_count': len(dense_hits),
            'titles': evidence_builder.labels_for_passages(
                dense_ids, self.passage_map, self.unit_map, 5),
        })

        keyword_hits, keyword_note = channels.keyword_candidates(
            self.bm25, self._passage_ids, rewritten,
        )
        if keyword_note:
            notes.append(keyword_note)
        self._emit(on_stage, 'keywords', {
            'passage_count': len(keyword_hits),
            'titles': evidence_builder.labels_for_passages(
                [pid for pid, _ in keyword_hits], self.passage_map, self.unit_map, 5),
        })

        # ---- 通道 3~6：条款号 / 文号 / 章节 / 标题 ----
        exact_channels = channels.exact_channels(self.units, self.unit_map, parsed)

        # ---- 相关性闸门：问题与知识库完全无关时，不要拿低分结果硬凑证据 ----
        min_dense = float(getattr(config, 'RETRIEVAL_MIN_DENSE', 0.45) or 0.0)
        gate = stage_filters.relevance_gate(dense_hits, exact_channels, min_dense)
        if gate.blocked:
            notes = ranking.dedupe(notes + [stage_filters.GATE_REFUSAL_NOTE])
            self._emit(on_stage, 'evidence', {'titles': [], 'notes': notes})
            return {
                'question': question,
                'rewritten': rewritten,
                'filters': {'department': department_filter, 'as_of': as_of},
                'results': [],
                'context': '',
                'tokens': 0,
                'notes': notes,
                'seconds': round(time.perf_counter() - started, 3),
            }

        # ---- 汇总候选并做 RRF 融合 ----
        records: dict[str, dict] = {}
        dense_unit_ids = fusion.add_dense_candidates(
            records, dense_hits, self.passage_map, self.unit_map)
        keyword_unit_ids = fusion.add_keyword_candidates(
            records, keyword_hits, self.passage_map, self.unit_map)
        channel_lists: dict[str, list[str]] = {
            'BGE': dense_unit_ids,
            'BM25': keyword_unit_ids,
        }
        channel_lists.update(exact_channels)
        fusion.fuse(records, channel_lists, self.unit_map, self.unit_passages)

        # ---- 时效 / 版本 / 硬过滤 ----
        current_docs = self._current_docs(as_of)
        explicit_doc_key = doc_no_key(parsed.get('doc_no') or '') if parsed.get('doc_no') else None
        filtered = stage_filters.apply_hard_filters(
            records, self.unit_map, current_docs, as_of,
            doc_filter=doc_filter,
            department_filter=department_filter,
            explicit_doc_key=explicit_doc_key,
        )
        usable = filtered.usable
        stale_ids = filtered.stale_ids
        stale_candidates = filtered.stale_candidates

        # ---- 章节导语去重与排序 ----
        heading_flags = stage_filters.heading_flags(usable, self.unit_map)
        kept = stage_filters.dedupe_heading_units(usable, heading_flags, self.unit_map)
        ordered = ranking.order_units(kept, records, stale_ids, heading_flags)

        # 跨制度问题：前几名全来自同一份文档时，用最后一个名额换入另一份文档
        selection = ranking.diversify_documents(ordered, top_k, self.unit_map, records)
        # 条款证据会带上紧邻的表格行；这些行不再单独占一个证据位
        selection = ranking.fill_with_attached_rows(
            selection, ordered, top_k, self.unit_map,
            lambda unit: evidence_builder.adjacent_table_rows(unit, self._doc_units),
        )

        # ---- 组装证据与 token 预算 ----
        budget = int(getattr(config, 'MAX_CONTEXT_TOKENS', 4096) or 4096)
        tokenizer = getattr(self._encoder, 'tokenizer', None) if self._encoder else None
        results: list[dict] = []
        used_tokens = 0
        version_notes: list[str] = []
        for unit_id in selection:
            if len(results) >= top_k:
                break
            record = records[unit_id]
            unit = self.unit_map[unit_id]
            # 单条结果也要过相关性：向量分太低、关键词也不强、又不是精确命中时，
            # 不放进证据，避免回答里出现「奖学金办法」这类无关引用。
            if not stage_filters.passes_result_gate(record, min_dense, gate.max_dense):
                continue
            evidence = evidence_builder.build_evidence(
                unit_id, unit, record,
                doc_units=self._doc_units,
                unit_passages=self.unit_passages,
                tokenizer=tokenizer,
            )
            cost = evidence_builder.count_tokens(evidence, tokenizer)
            if used_tokens + cost > budget:
                notes.append(ranking.note_over_budget(str(unit.get('doc_title') or ''), budget))
                continue
            used_tokens += cost
            if unit_id in stale_ids:
                note = evidence_builder.version_note(unit, current_docs, self._doc_samples)
                if note:
                    version_notes.append(note)
            results.append({
                'rank': len(results) + 1,
                'unit': dict(unit),
                'evidence': evidence,
                'citation': evidence_builder.citation(unit),
                'channels': [name for name in fusion.CHANNEL_ORDER if name in record['channels']],
                'scores': {
                    'dense': round(float(record['dense']), 4),
                    'bm25': round(float(record['bm25']), 3),
                    'rrf': round(float(record['rrf']), 6),
                },
            })

        # 用户问到的主题若存在更新版本，即便旧版没被选进证据也要提示；
        # 只有「问题确实在问版本/有效性」且旧版候选与最佳证据得分接近时才提示。
        best_selected = ranking.best_selected_score(results, records)
        if evidence_builder.wants_version_hint(question, rewritten) or explicit_doc_key:
            for unit_id, score in sorted(stale_candidates, key=lambda pair: -pair[1]):
                if not ranking.version_note_score_ok(score, best_selected):
                    continue
                note = evidence_builder.version_note(
                    self.unit_map[unit_id], current_docs, self._doc_samples)
                if note:
                    version_notes.append(note)

        # 版本提示放在预算提示之前，更符合阅读顺序
        notes = ranking.merge_notes(version_notes, notes)
        context = '\n\n'.join(
            f'[证据{item["rank"]}] {item["evidence"]}' for item in results
        )
        seconds = round(time.perf_counter() - started, 3)
        self._emit(on_stage, 'evidence', {
            'titles': [evidence_builder.unit_label(item['unit']) for item in results],
            'notes': notes,
        })

        return {
            'question': question,
            'rewritten': rewritten,
            'filters': {'department': department_filter, 'as_of': as_of},
            'results': results,
            'context': context,
            'tokens': used_tokens,
            'notes': notes,
            'seconds': seconds,
        }

    # ------------------------------------------------------------------
    # 时效与版本（规则在 app/domain/versions.py，这里只做适配与缓存）
    # ------------------------------------------------------------------
    def _current_docs(self, as_of: str) -> dict[str, str]:
        cached = self._current_cache.get(as_of)
        if cached is None:
            cached = versioning.current_documents(self.units, as_of)
            self._current_cache[as_of] = cached
        return cached

    # ------------------------------------------------------------------
    # 事件推送
    # ------------------------------------------------------------------
    @staticmethod
    def _emit(on_stage: Callable | None, stage: str, payload: dict) -> None:
        if not on_stage:
            return
        try:
            on_stage(stage, payload)
        except Exception:
            # 推送失败不应影响检索本身
            pass
