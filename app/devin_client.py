"""Thin async client for the Devin v1 API.

Endpoints used:
  POST /v1/sessions                    create a session (prompt, idempotent, structured_output_schema)
  GET  /v1/sessions/{id}               poll status_enum + messages + structured_output
  POST /v1/sessions/{id}/message       send a follow-up message to a running session
"""

import httpx


class DevinClient:
    def __init__(self, api_key: str, base_url: str = "https://api.devin.ai"):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

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
        resp = await self._client.post("/v1/sessions", json=body)
        resp.raise_for_status()
        return resp.json()  # {session_id, url, is_new_session}

    async def get_session(self, session_id: str) -> dict:
        resp = await self._client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    async def send_message(self, session_id: str, message: str) -> dict | None:
        resp = await self._client.post(
            f"/v1/sessions/{session_id}/message", json={"message": message}
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
