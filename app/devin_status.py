"""Normalization helpers for Devin v1 and v3 session responses.

v3 statuses (top-level `status`) include running / suspended / finished /
error; `status_detail` refines them — e.g. waiting_for_user,
waiting_for_approval, inactivity, finished. Suspended sessions are resumable
(the message endpoint wakes them), so suspension is "waiting", never final.
"""

FINAL_STATUSES = {"finished", "expired", "exit", "error"}
WAITING_STATUSES = {"blocked", "suspend_requested", "suspend_requested_frontend"}
# v3 status_detail values meaning the session is paused but resumable, or is
# stopped on a human: it still counts as settled for output processing.
V3_WAITING_DETAILS = {"waiting_for_user", "waiting_for_approval", "inactivity"}
V3_FINAL_STATUSES = {"finished", "expired", "exit", "error", "terminated"}


def session_state(session: dict) -> tuple[str, str]:
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
    if status in V3_FINAL_STATUSES or detail == "finished":
        return status or detail, "final"
    if status == "suspended" or detail in V3_WAITING_DETAILS:
        return f"{status}:{detail}" if detail else status, "waiting"
    return status or detail, "running"


def last_devin_message(session: dict) -> str | None:
    """Extract the latest Devin-authored message across v1 and v3 shapes."""
    messages = session.get("messages") or []
    for message in reversed(messages):
        message_type = (message.get("type") or "").lower()
        origin = (message.get("origin") or "").lower()
        role = (message.get("role") or "").lower()
        if (
            "devin" in message_type
            or origin == "devin"
            or role in {"assistant", "devin"}
        ):
            return message.get("message") or message.get("content")
    if messages:
        last = messages[-1]
        return last.get("message") or last.get("content")
    return None
