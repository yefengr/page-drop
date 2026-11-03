import hashlib
import io
from pathlib import Path
import sys
import tarfile
import uuid

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "service"))

from app.asgi import create_app
from app.config import Settings


class FakeClock:
    def __init__(self):
        self.wall, self.mono = 1800000000, 1000.0

    def now(self):
        return int(self.wall)

    def monotonic(self):
        return self.mono

    def monotonic_deadline(self, expires_at):
        return self.mono + max(0, expires_at - self.now())

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def make_tar(entries=None):
    entries = entries if entries is not None else [("index.html", b"<h1>Hello</h1>"), ("assets/app.css", b"body{}")]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(data, tarfile.TarInfo):
                archive.addfile(data)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def headers(body, key=None):
    return {"Authorization": "Bearer testing-token", "Content-Type": "application/x-tar",
            "Idempotency-Key": key or str(uuid.uuid4()),
            "X-Archive-SHA256": hashlib.sha256(body).hexdigest()}


def post(client, body=None, key=None, ttl="10m", title=""):
    body = make_tar() if body is None else body
    return client.post("/api/v1/publishes", params={"ttl": ttl, "title": title},
                       content=body, headers=headers(body, key))


AUTH = {"Authorization": "Bearer testing-token"}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings(tmp_path):
    return Settings("testing-token", tmp_path / "data")


@pytest.fixture
def client(settings, clock):
    with TestClient(create_app(settings, clock)) as result:
        yield result


def service(client):
    return client.app.state.publisher


def row(client, item_id):
    current = service(client)
    return client.portal.call(current.io, current.repo.get, item_id)


def maintain(client):
    from app.maintenance import maintain as run
    client.portal.call(run, service(client))
