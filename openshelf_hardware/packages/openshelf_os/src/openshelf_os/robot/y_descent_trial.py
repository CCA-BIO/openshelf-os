"""One Y descent-current trial, for finding the lowest peak current that still lowers the
carriage and its payload under control (development only; `scripts/y_descent_sweep.py` runs
a sweep of these against a machine).

A trial climbs to `start_mm`, then descends to `end_mm` at the trial current behind a tight
following-error trip, tracing the drive's live following error on both legs. The drive holds the
falling load hardest while it decelerates at the bottom of the descent, so the trace peaks there
first as the current drops.

`pause_at_mm` stops the descent part-way through the same safety-guard pause a light curtain
break causes: the drive stops, holds the load energised at the descent current for `pause_s`,
then the move resumes. That stop brakes harder than the end of a move.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from hardware.exceptions import MotorError, MotorTimeoutError
from hardware.motor import FollowingErrorSample

logger = logging.getLogger(__name__)

# The brake sets within Pr4.20 (250 ms) of the park; the sag reading waits a little longer.
SETTLE_AFTER_PARK_S = 0.5
# Motor.stop() returns once the stop is written; the drive takes up to Pr5.10 (100 ms) to halt.
SETTLE_AFTER_PAUSE_STOP_S = 0.3

LegOutcome = Literal["completed", "drive_alarm", "stalled", "timeout", "error"]


@dataclass
class TrialLeg:
    peak_current: int | None
    start_mm: float | None
    target_mm: float
    outcome: LegOutcome = "error"
    alarm_code: int | None = None
    duration_s: float | None = None
    end_mm: float | None = None
    settled_mm: float | None = None
    max_following_error_mm: float | None = None
    max_following_error_at_mm: float | None = None
    trace_failed: bool = False
    paused_at_mm: float | None = None
    hold_start_mm: float | None = None
    hold_end_mm: float | None = None
    samples: list[FollowingErrorSample] = field(default_factory=list)


@dataclass
class TrialResult:
    approach: TrialLeg
    descent: TrialLeg | None
    following_error_trip_mm: float
    # Pr0.05 as written, in the drive's own unit; the raw trace compares against this directly.
    following_error_trip_counts: int
    configured_peak_current: int
    holding_current_percent: int | None
    standby_current_percent: int | None

    @property
    def completed(self) -> bool:
        return self.approach.outcome == "completed" and self.descent is not None and self.descent.outcome == "completed"

    @property
    def outcome(self) -> LegOutcome:
        if self.approach.outcome != "completed" or self.descent is None:
            return self.approach.outcome
        return self.descent.outcome


async def run_descent_trial(
    motor: Any,
    *,
    start_mm: float,
    end_mm: float,
    descent_peak_current: int,
    following_error_trip_mm: float,
    approach_peak_current: int | None = None,
    rpm: int | None = None,
    pause_at_mm: float | None = None,
    pause_s: float = 5.0,
) -> TrialResult:
    """Run one trial; a leg that fails is reported in the result, not raised.

    `approach_peak_current=None` climbs at the configured current and trip, the production
    baseline. Raises only when trial settings cannot be applied or restored (MotorConfigError).
    """
    approach = await _run_leg(
        motor,
        target_mm=start_mm,
        peak_current=approach_peak_current,
        following_error_trip_mm=following_error_trip_mm,
        rpm=rpm,
    )
    descent = None
    if approach.outcome == "completed":
        descent = await _run_leg(
            motor,
            target_mm=end_mm,
            peak_current=descent_peak_current,
            following_error_trip_mm=following_error_trip_mm,
            rpm=rpm,
            pause=None if pause_at_mm is None else _PauseAt(motor, pause_at_mm, pause_s),
        )

    params = motor._params
    return TrialResult(
        approach=approach,
        descent=descent,
        following_error_trip_mm=following_error_trip_mm,
        following_error_trip_counts=motor.following_error_counts(following_error_trip_mm),
        configured_peak_current=int(params.Ip),
        holding_current_percent=await _read_optional(motor, "Closed loop holding current %"),
        standby_current_percent=await _read_optional(motor, "Standby current %"),
    )


async def _run_leg(
    motor: Any,
    *,
    target_mm: float,
    peak_current: int | None,
    following_error_trip_mm: float,
    rpm: int | None,
    pause: "_PauseAt | None" = None,
) -> TrialLeg:
    leg = TrialLeg(peak_current=peak_current, start_mm=motor.position_mm(), target_mm=target_mm)

    async with AsyncExitStack() as stack:
        if peak_current is not None:
            await stack.enter_async_context(
                motor.trial_drive_settings(peak_current=peak_current, following_error_mm=following_error_trip_mm)
            )
        trace = await stack.enter_async_context(motor.trace_following_error())
        if pause is not None:
            stack.enter_context(pause.installed())
        started_at = time.monotonic()
        try:
            await motor.move_to_position(target_mm, rpm=rpm)
            leg.outcome = "completed"
        except MotorTimeoutError as exc:
            leg.outcome = "timeout"
            logger.warning("Y descent trial leg timed out", exc_info=exc)
        except MotorError as exc:
            context = getattr(exc, "fault_context", None) or {}
            alarm_code = context.get("alarm_code")
            leg.alarm_code = None if alarm_code is None else int(alarm_code)
            if leg.alarm_code:
                leg.outcome = "drive_alarm"
            elif context.get("stall_detected") or context.get("no_progress"):
                leg.outcome = "stalled"
            else:
                leg.outcome = "error"
            logger.warning("Y descent trial leg failed", exc_info=exc)
        leg.duration_s = time.monotonic() - started_at
        leg.end_mm = motor.position_mm()
        leg.samples = list(trace.samples)
        leg.trace_failed = trace.read_failed
        if pause is not None:
            leg.paused_at_mm, leg.hold_start_mm, leg.hold_end_mm = pause.paused_at_mm, pause.hold_start_mm, pause.hold_end_mm
    # Leaving the stack parked the axis and restored its settings; let the brake set.

    await asyncio.sleep(SETTLE_AFTER_PARK_S)
    try:
        leg.settled_mm = await motor.get_position()
    except MotorError as exc:
        logger.warning("Y descent trial settle read failed", exc_info=exc)

    params = motor._params
    leg.max_following_error_mm, leg.max_following_error_at_mm = peak_following_error(
        leg.samples,
        profile_per_mm=int(getattr(params, "ppr", 10000)) / float(params.mpr),
        feedback_per_mm=int(getattr(params, "encoder_resolution_cpr", 4000)) / float(params.mpr),
    )
    return leg


class _PauseAt:
    """Stands in for the motor's safety guard during one leg and blocks it once.

    Blocks as the descent first reaches `at_mm` and clears `pause_s` later, so the motor's own
    safety pause stops the drive, holds the load, and resumes the move. The real guard still
    applies throughout: its block and wait are passed through.
    """

    def __init__(self, motor: Any, at_mm: float, pause_s: float) -> None:
        self._motor = motor
        self._at_mm = at_mm
        self._pause_s = pause_s
        self._real: Any = None
        self._pending = False
        self.paused_at_mm: float | None = None
        # Read once the stop has finished and again just before the resume: their difference
        # is how far the load slid while held on the descent current.
        self.hold_start_mm: float | None = None
        self.hold_end_mm: float | None = None

    @property
    def blocked(self) -> bool:
        if self.paused_at_mm is None:
            position = self._motor.position_mm()
            if position is not None and position <= self._at_mm:
                self.paused_at_mm = position
                self._pending = True
        return self._pending or bool(self._real is not None and self._real.blocked)

    async def wait_clear(self) -> None:
        if self._pending:
            await asyncio.sleep(min(SETTLE_AFTER_PAUSE_STOP_S, self._pause_s))
            self.hold_start_mm = await self._motor.get_position()
            await asyncio.sleep(max(0.0, self._pause_s - SETTLE_AFTER_PAUSE_STOP_S))
            self.hold_end_mm = await self._motor.get_position()
            self._pending = False
        if self._real is not None:
            await self._real.wait_clear()

    @contextmanager
    def installed(self):
        self._real = self._motor._safety
        self._motor._safety = self
        try:
            yield self
        finally:
            self._motor._safety = self._real


def peak_following_error(
    samples: list[FollowingErrorSample], *, profile_per_mm: float, feedback_per_mm: float
) -> tuple[float | None, float | None]:
    """Peak |commanded - actual| in mm over a leg, and the position it happened at.

    0x1010 on the CL57RS is not the following error in any usable unit (on lb4a-3 it ran at
    ~155k per mm of real error), so the error is the profile position (0x1012, command pulses)
    minus the feedback position (0x1014, encoder counts). Both restart at 0 every move and share
    its direction, verified on lb4a-3 and lb4a-9 at 250 and 100 per mm for 10000 ppr, 4000 cpr,
    40 mm/rev.
    """
    if not samples:
        return None, None
    peak = max(
        samples,
        key=lambda s: abs(s.profile_position / profile_per_mm - s.feedback_position / feedback_per_mm),
    )
    return (
        abs(peak.profile_position / profile_per_mm - peak.feedback_position / feedback_per_mm),
        peak.position_mm,
    )


async def _read_optional(motor: Any, register: str) -> int | None:
    try:
        return int(await motor.get_param(register))
    except Exception as exc:
        logger.warning("Y descent trial could not read %s", register, exc_info=exc)
        return None
