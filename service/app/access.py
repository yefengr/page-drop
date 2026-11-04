"""访问码保护：授权码、带盐校验记录、会话签名与认证预算。

授权码由服务端生成，数据库只保存可校验的派生记录；会话密钥与管理 Token
分离，轮换任一凭据都不会破坏其他凭据。此处不承诺隐藏受保护发布的存在性。
"""

import base64
import hashlib
import hmac
import os
import secrets

from . import config as C

SESSION_VERSION = "1"
CODE_RECORD_VERSION = "1"
# 允许解释的参数上限；记录中的参数越界视为损坏，避免用超大成本阻塞服务。
_MAX_N, _MAX_R, _MAX_P, _MAX_KEY = 262144, 32, 8, 64


def generate_code() -> str:
    return "".join(secrets.choice(C.ACCESS_CODE_ALPHABET) for _ in range(C.ACCESS_CODE_LENGTH))


def hash_code(code: str, salt: bytes | None = None) -> str:
    """生成自描述校验记录；参数随记录保存，便于将来调整成本。"""
    salt = salt if salt is not None else os.urandom(C.ACCESS_SALT_BYTES)
    derived = _derive(code, salt, C.ACCESS_SCRYPT_N, C.ACCESS_SCRYPT_R, C.ACCESS_SCRYPT_P, C.ACCESS_KEY_BYTES)
    return "$".join((
        "scrypt", CODE_RECORD_VERSION, str(C.ACCESS_SCRYPT_N), str(C.ACCESS_SCRYPT_R),
        str(C.ACCESS_SCRYPT_P),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    ))


def verify_code(record, code) -> bool:
    """恒定时间比较；损坏或越界的记录一律视为不匹配。"""
    if not isinstance(record, str) or not isinstance(code, str):
        return False
    parts = record.split("$")
    if len(parts) != 7 or parts[0] != "scrypt" or parts[1] != CODE_RECORD_VERSION:
        return False
    try:
        n, r, p = int(parts[2]), int(parts[3]), int(parts[4])
        salt = base64.b64decode(parts[5], validate=True)
        expected = base64.b64decode(parts[6], validate=True)
    except (ValueError, TypeError):
        return False
    if not 0 < len(salt) <= 64 or not 0 < len(expected) <= _MAX_KEY:
        return False
    if not (0 < n <= _MAX_N and 0 < r <= _MAX_R and 0 < p <= _MAX_P):
        return False
    if n * r * 128 > C.ACCESS_SCRYPT_MAXMEM:
        return False
    try:
        derived = _derive(code, salt, n, r, p, len(expected))
    except (ValueError, OverflowError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def _derive(code, salt, n, r, p, length):
    return hashlib.scrypt(code.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=length, maxmem=C.ACCESS_SCRYPT_MAXMEM)


def _sign(key: bytes, item_id: str, revision: int, issued_at: int, expires_at: int) -> str:
    message = f"pd-session|{SESSION_VERSION}|{item_id}|{revision}|{issued_at}|{expires_at}"
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def session_token(key: bytes, item_id: str, revision: int, issued_at: int, expires_at: int) -> str:
    return ".".join((SESSION_VERSION, str(revision), str(issued_at), str(expires_at),
                     _sign(key, item_id, revision, issued_at, expires_at)))


def verify_session(key: bytes, item_id: str, token, now: int):
    """返回匹配的 revision；任何不合法输入都按未解锁处理。"""
    if not key or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != SESSION_VERSION:
        return None
    try:
        revision, issued_at, expires_at = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    if revision <= 0 or expires_at <= now or issued_at > now:
        return None
    if expires_at - issued_at <= 0 or expires_at - issued_at > C.ACCESS_SESSION_SECONDS:
        return None
    try:
        presented = bytes.fromhex(parts[4])
    except ValueError:
        return None
    expected = bytes.fromhex(_sign(key, item_id, revision, issued_at, expires_at))
    if not hmac.compare_digest(expected, presented):
        return None
    return revision


def session_cookie_name(item_id: str) -> str:
    return C.ACCESS_COOKIE_PREFIX + item_id.replace("-", "")


class AuthLimiter:
    """进程内认证预算；容量满时拒绝新增尝试，不淘汰仍生效的计数。"""

    def __init__(self, clock, limit=C.AUTH_TRACKED_LIMIT):
        self.clock, self.limit = clock, limit
        self.entries = {}

    def _scopes(self, item_id, client):
        return (
            ("pair", (item_id, client), C.AUTH_FAILURE_LIMIT),
            ("ip", client, C.AUTH_IP_LIMIT),
            ("item", item_id, C.AUTH_ITEM_LIMIT),
        )

    def blocked(self, item_id, client):
        """返回需要等待的秒数；0 表示可以继续。"""
        now = self.clock.monotonic()
        delay = 0.0
        for scope, key, limit in self._scopes(item_id, client):
            entry = self.entries.get((scope, key))
            if entry is None or entry[1] <= now:
                continue
            if entry[0] >= limit:
                delay = max(delay, entry[1] - now)
        return int(delay + 0.999) if delay > 0 else 0

    def submit(self, item_id, client):
        """每次提交都计入客户端与发布维度的总体预算。"""
        now = self.clock.monotonic()
        expiry = now + C.AUTH_WINDOW
        try:
            self._bump(("ip", client), now, expiry)
            self._bump(("item", item_id), now, expiry)
        except _LimiterFull:
            return False
        return True

    def failure(self, item_id, client):
        """失败额外计入 (发布, 客户端) 维度；容量满时返回 False。"""
        now = self.clock.monotonic()
        try:
            self._bump(("pair", (item_id, client)), now, now + C.AUTH_WINDOW)
        except _LimiterFull:
            return False
        return True

    def clear(self, item_id, client):
        for scope, key, _ in self._scopes(item_id, client):
            if scope == "pair":
                self.entries.pop((scope, key), None)

    def _bump(self, key, now, expiry):
        entry = self.entries.get(key)
        if entry is None:
            if len(self.entries) >= self.limit:
                raise _LimiterFull()
            self.entries[key] = [1, expiry]
            return
        if entry[1] <= now:
            entry[0], entry[1] = 1, expiry
        else:
            entry[0] += 1

    def purge(self, now=None):
        now = self.clock.monotonic() if now is None else now
        for key, entry in list(self.entries.items()):
            if entry[1] <= now:
                self.entries.pop(key, None)


class _LimiterFull(Exception):
    pass


def is_limiter_full(exc) -> bool:
    return isinstance(exc, _LimiterFull)
