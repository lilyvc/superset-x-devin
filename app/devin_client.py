"""Resilient client for the Devin sessions API.

Two auth modes:
  - v3 (normal production path): org-scoped endpoints
    (/v3/organizations/{org_id}/sessions, ...), selected when org_id is set.
    Production integrations should authenticate as a service user; a personal
    access token also works but acts as that human user — use it for scripts,
    not for the running service.
  - v1 (legacy): service-user key against /v1/sessions. Kept for backward
    compatibility; new deployments should set DEVIN_ORG_ID and use v3.

v1 and v3 build their request bodies separately: field names mostly overlap
today but their semantics are not guaranteed identical (e.g. status models
differ — v3 reports `status`/`status_detail`, v1 `status_enum`;
app/devin_status.py normalizes both).

Resilience: every request goes through _request(), which retries transient
failures — 429 (rate limit), 5xx, and network errors — with exponential
backoff + jitter, honoring Retry-After. Non-transient errors raise.
POST /sessions (session creation) deliberately does NOT auto-retry: if the
response was lost the session may exist remotely, so the engine searches for
a matching session by tags (adopt_session) before issuing another create.
"""

import asyncio
import logging
import random

import httpx

logger = logging.getLogger("devin_client")

# Remote statuses considered done — adopt_session skips these. Anything else
# (running, suspended, blocked, new, ...) is adoptable during reconciliation.
# Suspended sessions are resumable, so they stay adoptable.
_TERMINAL_STATUSES = {"exit", "expired", "finished", "terminated", "error"}


def _devin_id(session_id: str) -> str:
    """v3 path parameters expect the `devin-`-prefixed id."""
    return session_id if session_id.startswith("devin-") else f"devin-{session_id}"


class DevinClient:
    def __init__(self, api_key: str, base_url: str = "https://api.devin.ai",
                 org_id: str = "", *, max_retries: int = 4,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            transport=transport,
        )
        self._org_id = org_id
        self._base = f"/v3/organizations/{org_id}" if org_id else "/v1"
        self._v3 = bool(org_id)
        self._max_retries = max_retries

    # -- request bodies -------------------------------------------------------

    def _v3_body(self, prompt, *, title, tags, structured_output_schema,
                 max_acu_limit, repos, devin_mode) -> dict:
        # No `idempotent` flag: it is not part of the documented v3 create
        # schema. Distributed idempotency is achieved by tag-based adoption
        # (adopt_session) before and after ambiguous create failures instead.
        body: dict = {"prompt": prompt}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if repos:
            # Scoping the session to its target repos makes Devin pre-clone
            # them, so the agent starts with the code already on disk.
            body["repos"] = repos
        if devin_mode:
            body["devin_mode"] = devin_mode
        return body

    def _v1_body(self, prompt, *, title, tags, structured_output_schema,
                 max_acu_limit, repos, devin_mode) -> dict:
        # Legacy service-user path. Same intent, spelled out separately so the
        # models can diverge without surprising either API.
        body: dict = {"prompt": prompt, "idempotent": True}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if repos:
            body["repos"] = repos
        if devin_mode:
            body["devin_mode"] = devin_mode
        return body

    # -- resilient transport --------------------------------------------------

    async def _request(self, method: str, path: str, *, retry: bool = True,
                       **kwargs) -> httpx.Response:
        if not retry:
            resp = await self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise
                await self._sleep(attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    resp.raise_for_status()
                await self._sleep(attempt, resp)
                continue
            resp.raise_for_status()
            return resp
        assert last_exc is not None
        raise last_exc

    async def _sleep(self, attempt: int, resp: httpx.Response | None = None):
        delay = None
        if resp is not None:
            try:
                delay = float(resp.headers.get("retry-after", ""))
            except ValueError:
                delay = None
        if delay is None:
            delay = min(2 ** attempt, 16) + random.uniform(0, 0.5)
        logger.warning("devin api retry %d after %.1fs", attempt + 1, delay)
        await asyncio.sleep(delay)

    # -- sessions -------------------------------------------------------------

    async def check_auth(self) -> None:
        params = {"first": 1} if self._v3 else {"limit": 1}
        await self._request(
            "GET", f"{self._base}/sessions", params=params, retry=False
        )

    async def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        structured_output_schema: dict | None = None,
        max_acu_limit: int | None = None,
        repos: list[str] | None = None,
        devin_mode: str | None = None,
    ) -> dict:
        if self._v3:
            body = self._v3_body(
                prompt, title=title, tags=tags,
                structured_output_schema=structured_output_schema,
                max_acu_limit=max_acu_limit, repos=repos,
                devin_mode=devin_mode,
            )
        else:
            body = self._v1_body(
                prompt, title=title, tags=tags,
                structured_output_schema=structured_output_schema,
                max_acu_limit=max_acu_limit, repos=repos,
                devin_mode=devin_mode,
            )
        # Never auto-retry a create: a lost response may mean the session
        # already exists remotely. The engine adopts by tags before retrying.
        resp = await self._request(
            "POST", f"{self._base}/sessions", json=body, retry=False)
        return resp.json()  # {session_id, url, ...}

    def _path_id(self, session_id: str) -> str:
        return _devin_id(session_id) if self._v3 else session_id

    async def get_session(self, session_id: str) -> dict:
        resp = await self._request(
            "GET", f"{self._base}/sessions/{self._path_id(session_id)}")
        return resp.json()

    async def send_message(self, session_id: str, message: str) -> dict | None:
        name = "messages" if self._v3 else "message"
        resp = await self._request(
            "POST", f"{self._base}/sessions/{self._path_id(session_id)}/{name}",
            json={"message": message},
        )
        return resp.json()

    async def _v3_sessions_page(self, after: str | None,
                              page_size: int) -> tuple[list[dict], str | None]:
        params: dict = {"first": page_size}
        if after:
            params["after"] = after
        resp = await self._request("GET", f"{self._base}/sessions",
                                   params=params)
        data = resp.json()
        items = data.get("items", [])
        cursor = data.get("end_cursor") if data.get("has_next_page") else None
        return items, cursor

    async def list_sessions(self, *, tags: list[str] | None = None,
                            limit: int = 100) -> list[dict]:
        """List org sessions; filter to those carrying every tag client-side.

        v1 accepts `limit` and returns {"sessions": [...]}; v3 paginates with
        first/after and returns {"items": [...], "has_next_page": bool,
        "end_cursor": str}. Neither documents a server-side tags filter on
        the v3 listing — so tags are matched locally against each summary's
        tag list to keep behavior identical across auth modes.
        """
        if self._v3:
            sessions: list[dict] = []
            after: str | None = None
            while len(sessions) < limit:
                items, after = await self._v3_sessions_page(
                    after, min(limit - len(sessions), 100))
                sessions.extend(items)
                if after is None or not items:
                    break
        else:
            resp = await self._request(
                "GET", f"{self._base}/sessions", params={"limit": limit})
            data = resp.json()
            sessions = data.get("sessions", data) if isinstance(data, dict) else data
        if tags:
            wanted = set(tags)
            sessions = [s for s in sessions
                        if wanted <= set(s.get("tags") or [])]
        return sessions

    async def adopt_session(self, tags: list[str]) -> dict | None:
        """Find a live remote session carrying all `tags`, newest first.

        Used for distributed idempotency: after a restart or an ambiguous
        create failure, adopt the session Devin already has rather than
        spawning a duplicate.
        """
        candidates = []
        for s in await self.list_sessions(tags=tags):
            status = (s.get("status") or s.get("status_enum") or "")
            if status in _TERMINAL_STATUSES:
                continue
            candidates.append(s)
        candidates.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        return candidates[0] if candidates else None

    async def terminate_session(self, session_id: str, *,
                                archive: bool = False) -> None:
        """Stop the remote session so it stops consuming ACUs."""
        path = f"{self._base}/sessions/{self._path_id(session_id)}"
        params = {"archive": "true"} if archive and self._v3 else None
        await self._request("DELETE", path, params=params)

    # -- knowledge ------------------------------------------------------------

    async def create_knowledge_note(self, *, name: str, body: str, trigger: str,
                                    pinned_repo: str | None = None) -> dict:
        if not self._v3:
            raise RuntimeError("Knowledge notes need the v3 API (set DEVIN_ORG_ID)")
        payload = {"name": name, "body": body, "trigger": trigger}
        if pinned_repo:
            payload["pinned_repo"] = pinned_repo
        resp = await self._request("POST", f"{self._base}/knowledge/notes", json=payload,
                                   retry=False)
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
