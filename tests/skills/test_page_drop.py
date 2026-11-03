"""校验技能包结构和文档命令契约；不执行上传，也不替代模型行为验收。"""

import importlib.machinery
import importlib.util
import json
import re
import shlex
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "page-drop"
DOCUMENTS = sorted(SKILL_DIR.rglob("*.md"))


def command_examples():
    for path in DOCUMENTS:
        in_shell = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("```"):
                in_shell = line.strip() in ("```bash", "```sh")
                continue
            if in_shell and line.strip().startswith("publish "):
                yield f"{path.relative_to(ROOT)}:{number}", shlex.split(line.strip())[1:]


EXAMPLES = list(command_examples())


@pytest.fixture(scope="module")
def publish_cli():
    loader = importlib.machinery.SourceFileLoader("skill_publish_cli", str(ROOT / "cli" / "publish"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_skill_frontmatter_is_discoverable():
    lines = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    fields = dict(line.split(": ", 1) for line in lines[1:end] if line.strip())
    name = fields["name"]
    assert name == SKILL_DIR.name
    assert len(name) <= 64
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
    assert 0 < len(fields["description"]) <= 1024
    assert EXAMPLES, "技能包应提供可验证的 CLI 命令示例"


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_local_references_resolve_inside_skill_package(path):
    targets = re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", path.read_text(encoding="utf-8"))
    for target in targets:
        if ":" in target or target.startswith("#"):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        assert resolved.is_relative_to(SKILL_DIR.resolve()), target
        assert resolved.is_file(), target


@pytest.mark.parametrize("argv", [argv for _, argv in EXAMPLES], ids=[label for label, _ in EXAMPLES])
def test_documented_commands_match_cli_parser(publish_cli, argv, capsys):
    # 从实际 parser 获取管理命令，不在测试中维护另一份命令清单。
    management = publish_cli.build_parser(management=True)
    commands = {
        name
        for action in management._actions
        if isinstance(action.choices, dict)
        for name in action.choices
    }
    parser = management if argv[0] in commands else publish_cli.build_parser()
    if "--help" in argv:
        with pytest.raises(SystemExit) as result:
            parser.parse_args(argv)
        assert result.value.code == 0
        assert "publish" in capsys.readouterr().out
        return

    args = parser.parse_args(argv)
    assert callable(args.func)
    if args.func is publish_cli.cmd_publish:
        assert args.directory
        assert publish_cli.parse_ttl(args.ttl) > 0
    # 只解析，不调用 args.func，避免示例中的发布、删除或恢复产生副作用。


def test_documented_dry_run_needs_no_token_or_network(publish_cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(publish_cli, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(publish_cli, "LEGACY_CONFIG_DIR", tmp_path / "legacy-config")
    monkeypatch.setattr(publish_cli, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("PAGE_DROP_TOKEN", raising=False)
    monkeypatch.delenv("EDGE_PUBLISH_TOKEN", raising=False)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<!doctype html><title>isolated test</title>", encoding="utf-8")

    def unexpected_request(*args, **kwargs):
        pytest.fail("仅预检不应发送网络请求")

    monkeypatch.setattr(publish_cli, "request", unexpected_request)
    assert publish_cli.main([str(site), "--ttl", "90m", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert not (tmp_path / "state" / "pending.json").exists()


@pytest.mark.parametrize("environment_override", [False, True], ids=["custom-file", "environment-priority"])
def test_documented_token_sources(publish_cli, tmp_path, monkeypatch, environment_override):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    custom_token = tmp_path / "custom-token"
    custom_token.write_text("isolated-file-token", encoding="utf-8")
    (config_dir / "config.json").write_text(
        json.dumps({"token_file": str(custom_token)}), encoding="utf-8"
    )
    monkeypatch.setattr(publish_cli, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(publish_cli, "LEGACY_CONFIG_DIR", tmp_path / "legacy-config")
    monkeypatch.delenv("PAGE_DROP_TOKEN", raising=False)
    monkeypatch.delenv("EDGE_PUBLISH_TOKEN", raising=False)
    if environment_override:
        monkeypatch.setenv("PAGE_DROP_TOKEN", "isolated-environment-token")

    config = publish_cli.load_config()
    assert not (config_dir / "token").exists()
    assert config["token_file"] == str(custom_token)
    expected = "isolated-environment-token" if environment_override else "isolated-file-token"
    assert publish_cli.load_token(config) == expected
