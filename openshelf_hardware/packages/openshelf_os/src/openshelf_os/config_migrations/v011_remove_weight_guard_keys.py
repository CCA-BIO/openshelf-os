"""Schema 10 → 11: drop the scale keys of the removed weight-change guard.

The guard sampled the IO scale while y descended and stopped motion on a weight change, but
it reacted too late to prevent damage and was removed. Values are dropped rather than moved
because nothing reads them any more.
"""

from __future__ import annotations

import copy
from typing import Any


REMOVED_SCALE_KEYS = (
    "motion_guard_enabled",
    "motion_guard_delta_grams",
    "motion_guard_min_samples",
    "motion_guard_arm_delay_s",
    "motion_guard_ack_grace_s",
    "motion_guard_sample_timeout_s",
    "motion_guard_idle_interval_s",
)


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    node: Any = migrated
    for key in ("robot", "io", "scale"):
        if not isinstance(node, dict) or key not in node:
            return migrated, []
        node = node[key]
    if not isinstance(node, dict):
        return migrated, []

    changes: list[str] = []
    for key in REMOVED_SCALE_KEYS:
        if key in node:
            del node[key]
            changes.append(f"robot.io.scale.{key} removed (unused)")
    return migrated, changes
