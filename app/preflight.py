"""Setup checks for external service credentials and repository access."""

import httpx

from .config import Settings
from .devin_client import DevinClient
from .github_client import GitHubClient


async def check_setup(
    settings: Settings,
    github: GitHubClient,
    devin: DevinClient,
) -> list[str]:
    problems = []
    if not settings.github_token:
        problems.append("GITHUB_TOKEN is not set.")
    if not settings.devin_api_key:
        problems.append("DEVIN_API_KEY is not set.")
    if not settings.devin_org_id:
        problems.append(
            "DEVIN_ORG_ID is not set, so the service is using the legacy v1 Devin API. "
            "Set it to your org-… id."
        )

    if settings.github_token:
        try:
            repo = await github.get_repo(settings.target_repo)
        except httpx.HTTPStatusError as exc:
            problems.append(
                f"GitHub can't read {settings.target_repo} (HTTP "
                f"{exc.response.status_code}). Check TARGET_REPO and GITHUB_TOKEN."
            )
        except httpx.HTTPError as exc:
            problems.append(f"Can't reach GitHub: {exc}")
        else:
            if not repo.get("has_issues"):
                problems.append(
                    f"Issues are disabled on {settings.target_repo}. "
                    "Enable them in the repo's Settings → General → Features."
                )

    if settings.devin_api_key:
        try:
            await devin.check_auth()
        except httpx.HTTPStatusError as exc:
            problems.append(
                f"The Devin API rejected the credential (HTTP "
                f"{exc.response.status_code}). Check DEVIN_API_KEY and DEVIN_ORG_ID."
            )
        except httpx.HTTPError as exc:
            problems.append(f"Can't reach Devin: {exc}")

    return problems
