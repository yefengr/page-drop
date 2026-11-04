import asyncio
import ipaddress
from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
import uuid

from . import access
from . import config as C
from .archive import extract, logical_size, sync_directory
from .clock import Clock
from .config import ServiceError
from .db import Database
from .ledger import Ledger
from .leases import Leases
from .lock import InstanceLock
from .locks import LockRegistry
from .repository import Repository

LOG = logging.getLogger("page_drop")


async def worker(function, *args, cancel=None, dispose=None):
    """Cancellation never abandons a thread that still owns files."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancel is not None:
            cancel.set()
        # A second cancellation must not let cleanup race the worker either.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            failure = task.exception()
            if failure is None and dispose is not None:
                await worker(dispose, task.result())
        raise


@dataclass
class Upload:
    task: asyncio.Task
    cancel: threading.Event = field(default_factory=threading.Event)


# 对外视图字段白名单：内部认证材料与账本字段不得跟随行对象泄露。
VIEW_FIELDS = (
    "id", "state", "ttl_seconds", "title", "label", "archive_sha256", "archive_bytes",
    "bytes", "file_count", "dir_count", "created_at", "expires_at", "committed_at",
    "deleted_at", "delete_reason", "error_code", "error_message",
    "access_mode", "access_revision",
)


class ArchiveWriter:
    def __init__(self, path):
        self.file = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        os.fchmod(self.file.fileno(), 0o600)
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, chunk):
        self.file.write(chunk)
        self.digest.update(chunk)
        self.size += len(chunk)

    def close(self):
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
        finally:
            self.file.close()


class Publisher:
    def __init__(self, settings, clock=None):
        self.settings, self.clock = settings, clock or Clock()
        self.root = Path(settings.data_dir)
        self.db = Database(self.root / "page-drop.db")
        self.repo = Repository(self.db)
        self.instance_lock = InstanceLock(self.root / "service.lock")
        self.ledger, self.leases = Ledger(), Leases()
        self.gate = asyncio.Lock()
        self.locks = LockRegistry(C.MAX_LOCK_ENTRIES)
        self.limiter = access.AuthLimiter(self.clock)
        self.derive_slots = asyncio.Semaphore(C.ACCESS_DERIVE_CONCURRENCY)
        try:
            self.session_key = bytes.fromhex(settings.session_key) if settings.session_key else b""
        except ValueError:
            self.session_key = b""
        self.trusted_proxies = C.parse_networks(settings.trusted_proxies)
        self.active = {}
        self.deadlines = {}
        self.response_tasks = set()
        self.ready, self.database_failed = False, False
        self.degraded_at = None
        self.restore_lock = asyncio.Lock()
        self.verify_cursor = 0
        self.stopping = False

    async def io(self, function, *args, cancel=None, dispose=None):
        try:
            return await worker(function, *args, cancel=cancel, dispose=dispose)
        except sqlite3.OperationalError:
            # Transient failures (busy, disk I/O, full disk) must not pin the service down.
            self.ready, self.degraded_at = False, self.clock.monotonic()
            for upload in self.active.values():
                upload.cancel.set()
            LOG.exception("Database temporarily unavailable; service paused")
            raise ServiceError("STORAGE_UNAVAILABLE", "Database unavailable") from None
        except sqlite3.Error:
            self.database_failed, self.ready = True, False
            for upload in self.active.values():
                upload.cancel.set()
            LOG.exception("Database integrity failed; preserving storage")
            raise ServiceError("STORAGE_UNAVAILABLE", "Database unavailable") from None

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.instance_lock.acquire()
        for name in ("tmp", "items", "trash"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.root / name, 0o700)

    def check_initial_content(self):
        try:
            if self.db.path.exists():
                return
            for area in ("tmp", "items", "trash"):
                with os.scandir(self.root / area) as entries:
                    if next(entries, None) is not None:
                        raise ServiceError("STORAGE_UNAVAILABLE",
                            f"Missing page-drop.db with existing content in {area}; "
                            "database initialization and recovery are disabled")
        except OSError as exc:
            raise ServiceError("STORAGE_UNAVAILABLE",
                "Cannot inspect storage directories; "
                "database initialization and recovery are disabled") from exc

    async def start(self):
        from .recovery import recover
        await self.io(self.prepare)
        try:
            # 旧库中的内容不能交给新空库的孤儿清理；即使新库同时存在也拒绝猜测归属。
            if await self.io(os.path.lexists, self.root / "edge-publish.db"):
                raise ServiceError("STORAGE_UNAVAILABLE",
                    "Legacy edge-publish.db found; PageDrop requires a separate data directory "
                    "for page-drop.db; database initialization and recovery are disabled")
            await self.io(self.check_initial_content)
            await self.io(self.db.initialize)
            if not self.session_key and await self.io(self.repo.has_protected):
                raise ServiceError("STORAGE_UNAVAILABLE",
                    "PAGE_DROP_SESSION_KEY is required while protected publishes exist")
            await recover(self)
            self.ready = True
        except ServiceError:
            self.ready = False
            LOG.exception("Recovery stopped; storage retained")
        except OSError:
            await self.io(self.instance_lock.release)
            raise

    async def stop(self):
        self.stopping, self.ready = True, False
        uploads = list(self.active.values())
        for upload in uploads:
            upload.cancel.set()
            upload.task.cancel()
        if uploads:
            await asyncio.gather(*(u.task for u in uploads), return_exceptions=True)
        responses = list(self.response_tasks)
        for task in responses:
            task.cancel()
        if responses:
            await asyncio.gather(*responses, return_exceptions=True)
        for handle in list(self.leases.handles):
            await self.io(handle.close)
        await self.io(self.instance_lock.release)

    async def try_restore(self):
        if self.degraded_at is None or self.active:
            return
        now = self.clock.monotonic()
        if now - self.degraded_at < C.DB_RETRY_INTERVAL:
            return
        async with self.restore_lock:
            if self.ready or self.database_failed or self.stopping:
                return
            self.degraded_at = now
            try:
                await self.io(self.db.check)
                from .recovery import recover
                await recover(self)
            except (ServiceError, OSError, sqlite3.Error):
                LOG.exception("Database still unavailable; staying in maintenance mode")
                return
            self.ready, self.degraded_at = True, None
            LOG.warning("Database recovered; service resumed")

    async def check_storage(self):
        if self.database_failed or self.stopping:
            raise ServiceError("STORAGE_UNAVAILABLE", "Storage is not ready")
        if not self.ready:
            await self.try_restore()
            if not self.ready:
                raise ServiceError("STORAGE_UNAVAILABLE", "Storage is not ready")
        await self.io(self.db.check)
        await self.io(self.check_directories)

    def trusts_proxy(self, host):
        """只有回环或显式声明的代理地址才采信其覆盖的客户端地址。"""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        if address.is_loopback:
            return True
        return any(address in network for network in self.trusted_proxies)

    def check_directories(self):
        for area in ("tmp", "items", "trash"):
            path = self.root / area
            if not path.is_dir() or path.is_symlink():
                raise OSError("Storage directory unavailable")
        os.statvfs(self.root)

    def disk_check(self, new_upload=False):
        info = os.statvfs(self.root)
        available = info.f_bavail * info.f_frsize
        needed = C.DISK_FLOOR + (C.UPLOAD_DISK_MARGIN if new_upload else 0)
        if available < needed:
            raise ServiceError("QUOTA_EXCEEDED", "Disk reserve would be exceeded")

    def is_available(self, row):
        if row["state"] != "committed":
            return False
        deadline = self.deadlines.setdefault(row["id"], self.clock.monotonic_deadline(row["expires_at"]))
        return self.clock.now() < row["expires_at"] and self.clock.monotonic() < deadline

    def view(self, row, base_url):
        result = {name: row.get(name) for name in VIEW_FIELDS}
        result["available"] = self.is_available(row)
        if result["available"]:
            result["url"] = f"{base_url.rstrip('/')}/{row['id']}/"
        return result

    async def current(self, item_id):
        row = await self.io(self.repo.get, item_id)
        if row and row["state"] == "committed" and not self.is_available(row):
            await self.mark_deleting(row, "expired")
            row = await self.io(self.repo.get, item_id)
        return row

    async def begin(self, key, ttl, title, digest, label="", access_mode="public"):
        item_id = None
        try:
            async with self.gate:
                old = await self.io(self.repo.by_key, key)
                if old is not None:
                    async with self.locks.hold(old["id"]):
                        old = await self.current(old["id"])
                        if ((old["ttl_seconds"], old["title"], old["label"],
                             old["archive_sha256"], old["access_mode"])
                                != (ttl, title, label, digest, access_mode)
                                or not self.is_available(old)):
                            raise ServiceError("CONFLICT", "Upload key is active, terminal, or has different parameters")
                        return old, False, None
                if self.active:
                    raise ServiceError("BUSY", "Another upload is active")
                if access_mode == "code" and not self.session_key:
                    raise ServiceError("STORAGE_UNAVAILABLE", "Session key is not configured")
                await self.io(self.disk_check, True)
                candidate = str(uuid.uuid4())
                self.ledger.reserve(candidate)
                item_id = candidate
                # 授权码只在本次请求内存中保留；数据库保存可校验的派生记录。
                code = access.generate_code() if access_mode == "code" else None
                if code:
                    async with self.derive_slots:
                        record = await self.io(access.hash_code, code)
                else:
                    record = None
                revision = 1 if code else 0
                await self.io(self.repo.insert, item_id, key, self.clock.now(), ttl, title, digest,
                              label, access_mode, record, revision)
                self.active[item_id] = Upload(asyncio.current_task())
                # No await between registration and returning ownership to publish().
                return ({"id": item_id, "ttl_seconds": ttl, "access_mode": access_mode,
                         "access_revision": revision}, True, code)
        except BaseException:
            if item_id is not None and not self.database_failed:
                settled = False
                try:
                    async with self.locks.hold(item_id):
                        row = await self.io(self.repo.get, item_id)
                        if row is not None:
                            await self.mark_deleting(row, "aborted")
                            settled = True
                        else:
                            self.ledger.release(item_id)
                            settled = True
                    await self.cleanup(item_id)
                except (ServiceError, OSError):
                    # 行可能已插入但读不回来；先释放预留，恢复阶段会按 uploading 无内容清理。
                    LOG.exception("Upload rollback deferred")
                finally:
                    if not settled:
                        self.ledger.release(item_id)
            raise

    async def publish(self, request, key, ttl, title, digest, length, label="", access_mode="public"):
        row, fresh, code = await self.begin(key, ttl, title, digest, label, access_mode)
        if not fresh:
            return row, False, None
        item_id = row["id"]
        upload = self.active[item_id]
        error = None
        try:
            async with asyncio.timeout(C.UPLOAD_TOTAL_TIMEOUT):
                committed = await self.receive_and_commit(request, row, upload, digest, length)
        except asyncio.CancelledError:
            error = ServiceError("INVALID_ARTIFACT", "Upload cancelled")
            raise
        except TimeoutError:
            error = ServiceError("INVALID_ARTIFACT", "Upload timed out")
            raise error from None
        except BaseException as exc:
            error = exc
            raise
        finally:
            upload.cancel.set()
            # This task owns all disk workers and waits for them before this point.
            if error is not None and not self.database_failed:
                try:
                    async with self.locks.hold(item_id):
                        current = await self.io(self.repo.get, item_id)
                        if current and current["state"] == "committed":
                            self.ledger.settle(item_id, current["accounted_bytes"])
                        elif current:
                            await self.mark_deleting(current, "aborted",
                                getattr(error, "code", "STORAGE_UNAVAILABLE" if isinstance(error, OSError) else "INTERNAL"),
                                "Upload did not complete", measure=True)
                except (OSError, ServiceError):
                    LOG.exception("Upload cleanup deferred")
            self.active.pop(item_id, None)
            if not self.database_failed:
                await self.cleanup(item_id)
        return committed, True, code

    async def receive_and_commit(self, request, row, upload, digest, length):
        item_id = row["id"]
        root = self.root / "tmp" / item_id
        await self.io(root.mkdir, 0o700)
        writer = await self.io(ArchiveWriter, root / ".archive.tar", dispose=lambda w: w.close())
        try:
            stream = request.stream().__aiter__()
            while True:
                if upload.cancel.is_set():
                    raise ServiceError("CONFLICT", "Upload cancelled")
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), C.UPLOAD_IDLE_TIMEOUT)
                except StopAsyncIteration:
                    break
                for offset in range(0, len(chunk), C.CHUNK_SIZE):
                    if upload.cancel.is_set():
                        raise ServiceError("CONFLICT", "Upload cancelled")
                    block = memoryview(chunk)[offset:offset + C.CHUNK_SIZE]
                    if writer.size + len(block) > C.MAX_ARCHIVE:
                        raise ServiceError("PAYLOAD_TOO_LARGE", "Archive exceeds limit")
                    await self.io(self.disk_check)
                    await self.io(writer.write, block)
        finally:
            await self.io(writer.close)
        if (length is not None and length != writer.size) or writer.digest.hexdigest() != digest:
            raise ServiceError("INVALID_ARTIFACT", "Archive length or SHA-256 mismatch")
        manifest = await self.io(extract, root / ".archive.tar", root,
                                 upload.cancel, self.disk_check, cancel=upload.cancel)
        async with self.locks.hold(item_id):
            current = await self.io(self.repo.get, item_id)
            if upload.cancel.is_set() or current["state"] != "uploading":
                raise ServiceError("CONFLICT", "Upload cancelled")
            if not await self.io(self.repo.checkpoint, item_id, writer.size, manifest):
                raise ServiceError("CONFLICT", "Upload no longer active")
            await self.io(self.rename_content, item_id)
            now = self.clock.now()
            self.deadlines[item_id] = self.clock.monotonic() + row["ttl_seconds"]
            if not await self.io(self.repo.commit, item_id, manifest, now):
                raise ServiceError("CONFLICT", "Upload no longer active")
            self.ledger.settle(item_id, manifest.bytes)
            return await self.io(self.repo.get, item_id)

    def rename_content(self, item_id):
        os.rename(self.root / "tmp" / item_id, self.root / "items" / item_id)
        sync_directory(self.root / "tmp")
        sync_directory(self.root / "items")

    def residual_size(self, item_id):
        return sum(logical_size(self.root / area / item_id) for area in ("tmp", "items", "trash"))

    async def mark_deleting(self, row, reason, code=None, message=None, measure=False):
        item_id = row["id"]
        accounted = max(row["accounted_bytes"], self.ledger.reservations.get(item_id, 0))
        if measure:
            accounted = max(accounted, await self.io(self.residual_size, item_id))
        await self.io(self.repo.deleting, item_id, accounted, reason, code, message)
        self.ledger.settle(item_id, accounted)
        if item_id in self.active:
            self.active[item_id].cancel.set()

    async def delete(self, item_id):
        async with self.locks.hold(item_id):
            row = await self.io(self.repo.get, item_id)
            if row is None:
                raise ServiceError("NOT_FOUND", "Publish not found")
            if row["state"] not in ("deleted", "deleting"):
                await self.mark_deleting(row, "user")
        # 204 closes admission; physical work is owned by maintenance/request cleanup.

    def stage_trash(self, item_id):
        target = self.root / "trash" / item_id
        for area in ("items", "tmp"):
            source = self.root / area / item_id
            if source.exists() or source.is_symlink():
                if not target.exists():
                    os.rename(source, target)
                else:
                    os.rename(source, target / (".residual-" + area + "-" + str(uuid.uuid4())))
                sync_directory(self.root / area)
        sync_directory(self.root / "trash")

    def remove_trash(self, item_id):
        path = self.root / "trash" / item_id
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
        sync_directory(self.root / "trash")

    async def cleanup(self, item_id):
        if self.database_failed or item_id in self.active:
            return
        async with self.locks.hold(item_id):
            try:
                row = await self.io(self.repo.get, item_id)
                if row is None or row["state"] != "deleting":
                    return
                size = max(row["accounted_bytes"], await self.io(self.residual_size, item_id))
                await self.io(self.repo.deleting, item_id, size, row["delete_reason"])
                self.ledger.settle(item_id, size)
                await self.io(self.stage_trash, item_id)
                if self.leases.counts[item_id]:
                    return
                await self.io(self.remove_trash, item_id)
                await self.io(self.repo.deleted, item_id, self.clock.now())
                self.ledger.release(item_id)
                self.deadlines.pop(item_id, None)
            except (OSError, ServiceError):
                LOG.exception("Physical cleanup deferred; accounting retained")

    def open_manifest_file(self, item_id, entry):
        directory = os.open(self.root / "items" / item_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = entry["path"].split("/")
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
                raise OSError("Manifest content changed")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    async def admit(self, item_id, path=None, token=None):
        """内容准入：区分不可用、需要解锁、跳转和正常响应。"""
        # 先确认元数据存在，避免未知 UUID 登记常驻锁；锁内仍要重新读取。
        if await self.io(self.repo.get, item_id) is None:
            return "missing", None
        async with self.locks.hold(item_id):
            row = await self.current(item_id)
            if row is None or not self.is_available(row):
                return "missing", None
            if path is None:
                # 无尾斜杠只确认可用性；跳转本身不提供内容或会话。
                return "redirect", row
            mode = row["access_mode"]
            if mode == "code":
                revision = access.verify_session(self.session_key, item_id, token, self.clock.now())
                if revision is None or revision != row["access_revision"]:
                    return "locked", row
            elif mode != "public":
                # 未知模式按损坏数据处理，不能回退成公开访问。
                return "missing", None
            entry = await self.io(self.repo.file, item_id, path)
            if entry is None:
                return "missing", None
            if len(self.leases.handles) >= C.MAX_DOWNLOADS:
                raise ServiceError("BUSY", "Download admission is full")
            handle = await self.io(self.open_manifest_file, item_id, entry, dispose=lambda h: h.close())
            try:
                self.leases.acquire(item_id, handle)
            except BaseException:
                await self.io(handle.close)
                raise
            return "ok", (handle, entry)

    async def unlock(self, item_id, code):
        """口令校验；返回 missing / denied / ok 三态。"""
        row = await self.io(self.repo.get, item_id)
        if row is None or row["access_mode"] != "code" or not self.is_available(row):
            return "missing", None
        # 记录本次校验依据的版本：重置后旧口令不能换到新版本会话。
        checked_revision = row["access_revision"]
        valid = False
        if code and row["access_hash"]:
            async with self.derive_slots:
                valid = await self.io(access.verify_code, row["access_hash"], code)
        if not valid:
            return "denied", row
        async with self.locks.hold(item_id):
            row = await self.current(item_id)
            if row is None or row["access_mode"] != "code" or not self.is_available(row):
                return "missing", None
            if row["access_revision"] != checked_revision:
                return "denied", row
            now = self.clock.now()
            expires = min(now + C.ACCESS_SESSION_SECONDS, row["expires_at"])
            token = access.session_token(self.session_key, item_id, row["access_revision"], now, expires)
            return "ok", (row, token)

    async def reset_code(self, item_id, expected_revision):
        """按当前版本条件换码；同版本最多生效一次。"""
        if not self.session_key:
            raise ServiceError("STORAGE_UNAVAILABLE", "Session key is not configured")
        code = access.generate_code()
        async with self.derive_slots:
            record = await self.io(access.hash_code, code)
        async with self.locks.hold(item_id):
            row = await self.current(item_id)
            if row is None or not self.is_available(row):
                raise ServiceError("NOT_FOUND", "Publish not found")
            if row["access_mode"] != "code":
                raise ServiceError("CONFLICT", "Publish is not protected")
            if row["access_revision"] != expected_revision:
                raise ServiceError("CONFLICT", "Access revision changed")
            if not await self.io(self.repo.replace_access, item_id, expected_revision, record):
                raise ServiceError("CONFLICT", "Access revision changed")
            return await self.io(self.repo.get, item_id), code

    async def release(self, item_id, handle):
        try:
            await self.io(handle.close)
        finally:
            self.leases.release(item_id, handle)
