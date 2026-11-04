import asyncio
import hmac
import logging
import re
import unicodedata
from urllib.parse import parse_qs, unquote_to_bytes
import uuid

from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import access
from . import config as C
from .archive import InvalidArtifact, validate_path
from .config import ServiceError
from .pages import LANDING, NOT_FOUND, unlock as unlock_page
from .service import worker

LOG = logging.getLogger("page_drop")


def error_response(exc):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


def public_base(service, request):
    """Prefer an explicit public origin: reverse proxies may not expose the client scheme."""
    return service.settings.public_url or str(request.base_url).rstrip("/")


class ResponseBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {h.lower().encode() for h in C.SECURITY_HEADERS}]
                headers.extend((k.lower().encode(), v.encode()) for k, v in C.SECURITY_HEADERS.items())
                message = dict(message, headers=headers)
            if scope["method"] == "HEAD" and message["type"] == "http.response.body":
                message = dict(message, body=b"")
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception as exc:
            if started:
                LOG.exception("Response interrupted after headers")
                raise
            if isinstance(exc, ServiceError):
                failure = exc
            elif isinstance(exc, OSError):
                failure = ServiceError("STORAGE_UNAVAILABLE", "Storage unavailable")
                LOG.exception("Storage operation failed")
            elif isinstance(exc, ClientDisconnect):
                failure = ServiceError("INVALID_ARTIFACT", "Upload disconnected")
            else:
                failure = ServiceError("INTERNAL", "Internal service error")
                LOG.exception("Unhandled request failure")
            await error_response(failure)(scope, receive, safe_send)


def wants_html(request):
    return "text/html" in request.headers.get("accept", "").lower()


def not_found(request):
    if wants_html(request):
        return HTMLResponse(NOT_FOUND, status_code=404)
    return Response(status_code=404)


async def landing(request):
    service = request.app.state.publisher
    await service.check_storage()
    return HTMLResponse(LANDING)


def authorize(request):
    service = request.app.state.publisher
    actual = request.headers.get("authorization", "").encode("utf-8")
    expected = ("Bearer " + service.settings.token).encode("utf-8")
    if not hmac.compare_digest(actual, expected):
        raise ServiceError("UNAUTHORIZED", "Unauthorized")
    return service


def query(request, allowed):
    values = request.query_params
    pairs = values.multi_items()
    if len(pairs) != len(values) or set(values) - set(allowed):
        raise ServiceError("INVALID_ARGUMENT", "Invalid query parameters")
    return values


def canonical_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ServiceError("INVALID_ARGUMENT", "Invalid UUID") from None


def upload_parameters(request):
    args = query(request, {"ttl", "title", "label", "access"})
    match = re.fullmatch(r"([0-9]+)([mhd])", args.get("ttl", ""))
    if match is None or len(match[1]) > 10:
        raise ServiceError("INVALID_ARGUMENT", "Invalid TTL")
    ttl = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    title = args.get("title", "")
    label = args.get("label", "").strip()
    access_mode = args.get("access", "public")
    if access_mode not in ("public", "code"):
        raise ServiceError("INVALID_ARGUMENT", "Invalid access mode")
    if not 600 <= ttl <= 30 * 86400 or len(title) > 200 or any(unicodedata.category(c).startswith("C") for c in title):
        raise ServiceError("INVALID_ARGUMENT", "Invalid TTL or title")
    if len(label) > 64 or any(unicodedata.category(c).startswith("C") for c in label):
        raise ServiceError("INVALID_ARGUMENT", "Invalid label")
    key = canonical_uuid(request.headers.get("idempotency-key", ""))
    digest = request.headers.get("x-archive-sha256", "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ServiceError("INVALID_ARGUMENT", "Invalid archive SHA-256")
    if (request.headers.get("content-type", "").lower() != "application/x-tar"
            or request.headers.get("content-encoding", "identity").lower() != "identity"):
        raise ServiceError("UNSUPPORTED_MEDIA_TYPE", "An uncompressed application/x-tar body is required")
    raw_length = request.headers.get("content-length")
    length = None
    if raw_length is not None:
        if not re.fullmatch(r"[0-9]+", raw_length) or len(raw_length) > 15:
            raise ServiceError("INVALID_ARGUMENT", "Invalid Content-Length")
        length = int(raw_length)
        if length > C.MAX_ARCHIVE:
            raise ServiceError("PAYLOAD_TOO_LARGE", "Archive exceeds limit")
    return key, ttl, title, digest, length, label, access_mode


async def publish(request):
    service = authorize(request)
    await service.check_storage()
    row, fresh, code = await service.publish(request, *upload_parameters(request))
    result = dict(row, url=f"{public_base(service, request)}/{row['id']}/")
    fields = ("id", "url", "expires_at", "created_at", "bytes", "file_count")
    payload = {key: result[key] for key in fields}
    payload["access_mode"] = row["access_mode"]
    payload["access_revision"] = row["access_revision"]
    if fresh and code:
        # 明文只在本次新提交中出现一次；幂等复用不返回。
        payload["access_code"] = code
    return JSONResponse(payload, status_code=201 if fresh else 200)


async def listing(request):
    service = authorize(request)
    await service.check_storage()
    args = query(request, {"all"})
    if "all" in args and args["all"] != "1":
        raise ServiceError("INVALID_ARGUMENT", "Invalid all parameter")
    result = []
    for row in await service.io(service.repo.list):
        async with service.locks.hold(row["id"]):
            current = await service.current(row["id"])
            if current is not None:
                view = service.view(current, public_base(service, request))
                if args.get("all") == "1" or view["available"]:
                    result.append(view)
    return JSONResponse(result)


async def status(request):
    service = authorize(request)
    await service.check_storage()
    query(request, set())
    value = canonical_uuid(request.path_params["value"])
    if request.url.path.startswith("/api/v1/uploads/"):
        row = await service.io(service.repo.by_key, value)
        if row is None:
            raise ServiceError("NOT_FOUND", "Upload not found")
        value = row["id"]
    async with service.locks.hold(value):
        row = await service.current(value)
        if row is None:
            raise ServiceError("NOT_FOUND", "Publish not found")
        return JSONResponse(service.view(row, public_base(service, request)))


async def delete(request):
    service = authorize(request)
    await service.check_storage()
    query(request, set())
    await service.delete(canonical_uuid(request.path_params["value"]))
    return Response(status_code=204)


async def health(request):
    try:
        await request.app.state.publisher.check_storage()
        return Response(status_code=200)
    except (ServiceError, OSError):
        return Response(status_code=503)


def content_path(request):
    """解析内容路径；返回 (item_id, path, 是否为发布根) 或 None。"""
    # Split the wire path first: an encoded slash is not a path separator.
    raw = request.scope.get("raw_path", request.url.path.encode("utf-8")).split(b"?", 1)[0]
    parts = raw.split(b"/")
    if len(parts) < 2 or parts[0]:
        return None
    try:
        decoded = [unquote_to_bytes(part).decode("utf-8", errors="strict") for part in parts[1:]]
        if any("/" in part for part in decoded):
            return None
        item_id = str(uuid.UUID(decoded[0]))
        if len(decoded) == 1:
            return item_id, None, False
        if decoded[1:] == [""]:
            return item_id, "index.html", True
        return item_id, validate_path("/".join(decoded[1:])), False
    except (UnicodeError, ValueError, InvalidArtifact):
        return None


def client_address(request):
    """只在直连对端是回环或已声明代理时采用其覆盖的 X-Forwarded-For。"""
    service = request.app.state.publisher
    client = request.scope.get("client")
    host = client[0] if client else ""
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded and service.trusts_proxy(host):
        return forwarded
    return host or "unknown"


def session_token(request, item_id):
    return request.cookies.get(access.session_cookie_name(item_id))


async def read_unlock_form(request):
    """流式读取小型表单，避免为公开端点整包缓冲。"""
    if not request.headers.get("content-type", "").lower().startswith(
            "application/x-www-form-urlencoded"):
        raise ServiceError("UNSUPPORTED_MEDIA_TYPE", "A form body is required")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > C.ACCESS_BODY_LIMIT:
            raise ServiceError("INVALID_ARGUMENT", "Unlock form too large")
        chunks.append(chunk)
    try:
        values = parse_qs(b"".join(chunks).decode("utf-8", "strict"), keep_blank_values=True)
    except UnicodeError:
        raise ServiceError("INVALID_ARGUMENT", "Invalid form encoding") from None
    codes = values.get("code", [])
    if set(values) - {"code"} or len(codes) > 1:
        raise ServiceError("INVALID_ARGUMENT", "Invalid form fields")
    return codes[0] if codes else ""


def unlock_response(error="", status_code=200):
    return HTMLResponse(unlock_page(error), status_code=status_code, headers={
        "Content-Security-Policy":
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    })


def byte_range(value, size):
    if value is None:
        return 0, size, 200
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
    if match is None or not any(match.groups()) or size == 0:
        return None
    first, last = match.groups()
    if len(first) > 20 or len(last) > 20:
        return None
    if not first:
        suffix = int(last)
        if suffix == 0:
            return None
        return max(0, size - suffix), size, 206
    start = int(first)
    end = min(size, int(last) + 1) if last else size
    if start >= size or end <= start:
        return None
    return start, end, 206


class LeaseResponse(Response):
    def __init__(self, service, item_id, handle, entry, interval, head):
        self.service, self.item_id, self.handle = service, item_id, handle
        self.start, self.end, status_code = interval
        self.head = head
        headers = {"Content-Type": entry["mime"], "Accept-Ranges": "bytes",
                   "Content-Length": str(self.end - self.start)}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {self.start}-{self.end - 1}/{entry['size']}"
        super().__init__(status_code=status_code, headers=headers)

    async def stream(self, send):
        await asyncio.wait_for(send({"type": "http.response.start", "status": self.status_code,
                                     "headers": self.raw_headers}), C.DOWNLOAD_IDLE_TIMEOUT)
        if not self.head:
            await worker(self.handle.seek, self.start)
            remaining = self.end - self.start
            while remaining:
                chunk = await worker(self.handle.read, min(C.CHUNK_SIZE, remaining))
                if not chunk:
                    raise OSError("Content truncated during response")
                remaining -= len(chunk)
                await asyncio.wait_for(send({"type": "http.response.body", "body": chunk,
                                             "more_body": True}), C.DOWNLOAD_IDLE_TIMEOUT)
        await asyncio.wait_for(send({"type": "http.response.body", "body": b"", "more_body": False}),
                               C.DOWNLOAD_IDLE_TIMEOUT)

    async def __call__(self, scope, receive, send):
        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass
        owner = asyncio.current_task()
        self.service.response_tasks.add(owner)
        stream, listener = None, None
        try:
            stream = asyncio.create_task(self.stream(send))
            listener = asyncio.create_task(disconnected())
            done, _ = await asyncio.wait((stream, listener), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (stream, listener):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (stream, listener) if t is not None), return_exceptions=True)
            await self.service.release(self.item_id, self.handle)
            self.service.response_tasks.discard(owner)


async def content(request):
    service = request.app.state.publisher
    await service.check_storage()
    parsed = content_path(request)
    if parsed is None:
        return not_found(request)
    item_id, path, is_root = parsed
    status, value = await service.admit(item_id, path, session_token(request, item_id))
    if status == "missing":
        return not_found(request)
    if status == "locked":
        # 根路径与 HTML 文档请求显示解锁表单；其他资源不泄露内容。
        if is_root or wants_html(request):
            return unlock_response()
        return Response(status_code=403)
    if status == "redirect":
        return Response(status_code=308, headers={"Location": f"/{item_id}/"})
    handle, entry = value
    interval = byte_range(request.headers.get("range"), entry["size"])
    if interval is None:
        await service.release(item_id, handle)
        return Response(status_code=416, headers={"Content-Range": f"bytes */{entry['size']}", "Accept-Ranges": "bytes"})
    return LeaseResponse(service, item_id, handle, entry, interval, request.method == "HEAD")


async def unlock(request):
    """校验授权码并下发 1 小时会话；未知与不可用统一返回 404。"""
    service = request.app.state.publisher
    await service.check_storage()
    try:
        item_id = str(uuid.UUID(request.path_params["item_id"]))
    except (ValueError, AttributeError):
        return not_found(request)
    client = client_address(request)
    wait = await service.io(service.limiter.blocked, item_id, client)
    if wait:
        return Response(status_code=429, headers={"Retry-After": str(wait)})
    try:
        code = (await read_unlock_form(request)).strip().upper()
    except ServiceError as exc:
        return error_response(exc)
    if not await service.io(service.limiter.submit, item_id, client):
        return Response(status_code=429, headers={"Retry-After": str(C.AUTH_WINDOW)})
    status, value = await service.unlock(item_id, code)
    if status == "missing":
        return not_found(request)
    if status == "denied":
        # 先返回本次失败结果，达到阈值后由下一次请求进入限流分支。
        recorded = await service.io(service.limiter.failure, item_id, client)
        if not recorded:
            return Response(status_code=429, headers={"Retry-After": str(C.AUTH_WINDOW)})
        return unlock_response("访问码不正确", 401)
    row, token = value
    await service.io(service.limiter.clear, item_id, client)
    max_age = max(0, min(C.ACCESS_SESSION_SECONDS, row["expires_at"] - service.clock.now()))
    response = Response(status_code=303, headers={"Location": f"/{item_id}/"})
    response.set_cookie(
        access.session_cookie_name(item_id), token, max_age=max_age,
        path=f"/{item_id}/", secure=True, httponly=True, samesite="lax",
    )
    return response


async def reset_access(request):
    """管理接口：按当前认证版本条件重新生成授权码。"""
    service = authorize(request)
    await service.check_storage()
    query(request, set())
    item_id = canonical_uuid(request.path_params["value"])
    raw = request.headers.get("x-access-revision", "")
    if re.fullmatch(r"[1-9][0-9]{0,8}", raw) is None:
        raise ServiceError("INVALID_ARGUMENT", "Invalid access revision")
    row, code = await service.reset_code(item_id, int(raw))
    return JSONResponse({"id": row["id"], "access_mode": row["access_mode"],
                         "access_revision": row["access_revision"], "access_code": code})


ROUTES = [
    Route("/healthz", health, methods=["GET"]),
    Route("/", landing, methods=["GET", "HEAD"]),
    Route("/api/v1/publishes", publish, methods=["POST"]),
    Route("/api/v1/publishes", listing, methods=["GET"]),
    Route("/api/v1/publishes/{value}", status, methods=["GET"]),
    Route("/api/v1/publishes/{value}", delete, methods=["DELETE"]),
    Route("/api/v1/publishes/{value}/access", reset_access, methods=["POST"]),
    Route("/api/v1/uploads/{value}", status, methods=["GET"]),
    Route("/{item_id}/_unlock", unlock, methods=["POST"]),
    Route("/{path:path}", content, methods=["GET", "HEAD"]),
]
