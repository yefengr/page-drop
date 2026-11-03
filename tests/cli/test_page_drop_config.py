import json
from pathlib import Path

import pytest

from test_publish import load_cli


@pytest.fixture
def config_cli(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("PAGE_DROP_TOKEN", "EDGE_PUBLISH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return load_cli()


def write_config(directory, config):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")


def test_default_paths_keep_publish_state_and_base_url(config_cli):
    cli = config_cli
    assert cli.CONFIG_DIR == Path.home() / ".config" / "page-drop"
    assert cli.LEGACY_CONFIG_DIR == Path.home() / ".config" / "edge-publish"
    assert cli.STATE_DIR == Path.home() / ".local" / "state" / "publish"
    assert cli.DEFAULT_BASE_URL == ""
    assert cli.load_config() == {
        "base_url": cli.DEFAULT_BASE_URL,
        "token_file": str(cli.CONFIG_DIR / "token"),
        "timeout": 300,
    }
    assert cli.state_paths() == {
        "pending": cli.STATE_DIR / "pending.json",
        "archive": cli.STATE_DIR / "pending.tar",
        "history": cli.STATE_DIR / "history.jsonl",
        "lock": cli.STATE_DIR / "lock",
    }
    assert not cli.CONFIG_DIR.exists()
    assert not cli.LEGACY_CONFIG_DIR.exists()


def test_new_config_wins_with_its_own_default_token(config_cli):
    cli = config_cli
    write_config(cli.LEGACY_CONFIG_DIR, {
        "base_url": "https://legacy.example.test",
        "token_file": str(cli.LEGACY_CONFIG_DIR / "custom-token"),
        "timeout": 9,
    })
    write_config(cli.CONFIG_DIR, {"base_url": "https://new.example.test", "timeout": 17})
    (cli.CONFIG_DIR / "token").write_text("new-file-token\n", encoding="utf-8")
    config = cli.load_config()
    assert config == {"base_url": "https://new.example.test", "timeout": 17,
                      "token_file": str(cli.CONFIG_DIR / "token")}
    assert cli.load_token(config) == "new-file-token"


def test_empty_new_config_does_not_merge_legacy_values(config_cli):
    cli = config_cli
    write_config(cli.LEGACY_CONFIG_DIR, {
        "base_url": "https://legacy.example.test", "timeout": 9,
        "token_file": str(cli.LEGACY_CONFIG_DIR / "custom-token"),
    })
    write_config(cli.CONFIG_DIR, {})
    assert cli.load_config() == {
        "base_url": cli.DEFAULT_BASE_URL, "timeout": 300,
        "token_file": str(cli.CONFIG_DIR / "token"),
    }


@pytest.mark.parametrize("new_directory_exists", [False, True])
def test_legacy_config_used_only_when_new_config_is_absent(config_cli, new_directory_exists):
    cli = config_cli
    write_config(cli.LEGACY_CONFIG_DIR, {"base_url": "https://legacy.example.test", "timeout": 23})
    (cli.LEGACY_CONFIG_DIR / "token").write_text("legacy-file-token\n", encoding="utf-8")
    if new_directory_exists:
        cli.CONFIG_DIR.mkdir(parents=True)
        (cli.CONFIG_DIR / "token").write_text("unused-new-token", encoding="utf-8")
    legacy_before = (cli.LEGACY_CONFIG_DIR / "config.json").read_bytes()
    config = cli.load_config()
    assert config == {"base_url": "https://legacy.example.test", "timeout": 23,
                      "token_file": str(cli.LEGACY_CONFIG_DIR / "token")}
    assert cli.load_token(config) == "legacy-file-token"
    assert (cli.LEGACY_CONFIG_DIR / "config.json").read_bytes() == legacy_before
    assert not (cli.CONFIG_DIR / "config.json").exists()


def test_legacy_token_alone_selects_legacy_directory(config_cli):
    cli = config_cli
    cli.LEGACY_CONFIG_DIR.mkdir(parents=True)
    token_path = cli.LEGACY_CONFIG_DIR / "token"
    token_path.write_text("legacy-file-token\n", encoding="utf-8")
    config = cli.load_config()
    assert config == {"base_url": cli.DEFAULT_BASE_URL, "timeout": 300,
                      "token_file": str(token_path)}
    assert cli.load_token(config) == "legacy-file-token"
    assert not cli.CONFIG_DIR.exists()
    assert not (cli.LEGACY_CONFIG_DIR / "config.json").exists()


@pytest.mark.parametrize("new_config,legacy_config,new_token,legacy_token,selected", [
    (False, False, False, False, "CONFIG_DIR"),
    (False, False, False, True, "LEGACY_CONFIG_DIR"),
    (False, False, True, False, "CONFIG_DIR"),
    (False, False, True, True, "CONFIG_DIR"),
    (False, True, False, False, "LEGACY_CONFIG_DIR"),
    (False, True, False, True, "LEGACY_CONFIG_DIR"),
    (False, True, True, False, "LEGACY_CONFIG_DIR"),
    (False, True, True, True, "LEGACY_CONFIG_DIR"),
    (True, False, False, False, "CONFIG_DIR"),
    (True, False, False, True, "CONFIG_DIR"),
    (True, False, True, False, "CONFIG_DIR"),
    (True, False, True, True, "CONFIG_DIR"),
    (True, True, False, False, "CONFIG_DIR"),
    (True, True, False, True, "CONFIG_DIR"),
    (True, True, True, False, "CONFIG_DIR"),
    (True, True, True, True, "CONFIG_DIR"),
])
def test_config_and_token_directory_selection_matrix(config_cli, new_config, legacy_config,
                                                      new_token, legacy_token, selected):
    cli = config_cli
    for directory, has_config, has_token in (
        (cli.CONFIG_DIR, new_config, new_token),
        (cli.LEGACY_CONFIG_DIR, legacy_config, legacy_token),
    ):
        if has_config:
            write_config(directory, {"base_url": f"https://{directory.name}.example.test"})
        if has_token:
            directory.mkdir(parents=True, exist_ok=True)
            token_path = directory / "token"
            token_path.write_text(f"test-{directory.name}-token\n", encoding="utf-8")
            assert token_path.is_file()
    config = cli.load_config()
    directory = getattr(cli, selected)
    selected_config = new_config if selected == "CONFIG_DIR" else legacy_config
    selected_token = new_token if selected == "CONFIG_DIR" else legacy_token
    assert config == {
        "base_url": f"https://{directory.name}.example.test" if selected_config else cli.DEFAULT_BASE_URL,
        "timeout": 300,
        "token_file": str(directory / "token"),
    }
    if selected_token:
        assert cli.load_token(config) == f"test-{directory.name}-token"
    else:
        with pytest.raises(cli.CliError, match="缺少 Token"):
            cli.load_token(config)
    assert (cli.CONFIG_DIR / "config.json").exists() == new_config
    assert (cli.LEGACY_CONFIG_DIR / "config.json").exists() == legacy_config


def test_empty_new_token_file_does_not_fall_back_to_legacy_token(config_cli):
    cli = config_cli
    for directory, token in ((cli.CONFIG_DIR, " \t\n"),
                             (cli.LEGACY_CONFIG_DIR, "unused-legacy-token")):
        directory.mkdir(parents=True)
        (directory / "token").write_text(token, encoding="utf-8")
    assert (cli.CONFIG_DIR / "token").is_file()
    config = cli.load_config()
    assert config["token_file"] == str(cli.CONFIG_DIR / "token")
    with pytest.raises(cli.CliError, match="Token 文件为空"):
        cli.load_token(config)


def test_invalid_new_config_reports_error_without_legacy_fallback(config_cli):
    cli = config_cli
    write_config(cli.LEGACY_CONFIG_DIR, {"timeout": 23})
    cli.CONFIG_DIR.mkdir(parents=True)
    (cli.CONFIG_DIR / "config.json").write_text("{", encoding="utf-8")
    with pytest.raises(cli.CliError, match="无法读取配置") as error:
        cli.load_config()
    assert str(cli.CONFIG_DIR / "config.json") in str(error.value)


@pytest.mark.parametrize("directory", ["CONFIG_DIR", "LEGACY_CONFIG_DIR"])
@pytest.mark.parametrize("relative", [False, True])
def test_explicit_token_file_keeps_existing_path_semantics(config_cli, tmp_path, monkeypatch,
                                                           directory, relative):
    cli = config_cli
    monkeypatch.chdir(tmp_path)
    token_path = Path("custom-token") if relative else tmp_path / "custom-token"
    token_path.write_text("explicit-file-token\n", encoding="utf-8")
    write_config(getattr(cli, directory), {"token_file": str(token_path)})
    config = cli.load_config()
    assert config["token_file"] == str(token_path)
    assert cli.load_token(config) == "explicit-file-token"


@pytest.mark.parametrize("new,legacy,expected", [
    (" new-env-token\n", "legacy-env-token", "new-env-token"),
    ("new-env-token", None, "new-env-token"),
    (None, " legacy-env-token\n", "legacy-env-token"),
    ("", "legacy-env-token", "legacy-env-token"),
    (" \t\n", "legacy-env-token", "legacy-env-token"),
    (None, None, "file-token"),
    ("", "", "file-token"),
    (" \n", " \t", "file-token"),
])
def test_token_precedence_uses_nonempty_values(config_cli, tmp_path, monkeypatch, new, legacy, expected):
    cli = config_cli
    for name, value in (("PAGE_DROP_TOKEN", new), ("EDGE_PUBLISH_TOKEN", legacy)):
        if value is not None:
            monkeypatch.setenv(name, value)
    token_path = tmp_path / "token"
    if expected == "file-token":
        token_path.write_text("file-token\n", encoding="utf-8")
    assert cli.load_token({"token_file": str(token_path)}) == expected


def test_missing_token_names_both_supported_environment_variables(config_cli):
    cli = config_cli
    config = cli.load_config()
    with pytest.raises(cli.CliError) as error:
        cli.load_token(config)
    message = str(error.value)
    assert "缺少 Token" in message
    assert "PAGE_DROP_TOKEN" in message
    assert "EDGE_PUBLISH_TOKEN" in message
    assert str(cli.CONFIG_DIR / "token") in message


def test_empty_token_file_still_fails_without_printing_content(config_cli, tmp_path):
    cli = config_cli
    token_path = tmp_path / "empty-token"
    token_path.write_text(" \t\n", encoding="utf-8")
    with pytest.raises(cli.CliError, match="Token 文件为空"):
        cli.load_token({"token_file": str(token_path)})


@pytest.mark.parametrize("base_url", ["", None, " \t", "not-a-service-url",
                                      "https://pages.example.com:99999", "https://pages.example.com:abc"])
def test_publish_requires_valid_base_url_before_creating_state(config_cli, tmp_path, monkeypatch,
                                                              capsys, base_url):
    cli = config_cli
    write_config(cli.CONFIG_DIR, {"base_url": base_url})
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>local</title>", encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("无有效 base_url 时不得打包或请求网络")

    monkeypatch.setattr(cli, "build_archive", forbidden)
    monkeypatch.setattr(cli, "request", forbidden)
    assert cli.main([str(root), "--ttl", "1d"]) == 2
    assert "base_url" in capsys.readouterr().err
    assert not cli.STATE_DIR.exists()


def test_request_without_base_url_fails_before_token_or_connection(config_cli, monkeypatch):
    cli = config_cli

    def forbidden(*args, **kwargs):
        pytest.fail("无 base_url 时不得读取 Token 或创建连接")

    monkeypatch.setattr(cli, "load_token", forbidden)
    monkeypatch.setattr(cli.http.client, "HTTPConnection", forbidden)
    monkeypatch.setattr(cli.http.client, "HTTPSConnection", forbidden)
    with pytest.raises(cli.CliError, match="未配置有效 base_url"):
        cli.request(cli.load_config(), "GET", "/api/v1/publishes")


@pytest.mark.parametrize("base_url", ["https://pages.example.com/prefix", "http://localhost:18090",
                                      "http://127.0.0.1:18090"])
def test_explicit_valid_base_url_keeps_existing_semantics(config_cli, base_url):
    assert config_cli.base_parts(base_url).geturl() == base_url


def test_discard_pending_requires_no_base_url_or_network(config_cli, monkeypatch, capsys):
    cli = config_cli

    def forbidden(*args, **kwargs):
        pytest.fail("discard-pending 不得读取 Token 或请求网络")

    monkeypatch.setattr(cli, "load_token", forbidden)
    monkeypatch.setattr(cli, "request", forbidden)
    assert cli.main(["discard-pending"]) == 0
    assert "没有待确认" in capsys.readouterr().out


def test_dry_run_never_loads_token_or_requests_network(config_cli, tmp_path, monkeypatch, capsys):
    cli = config_cli
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>dry run</title>", encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run 不得读取 Token 或请求网络")

    monkeypatch.setattr(cli, "load_token", forbidden)
    monkeypatch.setattr(cli, "request", forbidden)
    assert cli.main([str(root), "--ttl", "1d", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not cli.state_paths()["pending"].exists()
    assert not cli.state_paths()["archive"].exists()
    assert not cli.state_paths()["history"].exists()
