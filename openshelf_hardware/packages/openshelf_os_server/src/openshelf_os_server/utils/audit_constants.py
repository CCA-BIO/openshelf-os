from contextvars import ContextVar
from typing import Any, Dict

LEGACY_COMMAND_RECORDS_FIELD = "nats_commands"

AUDIT_RETENTION_DAYS = 120

# Machine actions an in-flight HTTP request enqueued (a workflow store hides several
# internal motions behind one POST). The middleware owns the per-request list; the
# command admission path appends to it, and rows outside a request record nothing.
COMMAND_RECORD_LIMIT = 50

command_records_context = ContextVar("command_records", default=[])


def record_enqueued_command(payload: Dict[str, Any], command_id: str, visibility: str) -> None:
    """Append one admitted robot motion to the requesting request's audit row."""
    records = command_records_context.get(None)
    if records is None:
        return
    if len(records) >= COMMAND_RECORD_LIMIT:
        if len(records) == COMMAND_RECORD_LIMIT:
            records.append({"command": "records_truncated"})
        return
    records.append({
        "command_id": command_id,
        "visibility": visibility,
        "payload": dict(payload),
    })
