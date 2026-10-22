"""FastAPI 入口：挂载 API 路由与前端构建产物。"""
from __future__ import annotations

import logging
import os
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config

API_PREFIX = '/api'
logger = logging.getLogger(__name__)


def _should_warmup() -> bool:
    """是否在启动后预热模型：测试进程跳过，避免测试里加载 GB 级模型。"""
    if os.environ.get('POLICYRAG_WARMUP', '1').lower() in ('0', 'false', 'off', 'no'):
        return False
    return 'pytest' not in sys.modules


def _warmup() -> None:
    """后台预热编码器与重排模型，把首次提问的模型加载时间挪到启动阶段。"""
    try:
        from app.wiring import runtime
        retriever = runtime.retriever()
        if retriever is not None:
            tokenizer = getattr(retriever.encoder, 'tokenizer', None)  # 触发编码器加载
            del tokenizer
    except Exception as exc:  # 预热失败不影响服务
        logger.warning('检索预热失败：%s', exc)
    try:
        from app import config
        if config.RERANK_ENABLED:
            from app.retrieval.rerank import get_reranker
            get_reranker()
    except Exception as exc:
        logger.warning('重排预热失败：%s', exc)


def create_app() -> FastAPI:
    config.ensure_dirs()
    info = config.project_info()
    app = FastAPI(title=info.get('name', 'PolicyRAG'), version=info.get('version', '0.0.0'))

    if _should_warmup():
        threading.Thread(target=_warmup, name='model-warmup', daemon=True).start()

    from app.api import ask, documents, ingest, info as info_api
    app.include_router(info_api.router, prefix=API_PREFIX)
    app.include_router(ask.router, prefix=API_PREFIX)
    app.include_router(documents.router, prefix=API_PREFIX)
    app.include_router(ingest.router, prefix=API_PREFIX)

    @app.get('/healthz')
    def healthz():
        return {'ok': True}

    if config.WEB_DIST.is_dir():
        assets = config.WEB_DIST / 'assets'
        if assets.is_dir():
            app.mount('/assets', StaticFiles(directory=assets), name='assets')

        @app.get('/')
        def index():
            return FileResponse(config.WEB_DIST / 'index.html')

        @app.get('/{path:path}')
        def spa(path: str):
            root = config.WEB_DIST.resolve()
            target = (config.WEB_DIST / path).resolve()
            if target.is_relative_to(root) and target.is_file():
                return FileResponse(target)
            return FileResponse(config.WEB_DIST / 'index.html')

    else:
        @app.get('/')
        def placeholder():
            return JSONResponse({'name': info.get('name'), 'web': '前端尚未构建，运行 web 目录下 npm run build'})

    return app


app = create_app()
