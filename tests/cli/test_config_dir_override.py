"""配置目录覆盖只影响客户端配置，不访问真实服务或用户恢复状态。"""

import json
from pathlib import Path

import pytest

from test_page_drop_config import config_cli, write_config
from test_skill_entrypoints import (
    assert_dry_run_matches,
    assert_matching,
    invoke_both,
    isolated_env,
    site,
)


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_override_preserves_legacy_selection(config_cli, monkeypatch, value):
    cli = config_cli
    if value is not None:
        monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", value)
    write_config(cli.LEGACY_CONFIG_DIR, {"base_url": "https://legacy.example.test"})
    (cli.LEGACY_CONFIG_DIR / "token").write_text("legacy-fixture-token")
    config = cli.load_config()
    assert config["base_url"] == "https://legacy.example.test"
    assert config["token_file"] == str(cli.LEGACY_CONFIG_DIR / "token")
    assert cli.load_token(config) == "legacy-fixture-token"


def test_absolute_override_selects_its_config_and_token_only(config_cli, tmp_path, monkeypatch):
    cli = config_cli
    for directory in (cli.CONFIG_DIR, cli.LEGACY_CONFIG_DIR):
        write_config(directory, {"base_url": "https://unused.example.test", "timeout": 1})
        (directory / "token").write_text("unused-fixture-token")
    chosen = tmp_path / "private config with 空格"
    write_config(chosen, {"base_url": "https://chosen.example.test", "timeout": 17})
    (chosen / "token").write_text("chosen-fixture-token\n")
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    assert cli.load_config() == {
        "base_url": "https://chosen.example.test", "timeout": 17,
        "token_file": str(chosen / "token"),
    }
    assert cli.load_token(cli.load_config()) == "chosen-fixture-token"
    assert cli.STATE_DIR == Path.home() / ".local/state/publish"
    assert not cli.STATE_DIR.exists()


@pytest.mark.parametrize("exists", [False, True])
def test_missing_override_config_does_not_fall_back(config_cli, tmp_path, monkeypatch, exists):
    cli = config_cli
    for directory in (cli.CONFIG_DIR, cli.LEGACY_CONFIG_DIR):
        write_config(directory, {"base_url": "https://unused.example.test"})
        (directory / "token").write_text("unused-fixture-token")
    chosen = tmp_path / "chosen"
    if exists:
        chosen.mkdir()
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    config = cli.load_config()
    assert config == {"base_url": "", "timeout": 300, "token_file": str(chosen / "token")}
    with pytest.raises(cli.CliError, match="缺少 Token"):
        cli.load_token(config)
    assert chosen.exists() is exists
    assert not (chosen / "config.json").exists()


@pytest.mark.parametrize("content", ["", "{", "{}"])
def test_override_does_not_merge_or_fall_back(config_cli, tmp_path, monkeypatch, content):
    cli = config_cli
    write_config(cli.CONFIG_DIR, {"base_url": "https://unused.example.test", "timeout": 1})
    (cli.CONFIG_DIR / "token").write_text("unused-fixture-token")
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "config.json").write_text(content)
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    if content != "{}":
        with pytest.raises(cli.CliError, match="无法读取配置"):
            cli.load_config()
    else:
        assert cli.load_config() == {
            "base_url": "", "timeout": 300, "token_file": str(chosen / "token"),
        }


@pytest.mark.parametrize("relative", [False, True])
def test_override_keeps_explicit_token_path_semantics(config_cli, tmp_path, monkeypatch, relative):
    cli = config_cli
    monkeypatch.chdir(tmp_path)
    chosen = tmp_path / "chosen"
    token_path = Path("custom-token") if relative else tmp_path / "custom-token"
    token_path.write_text("explicit-fixture-token")
    write_config(chosen, {"token_file": str(token_path)})
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    config = cli.load_config()
    assert config["token_file"] == str(token_path)
    assert cli.load_token(config) == "explicit-fixture-token"


@pytest.mark.parametrize("new,legacy,expected", [
    ("new-fixture", "legacy-fixture", "new-fixture"),
    ("", "legacy-fixture", "legacy-fixture"),
    (" ", "", "file-fixture"),
])
def test_override_preserves_token_environment_priority(config_cli, tmp_path, monkeypatch,
                                                       new, legacy, expected):
    cli = config_cli
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "token").write_text("file-fixture")
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    monkeypatch.setenv("PAGE_DROP_TOKEN", new)
    monkeypatch.setenv("EDGE_PUBLISH_TOKEN", legacy)
    assert cli.load_token(cli.load_config()) == expected


def test_empty_override_token_does_not_fall_back(config_cli, tmp_path, monkeypatch):
    cli = config_cli
    cli.CONFIG_DIR.mkdir(parents=True)
    (cli.CONFIG_DIR / "token").write_text("unused-fixture-token")
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "token").write_text(" \n")
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    with pytest.raises(cli.CliError, match="Token 文件为空"):
        cli.load_token(cli.load_config())


@pytest.mark.parametrize("value", ["relative", "./config", "~/config", "$HOME/config", " "])
def test_relative_override_fails_before_request_or_state(config_cli, monkeypatch, capsys, value):
    cli = config_cli
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", value)

    def forbidden(*args, **kwargs):
        pytest.fail("非法配置目录不得读凭据或发送请求")

    monkeypatch.setattr(cli, "request", forbidden)
    monkeypatch.setattr(cli, "load_token", forbidden)
    assert cli.main(["list", "--json"]) == 2
    output = capsys.readouterr()
    assert "PAGE_DROP_CONFIG_DIR 必须是绝对路径" in output.err
    assert "Traceback" not in output.err
    assert not output.out
    assert not cli.STATE_DIR.exists()


def test_override_rejects_existing_regular_file(config_cli, tmp_path, monkeypatch):
    chosen = tmp_path / "not-directory"
    chosen.write_text("fixture")
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    with pytest.raises(config_cli.CliError, match="必须指向目录"):
        config_cli.load_config()


def test_dry_run_keeps_home_state_separate_and_does_not_create_config(config_cli, tmp_path,
                                                                      monkeypatch, capsys):
    cli = config_cli
    chosen = tmp_path / "missing-private-config"
    monkeypatch.setenv("PAGE_DROP_CONFIG_DIR", str(chosen))
    site_path = tmp_path / "site"
    site_path.mkdir()
    (site_path / "index.html").write_text("<!doctype html><title>isolated</title>")

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run 不得读取 Token 或访问网络")

    monkeypatch.setattr(cli, "load_token", forbidden)
    monkeypatch.setattr(cli, "request", forbidden)
    assert cli.main([str(site_path), "--ttl", "3d", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not chosen.exists()
    assert (cli.STATE_DIR / "lock").is_file()
    assert not (cli.STATE_DIR / "pending.json").exists()
    assert not (cli.STATE_DIR / "history.jsonl").exists()


def test_both_entrypoints_honor_override(isolated_env, tmp_path, site):
    env, home, _, workdir = isolated_env
    chosen = tmp_path / "chosen config with 空格"
    chosen.mkdir()
    env["PAGE_DROP_CONFIG_DIR"] = str(chosen)
    (chosen / "config.json").write_text("{")
    args = [str(site), "--ttl", "3d", "--dry-run", "--json"]
    results = invoke_both(args, env, workdir)
    assert_matching(results, expected_code=2)
    assert str(chosen / "config.json") in results[0].stderr
    (chosen / "config.json").write_text("{}")
    for direct in (False, True):
        assert_dry_run_matches(invoke_both(args, env, workdir, direct=direct))
    assert not (home / ".config").exists()
    assert not list(home.rglob("pending.json"))
