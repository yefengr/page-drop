import io
from pathlib import Path
import tarfile

import pytest

from app import config as C
from app.archive import InvalidArtifact, extract, validate_path
from app.config import ServiceError
from conftest import make_tar


def unpack(tmp_path, body):
    root = tmp_path / "artifact"
    root.mkdir()
    archive = root / ".archive.tar"
    archive.write_bytes(body)
    return extract(archive, root)


@pytest.mark.parametrize("name", ["../index.html", "a/../index.html", ".git/config", "/index.html",
    "a//b", "a/./b", ".DS_Store", "a/.hidden", "", "a\x00b", "x" * 256, "a/" * 32 + "b"])
def test_path_rules(name):
    with pytest.raises(InvalidArtifact):
        validate_path(name)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                      tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_only_ordinary_files_and_directories(tmp_path, kind):
    info = tarfile.TarInfo("other")
    info.type = kind
    info.linkname = "index.html"
    with pytest.raises(InvalidArtifact):
        unpack(tmp_path, make_tar([("index.html", b"ok"), ("other", info)]))


@pytest.mark.parametrize("entries", [[("other.html", b"x")],
    [("index.html", b"one"), ("index.html", b"two")],
    [("index.html", b"one"), ("folder/a", b"x"), ("folder", b"x")],
    [("index.html", b"one"), ("folder", b"x"), ("folder/a", b"x")]])
def test_missing_index_duplicate_and_conflict(tmp_path, entries):
    with pytest.raises(InvalidArtifact):
        unpack(tmp_path, make_tar(entries))


def test_pax_unicode_and_zero_padding(tmp_path):
    path = "文" * 40 + "/资源.txt"
    manifest = unpack(tmp_path, make_tar([("index.html", b"ok"), (path, b"utf8")]) + b"\x00" * 1024)
    assert manifest.bytes == 6 and manifest.dir_count == 1
    assert {entry["path"] for entry in manifest.files} == {"index.html", path}
    assert not (tmp_path / "artifact/.archive.tar").exists()


def test_implicit_directories_unique(tmp_path):
    manifest = unpack(tmp_path, make_tar([("a/b", b"1"), ("a", None), ("index.html", b"2")]))
    assert manifest.dir_count == 1


def test_file_count_limit(tmp_path):
    entries = [("index.html", b"")] + [(f"file-{n}", b"") for n in range(5000)]
    with pytest.raises(InvalidArtifact, match="Too many files"):
        unpack(tmp_path, make_tar(entries))


def test_declared_content_limit_without_allocating_large_data(tmp_path):
    info = tarfile.TarInfo("index.html")
    info.size = C.MAX_CONTENT + 1
    body = info.tobuf(format=tarfile.USTAR_FORMAT) + b"\x00" * 1024
    with pytest.raises(ServiceError) as failure:
        unpack(tmp_path, body)
    assert failure.value.code == "PAYLOAD_TOO_LARGE"


@pytest.mark.parametrize("tail", [b"garbage", b"\x00" * 511, b"\x00" * 1024 + b"x" * 512])
def test_tar_end_padding(tmp_path, tail):
    with pytest.raises(InvalidArtifact):
        unpack(tmp_path, make_tar() + tail)


def test_directory_and_content_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_DIRS", 1)
    with pytest.raises(InvalidArtifact):
        unpack(tmp_path, make_tar([("index.html", b"ok"), ("a/b/file", b"x")]))


def test_content_exact_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_CONTENT", 2)
    assert unpack(tmp_path, make_tar([("index.html", b"ok")])).bytes == 2


def test_truncated_archive(tmp_path):
    with pytest.raises(InvalidArtifact):
        unpack(tmp_path, make_tar()[:600])
