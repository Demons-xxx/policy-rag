"""混合检索：BGE 向量 + BM25 + 条款/文号/章节精确通道 → RRF → 时效过滤 → 证据组装。

接口见 docs/架构与接口.md 3.11，返回结构见 2.5。检索结果只来自发布目录的
units/passages，不做任何改写，保证每条证据都能在 units.jsonl 中找到。
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np

from app import config
from app.retrieval.backends import create_backend
from app.retrieval.lexical import PostingBM25, words
from app.retrieval.query import (
    article_key,
    chinese_to_int,
    doc_no_key,
    expand_terms,
    normalize,
    parse_article,
    parse_query,
)

# 各路召回条数
DENSE_TOP = 50
KEYWORD_TOP = 50
# RRF 平滑常数
RRF_K = 60
# 单个证据单元超过该 token 数时只取命中片段 ±1 段
EVIDENCE_MAX_TOKENS = 1200

# RRF 通道权重：精确命中优先，保证「第十二条」这类问题稳定置顶
_CHANNEL_WEIGHTS = {
    'BGE': 1.0,
    'BM25': 1.0,
    '条款号': 2.0,
    '文号': 1.5,
    '章节': 1.2,
    '标题': 0.8,
}
_CHANNEL_ORDER = ['BGE', 'BM25', '文号', '条款号', '章节', '标题']

# 标题尾部版本标记，如「（2025年修订）」「（2023年版）」；用于把新旧版本归到同一主题
_VERSION_SUFFIX_RE = re.compile(r'[（(][^（）()]{0,20}?(?:修订|版|年)[^（）()]{0,20}?[）)]$')


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


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _label_parts(label: str) -> tuple[str, int | None] | None:
    """解析「第三章 / 第3节」→ (类别, 序号)。"""
    match = re.search(r'第\s*([0-9零〇一二两三四五六七八九十百千]+)\s*([章节])', label or '')
    if not match:
        return None
    return match.group(2), chinese_to_int(match.group(1))


def _same_label(left: str, right: str) -> bool:
    """章节标题比较：原文包含或「章/节 + 序号」相同都算命中。"""
    if not left or not right:
        return False
    if right in left:
        return True
    left_parts = _label_parts(left)
    right_parts = _label_parts(right)
    return bool(left_parts and right_parts and left_parts == right_parts)


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
    # 主检索
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

        # ---- 通道 1：BGE 向量 ----
        dense_hits: list[tuple[str, float]] = []
        try:
            matrix = self.encoder.encode([rewritten], query=True)
            if matrix is not None and len(matrix):
                dense_hits = self._backend.search(
                    np.asarray(matrix, dtype=np.float32)[0],
                    DENSE_TOP,
                    where=self._where(doc_filter),
                )
        except Exception as exc:
            notes.append(f'向量检索不可用（{type(exc).__name__}: {exc}），已回退关键词检索')
        dense_ids = [pid for pid, _ in dense_hits]
        self._emit(on_stage, 'vectors', {
            'passage_count': len(dense_hits),
            'titles': self._labels_for_passages(dense_ids, 5),
        })

        # ---- 通道 2：BM25 ----
        keyword_hits: list[tuple[str, float]] = []
        tokens = expand_terms(words(rewritten))
        try:
            scores = self.bm25.get_scores(tokens)
            if len(scores):
                limit = min(KEYWORD_TOP, len(scores))
                order = np.argsort(-scores, kind='stable')[:limit]
                for index in order:
                    value = float(scores[int(index)])
                    if value <= 0.0:
                        continue
                    keyword_hits.append((self._passage_ids[int(index)], value))
        except Exception as exc:  # pragma: no cover - 理论不可达
            notes.append(f'关键词检索失败（{type(exc).__name__}: {exc}）')
        self._emit(on_stage, 'keywords', {
            'passage_count': len(keyword_hits),
            'titles': self._labels_for_passages([pid for pid, _ in keyword_hits], 5),
        })

        # ---- 通道 3~6：条款号 / 文号 / 章节 / 标题 ----
        exact_channels = self._exact_channels(parsed)

        # ---- 汇总候选并做 RRF 融合 ----
        records: dict[str, dict] = {}

        def touch(unit_id: str) -> dict:
            record = records.get(unit_id)
            if record is None:
                record = {
                    'channels': set(),
                    'dense': 0.0,
                    'bm25': 0.0,
                    'rrf': 0.0,
                    'passage_id': None,
                    'bm25_passage_id': None,
                }
                records[unit_id] = record
            return record

        dense_unit_ids: list[str] = []
        for passage_id, score in dense_hits:
            passage = self.passage_map.get(passage_id)
            if not passage:
                continue
            unit_id = str(passage.get('unit_id') or '')
            if unit_id not in self.unit_map:
                continue
            record = touch(unit_id)
            record['channels'].add('BGE')
            if float(score) > record['dense'] or record['passage_id'] is None:
                record['dense'] = float(score)
                record['passage_id'] = passage_id
            if unit_id not in dense_unit_ids:
                dense_unit_ids.append(unit_id)

        keyword_unit_ids: list[str] = []
        for passage_id, score in keyword_hits:
            passage = self.passage_map.get(passage_id)
            if not passage:
                continue
            unit_id = str(passage.get('unit_id') or '')
            if unit_id not in self.unit_map:
                continue
            record = touch(unit_id)
            record['channels'].add('BM25')
            if float(score) > record['bm25'] or record['bm25_passage_id'] is None:
                record['bm25'] = float(score)
                record['bm25_passage_id'] = passage_id
            if record['passage_id'] is None:
                record['passage_id'] = passage_id
            if unit_id not in keyword_unit_ids:
                keyword_unit_ids.append(unit_id)

        channel_lists: dict[str, list[str]] = {
            'BGE': dense_unit_ids,
            'BM25': keyword_unit_ids,
        }
        channel_lists.update(exact_channels)
        for channel, unit_ids in channel_lists.items():
            weight = _CHANNEL_WEIGHTS.get(channel, 1.0)
            seen: set[str] = set()
            rank = 0
            for unit_id in unit_ids:
                if unit_id in seen:
                    continue
                seen.add(unit_id)
                rank += 1
                unit = self.unit_map.get(unit_id)
                if unit is None:
                    continue
                record = touch(unit_id)
                record['channels'].add(channel)
                if record['passage_id'] is None:
                    passages = self.unit_passages.get(unit_id) or []
                    if passages:
                        record['passage_id'] = str(passages[0].get('id'))
                record['rrf'] += weight / (RRF_K + rank)

        # ---- 时效 / 版本 / 硬过滤 ----
        current_docs = self._current_docs(as_of)
        stale_ids: set[str] = set()
        usable: list[str] = []
        for unit_id in records:
            unit = self.unit_map[unit_id]
            status = str(unit.get('status') or 'active').lower()
            if status == 'repealed':
                continue
            if self._table_header_only(unit):
                # 表头行没有数据，命中它只会让模型看到列名而看不到内容
                continue
            if doc_filter and str(unit.get('doc_id')) != doc_filter:
                continue
            if department_filter:
                unit_dept = normalize(str(unit.get('department') or ''))
                if department_filter not in unit_dept:
                    continue
            if not self._effective_from_ok(unit, as_of):
                continue
            title_key = self._title_key(unit)
            current_doc = current_docs.get(title_key)
            doc_id = str(unit.get('doc_id'))
            if (current_doc and doc_id != current_doc) or self._expired(unit, as_of):
                stale_ids.add(unit_id)
            usable.append(unit_id)

        # 未降权前的靠前候选：用来判断「用户确实问到了旧版本内容」，即使旧版
        # 最终没有进入证据，也要在 notes 里提示存在更新版本。
        window = max(top_k * 3, 10)
        raw_top = {
            unit_id
            for unit_id in sorted(usable, key=lambda uid: (-records[uid]['rrf'], uid))[:window]
        }

        # ---- 章节导语去重与排序 ----
        heading_flags = {
            unit_id: self._heading_only(self.unit_map[unit_id]) for unit_id in usable
        }
        substantive_sections: set[tuple[str, str]] = set()
        for unit_id in usable:
            if heading_flags[unit_id]:
                continue
            unit = self.unit_map[unit_id]
            substantive_sections.add(
                (str(unit.get('doc_id')), normalize(str(unit.get('chapter') or '')))
            )
        kept: list[str] = []
        for unit_id in usable:
            unit = self.unit_map[unit_id]
            section_key = (
                str(unit.get('doc_id')),
                normalize(str(unit.get('chapter') or '')),
            )
            # 同一章节已有正文单元时，章节导语不重复占位
            if heading_flags[unit_id] and section_key in substantive_sections:
                continue
            kept.append(unit_id)

        def order_key(unit_id: str):
            record = records[unit_id]
            score = float(record['rrf'])
            # 同标题的旧版本降权但不驱逐：正常提问由现行版胜出，用户明确点名
            # 旧文号时旧版仍可能排前（同时会在 notes 提示）
            if unit_id in stale_ids:
                score *= 0.7
            # 纯章节导语保留但压低权重，避免抢占正文位置
            if heading_flags.get(unit_id):
                score *= 0.8
            return (
                -score,
                1 if unit_id in stale_ids else 0,
                1 if heading_flags.get(unit_id) else 0,
                -float(record['dense']),
                -float(record['bm25']),
                unit_id,
            )

        ordered = sorted(kept, key=order_key)

        # 跨制度问题（如「科研经费买危化品」，同时涉及报销办法和安全办法）如果
        # 前几名全来自同一份文档，用最后一个名额换入另一份文档的最优候选，
        # 避免只答一半。单文档问题通常不受影响（另一份文档得分需达到一半）。
        selection = list(ordered[:top_k])
        if len(selection) >= 3:
            selected_docs = {str(self.unit_map[uid].get('doc_id')) for uid in selection}
            if len(selected_docs) == 1:
                best_score = float(records[selection[0]]['rrf'])
                for uid in ordered[top_k:]:
                    if str(self.unit_map[uid].get('doc_id')) in selected_docs:
                        continue
                    if float(records[uid]['rrf']) >= best_score * 0.5:
                        selection[-1] = uid
                        break

        # 条款证据会带上紧邻的表格行；这些行不再单独占一个证据位
        chosen: list[str] = []
        attached_rows: set[str] = set()
        for uid in list(selection) + [item for item in ordered if item not in selection]:
            if len(chosen) >= top_k:
                break
            if uid in attached_rows:
                continue
            chosen.append(uid)
            unit = self.unit_map[uid]
            if str(unit.get('kind') or '') == 'article':
                for row in self._adjacent_table_rows(unit):
                    attached_rows.add(str(row.get('id')))
        selection = chosen

        # ---- 组装证据与 token 预算 ----
        budget = int(getattr(config, 'MAX_CONTEXT_TOKENS', 4096) or 4096)
        results: list[dict] = []
        used_tokens = 0
        version_notes: list[str] = []
        for unit_id in selection:
            if len(results) >= top_k:
                break
            record = records[unit_id]
            unit = self.unit_map[unit_id]
            evidence = self._build_evidence(unit_id, record)
            cost = self._count_tokens(evidence)
            if used_tokens + cost > budget:
                title = str(unit.get('doc_title') or '')
                notes.append(f'《{title}》证据超出上下文预算（{budget} tokens），已省略')
                continue
            used_tokens += cost
            if unit_id in stale_ids:
                note = self._version_note(unit, current_docs)
                if note:
                    version_notes.append(note)
            results.append({
                'rank': len(results) + 1,
                'unit': dict(unit),
                'evidence': evidence,
                'citation': self._citation(unit),
                'channels': [name for name in _CHANNEL_ORDER if name in record['channels']],
                'scores': {
                    'dense': round(float(record['dense']), 4),
                    'bm25': round(float(record['bm25']), 3),
                    'rrf': round(float(record['rrf']), 6),
                },
            })

        # 用户问到的主题若存在更新版本，即便旧版没被选进证据也要提示
        for unit_id in sorted(raw_top):
            if unit_id in stale_ids:
                note = self._version_note(self.unit_map[unit_id], current_docs)
                if note:
                    version_notes.append(note)

        # 版本提示放在预算提示之前，更符合阅读顺序
        notes = _dedupe(version_notes) + _dedupe(notes)
        context = '\n\n'.join(
            f'[证据{item["rank"]}] {item["evidence"]}' for item in results
        )
        seconds = round(time.perf_counter() - started, 3)
        self._emit(on_stage, 'evidence', {
            'titles': [self._unit_label(item['unit']) for item in results],
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
    # 通道实现
    # ------------------------------------------------------------------
    def _where(self, doc_filter: str | None) -> dict | None:
        if not doc_filter:
            return None
        return {'doc_id': {'$in': [doc_filter]}}

    def _exact_channels(self, parsed: dict) -> dict[str, list[str]]:
        """条款号 / 文号 / 章节 / 标题 四路精确或半精确召回。"""
        channels: dict[str, list[str]] = {}
        doc_no = parsed.get('doc_no')
        article_no = parsed.get('article_no')
        article_text = parsed.get('article')
        chapter = parsed.get('chapter')
        section = parsed.get('section')

        doc_key = doc_no_key(doc_no) if doc_no else None

        def unit_doc_key(unit: dict) -> str:
            return doc_no_key(unit.get('doc_no') or '')

        def order_of(unit: dict) -> int:
            try:
                return int(unit.get('order') or 0)
            except (TypeError, ValueError):
                return 0

        # 文号：该文号下所有单元按原文顺序
        if doc_key:
            matched = [
                str(unit['id']) for unit in self.units
                if unit.get('id') and unit_doc_key(unit) == doc_key
            ]
            matched.sort(key=lambda uid: order_of(self.unit_map[uid]))
            if matched:
                channels['文号'] = matched[:200]

        # 条款号：优先 article_no，其次条款文本
        if article_no is not None or article_text:
            matched = []
            for unit in self.units:
                uid = str(unit.get('id') or '')
                if not uid:
                    continue
                if doc_key and unit_doc_key(unit) != doc_key:
                    continue
                same = False
                unit_no = unit.get('article_no')
                if article_no is not None and unit_no is not None:
                    try:
                        same = int(unit_no) == int(article_no)
                    except (TypeError, ValueError):
                        same = False
                if not same and article_text:
                    same = article_key(unit.get('article') or '') == article_key(article_text)
                if same:
                    matched.append(uid)
            matched.sort(key=lambda uid: order_of(self.unit_map[uid]))
            if matched:
                channels['条款号'] = matched[:50]

        # 章节：单元 chapter/section/heading_path 包含「第X章」「第X节」
        if chapter or section:
            matched = []
            for unit in self.units:
                uid = str(unit.get('id') or '')
                if not uid:
                    continue
                texts = [
                    normalize(str(unit.get('chapter') or '')),
                    normalize(str(unit.get('section') or '')),
                ] + [normalize(str(item)) for item in (unit.get('heading_path') or [])]
                hit = False
                if chapter and any(_same_label(text, chapter) for text in texts if text):
                    hit = True
                if section and any(_same_label(text, section) for text in texts if text):
                    hit = True
                if hit:
                    matched.append(uid)
            matched.sort(key=lambda uid: order_of(self.unit_map[uid]))
            if matched:
                channels['章节'] = matched[:50]

        # 标题：问题里的长词命中文档标题（同一文档的单元按原文顺序）
        keywords = [token for token in (parsed.get('keywords') or []) if len(token) >= 2]
        if keywords:
            scored: list[tuple[int, int, str]] = []
            for unit in self.units:
                uid = str(unit.get('id') or '')
                if not uid:
                    continue
                title = normalize(str(unit.get('doc_title') or ''))
                hits = sum(1 for token in keywords if token in title)
                if hits:
                    scored.append((-hits, order_of(unit), uid))
            scored.sort()
            if scored:
                channels['标题'] = [item[2] for item in scored][:50]
        return channels

    # ------------------------------------------------------------------
    # 时效与版本
    # ------------------------------------------------------------------
    @staticmethod
    def _effective_from_ok(unit: dict, as_of: str) -> bool:
        """生效日期晚于 as_of 的单元不参与检索（尚未施行）。"""
        start = str(unit.get('effective_from') or '')
        return not start or start <= as_of

    @staticmethod
    def _in_window(unit: dict, as_of: str) -> bool:
        start = str(unit.get('effective_from') or '')
        end = str(unit.get('effective_to') or '')
        if start and start > as_of:
            return False
        if end and end < as_of:
            return False
        return True

    @staticmethod
    def _expired(unit: dict, as_of: str) -> bool:
        """effective_to 早于基准日：该版本已失效，只能降权保留并提示。"""
        end = str(unit.get('effective_to') or '')
        return bool(end) and end < as_of

    def _current_docs(self, as_of: str) -> dict[str, str]:
        """同标题下选出 as_of 时点的现行文档：优先 active 且在有效期内，再比生效日期。"""
        cached = self._current_cache.get(as_of)
        if cached is not None:
            return cached
        by_title: dict[str, dict[str, dict]] = {}
        for unit in self.units:
            if str(unit.get('status') or '').lower() == 'repealed':
                continue
            title = self._title_key(unit)
            if not title:
                continue
            doc_id = str(unit.get('doc_id') or '')
            if not doc_id:
                continue
            by_title.setdefault(title, {}).setdefault(doc_id, unit)

        current: dict[str, str] = {}
        for title, docs in by_title.items():
            ranked: list[tuple[int, str, str]] = []
            for doc_id, unit in docs.items():
                status = str(unit.get('status') or 'active').lower()
                start = str(unit.get('effective_from') or '')
                if start and start > as_of:
                    continue
                active_now = 1 if (status == 'active' and self._in_window(unit, as_of)) else 0
                ranked.append((active_now, start, doc_id))
            if not ranked:
                # 全部已过期时仍指认最新生效版本，避免把旧版误判成「被替代」
                for doc_id, unit in docs.items():
                    ranked.append((0, str(unit.get('effective_from') or ''), doc_id))
            ranked.sort()
            current[title] = ranked[-1][2]
        self._current_cache[as_of] = current
        return current

    def _version_note(self, unit: dict, current_docs: dict[str, str]) -> str | None:
        title_key = self._title_key(unit)
        current_doc = current_docs.get(title_key)
        title = str(unit.get('doc_title') or '').strip()
        label = str(unit.get('version_label') or '').strip()
        version = label or '旧版'
        if not current_doc or current_doc == str(unit.get('doc_id')):
            # 没有识别到同主题现行版本，只能按失效日期给出通用提示
            end = str(unit.get('effective_to') or '').strip()
            if end:
                return f'《{title}》{version}已于{end}废止，内容仅作参考'
            return None
        sample = self._doc_samples.get(current_doc) or {}
        current_label = str(sample.get('version_label') or '').strip()
        effective = str(sample.get('effective_from') or '').strip()
        head = f'《{title}》{current_label}' if current_label else f'《{title}》现行版本'
        if effective:
            return f'{head}自{effective}起施行，旧版内容仅作参考'
        return f'{head}已替代旧版，旧版内容仅作参考'

    @staticmethod
    def _title_key(unit: dict) -> str:
        """主题键：去掉书名号、空白与尾部版本标记，使新旧版本归为同一主题。"""
        title = normalize(str(unit.get('doc_title') or ''))
        title = title.replace('《', '').replace('》', '')
        title = title.replace(' ', '')
        stripped = _VERSION_SUFFIX_RE.sub('', title)
        return stripped or title

    # ------------------------------------------------------------------
    # 证据组装
    # ------------------------------------------------------------------
    @staticmethod
    def _heading_only(unit: dict) -> bool:
        """判断单元是否只是章节导语（无实质正文），这类单元保留但不抢位。"""
        if unit.get('heading_only'):
            return True
        if str(unit.get('kind') or '') != 'section':
            return False
        text = normalize(str(unit.get('text') or ''))
        if not text:
            return True
        heading = normalize(' '.join(
            str(item) for item in (unit.get('heading_path') or []) if item
        )).replace(' ', '')
        if heading and text.replace(' ', '') in heading:
            return True
        if len(text) <= 30:
            return True
        return False

    def _build_evidence(self, unit_id: str, record: dict) -> str:
        unit = self.unit_map[unit_id]
        kind = str(unit.get('kind') or '')
        text = str(unit.get('text') or '').strip()
        if kind == 'table_row':
            text = self._table_text(unit) or text
        elif kind == 'article':
            block = self._table_block(self._adjacent_table_rows(unit))
            if block:
                room = EVIDENCE_MAX_TOKENS - self._count_tokens(block)
                if room > 80:
                    text = self._clip_tokens(text, room).rstrip() + '\n' + block
        if self._count_tokens(text) > EVIDENCE_MAX_TOKENS:
            text = self._excerpt(unit_id, record.get('passage_id'))
            if self._count_tokens(text) > EVIDENCE_MAX_TOKENS:
                text = self._clip_tokens(text, EVIDENCE_MAX_TOKENS)
        header = self._evidence_header(unit)
        return f'{header}\n{text}' if text else header

    def _adjacent_table_rows(self, unit: dict) -> list[dict]:
        """条款以冒号/「如下」结尾时，返回紧跟其后的连续表格行。"""
        if str(unit.get('kind') or '') != 'article':
            return []
        text = str(unit.get('text') or '').rstrip()
        if not (text.endswith('：') or text.endswith(':') or '如下' in text[-30:]):
            return []
        doc_id = str(unit.get('doc_id') or '')
        order = int(unit.get('order') or 0)
        rows: list[dict] = []
        for other in self._doc_units.get(doc_id, []):
            if int(other.get('order') or 0) <= order:
                continue
            if str(other.get('kind') or '') != 'table_row':
                break
            rows.append(other)
            if len(rows) >= 12:
                break
        return rows

    @classmethod
    def _table_block(cls, rows: list[dict]) -> str:
        if not rows:
            return ''
        table = rows[0].get('table') or {}
        headers = [str(cell).strip() for cell in (table.get('headers') or []) if str(cell).strip()]
        name = str(table.get('name') or '').strip()
        lines: list[str] = []
        if name:
            lines.append(f'表格：{name}')
        if headers:
            lines.append('列：' + ' | '.join(headers))
        for row in rows:
            cells = [str(cell).strip() for cell in ((row.get('table') or {}).get('row') or [])]
            cells = [cell for cell in cells if cell]
            if not cells or cells == headers:
                continue
            lines.append('行：' + ' | '.join(cells))
        return '\n'.join(lines)

    @staticmethod
    def _table_text(unit: dict) -> str:
        """表格行证据带上表名与表头，模型才知道每个单元格的含义。"""
        table = unit.get('table') or {}
        headers = [str(cell).strip() for cell in (table.get('headers') or []) if str(cell).strip()]
        row = [str(cell).strip() for cell in (table.get('row') or []) if str(cell).strip()]
        if not row:
            return ''
        lines: list[str] = []
        name = str(table.get('name') or '').strip()
        if name:
            lines.append(f'表格：{name}')
        if headers:
            lines.append('列：' + ' | '.join(headers))
        lines.append('行：' + ' | '.join(row))
        return '\n'.join(lines)

    @classmethod
    def _table_header_only(cls, unit: dict) -> bool:
        if str(unit.get('kind') or '') != 'table_row':
            return False
        table = unit.get('table') or {}
        headers = [str(cell).strip() for cell in (table.get('headers') or [])]
        row = [str(cell).strip() for cell in (table.get('row') or [])]
        return bool(headers) and row == headers

    def _excerpt(self, unit_id: str, passage_id: str | None) -> str:
        passages = self.unit_passages.get(unit_id) or []
        if not passages:
            return ''
        position = 0
        for index, passage in enumerate(passages):
            if str(passage.get('id')) == str(passage_id):
                position = index
                break
        window = passages[max(0, position - 1):position + 2]
        return '\n'.join(
            str(passage.get('text') or '').strip()
            for passage in window if str(passage.get('text') or '').strip()
        )

    def _clip_tokens(self, text: str, limit: int) -> str:
        tokenizer = getattr(self._encoder, 'tokenizer', None) if self._encoder else None
        if tokenizer is not None:
            try:
                ids = tokenizer.encode(text, add_special_tokens=False)[:max(1, limit)]
                decoded = tokenizer.decode(ids, clean_up_tokenization_spaces=False)
                # BERT 分词器逐字解码中文时会在字间插空格，这里还原成原文形式
                decoded = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', decoded)
                decoded = re.sub(r'\s+(?=[，。；：、！？）》】”’%,.;:!?)\]])', '', decoded)
                decoded = re.sub(r'(?<=[（《【“‘(])\s+', '', decoded)
                return decoded
            except Exception:
                pass
        return text[:max(1, limit)]

    def _count_tokens(self, text: str) -> int:
        tokenizer = getattr(self._encoder, 'tokenizer', None) if self._encoder else None
        if tokenizer is not None:
            try:
                return len(tokenizer.encode(str(text or ''), add_special_tokens=False))
            except Exception:
                pass
        # 无法拿到分词器时按字符数粗估（中文场景偏保守）
        return max(1, len(str(text or '')))

    @staticmethod
    def _pages(unit: dict) -> list[int]:
        pages: list[int] = []
        for item in unit.get('pages') or []:
            try:
                pages.append(int(item))
            except (TypeError, ValueError):
                continue
        return pages

    @classmethod
    def _evidence_header(cls, unit: dict) -> str:
        parts = [f'《{unit.get("doc_title") or ""}》']
        chapter = str(unit.get('chapter') or '').strip()
        section = str(unit.get('section') or '').strip()
        article = str(unit.get('article') or '').strip()
        if chapter:
            parts.append(chapter)
        if section and section not in chapter:
            parts.append(section)
        if article:
            parts.append(article)
        pages = cls._pages(unit)
        page = pages[0] if pages else None
        if page is None:
            source = unit.get('source') or {}
            try:
                page = int(source.get('page'))
            except (TypeError, ValueError):
                page = None
        header = ' '.join(part for part in parts if part)
        if page:
            header += f'（第{page}页）'
        return header

    @classmethod
    def _citation(cls, unit: dict) -> dict:
        pages = cls._pages(unit)
        page = pages[0] if pages else None
        if page is None:
            source = unit.get('source') or {}
            try:
                page = int(source.get('page'))
            except (TypeError, ValueError):
                page = None
        source = unit.get('source') or {}
        return {
            'doc_id': str(unit.get('doc_id') or ''),
            'title': str(unit.get('doc_title') or ''),
            'doc_no': unit.get('doc_no'),
            'article': unit.get('article'),
            'chapter': unit.get('chapter'),
            'page': page,
            'pages': pages,
            'version_label': unit.get('version_label'),
            # 前端打开原文需要，契约 citation 未列出但 api 层 sources 会用到
            'file': source.get('file'),
        }

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _unit_label(unit: dict) -> str:
        title = str(unit.get('doc_title') or '')
        extra = str(unit.get('article') or unit.get('chapter') or '').strip()
        return f'《{title}》{extra}'.strip()

    def _labels_for_passages(self, passage_ids: list[str], limit: int) -> list[str]:
        labels: list[str] = []
        for passage_id in passage_ids:
            passage = self.passage_map.get(passage_id)
            if not passage:
                continue
            unit = self.unit_map.get(str(passage.get('unit_id') or ''))
            if not unit:
                continue
            label = self._unit_label(unit)
            if label not in labels:
                labels.append(label)
            if len(labels) >= limit:
                break
        return labels

    @staticmethod
    def _emit(on_stage: Callable | None, stage: str, payload: dict) -> None:
        if not on_stage:
            return
        try:
            on_stage(stage, payload)
        except Exception:
            # 推送失败不应影响检索本身
            pass
