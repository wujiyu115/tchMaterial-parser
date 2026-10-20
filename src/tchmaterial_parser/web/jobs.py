# -*- coding: utf-8 -*-
# 下载任务调度：解析链接、排队下载、上报进度，并把结果写入下载记录
#
# 这里不重新实现传输逻辑，而是直接复用桌面版的 download_file / request_download /
# allocate_download_paths，保证 Web 版与桌面版在鉴权签名、镜像切换、400 重试、
# PDF 书签等行为上完全一致。

from __future__ import annotations

import os, threading, time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..api import ResourceInfo, parse
from ..ui import download_panel as panel
from . import store
from .paths import downloads_dir, ensure_dirs

MAX_WORKERS = 3 # 与桌面版一致：私有 CDN 并发过高会返回 400

class JobManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="web-download")
        self._states: dict[str, dict] = {}   # record id -> download_panel 状态字典
        self._records: dict[str, dict] = {}  # record id -> 记录

    # ---- 生命周期 ----

    def recover_interrupted(self) -> None: # 容器重启后，把上次未结束的任务标为已停止
        records = store.load_records()
        changed = False
        for record in records:
            if record.get("status") in ("queued", "parsing", "downloading", "bookmarking"):
                record["status"] = "stopped"
                record["error"] = "程序重启，任务已中断"
                record["finished_at"] = record.get("finished_at") or time.time()
                changed = True
        if changed:
            store.save_records(records)

    def shutdown(self) -> None: # 停止所有任务并等待线程退出
        self.stop_all()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ---- 解析 ----

    def parse_urls(self, urls: list[str], bookmarks: bool) -> tuple[list[ResourceInfo], list[str]]:
        """逐条解析资源页面。返回（去重后的资源列表，解析失败的链接）。"""
        resources: list[ResourceInfo] = []
        seen: set[str] = set()
        failed: list[str] = []
        for url in urls:
            try:
                parsed = parse(url, bookmarks)
            except Exception: # 单条失败不影响其余链接
                store_url = panel.redact_access_token(url)
                failed.append(store_url)
                continue
            if not parsed:
                failed.append(panel.redact_access_token(url))
                continue
            for resource in parsed:
                if resource.url in seen:
                    continue
                seen.add(resource.url)
                resources.append(resource)
        return resources, failed

    # ---- 下载 ----

    def start_batch(self, resources: list[ResourceInfo], bookmarks: bool = True) -> list[dict]:
        """为一批资源分配保存路径并开始下载，返回新建的记录。"""
        ensure_dirs()
        directory = downloads_dir()
        targets = list(zip(resources, panel.allocate_download_paths(resources, str(directory))))
        created: list[dict] = []
        for resource, save_path in targets:
            record = self._register(resource, Path(save_path), bookmarks)
            created.append(record)
        return created

    def _register(self, resource: ResourceInfo, save_path: Path, bookmarks: bool) -> dict:
        relative = store.relative_file(save_path)
        record = store.new_record(
            title=resource.title or save_path.stem,
            download_url=resource.url,
            file=relative,
            file_format=resource.file_format,
            edition=resource.edition,
            relative_dir=list(resource.relative_dir),
            chapters=len(resource.chapters or []),
            status="queued",
        )
        record["status_label"] = store.STATUS_LABELS.get("queued")
        state = {
            "download_url": resource.url,
            "save_path": str(save_path),
            "title": save_path.name,
            "downloaded_size": 0,
            "total_size": 0,
            "finished": False,
            "failed_reason": None,
            "skipped": False,
            "stopped": False,
            "phase": "排队中",
            "cancel": threading.Event(),
            "chapters": resource.chapters if bookmarks else None,
            "speed": 0,
            "attempt": 1,
            "error_details": None,
        }

        with self._lock:
            self._states[record["id"]] = state
            self._records[record["id"]] = record

        # 已完整下载过的同名文件直接跳过，避免重复占用带宽
        if save_path.is_file():
            state.update(finished=True, skipped=True, phase="已存在")
            record.update(status="skipped", finished_at=time.time(), file=relative)
            store.upsert_record(record)
            return record

        store.upsert_record(record)
        self._executor.submit(self._run, record, state)
        return record

    def _run(self, record: dict, state: dict) -> None:
        """在线程池中执行，复用桌面版的 download_file。"""
        record["status"] = "downloading"
        store.upsert_record(record)
        try:
            panel.download_file(
                state["download_url"],
                state["save_path"],
                state["chapters"],
                state,
            )
        except BaseException as error: # download_file 内部已兜底，这里防止线程静默退出
            state["finished"] = True
            state["failed_reason"] = panel.redact_access_token(str(error))
            state["error_details"] = panel.redact_access_token(traceback.format_exc().rstrip())
        finally:
            self._finalize(record, state)

    def _finalize(self, record: dict, state: dict) -> None:
        with self._lock:
            state["finished"] = True # 无论 download_file 是否已标记，这里都是任务的终态
            if state.get("skipped"):
                status = "skipped"
            elif state.get("stopped"):
                status = "stopped"
            elif state.get("failed_reason"):
                status = "failed"
            elif os.path.isfile(state["save_path"]):
                status = "completed"
            else:
                status = "failed"

            path = Path(state["save_path"])
            record.update(
                status=status,
                error=panel.redact_access_token(state["failed_reason"]) if state.get("failed_reason") else None,
                finished_at=time.time(),
            )
            if status in ("completed", "skipped"):
                record["file"] = store.relative_file(path) or record.get("file", "")
                try:
                    record["size"] = path.stat().st_size
                except OSError:
                    record["size"] = 0
            store.upsert_record(record)

    # ---- 查询与控制 ----

    def task_view(self, record_id: str) -> dict | None: # 合并记录与实时进度，生成前端使用的任务对象
        with self._lock:
            record = self._records.get(record_id)
            state = self._states.get(record_id)
            if record is None:
                return None
            record = dict(record)
            if state is None:
                record["status_label"] = store.STATUS_LABELS.get(record.get("status", ""), record.get("status", ""))
                record.setdefault("progress", 100.0 if record.get("status") == "completed" else 0.0)
                return record
            status = record.get("status") or "queued"
            if not state["finished"]:
                status = self._live_status(state)
            downloaded = state.get("downloaded_size", 0)
            total = state.get("total_size", 0)
            progress = min(100.0, downloaded * 100 / total) if total else (100.0 if state["finished"] and not state.get("failed_reason") else 0.0)
            record.update(
                status=status,
                status_label=store.STATUS_LABELS.get(status, status),
                downloaded_size=downloaded,
                total_size=total,
                speed=state.get("speed", 0),
                progress=round(progress, 1),
                error=panel.redact_access_token(state["failed_reason"]) if state.get("failed_reason") else record.get("error"),
                finished=state["finished"],
            )
            return record

    @staticmethod
    def _live_status(state: dict) -> str:
        if state.get("cancel") and state["cancel"].is_set():
            return "stopped"
        phase = state.get("phase") or "排队中"
        if phase == "写入书签":
            return "bookmarking"
        if phase == "下载中":
            return "downloading"
        return "queued"

    def tasks(self) -> list[dict]: # 当前进程内的任务（最新的在前）
        with self._lock:
            ids = list(self._records)
            views = [self.task_view(record_id) for record_id in ids]
        views = [view for view in views if view]
        views.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
        return views

    def stop(self, record_id: str) -> bool: # 请求停止单个任务
        with self._lock:
            state = self._states.get(record_id)
            record = self._records.get(record_id)
        if state is None or state["finished"]:
            return False
        state["cancel"].set()
        if record is not None:
            record["status"] = "stopped"
        return True

    def stop_all(self) -> int: # 请求停止全部未完成任务
        with self._lock:
            ids = [record_id for record_id, state in self._states.items() if not state["finished"]]
        return sum(self.stop(record_id) for record_id in ids)

    def clear_finished(self) -> int: # 清理内存中已结束的任务视图（记录仍保留在文件里）
        with self._lock:
            done = [record_id for record_id, state in self._states.items() if state["finished"]]
            for record_id in done:
                self._states.pop(record_id, None)
                self._records.pop(record_id, None)
        return len(done)

    def active_count(self) -> int: # 未完成任务数量，供健康检查与界面提示使用
        with self._lock:
            return sum(1 for state in self._states.values() if not state["finished"])

manager = JobManager() # 全局单例，由 FastAPI 应用复用
