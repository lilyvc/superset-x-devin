#!/usr/bin/env python3
"""Send a signed GitHub webhook payload to a running superset-x-devin server.

Usage:
    # fake issue-opened payload, no GitHub access needed:
    python scripts/simulate.py issue-opened --number 1 --title "Dashboard crashes on load"

    # simulate a human replying on a tracked issue:
    python scripts/simulate.py issue-comment --number 1 --body "It's the Explore page"

    # build the payload from a REAL issue in the target repo (needs GITHUB_TOKEN):
    python scripts/simulate.py issue-opened --real --number 4
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

REPO = os.getenv("TARGET_REPO", "lilyvc/superset")
SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
TOKEN = os.getenv("GITHUB_TOKEN", "")
SERVER = os.getenv("SERVER_URL", "http://localhost:8000")


def fetch_real_issue(number: int) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/issues/{number}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def fake_issue(number: int, title: str, body: str) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "user": {"login": "simulated-user"},
        "labels": [{"name": os.getenv("ELIGIBILITY_LABEL", "devin-remediate")}] if os.getenv(
            "ELIGIBILITY_LABEL", "devin-remediate"
        ) else [],
    }


def payload_for(args) -> tuple[str, dict]:
    if args.kind == "issue-opened":
        issue = fetch_real_issue(args.number) if args.real else fake_issue(
            args.number, args.title, args.body
        )
        return "issues", {
            "action": "opened",
            "issue": issue,
            "repository": {"full_name": REPO},
            "sender": {"login": "simulated-user"},
        }
    if args.kind == "issue-comment":
        issue = fetch_real_issue(args.number) if args.real else fake_issue(
            args.number, "simulated issue", ""
        )
        return "issue_comment", {
            "action": "created",
            "issue": issue,
            "comment": {
                "body": args.body,
                "user": {"login": args.author},
                "html_url": f"https://github.com/{REPO}/issues/{args.number}#issuecomment-1",
            },
            "repository": {"full_name": REPO},
            "sender": {"login": args.author},
        }
    raise ValueError(args.kind)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("kind", choices=["issue-opened", "issue-comment"])
    p.add_argument("--number", type=int, default=1)
    p.add_argument("--title", default="Simulated issue title")
    p.add_argument("--body", default="Simulated issue body describing a problem.")
    p.add_argument("--author", default="simulated-user")
    p.add_argument("--real", action="store_true", help="fetch the real issue from TARGET_REPO (needs GITHUB_TOKEN)")
    p.add_argument("--server", default=SERVER)
    args = p.parse_args()

    event, payload = payload_for(args)
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": f"sim-{int(time.time() * 1000)}",
    }
    if SECRET:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
            SECRET.encode(), body, hashlib.sha256
        ).hexdigest()

    req = urllib.request.Request(
        f"{args.server}/webhooks/github", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(resp.status, resp.read().decode())
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
