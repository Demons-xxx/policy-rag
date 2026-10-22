"""质量检查：空页、乱码、无正文、完全重复单元、条款号跳号。

契约（docs/架构与接口.md 3.4）：issue 字段为
``{"level": "warn|error", "code": ..., "message": 中文说明, "page": int|None, "blocking": bool}``。
``blocking`` 只在整份文档不可用时为 true（无正文 / 没有任何可用单元）。
"""
from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_PUNCT_RE = re.compile(r"[，。；：、！？《》“”‘’（）]")
_REPLACEMENT = "\ufffd"
_MOJIBAKE_MARKERS = ("锟斤拷", "Ã¤", "Ã©", "Ã¨", "â€", "ï¿½")


def _issue(code: str, message: str, page: int | None = None,
           level: str = "warn", blocking: bool = False) -> dict:
    return {
        "level": level,
        "code": code,
        "message": message,
        "page": int(page) if page else None,
        "blocking": bool(blocking),
    }


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _garbled_reason(text: str) -> str | None:
    """返回乱码原因；正常文本返回 None。"""
    if not text:
        return None
    length = len(text.strip())
    if not length:
        return None
    replacement = text.count(_REPLACEMENT)
    if replacement >= 3 or (replacement and replacement / length > 0.002):
        return f"包含 {replacement} 个无法识别的替换字符（U+FFFD）"
    for marker in _MOJIBAKE_MARKERS:
        if marker in text:
            return f"包含疑似编码错误的字符「{marker}」"
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 50:
        cjk = len(_CJK_RE.findall(compact))
        if cjk == 0 and len(_CJK_PUNCT_RE.findall(compact)) >= 5:
            return "中文标点密集但没有可识别汉字，疑似字体或编码损坏"
    return None


def check_units(units: list[dict], parsed: dict) -> list[dict]:
    """检查文档解析与结构化质量，返回 issue 列表（可为空）。"""
    issues: list[dict] = []
    pages = (parsed or {}).get("pages") or []

    # 1) 空页
    for page in pages:
        page_no = int(page.get("no") or 1)
        if not (page.get("text") or "").strip():
            issues.append(_issue(
                "empty_page",
                f"第 {page_no} 页没有提取到文本（可能是空白页或扫描图片页）。",
                page_no,
            ))

    # 1.1) OCR 页质量提示（不阻断，供人工复核）
    for page in pages:
        codes = page.get("ocr_issues") or []
        if not codes:
            continue
        page_no = int(page.get("no") or 1)
        issues.append(_issue(
            "ocr_quality",
            f"第 {page_no} 页由 OCR 识别，质量提示：{'、'.join(str(code) for code in codes)}，建议人工核对原文。",
            page_no,
        ))

    # 2) 全文字符数与无正文判断
    total_chars = sum(len((page.get("text") or "").strip()) for page in pages)
    unit_chars = sum(len((unit.get("text") or "").strip()) for unit in units or [])
    if not units or total_chars == 0 or unit_chars == 0:
        issues.append(_issue(
            "no_text",
            "未能从文档中提取到任何正文内容，请检查文件是否损坏、是否为扫描件或加密文档。",
            None,
            level="error",
            blocking=True,
        ))
        return issues

    # 3) 乱码（逐页检查，相同页码只报一次）
    reported_pages: set[int] = set()
    for page in pages:
        page_no = int(page.get("no") or 1)
        reason = _garbled_reason(page.get("text") or "")
        if reason and page_no not in reported_pages:
            reported_pages.add(page_no)
            issues.append(_issue(
                "garbled",
                f"第 {page_no} 页文本可能乱码：{reason}。建议检查原始文件的字体/编码。",
                page_no,
                level="error",
            ))

    # 4) 完全重复的单元（忽略空白，短文本不判重）
    seen: dict[str, dict] = {}
    for unit in units:
        text = _squash(unit.get("text") or "")
        if len(text) < 20:
            continue
        if text in seen:
            first = seen[text]
            page = (unit.get("pages") or [None])[0]
            issues.append(_issue(
                "duplicate",
                f"第 {unit.get('order')} 个单元与第 {first.get('order')} 个单元内容完全重复，"
                f"可能是重复页或解析重复。",
                page,
            ))
        else:
            seen[text] = unit

    # 5) 条款号跳号 / 重复
    articles = [unit for unit in units if unit.get("kind") == "article"]
    previous: dict | None = None
    for unit in articles:
        number = unit.get("article_no")
        if previous is not None and number is not None:
            prev_number = previous.get("article_no")
            if prev_number is not None and number > prev_number + 1:
                missing = "、".join(f"第{item}条" for item in range(prev_number + 1, number))
                issues.append(_issue(
                    "missing_article",
                    f"条款号不连续：{previous.get('article')} 之后直接是 "
                    f"{unit.get('article')}，缺少 {missing}。",
                    (unit.get("pages") or [None])[0],
                ))
            elif prev_number is not None and number <= prev_number:
                issues.append(_issue(
                    "missing_article",
                    f"条款号顺序异常：{unit.get('article')} 出现在 "
                    f"{previous.get('article')} 之后（重复或倒序）。",
                    (unit.get("pages") or [None])[0],
                ))
        if number is not None:
            previous = unit
    return issues


def has_blocking(issues: list[dict]) -> bool:
    return any(issue.get("blocking") for issue in issues or [])
