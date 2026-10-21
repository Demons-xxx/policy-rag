"""结构化：把解析后的页文本切成知识单元 unit。

契约（docs/架构与接口.md 2.2 / 3.3）：
- 识别「第X章」「第X节」「第X条」「（一）（二）」「附件」「表格行」；
- 每条 unit 的 text 是该条完整原文（含后续款项），不拆碎；
- 无编号正文归入最近条款，没有条款则 kind=paragraph；
- 表格行单独成 unit（kind=table_row，带 table 字段）；
- order 按文档顺序递增，id 形如 ``<doc_id>#u<order:04d>``。
"""
from __future__ import annotations

import re

_CN_NUMBER = "〇零一二三四五六七八九十百千两"
_CN_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

_CHAPTER_RE = re.compile(
    rf"^第\s*([{_CN_NUMBER}\d]+)\s*章(?:\s*[:：、.．]?\s*(.*))?$"
)
_SECTION_RE = re.compile(
    rf"^第\s*([{_CN_NUMBER}\d]+)\s*节(?:\s*[:：、.．]?\s*(.*))?$"
)
_ARTICLE_RE = re.compile(
    rf"^第\s*([{_CN_NUMBER}\d]+)\s*条(?:\s*[:：、.．]?\s*(.*))?$"
)
_ATTACHMENT_RE = re.compile(
    rf"^附\s*件(?:\s*([{_CN_NUMBER}\d]+))?(?:\s*[:：、.．]\s*|\s+|\s*$)(.*)$"
)
_ITEM_RE = re.compile(rf"^[（(]\s*([{_CN_NUMBER}\d]+)\s*[)）]")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")


def chinese_number(text: str | None) -> int | None:
    """把「十二」「一百零三」「12」转成整数；无法解析返回 None。"""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            return None
    total += section + number
    return total


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


class _PageLocator:
    """在 PDF 页文本中定位单元起始页；docx/md/txt 固定第 1 页。"""

    def __init__(self, pages: list[dict], has_pdf: bool):
        self.has_pdf = bool(has_pdf)
        self.items = [
            (int(page.get("no") or index + 1), _squash(page.get("text") or ""))
            for index, page in enumerate(pages or [])
        ]

    def locate(self, text: str, fallback: int = 1) -> int:
        if not self.has_pdf or not self.items:
            return fallback
        probes: list[str] = []
        for line in (text or "").splitlines():
            squashed = _squash(line)
            if squashed:
                probes.append(squashed[:40])
                break
        whole = _squash(text)
        if whole:
            probes.append(whole[:40])
        for probe in probes:
            if not probe:
                continue
            for page_no, content in self.items:
                if probe in content:
                    return page_no
        return fallback


class _UnitBuilder:
    def __init__(self, parsed: dict, meta: dict):
        self.parsed = parsed or {}
        self.meta = meta or {}
        self.doc_id = str(self.meta.get("doc_id") or "")
        if not self.doc_id:
            raise ValueError("build_units 需要 meta['doc_id']")
        self.title = str(self.meta.get("doc_title") or self.meta.get("title") or "")
        self.file_name = str(self.meta.get("file_name") or "")
        self.status = str(self.meta.get("status") or "active")
        self.parser = str(
            self.parsed.get("parser")
            or ("pymupdf" if self.parsed.get("has_pdf") else "text")
        )
        pages = self.parsed.get("pages") or []
        self.locator = _PageLocator(pages, bool(self.parsed.get("has_pdf")))
        self.units: list[dict] = []
        self.order = 1
        self.chapter: str | None = None
        self.section: str | None = None
        self.attachment: str | None = None
        self.article: str | None = None
        self.article_no: int | None = None
        self.article_unit: dict | None = None
        self.attachment_unit: dict | None = None
        self.paragraph_lines: list[str] = []
        self.paragraph_pages: list[int] = []

    # ---- 基础 ----
    def _heading_path(self, article: str | None = None) -> list[str]:
        parts = [self.chapter, self.section, self.attachment, article]
        return [part for part in parts if part]

    def add(self, kind: str, text: str, *, page: int | None = None,
            pages: list[int] | None = None, table: dict | None = None,
            article: str | None = None, article_no: int | None = None) -> dict:
        text = (text or "").strip()
        if page is None:
            page = self.locator.locate(text)
        page_list = sorted({int(item) for item in (pages or [page]) if item})
        if not page_list:
            page_list = [1]
        unit = {
            "id": f"{self.doc_id}#u{self.order:04d}",
            "doc_id": self.doc_id,
            "doc_title": self.title,
            "doc_no": self.meta.get("doc_no"),
            "version_label": self.meta.get("version_label"),
            "effective_from": self.meta.get("effective_from"),
            "effective_to": self.meta.get("effective_to"),
            "department": self.meta.get("department"),
            "status": self.status,
            "chapter": self.chapter,
            "section": self.section,
            "article": article,
            "article_no": article_no,
            "kind": kind,
            "heading_path": self._heading_path(article),
            "text": text,
            "pages": page_list,
            "table": table,
            "order": self.order,
            "source": {
                "doc_id": self.doc_id,
                "file": self.file_name,
                "page": page_list[0],
                "page_end": page_list[-1],
                "parser": self.parser,
            },
        }
        self.units.append(unit)
        self.order += 1
        return unit

    def _append_body(self, unit: dict, line: str, page: int) -> None:
        unit["text"] = f"{unit['text']}\n{line}".strip("\n")
        if page not in unit["pages"]:
            unit["pages"] = sorted({*unit["pages"], page})
        unit["source"]["page_end"] = unit["pages"][-1]

    def flush_paragraph(self) -> None:
        if not self.paragraph_lines:
            return
        text = "\n".join(self.paragraph_lines).strip()
        pages = sorted(set(self.paragraph_pages)) or [1]
        self.paragraph_lines = []
        self.paragraph_pages = []
        if text:
            self.add("paragraph", text, page=pages[0], pages=pages)

    def _reset_article(self) -> None:
        self.article = None
        self.article_no = None
        self.article_unit = None

    # ---- 标题 ----
    def on_markdown_heading(self, level: int, text: str, page: int) -> None:
        self.flush_paragraph()
        text = _match_line((text or "").strip().lstrip("#").strip())
        if not text:
            return
        if level <= 2:
            self.chapter = text
            self.section = None
            self.attachment = None
            self.attachment_unit = None
            self._reset_article()
        else:
            self.section = text
            self._reset_article()
        self.add("section", text, page=page)

    def on_chapter(self, line: str, page: int) -> None:
        self.flush_paragraph()
        self.chapter = line
        self.section = None
        self.attachment = None
        self.attachment_unit = None
        self._reset_article()
        self.add("section", line, page=page)

    def on_section(self, line: str, page: int) -> None:
        self.flush_paragraph()
        self.section = line
        self._reset_article()
        self.add("section", line, page=page)

    def on_article(self, line: str, number_text: str, page: int) -> None:
        self.flush_paragraph()
        number = chinese_number(number_text)
        label = f"第{number_text}条"
        self.article = label
        self.article_no = number
        unit = self.add("article", line, page=page, article=label, article_no=number)
        self.article_unit = unit

    def on_attachment(self, line: str, page: int) -> None:
        self.flush_paragraph()
        self.attachment = line
        self._reset_article()
        self.attachment_unit = self.add("attachment", line, page=page)

    def on_body(self, line: str, page: int) -> None:
        if self.article_unit is not None:
            self._append_body(self.article_unit, line, page)
        elif self.attachment_unit is not None:
            self._append_body(self.attachment_unit, line, page)
        else:
            self.paragraph_lines.append(line)
            self.paragraph_pages.append(page)

    # ---- 表格 ----
    def on_table(self, rows: list[list[str]], page: int, name: str | None,
                 index: int, headers: list[str] | None = None) -> None:
        self.flush_paragraph()
        rows = [
            row for row in rows
            if any(str(cell).strip() for cell in row) and not _is_separator_row(row)
        ]
        if not rows:
            return
        headers = list(headers or rows[0])
        table_name = (name or "").strip() or (
            f"{self.title} 表格{index}" if self.title else f"表格{index}"
        )
        for row in rows:
            text = " | ".join(str(cell).strip() for cell in row)
            if not text.strip(" |"):
                continue
            self.add(
                "table_row",
                text,
                page=page,
                table={
                    "name": table_name,
                    "headers": list(headers),
                    "row": [str(cell).strip() for cell in row],
                },
            )


def _match_line(line: str) -> str:
    """用于编号识别的文本：去掉列表符号与 Markdown 强调标记，原文不改。"""
    text = (line or "").strip()
    text = re.sub(r"^(?:[-+]|\d+[.)])\s+", "", text)
    changed = True
    while changed and len(text) > 4:
        changed = False
        for marker in ("**", "__"):
            if text.startswith(marker) and text.endswith(marker):
                text = text[len(marker):-len(marker)].strip()
                changed = True
        for marker in ("*", "_"):
            if text.startswith(marker) and text.endswith(marker) and len(text) > 2:
                text = text[1:-1].strip()
                changed = True
    for marker in ("**", "__", "*", "_"):
        if text.startswith(marker):
            text = text[len(marker):].strip()
            break
    return text


def _split_pipe_row(line: str) -> list[str]:
    """拆 pipe 表格行，去掉首尾空单元格（兼容 markdown 的 ``| a | b |`` 写法）。"""
    cells = [cell.strip() for cell in line.split("|")]
    while cells and not cells[0]:
        cells.pop(0)
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _is_separator_row(row: list[str]) -> bool:
    cells = [str(cell).strip().replace(" ", "") for cell in row or []]
    return bool(cells) and all(_TABLE_SEPARATOR_RE.match(cell) for cell in cells)


def _is_pipe_line(line: str) -> bool:
    if not line or "|" not in line:
        return False
    if line.lstrip().startswith("#"):
        return False
    cells = _split_pipe_row(line)
    if len(cells) < 2:
        return False
    if _is_separator_row(cells):
        return True
    return sum(1 for cell in cells if cell) >= 2


def _page_lines(parsed: dict) -> list[tuple[str, int]]:
    lines: list[tuple[str, int]] = []
    for page in parsed.get("pages") or []:
        page_no = int(page.get("no") or 1)
        for raw in (page.get("text") or "").splitlines():
            lines.append((raw.strip(), page_no))
    return lines


def build_units(parsed: dict, meta: dict) -> list[dict]:
    """把解析结果结构化为 unit 列表（契约 2.2）。"""
    if not parsed:
        raise ValueError("build_units 收到空的解析结果")
    builder = _UnitBuilder(parsed, meta)
    fmt = (parsed.get("format") or "").lower()
    table_index = 0

    if fmt == "docx" and parsed.get("blocks"):
        items: list[dict] = []
        for block in parsed["blocks"]:
            if block.get("type") == "table":
                items.append({"type": "table", "table": block})
            else:
                text = str(block.get("text") or "")
                for line in text.splitlines():
                    items.append({"type": "line", "line": line.strip(), "page": 1})
    else:
        line_items = _page_lines(parsed)
        items = []
        index = 0
        while index < len(line_items):
            line, page = line_items[index]
            if _is_pipe_line(line):
                rows: list[list[str]] = []
                start_page = page
                while index < len(line_items) and _is_pipe_line(line_items[index][0]):
                    row_line, row_page = line_items[index]
                    start_page = start_page or row_page
                    rows.append(_split_pipe_row(row_line))
                    index += 1
                items.append({"type": "table", "rows": rows, "page": start_page})
                continue
            items.append({"type": "line", "line": line, "page": page})
            index += 1

    for item in items:
        if item["type"] == "table":
            if "table" in item:
                table = item["table"]
                table_index += 1
                builder.on_table(
                    rows=table.get("rows") or [],
                    page=int(table.get("page") or 1),
                    name=table.get("name"),
                    index=table_index,
                    headers=table.get("headers"),
                )
            else:
                table_index += 1
                builder.on_table(
                    rows=item.get("rows") or [],
                    page=int(item.get("page") or 1),
                    name=None,
                    index=table_index,
                )
            continue

        line = item.get("line", "")
        page = int(item.get("page") or 1)
        if not line:
            # 空行只用来切分无编号段落
            builder.flush_paragraph()
            continue

        heading = _MD_HEADING_RE.match(line)
        if heading:
            builder.on_markdown_heading(len(heading.group(1)), heading.group(2), page)
            continue

        match_line = _match_line(line)
        match = _CHAPTER_RE.match(match_line)
        if match:
            builder.on_chapter(line, page)
            continue

        match = _SECTION_RE.match(match_line)
        if match:
            builder.on_section(line, page)
            continue

        match = _ARTICLE_RE.match(match_line)
        if match:
            builder.on_article(line, match.group(1), page)
            continue

        match = _ATTACHMENT_RE.match(match_line)
        if match:
            builder.on_attachment(line, page)
            continue

        if _ITEM_RE.match(match_line):
            # （一）（二）是上一条款的款项，附回条款正文；无条款时按普通正文处理
            builder.on_body(line, page)
            continue

        builder.on_body(line, page)

    builder.flush_paragraph()
    return builder.units


def unit_text_with_heading(unit: dict) -> str:
    """证据展示用文本：章节/条款抬头 + 原文。"""
    path = [part for part in (unit.get("heading_path") or []) if part]
    if path:
        return "\n".join(path + [unit.get("text") or ""])
    return unit.get("text") or ""
