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


#: 本地 RapidOCR 引擎（进程内单例，避免每页重复加载模型）
_LOCAL_ENGINE = None


def _local_engine(model_dir: Path | str | None):
    global _LOCAL_ENGINE
    if _LOCAL_ENGINE is not None:
        return _LOCAL_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - 环境已装
        raise OcrError(
            '本地 OCR 不可用：未安装 rapidocr-onnxruntime'
            '（pip install rapidocr-onnxruntime，或设置 POLICYRAG_OCR_LOCAL=0 关闭）'
        ) from exc
    root = Path(model_dir or '')
    names = {
        'det_model_path': 'ch_PP-OCRv4_det_infer.onnx',
        'rec_model_path': 'ch_PP-OCRv4_rec_infer.onnx',
        'cls_model_path': 'ch_ppocr_mobile_v2.0_cls_infer.onnx',
    }
    kwargs = {key: str(root / name) for key, name in names.items() if (root / name).is_file()}
    if kwargs:
        _LOCAL_ENGINE = RapidOCR(**kwargs)
    else:
        _LOCAL_ENGINE = RapidOCR()
    return _LOCAL_ENGINE


class LocalOcrClient:
    """本地 OCR（RapidOCR / ONNX Runtime）：一次一页，返回纯文本行。

    权重默认 `models/rapidocr/*.onnx`（项目自带），缺失时回退 RapidOCR 包内权重。
    与 `OcrClient` 同签名，入库流程按 `POLICYRAG_OCR_ENDPOINT` 是否配置切换。
    """

    def __init__(self, model_dir: Path | str | None = None, min_score: float = 0.3) -> None:
        self.model_dir = model_dir
        self.min_score = min_score

    def __call__(self, image: Path) -> tuple[str, dict]:
        return self.recognize(image)

    def recognize(self, image: Path) -> tuple[str, dict]:
        engine = _local_engine(self.model_dir)
        started = time.time()
        try:
            result, _elapse = engine(str(image))
        except Exception as exc:  # noqa: BLE001 - 引擎内部错误统一转中文
            raise OcrError(f'本地 OCR 失败：{exc}') from exc
        rows = [row for row in (result or []) if len(row) >= 3]
        rows = [row for row in rows if float(row[2]) >= self.min_score]
        # 按阅读顺序排序：先上后下、同一行从左到右
        rows.sort(key=lambda row: (min(point[1] for point in row[0]),
                                   min(point[0] for point in row[0])))
        lines = [str(row[1]).strip() for row in rows if str(row[1]).strip()]
        scores = [float(row[2]) for row in rows]
        meta = {
            'engine': 'rapidocr',
            'seconds': round(time.time() - started, 3),
            'lines': len(lines),
            'avg_score': round(sum(scores) / len(scores), 4) if scores else 0.0,
        }
        return '\n'.join(lines), meta


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
