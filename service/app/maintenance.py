import asyncio
import logging
import os
from pathlib import Path
import shutil
import uuid

from . import config as C
from .archive import logical_size, sync_directory
from .config import ServiceError
from .ledger import Orphan

LOG = logging.getLogger("page_drop")


def discover(service, known):
    result = {}
    for area in ("tmp", "items", "trash"):
        for path in (service.root / area).iterdir():
            if path.name not in known:
                result[str(path)] = logical_size(path)
    return result


def rename_orphan(source, target):
    os.rename(source, target)
    sync_directory(source.parent)
    sync_directory(target.parent)


def remove_orphan(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    sync_directory(path.parent)


async def reconcile_orphans(service):
    if service.database_failed:
        return
    # Serialize the short snapshot with new row creation, not network or recursive removal.
    async with service.gate:
        rows = await service.io(service.repo.list)
        found = await service.io(discover, service, {row["id"] for row in rows})
        old = service.ledger.orphans
        now = service.clock.monotonic()
        service.ledger.orphans = {
            path: Orphan(size, old[path].first_seen if path in old else now)
            for path, size in found.items()
        }
    for path, entry in list(service.ledger.orphans.items()):
        if path not in old:
            LOG.warning("Unowned content accounted; observation period started")
        if now - entry.first_seen < C.ORPHAN_GRACE:
            continue
        source = Path(path)
        try:
            if source.parent != service.root / "trash":
                target = service.root / "trash" / ("orphan-" + str(uuid.uuid4()))
                await service.io(rename_orphan, source, target)
                service.ledger.orphans.pop(path, None)
                path, source = str(target), target
                service.ledger.orphans[path] = entry
            await service.io(remove_orphan, source)
            service.ledger.orphans.pop(path, None)
        except (OSError, ServiceError):
            LOG.exception("Orphan cleanup deferred; accounting retained")


async def maintain(service):
    if not service.ready or service.database_failed or service.stopping:
        return
    await service.check_storage()
    from .archive import InvalidArtifact, inspect_tree
    from .recovery import locations, matches
    rows = await service.io(service.repo.list)
    committed = [row["id"] for row in rows
                 if row["state"] == "committed" and row["id"] not in service.active]
    verify_id = None
    if committed:
        verify_id = committed[service.verify_cursor % len(committed)]
        service.verify_cursor = (service.verify_cursor + 1) % len(committed)
    for row in rows:
        item_id = row["id"]
        if item_id in service.active:
            continue
        async with service.locks[item_id]:
            row = await service.current(item_id)
            if row is None:
                continue
            present = await service.io(locations, service, item_id)
            if row["state"] == "uploading":
                await service.mark_deleting(row, "aborted", measure=True)
            elif row["state"] == "deleted" and present:
                await service.mark_deleting(row, "orphan", measure=True)
            elif row["state"] == "committed" and item_id == verify_id:
                # 全量清单复核每轮最多一个，避免 30 秒周期对全部文件重复 stat。
                consistent = False
                if present == ["items"]:
                    try:
                        manifest = await service.io(inspect_tree, service.root / "items" / item_id)
                        files = await service.io(service.repo.files, item_id)
                        consistent = (matches(row, manifest) and files == manifest.files
                                      and row["accounted_bytes"] == manifest.bytes)
                    except InvalidArtifact:
                        pass
                if not consistent:
                    LOG.error("Committed content reconciliation failed: %s", item_id)
                    await service.mark_deleting(row, "orphan", measure=True)
        await service.cleanup(item_id)
    await reconcile_orphans(service)
    await service.io(service.repo.purge, service.clock.now() - C.RECORD_RETENTION)


async def loop(service):
    while True:
        await asyncio.sleep(C.MAINTENANCE_INTERVAL)
        try:
            await maintain(service)
        except (OSError, ServiceError):
            LOG.exception("Maintenance failed; storage retained")
        except Exception:
            LOG.exception("Unexpected maintenance failure")
