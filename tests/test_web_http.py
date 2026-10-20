# Web 版 HTTP 接口测试：教材库、下载、设置与安全约束
#
# 使用 FastAPI 的 TestClient，不启动真实服务器；下载引擎在部分用例中被替换，
# 以保证测试不依赖网络。

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.tchmaterial_parser import config
from src.tchmaterial_parser.web import jobs, paths, store
from src.tchmaterial_parser.web.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TCHMATERIAL_DATA_DIR", str(tmp_path))
    (tmp_path / "downloads").mkdir()
    manager = jobs.JobManager()
    monkeypatch.setattr(jobs, "manager", manager)
    # 路由模块导入的是 jobs.manager，需要一并替换模块内引用
    from src.tchmaterial_parser.web import api as api_module
    monkeypatch.setattr(api_module, "jobs", jobs)

    with TestClient(create_app()) as test_client:
        yield test_client
    manager.shutdown()


@pytest.fixture
def sample_book(tmp_path):
    downloads = tmp_path / "downloads"
    book = downloads / "初中" / "语文" / "book.pdf"
    book.parent.mkdir(parents=True)
    book.write_bytes(b"%PDF-1.4 fake book content")
    return "初中/语文/book.pdf"


class TestMeta:
    def test_health(self, client) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_index_serves_frontend(self, client) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "国家中小学智慧教育平台" in response.text

    def test_static_assets(self, client) -> None:
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

    def test_settings_never_exposes_credentials(self, client, monkeypatch) -> None:
        monkeypatch.setattr(config, "access_token", "super-secret-token-value")
        monkeypatch.setattr(config, "mac_key", "super-secret-mac-key")
        body = client.get("/api/settings").text
        assert "super-secret-token-value" not in body
        assert "super-secret-mac-key" not in body
        assert client.get("/api/settings").json()["logged_in"] is True


class TestLibrary:
    def test_empty_library(self, client) -> None:
        data = client.get("/api/library").json()
        assert data == {"items": [], "count": 0, "total_size": 0}

    def test_lists_downloaded_book(self, client, sample_book) -> None:
        data = client.get("/api/library").json()
        assert data["count"] == 1
        item = data["items"][0]
        assert item["file"] == sample_book
        assert item["size"] == len(b"%PDF-1.4 fake book content")

    def test_download_returns_file_with_utf8_name(self, client, sample_book) -> None:
        response = client.get("/api/library/file", params={"relative_path": sample_book})
        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 fake book content"
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert response.headers["accept-ranges"] == "bytes"

    def test_head_request_is_allowed(self, client, sample_book) -> None:
        # 内联预览器会先探测 HEAD，不应返回 405
        response = client.head("/api/library/file", params={"relative_path": sample_book})
        assert response.status_code == 200
        assert response.headers["content-length"] == str(len(b"%PDF-1.4 fake book content"))

    def test_missing_file_returns_404(self, client) -> None:
        response = client.get("/api/library/file", params={"relative_path": "nope.pdf"})
        assert response.status_code == 404

    def test_traversal_is_rejected(self, client) -> None:
        response = client.get("/api/library/file", params={"relative_path": "../../../etc/passwd"})
        assert response.status_code in (400, 404)

    def test_search_filters_by_name(self, client, sample_book) -> None:
        assert client.get("/api/library", params={"q": "book"}).json()["count"] == 1
        assert client.get("/api/library", params={"q": "不存在"}).json()["count"] == 0

    def test_delete_file(self, client, sample_book, tmp_path) -> None:
        response = client.delete("/api/library/file", params={"relative_path": sample_book})
        assert response.status_code == 200
        assert not (tmp_path / "downloads" / sample_book).exists()

    def test_batch_delete_reports_missing(self, client, sample_book, tmp_path) -> None:
        response = client.request(
            "DELETE", "/api/library", json={"files": [sample_book, "missing.pdf"]}
        )
        data = response.json()
        assert data["deleted"] == [sample_book]
        assert data["missing"] == ["missing.pdf"]

    def test_thumbnail_requires_pdf(self, client, sample_book) -> None:
        response = client.get("/api/library/thumbnail", params={"relative_path": sample_book})
        # 伪造的 PDF 无法渲染，应返回错误而不是 500 崩溃
        assert response.status_code in (404, 500)


class TestDownloadValidation:
    def test_empty_urls_rejected(self, client) -> None:
        response = client.post("/api/downloads", json={"urls": []})
        assert response.status_code == 400

    def test_non_ascii_token_rejected(self, client, monkeypatch) -> None:
        monkeypatch.setattr(config, "access_token", "含中文的令牌")
        response = client.post("/api/downloads", json={"urls": ["https://example.com/x"]})
        assert response.status_code == 400
        assert "Access Token" in response.json()["detail"]

    def test_unparsable_urls_report_422(self, client, monkeypatch) -> None:
        monkeypatch.setattr(config, "access_token", None)
        monkeypatch.setattr(jobs, "parse", lambda url, bookmarks: None)
        from src.tchmaterial_parser.web import api as api_module
        monkeypatch.setattr(api_module, "jobs", jobs)
        response = client.post("/api/downloads", json={"urls": ["https://example.com/bad"]})
        assert response.status_code == 422

    def test_successful_download_creates_task_and_record(self, client, monkeypatch) -> None:
        from src.tchmaterial_parser.api import ResourceInfo

        monkeypatch.setattr(config, "access_token", None)
        monkeypatch.setattr(
            jobs, "parse",
            lambda url, bookmarks: [ResourceInfo("语文八年级上册", "https://example.com/a.pdf", "pdf", [], relative_dir=("初中", "语文"))],
        )

        def fake_download(url, save_path, chapters, state):
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            Path(save_path).write_bytes(b"%PDF-1.4")
            state["downloaded_size"] = state["total_size"] = 8

        monkeypatch.setattr(jobs.panel, "download_file", fake_download)

        response = client.post("/api/downloads", json={"urls": ["https://example.com/page"]})
        assert response.status_code == 200
        data = response.json()
        assert data["created"] == 1

        record_id = data["tasks"][0]["id"]
        for _ in range(100):
            task = client.get(f"/api/downloads/{record_id}").json()
            if task.get("finished"):
                break
            time.sleep(0.05)

        task = client.get(f"/api/downloads/{record_id}").json()
        assert task["status"] == "completed"
        assert task["status_label"] == "已完成"
        assert task["file"] == "初中/语文/语文八年级上册.pdf"

        # 下载完成的教材应出现在教材库与记录中
        assert client.get("/api/library").json()["count"] == 1
        records = client.get("/api/records").json()["records"]
        assert any(record["id"] == record_id and record["status"] == "completed" for record in records)

    def test_stop_unknown_task_returns_409(self, client) -> None:
        assert client.post("/api/downloads/nope/stop").status_code == 409

    def test_list_labels_are_chinese(self, client) -> None:
        labels = client.get("/api/downloads").json()["labels"]
        assert labels["completed"] == "已完成"
        assert labels["failed"] == "下载失败"


class TestRecords:
    def test_clear_records_keeps_files(self, client, sample_book, tmp_path) -> None:
        store.upsert_record({**store.new_record("书", "https://example.com/a.pdf"), "file": sample_book, "status": "completed"})
        response = client.delete("/api/records")
        assert response.status_code == 200
        assert store.load_records() == []
        assert (tmp_path / "downloads" / sample_book).exists()


class TestSettings:
    def test_toggle_bookmarks(self, client) -> None:
        response = client.put("/api/settings", json={"bookmarks": False})
        assert response.status_code == 200
        assert response.json()["bookmarks"] is False
        assert client.get("/api/settings").json()["bookmarks"] is False

    def test_invalid_token_is_rejected(self, client) -> None:
        response = client.post("/api/token", json={"token": "not json"})
        assert response.status_code == 400
        assert "JSON" in response.json()["detail"]

    def test_valid_token_is_saved_without_echo(self, client, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(config, "config_file_path", lambda: tmp_path / "data.json")
        payload = '{"access_token":"tok-1234567890","mac_key":"mac-key","diff":0}'
        response = client.post("/api/token", json={"token": payload})
        assert response.status_code == 200
        assert response.json()["logged_in"] is True
        # 凭据写入配置文件，且接口不回显
        saved = (tmp_path / "data.json").read_text(encoding="utf-8")
        assert "tok-1234567890" in saved
        assert "tok-1234567890" not in client.get("/api/settings").text

    def test_empty_token_clears_credentials(self, client, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(config, "config_file_path", lambda: tmp_path / "data.json")
        client.post("/api/token", json={"token": '{"access_token":"a","mac_key":"b","diff":0}'})
        response = client.post("/api/token", json={"token": ""})
        assert response.json()["logged_in"] is False
