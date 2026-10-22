"""扫描页 OCR 客户端与质量检查。

入库解析时，PDF 每页先走 PyMuPDF 文本层；文本量不足的页（扫描件/图片页）
渲染成 PNG 后 POST 到 OCR 端点，拿回 Markdown 文本替换该页正文。

端点契约与 RAG 项目的 ``remote_ocr.py`` 一致（multipart 字段 ``image``，
返回 ``{"code": 0, "result": "markdown"}``），因此两边可以共用同一个 OCR 服务；
质量检查沿用其思路：过短、表格标签不闭合、重复行/重复片段。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

# 扫描页判定：文本层少于该字符数视为没有文本层。
MIN_TEXT_CHARS = 20
# 质量阈值
MIN_OCR_CHARS = 20
REPEAT_LINE_THRESHOLD = 6
REPEAT_SPAN_THRESHOLD = 6


class OcrError(RuntimeError):
    """OCR 调用失败（端点不可用、返回结构异常等）。"""


class OcrTruncated(OcrError):
    """OCR 输出被截断（达到 max tokens），该页需要人工复核。"""


class OcrClient:
    """极简 OCR HTTP 客户端：一次一页，multipart 上传 PNG。"""

    def __init__(self, endpoint: str, timeout: float = 180.0) -> None:
        self.endpoint = str(endpoint or '').strip()
        self.timeout = float(timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    def recognize(self, image: Path) -> tuple[str, dict]:
        """识别一页图片，返回（markdown 文本, 元信息）。"""
        if not self.enabled:
            raise OcrError('OCR 端点未配置（POLICYRAG_OCR_ENDPOINT）')
        image = Path(image)
        boundary = 'policyrag-' + uuid.uuid4().hex
        payload = (
            f'--{boundary}\r\n'
            'Content-Disposition: form-data; name="image"; filename="page.png"\r\n'
            'Content-Type: image/png\r\n\r\n'
        ).encode() + image.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        request = urllib.request.Request(
            self.endpoint, data=payload, method='POST',
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.URLError as exc:
            raise OcrError(f'OCR 服务连接失败：{getattr(exc, "reason", exc)}') from exc
        except (json.JSONDecodeError, TimeoutError) as exc:
            raise OcrError(f'OCR 服务响应异常：{type(exc).__name__}: {exc}') from exc
        if not isinstance(body, dict) or body.get('code') != 0 or not isinstance(body.get('result'), str):
            message = body.get('message') if isinstance(body, dict) else body
            raise OcrError(f'OCR 服务返回失败：{str(message)[:200]}')
        if body.get('output_truncated') or body.get('finish_reason') == 'length':
            raise OcrTruncated('OCR 输出达到长度上限，页面需人工复核')
        meta = {'endpoint': self.endpoint, 'seconds': round(time.perf_counter() - started, 3)}
        return body['result'], meta


def quality_issues(text: str) -> list[str]:
    """页面级质量检查；返回问题代码列表，空列表表示通过。"""
    issues: list[str] = []
    stripped = (text or '').strip()
    if len(stripped) < MIN_OCR_CHARS:
        issues.append('empty_or_very_short')
        return issues
    lowered = stripped.lower()
    if lowered.count('<table') != lowered.count('</table>'):
        issues.append('unclosed_html_table')
    lines = [re.sub(r'\s+', '', line) for line in stripped.splitlines() if len(line.strip()) >= 15]
    repeated = [line for line, count in Counter(lines).items() if count >= REPEAT_LINE_THRESHOLD]
    if repeated:
        issues.append('repeated_long_lines')
    compact = re.sub(r'\s+', '', stripped)
    if len(compact) > 80:
        chunks = Counter(compact[i:i + 80] for i in range(0, len(compact) - 80, 20))
        if any(count >= REPEAT_SPAN_THRESHOLD for count in chunks.values()):
            issues.append('repeated_long_spans')
    return issues


__all__ = ['OcrClient', 'OcrError', 'OcrTruncated', 'quality_issues', 'MIN_TEXT_CHARS']
