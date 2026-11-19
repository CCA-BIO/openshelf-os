"""Each robot crash is its own Sentry issue (OPENSHELF-API-D2: two door crashes merged into one).

Crashes with the same exception and stack, on one machine or several, are separate incidents
to handle one by one, so their reports carry a per-incident fingerprint instead of Sentry's
default stack grouping.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
from openshelf_os.robot.models import WorkerError
from openshelf_os.robot.worker import RobotWorker
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

DOOR_FAULT = "Failed to close door: Failed during pre-home approach: Movement failed for motor 31 (status=FAULT)"


class IdleRobot:
    def block_motion_for_safety(self) -> None:
        pass

    def release_motion_safety(self) -> None:
        pass


@pytest.fixture
def sent_events() -> Iterator[list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    # A scoped client: the worker's report scope inherits it, and no global Sentry state leaks.
    with sentry_sdk.new_scope() as scope:
        scope.set_client(
            sentry_sdk.Client(
                dsn="https://key@sentry.invalid/1",
                transport=CapturingTransport,
                default_integrations=False,
            )
        )
        yield events


@pytest.fixture
def worker() -> Iterator[RobotWorker]:
    RobotWorker._instance = None
    yield RobotWorker(IdleRobot())  # type: ignore[arg-type]
    RobotWorker._instance = None


def crash(error_type: str, command_id: str | None) -> WorkerError:
    queued_operation = None if command_id is None else {"command_id": command_id, "command": {"command": "store_item"}}
    return WorkerError(
        error_type=error_type,
        message=DOOR_FAULT,
        context={
            "queued_operation": queued_operation,
            "hardware_fault": {"is_hardware_fault": True, "exception_type": "MotorError", "message": DOOR_FAULT},
        },
    )


def report(worker: RobotWorker, error: WorkerError) -> None:
    try:
        raise RuntimeError(DOOR_FAULT)
    except RuntimeError as exc:
        worker._report_error(error, exc=exc, add_to_queue=False)


def test_each_crashed_command_is_its_own_issue(worker, sent_events) -> None:
    # The two door crashes of OPENSHELF-API-D2, plus the second report of the first one.
    report(worker, crash("HardwareFault", "5fbd08f7-cbc0-49e1-9441-68f7339a8617"))
    report(worker, crash("HardwareFault", "d1c52ce9-98ed-42c0-b06e-98963d6e2a51"))
    report(worker, crash("HardwareFault", "5fbd08f7-cbc0-49e1-9441-68f7339a8617"))

    assert [e.get("fingerprint") for e in sent_events] == [
        ["robot-crash", "5fbd08f7-cbc0-49e1-9441-68f7339a8617"],
        ["robot-crash", "d1c52ce9-98ed-42c0-b06e-98963d6e2a51"],
        ["robot-crash", "5fbd08f7-cbc0-49e1-9441-68f7339a8617"],
    ]
    assert sent_events[1]["tags"]["incident_id"] == "d1c52ce9-98ed-42c0-b06e-98963d6e2a51"


def test_crashes_outside_a_command_are_separate_issues(worker, sent_events) -> None:
    report(worker, crash("GripVerification", None))
    report(worker, crash("GripVerification", None))

    first, second = (e["fingerprint"] for e in sent_events)
    assert first[0] == second[0] == "robot-crash"
    assert first != second


def test_software_errors_keep_default_grouping(worker, sent_events) -> None:
    report(worker, crash("CommandExecutionError", "5fbd08f7-cbc0-49e1-9441-68f7339a8617"))

    assert "fingerprint" not in sent_events[0]
    assert "incident_id" not in sent_events[0].get("tags", {})
