import asyncio

import pytest

from app.config import Settings
from app.devin_client import DevinClient
from app.states import State
from app.store import Store
from app.workflow import WorkflowEngine, investigation_gate, verification_gate


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
    def __init__(self, issues):
        self.issues = issues
        self.comments = {i["number"]: [] for i in issues}
        self.posted = []
        self.pull = {"state": "open", "merged": False}

    async def list_open_issues(self, repo):
        return self.issues

    async def get_issue(self, repo, number):
        return next(i for i in self.issues if i["number"] == number)

    async def get_pull(self, repo, number):
        return self.pull

    async def list_issue_comments(self, repo, number):
        return self.comments[number]

    async def post_issue_comment(self, repo, number, body):
        self.posted.append((number, body))
        return {"id": len(self.posted), "user": {"type": "Bot"}}

    async def get_authenticated_user(self):
        return {"login": "automation"}


def _settings(tmp_path, **kwargs):
    defaults = {"target_repo": "owner/repo", "db_path": str(tmp_path / "test.db"),
                "dry_run": True, "poll_backlog": True, "eligibility_label": "",
                "max_concurrent_devins": 3, "triage_enabled": False,
                "max_new_issues_per_poll": 10, "ignore_labels": (), "ignore_issue_types": ()}
    defaults.update(kwargs)
    return Settings(**defaults)


def test_dry_run_state_machine_restart_safe(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = DevinClient("")
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.REMEDIATING.value
    sessions = store.get_sessions(1)
    assert {s["role"] for s in sessions} == {"investigator", "remediator", "analyst"}
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.READY_FOR_REVIEW.value
    count = len(store.get_sessions(1))
    restarted = WorkflowEngine(settings, store, github, DevinClient(""))
    asyncio.run(restarted.tick())
    assert len(store.get_sessions(1)) == count


def test_needs_info_reply_resumes_investigator(tmp_path):
    issue = {"number": 1, "title": "Bug", "body": "body",
             "html_url": "https://github.com/owner/repo/issues/1",
             "user": {"login": "reporter"}, "labels": []}
    settings = _settings(tmp_path)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = DevinClient("")
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    # Replace canned output with a clarification state for this targeted path.
    session = store.get_sessions(1)[0]
    devin._dry_sessions[session["session_id"]]["structured_output"] = {
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
    settings = _settings(tmp_path, dry_run=False)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = DevinClient("")
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    response = devin._dry_sessions[session["session_id"]]
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
    settings = _settings(tmp_path, dry_run=False)
    github = FakeGitHub([issue])
    store = Store(settings.db_path)
    devin = DevinClient("")
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    devin._dry_sessions[session["session_id"]].update(
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
    engine = WorkflowEngine(settings, store, github, DevinClient(""))
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
    engine = WorkflowEngine(settings, store, github, DevinClient(""))
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
    devin = DevinClient("")
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    devin._dry_sessions[session["session_id"]]["structured_output"] = {
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
    devin = DevinClient("")
    engine = WorkflowEngine(settings, store, github, devin)
    asyncio.run(engine.tick())
    session = store.get_sessions(1)[0]
    devin._dry_sessions[session["session_id"]]["structured_output"] = {
        "verdict": "NEEDS_INFO", "issue_kind": "unclear",
        "clarification_question": "Which Superset version?",
        "needs_info_kind": "NEEDS_REPORTER_INFO", "rationale": "No version given.",
    }
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.NEEDS_INFO.value
    github.comments[1].append({"id": 9, "body": "5.0", "user": {"login": "human"}})
    asyncio.run(engine.tick())
    assert store.get_workflow("owner/repo", 1)["state"] == State.INVESTIGATING.value


def test_intake_filters_skip_without_spending_devin(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True, ignore_labels=("question",),
                        ignore_issue_types=("feature",))
    issues = [_issue(1, labels=[{"name": "question"}]),
              _issue(2, type={"name": "Feature"}),
              _issue(3)]
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub(issues), DevinClient(""))
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
                            DevinClient(""))
    asyncio.run(engine.tick())
    assert len(store.list_workflows()) == 2
    asyncio.run(engine.tick())
    assert len(store.list_workflows()) == 4


def test_lookback_limit_skips_old_issues(tmp_path):
    settings = _settings(tmp_path, triage_enabled=True, issue_lookback_days=30)
    old = _issue(1, created_at="2020-01-01T00:00:00+00:00")
    store = Store(settings.db_path)
    engine = WorkflowEngine(settings, store, FakeGitHub([old]), DevinClient(""))
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
    engine = WorkflowEngine(settings, store, FakeGitHub(issues), DevinClient(""))
    asyncio.run(engine.tick())
    assert store.count_active_sessions() == 2
    assert len(store.get_sessions(store.list_workflows()[0]["id"])) == 1
