"""扫描件 OCR 兜底与 docling 可选后端的最小回归测试。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import config
from app.ingest import ocr, parser


def _scanned_pdf(path: Path) -> Path:
    """生成一份「只有图片、没有文本层」的 PDF。"""
    import fitz

    source = fitz.open()
    page = source.new_page()
    page.insert_text((72, 100), "第一条  实验室危险化学品领用须经导师审批。", fontsize=16)
    pixmap = page.get_pixmap(dpi=150)

    image_only = fitz.open()
    target = image_only.new_page(width=pixmap.width, height=pixmap.height)
    target.insert_image(target.rect, stream=pixmap.tobytes("png"))
    image_only.save(str(path))
    image_only.close()
    source.close()
    return path


class _OcrHandler(BaseHTTPRequestHandler):
    result = "第一条  实验室危险化学品领用须经导师审批。\n\n| 审批人 | 时限 |\n| --- | --- |\n| 导师 | 3 个工作日 |"

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"code": 0, "result": self.result}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默测试日志
        pass


@pytest.fixture()
def ocr_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OcrHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/ocr/markdown"
    server.shutdown()
    server.server_close()


def test_scanned_pdf_uses_ocr_endpoint(tmp_path, monkeypatch, ocr_server):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    monkeypatch.setattr(config, "OCR_ENDPOINT", ocr_server)
    monkeypatch.setattr(config, "OCR_MIN_CHARS", 20)
    parsed = parser.parse_document(pdf)
    assert parsed["ocr_pages"] == 1
    page = parsed["pages"][0]
    assert page["ocr"] is True
    assert "导师审批" in page["text"]
    assert "3 个工作日" in page["text"]


def test_scanned_pdf_without_endpoint_stays_empty(tmp_path, monkeypatch):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    monkeypatch.setattr(config, "OCR_ENDPOINT", "")
    parsed = parser.parse_document(pdf)
    assert parsed["pages"][0]["text"].strip() == ""


def test_ocr_client_rejects_failed_response(monkeypatch, tmp_path):
    class Bad(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.dumps({"code": 7, "message": "模型未加载"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        image = tmp_path / "page.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        client = ocr.OcrClient(f"http://127.0.0.1:{server.server_address[1]}/", timeout=5)
        with pytest.raises(ocr.OcrError):
            client.recognize(image)
    finally:
        server.shutdown()
        server.server_close()


def test_docling_backend_reports_missing_runtime(monkeypatch, tmp_path):
    from app.ingest import docling

    monkeypatch.setattr(config, "DOCLING_PYTHON", tmp_path / "missing-python.exe")
    monkeypatch.setattr(docling, "available", lambda: False)
    assert docling.available() is False
    with pytest.raises(RuntimeError, match="docling"):
        docling.parse_pdf(tmp_path / "x.pdf")
