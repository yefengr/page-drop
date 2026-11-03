from .db import Database


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, item_id):
        return self._one("SELECT * FROM items WHERE id=?", (item_id,))

    def by_key(self, key):
        return self._one("SELECT * FROM items WHERE idempotency_key=?", (key,))

    def _one(self, sql, args):
        with self.db.connect() as conn:
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row is not None else None

    def list(self):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY upload_started_at, id")]

    def insert(self, item_id, key, now, ttl, title, digest, label=""):
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO items
                (id,idempotency_key,upload_started_at,ttl_seconds,title,label,archive_sha256,state)
                VALUES (?,?,?,?,?,?,?,'uploading')""", (item_id, key, now, ttl, title, label, digest))

    def checkpoint(self, item_id, archive_bytes, manifest):
        with self.db.connect() as conn:
            cursor = conn.execute("""UPDATE items SET archive_bytes=?,bytes=?,file_count=?,dir_count=?
                WHERE id=? AND state='uploading'""",
                (archive_bytes, manifest.bytes, len(manifest.files), manifest.dir_count, item_id))
            return cursor.rowcount == 1

    def commit(self, item_id, manifest, now):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None or row["state"] != "uploading":
                return False
            conn.execute("DELETE FROM files WHERE item_id=?", (item_id,))
            conn.executemany("INSERT INTO files VALUES (?,?,?,?)",
                [(item_id, f["path"], f["size"], f["mime"]) for f in manifest.files])
            conn.execute("""UPDATE items SET state='committed',created_at=?,expires_at=?,
                committed_at=?,bytes=?,file_count=?,dir_count=?,accounted_bytes=? WHERE id=?""",
                (now, now + row["ttl_seconds"], now, manifest.bytes,
                 len(manifest.files), manifest.dir_count, manifest.bytes, item_id))
            return True

    def deleting(self, item_id, accounted, reason, code=None, message=None):
        with self.db.connect() as conn:
            conn.execute("""UPDATE items SET state='deleting',accounted_bytes=?,
                delete_reason=COALESCE(delete_reason,?),error_code=COALESCE(error_code,?),
                error_message=COALESCE(error_message,?) WHERE id=?""",
                (accounted, reason, code, message, item_id))

    def deleted(self, item_id, now):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM files WHERE item_id=?", (item_id,))
            conn.execute("""UPDATE items SET state='deleted',accounted_bytes=0,deleted_at=?
                WHERE id=? AND state='deleting'""", (now, item_id))

    def file(self, item_id, path):
        return self._one("SELECT path,size,mime FROM files WHERE item_id=? AND path=?", (item_id, path))

    def files(self, item_id):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT path,size,mime FROM files WHERE item_id=? ORDER BY path", (item_id,))]

    def purge(self, cutoff):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM items WHERE state='deleted' AND deleted_at<=?", (cutoff,))
