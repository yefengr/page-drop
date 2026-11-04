"""隔离验证技能内唯一 CLI 实现与旧兼容入口。

只使用临时目录、空 HOME 和受限 PATH：不读取真实配置或 Token，
不依赖仓库内其他文件，不访问网络，也不执行真实发布、查询或删除。
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_publish import CLI_PATH, IMPLEMENTATION_PATH


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "page-drop"
SKILL_SCRIPT = SKILL_DIR / "scripts" / "publish"
DRY_RUN_FIELDS = (
    "dry_run", "file_count", "dir_count", "content_bytes", "archive_bytes",
)
ARCHIVE_HASH_RE = re.compile(r"[0-9a-f]{64}")
TIMEOUT = 30


@pytest.fixture
def isolated_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # 直接执行入口的 shebang 是 /usr/bin/env python3，PATH 只保留这一份解释器。
    (bin_dir / "python3").symlink_to(sys.executable)
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    env = {
        "HOME": str(home),
        "PATH": str(bin_dir),
        "TMPDIR": str(temp_dir),
        "LANG": "en_US.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return env, home, bin_dir, workdir


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site with 空格"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>隔离测试</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return root


def invoke(entry, args, env, workdir, direct=False):
    command = [str(entry), *args] if direct else [sys.executable, str(entry), *args]
    return subprocess.run(
        command, cwd=str(workdir), env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )


def invoke_both(args, env, workdir, direct=False):
    return [invoke(entry, args, env, workdir, direct=direct) for entry in (CLI_PATH, SKILL_SCRIPT)]


def assert_matching(results, expected_code=0):
    legacy, skill = results
    assert legacy.returncode == expected_code, legacy.stderr
    assert skill.returncode == expected_code, skill.stderr
    assert skill.stdout == legacy.stdout
    assert skill.stderr == legacy.stderr


def assert_dry_run_matches(results):
    """比较两个入口的 dry-run 输出。

    归档包含以当前时间写入的目录 mtime，跨秒运行会改变 archive_sha256；
    该字段只校验格式，其余业务字段和输出结构必须一致。
    """
    legacy, skill = results
    assert legacy.returncode == 0, legacy.stderr
    assert skill.returncode == 0, skill.stderr
    assert skill.stderr == legacy.stderr
    payloads = [json.loads(result.stdout) for result in results]
    for payload in payloads:
        digest = payload.pop("archive_sha256")
        assert isinstance(digest, str) and ARCHIVE_HASH_RE.fullmatch(digest), digest
    assert payloads[1] == payloads[0]
    normalized = [
        re.sub(r'"archive_sha256": "[0-9a-f]{64}"', '"archive_sha256": "<hash>"', result.stdout)
        for result in results
    ]
    assert normalized[1] == normalized[0]
    return payloads


def test_entrypoints_are_executable():
    assert IMPLEMENTATION_PATH == SKILL_SCRIPT
    for path in (CLI_PATH, SKILL_SCRIPT):
        assert path.is_file(), path
        assert path.stat().st_mode & 0o111, path
        assert os.access(path, os.X_OK), path


def test_isolated_environment_has_no_service_token_or_global_publish(isolated_env):
    env, home, bin_dir, _ = isolated_env
    assert "PAGE_DROP_TOKEN" not in env and "EDGE_PUBLISH_TOKEN" not in env
    assert not any(key.startswith(("PAGE_DROP_", "EDGE_PUBLISH_")) for key in env)
    assert shutil.which("publish", path=env["PATH"]) is None
    assert os.listdir(bin_dir) == ["python3"]
    assert os.listdir(home) == []


def test_relative_legacy_entrypoint_from_repository(isolated_env, site):
    env, _, _, _ = isolated_env
    result = invoke("cli/publish", [str(site), "--ttl", "1d", "--dry-run", "--json"], env, ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"] is True


def test_legacy_entrypoint_does_not_fallback_to_path_publish(isolated_env, site):
    env, _, bin_dir, workdir = isolated_env
    fake = bin_dir / "publish"
    fake.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake.chmod(0o755)
    assert shutil.which("publish", path=env["PATH"]) == str(fake)

    result = invoke(
        CLI_PATH, [str(site), "--ttl", "1d", "--dry-run", "--json"], env, workdir, direct=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"] is True


def test_help_matches_between_entrypoints(isolated_env):
    env, _, _, workdir = isolated_env
    results = invoke_both(["--help"], env, workdir)
    assert_matching(results)
    assert "PageDrop 发布工具" in results[0].stdout
    assert "publish <dir>" in results[0].stdout
    assert "publish publish" not in results[0].stdout


def test_json_dry_run_matches_between_entrypoints(isolated_env, site):
    env, home, _, workdir = isolated_env
    results = invoke_both([str(site), "--ttl", "3d", "--dry-run", "--json"], env, workdir)
    payloads = assert_dry_run_matches(results)
    for field in DRY_RUN_FIELDS:
        assert payloads[1][field] == payloads[0][field], field
    assert payloads[0]["dry_run"] is True
    assert payloads[0]["file_count"] == 2
    assert payloads[0]["dir_count"] == 1
    assert payloads[0]["content_bytes"] > 0
    assert payloads[0]["archive_bytes"] >= payloads[0]["content_bytes"]

    # 空 HOME 没有配置、Token 或服务地址；dry-run 也不得产生待确认记录。
    assert not (home / ".config").exists()
    assert not list(home.rglob("pending.json"))
    assert not list(home.rglob("pending.tar"))
    assert not list(home.rglob("history.jsonl"))
    assert not list(home.rglob("token"))


def test_arguments_with_spaces_and_unicode_pass_through(isolated_env, site):
    env, home, _, workdir = isolated_env
    args = [
        str(site), "--ttl", "1d", "--title", "评审 稿 with space", "--label", "本机 标签",
        "--dry-run", "--json",
    ]
    results = invoke_both(args, env, workdir)
    payloads = assert_dry_run_matches(results)
    assert all(payload["dry_run"] is True for payload in payloads)
    assert not list(home.rglob("pending.json"))


@pytest.mark.parametrize("case", [
    "missing-directory", "missing-ttl", "missing-source", "invalid-ttl", "invalid-label",
])
def test_error_output_matches_between_entrypoints(isolated_env, site, tmp_path, case):
    env, home, _, workdir = isolated_env
    args = {
        "missing-directory": [],
        "missing-ttl": [str(site)],
        "missing-source": [str(tmp_path / "absent"), "--ttl", "1d", "--dry-run"],
        "invalid-ttl": [str(site), "--ttl", "1.5h", "--dry-run"],
        # 含空格与换行的 label 会因控制字符被拒绝，可证明参数值原样到达 parser。
        "invalid-label": [str(site), "--ttl", "1d", "--label", "坏\n标签", "--dry-run"],
    }[case]
    results = invoke_both(args, env, workdir)
    assert_matching(results, expected_code=2)
    assert results[0].stdout == ""
    assert results[0].stderr.strip()
    assert "Traceback" not in results[0].stderr
    assert not list(home.rglob("pending.json"))


def test_direct_execution_uses_python_from_restricted_path(isolated_env, site):
    env, _, _, workdir = isolated_env
    help_results = invoke_both(["--help"], env, workdir, direct=True)
    assert_matching(help_results)
    assert "PageDrop 发布工具" in help_results[0].stdout

    dry_results = invoke_both(
        [str(site), "--ttl", "1d", "--dry-run", "--json"], env, workdir, direct=True,
    )
    payloads = assert_dry_run_matches(dry_results)
    assert all(payload["dry_run"] is True for payload in payloads)


def test_temporary_symlink_to_legacy_entrypoint(isolated_env, site):
    env, _, bin_dir, workdir = isolated_env
    link = bin_dir / "legacy publish"
    os.symlink(CLI_PATH, link)
    result = invoke(
        link, [str(site), "--ttl", "1d", "--dry-run", "--json"], env, workdir, direct=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"] is True
    assert shutil.which("publish", path=env["PATH"]) is None


def test_copied_skill_directory_runs_outside_repository(isolated_env, tmp_path, site):
    env, home, _, workdir = isolated_env
    installed = tmp_path / "installed skill" / "page drop"
    shutil.copytree(
        SKILL_DIR, installed,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = installed / "scripts" / "publish"
    assert not script.resolve().is_relative_to(ROOT.resolve())
    for relative in ("SKILL.md", "references/usage.md", "scripts/publish", "LICENSE"):
        assert (installed / relative).is_file(), relative
    before = {path.relative_to(installed) for path in installed.rglob("*")}

    for direct in (False, True):
        help_result = invoke(script, ["--help"], env, workdir, direct=direct)
        assert help_result.returncode == 0, help_result.stderr
        assert "PageDrop 发布工具" in help_result.stdout

        dry_run = invoke(
            script, [str(site), "--ttl", "2h", "--dry-run", "--json"], env, workdir, direct=direct,
        )
        assert dry_run.returncode == 0, dry_run.stderr
        assert json.loads(dry_run.stdout)["dry_run"] is True

    after = {path.relative_to(installed) for path in installed.rglob("*")}
    assert after == before, "dry-run 不得向技能目录写入配置、Token、归档或状态"
    assert not (home / ".config").exists()
    assert not list(home.rglob("pending.json"))
    assert not list(home.rglob("history.jsonl"))
