"""Pure parsers for GitHub payloads and Devin session data."""

import hashlib
import json
import re
from datetime import datetime
from typing import Any

_ISSUE_URL_RE = re.compile(r"/issues/(\d+)(?:[^0-9]|$)")
_FIX_LINK_RE = re.compile(r"(?:fix(?:e[sd])?|fixing|close[sd]?|closing|resolve[sd]?|resolving)"
                          r"[:\s]+#?(\d+)", re.IGNORECASE)
_STOPWORDS = {
    "with", "that", "this", "from", "have", "been", "when", "where", "which",
    "would", "could", "should", "into", "their", "there", "about", "after",
    "before", "issue", "does", "http", "https", "com", "github",
}


def issue_labels(issue: dict) -> set[str]:
    return {
        item if isinstance(item, str) else item.get("name", "")
        for item in issue.get("labels") or []
    }


def issue_type(issue: dict) -> str:
    issue_type = issue.get("type")
    if isinstance(issue_type, dict):
        return (issue_type.get("name") or "").lower()
    return (issue_type or "").lower() if isinstance(issue_type, str) else ""


def issue_number_from_url(url: str | None) -> int | None:
    if not url:
        return None
    match = _ISSUE_URL_RE.search(url)
    return int(match.group(1)) if match else None


def pr_number_from_url(url: str | None) -> int | None:
    match = re.search(r"/pull/(\d+)", url or "")
    return int(match.group(1)) if match else None


def pull_url(pulls) -> str | None:
    for pull in pulls or []:
        if isinstance(pull, str):
            return pull
        if isinstance(pull, dict) and pull.get("url"):
            return pull["url"]
    return None


def linked_issue_numbers(text: str) -> set[int]:
    return {int(match.group(1)) for match in _FIX_LINK_RE.finditer(text)}


def tokens(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9_./-]{4,}", (text or "").lower())
        if word not in _STOPWORDS
    }


def fingerprint(output: Any) -> str:
    return hashlib.sha256(json.dumps(output or {}, sort_keys=True).encode()).hexdigest()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
