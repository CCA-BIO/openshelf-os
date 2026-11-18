import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select

from openshelf_os_server.database.models import AuditLog, RobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.utils.audit_constants import AUDIT_RETENTION_DAYS, LEGACY_COMMAND_RECORDS_FIELD
from openshelf_os_server.utils.audit_integrity import (
	build_binding_message,
	canonicalize_payload,
	hmac_sha256_hex,
	sha256_hex,
)


async def latest_audit_log(endpoint: str, method: str | None = None) -> AuditLog | None:
	async with AsyncSessionLocal() as db:
		query = select(AuditLog).filter(AuditLog.endpoint == endpoint)
		if method is not None:
			query = query.filter(AuditLog.method == method)
		res = await db.execute(query.order_by(AuditLog.id.desc()).limit(1))
		return res.scalar_one_or_none()


async def admin_headers(unauthenticated_client: AsyncClient) -> dict[str, str]:
	response = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "1234"},
	)
	assert response.status_code == 200
	return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.anyio
async def test_audit_records_mutations_and_failures_but_not_successful_reads(
	unauthenticated_client: AsyncClient,
) -> None:
	headers = await admin_headers(unauthenticated_client)

	first = await unauthenticated_client.post(
		"/api/roles", headers=headers, json={"name": f"policy_{uuid.uuid4().hex}", "permissions": []}
	)
	skipped_read = await unauthenticated_client.get("/api/users", headers=headers)
	skipped_health = await unauthenticated_client.get("/api/health", headers=headers)
	denied_read = await unauthenticated_client.get("/api/users")
	second = await unauthenticated_client.post(
		"/api/roles", headers=headers, json={"name": f"policy_{uuid.uuid4().hex}", "permissions": []}
	)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert first.status_code == 200
	assert skipped_read.status_code == 200
	assert skipped_health.status_code == 200
	assert denied_read.status_code == 401
	assert second.status_code == 200
	assert verify.status_code == 200
	assert verify.json()["ok"] is True

	denied_log = await latest_audit_log("/api/users", "GET")
	assert denied_log is not None and denied_log.response_status == 401
	# The successful read and the health check left no row of their own.
	async with AsyncSessionLocal() as db:
		successful_reads = await db.scalar(
			select(func.count())
			.select_from(AuditLog)
			.where(AuditLog.method == "GET", AuditLog.response_status == 200)
		)
	assert successful_reads == 0


@pytest.mark.anyio
async def test_concurrent_requests_keep_one_unbroken_chain(unauthenticated_client: AsyncClient) -> None:
	# A device answers bursts of parallel requests (startup, kiosk login); each must link to
	# the previous entry, or verify reports a chain break nobody caused.
	headers = await admin_headers(unauthenticated_client)

	responses = await asyncio.gather(
		*(unauthenticated_client.get("/api/users") for _ in range(8))
	)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert all(response.status_code == 401 for response in responses)
	assert verify.json()["ok"] is True
	async with AsyncSessionLocal() as db:
		duplicated = await db.scalar(
			select(func.count()).select_from(
				select(AuditLog.seq_no).group_by(AuditLog.seq_no).having(func.count() > 1).subquery()
			)
		)
	assert duplicated == 0


@pytest.mark.anyio
async def test_retention_prune_keeps_clock_skewed_chain_entry(
	unauthenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
	response = await unauthenticated_client.get("/api/users")
	assert response.status_code == 401

	async with AsyncSessionLocal() as db:
		anchor_seq = await db.scalar(select(func.max(AuditLog.seq_no)))
		assert anchor_seq is not None
		skewed_seq = int(anchor_seq) + 1
		db.add(
			AuditLog(
				timestamp=datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS + 30),
				method="GET",
				endpoint="/api/skewed",
				response_status=200,
				seq_no=skewed_seq,
				prev_hash="0" * 64,
				entry_hash="1" * 64,
				hmac="2" * 64,
				hmac_key_id="test",
			)
		)
		await db.commit()

	# The prune runs once per process-hour; open the gate so this write prunes.
	from openshelf_os_server.middleware import audit as audit_module

	monkeypatch.setattr(audit_module, "_last_prune", 0.0)
	response = await unauthenticated_client.get("/api/users")
	assert response.status_code == 401

	async with AsyncSessionLocal() as db:
		still_present = await db.scalar(
			select(func.count()).select_from(AuditLog).where(AuditLog.seq_no == skewed_seq)
		)
	assert still_present == 1


@pytest.mark.anyio
async def test_sensitive_request_fields_are_redacted(unauthenticated_client: AsyncClient) -> None:
	login_response = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "1234"},
	)
	assert login_response.status_code == 200

	login_log = await latest_audit_log("/api/auth/login/json", "POST")
	assert login_log is not None
	assert login_log.request_body == {"email": "admin@opshelf.com", "pin": "[REDACTED]"}

	headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}
	payload = {
		"name": f"redaction_test_{uuid.uuid4().hex}",
		"permissions": [],
		"password": "secret-password",
		"nested": {"token": "secret-token", "safe": "value"},
		"items": [{"api_key": "secret-key", "hmac_key": "secret-hmac"}],
	}
	role_response = await unauthenticated_client.post("/api/roles", headers=headers, json=payload)
	assert role_response.status_code == 200

	role_log = await latest_audit_log("/api/roles", "POST")
	assert role_log is not None
	assert role_log.request_body == {
		"name": payload["name"],
		"permissions": [],
		"password": "[REDACTED]",
		"nested": {"token": "[REDACTED]", "safe": "value"},
		"items": [{"api_key": "[REDACTED]", "hmac_key": "[REDACTED]"}],
	}
	assert "Bearer" not in str(role_log.request_body)


@pytest.mark.anyio
async def test_unusual_request_bodies_are_recorded_and_stay_readable(
	unauthenticated_client: AsyncClient,
) -> None:
	# The sign-in route is open to anyone. A JSON list body made the audit page holding its
	# row unreadable, and an escaped lone surrogate dropped the row of a change that was made.
	headers = await admin_headers(unauthenticated_client)
	role_name = f"audit_body_test_{uuid.uuid4().hex}"

	list_body = await unauthenticated_client.post("/api/auth/login/json", json=[1, 2])
	surrogate_body = await unauthenticated_client.post(
		"/api/roles",
		headers={**headers, "Content-Type": "application/json"},
		content=('{"name": "%s", "permissions": [], "note": "a\\ud800b"}' % role_name).encode(),
	)
	page = await unauthenticated_client.get("/api/audit/logs", headers=headers)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert list_body.status_code == 422
	assert surrogate_body.status_code == 200
	list_log = await latest_audit_log("/api/auth/login/json", "POST")
	assert list_log is not None and list_log.request_body == [1, 2]
	role_log = await latest_audit_log("/api/roles", "POST")
	assert role_log is not None
	assert role_log.request_body == {"name": role_name, "permissions": [], "note": "a?b"}
	assert page.status_code == 200
	assert [1, 2] in [entry["request_body"] for entry in page.json()["logs"]]
	assert verify.json()["ok"] is True


@pytest.mark.anyio
async def test_request_without_user_agent_keeps_the_chain_verifiable(
	unauthenticated_client: AsyncClient,
) -> None:
	# The entry hash covered a null user agent while the row stored "", so one such request
	# made verify report tampering from that entry on.
	request = unauthenticated_client.build_request("GET", "/api/users")
	del request.headers["user-agent"]

	response = await unauthenticated_client.send(request)
	headers = await admin_headers(unauthenticated_client)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert response.status_code == 401
	log = await latest_audit_log("/api/users", "GET")
	assert log is not None and log.user_agent == ""
	assert verify.json()["ok"] is True

	# An entry an older release wrote that way is intact and verifies as well.
	async with AsyncSessionLocal() as db:
		legacy = (await db.execute(select(AuditLog).filter_by(id=log.id))).scalar_one()
		payload = {
			"timestamp": legacy.timestamp.isoformat(),
			"method": legacy.method,
			"endpoint": legacy.endpoint,
			"request_body": legacy.request_body,
			"response_status": legacy.response_status,
			"response_body": legacy.response_body,
			LEGACY_COMMAND_RECORDS_FIELD: legacy.command_records,
			"error_message": legacy.error_message,
			"ip_address": legacy.ip_address,
			"user_agent": None,
			"request_duration_ms": legacy.request_duration_ms,
			"user_id": legacy.user_id,
			"seq_no": legacy.seq_no,
			"prev_hash": legacy.prev_hash,
		}
		legacy.entry_hash = sha256_hex(canonicalize_payload(payload))
		legacy.hmac = hmac_sha256_hex(
			build_binding_message(int(legacy.seq_no), str(legacy.prev_hash), legacy.entry_hash)
		)
		seq_no = int(legacy.seq_no)
		await db.commit()

	legacy_verify = await unauthenticated_client.get(
		f"/api/audit/verify?from_seq={seq_no}&to_seq={seq_no}", headers=headers
	)
	assert legacy_verify.json() == {"ok": True, "checked": 1, "first_error_seq": None, "message": None}


@pytest.mark.anyio
async def test_response_bodies_are_captured_and_redacted(unauthenticated_client: AsyncClient) -> None:
	# The response of a mutation or a failure is the only record of what the machine
	# actually did (the cell it chose, the reason it refused), so it belongs in the row —
	# minus the tokens a successful sign-in hands out.
	login_response = await unauthenticated_client.post(
		"/api/auth/login/json", json={"email": "admin@opshelf.com", "pin": "1234"}
	)
	assert login_response.status_code == 200

	login_log = await latest_audit_log("/api/auth/login/json", "POST")
	assert login_log is not None
	assert login_log.response_body is not None
	assert login_log.response_body["access_token"] == "[REDACTED]"
	assert login_log.response_body.get("refresh_token") in (None, "[REDACTED]")

	denied = await unauthenticated_client.get("/api/users")
	assert denied.status_code == 401
	denied_log = await latest_audit_log("/api/users", "GET")
	assert denied_log is not None and denied_log.response_status == 401
	assert denied_log.response_body is not None
	assert "detail" in denied_log.response_body

	headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)
	assert verify.json()["ok"] is True


@pytest.mark.anyio
async def test_over_cap_response_streams_to_the_client_in_full(seeded_db) -> None:
	# An audited JSON response larger than the capture cap is not stored, but the client
	# must still receive every byte: the replay iterator once iterated itself after the
	# cap, truncating the body at 64KB and then crashing the ASGI send. A failed read is
	# the audited shape that can stream this much back.
	from httpx import ASGITransport
	from starlette.applications import Starlette
	from starlette.responses import StreamingResponse
	from starlette.routing import Route

	from openshelf_os_server.middleware.audit import AuditMiddleware

	chunks = [("x" * 1024).encode() for _ in range(200)]  # 200KB, well past the 64KB cap

	async def oversized(request) -> StreamingResponse:
		return StreamingResponse(iter(chunks), media_type="application/json", status_code=400)

	small_app = Starlette(routes=[Route("/api/oversized", oversized, methods=["GET"])])
	small_app.add_middleware(AuditMiddleware)

	async with AsyncClient(transport=ASGITransport(app=small_app), base_url="http://test") as ac:
		response = await ac.get("/api/oversized")

	assert response.status_code == 400
	assert response.content == b"".join(chunks)

	row = await latest_audit_log("/api/oversized", "GET")
	assert row is not None
	assert row.response_status == 400
	assert row.response_body is None


@pytest.mark.anyio
async def test_admitted_commands_land_in_the_requesting_row(unauthenticated_client: AsyncClient) -> None:
	# A workflow store hides several internal motions behind one POST; the row must name
	# each motion the request actually enqueued, or the trail proves intent only.
	from openshelf_os_server.core.command_lifecycle import admit
	from openshelf_os_server.utils.audit_constants import command_records_context

	headers = await admin_headers(unauthenticated_client)
	token = command_records_context.set([])
	try:
		row, replayed = await admit(
			{"command": "move_to", "x": 1.0, "y": 2.0, "z": None},
			request_id=None,
			correlation_id=None,
		)
		assert replayed is False
		records = command_records_context.get()
		assert records == [
			{
				"command_id": row.command_id,
				"visibility": "public",
				"payload": {"command": "move_to", "x": 1.0, "y": 2.0, "z": None},
			}
		]
	finally:
		command_records_context.reset(token)
		async with AsyncSessionLocal() as db:
			await db.execute(delete(RobotCommand).where(RobotCommand.command_id == row.command_id))
			await db.commit()

	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)
	assert verify.json()["ok"] is True


@pytest.mark.anyio
async def test_mock_mode_enqueued_commands_land_in_the_requesting_row() -> None:
	# Mock mode bypasses production admission, so the runtime itself must feed the
	# requesting request's audit row or every mock motion vanishes from the trail.
	from openshelf_os_server.core.mock_robot import MockRobotRuntime
	from openshelf_os_server.database.models import MockRobotCommand
	from openshelf_os_server.utils.audit_constants import command_records_context

	runtime = MockRobotRuntime()
	token = command_records_context.set([])
	try:
		payload = {"command": "move_to", "x": 1.0, "y": 2.0, "z": None}
		row, replayed = await runtime.enqueue(payload, request_id=None, correlation_id=None)
		assert replayed is False
		assert command_records_context.get() == [
			{
				"command_id": row.command_id,
				"visibility": "public",
				"payload": payload,
			}
		]
	finally:
		command_records_context.reset(token)
		async with AsyncSessionLocal() as db:
			await db.execute(delete(MockRobotCommand).where(MockRobotCommand.command_id == row.command_id))
			await db.commit()


def test_command_records_skip_when_no_request_is_in_flight() -> None:
	# The recorder must never touch the shared default list, or rows from different
	# requests would bleed into each other. An empty context is any non-request caller.
	import contextvars

	from openshelf_os_server.utils.audit_constants import (
		command_records_context,
		record_enqueued_command,
	)

	def record_and_check() -> None:
		record_enqueued_command({"command": "home"}, "cmd-x", "public")
		assert command_records_context.get(None) is None

	contextvars.Context().run(record_and_check)


def test_command_records_truncate_at_the_limit() -> None:
	from openshelf_os_server.utils.audit_constants import (
		COMMAND_RECORD_LIMIT,
		command_records_context,
		record_enqueued_command,
	)

	token = command_records_context.set([])
	try:
		for index in range(COMMAND_RECORD_LIMIT):
			record_enqueued_command({"command": "move_to", "x": index}, f"cmd-{index}", "internal")
		record_enqueued_command({"command": "move_to", "x": 999}, "cmd-overflow", "internal")
		record_enqueued_command({"command": "move_to", "x": 1000}, "cmd-overflow-2", "internal")

		records = command_records_context.get()
		assert len(records) == COMMAND_RECORD_LIMIT + 1
		assert records[-1] == {"command": "records_truncated"}
		assert all(record["payload"]["x"] < 999 for record in records[:-1])
	finally:
		command_records_context.reset(token)
