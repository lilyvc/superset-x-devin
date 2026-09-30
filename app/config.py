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

    # Bearer token required on /admin/* endpoints. When empty the endpoints are
    # disabled (503) rather than left open.
    admin_token: str = os.getenv("ADMIN_TOKEN", "")

    devin_api_key: str = os.getenv("DEVIN_API_KEY", "")
    devin_api_base_url: str = os.getenv("DEVIN_API_BASE_URL", "https://api.devin.ai")
    # Set when authenticating with a Personal Access Token (cog_ user PAT):
    # PATs use the org-scoped v3 API; service-user keys use v1. Leave empty for v1.
    devin_org_id: str = os.getenv("DEVIN_ORG_ID", "")

    # When true, no external calls are made: the "Devin session" is faked and
    # the GitHub comment is logged instead of posted. Used by scripts/simulate.py
    # so the full pipeline can be exercised without credentials.
    dry_run: bool = _env_bool("DRY_RUN", False)

    max_acu_limit: int | None = (
        int(os.getenv("MAX_ACU_LIMIT")) if os.getenv("MAX_ACU_LIMIT") else None
    )

    # Intake is autonomous by default: every open issue is discovered and
    # triaged. Setting a label restricts intake to issues carrying it.
    eligibility_label: str = os.getenv("ELIGIBILITY_LABEL", "")
    # Safety rails for autonomous intake.
    max_new_issues_per_poll: int = int(os.getenv("MAX_NEW_ISSUES_PER_POLL", "5"))
    issue_lookback_days: int | None = (
        int(os.getenv("ISSUE_LOOKBACK_DAYS")) if os.getenv("ISSUE_LOOKBACK_DAYS") else None
    )
    ignore_labels: tuple[str, ...] = tuple(
        label.strip().lower()
        for label in os.getenv(
            "IGNORE_LABELS",
            "question,duplicate,invalid,wontfix,discussion,rfc,sip",
        ).split(",")
        if label.strip()
    )
    # GitHub issue types (Projects "type" field) that are never intake candidates.
    ignore_issue_types: tuple[str, ...] = tuple(
        item.strip().lower()
        for item in os.getenv("IGNORE_ISSUE_TYPES", "feature,task,epic").split(",")
        if item.strip()
    )
    triage_enabled: bool = _env_bool("TRIAGE_ENABLED", True)
    triage_acu_limit: int = int(os.getenv("TRIAGE_ACU_LIMIT", "2"))

    max_concurrent_devins: int = int(os.getenv("MAX_CONCURRENT_DEVINS", "3"))
    max_remediation_attempts: int = int(os.getenv("MAX_REMEDIATION_ATTEMPTS", "2"))
    # Org-level budget ceiling: once recorded sessions have consumed this many
    # ACUs in total, no new Devin sessions are dispatched. Empty = unlimited.
    max_total_acus: float | None = (
        float(os.getenv("MAX_TOTAL_ACUS")) if os.getenv("MAX_TOTAL_ACUS") else None
    )
    # Pre-remediation dedup: before dispatching a Remediator, check open PRs for
    # an existing fix (explicit issue link, or a cheap Dedup Devin verdict on
    # ambiguous candidates).
    dedup_enabled: bool = _env_bool("DEDUP_ENABLED", True)
    dedup_acu_limit: int = int(os.getenv("DEDUP_ACU_LIMIT", "3"))
    # CI verification gate: a PR is only READY_FOR_REVIEW once the PR's actual
    # GitHub checks pass. Until then it sits in CI_CHECKING with a visible
    # ci_status (pending / failed / unverified). Set CI_REQUIRED=false to let
    # verified fixes go straight to review on repos without CI.
    ci_required: bool = _env_bool("CI_REQUIRED", True)
    # After this long in CI_CHECKING a one-time ci_timeout event is recorded so
    # a stalled pipeline is visible on the issue timeline. 0 = never.
    ci_timeout_seconds: float = float(os.getenv("CI_TIMEOUT_SECONDS", "14400"))
    # Watchdog for Devin sessions that never settle: after this long an active
    # session is nudged once to finish and emit its structured output, and at
    # twice this long the workflow is escalated for a human. 0 = never.
    session_stall_seconds: float = float(os.getenv("SESSION_STALL_SECONDS", "5400"))
    analysis_enabled: bool = _env_bool("ANALYSIS_ENABLED", True)
    analysis_label: str = os.getenv("ANALYSIS_LABEL", "devin-analysis")
    investigator_acu_limit: int = int(os.getenv("INVESTIGATOR_ACU_LIMIT", "10"))
    remediator_acu_limit: int = int(os.getenv("REMEDIATOR_ACU_LIMIT", "25"))
    analyst_acu_limit: int = int(os.getenv("ANALYST_ACU_LIMIT", "8"))

    # Devin agent mode per role (the sessions API `devin_mode` field). Cheap,
    # short-verdict roles run lite; remediation gets the strongest mode.
    # Empty = the organization's default mode.
    triage_mode: str = os.getenv("TRIAGE_DEVIN_MODE", "lite")
    dedup_mode: str = os.getenv("DEDUP_DEVIN_MODE", "lite")
    investigator_mode: str = os.getenv("INVESTIGATOR_DEVIN_MODE", "")
    remediator_mode: str = os.getenv("REMEDIATOR_DEVIN_MODE", "ultra")
    analyst_mode: str = os.getenv("ANALYST_DEVIN_MODE", "")

    db_path: str = os.getenv("DB_PATH", "orchestrator.db")

    # Polling mode: watch the target repo for new issues/comments instead of
    # (or in addition to) webhooks. No public URL needed.
    enable_polling: bool = _env_bool("ENABLE_POLLING", False)
    github_poll_interval_seconds: float = float(
        os.getenv("GITHUB_POLL_INTERVAL_SECONDS", "30")
    )
    # When false, issues that already exist at startup are baselined (not
    # dispatched); when true the open-issues backlog is discovered too.
    poll_backlog: bool = _env_bool("POLL_BACKLOG", True)

    def role_devin_mode(self, role: str) -> str | None:
        # role is a Role enum value: triage/investigator/remediator/dedup/analyst
        return getattr(self, f"{role}_mode", "") or None

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
