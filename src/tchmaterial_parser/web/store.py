# -*- coding: utf-8 -*-
# 下载记录与教材库：记录保存在数据目录的 records.json，教材文件保存在 downloads 目录
#
# 教材库以「磁盘上真实存在的文件」为准，再用记录补充标题、版别、完成时间等元数据。
# 这样用户直接在宿主机上删除文件后，界面不会显示已经不存在的教材。

import json, os, threading, time
import uuid
from pathlib import Path

from ..ui.download_panel import redact_access_token
from .paths import downloads_dir, ensure_dirs, records_file

_lock = threading.RLock()
MAX_RECORDS = 500 # 只保留最近的任务记录，避免文件无限增长

STATUS_LABELS = {
    "queued": "排队中",
    "parsing": "正在解析",
    "downloading": "下载中",
    "bookmarking": "写入书签",
    "completed": "已完成",
    "skipped": "已存在",
    "failed": "下载失败",
    "stopped": "已停止",
}

def _now() -> float: # 统一使用秒级时间戳，便于前端格式化
    return time.time()

def _read_raw() -> list[dict]: # 读取记录文件；损坏或不存在时返回空列表
    path = records_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict): # 兼容 {"records": [...]} 结构
        data = data.get("records")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]

def _write_raw(records: list[dict]) -> None: # 原子写入，避免容器重启留下半个 JSON
    path = records_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(records[-MAX_RECORDS:], f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)

def load_records() -> list[dict]: # 读取全部记录（按创建时间升序）
    with _lock:
        return _read_raw()

def save_records(records: list[dict]) -> None: # 覆盖写入全部记录
    with _lock:
        _write_raw(records)

def _redact(value: str | None) -> str | None: # 记录里不得出现凭据
    return redact_access_token(value) if isinstance(value, str) else value

def new_record(title: str, download_url: str = "", **fields) -> dict: # 构造一条新记录
    record = {
        "id": uuid.uuid4().hex[:12],
        "title": title,
        "download_url": _redact(download_url) or "",
        "status": "queued",
        "file": "",          # 相对 downloads 目录的路径
        "file_format": "",
        "size": 0,
        "edition": None,
        "relative_dir": [],
        "chapters": 0,
        "error": None,
        "created_at": _now(),
        "finished_at": None,
    }
    record.update(fields)
    return record

def upsert_record(record: dict) -> None: # 新增或按 id 更新一条记录
    with _lock:
        records = _read_raw()
        for index, existing in enumerate(records):
            if existing.get("id") == record.get("id"):
                merged = {**existing, **record}
                for key in ("title", "download_url", "error"): # 这些字段可能含凭据或需保持干净
                    merged[key] = _redact(merged.get(key))
                records[index] = merged
                break
        else:
            record = dict(record)
            for key in ("title", "download_url", "error"):
                record[key] = _redact(record.get(key))
            records.append(record)
        _write_raw(records)

def delete_records(record_ids: set[str]) -> int: # 按 id 删除记录，返回删除数量
    with _lock:
        records = _read_raw()
        kept = [item for item in records if item.get("id") not in record_ids]
        removed = len(records) - len(kept)
        if removed:
            _write_raw(kept)
        return removed

def relative_file(path: Path) -> str: # 把绝对路径转成相对 downloads 目录的 POSIX 路径
    try:
        return path.resolve().relative_to(downloads_dir().resolve()).as_posix()
    except (ValueError, OSError):
        return ""

def safe_target(relative_path: str) -> Path | None: # 把相对路径解析回磁盘路径，并阻止目录穿越
    if not relative_path:
        return None
    root = downloads_dir().resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate

def _record_index(records: list[dict]) -> dict[str, dict]: # 文件相对路径 -> 最近一条成功记录
    index: dict[str, dict] = {}
    for record in sorted(records, key=lambda item: item.get("finished_at") or 0):
        if record.get("file"):
            index[record["file"]] = record
    return index

def scan_library() -> list[dict]: # 扫描下载目录，结合记录生成教材库列表
    ensure_dirs()
    root = downloads_dir()
    records = load_records()
    index = _record_index(records)
    items: list[dict] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        relative = path.relative_to(root).as_posix()
        record = index.get(relative, {})
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "file": relative,
            "name": path.name,
            "title": record.get("title") or path.stem,
            "file_format": (record.get("file_format") or path.suffix.lstrip(".")).lower(),
            "size": stat.st_size,
            "edition": record.get("edition"),
            "relative_dir": relative.split("/")[:-1],
            "modified_at": stat.st_mtime,
            "record_id": record.get("id"),
            "download_url": record.get("download_url"),
        })

    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return items

def library_entry(relative_path: str) -> dict | None: # 读取单个教材文件的库信息
    target = safe_target(relative_path)
    if target is None or not target.is_file():
        return None
    record = _record_index(load_records()).get(relative_path, {})
    stat = target.stat()
    return {
        "file": relative_path,
        "name": target.name,
        "title": record.get("title") or target.stem,
        "file_format": (record.get("file_format") or target.suffix.lstrip(".")).lower(),
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }
