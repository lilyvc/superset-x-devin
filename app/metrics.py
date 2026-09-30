"""Database-derived workflow metrics."""

from datetime import datetime, timedelta, timezone
from statistics import median

from .states import ACTIVE_STATES, FUNNEL, State


def _dt(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _duration(rows, end):
    values = []
    for row in rows:
        start, finish = _dt(row.get("discovered_at")), _dt(row.get(end))
        if start and finish:
            values.append((finish - start).total_seconds())
    return median(values) if values else None


def metrics(store, settings) -> dict:
    rows = store.list_workflows()
    states = {state.value: sum(r["state"] == state.value for r in rows) for state in State}
    active_values = {s.value for s in ACTIVE_STATES}
    groups = {
        "discovered": states[State.DISCOVERED.value],
        "triaging": states[State.TRIAGING.value],
        "skipped": states[State.SKIPPED.value],
        "backlog": sum(r["state"] in {State.QUEUED.value, State.DISCOVERED.value} for r in rows),
        "active": sum(r["state"] in active_values for r in rows),
        "waiting_for_human": sum(r["state"] in {State.NEEDS_INFO.value, State.BLOCKED.value} for r in rows),
        "blocked_failed": sum(r["state"] in {State.BLOCKED.value, State.FAILED.value, State.ESCALATED.value} for r in rows),
        "prs_ready": states[State.READY_FOR_REVIEW.value],
        "ci_pending": sum(r["state"] in {State.PR_OPENED.value, State.CI_CHECKING.value}
                          for r in rows),
        "completed": states[State.COMPLETED.value],
        "not_reproducible": states[State.NOT_REPRODUCIBLE.value],
    }
    funnel = {
        name: len(rows) if stage is None else sum(r["state"] in {s.value for s in stage} for r in rows)
        for name, stage in FUNNEL
    }
    triaged = sum(r["state"] not in {State.DISCOVERED.value, State.TRIAGING.value} for r in rows)
    investigated = sum(
        r["state"] not in {State.DISCOVERED.value, State.QUEUED.value, State.TRIAGING.value,
                           State.SKIPPED.value}
        for r in rows
    )
    reproduced = sum(r["state"] in {s.value for s in {
        State.REPRODUCED, State.ROOT_CAUSE_FOUND, State.REMEDIATING, State.VERIFYING,
        State.PR_OPENED, State.CI_CHECKING, State.READY_FOR_REVIEW, State.COMPLETED,
    }} for r in rows)
    now = datetime.now(timezone.utc)
    sessions = [s for r in rows for s in store.get_sessions(r["id"])]
    total_acus = sum(float(s.get("acus") or 0) for s in sessions)
    opened_24h = opened_7d = 0
    for row in rows:
        opened = _dt(row.get("pr_opened_at"))
        if opened:
            age = now - opened
            opened_24h += age <= timedelta(days=1)
            opened_7d += age <= timedelta(days=7)
    return {
        "counts": states, "groups": groups, "queue_depth": groups["backlog"],
        "active_sessions": store.count_active_sessions(), "max_concurrent": settings.max_concurrent_devins,
        "utilization": store.count_active_sessions() / settings.max_concurrent_devins
        if settings.max_concurrent_devins else 0, "funnel": funnel,
        "rates": {
            "reproduction_rate": reproduced / investigated if investigated else 0,
            "needs_info_rate": states[State.NEEDS_INFO.value] / len(rows) if rows else 0,
            "verified_fix_rate": groups["prs_ready"] / reproduced if reproduced else 0,
            "failure_rate": groups["blocked_failed"] / len(rows) if rows else 0,
            "triage_skip_rate": groups["skipped"] / triaged if triaged else 0,
        },
        "medians": {
            "discovered_to_triage_start": _duration(rows, "triaged_started_at"),
            "discovered_to_investigation_start": _duration(rows, "started_at"),
            "discovered_to_reproduced": _duration(rows, "reproduced_at"),
            "discovered_to_root_cause": _duration(rows, "root_cause_at"),
            "discovered_to_pr_opened": _duration(rows, "pr_opened_at"),
            "discovered_to_completed": _duration(rows, "completed_at"),
        },
        "throughput": {"prs_opened_last_24h": opened_24h, "prs_opened_last_7d": opened_7d},
        "total_acus": total_acus,
        "acus_per_completed_pr": total_acus / groups["completed"] if groups["completed"] else 0,
    }
