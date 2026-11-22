"""Schema 9 → 10: drop the scan keys of the removed automatic angle calibration.

Rotation and shelf stage extension are set by hand alongside the front and back reference
cells, so the stage-stall angle sweep and the stage-extension probe were removed. Values
are dropped rather than moved because nothing reads them any more.
"""

from __future__ import annotations

import copy
from typing import Any


REMOVED_SCAN_KEYS = (
    "angle_calibration_x_max_mm",
    "angle_calibration_stage_stall_no_progress_timeout_s",
)


def _scan_block(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return robot.grid.scan, or None when the grid carries no scan subtree."""
    node: Any = config
    for key in ("robot", "grid", "scan"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
        if key == "grid" and isinstance(node, str):
            # GridConfig also accepts a bare storage path, which has no scan subtree.
            return None
    if not isinstance(node, dict):
        raise ValueError("robot.grid.scan must be an object when present")
    return node


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    scan = _scan_block(migrated)
    if scan is None:
        return migrated, []

    changes: list[str] = []
    for key in REMOVED_SCAN_KEYS:
        if key in scan:
            del scan[key]
            changes.append(f"robot.grid.scan.{key} removed (unused)")
    return migrated, changes
