import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tarfile
import threading
import urllib.parse
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "cli" / "publish"


def load_cli():
    loader = importlib.machinery.SourceFileLoader("publish_cli", str(CLI_PATH))
    spec = importlib.util.spec_from_loader("publish_cli", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class StubState:
    def __init__(self):
        self.posts = []
        self.fail_post = False
        self.uploads = {}
        self.items = {}
        self.list_body = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.state

    def _send(self, status, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status, code):
        self._send(status, {"error": {"code": code, "message": code}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.state.posts.append({
            "path": self.path,
            "sha256": hashlib.sha256(body).hexdigest(),
            "key": self.headers.get("Idempotency-Key"),
            "declared": self.headers.get("X-Archive-SHA256"),
            "content_type": self.headers.get("Content-Type"),
            "body": body,
        })
        if self.state.declared_mismatch:
            self._error(422, "INVALID_ARTIFACT")
            return
        if self.state.fail_post:
            self._error(503, "STORAGE_UNAVAILABLE")
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        item = {
            "id": "11111111-1111-4111-8111-111111111111",
            "label": (query.get("label") or [""])[0],
            "url": "https://example.test/11111111-1111-4111-8111-111111111111/",
            "expires_at": int(time.time()) + 604800,
            "created_at": int(time.time()),
            "bytes": 42,
            "file_count": 1,
        }
        self.state.items[item["id"]] = {
            "state": "committed", "available": True,
            "archive_sha256": self.headers["X-Archive-SHA256"],
            "ttl_seconds": int(query["ttl"][0][:-1]) * {"m": 60, "h": 3600, "d": 86400}[query["ttl"][0][-1]],
            "title": (query.get("title") or [""])[0], **item,
        }
        self.state.uploads[self.headers["Idempotency-Key"]] = self.state.items[item["id"]]
        self._send(201, item)

    def do_GET(self):
        if self.path == "/api/v1/publishes" or self.path.startswith("/api/v1/publishes?"):
            self._send(200, self.state.list_body)
            return
        if self.path.startswith("/api/v1/uploads/"):
            key = self.path.rsplit("/", 1)[-1]
            item = self.state.uploads.get(key)
            if not item:
                self._error(404, "NOT_FOUND")
                return
            self._send(200, item)
            return
        if self.path.startswith("/api/v1/publishes/"):
            item = self.state.items.get(self.path.rsplit("/", 1)[-1])
            if not item:
                self._error(404, "NOT_FOUND")
                return
            self._send(200, item)
            return
        self._error(404, "NOT_FOUND")

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def stub():
    state = StubState()
    state.declared_mismatch = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def cli(tmp_path, monkeypatch, stub):
    module = load_cli()
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    monkeypatch.setattr(module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(module, "LEGACY_CONFIG_DIR", tmp_path / "legacy-config")
    monkeypatch.setattr(module, "STATE_DIR", state_dir)
    (config_dir / "config.json").write_text(json.dumps({
        "base_url": f"http://127.0.0.1:{stub.server_address[1]}",
        "timeout": 10,
    }))
    monkeypatch.delenv("PAGE_DROP_TOKEN", raising=False)
    monkeypatch.setenv("EDGE_PUBLISH_TOKEN", "test-token")
    module.stub_state = stub.state
    module.state_dir = state_dir
    return module


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>ok</title>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    return root


def test_publish_success(cli, site, capsys):
    code = cli.main([str(site), "--ttl", "7d", "--title", "示例"])
    out = capsys.readouterr().out
    assert code == 0
    assert "https://example.test/" in out
    assert not (cli.state_dir / "pending.json").exists()
    assert (cli.state_dir / "history.jsonl").exists()

    post = cli.stub_state.posts[0]
    assert post["content_type"] == "application/x-tar"
    assert post["sha256"] == post["declared"]
    assert "ttl=7d" in post["path"]

    with tarfile.open(fileobj=io.BytesIO(post["body"]), mode="r:") as archive:
        names = archive.getnames()
    assert "index.html" in names
    assert "assets/app.js" in names
    assert all(not name.startswith("./") for name in names)


def test_publish_requires_ttl(cli, site, capsys):
    assert cli.main([str(site)]) == 2
    assert "ttl" in capsys.readouterr().err.lower()
    assert cli.stub_state.posts == []


def test_dry_run_leaves_no_state(cli, site, capsys):
    assert cli.main([str(site), "--ttl", "1d", "--dry-run"]) == 0
    assert "可发布" in capsys.readouterr().out
    assert cli.stub_state.posts == []
    assert not (cli.state_dir / "pending.json").exists()


def test_preflight_rejects_symlink(cli, site, capsys):
    os.symlink(site / "index.html", site / "link.html")
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert "符号链接" in capsys.readouterr().err
    assert cli.stub_state.posts == []


def test_preflight_rejects_dotfile(cli, site, capsys):
    (site / ".env").write_text("secret")
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert "点开头" in capsys.readouterr().err


def test_preflight_requires_index(cli, tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "page.html").write_text("hi")
    assert cli.main([str(root), "--ttl", "1d"]) == 2
    assert "index.html" in capsys.readouterr().err


def test_retry_reuses_original_archive(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "2d"]) == 2
    pending = json.loads((cli.state_dir / "pending.json").read_text())
    first_sha = cli.stub_state.posts[0]["sha256"]
    assert pending["archive_sha256"] == first_sha

    (site / "index.html").write_text("<!doctype html><title>changed</title>")
    cli.stub_state.fail_post = False
    assert cli.main(["retry"]) == 0
    assert len(cli.stub_state.posts) == 2
    assert cli.stub_state.posts[1]["sha256"] == first_sha
    assert cli.stub_state.posts[1]["key"] == cli.stub_state.posts[0]["key"]
    assert not (cli.state_dir / "pending.json").exists()


def test_retry_refuses_outside_window(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    pending_path = cli.state_dir / "pending.json"
    pending = json.loads(pending_path.read_text())
    pending["first_attempt_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    pending_path.write_text(json.dumps(pending))
    cli.stub_state.fail_post = False

    assert cli.main(["retry"]) == 2
    assert "24 小时" in capsys.readouterr().err
    assert len(cli.stub_state.posts) == 1


def test_retry_refuses_tampered_archive(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    (cli.state_dir / "pending.tar").write_bytes(b"tampered")
    cli.stub_state.fail_post = False

    assert cli.main(["retry"]) == 2
    assert "不一致" in capsys.readouterr().err
    assert len(cli.stub_state.posts) == 1


def test_publish_refuses_to_overwrite_pending(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert "待确认" in capsys.readouterr().err


def test_status_reports_state(cli, site, capsys):
    assert cli.main([str(site), "--ttl", "1d"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "11111111-1111-4111-8111-111111111111"]) == 0
    out = capsys.readouterr().out
    assert "committed" in out
    assert "available yes" in out


def test_list_accepts_array_response(cli, site, capsys):
    assert cli.main([str(site), "--ttl", "1d"]) == 0
    cli.stub_state.list_body = [{"id": "abc", "state": "committed", "available": True, "title": "t"}]
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "abc" in capsys.readouterr().out


def test_retry_rejects_changed_base_url(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    config_path = cli.CONFIG_DIR / "config.json"
    config = json.loads(config_path.read_text())
    config["base_url"] = "http://127.0.0.1:9"
    config_path.write_text(json.dumps(config))
    cli.stub_state.fail_post = False

    assert cli.main(["retry"]) == 2
    assert "不一致" in capsys.readouterr().err
    assert len(cli.stub_state.posts) == 1


def test_retry_rejects_invalid_timestamp(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    pending_path = cli.state_dir / "pending.json"
    pending = json.loads(pending_path.read_text())
    pending["first_attempt_at"] = "not-a-time"
    pending_path.write_text(json.dumps(pending))
    cli.stub_state.fail_post = False

    assert cli.main(["retry"]) == 2
    assert "窗口" in capsys.readouterr().err
    assert len(cli.stub_state.posts) == 1


def test_retry_rejects_mismatched_server_record(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    pending = json.loads((cli.state_dir / "pending.json").read_text())
    cli.stub_state.fail_post = False
    cli.stub_state.uploads[pending["key"]] = {
        "id": pending["key"], "state": "committed", "available": True,
        "archive_sha256": "0" * 64, "ttl_seconds": 86400, "title": "",
    }

    assert cli.main(["retry"]) == 2
    assert "不一致" in capsys.readouterr().err


def test_retry_rejects_naive_timestamp(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    pending_path = cli.state_dir / "pending.json"
    pending = json.loads(pending_path.read_text())
    pending["first_attempt_at"] = "2026-11-03T14:36:15"
    pending_path.write_text(json.dumps(pending))
    cli.stub_state.fail_post = False

    assert cli.main(["retry"]) == 2
    assert "窗口" in capsys.readouterr().err


def test_publish_sends_label(cli, site, capsys):
    assert cli.main([str(site), "--ttl", "1d", "--label", "developer-laptop"]) == 0
    assert "label=developer-laptop" in cli.stub_state.posts[0]["path"]
    history = [json.loads(line) for line in (cli.state_dir / "history.jsonl").read_text().splitlines()]
    assert history[-1]["label"] == "developer-laptop"


def test_retry_rejects_label_mismatch(cli, site, capsys):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d", "--label", "a"]) == 2
    pending = json.loads((cli.state_dir / "pending.json").read_text())
    cli.stub_state.fail_post = False
    cli.stub_state.uploads[pending["key"]] = {
        "id": pending["key"], "state": "committed", "available": True,
        "archive_sha256": pending["archive_sha256"], "ttl_seconds": 86400,
        "title": "", "label": "b",
    }

    assert cli.main(["retry"]) == 2
    assert "不一致" in capsys.readouterr().err


def test_proxy_rules(monkeypatch):
    cli = load_cli()
    import urllib.parse

    parts = urllib.parse.urlsplit("https://pub.example.test")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    proxy = cli.proxy_for(parts)
    assert (proxy.hostname, proxy.port) == ("127.0.0.1", 7897)

    monkeypatch.setenv("NO_PROXY", "example.test")
    assert cli.proxy_for(parts) is None

    monkeypatch.setenv("NO_PROXY", "*")
    assert cli.proxy_for(parts) is None

    monkeypatch.delenv("NO_PROXY")
    monkeypatch.delenv("HTTPS_PROXY")
    assert cli.proxy_for(parts) is None


def test_http_request_uses_proxy_absolute_form(cli, site, capsys, monkeypatch):
    import urllib.parse

    posted = {}

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            posted["target"] = self.path
            posted["body"] = self.rfile.read(length)
            identifier = "11111111-1111-4111-8111-111111111111"
            body = json.dumps({"id": identifier, "url": f"https://public.test/{identifier}/",
                               "expires_at": 2, "created_at": 1, "bytes": 1, "file_count": 1}).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        config_path = cli.CONFIG_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["base_url"] = "http://127.0.0.1:9"
        config_path.write_text(json.dumps(config))

        assert cli.main([str(site), "--ttl", "1d"]) == 0
        assert posted["target"].startswith("http://127.0.0.1:9/api/v1/publishes?")
        assert posted["body"][:5] != b""
    finally:
        server.shutdown()
        server.server_close()
