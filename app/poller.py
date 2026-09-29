"""Polling watcher: an alternative to webhooks for repos where you can't (or
don't want to) configure a webhook or expose a public URL.

With ENABLE_POLLING=true the service periodically:
  1. lists open issues on TARGET_REPO and dispatches any created after the
     startup baseline (or the whole backlog with POLL_BACKLOG=true);
  2. lists comments on every issue that already has a Devin session and
     forwards new human comments into that session.

Bot-authored comments (including the orchestrator's own) are never forwarded,
so the loop can't talk to itself.
"""

import asyncio
import logging

from .config import Settings
from .orchestrator import Orchestrator

logger = logging.getLogger("poller")


async def run_poller(orch: Orchestrator, settings: Settings) -> None:
    if settings.poll_backlog:
        seen = 0
    else:
        issues = await orch.github.list_open_issues(settings.target_repo)
        seen = max((i["number"] for i in issues), default=0)
    logger.info(
        "polling %s every %.0fs; baseline issue number = %s",
        settings.target_repo,
        settings.github_poll_interval_seconds,
        seen,
    )
    while True:
        try:
            seen = await _poll_new_issues(orch, settings, seen)
            await _poll_new_comments(orch, settings)
        except Exception:
            logger.exception("poll iteration failed")
        await asyncio.sleep(settings.github_poll_interval_seconds)


async def _poll_new_issues(orch: Orchestrator, settings: Settings, seen: int) -> int:
    issues = await orch.github.list_open_issues(settings.target_repo)
    for issue in sorted(issues, key=lambda i: i["number"]):
        if issue["number"] <= seen or "pull_request" in issue:
            continue
        payload = {
            "action": "opened",
            "issue": issue,
            "repository": {"full_name": settings.target_repo},
        }
        result = await orch.handle_issue_event(payload)
        logger.info("polled new issue #%s -> %s", issue["number"], result)
    if issues:
        seen = max(seen, max(i["number"] for i in issues))
    return seen


async def _poll_new_comments(orch: Orchestrator, settings: Settings) -> None:
    for record in orch.store.list_tracked_issues():
        repo = record["repo"]
        number = record["issue_number"]
        last_id = record.get("last_comment_id") or 0
        comments = await orch.github.list_issue_comments(repo, number)
        for comment in sorted(comments, key=lambda c: c["id"]):
            if comment["id"] <= last_id:
                continue
            last_id = comment["id"]
            user = comment.get("user") or {}
            if user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]"):
                continue
            payload = {
                "action": "created",
                "issue": {"number": number},
                "comment": comment,
                "repository": {"full_name": repo},
            }
            result = await orch.handle_issue_comment_event(payload)
            logger.info("polled comment %s on #%s -> %s", comment["id"], number, result)
        if last_id != (record.get("last_comment_id") or 0):
            orch.store.set_last_comment_id(repo, number, last_id)
