from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _stopped_worker_with_open_doors():
	worker = MagicMock()
	worker._current_command = None
	worker._demo_cycling = False
	worker._safety_motion_blocked = True
	worker._awaiting_safety_ack = True
	worker._safety_active_sources = ["cabinet_doors"]
	worker.release_gripper = AsyncMock()
	worker.grip = AsyncMock()
	return worker


async def _post(client: AsyncClient, worker, state: str):
	with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=worker):
		with patch(
			"openshelf_os_server.routers.robot._ensure_laser_auto_calibration_not_running",
			AsyncMock(),
		):
			return await client.post(f"/api/robot/gripper?state={state}")


@pytest.mark.anyio
async def test_release_opens_the_gripper_while_the_doors_hold_a_safety_interruption(
	client: AsyncClient,
) -> None:
	# The operator supports the tray through the open doors while the fingers open.
	worker = _stopped_worker_with_open_doors()

	response = await _post(client, worker, "release")

	assert response.status_code == 200
	worker.release_gripper.assert_awaited_once()


@pytest.mark.anyio
async def test_grip_stays_blocked_during_a_safety_interruption(client: AsyncClient) -> None:
	worker = _stopped_worker_with_open_doors()

	response = await _post(client, worker, "grip")

	assert response.status_code == 409
	assert response.json()["detail"]["reason"] == "safety_interruption"
	worker.grip.assert_not_awaited()


@pytest.mark.anyio
async def test_release_stays_blocked_while_another_safety_source_is_active(client: AsyncClient) -> None:
	# A blocked light curtain can mean a hand in the IO box under a tray held there.
	worker = _stopped_worker_with_open_doors()
	worker._safety_active_sources = ["cabinet_doors", "light_curtain"]

	response = await _post(client, worker, "release")

	assert response.status_code == 409
	assert response.json()["detail"]["reason"] == "safety_interruption"
	worker.release_gripper.assert_not_awaited()


@pytest.mark.anyio
async def test_release_is_refused_while_a_command_runs(client: AsyncClient) -> None:
	worker = _stopped_worker_with_open_doors()
	worker._current_command = MagicMock()

	response = await _post(client, worker, "release")

	assert response.status_code == 409
	assert response.json()["detail"]["reason"] == "robot_command_in_progress"
	worker.release_gripper.assert_not_awaited()
