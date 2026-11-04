"""访问码保护的服务端契约。

覆盖正常发布、解锁、会话、重置、限流、迁移与不可用响应。测试使用 HTTPS
基址，因为 Secure Cookie 不会在明文 HTTP 请求中回传。
"""

import hashlib
import uuid

import pytest
from starlette.testclient import TestClient

from app.asgi import create_app
from app.config import Settings

from conftest import AUTH, FakeClock, make_tar

SESSION_KEY = "11" * 32
PUBLIC = {"Authorization": "Bearer testing-token", "Content-Type": "application/x-tar"}


@pytest.fixture
def protected_settings(tmp_path):
    return Settings("testing-token", tmp_path / "data", "", SESSION_KEY)


@pytest.fixture
def protected(protected_settings, clock):
    with TestClient(create_app(protected_settings, clock), base_url="https://testserver") as result:
        yield result


def publish(client, access=None, ttl="10m", key=None, title=""):
    body = make_tar()
    params = {"ttl": ttl, "title": title}
    if access:
        params["access"] = access
    return client.post(
        "/api/v1/publishes", params=params, content=body,
        headers={**PUBLIC, "Idempotency-Key": key or str(uuid.uuid4()),
                 "X-Archive-SHA256": hashlib.sha256(body).hexdigest()},
    )


def code_publish(client, **kwargs):
    result = publish(client, "code", **kwargs)
    assert result.status_code == 201, result.text
    return result.json()


def unlock(client, item_id, code):
    # 不跟随 303：需要断言真实状态和 Set-Cookie，同时保留客户端已保存的 Cookie。
    return client.post(f"/{item_id}/_unlock", data={"code": code}, follow_redirects=False)


def test_code_publish_returns_single_use_secret(protected):
    payload = code_publish(protected)
    assert payload["access_mode"] == "code"
    assert payload["access_revision"] == 1
    assert len(payload["access_code"]) == 8
    assert set(payload["access_code"]) <= set("ABCDEFGHJKMNPQRSTUVWXYZ23456789")
    detail = protected.get(f"/api/v1/publishes/{payload['id']}", headers=AUTH).json()
    assert detail["access_mode"] == "code"
    assert "access_code" not in detail and "access_hash" not in detail


def test_locked_root_serves_unlock_page_without_leaking(protected):
    payload = code_publish(protected)
    page = protected.get(f"/{payload['id']}/")
    assert page.status_code == 200
    assert 'name="code"' in page.text
    assert 'action="_unlock"' in page.text
    assert "index.html" not in page.text


def test_locked_subresources_do_not_leak_content(protected):
    payload = code_publish(protected)
    item = payload["id"]
    asset = protected.get(f"/{item}/assets/app.css")
    assert asset.status_code == 403
    assert asset.content == b""
    document = protected.get(f"/{item}/other.html", headers={"Accept": "text/html"})
    assert document.status_code == 200 and 'name="code"' in document.text
    unknown = protected.get(f"/{uuid.uuid4()}/")
    assert unknown.status_code == 404


def test_unlock_grants_session_and_rejects_wrong_code(protected):
    payload = code_publish(protected)
    item = payload["id"]
    wrong = unlock(protected, item, "ZZZZZZZZ")
    assert wrong.status_code == 401
    assert "访问码不正确" in wrong.text
    accepted = unlock(protected, item, payload["access_code"])
    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"/{item}/"
    cookie = accepted.cookies.get("pd_" + item.replace("-", ""))
    assert cookie
    set_cookie = accepted.headers["set-cookie"].lower()
    for flag in ("path=/" + item, "secure", "httponly", "samesite=lax", "max-age="):
        assert flag in set_cookie
    assert protected.get(f"/{item}/").text == "<h1>Hello</h1>"
    assert protected.get(f"/{item}/assets/app.css").text == "body{}"


def test_unlock_rejects_unknown_and_expired(protected, clock):
    unknown = unlock(protected, str(uuid.uuid4()), "ABCDEFGH")
    assert unknown.status_code == 404
    payload = code_publish(protected, ttl="10m")
    clock.advance(600)
    expired = unlock(protected, payload["id"], payload["access_code"])
    assert expired.status_code == 404
    assert protected.get(f"/{payload['id']}/").status_code == 404


def test_session_expires_after_one_hour(protected, clock):
    payload = code_publish(protected, ttl="1d")
    assert unlock(protected, payload["id"], payload["access_code"]).status_code == 303
    assert protected.get(f"/{payload['id']}/").status_code == 200
    clock.advance(3601)
    expired = protected.get(f"/{payload['id']}/")
    assert expired.status_code == 200 and 'name="code"' in expired.text


def test_session_does_not_outlive_publish_ttl(protected, clock):
    payload = code_publish(protected, ttl="10m")
    assert unlock(protected, payload["id"], payload["access_code"]).status_code == 303
    clock.advance(599)
    assert protected.get(f"/{payload['id']}/").status_code == 200
    clock.advance(2)
    assert protected.get(f"/{payload['id']}/").status_code == 404


def test_reset_invalidates_old_code_and_session(protected):
    payload = code_publish(protected)
    item = payload["id"]
    old_code = payload["access_code"]
    assert unlock(protected, item, old_code).status_code == 303
    assert protected.get(f"/{item}/").status_code == 200
    reset = protected.post(f"/api/v1/publishes/{item}/access",
                           headers={**AUTH, "X-Access-Revision": "1"})
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["access_revision"] == 2 and body["access_code"] != old_code
    assert protected.get(f"/{item}/").status_code == 200
    assert 'name="code"' in protected.get(f"/{item}/").text
    assert unlock(protected, item, old_code).status_code == 401
    stale = protected.post(f"/api/v1/publishes/{item}/access",
                           headers={**AUTH, "X-Access-Revision": "1"})
    assert stale.status_code == 409
    assert unlock(protected, item, body["access_code"]).status_code == 303
    assert protected.get(f"/{item}/").text == "<h1>Hello</h1>"


def test_reset_requires_revision_and_protected_mode(protected):
    public = publish(protected).json()
    assert protected.post(f"/api/v1/publishes/{public['id']}/access",
                          headers=AUTH).status_code == 400
    assert protected.post(f"/api/v1/publishes/{public['id']}/access",
                          headers={**AUTH, "X-Access-Revision": "1"}).status_code == 409
    protected_item = code_publish(protected)["id"]
    assert protected.post(f"/api/v1/publishes/{protected_item}/access",
                          headers={**AUTH, "X-Access-Revision": "9"}).status_code == 409


def test_idempotent_replay_does_not_return_secret(protected):
    key = str(uuid.uuid4())
    first = code_publish(protected, key=key)
    replay = publish(protected, "code", key=key)
    assert replay.status_code == 200
    body = replay.json()
    assert body["id"] == first["id"]
    assert body["access_mode"] == "code"
    assert "access_code" not in body
    switch = publish(protected, "public", key=key)
    assert switch.status_code == 409


def test_unlock_attempts_are_rate_limited(protected):
    payload = code_publish(protected)
    for _ in range(10):
        assert unlock(protected, payload["id"], "ZZZZZZZZ").status_code == 401
    limited = unlock(protected, payload["id"], payload["access_code"])
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_public_publish_still_open(protected):
    payload = publish(protected).json()
    assert payload["access_mode"] == "public"
    assert payload["access_revision"] == 0
    assert "access_code" not in payload
    assert protected.get(f"/{payload['id']}/").text == "<h1>Hello</h1>"
    assert protected.get(f"/{payload['id']}/assets/app.css").status_code == 200


def test_session_survives_restart(protected_settings, clock):
    with TestClient(create_app(protected_settings, clock), base_url="https://testserver") as first:
        payload = code_publish(first)
        assert unlock(first, payload["id"], payload["access_code"]).status_code == 303
        cookie = first.cookies.get("pd_" + payload["id"].replace("-", ""))
    name = "pd_" + payload["id"].replace("-", "")
    with TestClient(create_app(protected_settings, clock), base_url="https://testserver") as second:
        # 显式携带 Cookie，避免不同客户端的 cookie jar 域名匹配差异。
        restored = second.get(f"/{payload['id']}/", headers={"Cookie": f"{name}={cookie}"})
        assert restored.text == "<h1>Hello</h1>"


def test_head_and_range_still_exercise_admission(protected):
    payload = code_publish(protected)
    item = payload["id"]
    assert protected.head(f"/{item}/").status_code == 200
    assert unlock(protected, item, payload["access_code"]).status_code == 303
    head = protected.head(f"/{item}/")
    assert head.status_code == 200 and head.content == b""
    ranged = protected.get(f"/{item}/", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206 and ranged.content == b"<h1>"
    assert protected.get(f"/{item}", follow_redirects=False).status_code == 308


def test_missing_session_key_refuses_protected_publish(tmp_path, clock):
    settings = Settings("testing-token", tmp_path / "data", "", "")
    with TestClient(create_app(settings, clock), base_url="https://testserver") as client:
        assert publish(client, "code").status_code == 503
        assert publish(client).status_code == 201


def test_unknown_schema_mode_is_not_public(protected):
    payload = code_publish(protected)
    item = payload["id"]
    service = protected.app.state.publisher

    def downgrade():
        with service.db.connect() as conn:
            conn.execute("UPDATE items SET access_mode='broken' WHERE id=?", (item,))

    protected.portal.call(downgrade)
    assert protected.get(f"/{item}/").status_code == 404


def test_garbage_access_hash_is_never_accepted(protected):
    payload = code_publish(protected)
    item = payload["id"]
    service = protected.app.state.publisher

    def corrupt():
        with service.db.connect() as conn:
            conn.execute("UPDATE items SET access_hash='scrypt$1$16384$8$1$bad$bad' WHERE id=?", (item,))

    protected.portal.call(corrupt)
    assert unlock(protected, item, "ABCDEFGH").status_code == 401


def test_existing_v2_database_keeps_public_behaviour(tmp_path, clock):
    import sqlite3

    path = tmp_path / "data"
    path.mkdir(parents=True, mode=0o700)
    conn = sqlite3.connect(path / "page-drop.db")
    conn.executescript("""
    CREATE TABLE items (
     id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
     upload_started_at INTEGER NOT NULL, created_at INTEGER, expires_at INTEGER,
     ttl_seconds INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
     label TEXT NOT NULL DEFAULT '',
     archive_sha256 TEXT NOT NULL, archive_bytes INTEGER,
     bytes INTEGER, file_count INTEGER, dir_count INTEGER,
     accounted_bytes INTEGER NOT NULL DEFAULT 0,
     state TEXT NOT NULL, committed_at INTEGER, deleted_at INTEGER, delete_reason TEXT,
     error_code TEXT, error_message TEXT
    );
    CREATE TABLE files (item_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
     mime TEXT NOT NULL, PRIMARY KEY(item_id, path));
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO meta VALUES ('schema_version', '2');
    """)
    conn.commit()
    conn.close()
    from app.db import Database

    Database(path / "page-drop.db").initialize()
    with sqlite3.connect(path / "page-drop.db") as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(items)")}
        assert {"access_mode", "access_hash", "access_revision"} <= columns
        assert check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"


def test_malformed_cookie_is_rejected_without_server_error(protected):
    payload = code_publish(protected)
    item = payload["id"]
    name = "pd_" + item.replace("-", "")
    for value in ("1.1.0.200.not-hex", "1.1.0.200", "garbage", "1.1.0.200."):
        response = protected.get(f"/{item}/", headers={"Cookie": f"{name}={value}"})
        assert response.status_code == 200
        assert 'name="code"' in response.text


def test_non_ascii_session_value_is_rejected():
    from app import access

    # httpx 不允许非 ASCII 请求头，这里直接校验签名解析不会抛错。
    assert access.verify_session(bytes.fromhex(SESSION_KEY), "abc", "1.1.100.200.Ã¼", 100) is None


def test_unlock_normalizes_case_and_surrounding_space(protected):
    payload = code_publish(protected)
    assert unlock(protected, payload["id"], f"  {payload['access_code'].lower()} ").status_code == 303


def test_reset_during_unlock_does_not_grant_new_revision(protected, monkeypatch):
    payload = code_publish(protected)
    item, old_code = payload["id"], payload["access_code"]
    service = protected.app.state.publisher
    from app import access as access_module

    real_verify = access_module.verify_code

    def verify_then_reset(record, code):
        if code == old_code:
            protected.portal.call(service.reset_code, item, 1)
        return real_verify(record, code)

    monkeypatch.setattr(access_module, "verify_code", verify_then_reset)
    status, _ = protected.portal.call(service.unlock, item, old_code)
    assert status == "denied"


def test_cookie_verification_accepts_other_valid_cost_parameters(tmp_path, clock):
    import base64
    import hashlib
    import os

    from app import access as access_module
    from app.config import Settings

    code = "ABCDEFGH"
    salt = os.urandom(16)
    derived = hashlib.scrypt(code.encode(), salt=salt, n=8192, r=8, p=1, dklen=32,
                             maxmem=64 * 1024 * 1024)
    record = "$".join(("scrypt", "1", "8192", "8", "1",
                       base64.b64encode(salt).decode(), base64.b64encode(derived).decode()))
    assert access_module.verify_code(record, code)
    assert not access_module.verify_code(record, "ZZZZZZZZ")
    assert not access_module.verify_code("scrypt$1$2$2$1$AAAA$AAAA", code)

    settings = Settings("testing-token", tmp_path / "data", "", SESSION_KEY, "172.17.0.0/16")
    with TestClient(create_app(settings, clock), base_url="https://testserver") as client:
        service = client.app.state.publisher
        assert service.trusts_proxy("172.17.0.1")
        assert service.trusts_proxy("127.0.0.1")
        assert not service.trusts_proxy("203.0.113.9")
        assert not service.trusts_proxy("not-an-ip")


def test_limiter_counts_every_submit_and_only_failures_per_pair(tmp_path):
    from app import access as access_module
    from app.config import Settings

    settings = Settings("testing-token", tmp_path / "data", "", SESSION_KEY)
    with TestClient(create_app(settings, FakeClock()), base_url="https://testserver") as client:
        limiter = client.app.state.publisher.limiter
        for _ in range(100):
            assert limiter.submit("item-a", "203.0.113.7")
        assert limiter.blocked("item-a", "203.0.113.7") > 0
        # 另一个客户端不受该 IP 预算影响
        assert limiter.blocked("item-b", "203.0.113.8") == 0


def test_unknown_uuid_does_not_register_lock(protected):
    service = protected.app.state.publisher
    for _ in range(5):
        assert protected.get(f"/{uuid.uuid4()}/").status_code == 404
    assert service.locks.entries == {}
