"""Deterministic, restart-safe multi-stage workflow engine."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .devin_status import _last_devin_message, _session_state
from .github_client import GitHubClient
from .prompts import (
    ANALYSIS_SCHEMA,
    INVESTIGATION_SCHEMA,
    REMEDIATION_SCHEMA,
    analyst_prompt,
    clarification_comment,
    investigator_prompt,
    remediator_prompt,
)
from .states import ACTIVE_STATES, TERMINAL_STATES, Role, State
from .store import Store, utcnow

logger = logging.getLogger("workflow")


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


def _labels(issue: dict) -> set[str]:
    return {
        item if isinstance(item, str) else item.get("name", "")
        for item in issue.get("labels") or []
    }


def _fingerprint(output: Any) -> str:
    return hashlib.sha256(json.dumps(output or {}, sort_keys=True).encode()).hexdigest()


def _now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class WorkflowEngine:
    def __init__(self, settings: Settings, store: Store, github: GitHubClient, devin):
        self.settings, self.store, self.github, self.devin = settings, store, github, devin
        self._startup_baseline: int | None = None
        self._github_login: str | None = None

    async def tick(self) -> None:
        issues = []
        try:
            issues = await self.github.list_open_issues(self.settings.target_repo)
            await self._discover(issues)
        except Exception as exc:
            logger.exception("discovery failed")
            self.store.add_event(None, "error", detail=f"discovery: {exc}")
        try:
            await self._reconcile_sessions()
        except Exception as exc:
            logger.exception("session reconciliation failed")
            self.store.add_event(None, "error", detail=f"reconcile: {exc}")
        try:
            await self._reconcile_prs()
        except Exception as exc:
            logger.exception("pull request reconciliation failed")
            self.store.add_event(None, "error", detail=f"pull requests: {exc}")
        try:
            await self._replies()
        except Exception as exc:
            logger.exception("reply forwarding failed")
            self.store.add_event(None, "error", detail=f"replies: {exc}")
        try:
            await self._dispatch()
        except Exception as exc:
            logger.exception("dispatch failed")
            self.store.add_event(None, "error", detail=f"dispatch: {exc}")

    async def _discover(self, issues: list[dict]) -> None:
        numbers = {i.get("number") for i in issues}
        if self._startup_baseline is None:
            self._startup_baseline = max(numbers or {0})
        for issue in issues:
            if self.settings.eligibility_label and self.settings.eligibility_label not in _labels(issue):
                continue
            if not self.settings.poll_backlog and issue["number"] <= self._startup_baseline:
                continue
            if self.store.get_workflow(self.settings.target_repo, issue["number"]):
                continue
            workflow = self.store.upsert_workflow(
                self.settings.target_repo, issue["number"], title=issue.get("title", ""),
                issue_url=issue.get("html_url", ""),
                author=(issue.get("user") or {}).get("login", ""),
                labels=list(_labels(issue)), state=State.DISCOVERED.value,
                discovered_at=utcnow(),
            )
            self.store.set_state(workflow["id"], State.QUEUED)

        open_ids = {i["number"] for i in issues}
        for workflow in self.store.list_workflows():
            if workflow["repo"] != self.settings.target_repo or workflow["issue_number"] in open_ids:
                continue
            if workflow["state"] in {s.value for s in TERMINAL_STATES}:
                continue
            try:
                issue = await self.github.get_issue(workflow["repo"], workflow["issue_number"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not reconcile closed issue #%s: %s",
                               workflow["issue_number"], exc)
                continue
            if issue.get("state") == "closed":
                if workflow["state"] in {State.PR_OPENED.value, State.READY_FOR_REVIEW.value}:
                    self.store.set_state(workflow["id"], State.COMPLETED, completed_at=utcnow())
                else:
                    self.store.set_state(
                        workflow["id"], State.ESCALATED,
                        failure_reason="issue closed on GitHub before workflow finished",
                    )

    async def _reconcile_sessions(self) -> None:
        for session_row in self.store.list_active_sessions():
            try:
                await self._reconcile_session(session_row)
            except Exception as exc:
                logger.exception("session reconciliation failed for %s", session_row["session_id"])
                self.store.add_event(
                    session_row["workflow_id"], "error", detail=f"session {session_row['session_id']}: {exc}"
                )

    async def _reconcile_prs(self) -> None:
        states = {State.PR_OPENED.value, State.READY_FOR_REVIEW.value}
        for workflow in self.store.list_workflows(states):
            if not workflow.get("pr_number"):
                continue
            try:
                pull = await self.github.get_pull(workflow["repo"], workflow["pr_number"])
                if pull.get("merged"):
                    self.store.set_state(workflow["id"], State.COMPLETED, completed_at=utcnow())
                elif pull.get("state") == "closed":
                    self.store.set_state(
                        workflow["id"], State.FAILED,
                        failure_reason="PR closed without merge",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not reconcile PR #%s: %s", workflow["pr_number"], exc)
                self.store.add_event(
                    workflow["id"], "error",
                    detail=f"pull request reconciliation: {exc}",
                )

    async def _reconcile_session(self, row: dict) -> None:
        response = await self.devin.get_session(row["session_id"])
        output = response.get("structured_output") or {}
        pulls = response.get("pull_requests") or []
        status, detail = _session_state(response)
        fp = _fingerprint(output)
        self.store.update_session(
            row["session_id"], devin_status=response.get("status_enum") or response.get("status"),
            devin_status_detail=response.get("status_detail") or detail,
            acus=response.get("acus_consumed")
            if response.get("acus_consumed") is not None
            else response.get("acu"),
            structured_output=output, output_fingerprint=fp, pull_requests=pulls,
            last_polled_at=utcnow(),
        )
        settled = detail in {"final", "waiting"}
        if not settled or fp == row.get("acted_fingerprint"):
            return
        workflow = self.store.get_workflow_by_id(row["workflow_id"])
        if not workflow:
            return
        role = row["role"]
        if role == Role.INVESTIGATOR.value:
            await self._handle_investigator(row, workflow, output, status, detail, fp)
        elif role == Role.REMEDIATOR.value:
            await self._handle_remediator(row, workflow, output, status, detail, fp, pulls)
        elif role == Role.ANALYST.value:
            await self._handle_analyst(row, workflow, output, fp)

    async def _handle_investigator(self, row, workflow, out, status, session_kind, fp):
        self.store.set_state(workflow["id"], workflow["state"], investigation=out)
        if not out:
            if session_kind == "waiting":
                question = _last_devin_message(
                    await self.devin.get_session(row["session_id"])
                ) or "Please provide more information so the investigation can continue."
                self.store.set_state(
                    workflow["id"], State.NEEDS_INFO, needs_info_kind=None,
                    waiting_since=utcnow(), investigation=out,
                )
                await self._comment(
                    workflow, clarification_comment(question, None, row.get("url", "")),
                )
                self.store.update_session(row["session_id"], acted_fingerprint=fp)
            else:
                self.store.set_state(
                    workflow["id"], State.FAILED,
                    failure_reason="investigator session ended without structured output",
                )
                self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                          acted_fingerprint=fp)
            return
        kind = out.get("status")
        question = out.get("clarification_question") or "Please provide more information."
        if kind == "NEEDS_INFO" or (not out.get("enough_information") and out.get("clarification_question")):
            self.store.set_state(
                workflow["id"], State.NEEDS_INFO, needs_info_kind=out.get("needs_info_kind"),
                waiting_since=utcnow(), investigation=out,
            )
            await self._comment(
                workflow, clarification_comment(question,
                                                out.get("needs_info_kind"), row.get("url", "")),
            )
            self.store.update_session(row["session_id"], acted_fingerprint=fp)
            return
        if kind == "NOT_REPRODUCIBLE":
            self.store.set_state(workflow["id"], State.NOT_REPRODUCIBLE, investigation=out)
            await self._comment(workflow, f"**Issue investigation: not reproducible**\n\n"
                                f"{out.get('summary', '')}\n\n{out.get('observed_behavior', '')}")
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)
            return
        if kind == "BLOCKED":
            reason = "; ".join(out.get("missing_information") or [out.get("summary", "Investigator blocked")])
            self.store.set_state(workflow["id"], State.BLOCKED, failure_reason=reason,
                                 waiting_since=utcnow(), investigation=out)
            await self._comment(workflow, f"**Investigation blocked**\n\n{reason}")
            self.store.update_session(row["session_id"], acted_fingerprint=fp)
            return
        ok, reasons = investigation_gate(out)
        if not ok:
            attempts = int(row.get("attempts") or 0)
            if attempts == 0:
                await self.devin.send_message(row["session_id"],
                    "Please fill the missing structured-output fields: " + "; ".join(reasons))
                self.store.update_session(row["session_id"], attempts=1, acted_fingerprint=fp)
            else:
                self.store.set_state(workflow["id"], State.BLOCKED,
                                     failure_reason="; ".join(reasons), waiting_since=utcnow())
                self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                          acted_fingerprint=fp, attempts=attempts + 1)
            return
        now = utcnow()
        self.store.set_state(workflow["id"], State.REPRODUCED, investigation=out, reproduced_at=now)
        self.store.set_state(workflow["id"], State.ROOT_CAUSE_FOUND, root_cause_at=now,
                             investigated_at=now)
        evidence = "\n".join(f"- {x}" for x in out.get("reproduction_evidence", []))
        await self._comment(workflow, f"**Reproduced + root cause** ([investigation]({row.get('url', '')}))\n\n"
                            f"{out.get('summary', '')}\n\n**Root cause:** {out.get('root_cause', '')}\n\n"
                            f"**Evidence:**\n{evidence}")
        self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                  acted_fingerprint=fp)

    async def _handle_remediator(self, row, workflow, out, status, session_kind, fp, pulls):
        pr_url = out.get("pr_url") or self._pull_url(pulls)
        self.store.set_state(workflow["id"], workflow["state"], remediation=out)
        if not out:
            if session_kind == "waiting":
                reason = _last_devin_message(
                    await self.devin.get_session(row["session_id"])
                ) or "Remediator is waiting for human input."
                self.store.set_state(
                    workflow["id"], State.BLOCKED, failure_reason=reason,
                    waiting_since=utcnow(),
                )
                await self._comment(workflow, f"**Remediation blocked**\n\n{reason}")
                self.store.update_session(row["session_id"], acted_fingerprint=fp)
            else:
                self.store.set_state(
                    workflow["id"], State.FAILED,
                    failure_reason="remediator session ended without structured output",
                )
                self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                          acted_fingerprint=fp)
            return
        if out.get("status") == "BLOCKED":
            reason = "; ".join(out.get("blockers") or [out.get("summary", "Remediator blocked")])
            self.store.set_state(workflow["id"], State.BLOCKED, failure_reason=reason, waiting_since=utcnow())
            await self._comment(workflow, f"**Remediation blocked**\n\n{reason}")
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)
            return
        ok, reasons = verification_gate(out, pr_url)
        if ok:
            pr_number = self._pr_number(pr_url)
            now = utcnow()
            self.store.set_state(workflow["id"], State.PR_OPENED, remediation=out,
                                 pr_url=pr_url, pr_number=pr_number, pr_opened_at=now)
            self.store.set_state(workflow["id"], State.READY_FOR_REVIEW)
            tests = "\n".join(f"- {t.get('command', '')}" for t in out.get("tests_executed", []))
            evidence = "\n".join(f"- {x}" for x in out.get("verification_evidence", []))
            await self._comment(workflow, f"**Fix ready for review:** {pr_url}\n\n"
                                f"**Tests:**\n{tests}\n\n**Evidence:**\n{evidence}")
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)
            return
        self.store.set_state(workflow["id"], State.VERIFYING, remediation=out)
        attempts = int(workflow.get("remediation_attempts") or 0)
        if attempts < self.settings.max_remediation_attempts:
            await self.devin.send_message(row["session_id"],
                "Fix verification failed: " + "; ".join(reasons) +
                "; iterate and update structured output.")
            self.store.set_state(workflow["id"], State.REMEDIATING,
                                 remediation_attempts=attempts + 1)
            self.store.update_session(row["session_id"], acted_fingerprint=fp)
        else:
            self.store.set_state(workflow["id"], State.FAILED,
                                 failure_reason="; ".join(reasons))
            await self._comment(workflow, "**Fix verification failed**\n\n" + "; ".join(reasons))
            self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                      acted_fingerprint=fp)

    async def _handle_analyst(self, row, workflow, out, fp):
        self.store.set_state(workflow["id"], workflow["state"], analysis=out)
        await self._comment(workflow, f"**Engineering analysis**\n\n"
                            f"Systemic risk: {out.get('systemic_risk', 'unknown')}\n\n"
                            f"{out.get('summary', '')}\n\n"
                            f"Recommended follow-up: {out.get('recommended_followup', '')}\n"
                            f"{out.get('followup_issue_url') or ''}")
        self.store.update_session(row["session_id"], active=0, finished_at=utcnow(),
                                  acted_fingerprint=fp)

    async def _replies(self):
        if self._github_login is None and not self.settings.dry_run:
            try:
                self._github_login = (await self.github.get_authenticated_user()).get("login")
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not fetch authenticated GitHub user: %s", exc)
                self._github_login = ""
        for workflow in self.store.list_workflows():
            if workflow["state"] not in (
                {s.value for s in {State.NEEDS_INFO, State.BLOCKED} | ACTIVE_STATES}
            ):
                continue
            active = self.store.get_sessions(workflow["id"], active_only=True)
            session = next((s for s in active if s["role"] in
                            {Role.INVESTIGATOR.value, Role.REMEDIATOR.value}), None)
            waiting = workflow["state"] in {State.NEEDS_INFO.value, State.BLOCKED.value}
            if not session and not waiting:
                continue
            comments = await self.github.list_issue_comments(workflow["repo"], workflow["issue_number"])
            last_id = workflow.get("last_comment_id") or 0
            for comment in sorted(comments, key=lambda c: c.get("id", 0)):
                if comment.get("id", 0) <= last_id:
                    continue
                last_id = comment["id"]
                user = comment.get("user") or {}
                login = user.get("login", "")
                if user.get("type") == "Bot" or login.endswith("[bot]") or login == self._github_login:
                    continue
                if session:
                    await self.devin.send_message(
                        session["session_id"],
                        f"A human reply was posted on GitHub issue #{workflow['issue_number']} by {login}:\n\n"
                        f"{comment.get('body', '')}",
                    )
                    self.store.add_event(workflow["id"], "human_reply_forwarded",
                                         detail={"comment_id": comment["id"], "session_id": session["session_id"]})
                    self.store.set_state(workflow["id"], State.INVESTIGATING if
                                         workflow["state"] in {State.NEEDS_INFO.value, State.BLOCKED.value}
                                         else workflow["state"], waiting_since=None)
                elif workflow["state"] == State.BLOCKED.value:
                    self.store.add_event(
                        workflow["id"], "human_comment_unforwarded",
                        detail={"comment_id": comment["id"], "reason": "no active session"},
                    )
            current = self.store.get_workflow_by_id(workflow["id"])
            if current:
                self.store.set_state(workflow["id"], current["state"], last_comment_id=last_id)

    async def _dispatch(self):
        capacity = max(0, self.settings.max_concurrent_devins - self.store.count_active_sessions())
        if not capacity:
            return
        workflows = self.store.list_workflows()
        for workflow in workflows:
            if capacity <= 0:
                break
            if workflow["state"] == State.ROOT_CAUSE_FOUND.value and not self.store.get_sessions(
                workflow["id"], Role.REMEDIATOR, active_only=True
            ):
                await self._create_role_session(workflow, Role.REMEDIATOR)
                capacity -= 1
        if self.settings.analysis_enabled:
            for workflow in workflows:
                if capacity <= 0:
                    break
                if workflow["state"] in {s.value for s in
                                         {State.ROOT_CAUSE_FOUND, State.REMEDIATING, State.VERIFYING,
                                          State.PR_OPENED, State.READY_FOR_REVIEW}} and not self.store.get_sessions(
                                              workflow["id"], Role.ANALYST
                                          ):
                    await self._create_role_session(workflow, Role.ANALYST)
                    capacity -= 1
        for workflow in workflows:
            if capacity <= 0:
                break
            if workflow["state"] == State.QUEUED.value and not self.store.get_sessions(
                workflow["id"], Role.INVESTIGATOR, active_only=True
            ):
                self.store.set_state(workflow["id"], State.TRIAGING, started_at=utcnow())
                self.store.set_state(workflow["id"], State.INVESTIGATING)
                await self._create_role_session(workflow, Role.INVESTIGATOR)
                capacity -= 1

    async def _create_role_session(self, workflow: dict, role: Role):
        issue = await self.github.get_issue(workflow["repo"], workflow["issue_number"])
        comments = await self.github.list_issue_comments(workflow["repo"], workflow["issue_number"])
        inv = workflow.get("investigation") or {}
        if role == Role.INVESTIGATOR:
            prompt, schema, limit = investigator_prompt(workflow["repo"], issue, comments), INVESTIGATION_SCHEMA, self.settings.investigator_acu_limit
        elif role == Role.REMEDIATOR:
            investigator = next(iter(self.store.get_sessions(workflow["id"], Role.INVESTIGATOR)), {})
            prompt, schema, limit = remediator_prompt(workflow["repo"], issue, inv, investigator.get("url", "")), REMEDIATION_SCHEMA, self.settings.remediator_acu_limit
            self.store.set_state(workflow["id"], State.REMEDIATING, remediation_started_at=utcnow())
        else:
            prompt, schema, limit = analyst_prompt(workflow["repo"], issue, inv), ANALYSIS_SCHEMA, self.settings.analyst_acu_limit
        tags = ["superset-x-devin", f"repo:{workflow['repo']}",
                f"issue:{workflow['issue_number']}", f"role:{role.value}"]
        session = await self.devin.create_session(
            prompt, title=f"[{role.value}] {workflow['repo']}#{workflow['issue_number']}: {workflow.get('title', '')[:60]}",
            tags=tags, structured_output_schema=schema,
            max_acu_limit=limit if self.settings.max_acu_limit is None else min(limit, self.settings.max_acu_limit),
        )
        self.store.record_session(session_id=session["session_id"], workflow_id=workflow["id"],
                                  role=role.value, url=session.get("url", ""), active=1)
        self.store.add_event(workflow["id"], "session_created", detail={"session_id": session["session_id"],
                                                                        "role": role.value})

    async def _comment(self, workflow: dict, body: str):
        if self.settings.dry_run:
            logger.info("[dry-run] would comment on #%s: %s", workflow["issue_number"], body)
            self.store.add_event(workflow["id"], "comment_posted", detail=body)
            return
        comment = await self.github.post_issue_comment(workflow["repo"], workflow["issue_number"], body)
        self.store.add_event(workflow["id"], "comment_posted", detail={"comment_id": comment.get("id")})

    @staticmethod
    def _pull_url(pulls) -> str | None:
        for pull in pulls or []:
            if isinstance(pull, str):
                logger.info("pull_requests item shape: str")
                return pull
            if isinstance(pull, dict) and pull.get("url"):
                logger.info("pull_requests item shape: dict keys=%s", sorted(pull))
                return pull["url"]
        if pulls:
            logger.info("pull_requests item shape: %s", type(pulls[0]).__name__)
        return None

    @staticmethod
    def _pr_number(url: str | None) -> int | None:
        match = re.search(r"/pull/(\d+)", url or "")
        return int(match.group(1)) if match else None

    async def handle_issue_event(self, payload: dict) -> dict:
        if payload.get("action") not in {"opened", "reopened"}:
            return {"handled": False, "reason": "unsupported action"}
        issue = payload.get("issue") or {}
        repo = (payload.get("repository") or {}).get("full_name", self.settings.target_repo)
        if repo != self.settings.target_repo or "pull_request" in issue:
            return {"handled": False, "reason": "ineligible repository or pull request"}
        if self.settings.eligibility_label and self.settings.eligibility_label not in _labels(issue):
            return {"handled": False, "reason": "not eligible"}
        workflow = self.store.get_workflow(repo, issue["number"]) or self.store.upsert_workflow(
            repo, issue["number"], title=issue.get("title", ""), issue_url=issue.get("html_url", ""),
            author=(issue.get("user") or {}).get("login", ""), labels=list(_labels(issue)),
            state=State.QUEUED.value, discovered_at=utcnow(),
        )
        if workflow["state"] == State.DISCOVERED.value:
            self.store.set_state(workflow["id"], State.QUEUED)
        await self.tick()
        return {"handled": True, "workflow_id": workflow["id"]}

    async def handle_issue_comment_event(self, payload: dict) -> dict:
        if payload.get("action") != "created":
            return {"handled": False, "reason": "not a created comment"}
        await self.tick()
        return {"handled": True}
