"""Audit logging middleware."""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..database.session import AsyncSession, AsyncSessionLocal
from ..database.models import AuditLog
from sqlalchemy import delete, func, select
from ..utils.audit_integrity import (
    canonicalize_payload,
    sha256_hex,
    hmac_sha256_hex,
    build_binding_message,
    get_hmac_key_id,
)
from ..utils.audit_constants import (
    AUDIT_RETENTION_DAYS,
    LEGACY_COMMAND_RECORDS_FIELD,
    command_records_context,
)
from ..utils.cors_policy import response_allow_origin, resolve_allowed_origins
from ..routers.auth_deps import get_current_user_optional
from ..errors import capture_unhandled_exception, client_detail, error_detail, internal_error_detail, request_correlation_id

logger = logging.getLogger(__name__)

IGNORED_METHODS = frozenset(["OPTIONS"])
ALWAYS_EXCLUDED_ENDPOINTS = frozenset([
    "/api/health",
    "/api/health/cameras",
    "/api/robot/debug/motors/probe",
    "/api/calibration/laser/stream",
])
SUCCESSFUL_READ_METHODS = frozenset(["GET", "HEAD"])
# Reads that had to move to POST so the barcode can travel in the body; they stay unaudited
# like their GET counterparts, and only failures are recorded.
SUCCESSFUL_READ_POST_ENDPOINTS = frozenset([
    "/api/storage/items/lookup",
    "/api/system/config/validate",
	"/api/users/login-barcode/resolve",
])
# Auditing is for what changed and what failed: every successful mutation is recorded,
# every failed request is recorded (a refused read says as much about access control as a
# refused write), and a successful read is never recorded. A read-only poll that belongs
# in the trail anyway must move behind a POST or join SUCCESSFUL_READ_POST_ENDPOINTS.
SENSITIVE_FIELD_NAMES = frozenset([
    "pin",
	"identifier",
	"scan_token",
	"login_barcode",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "secret",
    "api_key",
    "hmac_key",
])
REDACTED_VALUE = "[REDACTED]"


def should_skip_before_request(request: Request) -> bool:
    if request.method in IGNORED_METHODS:
        return True
    return request.url.path in ALWAYS_EXCLUDED_ENDPOINTS


def should_skip_after_response(request: Request, status_code: int) -> bool:
    if status_code >= 400:
        return False
    if request.url.path in SUCCESSFUL_READ_POST_ENDPOINTS:
        return True
    return request.method in SUCCESSFUL_READ_METHODS


def _encodable_text(value: str) -> str:
    # JSON admits an escaped lone surrogate ("\ud800") that no UTF-8 encoder accepts, so
    # hashing the entry and serving it back would both fail and the request would go
    # unrecorded. Keep the entry and mark the character instead.
    return value.encode("utf-8", "replace").decode("utf-8")


def redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _encodable_text(key): REDACTED_VALUE
            if key.lower() in SENSITIVE_FIELD_NAMES
            else redact_sensitive_values(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    if isinstance(value, str):
        return _encodable_text(value)
    return value


# The response of a mutation is the only place that records what the machine actually did
# (the cell the workflow chose, the weight it measured, the reason a command was refused),
# so every audited row captures it. A response larger than the cap, or a stream a client
# is still reading, is left uncaptured rather than buffered.
RESPONSE_CAPTURE_LIMIT_BYTES = 64 * 1024
CAPTURABLE_CONTENT_TYPE = "application/json"


async def capture_response_body(response: Response) -> tuple[Optional[Any], Response]:
    """Drain a JSON response up to a size cap and return (redacted body, replayable response)."""
    if isinstance(response, JSONResponse):
        # The unhandled-500 path builds the response here; its body is already in memory.
        try:
            return redact_sensitive_values(json.loads(response.body)), response
        except Exception:
            return None, response

    if CAPTURABLE_CONTENT_TYPE not in response.headers.get("content-type", ""):
        return None, response

    chunks: list[Any] = []
    total = 0
    overflow = False
    while total < RESPONSE_CAPTURE_LIMIT_BYTES:
        try:
            chunk = await response.body_iterator.__anext__()
        except StopAsyncIteration:
            break
        chunks.append(chunk)
        total += len(chunk)
    else:
        overflow = True

    original_iterator = response.body_iterator

    async def replay() -> Any:
        for chunk in chunks:
            yield chunk
        if overflow:
            # response.body_iterator now points at replay() itself; drain the source we
            # captured before the reassignment.
            async for chunk in original_iterator:
                yield chunk

    response.body_iterator = replay()
    if overflow or any(isinstance(chunk, dict) for chunk in chunks):
        return None, response

    try:
        return redact_sensitive_values(json.loads(b"".join(bytes(chunk) for chunk in chunks))), response
    except Exception:
        return None, response


# Retention only moves forward an hour at a time, so the prune runs once per process-hour
# instead of as two DELETEs on every audited request.
AUDIT_PRUNE_INTERVAL_S = 3600.0
_last_prune: Optional[float] = 0.0


# Each entry links to the one before it, so reading the chain head and committing the new
# entry must not interleave with another request's append, or both take the same seq_no
# and prev_hash and verify reports a chain break. The api is one process on one loop; the
# lock is rebuilt per loop because an asyncio.Lock binds to the loop that first waits on it.
_chain_lock: Optional[asyncio.Lock] = None
_chain_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _audit_chain_lock() -> asyncio.Lock:
    global _chain_lock, _chain_lock_loop

    loop = asyncio.get_running_loop()
    if _chain_lock is None or _chain_lock_loop is not loop:
        _chain_lock = asyncio.Lock()
        _chain_lock_loop = loop
    return _chain_lock


class AuditMiddleware(BaseHTTPMiddleware):
	"""Middleware to log all API requests and responses."""

	def __init__(self, app: ASGIApp, sentry_enabled: bool = False) -> None:
		super().__init__(app)
		self.sentry_enabled = sentry_enabled

	async def dispatch(self, request: Request, call_next: Callable) -> Response:
		"""Process and log each request."""
		if should_skip_before_request(request):
			return await call_next(request)

		command_records_context.set([])
		
		start_time = time.time()
		
		request_body = None
		if request.method in ["POST", "PUT", "PATCH", "DELETE"]:
			try:
				body = await request.body()
				if body:
					request_body = redact_sensitive_values(json.loads(body))
				
				async def receive() -> dict[str, Any]:
					return {"type": "http.request", "body": body}
				
				request._receive = receive
			except Exception:
				pass
		
		response_body = None
		status_code = 500
		error_message = None
		
		try:
			response: Response = await call_next(request)
			status_code = response.status_code

		except Exception as e:
			error_message = str(e)
			correlation_id = request_correlation_id(request)
			logger.warning(
				f"500 unhandled {request.method} {request.url.path} (correlation_id={correlation_id})",
				exc_info=e,
			)
			capture_unhandled_exception(e, request.url.path, request.method, correlation_id, enabled=self.sentry_enabled)
			code, message = internal_error_detail(e)
			detail = client_detail(request, error_detail(message, code))
			response = JSONResponse(status_code=500, content={"detail": detail})
			# This response never travels back through the CORS middleware, so it carries
			# the origin header itself, under the same policy as every other response.
			allow_origin = response_allow_origin(
				resolve_allowed_origins(), request.headers.get("origin")
			)
			if allow_origin is not None:
				response.headers["Access-Control-Allow-Origin"] = allow_origin

		duration_ms = int((time.time() - start_time) * 1000)

		if should_skip_after_response(request, status_code):
			return response

		try:
			response_body, response = await capture_response_body(response)
		except Exception as exc:
			response_body = None
			logger.debug(f"Response body not captured for {request.method} {request.url.path}", exc_info=exc)

		try:
			await self._log_request(
				request=request,
				request_body=request_body,
				response_status=status_code,
				response_body=response_body,
				error_message=error_message or "",
				duration_ms=duration_ms
			)
		except Exception as exc:
			logger.error(
				f"Audit entry not written for {request.method} {request.url.path}",
				exc_info=exc,
			)

		return response

	async def _log_request(
		self,
		request: Request,
		request_body: Optional[Any],
		response_status: int,
		response_body: Optional[Dict[str, Any]],
		error_message: Optional[str],
		duration_ms: int
	) -> None:
		"""Log request to database."""
		async with _audit_chain_lock(), AsyncSessionLocal() as db:
			try:
				token = None
				auth_header = request.headers.get("Authorization")
				if auth_header and auth_header.startswith("Bearer "):
					token = auth_header.split(" ")[1]
				
				user = await get_current_user_optional(token=token, db=db)

				now = datetime.now(timezone.utc)
				authenticated_user_id = getattr(
					request.state, "authenticated_user_id", None
				)
				user_id = authenticated_user_id if authenticated_user_id is not None else (user.id if user else None)
				method = request.method
				endpoint = str(request.url.path)
				command_records = command_records_context.get()
				# Hash exactly what the row stores: verify recomputes the entry hash from the
				# stored columns, and these two are stored as "" when the request has none.
				ip_address = request.client.host if request.client else ""
				user_agent = request.headers.get("user-agent") or ""
				correlation_id = request.headers.get("X-Correlation-ID")

				last_seq_no = None
				last_entry_hash = None
				result = await db.execute(
					select(AuditLog.seq_no, AuditLog.entry_hash, AuditLog.id)
					.where(AuditLog.entry_hash.isnot(None))
					.order_by(AuditLog.seq_no.desc(), AuditLog.id.desc())
					.limit(1)
				)
				row = result.first()
				if row is not None and row[0] is not None and row[1] is not None:
					last_seq_no = int(row[0])
					last_entry_hash = str(row[1])
				else:
					max_seq = await db.scalar(select(func.max(AuditLog.seq_no)))
					last_seq_no = int(max_seq) if max_seq is not None else 0
					last_entry_hash = None

				seq_no = (last_seq_no or 0) + 1
				prev_hash = last_entry_hash if last_entry_hash else ("0" * 64)

				payload = {
					"timestamp": now.isoformat(),
					"method": method,
					"endpoint": endpoint,
					"request_body": request_body,
					"response_status": response_status,
					"response_body": response_body,
					LEGACY_COMMAND_RECORDS_FIELD: command_records,
					"error_message": error_message,
					"ip_address": ip_address,
					"user_agent": user_agent,
					"request_duration_ms": duration_ms,
					"user_id": user_id,
					"seq_no": seq_no,
					"prev_hash": prev_hash,
				}
				if correlation_id is not None:
					payload["correlation_id"] = correlation_id
				payload_bytes = canonicalize_payload(payload)
				entry_hash = sha256_hex(payload_bytes)
				binding = build_binding_message(seq_no, prev_hash, entry_hash)
				hmac_hex = hmac_sha256_hex(binding)
				key_id = get_hmac_key_id()

				audit_log = AuditLog(
					user_id=user_id,  # type: ignore[arg-type]
					timestamp=now,
					method=method,
					endpoint=endpoint,
					request_body=request_body,
					response_status=response_status,
					response_body=response_body,
					command_records=command_records,
					error_message=error_message or "",
					ip_address=ip_address,
					user_agent=user_agent,
					request_duration_ms=duration_ms,
					correlation_id=correlation_id,
					seq_no=seq_no,
					prev_hash=prev_hash,
					entry_hash=entry_hash,
					hmac=hmac_hex,
					hmac_key_id=key_id,
				)

				global _last_prune
				if now.timestamp() - _last_prune >= AUDIT_PRUNE_INTERVAL_S:
					await self._prune_retention(db, seq_no, now)
					_last_prune = now.timestamp()
				db.add(audit_log)
				await db.commit()
			except Exception as exc:
				logger.error(
					f"Audit entry not written for {request.method} {request.url.path}",
					exc_info=exc,
				)

	@staticmethod
	async def _prune_retention(db: AsyncSession, next_seq: int, now: datetime) -> None:
		"""Drop entries older than the retention window, under the chain lock."""
		try:
			cutoff = now - timedelta(days=AUDIT_RETENTION_DAYS)
			oldest_kept_seq = await db.scalar(
				select(func.min(AuditLog.seq_no)).where(
					AuditLog.seq_no.isnot(None),
					AuditLog.timestamp >= cutoff,
				)
			)
			prune_boundary = int(oldest_kept_seq) if oldest_kept_seq is not None else next_seq
			await db.execute(
				delete(AuditLog).where(
					AuditLog.seq_no.isnot(None),
					AuditLog.seq_no < prune_boundary,
				)
			)
			await db.execute(
				delete(AuditLog).where(
					AuditLog.seq_no.is_(None),
					AuditLog.timestamp < cutoff,
				)
			)
			await db.commit()
		except Exception as exc:
			logger.error("Audit retention prune failed", exc_info=exc)
