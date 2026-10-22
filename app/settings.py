"""应用配置的唯一来源（Settings 数据类）。

设计目标：让「配置」成为可注入对象，而不是散落在模块里的全局常量——
这样同一套代码可以跑多个知识库实例，也方便测试里替换路径与模型。

`app/config.py` 仍然保留模块级常量（等价于 `load_settings()` 的字段），
兼容现有 `from app import config` 的写法。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def _read_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略注释和空行。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip().lstrip('\ufeff')] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    """一次运行所需的全部配置；环境变量优先于 .env，.env 优先于默认值。"""

    root: Path = DEFAULT_ROOT
    # 数据目录
    data_dir: Path = field(default=None)  # type: ignore[assignment]
    state_dir: Path = field(default=None)  # type: ignore[assignment]
    releases_dir: Path = field(default=None)  # type: ignore[assignment]
    docs_dir: Path = field(default=None)  # type: ignore[assignment]
    jobs_dir: Path = field(default=None)  # type: ignore[assignment]
    logs_dir: Path = field(default=None)  # type: ignore[assignment]
    uploads_dir: Path = field(default=None)  # type: ignore[assignment]
    vector_dir: Path = field(default=None)  # type: ignore[assignment]
    knowledge_dir: Path = field(default=None)  # type: ignore[assignment]
    models_dir: Path = field(default=None)  # type: ignore[assignment]
    web_dist: Path = field(default=None)  # type: ignore[assignment]
    active_release: Path = field(default=None)  # type: ignore[assignment]
    catalog_db: Path = field(default=None)  # type: ignore[assignment]
    # 生成模型
    llm_base_url: str = 'http://127.0.0.1:8081/v1'
    llm_api_key: str = ''
    llm_model: str = 'Qwen3.8-27B'
    llm_thinking: str = 'disabled'
    llm_retries: int = 2
    llm_first_token_timeout: float = 60.0
    # 检索与向量
    vector_backend: str = 'chroma'
    device: str = 'auto'
    embedding_dir: Path = field(default=None)  # type: ignore[assignment]
    top_k: int = 5
    max_context_tokens: int = 4096
    retrieval_min_dense: float = 0.45
    # 交叉编码器重排（bge-reranker-v2-m3）；模型缺失时自动回退原排序
    rerank_enabled: bool = True
    rerank_model_dir: Path = field(default=None)  # type: ignore[assignment]
    rerank_top_n: int = 20
    rerank_batch: int = 16
    # 入库解析：扫描件 OCR 与可选 docling 后端
    ocr_endpoint: str = ''
    ocr_dpi: int = 200
    ocr_min_chars: int = 20
    ocr_timeout: int = 180
    ocr_max_pages: int = 100
    parser_pdf: str = 'pymupdf'
    docling_python: Path = field(default=None)  # type: ignore[assignment]
    docling_artifacts: Path = field(default=None)  # type: ignore[assignment]
    docling_device: str = 'cuda'
    docling_timeout: int = 600
    docx_preview: bool = True
    docx_preview_timeout: int = 120
    cache_size: int = 256
    # 服务
    host: str = '127.0.0.1'
    port: int = 7880
    # 对话
    max_history_chars: int = 6000
    max_history_messages: int = 12

    def with_overrides(self, **changes) -> 'Settings':
        return replace(self, **changes)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.state_dir, self.releases_dir, self.docs_dir,
                     self.jobs_dir, self.logs_dir, self.uploads_dir, self.vector_dir):
            Path(path).mkdir(parents=True, exist_ok=True)


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: str, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def load_settings(root: Path | None = None, env_file: Path | None = None) -> Settings:
    """从 .env + 环境变量构建 Settings；环境变量优先。"""
    root = Path(root or DEFAULT_ROOT).resolve()
    file_values = _read_dotenv(Path(env_file) if env_file else root / '.env')

    def get(name: str, default: str = '') -> str:
        value = os.environ.get(name)
        if value is None or value.strip() == '':
            value = file_values.get(name, default)
        return '' if value is None else str(value).strip()

    data_dir = root / 'data'
    models_dir = root / 'models'
    state_dir = data_dir / 'state'
    embedding_default = models_dir / 'bge-base-zh-v1.5'
    embedding = Path(get('POLICYRAG_EMBEDDING_DIR', '') or embedding_default)
    if not embedding.is_absolute():
        embedding = root / embedding
    rerank_default = models_dir / 'bge-reranker-v2-m3'
    rerank_dir = Path(get('POLICYRAG_RERANK_MODEL_DIR', '') or rerank_default)
    if not rerank_dir.is_absolute():
        rerank_dir = root / rerank_dir
    # 同盘 RAG 项目里的 docling 运行时与模型：存在就默认复用，不存在则留空。
    sibling = root.parent / 'RAG'
    docling_runtime = (sibling / 'local_service' / 'runtime' / 'parse' / 'Scripts' / 'python.exe'
                       if os.name == 'nt'
                       else sibling / 'script' / 'runtime' / 'parse' / 'bin' / 'python')
    docling_python = Path(get('POLICYRAG_DOCLING_PYTHON', '') or docling_runtime)
    if str(docling_python) and not docling_python.is_absolute():
        docling_python = root / docling_python
    docling_artifacts = Path(get('POLICYRAG_DOCLING_ARTIFACTS', '') or (sibling / 'model' / 'docling'))
    if not docling_artifacts.is_absolute():
        docling_artifacts = root / docling_artifacts

    return Settings(
        root=root,
        data_dir=data_dir,
        state_dir=state_dir,
        releases_dir=data_dir / 'releases',
        docs_dir=data_dir / 'docs',
        jobs_dir=data_dir / 'jobs',
        logs_dir=data_dir / 'logs',
        uploads_dir=data_dir / 'uploads',
        vector_dir=data_dir / 'vector_store',
        knowledge_dir=root / 'knowledge',
        models_dir=models_dir,
        web_dist=root / 'web' / 'dist',
        active_release=state_dir / 'active_release.json',
        catalog_db=state_dir / 'catalog.sqlite',
        llm_base_url=get('POLICYRAG_LLM_BASE_URL', 'http://127.0.0.1:8081/v1').rstrip('/'),
        llm_api_key=get('POLICYRAG_LLM_API_KEY', ''),
        llm_model=get('POLICYRAG_LLM_MODEL', 'Qwen3.8-27B'),
        llm_thinking=get('POLICYRAG_LLM_THINKING', 'disabled').lower(),
        llm_retries=_as_int(get('RAG_LLM_RETRIES', '2'), 2),
        llm_first_token_timeout=_as_float(get('RAG_LLM_FIRST_TOKEN_TIMEOUT', '60'), 60.0),
        vector_backend=get('POLICYRAG_VECTOR_BACKEND', 'chroma').lower(),
        device=get('POLICYRAG_DEVICE', 'auto').lower(),
        embedding_dir=embedding,
        top_k=_as_int(get('POLICYRAG_TOP_K', '5'), 5),
        max_context_tokens=_as_int(get('POLICYRAG_MAX_CONTEXT_TOKENS', '4096'), 4096),
        retrieval_min_dense=_as_float(get('POLICYRAG_RETRIEVAL_MIN_DENSE', '0.45'), 0.45),
        rerank_enabled=get('POLICYRAG_RERANK', '1').lower() not in ('0', 'false', 'off', 'no'),
        rerank_model_dir=rerank_dir,
        rerank_top_n=_as_int(get('POLICYRAG_RERANK_TOP_N', '20'), 20),
        rerank_batch=_as_int(get('POLICYRAG_RERANK_BATCH', '16'), 16),
        ocr_endpoint=get('POLICYRAG_OCR_ENDPOINT', ''),
        ocr_dpi=_as_int(get('POLICYRAG_OCR_DPI', '200'), 200),
        ocr_min_chars=_as_int(get('POLICYRAG_OCR_MIN_CHARS', '20'), 20),
        ocr_timeout=_as_int(get('POLICYRAG_OCR_TIMEOUT', '180'), 180),
        ocr_max_pages=_as_int(get('POLICYRAG_OCR_MAX_PAGES', '100'), 100),
        parser_pdf=get('POLICYRAG_PARSER_PDF', 'pymupdf').lower(),
        docling_python=docling_python,
        docling_artifacts=docling_artifacts,
        docling_device=get('POLICYRAG_DOCLING_DEVICE', 'cuda').lower(),
        docling_timeout=_as_int(get('POLICYRAG_DOCLING_TIMEOUT', '600'), 600),
        docx_preview=get('POLICYRAG_DOCX_PREVIEW', '1').lower() not in ('0', 'false', 'off', 'no'),
        docx_preview_timeout=_as_int(get('POLICYRAG_DOCX_PREVIEW_TIMEOUT', '120'), 120),
        cache_size=_as_int(get('RAG_CACHE_SIZE', '256'), 256),
        host=get('POLICYRAG_HOST', '127.0.0.1'),
        port=_as_int(get('POLICYRAG_PORT', '7880'), 7880),
        max_history_chars=_as_int(get('POLICYRAG_MAX_HISTORY_CHARS', '6000'), 6000),
        max_history_messages=_as_int(get('POLICYRAG_MAX_HISTORY_MESSAGES', '12'), 12),
    )
