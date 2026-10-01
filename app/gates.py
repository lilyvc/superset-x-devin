"""Python-owned decision gates. Devin supplies evidence; these decide."""

from datetime import datetime

from .config import Settings
from .parsing import issue_labels, issue_type, parse_dt, tokens

_FAILING_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled"}


def investigation_gate(out: dict) -> tuple[bool, list[str]]:
    reasons = []
    required = {
        "status": "REPRODUCED",
        "enough_information": True,
        "reproduced": True,
        "expected_behavior": "non-empty",
        "observed_behavior": "non-empty",
        "reproduction_steps": "at least one item",
        "reproduction_evidence": "at least one item",
        "root_cause": "non-empty",
        "verification_plan": "at least one item",
    }
    for key, expected in required.items():
        value = out.get(key)
        if expected == "non-empty" and (not isinstance(value, str) or not value.strip()):
            reasons.append(f"{key} must be non-empty")
        elif expected == "at least one item" and (not isinstance(value, list) or not value):
            reasons.append(f"{key} must contain at least one item")
        elif expected is True and value is not True:
            reasons.append(f"{key} must be true")
        elif expected == "REPRODUCED" and value != expected:
            reasons.append("status must be REPRODUCED")
    return not reasons, reasons


def verification_gate(out: dict, pr_url: str | None) -> tuple[bool, list[str]]:
    reasons = []
    if out.get("status") != "PR_OPENED":
        reasons.append("status must be PR_OPENED")
    if not out.get("verification_passed"):
        reasons.append("verification_passed must be true")
    if not out.get("reproduction_rerun_passed"):
        reasons.append("reproduction_rerun_passed must be true")
    tests = out.get("tests_executed")
    if not isinstance(tests, list) or not tests:
        reasons.append("tests_executed must contain at least one test")
    elif any(not isinstance(test, dict) or test.get("result") != "passed" for test in tests):
        reasons.append("all tests_executed results must be passed")
    if not pr_url:
        reasons.append("pr_url is required")
    return not reasons, reasons


def ci_verdict(data: dict) -> tuple[str, list[str]]:
    """Aggregate GitHub check-runs + commit statuses into one verdict.

    Returns (verdict, names) where verdict is passed | failed | pending |
    unverified (no checks configured at all).
    """
    runs = data.get("check_runs") or []
    statuses = data.get("statuses") or []
    if not runs and not statuses:
        return "unverified", []
    failing, pending = [], []
    for run in runs:
        name = run.get("name") or "check"
        if run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") in _FAILING_CONCLUSIONS:
            failing.append(name)
    for status in statuses:
        context = status.get("context") or "status"
        if status.get("state") in {"failure", "error"}:
            failing.append(context)
        elif status.get("state") == "pending":
            pending.append(context)
    if failing:
        return "failed", failing
    if pending:
        return "pending", pending
    return "passed", []


def failing_check_links(data: dict) -> list[str]:
    """Failing checks as "name: url" lines a Devin session can open."""
    lines = []
    for run in data.get("check_runs") or []:
        if run.get("status") == "completed" and run.get("conclusion") in _FAILING_CONCLUSIONS:
            url = run.get("html_url") or run.get("details_url") or ""
            lines.append(f"{run.get('name') or 'check'}: {url}".rstrip(": "))
    for status in data.get("statuses") or []:
        if status.get("state") in {"failure", "error"}:
            url = status.get("target_url") or ""
            lines.append(f"{status.get('context') or 'status'}: {url}".rstrip(": "))
    return lines


def intake_skip_reason(issue: dict, settings: Settings, now: datetime) -> str | None:
    """Cheap deterministic pre-filter; returns a skip reason detail or None."""
    labels = issue_labels(issue)
    if settings.eligibility_label and settings.eligibility_label not in labels:
        return f"missing eligibility label {settings.eligibility_label}"
    ignored = {label.lower() for label in labels} & set(settings.ignore_labels)
    if ignored:
        return f"ignored label(s): {', '.join(sorted(ignored))}"
    kind = issue_type(issue)
    if kind and kind in settings.ignore_issue_types:
        return f"ignored issue type: {kind}"
    if settings.issue_lookback_days is not None:
        created = parse_dt(issue.get("created_at"))
        if created and (now - created).days > settings.issue_lookback_days:
            return f"older than lookback of {settings.issue_lookback_days} days"
    return None


def dedup_candidates(workflow: dict, pulls: list[dict]) -> list[tuple[dict, set[str]]]:
    inv = workflow.get("investigation") or {}
    query = tokens(" ".join([
        workflow.get("title") or "",
        inv.get("root_cause") or "",
        " ".join(inv.get("affected_components") or []),
    ]))
    candidates = []
    for pull in pulls:
        shared = query & tokens(f"{pull.get('title') or ''} {pull.get('body') or ''}")
        if len(shared) >= 2:
            candidates.append((pull, shared))
    return candidates
