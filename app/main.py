"""FastAPI entrypoint for GitHub webhooks and workflow APIs."""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .devin_client import DevinClient
from .github_client import GitHubClient
from .metrics import metrics
from .poller import run_poller
from .prompts import validate_skills
from .states import Role, State
from .store import Store
from .workflow import WorkflowEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("webhook")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if the role-procedure skills are not packaged with the app
    # (e.g. a Docker image built before skills/ was added to the Dockerfile).
    validate_skills()
    store = Store(settings.db_path)
    github = GitHubClient(settings.github_token, settings.github_api_url)
    if not settings.devin_org_id:
        logger.warning(
            "DEVIN_ORG_ID is unset — using the legacy v1 sessions API. "
            "Set DEVIN_ORG_ID for the production v3 path."
        )
    devin = DevinClient(settings.devin_api_key,
                        settings.devin_api_base_url, settings.devin_org_id)
    engine = WorkflowEngine(settings, store, github, devin)
    app.state.engine, app.state.store = engine, store
    poller_task = None
    if settings.enable_polling:
        await engine.tick()
        poller_task = asyncio.create_task(run_poller(engine, settings))
    yield
    if poller_task:
        poller_task.cancel()
    await devin.aclose()
    await github.aclose()


app = FastAPI(title="superset-x-devin", lifespan=lifespan)
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/issues/{number}", include_in_schema=False)
async def issue_page(number: int):
    return FileResponse(STATIC / "issue.html")


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return False  # fail closed: unsigned webhooks are never acceptable
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "target_repo": settings.target_repo}


def _require_admin(request: Request) -> None:
    if not settings.admin_token:
        raise HTTPException(
            status_code=503,
            detail="admin endpoints disabled — set ADMIN_TOKEN to enable them",
        )
    if request.headers.get("authorization") != f"Bearer {settings.admin_token}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/admin/poll-now")
async def poll_now(request: Request):
    _require_admin(request)
    # tick() serializes itself internally — no external lock needed.
    await request.app.state.engine.tick()
    return {"workflows": len(request.app.state.store.list_workflows()),
            "active_sessions": request.app.state.store.count_active_sessions(),
            "ran_at": datetime.now(timezone.utc).isoformat()}


@app.post("/admin/workflows/{repo_owner}/{repo}/{number}/redispatch")
async def redispatch(repo_owner: str, repo: str, number: int, request: Request,
                     force: bool = False, note: str = ""):
    """Operator override: send a dedup- or intake-skipped workflow back into
    remediation. Default: clears prior dedup verdicts and returns the workflow
    to ROOT_CAUSE_FOUND so the next tick re-runs dedup then remediation.
    With ?force=true, the dedup gate is bypassed entirely — a remediator is
    dispatched immediately, with `note` passed to it as operator context (use
    it to scope the fix, e.g. "PR #X already covers component Y — fix only Z").
    """
    _require_admin(request)
    store = request.app.state.store
    workflow = store.get_workflow(f"{repo_owner}/{repo}", number)
    if not workflow:
        raise HTTPException(status_code=404, detail="workflow not found")
    if workflow["state"] != State.SKIPPED.value:
        raise HTTPException(
            status_code=409,
            detail=f"redispatch only applies to SKIPPED workflows, not {workflow['state']}",
        )
    if not workflow.get("root_cause_at"):
        raise HTTPException(
            status_code=409,
            detail="workflow never reached ROOT_CAUSE_FOUND — nothing to remediate",
        )
    removed = store.delete_sessions(workflow["id"], Role.DEDUP)
    store.set_state(workflow["id"], State.ROOT_CAUSE_FOUND,
                    needs_info_kind=None, failure_reason=None,
                    completed_at=None, waiting_since=None)
    if force:
        context = ("An operator has overridden the dedup skip and ordered "
                   "remediation anyway.")
        if note:
            context += f"\nOperator note: {note}"
        session = await request.app.state.engine._create_role_session(
            workflow, Role.REMEDIATOR, extra_context=context)
        store.add_event(workflow["id"], "manual_remediation_dispatch",
                        detail={"note": note or None,
                                "dedup_sessions_cleared": removed})
        return {"workflow_id": workflow["id"], "state": State.REMEDIATING.value,
                "forced": True, "dedup_sessions_cleared": removed,
                "session_id": (session or {}).get("session_id")}
    store.add_event(workflow["id"], "manual_redispatch",
                    detail=f"operator redispatch; cleared {removed} dedup session(s)")
    # tick() serializes itself internally — no external lock needed.
    await request.app.state.engine.tick()
    return {"workflow_id": workflow["id"], "state": State.ROOT_CAUSE_FOUND.value,
            "dedup_sessions_cleared": removed}


def _session_url(s: dict) -> str:
    if s.get("url"):
        return s["url"]
    return ("https://app.devin.ai/sessions/"
            + s["session_id"].removeprefix("devin-"))


def _session_summary(store, workflow_id):
    return [{"id": s["session_id"], "url": _session_url(s), "role": s["role"],
             "status": s.get("devin_status"), "acus": s.get("acus"),
             "active": bool(s.get("active")),
             "created_at": s.get("created_at"), "finished_at": s.get("finished_at")}
            for s in store.get_sessions(workflow_id)]


@app.get("/api/workflows")
async def list_workflows(request: Request):
    store = request.app.state.store
    now = datetime.now(timezone.utc)
    result = []
    for row in store.list_workflows():
        item = {k: row.get(k) for k in ("id", "repo", "issue_number", "title", "state",
                                        "issue_url", "discovered_at", "triaged_started_at",
                                        "started_at", "investigated_at",
                                        "reproduced_at", "root_cause_at",
                                        "remediation_started_at", "pr_opened_at",
                                        "completed_at", "waiting_since", "pr_url",
                                        "ci_status", "updated_at", "origin",
                                        "parent_issue_number", "failure_reason",
                                        "needs_info_kind", "triage", "investigation",
                                        "remediation", "analysis",
                                        "merged_without_changes", "ci_fix_pr_url",
                                        "ci_fix_pr_merged")}
        if row["state"] in {"NEEDS_INFO", "BLOCKED"} and row.get("waiting_since"):
            item["waiting_for_seconds"] = (now - datetime.fromisoformat(row["waiting_since"])).total_seconds()
        else:
            item["waiting_for_seconds"] = None
        item["sessions"] = _session_summary(store, row["id"])
        reached = {e.get("to_state") for e in store.get_events(row["id"])
                   if e.get("to_state")}
        reached.add(row["state"])
        item["reached"] = sorted(reached)
        result.append(item)
    return result


@app.get("/api/workflows/{repo_owner}/{repo}/{number}")
async def workflow_detail(repo_owner: str, repo: str, number: int, request: Request):
    store = request.app.state.store
    row = store.get_workflow(f"{repo_owner}/{repo}", number)
    if not row:
        raise HTTPException(status_code=404, detail="workflow not found")
    parent = (store.get_workflow(f"{repo_owner}/{repo}", row["parent_issue_number"])
              if row.get("parent_issue_number") else None)
    children = [
        {"issue_number": c["issue_number"], "title": c["title"], "state": c["state"],
         "issue_url": c["issue_url"]}
        for c in store.children_of(f"{repo_owner}/{repo}", number)
    ]
    sessions = store.get_sessions(row["id"])
    for s in sessions:
        s["url"] = _session_url(s)
    return {**row, "events": store.get_events(row["id"]),
            "sessions": sessions, "children": children,
            "parent": ({"issue_number": parent["issue_number"], "title": parent["title"],
                        "state": parent["state"], "issue_url": parent["issue_url"]}
                       if parent else None)}


@app.get("/api/metrics")
async def api_metrics(request: Request):
    return metrics(request.app.state.store, settings)


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    if not settings.github_webhook_secret:
        # Fail closed: an unsigned endpoint must not silently accept forged
        # events. Use ENABLE_POLLING or configure GITHUB_WEBHOOK_SECRET.
        raise HTTPException(
            status_code=503,
            detail="GITHUB_WEBHOOK_SECRET not configured; webhook intake disabled",
        )
    if not verify_signature(settings.github_webhook_secret, body,
                            request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="bad signature")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    payload = await request.json()
    store = request.app.state.store
    if delivery_id and not store.mark_delivery(delivery_id):
        return {"handled": False, "reason": "duplicate delivery"}
    event = request.headers.get("X-GitHub-Event", "")
    engine = request.app.state.engine
    if event == "issues" and payload.get("action") in {"opened", "reopened"}:
        return await engine.handle_issue_event(payload)
    if event == "issue_comment" and payload.get("action") == "created":
        return await engine.handle_issue_comment_event(payload)
    # PR feedback surfaces: all three just trigger a tick; _pr_replies picks
    # up the new comments from the API with per-surface cursors.
    if event == "pull_request_review_comment" and payload.get("action") == "created":
        return await engine.handle_issue_comment_event(payload)
    if event == "pull_request_review" and payload.get("action") == "submitted":
        return await engine.handle_issue_comment_event(payload)
    if event == "ping":
        return {"handled": True, "reason": "ping"}
    return {"handled": False, "reason": f"unsupported event {event}"}
