from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
 upload_started_at INTEGER NOT NULL, created_at INTEGER, expires_at INTEGER,
 ttl_seconds INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
 label TEXT NOT NULL DEFAULT '',
 archive_sha256 TEXT NOT NULL, archive_bytes INTEGER,
 bytes INTEGER, file_count INTEGER, dir_count INTEGER,
 accounted_bytes INTEGER NOT NULL DEFAULT 0,
 access_mode TEXT NOT NULL DEFAULT 'public', access_hash TEXT,
 access_revision INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL CHECK(state IN ('uploading','committed','deleting','deleted')),
 committed_at INTEGER, deleted_at INTEGER, delete_reason TEXT,
 error_code TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS files (
 item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 path TEXT NOT NULL, size INTEGER NOT NULL, mime TEXT NOT NULL,
 PRIMARY KEY(item_id, path)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_expiry ON items(state, expires_at);
CREATE INDEX IF NOT EXISTS items_deleted ON items(state, deleted_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        existed = self.path.exists()
        with self.connect() as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("database integrity check failed")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existed and not {"items", "files", "meta"} <= tables:
                raise sqlite3.DatabaseError("unrecognized database schema")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version is not None and version[0] not in ("1", "2", "3"):
                raise sqlite3.DatabaseError("unsupported database schema")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
            expected = {"id", "idempotency_key", "state", "accounted_bytes", "archive_bytes"}
            if not expected <= columns:
                raise sqlite3.DatabaseError("invalid items schema")
            if "label" not in columns:
                # v1 → v2：仅新增带默认值的列，不改动既有行。
                conn.execute("ALTER TABLE items ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            if "access_mode" not in columns:
                # v2 → v3：存量发布一律保持公开，不自动给旧链接加码。
                conn.execute("ALTER TABLE items ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'public'")
            if "access_hash" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN access_hash TEXT")
            if "access_revision" not in columns:
                conn.execute("ALTER TABLE items ADD COLUMN access_revision INTEGER NOT NULL DEFAULT 0")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '3')")

    def check(self):
        if not self.path.is_file():
            raise OSError("database missing")
        with self.connect() as conn:
            conn.execute("SELECT id, state, accounted_bytes, access_mode FROM items LIMIT 1").fetchall()
            conn.execute("SELECT path, size, mime FROM files LIMIT 1").fetchall()
