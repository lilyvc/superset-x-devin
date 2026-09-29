import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Target repository in "owner/name" form. All issue events and comments
    # are scoped to this repo; nothing is ever written into it besides
    # comments/labels via the GitHub API.
    target_repo: str = os.getenv("TARGET_REPO", "lilyvc/superset")

    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_webhook_secret: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    github_api_url: str = os.getenv("GITHUB_API_URL", "https://api.github.com")

    devin_api_key: str = os.getenv("DEVIN_API_KEY", "")
    devin_api_base_url: str = os.getenv("DEVIN_API_BASE_URL", "https://api.devin.ai")
    # Set when authenticating with a Personal Access Token (cog_ user PAT):
    # PATs use the org-scoped v3 API; service-user keys use v1. Leave empty for v1.
    devin_org_id: str = os.getenv("DEVIN_ORG_ID", "")

    # When true, no external calls are made: the "Devin session" is faked and
    # the GitHub comment is logged instead of posted. Used by scripts/simulate.py
    # so the full pipeline can be exercised without credentials.
    dry_run: bool = _env_bool("DRY_RUN", False)

    poll_interval_seconds: float = float(os.getenv("POLL_INTERVAL_SECONDS", "10"))
    poll_timeout_seconds: float = float(os.getenv("POLL_TIMEOUT_SECONDS", "1800"))
    max_acu_limit: int | None = (
        int(os.getenv("MAX_ACU_LIMIT")) if os.getenv("MAX_ACU_LIMIT") else None
    )

    # Optional label applied to issues once a Devin session has been dispatched.
    remediation_label: str = os.getenv("REMEDIATION_LABEL", "")

    db_path: str = os.getenv("DB_PATH", "orchestrator.db")

    @property
    def devin_use_v3(self) -> bool:
        return bool(self.devin_org_id)

    @property
    def repo_owner(self) -> str:
        return self.target_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.target_repo.split("/", 1)[1]


def get_settings() -> Settings:
    return Settings()
