# Web 版镜像：单阶段构建，只包含运行时所需依赖。
#
# 下载引擎复用桌面版模块，因此不需要 tkinter（slim 镜像本身也没有）；
# 教材文件、下载记录与登录凭据统一放在 /data，便于通过挂载持久化。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HOST=0.0.0.0 \
    PORT=8080 \
    HOME=/data \
    TCHMATERIAL_DATA_DIR=/data

WORKDIR /app

# curl 用于健康检查；tzdata 让日志与文件时间显示为本地时区
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，源码变更时可复用这层缓存
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY src/ ./src/
COPY pyproject.toml README.md LICENSE ./
COPY licenses/ ./licenses/

# 以可编辑方式安装，使 importlib.metadata 能读到版本号（__init__.py 依赖它）
RUN pip install --no-cache-dir --no-deps -e .

# 运行期用户：默认以 root 运行会与宿主机挂载目录的属主不一致，
# 因此使用 PUID/PGID 可配置的入口脚本在启动时调整。
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /data/downloads

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "tchmaterial_parser.web"]
