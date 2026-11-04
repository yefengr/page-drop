import asyncio
import gzip
import hashlib
import threading
import uuid

import pytest
from starlette.testclient import TestClient

from app import config as C
from app.archive import InvalidArtifact
from app.asgi import create_app
from app.config import ServiceError
from app.routes import LeaseResponse
from app.service import worker
import app.service as implementation
from conftest import AUTH, headers, make_tar, maintain, post, row, service


class BodyRequest:
    def __init__(self, body):
        self.body = body

    async def stream(self):
        yield self.body


def test_cancelled_open_disposes_returned_resource(client):
    started, resume = threading.Event(), threading.Event()
    closed = []

    def open_late():
        started.set()
        resume.wait()
        return "resource"

    async def scenario():
        task = asyncio.create_task(worker(open_late, dispose=closed.append))
        await asyncio.to_thread(started.wait)
        task.cancel()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == ["resource"]

    client.portal.call(scenario)


def test_cancel_waits_for_parser_before_directory_cleanup(client, monkeypatch):
    current = service(client)
    started = threading.Event()
    stopped_with_directory_intact = []
    body, key = make_tar(), str(uuid.uuid4())

    def parser(archive, root, cancel, disk_check):
        started.set()
        assert cancel.wait(5)
        stopped_with_directory_intact.append(root.exists())
        raise InvalidArtifact("Cancelled")

    monkeypatch.setattr(implementation, "extract", parser)

    async def scenario():
        task = asyncio.create_task(current.publish(BodyRequest(body), key, 600, "",
            hashlib.sha256(body).hexdigest(), None))
        await asyncio.to_thread(started.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped_with_directory_intact == [True]
        assert not current.active and current.ledger.total == 0
        assert (await current.io(current.repo.by_key, key))["state"] == "deleted"

    client.portal.call(scenario)


def test_cancel_during_initial_row_creation_rolls_back_reservation(client, monkeypatch):
    current = service(client)
    original = current.repo.insert
    started, resume = threading.Event(), threading.Event()
    body, key = make_tar(), str(uuid.uuid4())

    def insert(*args):
        original(*args)
        started.set()
        resume.wait()

    monkeypatch.setattr(current.repo, "insert", insert)

    async def scenario():
        task = asyncio.create_task(current.publish(BodyRequest(body), key, 600, "",
            hashlib.sha256(body).hexdigest(), None))
        await asyncio.to_thread(started.wait)
        task.cancel()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert current.ledger.total == 0 and not current.active
        assert (await current.io(current.repo.by_key, key))["state"] == "deleted"

    client.portal.call(scenario)


@pytest.mark.parametrize("timeout", ["UPLOAD_TOTAL_TIMEOUT", "UPLOAD_IDLE_TIMEOUT"])
def test_upload_timeouts_use_unified_cleanup(client, monkeypatch, timeout):
    monkeypatch.setattr(C, timeout, 0)
    response = post(client)
    assert response.status_code == 422
    assert service(client).ledger.total == 0 and not service(client).active


def test_archive_actual_budget_and_compressed_input(client, monkeypatch):
    monkeypatch.setattr(C, "MAX_ARCHIVE", 512)
    body = make_tar()
    request_headers = headers(body)
    # An iterable body has no Content-Length; enforcement must count actual chunks.
    response = client.post("/api/v1/publishes?ttl=10m", content=iter([body]), headers=request_headers)
    assert response.status_code == 413
    assert service(client).ledger.total == 0
    monkeypatch.setattr(C, "MAX_ARCHIVE", 256 * C.MIB)
    assert post(client, body=gzip.compress(body)).status_code == 422
    assert service(client).ledger.total == 0


def test_disk_admission_does_not_make_health_fail(client, monkeypatch):
    def full(_=False):
        raise ServiceError("QUOTA_EXCEEDED", "Disk reserve would be exceeded")
    monkeypatch.setattr(service(client), "disk_check", full)
    assert post(client).status_code == 507
    assert client.get("/healthz").status_code == 200


def test_existing_empty_database_is_not_reinitialized(settings, clock):
    settings.data_dir.mkdir()
    (settings.data_dir / "page-drop.db").touch()
    sentinel = settings.data_dir / "items" / str(uuid.uuid4())
    sentinel.mkdir(parents=True)
    (sentinel / "index.html").write_bytes(b"keep")
    with TestClient(create_app(settings, clock)) as client:
        assert client.get("/healthz").status_code == 503
        maintain(client)
        assert sentinel.exists()


@pytest.mark.parametrize("new_database", [False, True])
def test_legacy_database_blocks_initialization_and_cleanup(settings, clock, monkeypatch, caplog, new_database):
    settings.data_dir.mkdir()
    legacy = settings.data_dir / "edge-publish.db"
    legacy.write_bytes(b"legacy database sentinel")
    database = settings.data_dir / "page-drop.db"
    if new_database:
        database.write_bytes(b"new database sentinel")
    for area in ("tmp", "items", "trash"):
        content = settings.data_dir / area / str(uuid.uuid4())
        content.mkdir(parents=True)
        (content / "index.html").write_bytes(b"preserve existing content")

    def contents():
        return {str(path.relative_to(settings.data_dir)): path.read_bytes()
                for path in settings.data_dir.rglob("*") if path.is_file() and path.name != "service.lock"}

    before = contents()

    def forbidden(*args):
        pytest.fail("Legacy storage must not enter initialization, recovery, or cleanup")

    monkeypatch.setattr(implementation.Database, "initialize", forbidden)
    monkeypatch.setattr("app.recovery.recover", forbidden)
    monkeypatch.setattr(implementation.Publisher, "cleanup", forbidden)
    with TestClient(create_app(settings, clock)) as client:
        assert client.get("/healthz").status_code == 503
        assert client.get("/").status_code == 503
        assert post(client).status_code == 503
        maintain(client)
        clock.advance(C.DB_RETRY_INTERVAL + C.ORPHAN_GRACE)
        assert client.get("/healthz").status_code == 503
        maintain(client)
        assert contents() == before
        assert database.exists() == new_database
    assert contents() == before
    assert "Legacy edge-publish.db found" in caplog.text
    assert "database initialization and recovery are disabled" in caplog.text


@pytest.mark.parametrize("area", ["tmp", "items", "trash"])
@pytest.mark.parametrize("entry_kind", ["file", "empty_directory"])
def test_missing_database_with_existing_content_stays_unavailable(
        settings, clock, monkeypatch, caplog, area, entry_kind):
    for name in ("tmp", "items", "trash"):
        (settings.data_dir / name).mkdir(parents=True)
    entry = settings.data_dir / area / "existing-content"
    if entry_kind == "file":
        entry.write_bytes(b"preserve existing content")
    else:
        entry.mkdir()

    def contents():
        return {str(path.relative_to(settings.data_dir)): path.read_bytes() if path.is_file() else None
                for path in settings.data_dir.rglob("*") if path.name != "service.lock"}

    before = contents()

    def forbidden(*args):
        pytest.fail("Unowned storage must not enter initialization, recovery, or cleanup")

    monkeypatch.setattr(implementation.Database, "initialize", forbidden)
    monkeypatch.setattr("app.recovery.recover", forbidden)
    monkeypatch.setattr(implementation.Publisher, "cleanup", forbidden)
    with TestClient(create_app(settings, clock)) as client:
        assert not service(client).ready
        assert client.get("/healthz").status_code == 503
        assert client.get("/").status_code == 503
        assert post(client).status_code == 503
        maintain(client)
        clock.advance(C.DB_RETRY_INTERVAL + C.ORPHAN_GRACE)
        assert client.get("/healthz").status_code == 503
        maintain(client)
        assert contents() == before
        assert not (settings.data_dir / "page-drop.db").exists()
    assert contents() == before
    assert f"Missing page-drop.db with existing content in {area}" in caplog.text


@pytest.mark.parametrize("area", ["tmp", "items", "trash"])
def test_initial_directory_read_failure_preserves_storage(settings, clock, monkeypatch, caplog, area):
    directory = settings.data_dir / area
    directory.mkdir(parents=True)
    sentinel = directory / "index.html"
    sentinel.write_bytes(b"preserve unreadable content")
    original = implementation.os.scandir
    attempts = []

    def unreadable(path):
        if str(path) == str(directory):
            attempts.append(path)
            raise PermissionError("synthetic directory listing failure")
        return original(path)

    def forbidden(*args):
        pytest.fail("Unreadable storage must not enter initialization, recovery, or cleanup")

    monkeypatch.setattr(implementation.os, "scandir", unreadable)
    monkeypatch.setattr(implementation.Database, "initialize", forbidden)
    monkeypatch.setattr("app.recovery.recover", forbidden)
    monkeypatch.setattr(implementation.Publisher, "cleanup", forbidden)
    with TestClient(create_app(settings, clock)) as client:
        assert attempts
        assert not service(client).ready
        assert client.get("/healthz").status_code == 503
        assert post(client).status_code == 503
        maintain(client)
        clock.advance(C.DB_RETRY_INTERVAL + C.ORPHAN_GRACE)
        assert client.get("/healthz").status_code == 503
        maintain(client)
        assert not (settings.data_dir / "page-drop.db").exists()
        assert sentinel.read_bytes() == b"preserve unreadable content"
    assert sentinel.read_bytes() == b"preserve unreadable content"
    assert "Cannot inspect storage directories" in caplog.text


def test_committed_manifest_survives_real_restart(settings, clock):
    with TestClient(create_app(settings, clock)) as client:
        item_id = post(client).json()["id"]
    clock.advance(50)
    with TestClient(create_app(settings, clock)) as client:
        assert client.get(f"/{item_id}/").content == b"<h1>Hello</h1>"
        assert client.get(f"/{item_id}/assets/app.css").content == b"body{}"
        assert row(client, item_id)["expires_at"] - clock.now() == 550


def test_unlisted_file_is_not_served_and_reconciliation_closes_item(client):
    item_id = post(client).json()["id"]
    current = service(client)
    (current.root / "items" / item_id / "unlisted.txt").write_bytes(b"not in manifest")
    assert client.get(f"/{item_id}/unlisted.txt").status_code == 404
    maintain(client)
    assert row(client, item_id)["state"] == "deleted" and current.ledger.total == 0


def test_deleted_residual_is_reaccounted_during_maintenance(client, monkeypatch):
    item_id = post(client).json()["id"]
    current = service(client)
    client.delete(f"/api/v1/publishes/{item_id}", headers=AUTH)
    maintain(client)
    leftover = current.root / "tmp" / item_id
    leftover.mkdir()
    (leftover / "index.html").write_bytes(b"residual")
    original = current.remove_trash

    def unavailable(_):
        raise OSError("ordinary failure")

    monkeypatch.setattr(current, "remove_trash", unavailable)
    maintain(client)
    assert row(client, item_id)["state"] == "deleting"
    assert current.ledger.total == 8
    monkeypatch.setattr(current, "remove_trash", original)
    maintain(client)
    assert current.ledger.total == 0


def test_response_already_started_finishes_after_delete(client):
    item_id = post(client).json()["id"]
    current = service(client)

    async def scenario():
        status, value = await current.admit(item_id, "index.html")
        assert status == "ok"
        handle, entry = value
        started, resume, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
        messages = []

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.start":
                started.set()
                await resume.wait()

        async def receive():
            await never.wait()

        response = LeaseResponse(current, item_id, handle, entry, (0, entry["size"], 200), False)
        task = asyncio.create_task(response({}, receive, send))
        await started.wait()
        await current.delete(item_id)
        await current.cleanup(item_id)
        assert current.ledger.total > 0
        assert await current.admit(item_id, "index.html") == ("missing", None)
        resume.set()
        await task
        assert b"".join(m.get("body", b"") for m in messages) == b"<h1>Hello</h1>"
        await current.cleanup(item_id)
        assert current.ledger.total == 0 and not current.leases.handles

    client.portal.call(scenario)


def test_response_idle_timeout_releases_lease(client, monkeypatch):
    item_id = post(client).json()["id"]
    current = service(client)
    monkeypatch.setattr(C, "DOWNLOAD_IDLE_TIMEOUT", 0)

    async def scenario():
        status, value = await current.admit(item_id, "index.html")
        assert status == "ok"
        handle, entry = value
        never = asyncio.Event()

        async def send(_):
            await never.wait()

        async def receive():
            await never.wait()

        response = LeaseResponse(current, item_id, handle, entry, (0, entry["size"], 200), False)
        with pytest.raises(TimeoutError):
            await response({}, receive, send)
        assert not current.leases.handles and handle.closed

    client.portal.call(scenario)


def test_response_disconnect_releases_lease(client):
    item_id = post(client).json()["id"]
    current = service(client)

    async def scenario():
        status, value = await current.admit(item_id, "index.html")
        assert status == "ok"
        handle, entry = value
        never = asyncio.Event()

        async def send(_):
            await never.wait()

        async def receive():
            return {"type": "http.disconnect"}

        response = LeaseResponse(current, item_id, handle, entry, (0, entry["size"], 200), False)
        await response({}, receive, send)
        assert not current.leases.handles and handle.closed

    client.portal.call(scenario)
