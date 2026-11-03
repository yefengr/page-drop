import sys
from pathlib import Path

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "service"))

from app.asgi import create_app
from app.config import Settings

from conftest import AUTH, FakeClock, post


def test_public_url_overrides_proxy_scheme(tmp_path):
    settings = Settings("testing-token", tmp_path / "data", "https://pub.example.test")
    with TestClient(create_app(settings, FakeClock())) as client:
        created = post(client, ttl="1d")
        assert created.status_code == 201
        item_id = created.json()["id"]
        assert created.json()["url"] == f"https://pub.example.test/{item_id}/"

        status = client.get(f"/api/v1/publishes/{item_id}", headers=AUTH).json()
        assert status["url"] == f"https://pub.example.test/{item_id}/"


def test_falls_back_to_request_origin(settings, client):
    created = post(client, ttl="1d")
    item_id = created.json()["id"]
    assert created.json()["url"] == f"http://testserver/{item_id}/"


def test_transient_database_error_recovers(client, clock):
    import sqlite3
    from conftest import AUTH, service

    publisher = service(client)
    original = publisher.repo.list

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    publisher.repo.list = broken
    try:
        assert client.get("/api/v1/publishes", headers=AUTH).status_code == 503
        assert client.get("/healthz").status_code == 503
    finally:
        publisher.repo.list = original

    clock.advance(11)
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/publishes", headers=AUTH).status_code == 200


def test_begin_rollback_releases_reservation(client, clock):
    import sqlite3
    from conftest import AUTH, post, service

    publisher = service(client)
    original_insert = publisher.repo.insert
    original_get = publisher.repo.get

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    publisher.repo.insert = broken
    publisher.repo.get = broken
    try:
        assert post(client, ttl="1d").status_code == 503
    finally:
        publisher.repo.insert = original_insert
        publisher.repo.get = original_get

    assert 200 * 1024 * 1024 not in publisher.ledger.reservations.values()
    clock.advance(11)
    assert client.get("/healthz").status_code == 200
    assert post(client, ttl="1d").status_code == 201
