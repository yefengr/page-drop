import asyncio
import hmac
import logging
import re
import unicodedata
from urllib.parse import unquote_to_bytes
import uuid

from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import config as C
from .archive import InvalidArtifact, validate_path
from .config import ServiceError
from .pages import LANDING, NOT_FOUND
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
    args = query(request, {"ttl", "title", "label"})
    match = re.fullmatch(r"([0-9]+)([mhd])", args.get("ttl", ""))
    if match is None or len(match[1]) > 10:
        raise ServiceError("INVALID_ARGUMENT", "Invalid TTL")
    ttl = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    title = args.get("title", "")
    label = args.get("label", "").strip()
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
    return key, ttl, title, digest, length, label


async def publish(request):
    service = authorize(request)
    await service.check_storage()
    row, fresh = await service.publish(request, *upload_parameters(request))
    result = dict(row, url=f"{public_base(service, request)}/{row['id']}/")
    fields = ("id", "url", "expires_at", "created_at", "bytes", "file_count")
    return JSONResponse({key: result[key] for key in fields}, status_code=201 if fresh else 200)


async def listing(request):
    service = authorize(request)
    await service.check_storage()
    args = query(request, {"all"})
    if "all" in args and args["all"] != "1":
        raise ServiceError("INVALID_ARGUMENT", "Invalid all parameter")
    result = []
    for row in await service.io(service.repo.list):
        async with service.locks[row["id"]]:
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
    async with service.locks[value]:
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
            return item_id, None
        if decoded[1:] == [""]:
            return item_id, "index.html"
        return item_id, validate_path("/".join(decoded[1:]))
    except (UnicodeError, ValueError, InvalidArtifact):
        return None


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
    item_id, path = parsed
    admitted = await service.admit(item_id, path)
    if admitted is None:
        return not_found(request)
    if path is None:
        return Response(status_code=308, headers={"Location": f"/{item_id}/"})
    handle, entry = admitted
    interval = byte_range(request.headers.get("range"), entry["size"])
    if interval is None:
        await service.release(item_id, handle)
        return Response(status_code=416, headers={"Content-Range": f"bytes */{entry['size']}", "Accept-Ranges": "bytes"})
    return LeaseResponse(service, item_id, handle, entry, interval, request.method == "HEAD")


ROUTES = [
    Route("/healthz", health, methods=["GET"]),
    Route("/", landing, methods=["GET", "HEAD"]),
    Route("/api/v1/publishes", publish, methods=["POST"]),
    Route("/api/v1/publishes", listing, methods=["GET"]),
    Route("/api/v1/publishes/{value}", status, methods=["GET"]),
    Route("/api/v1/publishes/{value}", delete, methods=["DELETE"]),
    Route("/api/v1/uploads/{value}", status, methods=["GET"]),
    Route("/{path:path}", content, methods=["GET", "HEAD"]),
]
