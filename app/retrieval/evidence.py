"""证据组装：单元 → 证据文本、表格挂接、片段窗口、token 预算与版本提示。

所有函数都显式接收所需数据（unit / 片段索引 / 分词器），不依赖检索器实例，
因此可以单独测试；token 预算的取舍顺序由 ``app.retrieval.engine`` 编排。
"""
from __future__ import annotations

import re

from app.domain import versions as versioning

# 单个证据单元超过该 token 数时只取命中片段 ±1 段
EVIDENCE_MAX_TOKENS = 1200
# 表头 + 表名占用的预算余量下限：余量太小时不再并入表格
TABLE_ROOM_FLOOR = 80
# 「问题确实在问版本/有效性」的判断词
VERSION_WORDS = ('有效', '现行', '旧版', '新版', '修订', '废止', '版本', '之前', '原来')
# 问题里的数值/期限：命中时优先展示含该数值的条款与表格行
NUMERIC_RE = re.compile(
    r'(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千两]+)\s*'
    r'(?:个?工作日|个?自然日|日|天|周|个月|月|年|小时|分钟|'
    r'%|％|元|万元|倍|次|人|份|页|字|米|平方米|公里|千米|公斤|kg|折)'
)


def numeric_tokens(*texts: str) -> list[str]:
    """从问题与改写问句里提取数值/期限词（去空格、去重，最多 6 个）。"""
    tokens: list[str] = []
    for text in texts:
        for match in NUMERIC_RE.finditer(str(text or '')):
            token = re.sub(r'\s+', '', match.group(0))
            if len(token.replace('.', '')) < 2:
                continue
            if token not in tokens:
                tokens.append(token)
    return tokens[:6]


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------

def adjacent_table_rows(unit: dict, doc_units: dict[str, list[dict]]) -> list[dict]:
    """返回条款后紧跟的连续表格行。

    - 条款以冒号/「如下」结尾：表题可省略；
    - 否则允许跨过至多 2 个短标题/表题行（如「科研经费报销标准表」，≤30 字且不以句号结尾），
      再接表格。这样「条款 + 标准表」的写法也能把表格作为该条款的证据，
      不会因为表题单独成了一个段落而丢失数值行。
    """
    if str(unit.get('kind') or '') != 'article':
        return []
    text = str(unit.get('text') or '').rstrip()
    explicit = bool(text.endswith('：') or text.endswith(':') or '如下' in text[-30:])
    doc_id = str(unit.get('doc_id') or '')
    order = int(unit.get('order') or 0)
    rows: list[dict] = []
    skipped = 0
    for other in doc_units.get(doc_id, []):
        if int(other.get('order') or 0) <= order:
            continue
        kind = str(other.get('kind') or '')
        if kind == 'table_row':
            rows.append(other)
            if len(rows) >= 12:
                break
            continue
        if rows:
            break
        caption = str(other.get('text') or '').strip()
        if (not explicit and skipped < 2 and kind in ('paragraph', 'heading')
                and 0 < len(caption) <= 30 and not caption.endswith('。')):
            skipped += 1
            continue
        break
    return rows


def _row_matches(row: dict, prefer: list[str] | None) -> bool:
    if not prefer:
        return False
    cells = [str(cell).strip() for cell in ((row.get('table') or {}).get('row') or [])]
    text = re.sub(r'\s+', '', ' | '.join(cell for cell in cells if cell))
    return any(token in text for token in prefer)


def table_block(rows: list[dict], prefer: list[str] | None = None) -> str:
    """把连续表格行渲染成证据：表名 + 列 + 行；问题含数值时该行排到最前。"""
    if not rows:
        return ''
    table = rows[0].get('table') or {}
    headers = [str(cell).strip() for cell in (table.get('headers') or []) if str(cell).strip()]
    name = str(table.get('name') or '').strip()
    ordered = sorted(rows, key=lambda row: 0 if _row_matches(row, prefer) else 1)
    lines: list[str] = []
    if name:
        lines.append(f'表格：{name}')
    if headers:
        lines.append('列：' + ' | '.join(headers))
    for row in ordered:
        cells = [str(cell).strip() for cell in ((row.get('table') or {}).get('row') or [])]
        cells = [cell for cell in cells if cell]
        if not cells or cells == headers:
            continue
        lines.append('行：' + ' | '.join(cells))
    return '\n'.join(lines)


def table_text(unit: dict) -> str:
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


# ---------------------------------------------------------------------------
# token 计数与截断
# ---------------------------------------------------------------------------

def count_tokens(text: str, tokenizer=None) -> int:
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(str(text or ''), add_special_tokens=False))
        except Exception:
            pass
    # 无法拿到分词器时按字符数粗估（中文场景偏保守）
    return max(1, len(str(text or '')))


def clip_tokens(text: str, limit: int, tokenizer=None) -> str:
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


def excerpt(unit_id: str, passage_id: str | None,
            unit_passages: dict[str, list[dict]]) -> str:
    """长单元只保留命中片段 ±1 段。"""
    passages = unit_passages.get(unit_id) or []
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


# ---------------------------------------------------------------------------
# 证据文本
# ---------------------------------------------------------------------------

def build_evidence(unit_id: str, unit: dict, record: dict,
                   doc_units: dict[str, list[dict]],
                   unit_passages: dict[str, list[dict]],
                   tokenizer=None,
                   max_tokens: int = EVIDENCE_MAX_TOKENS,
                   prefer: list[str] | None = None,
                   merged_units: list[dict] | None = None) -> str:
    """生成一段证据：抬头 + 原文/表格/命中片段，超长时截断。

    ``merged_units``：同一 article 的其它单元，正文按原顺序拼在主语段之后；
    ``prefer`` 是问题里的数值/期限，表格行据此把命中行排到最前。
    """
    kind = str(unit.get('kind') or '')
    text = str(unit.get('text') or '').strip()
    extras = [str(other.get('text') or '').strip() for other in (merged_units or [])]
    extras = [item for item in extras if item and item not in text]
    if extras:
        text = '\n'.join([text] + extras)
    if kind == 'table_row':
        text = table_text(unit) or text
    elif kind == 'article':
        block = table_block(adjacent_table_rows(unit, doc_units), prefer)
        if block:
            room = max_tokens - count_tokens(block, tokenizer)
            if room > TABLE_ROOM_FLOOR:
                text = clip_tokens(text, room, tokenizer).rstrip() + '\n' + block
    if count_tokens(text, tokenizer) > max_tokens:
        text = excerpt(unit_id, record.get('passage_id'), unit_passages)
        if count_tokens(text, tokenizer) > max_tokens:
            text = clip_tokens(text, max_tokens, tokenizer)
    header = evidence_header(unit)
    return f'{header}\n{text}' if text else header


def pages(unit: dict) -> list[int]:
    values: list[int] = []
    for item in unit.get('pages') or []:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    return values


def _first_page(unit: dict) -> int | None:
    values = pages(unit)
    page = values[0] if values else None
    if page is None:
        source = unit.get('source') or {}
        try:
            page = int(source.get('page'))
        except (TypeError, ValueError):
            page = None
    return page


def evidence_header(unit: dict) -> str:
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
    page = _first_page(unit)
    header = ' '.join(part for part in parts if part)
    if page:
        header += f'（第{page}页）'
    return header


def citation(unit: dict) -> dict:
    values = pages(unit)
    source = unit.get('source') or {}
    return {
        'doc_id': str(unit.get('doc_id') or ''),
        'title': str(unit.get('doc_title') or ''),
        'doc_no': unit.get('doc_no'),
        'article': unit.get('article'),
        'chapter': unit.get('chapter'),
        'page': _first_page(unit),
        'pages': values,
        'version_label': unit.get('version_label'),
        # 前端打开原文需要，契约 citation 未列出但 api 层 sources 会用到
        'file': source.get('file'),
    }


def unit_label(unit: dict) -> str:
    title = str(unit.get('doc_title') or '')
    extra = str(unit.get('article') or unit.get('chapter') or '').strip()
    return f'《{title}》{extra}'.strip()


def labels_for_passages(passage_ids: list[str], passage_map: dict[str, dict],
                        unit_map: dict[str, dict], limit: int) -> list[str]:
    labels: list[str] = []
    for passage_id in passage_ids:
        passage = passage_map.get(passage_id)
        if not passage:
            continue
        unit = unit_map.get(str(passage.get('unit_id') or ''))
        if not unit:
            continue
        label = unit_label(unit)
        if label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


# ---------------------------------------------------------------------------
# 版本提示
# ---------------------------------------------------------------------------

def version_note(unit: dict, current_docs: dict[str, str],
                 doc_samples: dict[str, dict] | None = None) -> str | None:
    """版本提示的规则在 app/domain/versions.py，这里只做取值适配。"""
    return versioning.version_note(unit, current_docs, doc_samples)


def wants_version_hint(question: str, rewritten: str) -> bool:
    topic_text = f'{question}{rewritten}'
    return any(word in topic_text for word in VERSION_WORDS)


__all__ = [
    'EVIDENCE_MAX_TOKENS', 'TABLE_ROOM_FLOOR', 'VERSION_WORDS', 'NUMERIC_RE', 'numeric_tokens',
    'adjacent_table_rows', 'table_block', 'table_text',
    'count_tokens', 'clip_tokens', 'excerpt',
    'build_evidence', 'pages', 'evidence_header', 'citation', 'unit_label',
    'labels_for_passages', 'version_note', 'wants_version_hint',
]
