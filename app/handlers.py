"""Apply a settled Devin session's structured output to its workflow."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .devin_status import last_devin_message
from .gates import changed_test_expectations, investigation_gate, verification_gate
from .parsing import issue_number_from_url, pr_number_from_url, pull_url
from .prompts import clarification_comment, skip_comment
from .states import NeedsInfoKind, Origin, Role, SkipReason, State
from .store import utcnow

if TYPE_CHECKING:
    from .workflow import WorkflowEngine

logger = logging.getLogger(__name__)


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
    if kind == "REPRODUCED" and out.get("current_behavior_intent") == "DELIBERATE":
        evidence = out.get("intent_evidence") or []
        summary = evidence[0] if evidence else "No intent evidence was provided."
        engine.store.set_state(
            workflow["id"], State.SKIPPED,
            needs_info_kind=SkipReason.INTENDED_BEHAVIOR.value,
            failure_reason="current behaviour is deliberate: " + summary,
            completed_at=utcnow(), investigation=out,
        )
        evidence_list = "\n".join(
            f"- {item}" for item in (evidence or ["No intent evidence was provided."])
        )
        await engine.comment(
            workflow,
            f"**Not a bug: current behaviour is deliberate** "
            f"([investigation]({row.get('url', '')}))\n\n"
            f"{out.get('summary', '')}\n\n**Intent evidence:**\n{evidence_list}",
        )
        _finish(engine, s)
        return
    if kind == "REPRODUCED" and out.get("current_behavior_intent") == "UNCLEAR":
        expected = out.get("expected_behavior") or "<expected_behavior>"
        observed = out.get("observed_behavior") or "<observed_behavior>"
        question = out.get("clarification_question") or (
            f"Is the current behaviour intended? {expected} vs {observed}"
        )
        engine.store.set_state(
            workflow["id"], State.NEEDS_INFO,
            needs_info_kind=NeedsInfoKind.PRODUCT.value,
            waiting_since=utcnow(), investigation=out,
        )
        await engine.comment(
            workflow,
            clarification_comment(question, NeedsInfoKind.PRODUCT.value, row.get("url", "")),
        )
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
    await engine.learn(workflow, out.get("learned_rules") or [])
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
        approved = any(
            event["kind"] == "behavior_change_approved"
            for event in engine.store.get_events(workflow["id"])
        )
        if not approved:
            try:
                changed = changed_test_expectations(
                    await engine.github.list_pull_file_patches(workflow["repo"], pr_number)
                )
            except Exception as exc:
                logger.exception("could not inspect test changes in PR #%s", pr_number)
                engine.store.add_event(
                    workflow["id"], "error",
                    detail=f"behavior-change gate: {exc}",
                )
                changed = []
            if changed:
                now = utcnow()
                engine.store.set_state(
                    workflow["id"], State.BLOCKED,
                    pr_url=pr_url, pr_number=pr_number, pr_opened_at=now,
                    remediation=out,
                    needs_info_kind=NeedsInfoKind.BEHAVIOR_CHANGE.value,
                    failure_reason="fix changes existing test expectations",
                    waiting_since=now,
                )
                engine.store.add_event(
                    workflow["id"], "behavior_change_held", detail=changed,
                )
                lines = "\n".join(changed[:10])
                await engine.comment(
                    workflow,
                    "**Maintainer decision needed: this fix changes existing behaviour**\n\n"
                    f"{pr_url}\n\n```text\n{lines}\n```\n\n"
                    "_Existing tests asserted the old behaviour, so this may be a product "
                    "change rather than a bug fix. A maintainer (owner/member/collaborator) "
                    "can reply here to approve it, or close the PR._",
                )
                _finish(engine, s)
                return
        now = utcnow()
        updated = bool(workflow.get("pr_url")) and workflow.get("pr_url") == pr_url
        triage = out.get("ci_triage") or {}
        ci_fix_pr = triage.get("ci_fix_pr_url") if triage.get("cause") == "unrelated" else None
        extra = {}
        if ci_fix_pr and ci_fix_pr != workflow.get("ci_fix_pr_url"):
            extra = {"ci_fix_pr_url": ci_fix_pr, "ci_fix_pr_merged": 0}
        engine.store.set_state(workflow["id"], State.PR_OPENED, remediation=out,
                               pr_url=pr_url, pr_number=pr_number, pr_opened_at=now, **extra)
        tests = "\n".join(f"- {t.get('command', '')}" for t in out.get("tests_executed", []))
        evidence = "\n".join(f"- {x}" for x in out.get("verification_evidence", []))
        if engine.settings.ci_required and updated:
            engine.store.set_state(workflow["id"], State.CI_CHECKING)
            previous = workflow.get("remediation")
            previous = previous.get("ci_triage") if isinstance(previous, dict) else None
            if triage and triage != previous:
                await engine.comment(workflow, _ci_triage_comment(triage, pr_url, ci_fix_pr))
            else:
                await engine.comment(workflow, f"**Fix PR updated:** {pr_url}")
        elif engine.settings.ci_required:
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


def _ci_triage_comment(triage: dict, pr_url: str, ci_fix_pr: str | None) -> str:
    evidence = triage.get("evidence") or ""
    if triage.get("cause") == "caused_by_fix":
        return (f"**CI failure was caused by the fix** — Devin pushed a follow-up to "
                f"{pr_url}.\n\n{evidence}")
    if ci_fix_pr:
        return (f"**CI failure is unrelated to the fix** — {evidence}\n\n"
                f"Devin opened {ci_fix_pr} to fix CI. Once a maintainer merges it, "
                f"Devin updates {pr_url} so CI re-runs.")
    return (f"**CI failure is unrelated to the fix** — {evidence}\n\n"
            "_No CI fix PR was opened; a maintainer needs to look._")


async def handle_analyst(engine: WorkflowEngine, s: SettledSession) -> None:
    row, workflow, out = s.row, s.workflow, s.output
    engine.store.set_state(workflow["id"], workflow["state"], analysis=out)
    if not out:
        # A session that dies before producing a verdict leaves nothing to
        # report — don't post an empty analysis comment on the issue.
        _finish(engine, s)
        return
    followup_number = issue_number_from_url(out.get("followup_issue_url"))
    if followup_number:
        # Always (re)record provenance: the poller usually discovers the filed
        # issue before this session settles, so the workflow already exists
        # with origin=DEVIN_DISCOVERED (label path) but no parent recorded.
        # record_issue_origin is idempotent and only fills missing fields.
        engine.store.record_issue_origin(
            workflow["repo"], followup_number, Origin.DEVIN_DISCOVERED.value,
            parent_issue_number=workflow["issue_number"],
            session_id=row["session_id"],
        )
    await engine.comment(
        workflow,
        f"**Retro Devin — defect-family analysis**\n\n"
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
