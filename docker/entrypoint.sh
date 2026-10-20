#!/bin/sh
# 容器入口：按 PUID/PGID 调整 /data 属主，必要时降权运行，然后启动 Web 服务。
#
# 默认以 root 运行（PUID=0），适合直接挂载 appdata；
# 设置 PUID/PGID（如 Unraid 常见的 99:100）后会修正挂载目录属主并降权运行，
# 避免宿主机上出现 root 属主的教材文件。setpriv 来自 util-linux，Debian slim 自带。

set -e

PUID="${PUID:-0}"
PGID="${PGID:-0}"
DATA_DIR="${TCHMATERIAL_DATA_DIR:-/data}"
DOWNLOADS_DIR="$DATA_DIR/downloads"

mkdir -p "$DOWNLOADS_DIR"

if [ "$(id -u)" = "0" ] && { [ "$PUID" != "0" ] || [ "$PGID" != "0" ]; }; then
  if [ "$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null)" != "$PUID:$PGID" ]; then
    # 首次启动（含已存在的教材）时递归修正一次；此后应用以该用户写入，无需再扫描
    chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || true
  else
    # 属主已正确，只需修正本次由 root 创建的目录，避免深目录遍历
    chown "$PUID:$PGID" "$DOWNLOADS_DIR" 2>/dev/null || true
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
