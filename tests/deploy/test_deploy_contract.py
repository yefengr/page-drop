"""部署契约使用本地假 SSH 和合成配置验证，不连接服务器。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "deploy.sh"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"


@pytest.fixture
def fake_ssh(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    ssh = binary / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n"
        "case \"$*\" in\n"
        "  *\"test -s\"*) [ \"${FAIL_PREFLIGHT:-0}\" = 0 ] || exit 1 ;;\n"
        "  *\"tar -xzf\"*) cat > \"$SSH_ARCHIVE\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{binary}:{env['PATH']}", SSH_LOG=str(tmp_path / "ssh.log"),
               SSH_ARCHIVE=str(tmp_path / "archive.tar.gz"))
    env["HOST"] = "deploy.example.com"
    env.pop("REMOTE", None)
    return env, tmp_path / "ssh.log"


def test_deploy_uses_named_project_and_existing_token(fake_ssh):
    env, log = fake_ssh
    result = subprocess.run(["bash", str(DEPLOY)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 6
    assert "test -s '/srv/page-drop/.env'" in calls[0]
    assert "mkdir -p '/srv/page-drop/src'" in calls[1]
    assert "tar -xzf - -C '/srv/page-drop/src'" in calls[2]
    assert "install -d -m 0700 -o 10001 -g 10001 '/srv/page-drop/data'" in calls[3]
    assert "docker compose --env-file '/srv/page-drop/.env' -p page-drop" in calls[4]
    assert "PAGE_DROP_ENV_FILE='/srv/page-drop/.env'" in calls[4]
    assert "PAGE_DROP_DATA_DIR='/srv/page-drop/data'" in calls[4]
    assert all(call.startswith("deploy.example.com ") for call in calls)
    assert "--wait --wait-timeout 120" in calls[4]
    assert "docker ps --filter name=page-drop" in calls[5]
    assert all("openssl" not in call and "EDGE_PUBLISH_TOKEN=" not in call for call in calls)
    with tarfile.open(env["SSH_ARCHIVE"], "r:gz") as archive:
        names = archive.getnames()
    assert ".dockerignore" in names
    assert "LICENSE" in names
    assert all("__pycache__" not in Path(name).parts and not name.endswith(".pyc") for name in names)
    assert all(not any(part.startswith("._") for part in Path(name).parts) for name in names)


@pytest.mark.parametrize("host", [None, ""])
def test_missing_host_stops_before_ssh(fake_ssh, host):
    env, log = fake_ssh
    if host is None:
        env.pop("HOST", None)
    else:
        env["HOST"] = host
    result = subprocess.run(["bash", str(DEPLOY)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "Set HOST" in result.stderr
    assert not log.exists()


def test_missing_config_stops_before_sync(fake_ssh):
    env, log = fake_ssh
    env["FAIL_PREFLIGHT"] = "1"
    result = subprocess.run(["bash", str(DEPLOY)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("remote", ["/", "/srv/../etc", "/srv//page-drop", "/srv/evil';id;'"])
def test_unsafe_remote_path_is_rejected_before_ssh(fake_ssh, remote):
    env, log = fake_ssh
    env["REMOTE"] = remote
    result = subprocess.run(["bash", str(DEPLOY)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert not log.exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 docker compose 验证配置解析")
@pytest.mark.parametrize("legacy", [False, True], ids=["page-drop-variables", "legacy-fallback"])
def test_compose_variable_resolution(tmp_path, legacy):
    env_file = tmp_path / "test.env"
    env_file.write_text("PAGE_DROP_TOKEN=synthetic-test-token\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    for prefix in ("PAGE_DROP", "EDGE_PUBLISH"):
        for suffix in ("ENV_FILE", "DATA_DIR", "PUBLIC_URL"):
            env.pop(f"{prefix}_{suffix}", None)
    prefix = "EDGE_PUBLISH" if legacy else "PAGE_DROP"
    env[f"{prefix}_ENV_FILE"] = str(env_file)
    env[f"{prefix}_DATA_DIR"] = str(data_dir)
    env[f"{prefix}_PUBLIC_URL"] = "https://pages.example.test"
    result = subprocess.run(
        ["docker", "compose", "-p", "page-drop", "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    service = config["services"]["page-drop"]
    assert service["container_name"] == "page-drop"
    assert service["image"] == "page-drop:local"
    assert service["environment"]["PAGE_DROP_DATA_DIR"] == "/data"
    assert service["environment"]["PAGE_DROP_PUBLIC_URL"] == "https://pages.example.test"
    assert any(v["source"] == str(data_dir) and v["target"] == "/data" for v in service["volumes"])


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 docker compose 验证配置解析")
def test_compose_rejects_missing_data_dir(tmp_path):
    env_file = tmp_path / "test.env"
    env_file.write_text("PAGE_DROP_TOKEN=synthetic-test-token\n", encoding="utf-8")
    env = os.environ.copy()
    for prefix in ("PAGE_DROP", "EDGE_PUBLISH"):
        for suffix in ("ENV_FILE", "DATA_DIR", "PUBLIC_URL"):
            env.pop(f"{prefix}_{suffix}", None)
    env["PAGE_DROP_ENV_FILE"] = str(env_file)
    env["PAGE_DROP_PUBLIC_URL"] = "https://pages.example.com"
    result = subprocess.run(
        ["docker", "compose", "-p", "page-drop", "-f", str(COMPOSE), "config", "-q"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "PAGE_DROP_DATA_DIR" in result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 docker compose 验证配置解析")
@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
def test_compose_requires_public_url(tmp_path, empty):
    env_file = tmp_path / "test.env"
    env_file.write_text("PAGE_DROP_TOKEN=synthetic-test-token\n", encoding="utf-8")
    env = os.environ.copy()
    for prefix in ("PAGE_DROP", "EDGE_PUBLISH"):
        for suffix in ("ENV_FILE", "DATA_DIR", "PUBLIC_URL"):
            env.pop(f"{prefix}_{suffix}", None)
        if empty:
            env[f"{prefix}_PUBLIC_URL"] = ""
    env["PAGE_DROP_ENV_FILE"] = str(env_file)
    env["PAGE_DROP_DATA_DIR"] = str(tmp_path / "data")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-p", "page-drop",
         "-f", str(COMPOSE), "config", "-q"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "PAGE_DROP_PUBLIC_URL" in result.stderr
    assert "EDGE_PUBLISH_PUBLIC_URL" in result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 docker compose 验证配置解析")
@pytest.mark.parametrize("legacy", [False, True], ids=["page-drop-variables", "legacy-fallback"])
@pytest.mark.parametrize("override", [False, True], ids=["env-file", "shell-overrides"])
def test_compose_loads_interpolation_from_explicit_env_file(tmp_path, legacy, override):
    env_file = tmp_path / "remote.env"
    data_dir = tmp_path / "env-data"
    prefix = "EDGE_PUBLISH" if legacy else "PAGE_DROP"
    env_file.write_text(
        "PAGE_DROP_TOKEN=synthetic-env-file-token\n"
        f"{prefix}_ENV_FILE={env_file}\n"
        f"{prefix}_DATA_DIR={data_dir}\n"
        f"{prefix}_PUBLIC_URL=https://pages.example.com\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name_prefix in ("PAGE_DROP", "EDGE_PUBLISH"):
        for suffix in ("ENV_FILE", "DATA_DIR", "PUBLIC_URL", "TOKEN"):
            env.pop(f"{name_prefix}_{suffix}", None)
    if override:
        override_env = tmp_path / "override.env"
        override_env.write_text("PAGE_DROP_TOKEN=synthetic-override-token\n", encoding="utf-8")
        data_dir = tmp_path / "override-data"
        env["PAGE_DROP_ENV_FILE"] = str(override_env)
        env["PAGE_DROP_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-p", "page-drop",
         "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    service = json.loads(result.stdout)["services"]["page-drop"]
    assert service["environment"]["PAGE_DROP_PUBLIC_URL"] == "https://pages.example.com"
    expected_token = "synthetic-override-token" if override else "synthetic-env-file-token"
    assert service["environment"]["PAGE_DROP_TOKEN"] == expected_token
    assert any(v["source"] == str(data_dir) and v["target"] == "/data" for v in service["volumes"])
