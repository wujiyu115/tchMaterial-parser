# -*- coding: utf-8 -*-
# Web 版入口：python -m tchmaterial_parser.web [--host 0.0.0.0] [--port 8080]
#
# 也可以直接用环境变量配置：HOST / PORT。

from __future__ import annotations

import argparse, os

def main() -> None:
    parser = argparse.ArgumentParser(description="国家中小学智慧教育平台 资源下载工具（Web 版）")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"), help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")), help="监听端口，默认 8080")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更后自动重载")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "tchmaterial_parser.web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )

if __name__ == "__main__":
    main()
