"""文档解析：PDF / DOCX / Markdown / 纯文本 / HTML → 统一的 pages 结构。

契约（docs/架构与接口.md 3.2）::

    parse_document(path) -> {
        "format": "pdf" | "docx" | "md" | "txt" | "html",
        "pages": [{"no": 1, "text": "..."}],
        "has_pdf": bool,
        "page_count": int,
    }

DOCX 额外返回 ``tables`` 与 ``blocks``：表格按 markdown 风格行写入 pages 文本，
同时保留结构化行列，供 structure 生成 ``table_row`` 单元；``blocks`` 保留正文与
表格的原始顺序，避免用文本行反推表格而错位。

Stage 3 起解析器按扩展名注册在 ``app.ingest.registry.PARSE_REGISTRY``：
``parse_document`` 只是查表后调用；本文件保留各格式的具体实现与常量，
已有格式（PDF/DOCX/MD/TXT）的解析结果逐字不变，HTML 为新增格式。
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

FORMAT_BY_SUFFIX = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".html": "html",
    ".htm": "html",
}

PARSER_NAME = {
    "pdf": "pymupdf",
    "docx": "python-docx",
    "md": "text",
    "txt": "text",
    "html": "html.parser",
}

SOURCE_SUFFIX = {
    "pdf": ".pdf",
    "docx": ".docx",
    "md": ".md",
    "txt": ".txt",
    "html": ".html",
}


def detect_format(path: Path) -> str:
    """按扩展名判断格式（查注册表），未知格式抛出中文异常并列出支持列表。"""
    from app.ingest.registry import format_for

    return format_for(path)


def _read_text(path: Path) -> str:
    """读取文本：优先 UTF-8，其次 GBK，最后 UTF-8 容错替换。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_pdf(path: Path) -> dict:
    try:
        import fitz  # pymupdf
    except ImportError as exc:  # pragma: no cover - 环境已装
        raise RuntimeError("缺少 PDF 解析依赖 pymupdf，请先安装。") from exc

    try:
        document = fitz.open(str(path))
    except Exception as exc:
        raise RuntimeError(f"PDF 打开失败：{exc}") from exc
    try:
        if getattr(document, "needs_pass", False):
            raise RuntimeError("PDF 已加密，无法解析，请先解除密码保护。")
        pages = [
            {"no": index + 1, "text": page.get_text("text") or ""}
            for index, page in enumerate(document)
        ]
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"PDF 解析失败：{exc}") from exc
    finally:
        document.close()

    return {
        "format": "pdf",
        "pages": pages,
        "has_pdf": True,
        "page_count": len(pages),
        "tables": [],
        "parser": PARSER_NAME["pdf"],
    }


def _cell_text(cell) -> str:
    return " ".join((cell.text or "").split())


def _table_caption(previous_lines: list[str], index: int) -> str | None:
    """从紧邻表格上方的短段落里猜测表名。"""
    for line in reversed(previous_lines[-3:]):
        text = line.strip()
        if not text or len(text) > 40:
            continue
        if text.endswith(("。", "；", ";", "：")):
            continue
        if "表" in text or "标准" in text or "一览" in text:
            return text
    return None


def _parse_docx(path: Path) -> dict:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 环境已装
        raise RuntimeError("缺少 DOCX 解析依赖 python-docx，请先安装。") from exc

    try:
        document = Document(str(path))
    except Exception as exc:
        raise RuntimeError(f"DOCX 打开失败：{exc}") from exc

    lines: list[str] = []
    blocks: list[dict] = []
    tables: list[dict] = []
    try:
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                text = Paragraph(child, document).text.strip()
                if text:
                    lines.append(text)
                    blocks.append({"type": "paragraph", "text": text})
            elif tag == "tbl":
                table = Table(child, document)
                rows: list[list[str]] = []
                for row in table.rows:
                    cells = [_cell_text(cell) for cell in row.cells]
                    if any(cells):
                        rows.append(cells)
                if not rows:
                    continue
                headers = rows[0]
                name = _table_caption(lines, len(tables) + 1)
                rows_md = [" | ".join(row) for row in rows]
                table_record = {
                    "name": name,
                    "headers": headers,
                    "rows": rows,
                    "rows_md": rows_md,
                    "page": 1,
                }
                tables.append(table_record)
                blocks.append({"type": "table", **table_record})
                lines.extend(rows_md)
    except Exception as exc:
        raise RuntimeError(f"DOCX 解析失败：{exc}") from exc

    text = "\n".join(lines)
    return {
        "format": "docx",
        "pages": [{"no": 1, "text": text}],
        "has_pdf": False,
        "page_count": 1,
        "tables": tables,
        "blocks": blocks,
        "parser": PARSER_NAME["docx"],
    }


def _parse_text(path: Path, fmt: str) -> dict:
    text = _read_text(path)
    return {
        "format": fmt,
        "pages": [{"no": 1, "text": text}],
        "has_pdf": False,
        "page_count": 1,
        "tables": [],
        "blocks": [{"type": "paragraph", "text": line.strip()}
                   for line in text.splitlines() if line.strip()],
        "parser": PARSER_NAME[fmt],
    }


_HTML_SKIP_TAGS = {"head", "script", "style", "noscript", "template"}
_HTML_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "tbody", "thead",
    "section", "article", "header", "footer", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _HTMLTextExtractor(HTMLParser):
    """把 HTML 正文抽成纯文本行；表格单元格用 ``" | "`` 连接，便于结构识别表格行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buffer: list[str] = []
        self.skip_depth = 0

    def _flush(self) -> None:
        text = "".join(self.buffer)
        self.buffer = []
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if line:
                self.lines.append(line)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _HTML_SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in ("td", "th"):
            current = "".join(self.buffer).rstrip()
            if current and not current.endswith("|"):
                self.buffer.append(" | ")
            return
        if tag in _HTML_BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _HTML_SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in _HTML_BLOCK_TAGS or tag == "tr":
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.buffer.append(data)

    def text(self) -> str:
        self.close()
        self._flush()
        return "\n".join(self.lines)


def _parse_html(path: Path) -> dict:
    raw = _read_text(path)
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(raw)
        text = extractor.text()
    except Exception as exc:
        raise RuntimeError(f"HTML 解析失败：{exc}") from exc
    return {
        "format": "html",
        "pages": [{"no": 1, "text": text}],
        "has_pdf": False,
        "page_count": 1,
        "tables": [],
        "blocks": [{"type": "paragraph", "text": line}
                   for line in text.splitlines() if line.strip()],
        "parser": PARSER_NAME["html"],
    }


def parse_document(path: Path) -> dict:
    """解析一份文档（按注册表选择解析器），返回契约 3.2 规定的结构。"""
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"文件不存在：{path}")
    from app.ingest.registry import parser_for

    return parser_for(path)(path)


def source_suffix(fmt: str) -> str:
    """入库时复制原文使用的扩展名。"""
    return SOURCE_SUFFIX.get(fmt, ".bin")


def parser_name(fmt: str) -> str:
    return PARSER_NAME.get(fmt, "text")
