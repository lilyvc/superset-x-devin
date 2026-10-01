import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.devin_client import DevinClient
from app.gates import investigation_gate, verification_gate
from app.handlers import SettledSession, handle_analyst
from app.parsing import issue_number_from_url
from app.states import State
from app.store import Store
from app.workflow import WorkflowEngine


def _investigation():
    return {
        "status": "REPRODUCED", "enough_information": True, "reproduced": True,
        "expected_behavior": "expected", "observed_behavior": "observed",
        "reproduction_steps": ["step"], "reproduction_evidence": ["evidence"],
        "root_cause": "cause", "verification_plan": ["test"], "summary": "summary",
    }


def _remediation():
    return {
        "status": "PR_OPENED", "pr_url": "https://github.com/x/y/pull/4",
        "verification_passed": True, "reproduction_rerun_passed": True,
        "tests_executed": [{"command": "pytest", "result": "passed"}],
    }


@pytest.mark.parametrize("field", [
    "status", "enough_information", "reproduced", "expected_behavior",
    "observed_behavior", "reproduction_steps", "reproduction_evidence",
    "root_cause", "verification_plan",
])
def test_investigation_gate_each_missing_field(field):
    output = _investigation()
    if field in {"status", "expected_behavior", "observed_behavior", "root_cause"}:
        output[field] = "" if field != "status" else "BLOCKED"
    elif field in {"reproduction_steps", "reproduction_evidence", "verification_plan"}:
        output[field] = []
    else:
        output[field] = False
    ok, reasons = investigation_gate(output)
    assert not ok and reasons


@pytest.mark.parametrize("field", [
    "status", "verification_passed", "reproduction_rerun_passed", "tests_executed", "pr_url",
])
def test_verification_gate_each_missing_field(field):
    output = _remediation()
    pr_url = output["pr_url"]
    if field == "pr_url":
        pr_url = None
    elif field == "tests_executed":
        output[field] = [{"command": "pytest", "result": "failed"}]
    elif field == "status":
        output[field] = "VERIFICATION_FAILED"
    else:
        output[field] = False
    ok, reasons = verification_gate(output, pr_url)
    assert not ok and reasons


class FakeGitHub:
    def __init__(self, issues, pulls=None):
        self.issues = issues
        self.pulls = pulls or []
        self.comments = {i["number"]: [] for i in issues}
        self.review_comments = {}
        self.reviews = {}
        self.posted = []
        self.pull = {"state": "open", "merged": False, "head": {"sha": "abc123"}}
        self.checks = {
            "check_runs": [
                {"name": "unit tests", "status": "completed", "conclusion": "success"}
            ],
            "statuses": [],
        }

    async def list_open_issues(self, repo):
        return self.issues

    async def get_issue(self, repo, number):
        return next(i for i in self.issues if i["number"] == number)

    async def get_pull(self, repo, number):
        return self.pull

    async def list_open_pulls(self, repo):
        return self.pulls

    async def list_pull_files(self, repo, number):
        return []

    async def get_commit_checks(self, repo, ref):
        return self.checks

    async def list_issue_comments(self, repo, number):
        return self.comments.get(number, [])

    async def list_pull_review_comments(self, repo, number):
        return self.review_comments.get(number, [])

    async def list_pull_reviews(self, repo, number):
        return self.reviews.get(number, [])

    async def post_issue_comment(self, repo, number, body):
        self.posted.append((number, body))
        return {"id": len(self.posted), "user": {"type": "Bot"}}

    async def get_authenticated_user(self):
        return {"login": "automation"}


class FakeDevin:
    """Stand-in for DevinClient: returns a canned verdict per role."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self._counter = 0
        self.terminated: list[str] = []
        self.adoptable: dict | None = None

    async def create_session(self, prompt, *, title=None, tags=None,
                             structured_output_schema=None, max_acu_limit=None,
                             repos=None, devin_mode=None):
        self._counter += 1
        session_id = f"fake-session-{self._counter}"
        role = next((tag.split(":", 1)[1] for tag in tags or []
                     if tag.startswith("role:")), "investigator")
        if role == "triage":
            output = {
                "verdict": "ACTIONABLE", "issue_kind": "bug",
                "suspected_area": "fake", "rationale": "Fake triage verdict",
            }
        elif role == "investigator":
            output = {
                "status": "REPRODUCED", "enough_information": True, "reproduced": True,
                "expected_behavior": "Expected behavior", "observed_behavior": "Observed behavior",
                "reproduction_steps": ["Run the reported scenario"],
                "reproduction_evidence": ["Fake evidence"], "root_cause": "Fake root cause",
                "verification_plan": ["Run the regression test"], "summary": "Fake investigation",
            }
        elif role == "remediator":
            output = {
                "status": "PR_OPENED", "pr_url": "https://github.com/example/fake/pull/1",
                "verification_passed": True, "reproduction_rerun_passed": True,
                "tests_executed": [{"command": "fake", "result": "passed"}],
                "verification_evidence": ["Fake evidence"], "summary": "Fake remediation",
            }
        elif role == "dedup":
            output = {"verdict": "PROCEED", "duplicate_pr_url": None,
                      "rationale": "Fake dedup verdict"}
        else:
            output = {"systemic_risk": "none", "summary": "Fake analysis",
                      "recommended_followup": "NONE", "followup_issue_url": None}
        session = {"session_id": session_id, "url": "https://app.devin.ai/fake/" + session_id,
                   "repos": repos or [], "status": "exit", "structured_output": output,
                   "acus_consumed": 0.0, "pull_requests": []}
        self.sessions[session_id] = session
        return session

    async def get_session(self, session_id):
        return self.sessions[session_id]

    async def send_message(self, session_id, message):
        if session_id not in self.sessions:
            raise RuntimeError(f"unknown session {session_id}: cannot resume")
        return {"ok": True}

    async def adopt_session(self, tags):
        return self.adoptable

    async def terminate_session(self, session_id, *, archive=False):
        self.terminated.append(session_id)

    async def aclose(self):
        pass


def _settings(tmp_path, **kwargs):
    defaults = {"target_repo": "owner/repo", "db_path": str(tmp_path / "test.db"),
                "poll_backlog": True, "eligibility_label": "",
                "max_concurrent_devins": 3, "triage_enabled": False,
                "max_new_issues_per_poll": 10, "ignore_labels": (), "ignore_issue_types": ()}
    defaults.update(kwargs)
    return Settings(**defaults)


def test_state_machine_restart_safe(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.REMEDIATING.value
    sessions = store.get_sessions(1)
    assert {s["role"] for s in sessions} == {"investigator", "remediator"}
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.READY_FOR_REVIEW.value
    # The analyst only runs once a fix PR exists.
    assert "analyst" in {s["role"] for s in store.get_sessions(1)}
    count = len(store.get_sessions(1))
    restarted = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(restarted.tick())
    assert len(store.get_sessions(1)) == count


def test_comment_failure_does_not_rewind_state(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])

    async def forbidden(repo, number, body):
        raise RuntimeError("403 Resource not accessible by personal access token")

    github.post_issue_comment = forbidden
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.REMEDIATING.value
    remediator = store.get_sessions(1, active_only=True)[0]
    assert remediator["role"] == "remediator"
    devin.sessions[remediator["session_id"]]["status"] = "running"
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.REMEDIATING.value
    assert any(e["kind"] == "comment_failed" for e in store.get_events(1))


def test_needs_info_reply_resumes_investigator(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    # Replace canned output with a clarification state for this targeted path.
    session = store.get_sessions(1)[0]
    devin.sessions[session["session_id"]]["structured_output"] = {
        "status": "NEEDS_INFO", "enough_information": False,
        "clarification_question": "Which environment?", "summary": "Need environment",
    }
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.NEEDS_INFO.value
    github.comments[1].append({"id": 9, "body": "Linux", "user": {"login": "human"}})
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value


def test_waiting_empty_output_question_stays_needs_info(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    response = devin.sessions[session["session_id"]]
    response.update(status="suspended", status_detail="waiting_for_user",
                    structured_output={},
                    messages=[{"type": "devin_message", "message": "Which database?"}])
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.NEEDS_INFO.value
    assert "Which database?" in github.posted[-1][1]
    response["status_detail"] = "inactivity"
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.NEEDS_INFO.value


def test_waiting_empty_output_uses_devin_question(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    devin.sessions[session["session_id"]].update(
        status="suspended", status_detail="waiting_for_user",
        structured_output={},
        messages=[{"type": "devin_message", "message": "Please share the version."}],
    )
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.NEEDS_INFO.value
    assert "Please share the version." in github.posted[-1][1]


def test_merged_pr_reaches_completed(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.READY_FOR_REVIEW.value
    github.pull = {"state": "closed", "merged": True}
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.COMPLETED.value


def _issue(number=1, **kwargs):
    issue = {"number": number, "title": "Bug", "body": "body",
             "html_url": f"https://github.com/owner/repo/issues/{number}",
             "user": {"login": "reporter"}, "labels": []}
    issue.update(kwargs)
    return issue


def test_autonomous_intake_triages_then_investigates(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True)
    github = FakeGitHub([_issue()])
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.TRIAGING.value
    assert [s["role"] for s in store.get_sessions(workflow["id"])] == ["triage"]
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value
    assert {s["role"] for s in store.get_sessions(workflow["id"])} == {"triage", "investigator"}


def test_triage_skip_marks_skipped_with_reason(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True)
    github = FakeGitHub([_issue()])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    devin.sessions[session["session_id"]]["structured_output"] = {
        "verdict": "SKIP", "issue_kind": "question", "skip_reason": "NOT_ENGINEERING",
        "rationale": "Support question about configuring OAuth.",
    }
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.SKIPPED.value
    assert "NOT_ENGINEERING" in workflow["failure_reason"]
    assert [s["role"] for s in store.get_sessions(workflow["id"])] == ["triage"]
    asyncio.run(engine.tick())
    assert [s["role"] for s in store.get_sessions(workflow["id"])] == ["triage"]


def test_triage_needs_info_asks_then_reply_queues_investigation(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True)
    github = FakeGitHub([_issue()])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    devin.sessions[session["session_id"]]["structured_output"] = {
        "verdict": "NEEDS_INFO", "issue_kind": "unclear",
        "clarification_question": "Which Superset version?",
        "needs_info_kind": "NEEDS_REPORTER_INFO", "rationale": "No version given.",
    }
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.NEEDS_INFO.value
    github.comments[1].append({"id": 9, "body": "5.0", "user": {"login": "human"}})
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value


def test_analysis_labeled_issue_is_devin_discovered_not_ignored(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True)
    github = FakeGitHub([_issue(labels=[{"name": "devin-analysis"}])])
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    # triaged and fixed like any issue — not filtered out
    assert workflow["state"] == State.TRIAGING.value
    assert workflow["origin"] == "DEVIN_DISCOVERED"


def test_intake_skips_without_spending_devin(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True, ignore_labels=("question",),
                        ignore_issue_types=("feature",))
    issues = [_issue(1, labels=[{"name": "question"}]),
              _issue(2, type={"name": "Feature"}),
              _issue(3)]
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub(issues), FakeDevin())
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.SKIPPED.value
    assert store.get_workflow("owner/repo", 2)["state"] == State.SKIPPED.value
    assert store.get_workflow("owner/repo", 3)["state"] == State.TRIAGING.value
    assert store.count_active_sessions() == 1


def test_max_new_issues_per_poll(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True, max_new_issues_per_poll=2,
                         max_concurrent_devins=10)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue(i) for i in range(1, 6)]),
                            FakeDevin())
    asyncio.run(engine.tick())
    assert len(store.list_workflows()) == 2
    asyncio.run(engine.tick())
    assert len(store.list_workflows()) == 4


def test_lookback_limit_skips_old_issues(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True, issue_lookback_days=30)
    old = _issue(1, created_at="2020-01-01T00:00:00+00:00")
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub([old]), FakeDevin())
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.SKIPPED.value
    assert "lookback" in workflow["failure_reason"]
    assert store.count_active_sessions() == 0


def test_concurrency_cap(tmp_path):
    issues = [{"number": i, "title": f"Bug {i}", "body": "",
               "html_url": f"https://github.com/owner/repo/issues/{i}",
               "user": {"login": "reporter"}, "labels": []} for i in range(1, 6)]
    settings = _settings(tmp_path, max_concurrent_devins=2)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub(issues), FakeDevin())
    asyncio.run(engine.tick())
    assert store.count_active_sessions() == 2
    assert len(store.get_sessions(store.list_workflows()[0]["id"])) == 1


def _run_to_ci_checking(tmp_path, github=None, **settings_kwargs):
    """Drive the canned pipeline to the CI gate and return the pieces."""
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path, **settings_kwargs)
    github = github or FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    return settings, github, store, devin, engine


def test_ready_for_review_requires_green_ci(tmp_path):
    """A failing or absent CI must never surface as ready for review."""
    github = FakeGitHub([_issue()])
    github.checks = {"check_runs": [
        {"name": "unit tests", "status": "completed", "conclusion": "failure"},
    ], "statuses": []}
    _, github, store, _, engine = _run_to_ci_checking(tmp_path, github=github)
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.CI_CHECKING.value
    assert workflow["ci_status"] == "failed"
    comments = [e["detail"] for e in store.get_events(1) if e["kind"] == "comment_posted"
                and "CI checks are failing" in e["detail"]]
    assert len(comments) == 1
    # A second poll must not re-post the failure comment.
    asyncio.run(engine.tick())
    comments = [e["detail"] for e in store.get_events(1) if e["kind"] == "comment_posted"
                and "CI checks are failing" in e["detail"]]
    assert len(comments) == 1
    # Green CI then promotes the PR.
    github.checks = {"check_runs": [
        {"name": "unit tests", "status": "completed", "conclusion": "success"},
    ], "statuses": []}
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.READY_FOR_REVIEW.value
    assert workflow["ci_status"] == "passed"


def test_ci_unverified_stays_in_checking(tmp_path):
    github = FakeGitHub([_issue()])
    github.checks = {"check_runs": [], "statuses": []}
    _, _, store, _, _ = _run_to_ci_checking(tmp_path, github=github)
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.CI_CHECKING.value
    assert workflow["ci_status"] == "unverified"


def test_ci_not_required_goes_straight_to_review(tmp_path):
    github = FakeGitHub([_issue()])
    _, _, store, _, _ = _run_to_ci_checking(tmp_path, github=github, ci_required=False)
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.READY_FOR_REVIEW.value
    assert workflow["ci_status"] == "skipped"


def test_dedup_skips_when_pr_already_links_issue(tmp_path):
    pulls = [{"number": 7, "title": "fix: aggregate NULL groups",
              "body": "## Related issue\nFixes #1",
              "html_url": "https://github.com/owner/repo/pull/7"}]
    github = FakeGitHub([_issue()], pulls=pulls)
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.SKIPPED.value
    assert workflow["needs_info_kind"] == "DUPLICATE"
    roles = {s["role"] for s in store.get_sessions(workflow["id"])}
    assert "remediator" not in roles
    comments = [e["detail"] for e in store.get_events(1) if e["kind"] == "comment_posted"]
    assert any("duplicate remediation avoided" in c for c in comments)


def test_dedup_ambiguous_pr_uses_dedup_session(tmp_path):
    pulls = [{"number": 7, "title": "fix root cause handling",
              "body": "changes the root cause code path",
              "html_url": "https://github.com/owner/repo/pull/7"}]
    github = FakeGitHub([_issue()], pulls=pulls)
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    roles = {s["role"] for s in store.get_sessions(workflow["id"])}
    # The ambiguous match spawned a dedup session (canned verdict PROCEED),
    # which then cleared the gate and let the remediator dispatch.
    assert "dedup" in roles
    assert "remediator" in roles
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "dedup_candidates" in kinds
    assert "dedup_cleared" in kinds
    assert workflow["state"] in (State.ROOT_CAUSE_FOUND.value, State.REMEDIATING.value)


def test_blocked_reply_resumes_finished_session(tmp_path):
    github = FakeGitHub([_issue()])
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    store.update_session(session["session_id"], active=0)
    store.set_state(1, State.BLOCKED, failure_reason="waiting on environment info",
                    waiting_since="2026-01-01T00:00:00+00:00")
    github.comments[1].append({"id": 9, "body": "It's Postgres 16",
                               "user": {"login": "human"}})
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.INVESTIGATING.value
    assert store.get_session(session["session_id"])["active"] == 1
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "human_reply_resumed_session" in kinds
    assert "human_comment_unforwarded" not in kinds


def test_pr_comment_resumes_finished_remediator(tmp_path):
    github = FakeGitHub([_issue()])
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    for _ in range(3):
        asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.READY_FOR_REVIEW.value
    remediator = next(s for s in store.get_sessions(1) if s["role"] == "remediator")
    store.update_session(remediator["session_id"], active=0)
    github.comments[1].append({"id": 9, "body": "please use dropna=False",
                               "user": {"login": "reviewer"}})
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.REMEDIATING.value
    assert store.get_session(remediator["session_id"])["active"] == 1
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "pr_comment_resumed_session" in kinds


def test_pr_review_comment_forwards_to_active_remediator(tmp_path):
    github = FakeGitHub([_issue()])
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    asyncio.run(engine.tick())
    remediator = next(s for s in store.get_sessions(1) if s["role"] == "remediator")
    store.update_session(remediator["session_id"], active=1)
    store.set_state(1, State.REMEDIATING, pr_number=5,
                    pr_url="https://github.com/owner/repo/pull/5")
    github.review_comments[5] = [
        {"id": 3, "body": "use dropna=False here", "path": "a.py", "line": 10,
         "user": {"login": "reviewer"}},
    ]
    asyncio.run(engine._replies())
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "pr_comment_forwarded" in kinds
    assert store.get_workflow("owner/repo", 1)["state"] == State.REMEDIATING.value


def test_pr_review_body_and_bot_filtering(tmp_path):
    github = FakeGitHub([_issue()])
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    for _ in range(3):
        asyncio.run(engine.tick())
    store.set_state(1, State.READY_FOR_REVIEW, pr_number=5,
                    pr_url="https://github.com/owner/repo/pull/5")
    github.reviews[5] = [
        {"id": 7, "body": "fix the null handling", "state": "CHANGES_REQUESTED",
         "user": {"login": "reviewer"}},
        {"id": 8, "body": "looks good", "state": "APPROVED",
         "user": {"login": "reviewer2"}},
        {"id": 9, "body": "ci noise", "state": "COMMENTED",
         "user": {"login": "ci[bot]", "type": "Bot"}},
    ]
    asyncio.run(engine._replies())
    events = [e for e in store.get_events(1)
              if e["kind"].startswith("pr_comment_")]
    # only the changes-requested review is forwarded; approval and bot skipped
    assert len(events) == 1 and events[0]["kind"] == "pr_comment_resumed_session"
    # a second pass forwards nothing new — the cursor advanced past all three
    asyncio.run(engine._replies())
    events = [e for e in store.get_events(1)
              if e["kind"].startswith("pr_comment_")]
    assert len(events) == 1


def test_blocked_reply_recovers_with_fresh_session(tmp_path):
    github = FakeGitHub([_issue()])
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    store.update_session(session["session_id"], active=0)
    # The session can no longer be resumed (expired/deleted upstream).
    del devin.sessions[session["session_id"]]
    store.set_state(1, State.BLOCKED, failure_reason="needed the DB version",
                    waiting_since="2026-01-01T00:00:00+00:00")
    github.comments[1].append({"id": 9, "body": "It's Postgres 16",
                               "user": {"login": "human"}})
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.INVESTIGATING.value
    sessions = store.get_sessions(1)
    assert len(sessions) == 2
    assert sessions[-1]["role"] == "investigator"
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "human_reply_recovery_session" in kinds


def test_not_reproducible_reply_resumes_investigator(tmp_path):
    github = FakeGitHub([_issue()])
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    investigator = next(s for s in store.get_sessions(1)
                        if s["role"] == "investigator")
    store.update_session(investigator["session_id"], active=0)
    store.set_state(1, State.NOT_REPRODUCIBLE,
                    investigation={"summary": "tried the steps", "reproduced": False})
    github.comments[1].append({"id": 9, "body": "it only happens on Postgres 14",
                               "user": {"login": "human"}})
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.INVESTIGATING.value
    assert store.get_session(investigator["session_id"])["active"] == 1
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "human_reply_resumed_session" in kinds


def test_acu_budget_ceiling_stops_dispatch(tmp_path):
    settings = _settings(tmp_path, max_total_acus=0)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue()]), FakeDevin())
    asyncio.run(engine.tick())
    assert store.count_active_sessions() == 0
    assert store.get_workflow("owner/repo", 1)["state"] == State.QUEUED.value


class SpyDevin(FakeDevin):
    def __init__(self):
        super().__init__()
        self.created = []

    async def create_session(self, prompt, **kwargs):
        self.created.append(kwargs)
        return await super().create_session(prompt, **kwargs)


def test_role_devin_modes(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True)
    store = Store(settings.db_path)
    devin = SpyDevin()
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue()]), devin)
    for _ in range(4):
        asyncio.run(engine.tick())
    modes = {}
    for c in devin.created:
        role = next(t.split(":", 1)[1] for t in c["tags"] if t.startswith("role:"))
        modes[role] = c["devin_mode"]
    assert modes["triage"] == "lite"
    assert modes["investigator"] is None
    assert modes["remediator"] == "fusion"
    assert modes["analyst"] is None


class StuckDevin(FakeDevin):
    """A Devin session that keeps running and never reports a verdict."""

    def __init__(self):
        super().__init__()
        self.nudges = []

    async def get_session(self, session_id):
        return {"status": "running", "status_detail": "working", "structured_output": {}}

    async def send_message(self, session_id, message):
        self.nudges.append((session_id, message))
        return {}


def _age_session(store, session_id, hours):
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    store.update_session(session_id, created_at=old)


def test_stalled_session_is_nudged_then_escalated(tmp_path):
    settings = _settings(tmp_path, session_stall_seconds=3600)
    store = Store(settings.db_path)
    github = FakeGitHub([_issue()])
    devin = StuckDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session_id = store.get_sessions(1)[0]["session_id"]

    _age_session(store, session_id, hours=1.5)
    asyncio.run(engine.tick())
    assert len(devin.nudges) == 1
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value

    # A nudge is sent once, not on every tick.
    asyncio.run(engine.tick())
    assert len(devin.nudges) == 1

    _age_session(store, session_id, hours=3)
    asyncio.run(engine.tick())
    workflow = store.get_workflow("owner/repo", 1)
    assert workflow["state"] == State.ESCALATED.value
    assert "stalled" in workflow["failure_reason"]
    assert store.count_active_sessions() == 0


def test_validate_skills_fails_when_missing(tmp_path):
    from app.prompts import validate_skills
    assert validate_skills()  # repo skills directory is complete
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        validate_skills(tmp_path)


def test_issue_origin_propagates_to_workflow(tmp_path):
    settings = _settings(tmp_path)
    github = FakeGitHub([_issue(1), _issue(2, title="related defect")])
    store = Store(settings.db_path)
    store.record_issue_origin("owner/repo", 2, "DEVIN_DISCOVERED",
                            parent_issue_number=1, session_id="sess-ana")
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    asyncio.run(engine.tick())
    child = store.get_workflow("owner/repo", 2)
    assert child["origin"] == "DEVIN_DISCOVERED"
    assert child["parent_issue_number"] == 1
    assert child["discovered_by_session_id"] == "sess-ana"
    assert store.get_workflow("owner/repo", 1)["origin"] == "HUMAN_REPORTED"
    assert [c["issue_number"] for c in store.children_of("owner/repo", 1)] == [2]


def test_analyst_followup_records_issue_origin(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue()]), FakeDevin())
    workflow = store.upsert_workflow("owner/repo", 1, title="bug", state="REMEDIATING")
    row = {"session_id": "sess-ana", "url": "https://app.devin.ai/x"}
    out = {"systemic_risk": "low", "summary": "s", "recommended_followup": "FILE",
           "followup_issue_url": "https://github.com/owner/repo/issues/77"}
    asyncio.run(handle_analyst(
        engine,
        SettledSession(
            row=row, workflow=workflow, output=out, kind="final",
            fingerprint="fp", response={}, pulls=[],
        ),
    ))
    origin = store.get_issue_origin("owner/repo", 77)
    assert origin["origin"] == "DEVIN_DISCOVERED"
    assert origin["parent_issue_number"] == 1
    assert origin["session_id"] == "sess-ana"


def test_issue_number_from_url():
    assert issue_number_from_url("https://github.com/o/r/issues/42") == 42
    assert issue_number_from_url("https://github.com/o/r/pull/7") is None
    assert issue_number_from_url(None) is None


def test_analyst_empty_output_posts_no_comment(tmp_path):
    settings = _settings(tmp_path)
    github = FakeGitHub([_issue()])
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, github, FakeDevin())
    workflow = store.upsert_workflow("owner/repo", 1, title="bug", state="REMEDIATING")
    row = {"session_id": "sess-ana", "url": "https://app.devin.ai/x"}
    asyncio.run(handle_analyst(
        engine,
        SettledSession(
            row=row, workflow=workflow, output={}, kind="final",
            fingerprint="fp", response={}, pulls=[],
        ),
    ))
    assert not github.comments.get(1)


def test_known_defects_loop_reaches_prompts(tmp_path):
    from app.prompts import analyst_prompt, investigator_prompt
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    store.upsert_workflow(
        "owner/repo", 9, title="null groupby",
        analysis={"summary": "pandas groupby drops NULL keys by default",
                  "systemic_risk": "high"},
    )
    analyses = store.recent_analyses("owner/repo")
    assert len(analyses) == 1
    issue = {"number": 10, "title": "similar bug", "html_url": "https://x",
             "body": "b", "labels": []}
    inv = investigator_prompt("owner/repo", issue, [], analyses)
    assert "issue #9" in inv and "NULL keys" in inv
    ana = analyst_prompt("owner/repo", issue, {}, analyses)
    assert "issue #9" in ana
    # Empty history: prompts stay clean, no dangling header.
    assert "{known_defects}" not in inv
    assert "Defect families" not in investigator_prompt("owner/repo", issue, [], [])


def test_render_comments_caps_context():
    from app.prompts import render_comments
    comments = [{"user": {"login": "u"}, "body": f"c{i}"} for i in range(40)]
    rendered = render_comments(comments)
    assert "c14" not in rendered and "c39" in rendered
    assert rendered.count("**u**") == 25


def test_remote_session_is_adopted_instead_of_created(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FakeDevin()
    devin.adoptable = {
        "session_id": "sess-remote", "status": "running",
        "url": "https://app.devin.ai/sessions/sess-remote",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue()]), devin)
    asyncio.run(engine.tick())
    sessions = store.get_sessions(1)
    assert [s["session_id"] for s in sessions] == ["sess-remote"]
    kinds = {e["kind"] for e in store.get_events(1)}
    assert "session_adopted" in kinds and "session_created" in kinds


def test_create_failure_falls_back_to_adoption(tmp_path):
    class FlakyDevin(FakeDevin):
        async def create_session(self, prompt, **kwargs):
            raise RuntimeError("response lost")

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    devin = FlakyDevin()
    devin.adoptable = {
        "session_id": "sess-remote", "status": "running",
        "url": "https://app.devin.ai/sessions/sess-remote",
    }
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue()]), devin)
    asyncio.run(engine.tick())
    assert [s["session_id"] for s in store.get_sessions(1)] == ["sess-remote"]


def test_stalled_session_is_terminated_remotely(tmp_path):
    settings = _settings(tmp_path, session_stall_seconds=3600)
    store = Store(settings.db_path)
    github = FakeGitHub([_issue()])
    devin = StuckDevin()
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session_id = store.get_sessions(1)[0]["session_id"]
    _age_session(store, session_id, hours=3)
    asyncio.run(engine.tick())
    assert devin.terminated == [session_id]
    assert store.get_workflow("owner/repo", 1)["state"] == State.ESCALATED.value


def test_concurrent_ticks_are_serialized(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub([_issue()]), FakeDevin())
    entered = []
    original = engine._tick
    gate = asyncio.Event()

    async def spy():
        entered.append("start")
        await gate.wait()
        await original()

    engine._tick = spy

    async def run():
        first = asyncio.create_task(engine.tick())
        while not entered:
            await asyncio.sleep(0)
        second = asyncio.create_task(engine.tick())
        await asyncio.sleep(0.05)
        # The second tick must be blocked on the engine's internal lock.
        assert len(entered) == 1
        gate.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert entered == ["start", "start"]


def test_devin_client_retries_transient_errors():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"session_id": "s1", "url": "u"})

    client = DevinClient("k", org_id="org-x",
                         transport=httpx.MockTransport(handler))
    client._sleep = lambda attempt, resp=None: asyncio.sleep(0)
    out = asyncio.run(client.get_session("s1"))
    assert out["session_id"] == "s1" and len(calls) == 3
    asyncio.run(client.aclose())


def test_devin_client_gives_up_after_max_retries():
    def handler(request):
        return httpx.Response(503)

    client = DevinClient("k", org_id="org-x", max_retries=2,
                         transport=httpx.MockTransport(handler))
    client._sleep = lambda attempt, resp=None: asyncio.sleep(0)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.get_session("s1"))
    asyncio.run(client.aclose())


def test_create_session_is_never_auto_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    client = DevinClient("k", org_id="org-x",
                         transport=httpx.MockTransport(handler))
    client._sleep = lambda attempt, resp=None: asyncio.sleep(0)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.create_session("p"))
    # A lost create response may hide a live remote session — the engine
    # adopts by tags instead of the transport re-POSTing blindly.
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert "idempotent" not in body
    asyncio.run(client.aclose())


def test_v3_paths_use_prefixed_devin_id():
    urls = []

    def handler(request):
        urls.append(request.url.path)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"session_id": "devin-abc"})

    client = DevinClient("k", org_id="org-x",
                         transport=httpx.MockTransport(handler))
    asyncio.run(client.get_session("abc"))
    asyncio.run(client.terminate_session("devin-def"))
    assert urls == [
        "/v3/organizations/org-x/sessions/devin-abc",
        "/v3/organizations/org-x/sessions/devin-def",
    ]
    asyncio.run(client.aclose())


def test_v3_list_sessions_paginates_and_filters_by_tags():
    pages = {
        None: {
            "items": [
                {"session_id": "devin-1", "tags": ["repo:x", "issue:1"],
                 "status": "running", "created_at": "2026-01-01"},
                {"session_id": "devin-2", "tags": ["repo:x"],
                 "status": "running", "created_at": "2026-01-02"},
            ],
            "has_next_page": True,
            "end_cursor": "c1",
        },
        "c1": {
            "items": [
                {"session_id": "devin-3", "tags": ["repo:x", "issue:1"],
                 "status": "suspended", "created_at": "2026-01-03"},
                {"session_id": "devin-4", "tags": ["repo:x", "issue:1"],
                 "status": "error", "created_at": "2026-01-04"},
            ],
            "has_next_page": False,
        },
    }

    def handler(request):
        after = request.url.params.get("after") or None
        return httpx.Response(200, json=pages[after])

    client = DevinClient("k", org_id="org-x",
                         transport=httpx.MockTransport(handler))
    out = asyncio.run(client.list_sessions(tags=["repo:x", "issue:1"]))
    assert [s["session_id"] for s in out] == ["devin-1", "devin-3", "devin-4"]
    # adopt_session skips terminal statuses (v3 "error") and prefers the
    # newest live session — a suspended one is resumable, hence adoptable.
    adopted = asyncio.run(client.adopt_session(["repo:x", "issue:1"]))
    assert adopted["session_id"] == "devin-3"
    asyncio.run(client.aclose())
