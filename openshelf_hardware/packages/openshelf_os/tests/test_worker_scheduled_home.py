"""Extended runs drift out of position: the worker owes a full home every HOME_EVERY_CYCLES
store/retrieve commands and after IDLE_HOME_AFTER_S idle following one."""
from __future__ import annotations

import asyncio
import json

import pytest

from openshelf_os.robot import worker as worker_module
from openshelf_os.robot.commands import StopCommand, StoreItemCommand
from openshelf_os.robot.status import RobotCommandError, RobotCommandSuccessful, ScaleStatus
from openshelf_os.robot.worker import RobotWorker


class RecordingRobot:
    def __init__(self, *, home_fails: bool = False) -> None:
        self.commands: list[str] = []
        self.home_fails = home_fails
        self.tray_held = False

    async def read_tray_held(self) -> bool:
        return self.tray_held

    def block_motion_for_safety(self) -> None:
        pass

    def release_motion_safety(self) -> None:
        pass

    async def get_status(self) -> None:
        return None

    async def handle_command(self, command):
        self.commands.append(command.command)
        if command.command == "home" and self.home_fails:
            return RobotCommandError(command="home", error="The robot could not home.")
        return RobotCommandSuccessful(command=command.command)


def store() -> StoreItemCommand:
    return StoreItemCommand(item={"barcode": "880000000001"})


@pytest.fixture
async def run_worker():
    started: list[tuple[RobotWorker, asyncio.Task]] = []

    async def start(robot: RecordingRobot, **kwargs) -> RobotWorker:
        RobotWorker._instance = None
        worker = RobotWorker(robot, **kwargs)  # type: ignore[arg-type]
        worker._running = True
        started.append((worker, asyncio.create_task(worker.process_commands())))
        return worker

    yield start
    for worker, task in started:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    RobotWorker._instance = None


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while predicate() is False:
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def test_due_home_runs_ahead_of_the_next_queued_cycle_but_not_a_stop(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 2)
    robot = RecordingRobot()
    worker = await run_worker(robot)
    for command in (store(), store(), StopCommand(), store()):
        await worker.enqueue_command(command)

    await wait_for(lambda: len(robot.commands) == 5)

    assert robot.commands == ["store_item", "store_item", "stop", "home", "store_item"]
    assert worker._cycles_since_home == 1


async def test_a_cycle_arriving_while_a_due_home_waits_does_not_jump_it(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 1)
    robot = RecordingRobot()
    worker = await run_worker(robot)
    worker._latest_scale_status = ScaleStatus(occupied=True)
    await worker.enqueue_command(store())
    await wait_for(lambda: robot.commands == ["store_item"] and worker._current_task is None)
    await asyncio.sleep(0.5)

    await worker.enqueue_command(store())
    await wait_for(lambda: len(robot.commands) == 3)

    assert robot.commands == ["store_item", "home", "store_item"]


async def test_a_held_cycle_stays_held_while_the_due_home_is_safety_blocked(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 1)
    robot = RecordingRobot()
    worker = await run_worker(robot)
    await worker.enqueue_command(store())
    await wait_for(lambda: robot.commands == ["store_item"] and worker._current_task is None)
    worker._cabinet_doors = {"open": True}
    await worker._evaluate_safety_guard()
    assert worker.is_safety_blocking() is True

    await worker.enqueue_command(store())
    await asyncio.sleep(1.5)

    # Released while blocked, the cycle would move an unhomed robot as soon as the block clears.
    assert robot.commands == ["store_item"]

    worker._cabinet_doors = {"open": False}
    assert (await worker.acknowledge_safety_guard())["accepted"] is True
    await wait_for(lambda: len(robot.commands) == 3)

    assert robot.commands == ["store_item", "home", "store_item"]


async def test_idle_home_waits_for_manual_motion_and_fires_once(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "IDLE_HOME_AFTER_S", 0.2)
    manual_motion = True
    robot = RecordingRobot()
    worker = await run_worker(robot, external_motion_active=lambda: manual_motion)
    await worker.enqueue_command(store())

    await asyncio.sleep(1.0)
    assert robot.commands == ["store_item"]

    manual_motion = False
    await wait_for(lambda: robot.commands == ["store_item", "home"])
    await asyncio.sleep(1.0)

    assert robot.commands == ["store_item", "home"]
    assert worker._cycles_since_home == 0


async def test_a_failed_scheduled_home_owes_a_home_instead_of_retrying(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 1)
    robot = RecordingRobot(home_fails=True)
    worker = await run_worker(robot)
    events = await worker.subscribe_events()
    await worker.enqueue_command(store())
    await worker.enqueue_command(store())

    await wait_for(lambda: worker.needs_home() is True and worker._queued_commands == [])
    await asyncio.sleep(0.5)

    # The store queued behind the failed home must not move an unhomed robot.
    assert robot.commands == ["store_item", "home"]
    failures = [json.loads(events.get_nowait()) for _ in range(events.qsize())]
    assert [
        (e["error"], e["code"])
        for e in failures
        if e.get("message") == "failed" and e.get("command") == "store_item"
    ] == [(worker_module.SCHEDULED_HOME_FAILED_REASON, "home_required")]
    assert worker.needs_home_reason() == worker_module.SCHEDULED_HOME_FAILED_REASON


async def test_a_due_home_does_not_release_a_latched_estop_once_needs_home_is_cleared(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 1)
    robot = RecordingRobot()
    worker = await run_worker(robot)
    worker._cycles_since_home = 1
    worker._is_emergency_stopped = True
    assert worker.needs_reset() is False

    await asyncio.sleep(1.5)

    assert robot.commands == []
    assert worker._is_emergency_stopped is True


async def test_a_due_home_does_not_run_over_a_tray_the_cached_flag_missed(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 1)
    robot = RecordingRobot()
    worker = await run_worker(robot)
    worker._cycles_since_home = 1
    robot.tray_held = True
    assert worker.tray_on_tool() is False

    await asyncio.sleep(1.5)

    assert robot.commands == []
    assert worker.tray_on_tool() is True


async def test_an_idle_home_waits_for_an_open_cabinet_even_when_the_cabinet_is_bypassed(monkeypatch, run_worker):
    monkeypatch.setattr(worker_module, "HOME_EVERY_CYCLES", 1)
    robot = RecordingRobot()
    worker = await run_worker(robot)
    worker._cycles_since_home = 1
    worker._component_bypasses["cabinet_doors"] = True
    worker._cabinet_doors = {"open": True}
    assert worker.is_safety_blocking() is False

    await asyncio.sleep(1.5)
    assert robot.commands == []

    worker._cabinet_doors = {"open": False}
    await wait_for(lambda: robot.commands == ["home"])
