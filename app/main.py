"""FastAPI entrypoint: receives GitHub webhooks and hands them to the orchestrator."""

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .config import get_settings
from .orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("webhook")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.orchestrator = Orchestrator(settings)
    if settings.dry_run:
        logger.warning("DRY_RUN is on — no Devin or GitHub calls will be made")
    yield
    await app.state.orchestrator.devin.aclose()
    await app.state.orchestrator.github.aclose()


app = FastAPI(title="superset-x-devin", lifespan=lifespan)


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret:
        return True  # no secret configured: accept (local dev / simulate)
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "target_repo": settings.target_repo, "dry_run": settings.dry_run}


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    if not verify_signature(
        settings.github_webhook_secret, body, request.headers.get("X-Hub-Signature-256")
    ):
        raise HTTPException(status_code=401, detail="bad signature")

    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    orch: Orchestrator = request.app.state.orchestrator

    if delivery_id and not orch.store.mark_delivery(delivery_id):
        logger.info("duplicate delivery %s, ignoring", delivery_id)
        return {"handled": False, "reason": "duplicate delivery"}

    logger.info("event=%s action=%s delivery=%s", event, payload.get("action"), delivery_id)

    if event == "issues":
        result = await orch.handle_issue_event(payload)
    elif event == "issue_comment":
        result = await orch.handle_issue_comment_event(payload)
    elif event == "ping":
        result = {"handled": True, "reason": "ping"}
    else:
        result = {"handled": False, "reason": f"unsupported event {event}"}

    return result
