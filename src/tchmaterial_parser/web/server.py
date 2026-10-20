# -*- coding: utf-8 -*-
# Web 版的 FastAPI 应用：挂载接口与静态前端，并在启动/停止时管理下载任务

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import jobs
from .api import router
from .paths import ensure_dirs

logger = logging.getLogger("tchmaterial.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    jobs.manager.recover_interrupted() # 上次容器被强制停止时遗留的任务标为中断
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("tchMaterial-parser Web %s 已启动", __version__)
    try:
        yield
    finally:
        jobs.manager.shutdown()

def create_app() -> FastAPI:
    app = FastAPI(
        title="国家中小学智慧教育平台 资源下载工具",
        description="Web 版：浏览资源目录、批量下载电子课本，并管理已下载的教材。",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():
            return JSONResponse(
                {"detail": "前端资源缺失，请使用完整镜像或先构建静态文件", "version": __version__},
                status_code=500,
            )
        return FileResponse(index_file, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        icon = STATIC_DIR / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return JSONResponse({}, status_code=404)

    return app

app = create_app()
