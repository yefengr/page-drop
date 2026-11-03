import json
import os
import stat
from pathlib import Path

import pytest

from test_publish import cli, site, stub
from test_contract_fixes import SUCCESS, response


def history(cli):
    return [json.loads(line) for line in cli.state_paths()["history"].read_text().splitlines()]


def test_durable_order_before_network_and_after_success(cli, site, monkeypatch):
    events = []
    real_fsync, real_replace, real_unlink = os.fsync, os.replace, Path.unlink

    def sync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            events.append("dir_sync")
        else:
            for path in cli.STATE_DIR.iterdir():
                if path.stat().st_ino == info.st_ino:
                    name = path.name
                    kind = ("archive" if name.startswith("pack-") else
                            "pending" if name.startswith(".tmp-") else
                            "history" if name == "history.jsonl" else name)
                    events.append(kind + "_sync")
                    break
        real_fsync(fd)

    def replace(source, target):
        real_replace(source, target)
        events.append(Path(target).name + "_rename")

    def unlink(path, *args, **kwargs):
        real_unlink(path, *args, **kwargs)
        if path.name in ("pending.tar", "pending.json"):
            events.append(path.name + "_unlink")

    def upload(config, method, *args, **kwargs):
        events.append("network")
        assert cli.load_pending() is not None
        assert cli.state_paths()["archive"].stat().st_mode & 0o777 == 0o600
        assert cli.state_paths()["pending"].stat().st_mode & 0o777 == 0o600
        return response(SUCCESS)

    monkeypatch.setattr(cli.os, "fsync", sync)
    monkeypatch.setattr(cli.os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(cli, "request", upload)
    assert cli.main([str(site), "--ttl", "1d"]) == 0
    assert events == ["archive_sync", "pending.tar_rename", "dir_sync", "pending_sync",
                      "pending.json_rename", "dir_sync", "network", "history_sync", "dir_sync",
                      "pending.tar_unlink", "dir_sync", "pending.json_unlink", "dir_sync"]
    assert history(cli)[0]["key"]
    assert cli.STATE_DIR.stat().st_mode & 0o777 == 0o700
    assert cli.state_paths()["history"].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stage", ["history_write", "history_sync", "history_dir", "archive_unlink",
                                    "archive_dir", "pending_unlink", "pending_dir"])
def test_cleanup_io_failure_is_recoverable(cli, site, monkeypatch, stage, capsys):
    real_append, real_sync, real_dir, real_unlink = cli.append_history, os.fsync, cli.fsync_directory, Path.unlink
    fired = False

    def trigger():
        nonlocal fired
        if not fired:
            fired = True
            raise OSError("injected local I/O failure")

    def append(record):
        if stage == "history_write":
            trigger()
        return real_append(record)

    def sync(fd):
        path = cli.state_paths()["history"]
        if stage == "history_sync" and path.exists() and os.fstat(fd).st_ino == path.stat().st_ino:
            trigger()
        return real_sync(fd)

    def directory(path):
        paths = cli.state_paths()
        if paths["history"].exists():
            if stage == "history_dir" and paths["archive"].exists():
                trigger()
            if stage == "archive_dir" and not paths["archive"].exists() and paths["pending"].exists():
                trigger()
            if stage == "pending_dir" and not paths["pending"].exists():
                trigger()
        return real_dir(path)

    def unlink(path, *args, **kwargs):
        if stage == "archive_unlink" and path == cli.state_paths()["archive"]:
            trigger()
        if stage == "pending_unlink" and path == cli.state_paths()["pending"]:
            trigger()
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(cli, "append_history", append)
    monkeypatch.setattr(cli.os, "fsync", sync)
    monkeypatch.setattr(cli, "fsync_directory", directory)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert cli.main([str(site), "--ttl", "1d", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "I/O" in captured.err
    assert fired
    if stage == "pending_dir":
        assert not cli.load_pending()
        assert len(history(cli)) == 1
        return
    assert cli.load_pending()
    if stage in ("history_write", "history_sync", "history_dir", "archive_unlink"):
        assert cli.state_paths()["archive"].exists()
    assert cli.main(["retry", "--json"]) == 0
    assert len(cli.stub_state.posts) == 1
    assert not cli.load_pending()
    assert len(history(cli)) == 1
    assert history(cli)[0]["key"] == cli.stub_state.posts[0]["key"]


def test_confirmed_key_is_never_reposted_after_get_404(cli, site, monkeypatch, capsys):
    real_clear = cli.clear_pending
    monkeypatch.setattr(cli, "clear_pending", lambda: (_ for _ in ()).throw(OSError("stop after history")))
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert len(history(cli)) == 1
    cli.stub_state.uploads.clear()
    monkeypatch.setattr(cli, "clear_pending", real_clear)
    assert cli.main(["retry"]) == 0
    assert len(cli.stub_state.posts) == 1
    assert len(history(cli)) == 1
    assert not cli.load_pending()
    assert "不代表远端仍可访问" in capsys.readouterr().err


@pytest.mark.parametrize("tail", [b'{"id":', json.dumps({"older": True}).encode()])
def test_history_repairs_only_incomplete_tail_and_preserves_valid_unterminated_record(cli, site, tail):
    cli.STATE_DIR.mkdir()
    first = json.dumps({"older": "retained"}).encode() + b"\n"
    cli.state_paths()["history"].write_bytes(first + tail)
    assert cli.main([str(site), "--ttl", "1d"]) == 0
    rows = history(cli)
    assert rows[0] == {"older": "retained"}
    if tail.endswith(b"}"):
        assert rows[1] == {"older": True}
        assert len(rows) == 3
    else:
        assert len(rows) == 2


@pytest.mark.parametrize("bad", [b'broken\n', b'{"older":1}\nbroken\n{"later":2}\n', b'[]'])
def test_history_corrupt_complete_or_middle_line_is_not_deleted(cli, site, bad, capsys):
    cli.STATE_DIR.mkdir()
    cli.state_paths()["history"].write_bytes(bad)
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert "保留文件" in capsys.readouterr().err
    assert cli.state_paths()["history"].read_bytes() == bad
    assert cli.load_pending()
    assert cli.state_paths()["archive"].exists()


def test_history_dedupes_by_publish_id(cli):
    cli.ensure_private_dir(cli.STATE_DIR)
    first = {**SUCCESS, "key": "first"}
    cli.append_history(first)
    cli.append_history({**first, "key": "another"})
    cli.append_history(first)
    assert history(cli) == [first]


@pytest.mark.parametrize("stage", ["archive_sync", "archive_dir", "pending_sync", "pending_dir"])
def test_pre_network_fsync_failure_never_sends(cli, site, monkeypatch, stage, capsys):
    real_sync, real_dir = os.fsync, cli.fsync_directory

    def sync(fd):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            names = [p.name for p in cli.STATE_DIR.iterdir() if p.stat().st_ino == info.st_ino]
            if stage == "archive_sync" and any(n.startswith("pack-") for n in names):
                raise OSError("archive sync")
            if stage == "pending_sync" and any(n.startswith(".tmp-") for n in names):
                raise OSError("pending sync")
        real_sync(fd)

    def directory(path):
        pending = cli.state_paths()["pending"].exists()
        if stage == "archive_dir" and not pending or stage == "pending_dir" and pending:
            raise OSError("directory sync")
        real_dir(path)

    monkeypatch.setattr(cli.os, "fsync", sync)
    monkeypatch.setattr(cli, "fsync_directory", directory)
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert cli.stub_state.posts == []
    assert "I/O" in capsys.readouterr().err


def test_keyboard_interrupt_after_pending_keeps_recovery_inputs(cli, site, monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "request", interrupted)
    assert cli.main([str(site), "--ttl", "1d"]) == 130
    record = cli.load_pending()
    assert record
    assert cli.sha256_file(record["archive"]) == record["archive_sha256"]
    assert not cli.state_paths()["history"].exists()
    assert "中断" in capsys.readouterr().err


def test_first_history_partial_write_is_explicitly_repaired_on_retry(cli, site, monkeypatch, capsys):
    original = cli.append_history

    def partial(record):
        with open(cli.state_paths()["history"], "wb") as handle:
            handle.write(b'{"key":')
            handle.flush()
            os.fsync(handle.fileno())
        raise OSError("interrupted first history append")

    monkeypatch.setattr(cli, "append_history", partial)
    assert cli.main([str(site), "--ttl", "1d"]) == 2
    assert cli.load_pending()
    assert cli.state_paths()["archive"].exists()
    monkeypatch.setattr(cli, "append_history", original)
    assert cli.main(["retry"]) == 0
    assert len(history(cli)) == 1
    assert len(cli.stub_state.posts) == 1
    assert "history 尾行不完整" in capsys.readouterr().err


@pytest.mark.parametrize("stage", ["history", "archive", "pending"])
def test_keyboard_interrupt_during_cleanup_keeps_durable_recovery(cli, site, monkeypatch, stage):
    original_append, original_unlink = cli.append_history, Path.unlink
    interrupted = False

    def stop_once():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    def append(record):
        original_append(record)
        if stage == "history":
            stop_once()

    def unlink(path, *args, **kwargs):
        if stage == "pending" and path == cli.state_paths()["pending"]:
            stop_once()
        original_unlink(path, *args, **kwargs)
        if stage == "archive" and path == cli.state_paths()["archive"]:
            stop_once()

    monkeypatch.setattr(cli, "append_history", append)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert cli.main([str(site), "--ttl", "1d"]) == 130
    assert cli.load_pending()
    assert len(history(cli)) == 1
    assert cli.main(["retry"]) == 0
    assert len(cli.stub_state.posts) == 1
    assert len(history(cli)) == 1
    assert not cli.load_pending()
