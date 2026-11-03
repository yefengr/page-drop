from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import maintenance, recovery, routes, service
from app.asgi import create_app
from app.config import Settings
from app.pages import LANDING, NOT_FOUND


@pytest.fixture(autouse=True)
def isolated_service_environment(monkeypatch):
    for prefix in ("PAGE_DROP", "EDGE_PUBLISH"):
        for suffix in ("TOKEN", "DATA_DIR", "PUBLIC_URL"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)


@pytest.mark.parametrize("prefix", ["PAGE_DROP", "EDGE_PUBLISH"])
def test_each_environment_namespace_is_supported(monkeypatch, tmp_path, prefix):
    data_dir = tmp_path / "configured"
    monkeypatch.setenv(f"{prefix}_TOKEN", "synthetic-token")
    monkeypatch.setenv(f"{prefix}_DATA_DIR", str(data_dir))
    monkeypatch.setenv(f"{prefix}_PUBLIC_URL", "  https://pages.example.test///  ")
    settings = Settings.from_env()
    assert settings.token == "synthetic-token"
    assert settings.data_dir == data_dir
    assert settings.public_url == "https://pages.example.test"
    assert not data_dir.exists()


@pytest.mark.parametrize("suffix,attribute,new,old,expected", [
    ("TOKEN", "token", "new-token", "old-token", "new-token"),
    ("TOKEN", "token", None, "old-token", "old-token"),
    ("TOKEN", "token", "", "old-token", "old-token"),
    ("TOKEN", "token", "  ", " old-token ", "old-token"),
    ("TOKEN", "token", "new-token", "", "new-token"),
    ("DATA_DIR", "data_dir", "/new-data", "/old-data", Path("/new-data")),
    ("DATA_DIR", "data_dir", None, "/old-data", Path("/old-data")),
    ("DATA_DIR", "data_dir", "", "/old-data", Path("/old-data")),
    ("DATA_DIR", "data_dir", "/new-data", "", Path("/new-data")),
    ("DATA_DIR", "data_dir", "", "", Path("/data")),
    ("PUBLIC_URL", "public_url", "https://new.example.test/", "https://old.example.test/",
     "https://new.example.test"),
    ("PUBLIC_URL", "public_url", None, "https://old.example.test/", "https://old.example.test"),
    ("PUBLIC_URL", "public_url", "", "https://old.example.test/", "https://old.example.test"),
    ("PUBLIC_URL", "public_url", "  ", " https://old.example.test/ ", "https://old.example.test"),
    ("PUBLIC_URL", "public_url", "https://new.example.test/", "", "https://new.example.test"),
    ("PUBLIC_URL", "public_url", "", "", ""),
])
def test_nonempty_new_values_take_priority(monkeypatch, suffix, attribute, new, old, expected):
    if suffix != "TOKEN":
        monkeypatch.setenv("PAGE_DROP_TOKEN", "synthetic-token")
    if new is not None:
        monkeypatch.setenv(f"PAGE_DROP_{suffix}", new)
    monkeypatch.setenv(f"EDGE_PUBLISH_{suffix}", old)
    assert getattr(Settings.from_env(), attribute) == expected


@pytest.mark.parametrize("prefix", ["PAGE_DROP", "EDGE_PUBLISH"])
def test_storage_and_public_url_defaults(monkeypatch, prefix):
    monkeypatch.setenv(f"{prefix}_TOKEN", "synthetic-token")
    settings = Settings.from_env()
    assert settings.data_dir == Path("/data")
    assert settings.public_url == ""


@pytest.mark.parametrize("new", [None, "", "  "])
@pytest.mark.parametrize("old", [None, "", "  "])
def test_missing_token_names_both_supported_variables(monkeypatch, new, old):
    if new is not None:
        monkeypatch.setenv("PAGE_DROP_TOKEN", new)
    if old is not None:
        monkeypatch.setenv("EDGE_PUBLISH_TOKEN", old)
    with pytest.raises(RuntimeError) as failure:
        Settings.from_env()
    assert "PAGE_DROP_TOKEN" in str(failure.value)
    assert "EDGE_PUBLISH_TOKEN" in str(failure.value)


@pytest.mark.parametrize("precreate_directories", [False, True])
def test_page_drop_database_is_created_in_a_fresh_directory(settings, clock, precreate_directories):
    if precreate_directories:
        for area in ("tmp", "items", "trash"):
            (settings.data_dir / area).mkdir(parents=True)
    with TestClient(create_app(settings, clock)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.app.state.publisher.db.path == settings.data_dir / "page-drop.db"
        assert (settings.data_dir / "page-drop.db").is_file()
        assert not (settings.data_dir / "edge-publish.db").exists()


@pytest.mark.parametrize("module", [service, maintenance, recovery, routes])
def test_service_loggers_use_page_drop(module):
    assert module.LOG.name == "page_drop"


@pytest.mark.parametrize("page,title", [
    (LANDING, "PageDrop · 临时发布服务"),
    (NOT_FOUND, "链接不可用 · PageDrop"),
])
def test_builtin_pages_use_page_drop_titles(page, title):
    assert f"<title>{title}</title>" in page
    assert "edge-publish" not in page
