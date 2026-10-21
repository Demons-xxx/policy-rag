"""FastAPI 入口：挂载 API 路由与前端构建产物。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config

API_PREFIX = '/api'


def create_app() -> FastAPI:
    config.ensure_dirs()
    info = config.project_info()
    app = FastAPI(title=info.get('name', 'PolicyRAG'), version=info.get('version', '0.0.0'))

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
