from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tarfile

from . import config as C
from .config import ServiceError


class InvalidArtifact(ServiceError):
    def __init__(self, message="Invalid artifact"):
        super().__init__("INVALID_ARTIFACT", message)


@dataclass
class Manifest:
    files: list
    bytes: int
    dir_count: int


def validate_path(name: str) -> str:
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InvalidArtifact("Path must be UTF-8") from None
    parts = name.split("/")
    if (not encoded or len(encoded) > C.MAX_PATH_BYTES or "\x00" in name
            or any(not part or part.startswith(".") for part in parts)
            or len(parts) > C.MAX_DEPTH):
        raise InvalidArtifact("Invalid artifact path")
    return name


def mime_type(path):
    return C.MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check(cancel, disk_check):
    if cancel is not None and cancel.is_set():
        raise InvalidArtifact("Upload cancelled")
    if disk_check is not None:
        disk_check()


def extract(archive: Path, root: Path, cancel=None, disk_check=None) -> Manifest:
    files, directories, explicit = [], set(), set()
    file_names = set()
    total, end = 0, 0
    try:
        with tarfile.open(archive, mode="r|", encoding="utf-8", errors="surrogateescape") as tar:
            for member in tar:
                _check(cancel, disk_check)
                if not (member.isfile() or member.isdir()) or member.sparse is not None:
                    raise InvalidArtifact("Only ordinary files and directories are accepted")
                path = validate_path(member.name)
                parts = path.split("/")
                if path in explicit:
                    raise InvalidArtifact("Duplicate archive member")
                explicit.add(path)
                parents = {"/".join(parts[:n]) for n in range(1, len(parts))}
                if parents & file_names or (member.isdir() and path in file_names):
                    raise InvalidArtifact("File and directory conflict")
                created = parents - directories
                if member.isdir():
                    created.add(path)
                if not member.isdir() and path in directories:
                    raise InvalidArtifact("File and directory conflict")
                for directory in sorted(created, key=lambda p: p.count("/")):
                    (root / directory).mkdir(mode=0o700, exist_ok=True)
                directories.update(created)
                if len(directories) > C.MAX_DIRS:
                    raise InvalidArtifact("Too many directories")
                end = member.offset_data + ((member.size + 511) // 512) * 512
                if member.isdir():
                    continue
                if len(files) >= C.MAX_FILES:
                    raise InvalidArtifact("Too many files")
                if member.size < 0 or total + member.size > C.MAX_CONTENT:
                    raise ServiceError("PAYLOAD_TOO_LARGE", "Content exceeds limit")
                source = tar.extractfile(member)
                size = 0
                with source, (root / path).open("xb") as output:
                    os.chmod(root / path, 0o600)
                    while chunk := source.read(C.CHUNK_SIZE):
                        _check(cancel, disk_check)
                        size += len(chunk)
                        total += len(chunk)
                        if total > C.MAX_CONTENT:
                            raise ServiceError("PAYLOAD_TOO_LARGE", "Content exceeds limit")
                        output.write(chunk)
                    output.flush()
                    os.fchmod(output.fileno(), 0o440)
                    os.fsync(output.fileno())
                if size != member.size:
                    raise InvalidArtifact("Truncated file")
                files.append({"path": path, "size": size, "mime": mime_type(path)})
                file_names.add(path)
        if "index.html" not in file_names:
            raise InvalidArtifact("Root index.html is required")
        # tarfile stops at the first end marker. Check the unread padding separately.
        with archive.open("rb") as raw:
            raw.seek(end)
            padding = 0
            while chunk := raw.read(C.CHUNK_SIZE):
                _check(cancel, disk_check)
                if any(chunk):
                    raise InvalidArtifact("Nonzero data after tar end")
                padding += len(chunk)
            if padding < 1024 or padding % 512:
                raise InvalidArtifact("Missing or invalid tar end blocks")
        archive.unlink()
        for directory in sorted(directories, key=lambda p: p.count("/"), reverse=True):
            os.chmod(root / directory, 0o750)
            sync_directory(root / directory)
        os.chmod(root, 0o750)
        sync_directory(root)
        return Manifest(files, total, len(directories))
    except (tarfile.TarError, UnicodeError, ValueError, EOFError) as exc:
        raise InvalidArtifact("Malformed uncompressed tar") from exc


def inspect_tree(root: Path) -> Manifest:
    files, directories, total = [], 0, 0
    if not root.is_dir() or root.is_symlink():
        raise InvalidArtifact("Content directory missing")
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(base) / name
            relative = validate_path(path.relative_to(root).as_posix())
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                directories += 1
            elif stat.S_ISREG(mode):
                size = path.stat().st_size
                total += size
                files.append({"path": relative, "size": size, "mime": mime_type(relative)})
            else:
                raise InvalidArtifact("Unexpected content type")
    if (len(files) > C.MAX_FILES or directories > C.MAX_DIRS or total > C.MAX_CONTENT
            or not any(f["path"] == "index.html" for f in files)):
        raise InvalidArtifact("Content does not match artifact limits")
    return Manifest(sorted(files, key=lambda f: f["path"]), total, directories)


def logical_size(path: Path) -> int:
    """Conservatively count all regular files, including unfinished archive buffers."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(info.st_mode):
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    total = 0
    for base, _, names in os.walk(path, followlinks=False, onerror=lambda e: _raise(e)):
        for name in names:
            info = (Path(base) / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _raise(error):
    raise error
