"""Minimal async client for the GitHub REST endpoints we need."""

import httpx


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def post_issue_comment(self, repo: str, issue_number: int, body: str) -> dict:
        resp = await self._client.post(
            f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body}
        )
        resp.raise_for_status()
        return resp.json()

    async def add_label(self, repo: str, issue_number: int, label: str) -> None:
        resp = await self._client.post(
            f"/repos/{repo}/issues/{issue_number}/labels", json={"labels": [label]}
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
