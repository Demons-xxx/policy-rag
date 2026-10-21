"""解析器注册表：扩展名 → 解析函数。

入库链路不再写死 ``if fmt == "pdf"`` 分支，只依赖本注册表：
新增格式只需 ``register_parser(".html", parse_html)``（领域包也可以自己注册），
结构化 / 质检 / 切分 / 发布阶段完全不用改。内置解析器直接复用
``app.ingest.parser`` 的现有实现（HTML 为 Stage 3 新增），已有格式的解析结果不变：

===============  ==========================================
扩展名           解析函数
===============  ==========================================
.pdf             ``parser._parse_pdf``
.docx            ``parser._parse_docx``
.md / .markdown  ``parser._parse_text(fmt="md")``
.txt             ``parser._parse_text(fmt="txt")``
.html / .htm     ``parser._parse_html``
===============  ==========================================

对外 API：

- ``PARSE_REGISTRY``：扩展名（小写带点）→ ``fn(path) -> parsed``
- ``register_parser(ext, fn, fmt=None)``：注册/覆盖解析器，返回 ``fn``
- ``parser_for(path)``：按扩展名取解析函数，未注册时抛中文 ``RuntimeError``
- ``supported_extensions()``：当前支持的扩展名（排序后）
- ``format_for(path)``：扩展名 → 契约 2.1 的 ``format`` 字段值

解析函数契约（docs/架构与接口.md 3.2）::

    fn(path: Path) -> {
        "format": "pdf" | "docx" | "md" | "txt" | "html",
        "pages": [{"no": 1, "text": "..."}],
        "has_pdf": bool,
        "page_count": int,
    }
"""
from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Callable

from app.ingest import parser as _parser

#: 扩展名（小写、带点）→ 解析函数 ``fn(path) -> parsed``
PARSE_REGISTRY: dict[str, Callable[[Path], dict]] = {}

#: 扩展名（小写、带点）→ 文档 ``format`` 字段值
FORMAT_REGISTRY: dict[str, str] = {}

_UNSUPPORTED_PREFIX = "不支持的文件格式"


def _normalize_extension(ext: str) -> str:
    """统一成小写带点形式：``PDF`` / ``.PDF`` / ``pdf`` → ``.pdf``。"""
    value = str(ext or "").strip().lower()
    if not value:
        raise ValueError("扩展名不能为空")
    return value if value.startswith(".") else f".{value}"


def register_parser(ext: str, fn: Callable[[Path], dict],
                    fmt: str | None = None) -> Callable[[Path], dict]:
    """注册扩展名对应的解析函数。

    - ``ext`` 可写 ``.html`` / ``html`` / ``HTML``，统一按小写带点保存；
    - ``fmt`` 缺省时沿用 ``parser.FORMAT_BY_SUFFIX``，再缺省用扩展名本身；
    - 允许覆盖已有扩展名（例如测试或领域包替换默认解析器），返回 ``fn`` 便于链式使用。
    """
    if not callable(fn):
        raise TypeError(f"解析函数必须可调用：{fn!r}")
    extension = _normalize_extension(ext)
    PARSE_REGISTRY[extension] = fn
    format_name = str(fmt or "").strip().lower()
    if not format_name:
        format_name = _parser.FORMAT_BY_SUFFIX.get(extension) or extension.lstrip(".")
    FORMAT_REGISTRY[extension] = format_name
    return fn


def supported_extensions() -> list[str]:
    """当前已注册的扩展名（排序后），用于错误提示与上传白名单。"""
    return sorted(PARSE_REGISTRY)


def unsupported_message(suffix: str | None) -> str:
    """未知扩展名的统一中文错误：说明不支持的扩展名并列出支持列表。"""
    extension = str(suffix or "").strip().lower()
    supported = "、".join(supported_extensions()) or "（无）"
    return (
        f"{_UNSUPPORTED_PREFIX}：{extension or '（无扩展名）'}"
        f"（已支持的扩展名：{supported}）"
    )


def parser_for(path: Path | str) -> Callable[[Path], dict]:
    """按文件扩展名返回解析函数；未注册时抛 ``RuntimeError`` 并列出支持列表。"""
    extension = Path(path).suffix.lower()
    parser_fn = PARSE_REGISTRY.get(extension)
    if parser_fn is None:
        raise RuntimeError(unsupported_message(extension))
    return parser_fn


def format_for(path: Path | str) -> str:
    """按文件扩展名返回 ``format`` 字段值；未注册时抛同样的中文错误。"""
    extension = Path(path).suffix.lower()
    if extension not in PARSE_REGISTRY:
        raise RuntimeError(unsupported_message(extension))
    return FORMAT_REGISTRY.get(extension) or extension.lstrip(".")


def _register_builtin_parsers() -> None:
    """内置解析器只做注册，解析实现仍在 parser.py，结果与旧版逐字一致。"""
    register_parser(".pdf", _parser._parse_pdf)
    register_parser(".docx", _parser._parse_docx)
    register_parser(".md", partial(_parser._parse_text, fmt="md"), fmt="md")
    register_parser(".markdown", partial(_parser._parse_text, fmt="md"), fmt="md")
    register_parser(".txt", partial(_parser._parse_text, fmt="txt"), fmt="txt")
    register_parser(".html", _parser._parse_html)
    register_parser(".htm", _parser._parse_html)


_register_builtin_parsers()

__all__ = [
    "FORMAT_REGISTRY",
    "PARSE_REGISTRY",
    "format_for",
    "parser_for",
    "register_parser",
    "supported_extensions",
    "unsupported_message",
]
