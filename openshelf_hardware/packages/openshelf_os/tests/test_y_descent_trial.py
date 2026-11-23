"""Y descent trial: following error from the drive's profile and feedback positions."""

import pathlib
import sys

BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))
sys.path.insert(0, str(BASE.parents[3] / "packages" / "hardware" / "src"))

from hardware.motor import FollowingErrorSample
from openshelf_os.robot.y_descent_trial import peak_following_error

# 10000 ppr command pulses and 4000 cpr encoder counts at 40 mm/rev.
PROFILE_PER_MM = 250.0
FEEDBACK_PER_MM = 100.0


def _sample(position_mm: float, commanded_mm: float, actual_mm: float, raw_fe: int = 0) -> FollowingErrorSample:
    return FollowingErrorSample(
        elapsed_s=0.0,
        position_mm=position_mm,
        raw_position=None,
        following_error=raw_fe,
        profile_position=round(commanded_mm * PROFILE_PER_MM),
        feedback_position=round(actual_mm * FEEDBACK_PER_MM),
    )


def test_descent_lag_is_reported_in_mm_regardless_of_the_raw_register():
    # Counters zero at the start of the move and count negative on the way down.
    samples = [
        _sample(700.0, 0.0, 0.0, raw_fe=900_000),
        _sample(500.0, -200.9, -200.0, raw_fe=150_000_000),
        _sample(320.0, -380.95, -380.0, raw_fe=140_000_000),
        _sample(300.0, -400.0, -400.0, raw_fe=0),
    ]

    error_mm, at_mm = peak_following_error(samples, profile_per_mm=PROFILE_PER_MM, feedback_per_mm=FEEDBACK_PER_MM)

    assert abs(error_mm - 0.95) < 0.01
    assert at_mm == 320.0


def test_carriage_running_ahead_at_the_stop_is_the_peak():
    # lb4a-9, 1.3 A with 5 kg: the load overran the stop until the drive alarmed.
    samples = [
        _sample(320.0, -380.0, -379.07),
        _sample(300.69, -400.0, -399.31 - 0.92),
        _sample(293.53, -400.0, -408.85),
    ]

    error_mm, at_mm = peak_following_error(samples, profile_per_mm=PROFILE_PER_MM, feedback_per_mm=FEEDBACK_PER_MM)

    assert abs(error_mm - 8.85) < 0.01
    assert at_mm == 293.53


def test_no_samples_leaves_the_error_unknown():
    assert peak_following_error([], profile_per_mm=PROFILE_PER_MM, feedback_per_mm=FEEDBACK_PER_MM) == (None, None)
