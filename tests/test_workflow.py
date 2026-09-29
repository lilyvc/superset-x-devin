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

    async def list_open_issues(self, repo):
        return self.issues

    async def get_issue(self, repo, number):
        return next(i for i in self.issues if i["number"] == number)

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
                "max_concurrent_devins": 3}
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
