"""The orchestrator: turns GitHub issue events into Devin sessions and
writes results back to GitHub.

Current behaviour (v1):
  issues.opened / issues.reopened
      -> create a Devin session asking it to summarize the issue
      -> poll until the session finishes/blocks
      -> post the summary (or Devin's question, if blocked) as an issue comment
      -> record issue -> session so follow-ups can resume it

  issue_comment.created
      -> if a Devin session is on record for that issue, forward the comment
         to the session (this is how "Devin asked a question and a human
         replied" picks the workflow back up)

Tracked in TODO.md: full dispatch-state idempotency, a remediation-started
label, and dispatching the remediation workflow itself.
"""

import asyncio
import logging
from typing import Any

from .config import Settings
from .devin_client import DevinClient, SUMMARY_SCHEMA
from .github_client import GitHubClient
from .store import Store

logger = logging.getLogger("orchestrator")

FINAL_STATUSES = {"finished", "expired"}
WAITING_STATUSES = {"blocked", "suspend_requested", "suspend_requested_frontend"}

SUMMARY_PROMPT_TEMPLATE = """You are triaging a GitHub issue in the repository {repo}.

Issue #{number}: {title}
Opened by: {author}
URL: {url}

--- issue body ---
{body}
--- end issue body ---

Summarize the issue for a maintainer in a few sentences of markdown: what is
being asked or reported, the likely area of the codebase involved, and the
single most useful next step. Fill in the structured output. Do not modify
any code and do not create branches or pull requests.
"""


def _last_devin_message(session: dict) -> str | None:
    """Best-effort extraction of Devin's latest message text."""
    messages = session.get("messages") or []
    for msg in reversed(messages):
        mtype = (msg.get("type") or "").lower()
        if "devin" in mtype or (msg.get("origin") or "").lower() == "devin":
            return msg.get("message")
    return messages[-1]["message"] if messages else None


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.devin = DevinClient(settings.devin_api_key, settings.devin_api_base_url)
        self.github = GitHubClient(settings.github_token, settings.github_api_url)

    # -- event handlers -----------------------------------------------------

    async def handle_issue_event(self, payload: dict) -> dict:
        action = payload.get("action")
        issue = payload.get("issue") or {}
        repo = (payload.get("repository") or {}).get("full_name") or self.settings.target_repo

        if repo != self.settings.target_repo:
            logger.info("ignoring event for non-target repo %s", repo)
            return {"handled": False, "reason": "non-target repo"}
        if action not in {"opened", "reopened"}:
            logger.info("ignoring issues action %s", action)
            return {"handled": False, "reason": f"action {action}"}
        if "pull_request" in issue:
            return {"handled": False, "reason": "pull request, not an issue"}

        number = issue["number"]
        existing = self.store.get_issue(repo, number)
        if existing and existing.get("session_id") and action == "opened":
            logger.info("issue #%s already dispatched as %s", number, existing["session_id"])
            return {"handled": False, "reason": "already dispatched", "session_id": existing["session_id"]}

        prompt = SUMMARY_PROMPT_TEMPLATE.format(
            repo=repo,
            number=number,
            title=issue.get("title", ""),
            author=(issue.get("user") or {}).get("login", "unknown"),
            url=issue.get("html_url", ""),
            body=issue.get("body") or "(empty)",
        )

        if self.settings.dry_run:
            session = {"session_id": "dry-run-session", "url": "https://app.devin.ai (dry run)"}
            logger.info("[dry-run] would create Devin session for issue #%s", number)
        else:
            session = await self.devin.create_session(
                prompt,
                title=f"Summarize {repo}#{number}",
                tags=["superset-x-devin", f"repo:{repo}", f"issue:{number}"],
                structured_output_schema=SUMMARY_SCHEMA,
                max_acu_limit=self.settings.max_acu_limit,
            )

        self.store.record_dispatch(repo, number, session["session_id"], session.get("url", ""), "dispatched")
        logger.info("dispatched issue #%s -> session %s", number, session["session_id"])

        if self.settings.remediation_label and not self.settings.dry_run:
            try:
                await self.github.add_label(repo, number, self.settings.remediation_label)
            except Exception:
                logger.exception("failed to add label to #%s", number)

        asyncio.create_task(self._await_and_comment(repo, number, session["session_id"]))
        return {"handled": True, "session_id": session["session_id"], "session_url": session.get("url")}

    async def handle_issue_comment_event(self, payload: dict) -> dict:
        """Forward human replies on a tracked issue back into its Devin session."""
        if payload.get("action") != "created":
            return {"handled": False, "reason": "not a created comment"}
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        repo = (payload.get("repository") or {}).get("full_name") or self.settings.target_repo
        if repo != self.settings.target_repo:
            return {"handled": False, "reason": "non-target repo"}

        record = self.store.get_issue(repo, issue.get("number", -1))
        if not record or not record.get("session_id"):
            return {"handled": False, "reason": "no session on record for issue"}

        author = (comment.get("user") or {}).get("login", "unknown")
        body = comment.get("body", "")
        if self.settings.dry_run:
            logger.info("[dry-run] would forward comment by %s to %s", author, record["session_id"])
            return {"handled": True, "session_id": record["session_id"], "dry_run": True}

        await self.devin.send_message(
            record["session_id"],
            f"A reply was posted on GitHub issue #{issue['number']} by {author}:\n\n{body}",
        )
        self.store.update_status(repo, issue["number"], "resumed")
        logger.info("forwarded reply on #%s to session %s", issue["number"], record["session_id"])
        return {"handled": True, "session_id": record["session_id"]}

    # -- internals ----------------------------------------------------------

    async def _await_and_comment(self, repo: str, issue_number: int, session_id: str) -> None:
        try:
            session = await self._poll_session(session_id)
        except Exception:
            logger.exception("polling session %s failed", session_id)
            self.store.update_status(repo, issue_number, "poll_failed")
            return

        status = session.get("status_enum") or session.get("status") or "unknown"
        output = session.get("structured_output") or {}
        summary = output.get("summary") if isinstance(output, dict) else None
        text = summary or _last_devin_message(session) or "(no output produced)"

        if status in WAITING_STATUSES:
            body = (
                f"**Devin session** [{session_id}]({session.get('url', '')}) has a question:\n\n"
                f"> {text}\n\n"
                "_Reply here and it will be forwarded back to the session._"
            )
            self.store.update_status(repo, issue_number, "waiting_on_reply")
        elif status == "finished":
            extra = ""
            if isinstance(output, dict) and output.get("suggested_next_step"):
                extra = f"\n\n**Suggested next step:** {output['suggested_next_step']}"
            body = f"**Devin summary** ([session]({session.get('url', '')})):\n\n{text}{extra}"
            self.store.update_status(repo, issue_number, "summarized")
        else:
            body = (
                f"**Devin session** [{session_id}]({session.get('url', '')}) ended "
                f"with status `{status}`.\n\n> {text}"
            )
            self.store.update_status(repo, issue_number, f"ended_{status}")

        if self.settings.dry_run:
            logger.info("[dry-run] would comment on #%s:\n%s", issue_number, body)
            return
        try:
            await self.github.post_issue_comment(repo, issue_number, body)
        except Exception:
            logger.exception("failed to comment on #%s", issue_number)
            self.store.update_status(repo, issue_number, "comment_failed")

    async def _poll_session(self, session_id: str) -> dict:
        if self.settings.dry_run:
            return {
                "session_id": session_id,
                "status_enum": "finished",
                "url": "https://app.devin.ai (dry run)",
                "structured_output": {
                    "summary": "_(dry run)_ The issue would be summarized here by a real Devin session.",
                    "severity_guess": "unknown",
                    "suggested_next_step": "Run without DRY_RUN to get a real summary.",
                },
                "messages": [],
            }

        waited = 0.0
        while waited < self.settings.poll_timeout_seconds:
            session = await self.devin.get_session(session_id)
            status = session.get("status_enum") or ""
            if status in FINAL_STATUSES or status in WAITING_STATUSES:
                return session
            await asyncio.sleep(self.settings.poll_interval_seconds)
            waited += self.settings.poll_interval_seconds
        raise TimeoutError(f"session {session_id} did not finish within {waited:.0f}s")
