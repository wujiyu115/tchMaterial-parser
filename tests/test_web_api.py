# Web 版：数据目录（下载的教材与记录）、下载记录存储、任务调度与接口

import json
import time
from pathlib import Path

import pytest

from src.tchmaterial_parser import config
from src.tchmaterial_parser.web import jobs, paths, store


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 Web 版的数据目录指向临时目录，避免污染真实配置。"""
    monkeypatch.setenv("TCHMATERIAL_DATA_DIR", str(tmp_path))
    (tmp_path / "downloads").mkdir()
    yield tmp_path


@pytest.fixture
def isolated_manager(data_dir, monkeypatch):
    """使用独立的任务管理器，避免污染全局单例。"""
    manager = jobs.JobManager()
    monkeypatch.setattr(jobs, "manager", manager)
    yield manager
    manager.shutdown()


class TestPaths:
    def test_data_dir_follows_environment(self, data_dir) -> None:
        assert paths.data_dir() == data_dir
        assert paths.downloads_dir() == data_dir / "downloads"
        assert paths.records_file() == data_dir / "records.json"

    def test_ensure_dirs_creates_downloads(self, data_dir) -> None:
        target = data_dir / "nested"
        paths.ensure_dirs()
        assert paths.downloads_dir().is_dir()


class TestStore:
    def test_records_round_trip(self, data_dir) -> None:
        record = store.new_record("语文八年级上册", "https://example.com/a.pdf")
        store.upsert_record(record)
        loaded = store.load_records()
        assert len(loaded) == 1
        assert loaded[0]["title"] == "语文八年级上册"
        assert loaded[0]["id"] == record["id"]

    def test_upsert_updates_existing_record(self, data_dir) -> None:
        record = store.new_record("书", "https://example.com/a.pdf")
        store.upsert_record(record)
        store.upsert_record({**record, "status": "completed", "size": 42})
        loaded = store.load_records()
        assert len(loaded) == 1
        assert loaded[0]["status"] == "completed"
        assert loaded[0]["size"] == 42

    def test_credentials_are_redacted_in_records(self, data_dir) -> None:
        record = store.new_record("书", "https://example.com/a.pdf?accessToken=SECRET-TOKEN")
        store.upsert_record(record)
        text = json.dumps(store.load_records(), ensure_ascii=False)
        assert "SECRET-TOKEN" not in text
        assert "<已隐藏>" in text

    def test_corrupted_records_file_returns_empty(self, data_dir) -> None:
        paths.records_file().write_text("{ not json", encoding="utf-8")
        assert store.load_records() == []

    def test_delete_records_removes_only_selected(self, data_dir) -> None:
        first = store.new_record("一", "https://example.com/1.pdf")
        second = store.new_record("二", "https://example.com/2.pdf")
        store.upsert_record(first)
        store.upsert_record(second)
        assert store.delete_records({first["id"]}) == 1
        remaining = store.load_records()
        assert [item["id"] for item in remaining] == [second["id"]]

    def test_safe_target_rejects_traversal(self, data_dir) -> None:
        assert store.safe_target("../../etc/passwd") is None
        assert store.safe_target("") is None
        inside = store.safe_target("初中/语文/book.pdf")
        assert inside is not None
        assert str(inside).startswith(str(data_dir / "downloads"))

    def test_scan_library_reads_disk_and_metadata(self, data_dir) -> None:
        book = data_dir / "downloads" / "初中" / "语文" / "book.pdf"
        book.parent.mkdir(parents=True)
        book.write_bytes(b"%PDF-1.4 test")
        store.upsert_record({
            **store.new_record("语文八年级上册", "https://example.com/a.pdf"),
            "file": "初中/语文/book.pdf",
            "file_format": "pdf",
            "status": "completed",
            "finished_at": time.time(),
        })

        items = store.scan_library()

        assert len(items) == 1
        assert items[0]["title"] == "语文八年级上册"
        assert items[0]["file"] == "初中/语文/book.pdf"
        assert items[0]["size"] == len(b"%PDF-1.4 test")

    def test_scan_library_ignores_temp_and_reports_untracked_files(self, data_dir) -> None:
        downloads = data_dir / "downloads"
        (downloads / "book.pdf").write_bytes(b"pdf")
        (downloads / "partial.pdf.tmp").write_bytes(b"tmp")

        items = store.scan_library()

        assert [item["file"] for item in items] == ["book.pdf"]
        assert items[0]["title"] == "book"  # 无记录时回退为文件名

    def test_scan_library_reflects_deleted_files(self, data_dir) -> None:
        book = data_dir / "downloads" / "book.pdf"
        book.write_bytes(b"pdf")
        assert len(store.scan_library()) == 1
        book.unlink()
        assert store.scan_library() == []


class TestRecovery:
    def test_interrupted_tasks_become_stopped(self, data_dir, isolated_manager) -> None:
        store.upsert_record({**store.new_record("书", "https://example.com/a.pdf"), "status": "downloading"})
        store.upsert_record({**store.new_record("完成", "https://example.com/b.pdf"), "status": "completed", "finished_at": time.time()})

        isolated_manager.recover_interrupted()

        by_title = {record["title"]: record for record in store.load_records()}
        assert by_title["书"]["status"] == "stopped"
        assert by_title["完成"]["status"] == "completed"


class TestJobManager:
    def test_parse_urls_deduplicates_and_collects_failures(self, data_dir, isolated_manager, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo

        resources = [
            ResourceInfo("A", "https://example.com/a.pdf", "pdf", []),
            ResourceInfo("A duplicate", "https://example.com/a.pdf", "pdf", []),
        ]

        def fake_parse(url, bookmarks):
            return resources if "good" in url else None

        monkeypatch.setattr(jobs, "parse", fake_parse)
        parsed, failed = isolated_manager.parse_urls(
            ["https://example.com/good", "https://example.com/bad"], True
        )

        assert [resource.title for resource in parsed] == ["A"]
        assert failed == ["https://example.com/bad"]

    def test_parse_redacts_credentials_from_failure_list(self, data_dir, isolated_manager, monkeypatch) -> None:
        monkeypatch.setattr(jobs, "parse", lambda url, bookmarks: None)
        _parsed, failed = isolated_manager.parse_urls(
            ["https://example.com/x?accessToken=SECRET"], True
        )
        assert "SECRET" not in json.dumps(failed)
        assert "<已隐藏>" in failed[0]

    def test_completed_download_marks_record_and_file(self, data_dir, isolated_manager, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo

        def fake_download(url, save_path, chapters, state):
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            Path(save_path).write_bytes(b"%PDF-1.4 content")
            state["downloaded_size"] = 17
            state["total_size"] = 17

        monkeypatch.setattr(jobs.panel, "download_file", fake_download)
        resource = ResourceInfo("语文八年级上册", "https://example.com/a.pdf", "pdf", [], relative_dir=("初中", "语文"))

        created = isolated_manager.start_batch([resource], True)
        assert len(created) == 1

        record = created[0]
        for _ in range(100):
            view = isolated_manager.task_view(record["id"])
            if view and view.get("finished"):
                break
            time.sleep(0.05)

        view = isolated_manager.task_view(record["id"])
        assert view["status"] == "completed"
        assert view["status_label"] == "已完成"
        assert view["file"] == "初中/语文/语文八年级上册.pdf"

        persisted = store.load_records()[0]
        assert persisted["status"] == "completed"
        assert persisted["size"] == len(b"%PDF-1.4 content")

    def test_failed_download_records_reason(self, data_dir, isolated_manager, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo

        def failing_download(url, save_path, chapters, state):
            state["failed_reason"] = "服务器返回 HTTP 状态码 403"

        monkeypatch.setattr(jobs.panel, "download_file", failing_download)
        resource = ResourceInfo("书", "https://example.com/a.pdf", "pdf", [])

        record = isolated_manager.start_batch([resource], True)[0]
        for _ in range(100):
            view = isolated_manager.task_view(record["id"])
            if view and view.get("finished"):
                break
            time.sleep(0.05)

        view = isolated_manager.task_view(record["id"])
        assert view["status"] == "failed"
        assert "403" in store.load_records()[0]["error"]

    def test_existing_file_is_skipped_without_downloading(self, data_dir, isolated_manager, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo

        called = []
        monkeypatch.setattr(jobs.panel, "download_file", lambda *args: called.append(args))
        (data_dir / "downloads" / "书.pdf").write_bytes(b"existing")
        resource = ResourceInfo("书", "https://example.com/a.pdf", "pdf", [])

        record = isolated_manager.start_batch([resource], True)[0]

        assert called == []
        assert record["status"] == "skipped"
        assert isolated_manager.task_view(record["id"])["status_label"] == "已存在"

    def test_stop_marks_unfinished_task(self, data_dir, isolated_manager, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo
        import threading

        release = threading.Event()

        def slow_download(url, save_path, chapters, state):
            release.wait(timeout=5)
            # 模拟真实 download_file：用户请求停止后标记 stopped 且不发布文件
            if state["cancel"].is_set():
                state["stopped"] = True

        monkeypatch.setattr(jobs.panel, "download_file", slow_download)
        resource = ResourceInfo("书", "https://example.com/a.pdf", "pdf", [])
        record = isolated_manager.start_batch([resource], True)[0]

        assert isolated_manager.stop(record["id"]) is True
        release.set()

        for _ in range(100):
            if isolated_manager.task_view(record["id"]).get("finished"):
                break
            time.sleep(0.05)
        assert isolated_manager.task_view(record["id"])["status"] == "stopped"

    def test_save_paths_are_unique_and_classified(self, data_dir, isolated_manager, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo

        monkeypatch.setattr(jobs.panel, "download_file", lambda *args: None)
        resources = [
            ResourceInfo("同名教材", "https://example.com/a.pdf", "pdf", [], edition="人教版", relative_dir=("初中", "语文")),
            ResourceInfo("同名教材", "https://example.com/b.pdf", "pdf", [], edition="北师大版", relative_dir=("初中", "语文")),
        ]

        created = isolated_manager.start_batch(resources, True)
        files = [record["file"] for record in created]

        assert len(set(files)) == 2
        assert all(file.startswith("初中/语文/") for file in files)
        assert any("人教版" in file for file in files)
        assert any("北师大版" in file for file in files)
