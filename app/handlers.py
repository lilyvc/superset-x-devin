"""Apply a settled Devin session's structured output to its workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from .devin_status import last_devin_message
from .gates import investigation_gate, verification_gate
from .parsing import issue_number_from_url, pr_number_from_url, pull_url
from .prompts import clarification_comment, skip_comment
from .states import Origin, Role, SkipReason, State
from .store import utcnow

if TYPE_CHECKING:
    from .workflow import WorkflowEngine


@dataclass(frozen=True)
class SettledSession:
    row: dict
    workflow: dict
    output: dict
    kind: str
    fingerprint: str
    response: dict
    pulls: list


def _finish(engine: WorkflowEngine, s: SettledSession, **fields) -> None:
    engine.store.update_session(
        s.row["session_id"], active=0, finished_at=utcnow(),
        acted_fingerprint=s.fingerprint, **fields,
    )


def _acknowledge(engine: WorkflowEngine, s: SettledSession, **fields) -> None:
    engine.store.update_session(
        s.row["session_id"], acted_fingerprint=s.fingerprint, **fields,
    )


async def handle_triage(engine: WorkflowEngine, s: SettledSession) -> None:
    row, workflow, out = s.row, s.workflow, s.output
    engine.store.set_state(workflow["id"], workflow["state"], triage=out)
    if not out:
        engine.store.set_state(
            workflow["id"], State.QUEUED,
            failure_reason=f"triage produced no verdict ({s.kind}); queued anyway",
        )
        _finish(engine, s)
        return
    verdict = (out.get("verdict") or "").upper()
    rationale = out.get("rationale") or ""
    if verdict == "SKIP":
        reason = out.get("skip_reason") or SkipReason.UNSUITABLE.value
        detail = f"{reason}: {rationale}"
        if out.get("duplicate_of"):
            detail += f" (duplicate of {out['duplicate_of']})"
        engine.store.set_state(workflow["id"], State.SKIPPED, needs_info_kind=reason,
                               failure_reason=detail, completed_at=utcnow(), triage=out)
        await engine.comment(workflow, skip_comment(reason, rationale, row.get("url", "")))
    elif verdict == "NEEDS_INFO":
        question = out.get("clarification_question") or (
            "Could you add the missing details (Superset version, exact steps, and what you "
            "expected to happen)?"
        )
        engine.store.set_state(workflow["id"], State.NEEDS_INFO,
                               needs_info_kind=out.get("needs_info_kind"),
                               waiting_since=utcnow(), triage=out)
        await engine.comment(
            workflow,
            clarification_comment(question, out.get("needs_info_kind"), row.get("url", "")),
        )
    else:
        engine.store.set_state(workflow["id"], State.QUEUED, triage=out)
    _finish(engine, s)


async def handle_investigator(engine: WorkflowEngine, s: SettledSession) -> None:
    row, workflow, out = s.row, s.workflow, s.output
    engine.store.set_state(workflow["id"], workflow["state"], investigation=out)
    if not out:
        if s.kind == "waiting":
            question = last_devin_message(s.response) or (
                "Please provide more information so the investigation can continue."
            )
            engine.store.set_state(
                workflow["id"], State.NEEDS_INFO, needs_info_kind=None,
                waiting_since=utcnow(), investigation=out,
            )
            await engine.comment(
                workflow, clarification_comment(question, None, row.get("url", "")),
            )
            _acknowledge(engine, s)
        else:
            engine.store.set_state(
                workflow["id"], State.FAILED,
                failure_reason="investigator session ended without structured output",
            )
            _finish(engine, s)
        return
    kind = out.get("status")
    question = out.get("clarification_question") or "Please provide more information."
    if kind == "NEEDS_INFO" or (not out.get("enough_information") and out.get("clarification_question")):
        engine.store.set_state(
            workflow["id"], State.NEEDS_INFO, needs_info_kind=out.get("needs_info_kind"),
            waiting_since=utcnow(), investigation=out,
        )
        await engine.comment(
            workflow, clarification_comment(
                question, out.get("needs_info_kind"), row.get("url", ""),
            ),
        )
        _acknowledge(engine, s)
        return
    if kind == "NOT_REPRODUCIBLE":
        engine.store.set_state(workflow["id"], State.NOT_REPRODUCIBLE, investigation=out)
        await engine.comment(
            workflow,
            f"**Issue investigation: not reproducible**\n\n"
            f"{out.get('summary', '')}\n\n{out.get('observed_behavior', '')}",
        )
        _finish(engine, s)
        return
    if kind == "BLOCKED":
        reason = "; ".join(
            out.get("missing_information") or [out.get("summary", "Investigator blocked")]
        )
        engine.store.set_state(workflow["id"], State.BLOCKED, failure_reason=reason,
                               waiting_since=utcnow(), investigation=out)
        await engine.comment(workflow, f"**Investigation blocked**\n\n{reason}")
        _acknowledge(engine, s)
        return
    ok, reasons = investigation_gate(out)
    if not ok:
        attempts = int(row.get("attempts") or 0)
        if attempts == 0:
            await engine.devin.send_message(
                row["session_id"],
                "Please fill the missing structured-output fields: " + "; ".join(reasons),
            )
            _acknowledge(engine, s, attempts=1)
        else:
            engine.store.set_state(workflow["id"], State.BLOCKED,
                                   failure_reason="; ".join(reasons), waiting_since=utcnow())
            _finish(engine, s, attempts=attempts + 1)
        return
    now = utcnow()
    engine.store.set_state(workflow["id"], State.REPRODUCED, investigation=out,
                           reproduced_at=now)
    engine.store.set_state(workflow["id"], State.ROOT_CAUSE_FOUND, root_cause_at=now,
                           investigated_at=now)
    evidence = "\n".join(f"- {x}" for x in out.get("reproduction_evidence", []))
    await engine.comment(
        workflow,
        f"**Reproduced + root cause** ([investigation]({row.get('url', '')}))\n\n"
        f"{out.get('summary', '')}\n\n**Root cause:** {out.get('root_cause', '')}\n\n"
        f"**Evidence:**\n{evidence}",
    )
    _finish(engine, s)


async def handle_remediator(engine: WorkflowEngine, s: SettledSession) -> None:
    row, workflow, out = s.row, s.workflow, s.output
    pr_url = out.get("pr_url") or pull_url(s.pulls)
    engine.store.set_state(workflow["id"], workflow["state"], remediation=out)
    if not out:
        if s.kind == "waiting":
            reason = last_devin_message(s.response) or "Remediator is waiting for human input."
            engine.store.set_state(
                workflow["id"], State.BLOCKED, failure_reason=reason,
                waiting_since=utcnow(),
            )
            await engine.comment(workflow, f"**Remediation blocked**\n\n{reason}")
            _acknowledge(engine, s)
        else:
            engine.store.set_state(
                workflow["id"], State.FAILED,
                failure_reason="remediator session ended without structured output",
            )
            _finish(engine, s)
        return
    if out.get("status") == "BLOCKED":
        reason = "; ".join(out.get("blockers") or [out.get("summary", "Remediator blocked")])
        engine.store.set_state(workflow["id"], State.BLOCKED, failure_reason=reason,
                               waiting_since=utcnow())
        await engine.comment(workflow, f"**Remediation blocked**\n\n{reason}")
        _finish(engine, s)
        return
    ok, reasons = verification_gate(out, pr_url)
    if ok:
        pr_number = pr_number_from_url(pr_url)
        now = utcnow()
        engine.store.set_state(workflow["id"], State.PR_OPENED, remediation=out,
                               pr_url=pr_url, pr_number=pr_number, pr_opened_at=now)
        tests = "\n".join(f"- {t.get('command', '')}" for t in out.get("tests_executed", []))
        evidence = "\n".join(f"- {x}" for x in out.get("verification_evidence", []))
        if engine.settings.ci_required:
            engine.store.set_state(workflow["id"], State.CI_CHECKING, ci_status="pending")
            await engine.comment(
                workflow,
                f"**Fix opened:** {pr_url}\n\n"
                f"**Tests:**\n{tests}\n\n**Evidence:**\n{evidence}\n\n"
                "_Agent-verified — the workflow moves to ready for review "
                "only once the PR's GitHub checks pass._",
            )
        else:
            engine.store.set_state(workflow["id"], State.READY_FOR_REVIEW,
                                   ci_status="skipped")
            await engine.comment(
                workflow,
                f"**Fix ready for review:** {pr_url}\n\n"
                f"**Tests:**\n{tests}\n\n**Evidence:**\n{evidence}",
            )
        _finish(engine, s)
        return
    engine.store.set_state(workflow["id"], State.VERIFYING, remediation=out)
    attempts = int(workflow.get("remediation_attempts") or 0)
    if attempts < engine.settings.max_remediation_attempts:
        await engine.devin.send_message(
            row["session_id"],
            "Fix verification failed: " + "; ".join(reasons) +
            "; iterate and update structured output.",
        )
        engine.store.set_state(workflow["id"], State.REMEDIATING,
                               remediation_attempts=attempts + 1)
        _acknowledge(engine, s)
    else:
        engine.store.set_state(workflow["id"], State.FAILED,
                               failure_reason="; ".join(reasons))
        await engine.comment(workflow, "**Fix verification failed**\n\n" + "; ".join(reasons))
        _finish(engine, s)


async def handle_analyst(engine: WorkflowEngine, s: SettledSession) -> None:
    row, workflow, out = s.row, s.workflow, s.output
    engine.store.set_state(workflow["id"], workflow["state"], analysis=out)
    followup_number = issue_number_from_url(out.get("followup_issue_url"))
    if followup_number:
        engine.store.record_issue_origin(
            workflow["repo"], followup_number, Origin.DEVIN_DISCOVERED.value,
            parent_issue_number=workflow["issue_number"],
            session_id=row["session_id"],
        )
    await engine.comment(
        workflow,
        f"**Engineering analysis**\n\n"
        f"Systemic risk: {out.get('systemic_risk', 'unknown')}\n\n"
        f"{out.get('summary', '')}\n\n"
        f"Recommended follow-up: {out.get('recommended_followup', '')}\n"
        f"{out.get('followup_issue_url') or ''}",
    )
    _finish(engine, s)


async def handle_dedup(engine: WorkflowEngine, s: SettledSession) -> None:
    row, workflow, out = s.row, s.workflow, s.output
    if not out:
        if s.kind == "waiting":
            reason = last_devin_message(s.response) or "Dedup check is waiting for input."
            engine.store.set_state(workflow["id"], State.BLOCKED,
                                   failure_reason=reason, waiting_since=utcnow())
            _acknowledge(engine, s)
        else:
            engine.store.set_state(
                workflow["id"], State.BLOCKED,
                failure_reason="dedup session ended without a verdict",
                waiting_since=utcnow(),
            )
            _finish(engine, s)
        return
    verdict = (out.get("verdict") or "").upper()
    rationale = out.get("rationale") or ""
    if verdict == "DUPLICATE":
        pr = out.get("duplicate_pr_url") or ""
        engine.store.set_state(workflow["id"], State.SKIPPED,
                               needs_info_kind=SkipReason.DUPLICATE.value,
                               failure_reason=f"already fixed by open PR {pr}: {rationale}",
                               completed_at=utcnow())
        await engine.comment(
            workflow,
            f"**Skipped — duplicate remediation avoided** "
            f"([dedup check]({row.get('url', '')}))\n\n"
            f"An open PR already fixes this root cause: {pr}\n\n{rationale}",
        )
    elif verdict == "PROCEED":
        engine.store.add_event(workflow["id"], "dedup_cleared", detail=rationale)
    else:
        reason = rationale or "dedup verdict was UNSURE"
        engine.store.set_state(workflow["id"], State.BLOCKED,
                               failure_reason=reason, waiting_since=utcnow())
        await engine.comment(
            workflow,
            f"**Duplicate check needs a maintainer** "
            f"([dedup check]({row.get('url', '')}))\n\n"
            f"{reason}\n\n_Reply on this issue to confirm whether an open PR already "
            "fixes this root cause._",
        )
    _finish(engine, s)


ROLE_HANDLERS: dict[str, Callable[[WorkflowEngine, SettledSession], Awaitable[None]]] = {
    Role.TRIAGE.value: handle_triage,
    Role.INVESTIGATOR.value: handle_investigator,
    Role.REMEDIATOR.value: handle_remediator,
    Role.ANALYST.value: handle_analyst,
    Role.DEDUP.value: handle_dedup,
}
