# -*- coding: utf-8 -*-
# Web 版的 HTTP 接口：资源目录、解析、下载任务、教材库与设置
#
# 设计要点：
# - 教材文件与下载记录都在挂载目录中，容器重建后依然存在；
# - 教材下载走「浏览器下载」接口，由服务端读文件流式返回，支持 Range 以便 PDF 预览；
# - 登录凭据只保存在挂载目录的配置文件里，接口从不回显明文。

from __future__ import annotations

import hashlib, mimetypes
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import __version__, config
from ..auth import TokenInputError, format_token_json
from ..catalog import ResourceHelper, build_resource_url, count_resource_items, filter_resource_items
from ..ui import download_panel as panel
from . import jobs, store
from .paths import data_dir, downloads_dir, ensure_dirs, settings_file

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------- 资源目录

_catalog_cache: dict = {"data": None, "error": None, "loading": False, "at": 0.0}

def load_catalog(force: bool = False) -> dict:
    """拉取平台资源目录。目录较大且变化不频繁，成功一次后即缓存。"""
    if _catalog_cache["data"] is not None and not force:
        return _catalog_cache["data"]
    try:
        data = ResourceHelper().fetch_resource_list()
        _catalog_cache.update(data=data, error=None)
    except Exception as error:
        _catalog_cache["error"] = str(error)
        if _catalog_cache["data"] is None:
            raise
    return _catalog_cache["data"] or {}

def flatten_catalog(items: dict, parent_path: str = "") -> list[dict]: # 展平资源树，供前端搜索与列表渲染
    rows: list[dict] = []
    for option_id, option_data in items.items():
        item_path = f"{parent_path}:{option_id}" if parent_path else option_id
        children = option_data.get("children") or {}
        thumbnails = (option_data.get("custom_properties") or {}).get("thumbnails") or []
        if children:
            rows.append({
                "path": item_path,
                "name": option_data.get("display_name") or option_id,
                "is_leaf": False,
                "count": count_resource_items(children),
            })
            rows.extend(flatten_catalog(children, item_path))
        else:
            rows.append({
                "path": item_path,
                "name": option_data.get("display_name") or option_id,
                "is_leaf": True,
                "count": 1,
                "url": build_resource_url(item_path, option_data),
                "thumbnail": thumbnails[0] if thumbnails else None,
            })
    return rows

@router.get("/catalog")
def get_catalog(q: str = "", limit: int = Query(300, ge=1, le=2000), refresh: bool = False) -> dict:
    """返回资源目录（扁平列表，主要用于搜索）。带 q 时按完整分类路径筛选。"""
    try:
        tree = load_catalog(force=refresh)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"获取资源列表失败：{error}")

    visible = filter_resource_items(tree, q) if q else tree
    rows = flatten_catalog(visible)
    return {
        "total": count_resource_items(visible),
        "returned": len(rows[:limit]),
        "truncated": len(rows) > limit,
        "items": rows[:limit],
        "leaves": sum(1 for row in rows if row["is_leaf"]),
    }

def _find_branch(tree: dict, path: str) -> dict:
    """按冒号分隔的路径定位子树；路径为空时返回根。"""
    branch = tree
    for segment in [part for part in path.split(":") if part]:
        node = branch.get(segment)
        if not isinstance(node, dict):
            raise HTTPException(status_code=404, detail="分类不存在，请刷新资源目录")
        branch = node.get("children") or {}
    return branch

@router.get("/catalog/children")
def get_catalog_children(path: str = "", refresh: bool = False) -> dict:
    """返回某一层分类下的直接子项，供前端逐层展开浏览。"""
    try:
        tree = load_catalog(force=refresh)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"获取资源列表失败：{error}")

    items: list[dict] = []
    for option_id, option_data in _find_branch(tree, path).items():
        item_path = f"{path}:{option_id}" if path else option_id
        children = option_data.get("children") or {}
        thumbnails = (option_data.get("custom_properties") or {}).get("thumbnails") or []
        if children:
            items.append({
                "path": item_path,
                "name": option_data.get("display_name") or option_id,
                "is_leaf": False,
                "count": count_resource_items(children),
            })
        else:
            items.append({
                "path": item_path,
                "name": option_data.get("display_name") or option_id,
                "is_leaf": True,
                "count": 1,
                "url": build_resource_url(item_path, option_data),
                "thumbnail": thumbnails[0] if thumbnails else None,
            })
    return {"path": path, "items": items, "count": len(items)}

# ---------------------------------------------------------------- 解析

class ParseRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    bookmarks: bool = True

@router.post("/parse")
def parse_links(payload: ParseRequest) -> dict:
    """解析资源页面链接，返回可下载的资源（不落盘）。"""
    urls = [line.strip() for line in payload.urls if line and line.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="请至少提供一个资源页面链接")
    resources, failed = jobs.manager.parse_urls(urls, payload.bookmarks)
    return {
        "resources": [
            {
                "title": resource.title,
                "url": panel.redact_access_token(resource.url),
                "file_format": resource.file_format,
                "edition": resource.edition,
                "relative_dir": list(resource.relative_dir),
                "chapters": len(resource.chapters or []),
            }
            for resource in resources
        ],
        "failed": failed,
    }

# ---------------------------------------------------------------- 下载任务

class DownloadRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    bookmarks: bool = True

@router.post("/downloads")
def create_downloads(payload: DownloadRequest) -> dict:
    """解析链接并立即开始下载，返回新建的任务记录。"""
    urls = [line.strip() for line in payload.urls if line and line.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="请至少提供一个资源页面链接")
    if config.access_token and not config.access_token.isascii():
        raise HTTPException(status_code=400, detail="Access Token 不正确（包含非 ASCII 字符），请重新设置")

    resources, failed = jobs.manager.parse_urls(urls, payload.bookmarks)
    if not resources:
        detail = "未能从这些链接中解析出可下载的资源"
        if failed:
            detail += f"，失败 {len(failed)} 条"
        raise HTTPException(status_code=422, detail=detail)

    created = jobs.manager.start_batch(resources, payload.bookmarks)
    return {"created": len(created), "failed": failed, "tasks": created}

@router.get("/downloads")
def list_downloads() -> dict:
    tasks = jobs.manager.tasks()
    return {
        "tasks": tasks,
        "active": jobs.manager.active_count(),
        "labels": store.STATUS_LABELS,
    }

@router.get("/downloads/{record_id}")
def get_download(record_id: str) -> dict:
    task = jobs.manager.task_view(record_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    return task

@router.post("/downloads/{record_id}/stop")
def stop_download(record_id: str) -> dict:
    if not jobs.manager.stop(record_id):
        raise HTTPException(status_code=409, detail="任务已结束，无需停止")
    return {"stopped": True}

@router.post("/downloads/stop-all")
def stop_all_downloads() -> dict:
    return {"stopped": jobs.manager.stop_all()}

@router.post("/downloads/clear-finished")
def clear_finished() -> dict:
    return {"cleared": jobs.manager.clear_finished()}

# ---------------------------------------------------------------- 教材库

@router.get("/library")
def list_library(q: str = "") -> dict:
    """已下载的教材列表（以磁盘实际文件为准）。"""
    items = store.scan_library()
    if q:
        keyword = q.casefold()
        items = [item for item in items if keyword in (item["title"] or "").casefold() or keyword in item["file"].casefold()]
    total_size = sum(item["size"] for item in items)
    return {"items": items, "count": len(items), "total_size": total_size}

@router.get("/library/search")
def search_library(q: str = Query(..., min_length=1)) -> dict: # 与列表接口一致的搜索，便于前端语义化调用
    return list_library(q=q)

def _resolve_library_file(relative_path: str):
    entry = store.library_entry(relative_path)
    if entry is None:
        raise HTTPException(status_code=404, detail="教材文件不存在")
    target = store.safe_target(relative_path)
    if target is None:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    return entry, target

@router.api_route("/library/file", methods=["GET", "HEAD"])
def download_library_file(relative_path: str, inline: bool = False):
    """下载（或内联预览）已下载的教材，支持 Range 请求断点续传。

    显式允许 HEAD：内联预览器与下载器常先探测文件是否存在。
    """
    entry, target = _resolve_library_file(relative_path)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    filename = target.name
    disposition = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"}
    return FileResponse(
        target,
        media_type=media_type,
        headers=headers,
        content_disposition_type=disposition,
        filename=filename,
    )

@router.get("/library/thumbnail")
def library_thumbnail(relative_path: str):
    """生成 PDF 首页缩略图（作为书封）；未安装渲染组件时返回 404，前端自动降级为占位图标。"""
    entry, target = _resolve_library_file(relative_path)
    if entry["file_format"] != "pdf":
        raise HTTPException(status_code=404, detail="仅支持 PDF 缩略图")
    try:
        import pymupdf  # 仅此一处用到，延迟导入以免影响启动速度
    except ImportError:
        raise HTTPException(status_code=404, detail="未安装缩略图组件")

    cache = data_dir() / "thumbnails"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise HTTPException(status_code=500, detail="无法创建缩略图缓存目录")

    # 缓存名带上文件指纹：教材被重新下载（大小或时间变化）时自动重新生成
    fingerprint = f"{entry['file']}|{entry['size']}|{int(entry['modified_at'])}"
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()
    cached = cache / f"{digest}.png"
    if not cached.is_file():
        try:
            with pymupdf.open(target) as doc:
                if doc.page_count == 0:
                    raise HTTPException(status_code=404, detail="PDF 没有页面")
                pixmap = doc.load_page(0).get_pixmap(matrix=pymupdf.Matrix(0.7, 0.7))
                pixmap.save(cached)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=500, detail="生成缩略图失败")
    return FileResponse(cached, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

class DeleteFilesRequest(BaseModel):
    files: list[str] = Field(default_factory=list)

@router.delete("/library/file")
def delete_library_file(relative_path: str) -> dict:
    entry, target = _resolve_library_file(relative_path)
    try:
        target.unlink()
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"删除失败：{error}")
    return {"deleted": entry["file"]}

@router.delete("/library")
def delete_library_files(payload: DeleteFilesRequest) -> dict:
    deleted: list[str] = []
    missing: list[str] = []
    for relative_path in payload.files:
        target = store.safe_target(relative_path)
        if target is None or not target.is_file():
            missing.append(relative_path)
            continue
        try:
            target.unlink()
            deleted.append(relative_path)
        except OSError:
            missing.append(relative_path)
    return {"deleted": deleted, "missing": missing}

# ---------------------------------------------------------------- 记录

@router.get("/records")
def list_records(limit: int = Query(200, ge=1, le=1000)) -> dict:
    """下载记录（持久化），包含历史任务与当前任务。"""
    persisted = store.load_records()
    persisted.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    live = {task["id"]: task for task in jobs.manager.tasks()}
    merged: list[dict] = []
    seen: set[str] = set()
    for record in persisted[:limit]:
        entry = live.get(record.get("id"), record)
        entry = {**record, **entry}
        entry["status_label"] = store.STATUS_LABELS.get(entry.get("status", ""), entry.get("status", ""))
        merged.append(entry)
        seen.add(record.get("id"))
    for record_id, task in live.items(): # 刚创建但尚未写入文件的记录
        if record_id not in seen:
            task["status_label"] = store.STATUS_LABELS.get(task.get("status", ""), task.get("status", ""))
            merged.append(task)
    return {"records": merged[:limit], "count": len(merged)}

@router.delete("/records")
def clear_records() -> dict:
    """清空下载记录（不删除教材文件）。"""
    count = len(store.load_records())
    store.save_records([])
    return {"cleared": count}

# ---------------------------------------------------------------- 设置 / 凭据

class TokenRequest(BaseModel):
    token: str = ""

def _load_settings() -> dict:
    import json
    try:
        with open(settings_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _save_settings(updates: dict) -> None:
    import json
    data = _load_settings()
    data.update(updates)
    ensure_dirs()
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

@router.get("/settings")
def get_settings() -> dict:
    """返回设置与登录状态。绝不回显凭据明文。"""
    saved = _load_settings()
    return {
        "version": __version__,
        "logged_in": bool(config.access_token),
        "has_mac_key": bool(config.mac_key),
        "bookmarks": saved.get("bookmarks", True),
        "data_dir": str(data_dir()),
        "downloads_dir": str(downloads_dir()),
        "token_hint": (config.access_token[:4] + "…" + config.access_token[-4:]) if config.access_token and len(config.access_token) > 10 else None,
    }

class SettingsRequest(BaseModel):
    bookmarks: bool | None = None

@router.put("/settings")
def update_settings(payload: SettingsRequest) -> dict:
    updates = {key: value for key, value in payload.model_dump().items() if value is not None}
    _save_settings(updates)
    return get_settings()

@router.post("/token")
def set_token(payload: TokenRequest) -> dict:
    """保存或清除登录凭据（与桌面版共用同一份配置文件）。"""
    try:
        message = config.set_access_token(payload.token)
    except TokenInputError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"message": message, "logged_in": bool(config.access_token)}

@router.get("/token/current")
def current_token() -> dict:
    """返回已保存凭据的三项 JSON，便于用户复制到别处；仅在已登录时返回。"""
    if not config.access_token:
        return {"token": ""}
    return {"token": format_token_json(config.access_token, config.mac_key, config.token_diff)}

@router.get("/stats")
def get_stats() -> dict: # 顶部概览数字
    library = store.scan_library()
    return {
        "library_count": len(library),
        "library_size": sum(item["size"] for item in library),
        "records": len(store.load_records()),
        "active": jobs.manager.active_count(),
    }

@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "active": jobs.manager.active_count()}
