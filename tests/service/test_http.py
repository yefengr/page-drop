import os
import sqlite3
import uuid

import pytest

from app import config as C
from app.asgi import app
from app.config import SECURITY_HEADERS
from conftest import AUTH, headers, make_tar, maintain, post, row, service


def test_publish_and_exact_manifest(client):
    result = post(client)
    assert result.status_code == 201
    data = result.json()
    item_id = data["id"]
    assert uuid.UUID(item_id).version == 4
    assert set(data) == {"id", "url", "expires_at", "created_at", "bytes", "file_count"}
    assert data["expires_at"] - data["created_at"] == 600
    assert data["bytes"] == len(b"<h1>Hello</h1>body{}")
    assert data["file_count"] == 2
    assert client.get(data["url"]).content == b"<h1>Hello</h1>"
    assert client.get(f"/{item_id}/assets/app.css").headers["content-type"] == "text/css; charset=utf-8"
    for path in (f"/{uuid.uuid4()}/", f"/{item_id}/assets/", f"/{item_id}/unknown"):
        response = client.get(path)
        assert response.status_code == 404 and response.content == b""
    assert app is not None


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_root_serves_default_page_without_listing(client, method):
    response = client.request(method, "/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    if method == "HEAD":
        assert response.content == b""
        assert int(response.headers["content-length"]) > 0
        return
    assert "这里没有目录，只有保质期。" in response.text
    assert "/api/" not in response.text
    assert "/2f7d" not in response.text


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("failure", ["not_ready", "database", "directories"])
def test_root_refuses_unavailable_storage(client, monkeypatch, method, failure):
    current = service(client)
    if failure == "not_ready":
        current.ready = False
    else:
        def unavailable():
            if failure == "database":
                raise sqlite3.DatabaseError("injected database failure")
            raise OSError("injected directory failure")

        if failure == "database":
            monkeypatch.setattr(current.db, "check", unavailable)
        else:
            monkeypatch.setattr(current, "check_directories", unavailable)
    response = client.request(method, "/", headers={"Accept": "text/html"})
    assert response.status_code == 503
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    if method == "HEAD":
        assert response.content == b""
    else:
        assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert client.get("/healthz").status_code == 503


def test_not_found_page_is_uniform_and_negotiated(client, clock):
    item_id = post(client, ttl="10m").json()["id"]
    clock.advance(600)
    expired = client.get(f"/{item_id}/", headers={"Accept": "text/html"})
    unknown = client.get(f"/{uuid.uuid4()}/", headers={"Accept": "text/html"})
    assert expired.status_code == unknown.status_code == 404
    assert expired.text == unknown.text
    assert "这个链接过期了，或者从未存在。" in expired.text
    assert client.get("/nope", headers={"Accept": "*/*"}).content == b""
    head = client.head(f"/{item_id}/", headers={"Accept": "text/html"})
    assert head.status_code == 404 and head.content == b""
    assert int(head.headers["content-length"]) > 0


@pytest.mark.parametrize("ttl,expected", [("9m", 400), ("10m", 201), ("30d", 201),
    ("31d", 400), ("1.5h", 400), ("1h30m", 400), ("0m", 400), ("-1d", 400)])
def test_ttl_boundaries(client, ttl, expected):
    assert post(client, ttl=ttl).status_code == expected


@pytest.mark.parametrize("suffix", ["", "/", "/assets/app.css"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_expiry_all_content_admission(client, clock, suffix, method):
    item_id = post(client).json()["id"]
    clock.advance(600)
    result = client.request(method, f"/{item_id}{suffix}", headers={"Range": "bytes=0-1"}, follow_redirects=False)
    assert result.status_code == 404 and result.content == b""
    assert row(client, item_id)["state"] == "deleting"


def test_idempotency_queries_and_conflicts(client, clock):
    key = str(uuid.uuid4())
    first = post(client, key=key)
    clock.advance(30)
    again = post(client, key=key)
    assert again.status_code == 200 and again.json() == first.json()
    assert post(client, key=key, ttl="11m").status_code == 409
    assert post(client, key=key, title="other").status_code == 409
    status = client.get(f"/api/v1/uploads/{key}", headers=AUTH).json()
    assert status["available"] and status["archive_bytes"] == len(make_tar())
    assert status["archive_sha256"] == headers(make_tar())["X-Archive-SHA256"]
    assert len(client.get("/api/v1/publishes", headers=AUTH).json()) == 1
    clock.advance(600)
    assert post(client, key=key).status_code == 409
    assert client.get("/api/v1/publishes", headers=AUTH).json() == []
    terminal = client.get("/api/v1/publishes?all=1", headers=AUTH).json()[0]
    assert not terminal["available"] and "url" not in terminal


@pytest.mark.parametrize("path", ["/healthz", "/", "/api/v1/publishes", "/api/v1/publishes?all=wrong"])
def test_response_headers(client, path):
    response = client.get(path, headers=AUTH)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_headers_for_success_redirect_and_error(client):
    item_id = post(client).json()["id"]
    for response in (client.get(f"/{item_id}", follow_redirects=False), client.get(f"/{item_id}/"),
                     client.get("/api/v1/publishes"), client.post("/api/v1/publishes", headers=AUTH)):
        for key, value in SECURITY_HEADERS.items():
            assert response.headers[key] == value
    response = client.get("/api/v1/publishes")
    assert response.status_code == 401
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("value,status,start,end", [
    ("bytes=0-3", 206, 0, 4), ("bytes=4-", 206, 4, 10),
    ("bytes=-3", 206, 7, 10), ("bytes=0-999", 206, 0, 10),
    ("bytes=11-12", 416, 0, 0), ("bytes=5-3", 416, 0, 0),
    ("bytes=0-1,4-5", 416, 0, 0), ("bytes=-0", 416, 0, 0),
    ("garbage", 416, 0, 0)])
def test_range(client, value, status, start, end):
    body = make_tar([("index.html", b"0123456789"), ("opaque.zzz", b"x")])
    item_id = post(client, body=body).json()["id"]
    response = client.get(f"/{item_id}/", headers={"Range": value})
    assert response.status_code == status
    assert response.content == b"0123456789"[start:end]
    assert response.headers["content-range"] == (f"bytes {start}-{end-1}/10" if status == 206 else "bytes */10")
    head = client.head(f"/{item_id}/", headers={"Range": value})
    assert head.status_code == status and head.content == b""
    assert not service(client).leases.handles
    assert client.get(f"/{item_id}/opaque.zzz").headers["content-type"] == "application/octet-stream"


def test_permissions_unicode_and_restart_ready(client):
    item_id = post(client, body=make_tar([("index.html", b"ok"), ("文档/页面.html", b"unicode")])).json()["id"]
    root = service(client).root / "items" / item_id
    assert os.stat(root).st_mode & 0o777 == 0o750
    assert os.stat(root / "文档").st_mode & 0o777 == 0o750
    assert os.stat(root / "文档/页面.html").st_mode & 0o777 == 0o440
    assert client.get(f"/{item_id}/文档/页面.html").content == b"unicode"
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("change,code,status", [
    ({"Content-Type": "application/gzip"}, "UNSUPPORTED_MEDIA_TYPE", 415),
    ({"Content-Encoding": "gzip"}, "UNSUPPORTED_MEDIA_TYPE", 415),
    ({"X-Archive-SHA256": "x"}, "INVALID_ARGUMENT", 400),
    ({"Idempotency-Key": "bad"}, "INVALID_ARGUMENT", 400),
    ({"X-Archive-SHA256": "0" * 64}, "INVALID_ARTIFACT", 422),
    ({"Authorization": "Bearer wrong"}, "UNAUTHORIZED", 401)])
def test_error_mapping(client, change, code, status):
    body = make_tar()
    request_headers = headers(body)
    request_headers.update(change)
    response = client.post("/api/v1/publishes?ttl=10m", content=body, headers=request_headers)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert service(client).ledger.total == 0


def test_query_validation_and_unknown_api(client):
    body = make_tar()
    for path in ("/api/v1/publishes", "/api/v1/publishes?ttl=10m&ttl=10m",
                 "/api/v1/publishes?ttl=10m&extra=1", "/api/v1/publishes?ttl=10m&title=%00"):
        assert client.post(path, content=body, headers=headers(body)).status_code == 400
    assert client.get(f"/api/v1/publishes/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/uploads/{uuid.uuid4()}", headers=AUTH).status_code == 404


def test_quotas_and_content_length(client, monkeypatch):
    monkeypatch.setattr(C, "CONTENT_BUDGET", C.MAX_CONTENT - 1)
    assert post(client).status_code == 507
    assert service(client).ledger.total == 0
    monkeypatch.setattr(C, "CONTENT_BUDGET", 5 * 1024 * C.MIB)
    body = make_tar()
    request_headers = headers(body)
    request_headers["Content-Length"] = str(C.MAX_ARCHIVE + 1)
    assert client.post("/api/v1/publishes?ttl=10m", content=body, headers=request_headers).status_code == 413
    request_headers["Content-Length"] = "1"
    assert client.post("/api/v1/publishes?ttl=10m", content=body, headers=request_headers).status_code == 422
    maintain(client)
    assert service(client).ledger.total == 0


def test_label_is_stored_and_returned(client):
    created = post(client)
    assert created.status_code == 201
    item_id = created.json()["id"]
    body = make_tar()
    key = str(uuid.uuid4())
    response = client.post("/api/v1/publishes",
        params={"ttl": "1d", "title": "带标签", "label": "developer-laptop"},
        content=body, headers=headers(body, key))
    assert response.status_code == 201
    item_id = response.json()["id"]
    status = client.get(f"/api/v1/publishes/{item_id}", headers=AUTH).json()
    assert status["label"] == "developer-laptop"
    assert status["title"] == "带标签"


def test_label_rejects_invalid(client):
    body = make_tar()
    long_label = "x" * 65
    response = client.post("/api/v1/publishes",
        params={"ttl": "1d", "label": long_label}, content=body, headers=headers(body))
    assert response.status_code == 400
    response = client.post("/api/v1/publishes",
        params={"ttl": "1d", "label": "bad\x07label"}, content=body, headers=headers(body))
    assert response.status_code == 400


def test_idempotency_requires_same_label(client):
    body = make_tar()
    key = str(uuid.uuid4())
    first = client.post("/api/v1/publishes", params={"ttl": "1d", "label": "a"},
                        content=body, headers=headers(body, key))
    assert first.status_code == 201
    again = client.post("/api/v1/publishes", params={"ttl": "1d", "label": "b"},
                        content=body, headers=headers(body, key))
    assert again.status_code == 409
