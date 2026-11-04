from dataclasses import dataclass
import ipaddress
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

# 访问码保护：授权码、会话与认证预算。
ACCESS_CODE_LENGTH = 8
ACCESS_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ACCESS_SESSION_SECONDS = 3600
ACCESS_SCRYPT_N = 16384
ACCESS_SCRYPT_R = 8
ACCESS_SCRYPT_P = 1
ACCESS_SALT_BYTES = 16
ACCESS_KEY_BYTES = 32
# scrypt 的 maxmem 需覆盖 128 * N * r 并留余量；固定上限同时限制攻击者构造的超大参数。
ACCESS_SCRYPT_MAXMEM = 64 * 1024 * 1024
ACCESS_DERIVE_CONCURRENCY = 2
ACCESS_BODY_LIMIT = 4096
ACCESS_COOKIE_PREFIX = "pd_"

# 认证失败预算：按 (发布, 客户端)、客户端和发布三个维度分别计数。
AUTH_FAILURE_LIMIT = 10
AUTH_IP_LIMIT = 100
AUTH_ITEM_LIMIT = 1000
AUTH_WINDOW = 300
AUTH_TRACKED_LIMIT = 10000
# 每个 UUID 的互斥锁只在有人使用时登记，容量上限避免未知请求长期占用内存。
MAX_LOCK_ENTRIES = 10000

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
    "STORAGE_UNAVAILABLE": 503, "INTERNAL": 500, "TOO_MANY_REQUESTS": 429,
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
    # 会话签名密钥与管理 Token 完全独立；为空时拒绝受保护发布。
    session_key: str = ""
    # 逗号分隔的可信代理网段；仅在直连对端属于该集合时采信 X-Forwarded-For。
    trusted_proxies: str = ""

    @classmethod
    def from_env(cls):
        token = (os.environ.get("PAGE_DROP_TOKEN") or "").strip() or (os.environ.get("EDGE_PUBLISH_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("PAGE_DROP_TOKEN is required (EDGE_PUBLISH_TOKEN is supported for compatibility)")
        data_dir = os.environ.get("PAGE_DROP_DATA_DIR") or os.environ.get("EDGE_PUBLISH_DATA_DIR") or "/data"
        public_url = ((os.environ.get("PAGE_DROP_PUBLIC_URL") or "").strip()
                      or (os.environ.get("EDGE_PUBLISH_PUBLIC_URL") or "").strip()).rstrip("/")
        session_key = (os.environ.get("PAGE_DROP_SESSION_KEY") or "").strip()
        if session_key and not _valid_session_key(session_key):
            raise RuntimeError("PAGE_DROP_SESSION_KEY must be 64 hexadecimal characters (32 bytes)")
        trusted_proxies = (os.environ.get("PAGE_DROP_TRUSTED_PROXIES") or "").strip()
        return cls(token, Path(data_dir), public_url, session_key, trusted_proxies)


def parse_networks(value: str):
    """解析逗号分隔的代理网段；非法条目直接拒绝启动，不静默忽略。"""
    networks = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise RuntimeError(f"invalid PAGE_DROP_TRUSTED_PROXIES entry: {part}") from None
    return tuple(networks)


def _valid_session_key(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
