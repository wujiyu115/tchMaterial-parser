#!/bin/sh
# 容器入口：按 PUID/PGID 调整 /data 属主，必要时降权运行，然后启动 Web 服务。
#
# 默认以 root 运行（PUID=0），适合 Unraid 直接挂载 appdata；
# 设置 PUID/PGID（如 Unraid 常见的 99:100）后会 chown 挂载目录并降权运行，
# 避免宿主机上出现 root 属主的教材文件。setpriv 来自 util-linux，Debian slim 自带。

set -e

PUID="${PUID:-0}"
PGID="${PGID:-0}"
DATA_DIR="${TCHMATERIAL_DATA_DIR:-/data}"

mkdir -p "$DATA_DIR/downloads"

if [ "$(id -u)" = "0" ] && { [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; }; then
  chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
