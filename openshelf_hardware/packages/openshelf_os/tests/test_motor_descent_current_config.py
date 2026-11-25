"""descent_Ip: Y's peak current for downward moves, off unless configured."""

import pytest
from pydantic import ValidationError

from openshelf_os.config.models import Motor

Y_AXIS = {
    "motor_name": "y_axis",
    "motor_id": 2,
    "Ip": 60,
    "rpm": 150,
    "dr": 0,
    "mpr": 40.0,
    "accel": 500,
    "ret_rpm": 64,
    "lim": 1165,
    "brake_output_function": 0x24,
}


def test_descent_current_defaults_off():
    assert Motor(**Y_AXIS).descent_Ip is None


def test_y_axis_accepts_a_descent_current_up_to_ip():
    assert Motor(**Y_AXIS, descent_Ip=30).descent_Ip == 30
    assert Motor(**Y_AXIS, descent_Ip=60).descent_Ip == 60


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"descent_Ip": 61}, "cannot exceed Ip", id="above-ip"),
        pytest.param({"descent_Ip": 0}, "greater than 0", id="zero"),
        pytest.param({"motor_name": "z_axis", "descent_Ip": 10}, "only supported on y_axis", id="not-y"),
    ],
)
def test_invalid_descent_current_is_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Motor(**{**Y_AXIS, **overrides})


def test_home_current_is_optional_and_allowed_on_any_axis_up_to_ip():
    assert Motor(**Y_AXIS).home_Ip is None
    assert Motor(**{**Y_AXIS, "motor_name": "rotate", "Ip": 36, "home_Ip": 36}).home_Ip == 36


@pytest.mark.parametrize(
    ("home_ip", "message"),
    [
        pytest.param(61, "home_Ip \\(61\\) cannot exceed Ip", id="above-ip"),
        pytest.param(0, "greater than 0", id="zero"),
    ],
)
def test_invalid_home_current_is_rejected(home_ip, message):
    with pytest.raises(ValidationError, match=message):
        Motor(**{**Y_AXIS, "home_Ip": home_ip})


def test_fast_home_speed_must_exceed_the_final_speed_and_is_refused_on_y():
    rotate = {**Y_AXIS, "motor_name": "rotate", "Ip": 36, "ret_rpm": 36, "home_rpm": 12}
    assert Motor(**rotate, home_fast_rpm=24).home_fast_rpm == 24
    with pytest.raises(ValidationError, match="must exceed the final homing speed"):
        Motor(**rotate, home_fast_rpm=12)
    with pytest.raises(ValidationError, match="not supported on y_axis"):
        Motor(**Y_AXIS, home_fast_rpm=100)
