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
from .store import Store
from .workflow import WorkflowEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("webhook")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(settings.db_path)
    github = GitHubClient(settings.github_token, settings.github_api_url)
    devin = DevinClient("" if settings.dry_run else settings.devin_api_key,
                        settings.devin_api_base_url, settings.devin_org_id)
    engine = WorkflowEngine(settings, store, github, devin)
    app.state.engine, app.state.store = engine, store
    app.state.tick_lock = asyncio.Lock()
    if settings.dry_run:
        logger.warning("DRY_RUN is on — external writes are disabled")
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
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "target_repo": settings.target_repo, "dry_run": settings.dry_run}


@app.post("/admin/poll-now")
async def poll_now(request: Request):
    async with request.app.state.tick_lock:
        await request.app.state.engine.tick()
    return {"workflows": len(request.app.state.store.list_workflows()),
            "active_sessions": request.app.state.store.count_active_sessions(),
            "ran_at": datetime.now(timezone.utc).isoformat()}


def _session_summary(store, workflow_id):
    return [{"id": s["session_id"], "url": s.get("url"), "role": s["role"],
             "status": s.get("devin_status"), "acus": s.get("acus")}
            for s in store.get_sessions(workflow_id)]


@app.get("/api/workflows")
async def list_workflows(request: Request):
    store = request.app.state.store
    now = datetime.now(timezone.utc)
    result = []
    for row in store.list_workflows():
        item = {k: row.get(k) for k in ("id", "repo", "issue_number", "title", "state",
                                        "discovered_at", "started_at", "investigated_at",
                                        "reproduced_at", "root_cause_at",
                                        "remediation_started_at", "pr_opened_at",
                                        "completed_at", "waiting_since", "pr_url", "updated_at")}
        if row["state"] in {"NEEDS_INFO", "BLOCKED"} and row.get("waiting_since"):
            item["waiting_for_seconds"] = (now - datetime.fromisoformat(row["waiting_since"])).total_seconds()
        else:
            item["waiting_for_seconds"] = None
        item["sessions"] = _session_summary(store, row["id"])
        result.append(item)
    return result


@app.get("/api/workflows/{repo_owner}/{repo}/{number}")
async def workflow_detail(repo_owner: str, repo: str, number: int, request: Request):
    store = request.app.state.store
    row = store.get_workflow(f"{repo_owner}/{repo}", number)
    if not row:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {**row, "events": store.get_events(row["id"]), "sessions": store.get_sessions(row["id"])}


@app.get("/api/metrics")
async def api_metrics(request: Request):
    return metrics(request.app.state.store, settings)


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
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
    if event == "ping":
        return {"handled": True, "reason": "ping"}
    return {"handled": False, "reason": f"unsupported event {event}"}
