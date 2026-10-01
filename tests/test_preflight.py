import asyncio

import httpx
import pytest

from app.config import Settings
from app.devin_client import DevinClient
from app.preflight import check_setup
from app.store import Store
from app.workflow import WorkflowEngine


class FakeGitHub:
    def __init__(self, repo=None, error=None):
        self.repo = repo or {"has_issues": True}
        self.error = error
        self.calls = []

    async def get_repo(self, repo):
        self.calls.append(repo)
        if self.error:
            raise self.error
        return self.repo


class FakeDevin:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def check_auth(self):
        self.calls += 1
        if self.error:
            raise self.error


def _settings(**kwargs):
    values = {
        "target_repo": "owner/repo",
        "github_token": "github-token",
        "devin_api_key": "devin-key",
        "devin_org_id": "org-test",
        "db_path": ":memory:",
    }
    values.update(kwargs)
    return Settings(**values)


def _status_error(status_code):
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


def test_check_setup_all_good():
    github = FakeGitHub()
    devin = FakeDevin()

    problems = asyncio.run(check_setup(_settings(), github, devin))

    assert problems == []
    assert github.calls == ["owner/repo"]
    assert devin.calls == 1


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("github_token", "GITHUB_TOKEN is not set."),
        ("devin_api_key", "DEVIN_API_KEY is not set."),
        (
            "devin_org_id",
            (
                "DEVIN_ORG_ID is not set, so the service is using the legacy v1 Devin API. "
                "Set it to your org-… id."
            ),
        ),
    ],
)
def test_check_setup_missing_credential(setting, message):
    github = FakeGitHub()
    devin = FakeDevin()

    problems = asyncio.run(check_setup(_settings(**{setting: ""}), github, devin))

    assert message in problems
    if setting == "github_token":
        assert github.calls == []
    if setting == "devin_api_key":
        assert devin.calls == 0


def test_check_setup_github_unauthorized():
    github = FakeGitHub(error=_status_error(401))

    problems = asyncio.run(check_setup(_settings(), github, FakeDevin()))

    assert problems == [
        "GitHub can't read owner/repo (HTTP 401). Check TARGET_REPO and GITHUB_TOKEN."
    ]


def test_check_setup_issues_disabled():
    github = FakeGitHub(repo={"has_issues": False})

    problems = asyncio.run(check_setup(_settings(), github, FakeDevin()))

    assert problems == [
        (
            "Issues are disabled on owner/repo. Enable them in the repo's Settings "
            "→ General → Features."
        )
    ]


def test_check_setup_devin_forbidden():
    github = FakeGitHub()
    devin = FakeDevin(error=_status_error(403))

    problems = asyncio.run(check_setup(_settings(), github, devin))

    assert problems == [
        (
            "The Devin API rejected the credential (HTTP 403). "
            "Check DEVIN_API_KEY and DEVIN_ORG_ID."
        )
    ]


@pytest.mark.parametrize(
    ("org_id", "expected_path", "expected_params"),
    [
        ("org-test", "/v3/organizations/org-test/sessions", {"first": "1"}),
        ("", "/v1/sessions", {"limit": "1"}),
    ],
)
def test_devin_check_auth_uses_correct_api(org_id, expected_path, expected_params):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    client = DevinClient(
        "devin-key",
        org_id=org_id,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.check_auth())

    assert requests[0].url.path == expected_path
    assert dict(requests[0].url.params) == expected_params
    asyncio.run(client.aclose())


def test_check_setup_devin_403_from_client():
    def handler(request):
        return httpx.Response(403, request=request)

    devin = DevinClient(
        "bad-key",
        org_id="org-test",
        transport=httpx.MockTransport(handler),
    )

    problems = asyncio.run(check_setup(_settings(), FakeGitHub(), devin))

    assert problems == [
        (
            "The Devin API rejected the credential (HTTP 403). "
            "Check DEVIN_API_KEY and DEVIN_ORG_ID."
        )
    ]
    asyncio.run(devin.aclose())


def test_tick_errors_reset_after_success(tmp_path):
    class FlakyGitHub:
        def __init__(self):
            self.fail = True

        async def list_open_issues(self, repo):
            if self.fail:
                raise RuntimeError("GitHub unavailable")
            return []

    github = FlakyGitHub()
    settings = _settings(db_path=str(tmp_path / "tick.db"))
    engine = WorkflowEngine(settings, Store(settings.db_path), github, FakeDevin())

    asyncio.run(engine.tick())
    assert engine.tick_errors == ["discovery: GitHub unavailable"]

    github.fail = False
    asyncio.run(engine.tick())
    assert engine.tick_errors == []
