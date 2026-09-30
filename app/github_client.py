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

    async def list_open_issues(self, repo: str, per_page: int = 100) -> list[dict]:
        issues: list[dict] = []
        for page in range(1, 101):
            resp = await self._client.get(
                f"/repos/{repo}/issues",
                params={"state": "open", "sort": "created", "direction": "asc",
                        "per_page": per_page, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            issues.extend(batch)
            if len(batch) < per_page:
                break
        return [issue for issue in issues if "pull_request" not in issue]

    async def get_issue(self, repo: str, issue_number: int) -> dict:
        resp = await self._client.get(f"/repos/{repo}/issues/{issue_number}")
        resp.raise_for_status()
        return resp.json()

    async def get_pull(self, repo: str, pull_number: int) -> dict:
        resp = await self._client.get(f"/repos/{repo}/pulls/{pull_number}")
        resp.raise_for_status()
        return resp.json()

    async def list_open_pulls(self, repo: str, per_page: int = 100) -> list[dict]:
        pulls: list[dict] = []
        for page in range(1, 101):
            resp = await self._client.get(
                f"/repos/{repo}/pulls",
                params={"state": "open", "per_page": per_page, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            pulls.extend(batch)
            if len(batch) < per_page:
                break
        return pulls

    async def list_pull_files(self, repo: str, pull_number: int) -> list[str]:
        files: list[str] = []
        for page in range(1, 101):
            resp = await self._client.get(
                f"/repos/{repo}/pulls/{pull_number}/files",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            files.extend(f.get("filename", "") for f in batch)
            if len(batch) < 100:
                break
        return files

    async def get_commit_checks(self, repo: str, ref: str) -> dict:
        """Check runs + legacy combined commit status for a ref (e.g. PR head sha)."""
        runs_resp = await self._client.get(
            f"/repos/{repo}/commits/{ref}/check-runs", params={"per_page": 100}
        )
        runs_resp.raise_for_status()
        status_resp = await self._client.get(f"/repos/{repo}/commits/{ref}/status")
        status_resp.raise_for_status()
        combined = status_resp.json()
        return {
            "check_runs": runs_resp.json().get("check_runs", []),
            "statuses": combined.get("statuses", []),
            "combined_state": combined.get("state"),
        }

    async def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        comments: list[dict] = []
        for page in range(1, 101):
            resp = await self._client.get(
                f"/repos/{repo}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            comments.extend(batch)
            if len(batch) < 100:
                break
        return comments

    async def get_authenticated_user(self) -> dict:
        resp = await self._client.get("/user")
        resp.raise_for_status()
        return resp.json()

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
