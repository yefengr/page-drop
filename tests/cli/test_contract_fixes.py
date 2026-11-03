import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_publish import CLI_PATH, cli, load_cli, site, stub

IDENTIFIER = "11111111-1111-4111-8111-111111111111"
SUCCESS = {
    "id": IDENTIFIER, "url": f"https://public.example/{IDENTIFIER}/",
    "created_at": 1, "expires_at": 86401, "bytes": 42, "file_count": 1,
}


def response(data, status=201):
    return status, {}, json.dumps(data).encode()


@pytest.mark.parametrize("field,value", [
    ("id", None), ("id", "not-a-uuid"), ("id", "11111111-1111-1111-8111-111111111111"),
    ("url", "relative/path"), ("url", f"http://public.example/{IDENTIFIER}/"),
    ("url", "https://public.example/22222222-2222-4222-8222-222222222222/"),
    ("url", f"https://public.example/{IDENTIFIER}/?secret=x"),
    ("url", f"https://user:password@public.example/{IDENTIFIER}/"),
    ("url", f"https://public.example:bad/{IDENTIFIER}/"), ("url", []),
    ("url", f"https://public.example/{IDENTIFIER}///"),
    ("created_at", True), ("created_at", -1), ("created_at", 253402300800),
    ("expires_at", "86401"), ("expires_at", 1), ("expires_at", 0),
    ("bytes", False), ("bytes", -1), ("bytes", 200 * 1024 * 1024 + 1),
    ("file_count", True), ("file_count", 0), ("file_count", 5001),
])
def test_success_rejects_invalid_fields(field, value):
    module = load_cli()
    with pytest.raises(module.CliError):
        module.validate_success({**SUCCESS, field: value})


@pytest.mark.parametrize("field", list(SUCCESS))
def test_success_requires_every_field(field):
    module = load_cli()
    data = dict(SUCCESS)
    del data[field]
    with pytest.raises(module.CliError):
        module.validate_success(data)


@pytest.mark.parametrize("data", [None, [], True, "success", 1])
def test_success_requires_object(data):
    module = load_cli()
    with pytest.raises(module.CliError):
        module.validate_success(data)


@pytest.mark.parametrize("host", ["https://different-public.example/prefix", "http://127.0.0.1:8000", "http://[::1]:8000", "http://localhost"])
def test_success_accepts_public_override_loopback_and_old_timestamps(host):
    module = load_cli()
    result = module.validate_success({**SUCCESS, "url": f"{host}/{IDENTIFIER}/"})
    assert isinstance(result, module.ValidatedResult)


@pytest.mark.parametrize("body,status", [([], 201), ({}, 201), (SUCCESS, 202), (SUCCESS, 204)])
def test_bad_post_retains_complete_pending(cli, site, monkeypatch, body, status, capsys):
    monkeypatch.setattr(cli, "request", lambda *a, **kw: response(body, status))
    assert cli.main([str(site), "--ttl", "1d", "--json"]) == 2
    assert capsys.readouterr().out == ""
    pending = json.loads(cli.state_paths()["pending"].read_text())
    assert cli.state_paths()["archive"].exists()
    assert cli.sha256_file(pending["archive"]) == pending["archive_sha256"]
    assert not cli.state_paths()["history"].exists()


@pytest.mark.parametrize("change", [
    {"state": "uploading"}, {"state": "deleted"}, {"available": 1},
    {"available": "true"}, {"available": False}, {"ttl_seconds": True},
    {"title": None}, {"label": None}, {"archive_sha256": "0" * 64},
    {"url": "/bad"}, {"file_count": True},
])
def test_retry_confirmation_strict_and_preserves_state(cli, site, monkeypatch, change):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    record = cli.load_pending()
    data = {**SUCCESS, "state": "committed", "available": True,
            "archive_sha256": record["archive_sha256"], "ttl_seconds": 86400,
            "title": "", "label": "", **change}
    calls = []

    def query(*args, **kwargs):
        calls.append(args[1])
        return response(data, 200)

    monkeypatch.setattr(cli, "request", query)
    assert cli.main(["retry"]) == 2
    assert calls == ["GET"]
    assert cli.load_pending() == record
    assert cli.state_paths()["archive"].exists()
    assert not cli.state_paths()["history"].exists()


def test_retry_post_is_also_validated(cli, site, monkeypatch):
    cli.stub_state.fail_post = True
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    calls = []

    def query(config, method, *args, **kwargs):
        calls.append(method)
        return response({} if method == "POST" else {"error": {}}, 201 if method == "POST" else 404)

    monkeypatch.setattr(cli, "request", query)
    assert cli.main(["retry"]) == 2
    assert calls == ["GET", "POST"]
    assert cli.load_pending() is not None
    assert cli.state_paths()["archive"].exists()
    assert not cli.state_paths()["history"].exists()


def test_finalize_rejects_unvalidated_results(cli):
    with pytest.raises(cli.CliError):
        cli.finalize(SUCCESS, {"key": "test"})
    assert not cli.state_paths()["history"].exists()


@pytest.fixture
def isolated_env(tmp_path):
    home = tmp_path / "home"
    config = home / ".config" / "page-drop"
    config.mkdir(parents=True)
    (config / "config.json").write_text(json.dumps({"base_url": "http://127.0.0.1:9"}))
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                  "PAGE_DROP_TOKEN", "EDGE_PUBLISH_TOKEN")}
    env.update(HOME=str(home), PAGE_DROP_TOKEN="isolated-test-token", NO_PROXY="*",
               PYTHONDONTWRITEBYTECODE="1")
    return env, home


def test_real_entry_dry_run_and_help(site, isolated_env):
    env, home = isolated_env
    env.pop("PAGE_DROP_TOKEN", None)
    env.pop("EDGE_PUBLISH_TOKEN", None)
    result = subprocess.run([sys.executable, str(CLI_PATH), str(site), "--ttl", "1d", "--dry-run"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "可发布" in result.stdout
    assert not (home / ".local/state/publish/pending.json").exists()
    help_result = subprocess.run([sys.executable, str(CLI_PATH), "--help"], env=env,
                                 capture_output=True, text=True, timeout=10)
    assert help_result.returncode == 0
    assert "PageDrop 发布工具" in help_result.stdout
    assert "publish <dir>" in help_result.stdout
    assert "publish publish" not in help_result.stdout
    assert "_directory" not in help_result.stdout


def test_real_entry_publish_and_management(site, isolated_env, stub):
    env, home = isolated_env
    config = home / ".config/page-drop/config.json"
    config.write_text(json.dumps({"base_url": f"http://127.0.0.1:{stub.server_address[1]}"}))
    result = subprocess.run([sys.executable, str(CLI_PATH), str(site), "--ttl", "1d", "--json"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == IDENTIFIER
    for args in (["list", "--json"], ["status", IDENTIFIER, "--json"], ["url", IDENTIFIER],
                 ["rm", IDENTIFIER], ["discard-pending"]):
        managed = subprocess.run([sys.executable, str(CLI_PATH), *args], env=env,
                                 capture_output=True, text=True, timeout=10)
        assert managed.returncode == 0, managed.stderr
    assert len(stub.state.posts) == 1


def test_directory_named_management_command_uses_explicit_path(site, isolated_env):
    env, _ = isolated_env
    target = site.parent / "retry"
    site.rename(target)
    result = subprocess.run([sys.executable, str(CLI_PATH), "./retry", "--ttl", "1d", "--dry-run"],
                            cwd=target.parent, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
