# -*- coding: utf-8 -*-
# Web 版的数据目录：下载的教材、下载记录与设置都放在同一个可挂载目录中
#
# 容器里通过 -v /mnt/user/...:/data 挂载，并把环境变量 TCHMATERIAL_DATA_DIR 设为 /data，
# 这样教材、记录与登录凭据（HOME=/data 时的 ~/.config）都会落在宿主机上。

import os
from pathlib import Path

DEFAULT_DATA_DIR_NAME = "tchMaterial-parser"

def data_dir() -> Path: # 数据根目录，可由环境变量覆盖
    raw = os.getenv("TCHMATERIAL_DATA_DIR") or os.getenv("DATA_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share" / DEFAULT_DATA_DIR_NAME

def downloads_dir() -> Path: # 已下载教材的存放目录
    return data_dir() / "downloads"

def records_file() -> Path: # 下载记录（任务历史）文件
    return data_dir() / "records.json"

def settings_file() -> Path: # Web 界面的设置文件
    return data_dir() / "settings.json"

def ensure_dirs() -> None: # 确保下载目录存在
    downloads_dir().mkdir(parents=True, exist_ok=True)
