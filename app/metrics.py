"""Database-derived workflow metrics."""

from datetime import datetime, timedelta, timezone
from statistics import median

from .parsing import parse_dt
from .states import ACTIVE_STATES, FUNNEL, Origin, State

_WAIT_STATES = {State.NEEDS_INFO.value, State.BLOCKED.value}
_TERMINAL = {s.value for s in
             (State.SKIPPED, State.COMPLETED, State.NOT_REPRODUCIBLE,
              State.FAILED, State.ESCALATED)}
_IN_PROGRESS = {s.value for s in (
    State.DISCOVERED, State.QUEUED, State.TRIAGING, State.INVESTIGATING,
    State.REPRODUCED, State.ROOT_CAUSE_FOUND, State.REMEDIATING,
    State.VERIFYING, State.PR_OPENED, State.CI_CHECKING)}


def _duration(rows, end):
    values = []
    for row in rows:
        start, finish = parse_dt(row.get("discovered_at")), parse_dt(row.get(end))
        if start and finish:
            values.append((finish - start).total_seconds())
    return median(values) if values else None


def _wait_seconds(store, row) -> float:
    """Total time the workflow spent waiting on a human (NEEDS_INFO/BLOCKED),
    derived from recorded state transitions."""
    total = 0.0
    entered = None
    for event in store.get_events(row["id"]):
        to_state = event.get("to_state")
        if to_state in _WAIT_STATES:
            entered = parse_dt(event.get("at"))
        elif to_state and entered is not None:
            left = parse_dt(event.get("at"))
            if left:
                total += max(0.0, (left - entered).total_seconds())
            entered = None
    return total


def _time_to_fix(store, rows) -> float | None:
    """Median discovered_at -> pr_opened_at, minus time parked waiting for a
    human (NEEDS_INFO/BLOCKED)."""
    values = []
    for row in rows:
        start, finish = parse_dt(row.get("discovered_at")), parse_dt(row.get("pr_opened_at"))
        if start and finish:
            values.append(
                max(0.0, (finish - start).total_seconds() - _wait_seconds(store, row)))
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
        opened = parse_dt(row.get("pr_opened_at"))
        if opened:
            age = now - opened
            opened_24h += age <= timedelta(days=1)
            opened_7d += age <= timedelta(days=7)
    verified = sum(r["state"] in {State.READY_FOR_REVIEW.value, State.COMPLETED.value}
                   for r in rows)
    defects_discovered = sum(
        r.get("origin") == Origin.DEVIN_DISCOVERED.value for r in rows)
    # Review-ready PRs are human attention too — a VP looking at "needs human"
    # expects to see PRs waiting on a reviewer alongside clarification asks.
    awaiting_review = states[State.READY_FOR_REVIEW.value]
    awaiting_input = groups["waiting_for_human"]
    awaiting_blocked = groups["blocked_failed"]
    awaiting_human = awaiting_review + awaiting_input + awaiting_blocked
    in_progress = sum(r["state"] in _IN_PROGRESS for r in rows)
    merged_prs = [r for r in rows if r["state"] == State.COMPLETED.value and r.get("pr_number")]
    merged_clean = sum(bool(r.get("merged_without_changes")) for r in merged_prs)
    running = store.count_running_sessions()
    return {
        "counts": states, "groups": groups, "queue_depth": groups["backlog"],
        "learned_rules": store.list_learned_rules(settings.target_repo),
        "executive": {
            "bugs_handled": len(rows),
            "verified_fixes": verified,
            "median_time_to_fix": _time_to_fix(store, rows),
            "needs_human": awaiting_human,
            "defects_discovered": defects_discovered,
            "acus_per_verified_fix": total_acus / verified if verified else None,
            "issues_open": sum(r["state"] not in _TERMINAL for r in rows),
            "in_progress": in_progress,
            "awaiting_human": awaiting_human,
            "awaiting_review": awaiting_review,
            "awaiting_input": awaiting_input,
            "awaiting_blocked": awaiting_blocked,
            "solved": groups["completed"],
            "merged_without_changes_pct": (
                merged_clean / len(merged_prs) if merged_prs else None),
        },
        "active_sessions": running, "waiting_sessions": store.count_active_sessions() - running,
        "max_concurrent": settings.max_concurrent_devins,
        "utilization": running / settings.max_concurrent_devins
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
