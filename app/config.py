"""配置的模块级视图（兼容层）。

真实来源是 `app.settings.Settings`；这里把字段展开成模块常量，方便既有代码
继续写 `from app import config`。新代码建议直接使用 `load_settings()`。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.settings import Settings, load_settings

_settings: Settings = load_settings()

ROOT = _settings.root

# ---- 目录 ----
DATA_DIR = _settings.data_dir
STATE_DIR = _settings.state_dir
RELEASES_DIR = _settings.releases_dir
DOCS_DIR = _settings.docs_dir
JOBS_DIR = _settings.jobs_dir
LOGS_DIR = _settings.logs_dir
UPLOADS_DIR = _settings.uploads_dir
VECTOR_DIR = _settings.vector_dir
KNOWLEDGE_DIR = _settings.knowledge_dir
MODELS_DIR = _settings.models_dir
WEB_DIST = _settings.web_dist

ACTIVE_RELEASE = _settings.active_release
CATALOG_DB = _settings.catalog_db

# ---- 模型与服务 ----
LLM_BASE_URL = _settings.llm_base_url
LLM_API_KEY = _settings.llm_api_key
LLM_MODEL = _settings.llm_model
# 推理型模型是否输出思考过程：disabled（默认）/ enabled / auto / none（不发送该参数）
LLM_THINKING = _settings.llm_thinking
VECTOR_BACKEND = _settings.vector_backend
DEVICE = _settings.device
HOST = _settings.host
PORT = _settings.port
EMBEDDING_DIR = _settings.embedding_dir

# ---- 问答参数 ----
TOP_K = _settings.top_k
MAX_CONTEXT_TOKENS = _settings.max_context_tokens
MAX_HISTORY_CHARS = _settings.max_history_chars
MAX_HISTORY_MESSAGES = _settings.max_history_messages
# 向量相关性闸门：最佳向量相似度低于该值时视为「知识库没有相关内容」，
# 不拿低分结果凑证据；设 0 关闭。有条款号/文号等精确命中时不受该阈值限制。
RETRIEVAL_MIN_DENSE = _settings.retrieval_min_dense

# ---- 交叉编码器重排 ----
RERANK_ENABLED = _settings.rerank_enabled
RERANK_MODEL_DIR = _settings.rerank_model_dir
RERANK_TOP_N = _settings.rerank_top_n
RERANK_BATCH = _settings.rerank_batch

# ---- 入库解析：扫描件 OCR 与可选 docling 后端 ----
OCR_ENDPOINT = _settings.ocr_endpoint
OCR_DPI = _settings.ocr_dpi
OCR_MIN_CHARS = _settings.ocr_min_chars
OCR_TIMEOUT = _settings.ocr_timeout
OCR_MAX_PAGES = _settings.ocr_max_pages
PARSER_PDF = _settings.parser_pdf
DOCLING_PYTHON = _settings.docling_python
DOCLING_ARTIFACTS = _settings.docling_artifacts
DOCLING_DEVICE = _settings.docling_device
DOCLING_TIMEOUT = _settings.docling_timeout
DOCX_PREVIEW = _settings.docx_preview
DOCX_PREVIEW_TIMEOUT = _settings.docx_preview_timeout
CACHE_SIZE = _settings.cache_size


def env(name: str, default: str = '') -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == '' else value.strip()


def settings() -> Settings:
    """当前配置对象（只读）。"""
    return _settings


def ensure_dirs() -> None:
    _settings.ensure_dirs()


def project_info() -> dict:
    return json.loads((Path(__file__).resolve().parent / 'project.json').read_text(encoding='utf-8'))


def resolve_device() -> str:
    if DEVICE in ('cpu', 'cuda'):
        return DEVICE
    try:
        import torch
        return 'cuda' if torch.cuda.is_available() else 'cpu'
    except Exception:
        return 'cpu'
