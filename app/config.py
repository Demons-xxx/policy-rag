"""集中配置：.env / 环境变量 / 默认值。所有路径都相对项目根目录。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """极简 .env 读取；已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ROOT / '.env')


def env(name: str, default: str = '') -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == '' else value.strip()


# ---- 目录 ----
DATA_DIR = ROOT / 'data'
STATE_DIR = DATA_DIR / 'state'
RELEASES_DIR = DATA_DIR / 'releases'
DOCS_DIR = DATA_DIR / 'docs'
JOBS_DIR = DATA_DIR / 'jobs'
LOGS_DIR = DATA_DIR / 'logs'
UPLOADS_DIR = DATA_DIR / 'uploads'
VECTOR_DIR = DATA_DIR / 'vector_store'
KNOWLEDGE_DIR = ROOT / 'knowledge'
MODELS_DIR = ROOT / 'models'
WEB_DIST = ROOT / 'web' / 'dist'

ACTIVE_RELEASE = STATE_DIR / 'active_release.json'
CATALOG_DB = STATE_DIR / 'catalog.sqlite'

# ---- 模型与服务 ----
LLM_BASE_URL = env('POLICYRAG_LLM_BASE_URL', 'http://127.0.0.1:8081/v1').rstrip('/')
LLM_API_KEY = env('POLICYRAG_LLM_API_KEY', '')
LLM_MODEL = env('POLICYRAG_LLM_MODEL', 'Qwen3.8-27B')
# 推理型模型是否输出思考过程：disabled（默认，推荐）/ enabled / auto / none（不发送该参数）
LLM_THINKING = env('POLICYRAG_LLM_THINKING', 'disabled').lower()
VECTOR_BACKEND = env('POLICYRAG_VECTOR_BACKEND', 'chroma').lower()
DEVICE = env('POLICYRAG_DEVICE', 'auto').lower()
HOST = env('POLICYRAG_HOST', '127.0.0.1')
PORT = int(env('POLICYRAG_PORT', '7880'))
EMBEDDING_DIR = Path(env('POLICYRAG_EMBEDDING_DIR', str(MODELS_DIR / 'bge-base-zh-v1.5')))

# ---- 问答参数 ----
TOP_K = int(env('POLICYRAG_TOP_K', '5'))
MAX_CONTEXT_TOKENS = int(env('POLICYRAG_MAX_CONTEXT_TOKENS', '4096'))
MAX_HISTORY_CHARS = int(env('POLICYRAG_MAX_HISTORY_CHARS', '6000'))
MAX_HISTORY_MESSAGES = int(env('POLICYRAG_MAX_HISTORY_MESSAGES', '12'))


def ensure_dirs() -> None:
    for path in (DATA_DIR, STATE_DIR, RELEASES_DIR, DOCS_DIR, JOBS_DIR, LOGS_DIR,
                 UPLOADS_DIR, VECTOR_DIR):
        path.mkdir(parents=True, exist_ok=True)


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
