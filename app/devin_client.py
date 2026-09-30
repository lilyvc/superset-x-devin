"""Thin async client for the Devin API.

Two auth modes:
  - service-user key  -> v1 endpoints (POST /v1/sessions, ...)
  - personal access token (PAT) -> org-scoped v3 endpoints
    (/v3/organizations/{org_id}/sessions, ...). Pass org_id to select v3.

v3 sessions report `status` (new|claimed|running|suspended|resuming|exit|error)
plus a `status_detail` (working|waiting_for_user|inactivity|...) instead of v1's
`status_enum`; the orchestrator handles both.
"""

import itertools

import httpx


class DevinClient:
    def __init__(self, api_key: str, base_url: str = "https://api.devin.ai", org_id: str = ""):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        self._base = f"/v3/organizations/{org_id}" if org_id else "/v1"
        self._v3 = bool(org_id)
        self._dry_run = not api_key
        self._dry_sessions: dict[str, dict] = {}
        self._dry_counter = itertools.count(1)

    async def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        structured_output_schema: dict | None = None,
        max_acu_limit: int | None = None,
    ) -> dict:
        body: dict = {"prompt": prompt, "idempotent": True}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if self._dry_run:
            session_id = f"dry-run-session-{next(self._dry_counter)}"
            role = next((tag.split(":", 1)[1] for tag in tags or [] if tag.startswith("role:")), "investigator")
            if role == "triage":
                output = {
                    "verdict": "ACTIONABLE", "issue_kind": "bug",
                    "suspected_area": "dry-run", "rationale": "Dry-run triage verdict",
                }
            elif role == "investigator":
                output = {
                    "status": "REPRODUCED", "enough_information": True, "reproduced": True,
                    "expected_behavior": "Expected behavior", "observed_behavior": "Observed behavior",
                    "reproduction_steps": ["Run the reported scenario"],
                    "reproduction_evidence": ["Dry-run evidence"], "root_cause": "Dry-run root cause",
                    "verification_plan": ["Run the regression test"], "summary": "Dry-run investigation",
                }
            elif role == "remediator":
                output = {
                    "status": "PR_OPENED", "pr_url": "https://github.com/example/dry-run/pull/1",
                    "verification_passed": True, "reproduction_rerun_passed": True,
                    "tests_executed": [{"command": "dry-run", "result": "passed"}],
                    "verification_evidence": ["Dry-run evidence"], "summary": "Dry-run remediation",
                }
            elif role == "dedup":
                output = {"verdict": "PROCEED", "duplicate_pr_url": None,
                          "rationale": "Dry-run dedup verdict"}
            else:
                output = {"systemic_risk": "none", "summary": "Dry-run analysis",
                          "recommended_followup": "NONE", "followup_issue_url": None}
            session = {"session_id": session_id, "url": "https://app.devin.ai/dry-run/" + session_id,
                       "status": "exit", "structured_output": output,
                       "acus_consumed": 0.0, "pull_requests": []}
            self._dry_sessions[session_id] = session
            return session
        resp = await self._client.post(f"{self._base}/sessions", json=body)
        resp.raise_for_status()
        return resp.json()  # {session_id, url, ...}

    async def get_session(self, session_id: str) -> dict:
        if session_id in self._dry_sessions:
            return self._dry_sessions[session_id]
        resp = await self._client.get(f"{self._base}/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    async def send_message(self, session_id: str, message: str) -> dict | None:
        if session_id in self._dry_sessions:
            return {"ok": True}
        if self._dry_run:
            raise RuntimeError(f"unknown session {session_id}: cannot resume")
        name = "messages" if self._v3 else "message"
        resp = await self._client.post(
            f"{self._base}/sessions/{session_id}/{name}", json={"message": message}
        )
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "A concise markdown summary of the GitHub issue, suitable for posting as a comment.",
        },
        "severity_guess": {
            "type": "string",
            "description": "One of: bug, feature_request, question, docs, chore, unknown.",
        },
        "suggested_next_step": {
            "type": "string",
            "description": "The single most useful next action for a maintainer.",
        },
    },
    "required": ["summary"],
}
