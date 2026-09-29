"""Normalization helpers for Devin v1 and v3 session responses."""

FINAL_STATUSES = {"finished", "expired", "exit", "error"}
WAITING_STATUSES = {"blocked", "suspend_requested", "suspend_requested_frontend"}
V3_WAITING_DETAILS = {"waiting_for_user", "inactivity"}


def _session_state(session: dict) -> tuple[str, str]:
    """Return ``(status, kind)`` where kind is final, waiting, or running."""
    enum = session.get("status_enum")
    if enum:
        if enum in WAITING_STATUSES:
            return enum, "waiting"
        if enum in FINAL_STATUSES:
            return enum, "final"
        return enum, "running"
    status = session.get("status") or ""
    detail = session.get("status_detail") or ""
    if status in {"exit", "error"}:
        return status, "final"
    if status == "suspended" or detail in V3_WAITING_DETAILS:
        kind = "waiting" if detail == "waiting_for_user" else "final"
        return f"{status}:{detail}" if detail else status, kind
    return status or detail, "running"
