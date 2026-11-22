"""A closed /api/robot/status stream must drop its worker and init-progress subscriptions.

lb4a-9 OOM-killed the API after a 12 h cycle test: on disconnect sse-starlette cancels its
anyio task group, which re-cancels every suspending await in the generator's `finally`, so
cleanup that awaited the waiter tasks first never reached the unsubscribes. Each dead queue
then held every later status payload (~50 KB each) for the life of the process.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from openshelf_os_server.core.init_progress import get_init_tracker
from openshelf_os_server.main import app
from openshelf_os_server.routers import robot as robot_router

STATUS = json.dumps({"robot_status": {"storage": {}}, "running": True})


class SubscribingWorker:
	def __init__(self) -> None:
		self.subscribers: list[asyncio.Queue[str]] = []

	async def subscribe_status(self) -> tuple[asyncio.Queue[str], str]:
		queue: asyncio.Queue[str] = asyncio.Queue()
		self.subscribers.append(queue)
		return queue, STATUS

	async def unsubscribe_status(self, queue: asyncio.Queue[str]) -> None:
		self.subscribers.remove(queue)

	async def get_status_snapshot(self, force_refresh: bool = False) -> str:
		return STATUS


@pytest.mark.anyio
async def test_disconnected_status_stream_unsubscribes(
	seeded_db: AsyncGenerator[None, None], auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
	worker = SubscribingWorker()

	async def get_worker() -> SubscribingWorker:
		return worker

	async def no_occupancy() -> set[tuple[str, int, int]]:
		return set()

	monkeypatch.setattr(robot_router, "get_robot_worker", get_worker)
	monkeypatch.setattr(robot_router, "get_occupied_cells_for_overlay", no_occupancy)
	tracker = get_init_tracker()
	tracker_baseline = len(tracker._subscribers)

	disconnected = asyncio.Event()
	sent: list[dict[str, Any]] = []

	async def receive() -> dict[str, Any]:
		await disconnected.wait()
		return {"type": "http.disconnect"}

	async def send(message: dict[str, Any]) -> None:
		sent.append(message)

	scope = {
		"type": "http",
		"asgi": {"version": "3.0"},
		"http_version": "1.1",
		"method": "GET",
		"headers": [(key.lower().encode(), value.encode()) for key, value in auth_headers.items()],
		"scheme": "http",
		"path": "/api/robot/status",
		"raw_path": b"/api/robot/status",
		"query_string": b"",
		"server": ("testserver", 80),
		"client": ("testclient", 50000),
		"root_path": "",
	}
	task = asyncio.create_task(app(scope, receive, send))
	loop = asyncio.get_running_loop()
	deadline = loop.time() + 5
	while not any(b'"robot_status"' in message.get("body", b"") for message in sent):
		assert loop.time() < deadline, "status frame never arrived"
		await asyncio.sleep(0.01)
	# The generator now waits on its status queue, where a real client disconnect lands.
	await asyncio.sleep(0.05)
	assert len(worker.subscribers) == 1

	disconnected.set()
	await asyncio.wait_for(task, timeout=5)

	assert worker.subscribers == []
	assert len(tracker._subscribers) == tracker_baseline
