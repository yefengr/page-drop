import logging

from . import config as C
from .archive import InvalidArtifact, inspect_tree

LOG = logging.getLogger("page_drop")


def locations(service, item_id):
    return [area for area in ("tmp", "items", "trash")
            if (service.root / area / item_id).exists()
            or (service.root / area / item_id).is_symlink()]


def matches(row, manifest):
    return (row["archive_bytes"] is not None
            and 0 < row["archive_bytes"] <= C.MAX_ARCHIVE
            and (row["bytes"], row["file_count"], row["dir_count"])
            == (manifest.bytes, len(manifest.files), manifest.dir_count))


async def recover(service):
    from .maintenance import reconcile_orphans
    rows = await service.io(service.repo.list)
    for row in rows:
        item_id, state = row["id"], row["state"]
        present = await service.io(locations, service, item_id)
        if state == "uploading" and present == ["items"]:
            try:
                manifest = await service.io(inspect_tree, service.root / "items" / item_id)
                if matches(row, manifest):
                    now = service.clock.now()
                    await service.io(service.repo.commit, item_id, manifest, now)
                    service.ledger.settle(item_id, manifest.bytes)
                    service.deadlines[item_id] = service.clock.monotonic() + row["ttl_seconds"]
                    continue
            except InvalidArtifact:
                pass
        elif state == "committed" and present == ["items"]:
            try:
                manifest = await service.io(inspect_tree, service.root / "items" / item_id)
                files = await service.io(service.repo.files, item_id)
                if (matches(row, manifest) and files == manifest.files
                        and row["accounted_bytes"] == manifest.bytes):
                    service.ledger.settle(item_id, row["accounted_bytes"])
                    service.deadlines[item_id] = service.clock.monotonic_deadline(row["expires_at"])
                    if service.is_available(row):
                        continue
                    await service.mark_deleting(row, "expired")
                    await service.cleanup(item_id)
                    continue
            except InvalidArtifact:
                pass
        elif state == "deleted" and not present:
            continue
        if state == "committed":
            LOG.error("Committed content missing or inconsistent: %s", item_id)
        reason = row["delete_reason"] or ("orphan" if state in ("committed", "deleted") else "aborted")
        await service.mark_deleting(row, reason,
            "INVALID_ARTIFACT" if state == "uploading" else None,
            "Interrupted upload" if state == "uploading" else None, measure=True)
        await service.cleanup(item_id)
    await reconcile_orphans(service)
