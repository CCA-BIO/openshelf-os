"""One Y descent-current trial, for finding the lowest peak current that still lowers the
carriage and its payload under control (development only; `scripts/y_descent_sweep.py` runs
a sweep of these against a machine).

A trial climbs to `start_mm`, then descends to `end_mm` at the trial current behind a tight
following-error trip, tracing the drive's live following error on both legs. The drive holds the
falling load hardest while it decelerates at the bottom of the descent, so the trace peaks there
first as the current drops.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from hardware.exceptions import MotorError, MotorTimeoutError
from hardware.motor import FollowingErrorSample

logger = logging.getLogger(__name__)

# The brake sets within Pr4.20 (250 ms) of the park; the sag reading waits a little longer.
SETTLE_AFTER_PARK_S = 0.5
# A leg shorter than one motor revolution (in command pulses) says too little about the trace unit.
UNIT_PROBE_MIN_RAW_TRAVEL = 10000

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
    max_abs_following_error: int | None = None
    max_following_error_at_mm: float | None = None
    max_following_error_mm: float | None = None
    trace_failed: bool = False
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
    following_error_units_per_mm: float | None

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
        )

    params = motor._params
    units_per_mm = _following_error_units_per_mm(
        [leg.samples for leg in (approach, descent) if leg is not None],
        ppr=int(getattr(params, "ppr", 10000)),
        mpr=float(params.mpr),
    )
    for leg in (approach, descent):
        if leg is not None and leg.max_abs_following_error is not None and units_per_mm is not None:
            leg.max_following_error_mm = leg.max_abs_following_error / units_per_mm

    return TrialResult(
        approach=approach,
        descent=descent,
        following_error_trip_mm=following_error_trip_mm,
        following_error_trip_counts=motor.following_error_counts(following_error_trip_mm),
        configured_peak_current=int(params.Ip),
        holding_current_percent=await _read_optional(motor, "Closed loop holding current %"),
        standby_current_percent=await _read_optional(motor, "Standby current %"),
        following_error_units_per_mm=units_per_mm,
    )


async def _run_leg(
    motor: Any,
    *,
    target_mm: float,
    peak_current: int | None,
    following_error_trip_mm: float,
    rpm: int | None,
) -> TrialLeg:
    leg = TrialLeg(peak_current=peak_current, start_mm=motor.position_mm(), target_mm=target_mm)

    async with AsyncExitStack() as stack:
        if peak_current is not None:
            await stack.enter_async_context(
                motor.trial_drive_settings(peak_current=peak_current, following_error_mm=following_error_trip_mm)
            )
        trace = await stack.enter_async_context(motor.trace_following_error())
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
    # Leaving the stack parked the axis and restored its settings; let the brake set.

    await asyncio.sleep(SETTLE_AFTER_PARK_S)
    try:
        leg.settled_mm = await motor.get_position()
    except MotorError as exc:
        logger.warning("Y descent trial settle read failed", exc_info=exc)

    if leg.samples:
        peak = max(leg.samples, key=lambda sample: abs(sample.following_error))
        leg.max_abs_following_error = abs(peak.following_error)
        leg.max_following_error_at_mm = peak.position_mm
    return leg


def _following_error_units_per_mm(
    legs: list[list[FollowingErrorSample]], *, ppr: int, mpr: float
) -> float | None:
    """Trace units per mm, from how far the feedback position moved against the 0x602C position.

    The 0x602C position is in command pulses (ppr per rev). Travel, not absolute values, because
    the two registers need not share a zero. A ratio of 1 means the trace counts pulses, 0.4
    (4000/10000) encoder counts. Legs that disagree leave it unresolved.
    """
    ratios = []
    for samples in legs:
        located = [sample for sample in samples if sample.raw_position is not None]
        if len(located) < 2:
            continue
        raw_travel = located[-1].raw_position - located[0].raw_position
        if abs(raw_travel) < UNIT_PROBE_MIN_RAW_TRAVEL:
            continue
        ratios.append(abs((located[-1].feedback_position - located[0].feedback_position) / raw_travel))
    if not ratios:
        return None
    ratio = statistics.median(ratios)
    if ratio <= 0 or max(abs(value - ratio) for value in ratios) > 0.02 * ratio:
        return None
    return ratio * ppr / mpr


async def _read_optional(motor: Any, register: str) -> int | None:
    try:
        return int(await motor.get_param(register))
    except Exception as exc:
        logger.warning("Y descent trial could not read %s", register, exc_info=exc)
        return None
