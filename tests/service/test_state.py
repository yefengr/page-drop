import asyncio
import hashlib
import os
from pathlib import Path
import sqlite3
import uuid

import pytest
from starlette.testclient import TestClient

from app import config as C
from app.archive import inspect_tree
from app.asgi import create_app
from app.config import ServiceError
from app.lock import InstanceLock
from app.service import Publisher
from conftest import AUTH, headers, make_tar, maintain, post, row, service


def test_delete_and_retention(client, clock):
    item_id = post(client).json()["id"]
    size = row(client, item_id)["bytes"]
    assert service(client).ledger.total == size
    for _ in range(2):
        assert client.delete(f"/api/v1/publishes/{item_id}", headers=AUTH).status_code == 204
    assert client.get(f"/{item_id}/").status_code == 404
    maintain(client)
    assert row(client, item_id)["state"] == "deleted"
    assert service(client).ledger.total == 0
    clock.advance(86400)
    maintain(client)
    assert client.get(f"/api/v1/publishes/{item_id}", headers=AUTH).status_code == 404
    assert client.delete(f"/api/v1/publishes/{uuid.uuid4()}", headers=AUTH).status_code == 404


def test_inflight_lease_retains_accounting(client, clock):
    item_id = post(client).json()["id"]
    current = service(client)
    status, admitted = client.portal.call(current.admit, item_id, "index.html")
    assert status == "ok"
    handle, entry = admitted
    size = current.ledger.total
    clock.advance(600)
    assert client.get(f"/{item_id}/").status_code == 404
    maintain(client)
    assert row(client, item_id)["state"] == "deleting"
    assert current.ledger.total == size
    assert client.portal.call(current.io, handle.read) == b"<h1>Hello</h1>"
    client.portal.call(current.release, item_id, handle)
    maintain(client)
    assert current.ledger.total == 0


def test_clock_rollback_does_not_extend_lifetime(client, clock):
    item_id = post(client).json()["id"]
    clock.mono += 600
    clock.wall -= 10000
    assert client.get(f"/{item_id}/").status_code == 404
    clock.mono -= 600
    assert client.get(f"/{item_id}/").status_code == 404


def test_cleanup_failure_preserves_accounting_and_retries(client, monkeypatch):
    item_id = post(client).json()["id"]
    current = service(client)
    size = current.ledger.total
    original = current.remove_trash

    def unavailable(_):
        raise OSError("ordinary I/O failure")

    monkeypatch.setattr(current, "remove_trash", unavailable)
    client.delete(f"/api/v1/publishes/{item_id}", headers=AUTH)
    maintain(client)
    assert row(client, item_id)["state"] == "deleting"
    assert current.ledger.total == size
    assert (current.root / "trash" / item_id).exists()
    monkeypatch.setattr(current, "remove_trash", original)
    maintain(client)
    assert row(client, item_id)["state"] == "deleted" and current.ledger.total == 0


def seed(settings, clock, phase):
    publisher = Publisher(settings, clock)
    publisher.prepare()
    publisher.db.initialize()
    item_id, key = str(uuid.uuid4()), str(uuid.uuid4())
    publisher.repo.insert(item_id, key, clock.now(), 600, "", hashlib.sha256(make_tar()).hexdigest())
    root = publisher.root / "tmp" / item_id
    root.mkdir()
    (root / "index.html").write_bytes(b"ok")
    manifest = inspect_tree(root)
    if phase != "tmp":
        publisher.repo.checkpoint(item_id, len(make_tar()), manifest)
        publisher.rename_content(item_id)
    if phase in ("committed", "missing", "deleted-residual"):
        publisher.repo.commit(item_id, manifest, clock.now())
    if phase == "missing":
        (publisher.root / "items" / item_id / "index.html").unlink()
        (publisher.root / "items" / item_id).rmdir()
    if phase == "deleted-residual":
        publisher.repo.deleting(item_id, manifest.bytes, "user")
        publisher.repo.deleted(item_id, clock.now())
    publisher.instance_lock.release()
    return item_id


@pytest.mark.parametrize("phase,state", [("tmp", "deleted"), ("renamed", "committed"),
    ("committed", "committed"), ("missing", "deleted"), ("deleted-residual", "deleted")])
def test_recovery_phases(settings, clock, phase, state):
    item_id = seed(settings, clock, phase)
    if phase == "renamed":
        clock.advance(300)
    with TestClient(create_app(settings, clock)) as client:
        actual = row(client, item_id)
        assert actual["state"] == state
        assert client.get("/healthz").status_code == 200
        response = client.get(f"/{item_id}/")
        assert response.status_code == (200 if state == "committed" else 404)
        assert service(client).ledger.total == (2 if state == "committed" else 0)
        if phase == "renamed":
            assert actual["created_at"] == clock.now() and actual["expires_at"] == clock.now() + 600


def test_corrupt_database_does_not_delete_storage(settings, clock):
    settings.data_dir.mkdir()
    directory = settings.data_dir / "tmp" / str(uuid.uuid4())
    directory.mkdir(parents=True)
    sentinel = directory / "index.html"
    sentinel.write_bytes(b"preserve")
    (settings.data_dir / "page-drop.db").write_bytes(b"not a database")
    with TestClient(create_app(settings, clock)) as client:
        assert client.get("/healthz").status_code == 503
        assert post(client).status_code == 503
        assert client.get(f"/{directory.name}/").status_code == 503
        for method in ("GET", "HEAD"):
            response = client.request(method, "/")
            assert response.status_code == 503
            assert response.headers["cache-control"] == "no-store"
            if method == "HEAD":
                assert response.content == b""
        maintain(client)
        assert sentinel.read_bytes() == b"preserve"


def test_orphan_observation_and_multiple_residuals(settings, clock):
    item_id = seed(settings, clock, "tmp")
    residual = settings.data_dir / "items" / item_id
    residual.mkdir()
    (residual / "index.html").write_bytes(b"second copy")
    orphan = settings.data_dir / "items" / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "index.html").write_bytes(b"orphan")
    with TestClient(create_app(settings, clock)) as client:
        assert row(client, item_id)["state"] == "deleted"
        assert orphan.exists() and service(client).ledger.total == 6
        clock.advance(3599)
        maintain(client)
        assert orphan.exists()
        clock.advance(1)
        maintain(client)
        assert not orphan.exists() and service(client).ledger.total == 0


def test_single_instance_lock(settings):
    settings.data_dir.mkdir()
    first = InstanceLock(settings.data_dir / "service.lock")
    second = InstanceLock(settings.data_dir / "service.lock")
    first.acquire()
    try:
        with pytest.raises(SystemExit) as failure:
            second.acquire()
        assert failure.value.code == 2
    finally:
        first.release()
    second.acquire()
    second.release()


def test_live_upload_busy_idempotency_slow_clock_and_maintenance(client, clock):
    current = service(client)
    body, key = make_tar(), str(uuid.uuid4())
    digest = hashlib.sha256(body).hexdigest()

    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()

        class Request:
            async def stream(self):
                yield body[:512]
                clock.advance(300)
                started.set()
                await resume.wait()
                yield body[512:]

        task = asyncio.create_task(current.publish(Request(), key, 600, "", digest, None))
        await started.wait()
        try:
            assert current.ledger.total == C.MAX_CONTENT
            with pytest.raises(ServiceError) as conflict:
                await current.begin(key, 600, "", digest)
            assert conflict.value.code == "CONFLICT"
            with pytest.raises(ServiceError) as busy:
                await current.begin(str(uuid.uuid4()), 600, "", digest)
            assert busy.value.code == "BUSY"
            from app.maintenance import maintain
            await maintain(current)
            assert current.active
            await current.check_storage()
        finally:
            resume.set()
        result, fresh, code = await task
        assert fresh and code is None and result["created_at"] == clock.now()
        assert result["expires_at"] - clock.now() == 600
        assert current.ledger.total == result["bytes"]

    client.portal.call(scenario)


def test_cancel_active_upload_cannot_commit(client):
    current = service(client)
    body, key = make_tar(), str(uuid.uuid4())

    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()

        class Request:
            async def stream(self):
                yield body[:512]
                started.set()
                await resume.wait()
                yield body[512:]

        task = asyncio.create_task(current.publish(Request(), key, 600, "", hashlib.sha256(body).hexdigest(), None))
        await started.wait()
        record = await current.io(current.repo.by_key, key)
        await current.delete(record["id"])
        assert current.ledger.total == C.MAX_CONTENT
        resume.set()
        with pytest.raises(ServiceError):
            await task
        record = await current.io(current.repo.by_key, key)
        assert record["state"] == "deleted"
        assert current.ledger.total == 0

    client.portal.call(scenario)


def test_download_limit_and_health_independence(client, monkeypatch):
    item_id = post(client).json()["id"]
    current = service(client)
    monkeypatch.setattr(C, "MAX_DOWNLOADS", 1)
    status, admitted = client.portal.call(current.admit, item_id, "index.html")
    assert status == "ok"
    handle, _ = admitted
    try:
        assert client.get(f"/{item_id}/").status_code == 503
        assert client.get("/healthz").status_code == 200
    finally:
        client.portal.call(current.release, item_id, handle)


def test_runtime_database_fault_stops_reclamation(client, monkeypatch):
    item_id = post(client).json()["id"]
    current = service(client)

    def broken(*_):
        raise sqlite3.DatabaseError("database is unavailable")

    monkeypatch.setattr(current.repo, "get", broken)
    assert client.get(f"/{item_id}/").status_code == 503
    assert client.get("/healthz").status_code == 503
    maintain(client)
    assert (current.root / "items" / item_id / "index.html").exists()


def test_schema_migrates_old_database(tmp_path):
    import sqlite3
    from app.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE items (
     id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
     upload_started_at INTEGER NOT NULL, created_at INTEGER, expires_at INTEGER,
     ttl_seconds INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
     archive_sha256 TEXT NOT NULL, archive_bytes INTEGER,
     bytes INTEGER, file_count INTEGER, dir_count INTEGER,
     accounted_bytes INTEGER NOT NULL DEFAULT 0,
     state TEXT NOT NULL, committed_at INTEGER, deleted_at INTEGER, delete_reason TEXT,
     error_code TEXT, error_message TEXT
    );
    CREATE TABLE files (item_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
     mime TEXT NOT NULL, PRIMARY KEY(item_id, path));
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO meta VALUES ('schema_version', '1');
    INSERT INTO items (id, idempotency_key, upload_started_at, ttl_seconds, archive_sha256,
     accounted_bytes, state) VALUES ('old-id','old-key',1,600,'x',0,'deleted');
    """)
    conn.commit()
    conn.close()

    Database(path).initialize()
    with sqlite3.connect(path) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(items)")}
        assert {"label", "access_mode", "access_hash", "access_revision"} <= columns
        row = check.execute("SELECT label, state FROM items WHERE id='old-id'").fetchone()
        assert row == ("", "deleted")
        version = check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert version == "3"
