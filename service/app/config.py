from dataclasses import dataclass
import os
from pathlib import Path

MIB = 1024 * 1024
MAX_CONTENT = 200 * MIB
MAX_ARCHIVE = 256 * MIB
MAX_FILES = 5000
MAX_DIRS = 1000
MAX_DEPTH = 32
MAX_PATH_BYTES = 255
CONTENT_BUDGET = 5 * 1024 * MIB
DISK_FLOOR = 2 * 1024 * MIB
UPLOAD_DISK_MARGIN = 512 * MIB
UPLOAD_TOTAL_TIMEOUT = 1800
UPLOAD_IDLE_TIMEOUT = 120
DOWNLOAD_IDLE_TIMEOUT = 60
MAX_DOWNLOADS = 32
MAINTENANCE_INTERVAL = 30
DB_RETRY_INTERVAL = 10
ORPHAN_GRACE = 3600
RECORD_RETENTION = 86400
CHUNK_SIZE = 64 * 1024
THREAD_WORKERS = 8

MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json",
    ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".pdf": "application/pdf", ".wasm": "application/wasm",
    ".woff": "font/woff", ".woff2": "font/woff2", ".mp4": "video/mp4",
    ".webm": "video/webm", ".avif": "image/avif", ".map": "application/json",
}
STATUS_CODES = {
    "INVALID_ARGUMENT": 400, "UNAUTHORIZED": 401, "NOT_FOUND": 404,
    "CONFLICT": 409, "PAYLOAD_TOO_LARGE": 413, "UNSUPPORTED_MEDIA_TYPE": 415,
    "INVALID_ARTIFACT": 422, "BUSY": 503, "QUOTA_EXCEEDED": 507,
    "STORAGE_UNAVAILABLE": 503, "INTERNAL": 500,
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive, noimageindex",
    "Referrer-Policy": "no-referrer",
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        self.status = STATUS_CODES[code]
        super().__init__(message)


@dataclass(frozen=True)
class Settings:
    token: str
    data_dir: Path
    public_url: str = ""

    @classmethod
    def from_env(cls):
        token = (os.environ.get("PAGE_DROP_TOKEN") or "").strip() or (os.environ.get("EDGE_PUBLISH_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("PAGE_DROP_TOKEN is required (EDGE_PUBLISH_TOKEN is supported for compatibility)")
        data_dir = os.environ.get("PAGE_DROP_DATA_DIR") or os.environ.get("EDGE_PUBLISH_DATA_DIR") or "/data"
        public_url = ((os.environ.get("PAGE_DROP_PUBLIC_URL") or "").strip()
                      or (os.environ.get("EDGE_PUBLISH_PUBLIC_URL") or "").strip()).rstrip("/")
        return cls(token, Path(data_dir), public_url)
