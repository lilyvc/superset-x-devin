"""Prompts and structured-output contracts for the role-specific Devin sessions.

The structured output is a CONTRACT: the orchestrator reads these fields to
decide state transitions (see workflow.py gates). Prompts are deliberately
explicit about that so Devin fills the fields honestly rather than narrating.

Skills: the procedures in skills/*.md are embedded verbatim into the prompts
so every session follows the same engineering discipline.
"""

from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# Skills that must exist for the pipeline to produce well-formed sessions.
# Missing files previously degraded prompts silently; startup now fails fast.
REQUIRED_SKILLS = ("superset-bug-investigation", "superset-fix-verification")


def _skill(name: str) -> str:
    path = _SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"required skill file missing: {path}")
    return path.read_text()


def validate_skills(skills_dir: Path | None = None) -> list[str]:
    """Fail startup if any required skill file is absent. Returns loaded names."""
    directory = skills_dir or _SKILLS_DIR
    missing = [name for name in REQUIRED_SKILLS if not (directory / f"{name}.md").exists()]
    if missing:
        raise RuntimeError(
            "required skill files missing from " + str(directory) + ": " + ", ".join(missing)
        )
    return list(REQUIRED_SKILLS)


# ---------------------------------------------------------------------------
# Triage (cheap intake gate — runs on every newly discovered issue)
# ---------------------------------------------------------------------------

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "description": "One of: ACTIONABLE, NEEDS_INFO, SKIP.",
        },
        "issue_kind": {
            "type": "string",
            "description": "One of: bug, regression, feature_request, question, docs, "
            "chore, duplicate, invalid, unclear.",
        },
        "skip_reason": {
            "type": ["string", "null"],
            "description": "When verdict is SKIP: NOT_ENGINEERING | DUPLICATE | INVALID | "
            "FEATURE_REQUEST | UNSUITABLE.",
        },
        "duplicate_of": {"type": ["string", "null"]},
        "clarification_question": {
            "type": ["string", "null"],
            "description": "When verdict is NEEDS_INFO: ONE concise question the reporter can answer.",
        },
        "needs_info_kind": {
            "type": ["string", "null"],
            "description": "NEEDS_REPORTER_INFO | NEEDS_PRODUCT_INPUT | NEEDS_DESIGN_INPUT | "
            "NEEDS_ENVIRONMENT_INFO.",
        },
        "suspected_area": {
            "type": "string",
            "description": "Best guess at the affected area of Superset (e.g. explore, SQL Lab, "
            "dashboard filters, db_engine_specs/bigquery). One short phrase.",
        },
        "rationale": {"type": "string", "description": "2-4 sentences justifying the verdict."},
    },
    "required": ["verdict", "issue_kind", "rationale"],
}

TRIAGE_PROMPT = """You are the TRIAGE step of an autonomous engineering remediation system for the
Apache Superset fork `{repo}`. You are the cheap gate in front of expensive work.

Decide ONLY whether this issue is worth a full investigation. Spend as little
effort as possible: read the issue and its comments, and at most do a quick
look at the repository (a grep or a file read) if it is needed to tell whether
the report describes real engineering work. Do NOT reproduce the bug, do NOT
modify code, do NOT open PRs, do NOT run test suites.

## Issue #{number}: {title}
Reporter: {author}
Labels: {labels}
URL: {url}

{body}

## Comments so far
{comments}

## Verdicts
- ACTIONABLE — a concrete defect/regression in this codebase with enough
  information that an engineer could start reproducing it now.
- NEEDS_INFO — plausibly a real defect, but essential information is missing
  (version, steps, which chart/filter, expected behavior). Put ONE concise,
  answerable question in clarification_question and set needs_info_kind.
  Never invent the missing details.
- SKIP — not engineering work for this system: a support question, a feature
  request or design discussion, an obvious duplicate (set duplicate_of), spam,
  or otherwise unsuitable for autonomous remediation. Set skip_reason.

Be decisive and honest: SKIP and NEEDS_INFO are good answers. An orchestrator
reads your structured output and acts on it directly, so fill the fields
truthfully, then finish the session.
"""


# ---------------------------------------------------------------------------
# Investigator
# ---------------------------------------------------------------------------

INVESTIGATION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "One of: REPRODUCED, NOT_REPRODUCIBLE, NEEDS_INFO, BLOCKED. "
            "REPRODUCED requires reproduction_evidence to be non-empty.",
        },
        "enough_information": {"type": "boolean"},
        "reproduced": {"type": "boolean"},
        "expected_behavior": {"type": "string"},
        "observed_behavior": {"type": "string"},
        "reproduction_steps": {"type": "array", "items": {"type": "string"}},
        "reproduction_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete artifacts: failing test output, API responses, "
            "screenshot/recording URLs, log excerpts. Each item is a short label + "
            "the evidence or a URL to it.",
        },
        "root_cause": {"type": "string"},
        "root_cause_confidence": {
            "type": "string",
            "description": "One of: high, medium, low.",
        },
        "affected_components": {"type": "array", "items": {"type": "string"}},
        "verification_plan": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete checks a fixer must run to prove the fix: specific test "
            "files/functions, API calls, or UI steps.",
        },
        "suggested_tests": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Targeted existing test modules/commands relevant to this area.",
        },
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "clarification_question": {"type": ["string", "null"]},
        "needs_info_kind": {
            "type": ["string", "null"],
            "description": "When status is NEEDS_INFO: NEEDS_REPORTER_INFO | NEEDS_PRODUCT_INPUT | "
            "NEEDS_DESIGN_INPUT | NEEDS_ENVIRONMENT_INFO.",
        },
        "needs_product_input": {"type": "boolean"},
        "needs_design_input": {"type": "boolean"},
        "summary": {"type": "string", "description": "3-6 sentence engineer-facing summary."},
    },
    "required": ["status", "enough_information", "reproduced", "summary"],
}

INVESTIGATOR_PROMPT = """You are the INVESTIGATOR for an autonomous engineering remediation system
working on the Apache Superset fork `{repo}`.

Your job is to understand a bug BEFORE anyone writes a fix. You do NOT modify
source code, create branches, or open PRs. You may write throwaway scripts or
tests locally to reproduce the problem.

## Issue #{number}: {title}
Reporter: {author}
URL: {url}

{body}

## Comments so far
{comments}

{known_defects}
## Procedure
{skill}

## Output contract (IMPORTANT)
An orchestrator program reads your structured output and decides what happens
next. Fill it truthfully:

- status = REPRODUCED only if you actually observed the bug (failing test,
  wrong API response, wrong UI behavior) and put the proof in
  reproduction_evidence. Code reading alone is NOT reproduction.
- status = NEEDS_INFO if the report is ambiguous or the expected behavior is
  a product decision you cannot infer. Put ONE concise, answerable question in
  clarification_question and set needs_info_kind. Never invent requirements.
- status = NOT_REPRODUCIBLE if you followed the steps on current master and
  the behavior is correct; say what you tried in observed_behavior.
- status = BLOCKED if an environment problem prevents you (say what).
- verification_plan must be concrete enough that a different engineer could
  execute it: exact test paths, API calls, or UI steps.

When you have filled the structured output you are done; finish the session.
If you asked a question, a human reply will be delivered into THIS session as a
new message; when that happens, continue the investigation and update the
structured output.
"""

# ---------------------------------------------------------------------------
# Remediator
# ---------------------------------------------------------------------------

REMEDIATION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "One of: PR_OPENED, VERIFICATION_FAILED, BLOCKED, FAILED.",
        },
        "pr_url": {"type": ["string", "null"]},
        "branch": {"type": ["string", "null"]},
        "fix_summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "regression_tests_added": {"type": "array", "items": {"type": "string"}},
        "tests_executed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "result": {"type": "string", "description": "passed | failed | skipped"},
                    "notes": {"type": "string"},
                },
                "required": ["command", "result"],
            },
        },
        "reproduction_rerun_passed": {
            "type": "boolean",
            "description": "True only if you re-ran the ORIGINAL reproduction scenario after the fix and it now behaves correctly.",
        },
        "verification_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Before/after proof: test output, API output, screenshot or recording URLs.",
        },
        "verification_passed": {"type": "boolean"},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "learned_rules": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Standing rules a reviewer stated for ALL future work in this repo "
            "(not fixes specific to this PR), one imperative sentence each. Usually empty.",
        },
    },
    "required": ["status", "verification_passed", "reproduction_rerun_passed", "summary"],
}

LEARN_RULES_NOTE = (
    "This reviewer is a repository maintainer. If their feedback states a standing rule "
    "for future work (e.g. \"PRs must include X\"), apply it here AND add it as one "
    "sentence to `learned_rules` in your structured output; it will be saved as Devin "
    "Knowledge for every future session."
)

REMEDIATOR_PROMPT = """You are the REMEDIATOR for an autonomous engineering remediation system
working on the Apache Superset fork `{repo}`.

An Investigator has already reproduced this bug and identified a root cause.
Your job: implement the smallest correct fix, add regression coverage, verify
the original scenario now behaves correctly, and open a pull request.

## Issue #{number}: {title}
URL: {url}

{body}

## Investigation findings (from the Investigator session {investigator_url})
Expected behavior: {expected_behavior}
Observed behavior: {observed_behavior}
Root cause ({root_cause_confidence} confidence): {root_cause}
Affected components: {affected_components}

Reproduction steps:
{reproduction_steps}

Reproduction evidence:
{reproduction_evidence}

Verification plan you MUST execute after the fix:
{verification_plan}

Suggested targeted tests:
{suggested_tests}

## Procedure
{skill}

## Constraints
- Smallest appropriate fix. No unrelated refactors, formatting, or cleanup.
- Add a regression test that fails before your change and passes after.
- Run TARGETED tests (the relevant module(s)), not the whole suite.
- Re-run the original reproduction after the fix. For UI bugs, use the browser
  and capture a screenshot or short recording of the fixed behavior.
- Branch name: `devin/issue-{number}-<short-slug>`.
- Open the PR against the default branch of `{repo}`. PR title must be
  conventional-commit style, e.g. `fix(explore): ...`. PR body sections:
  ## Problem, ## Reproduction, ## Root cause, ## Fix, ## Regression coverage,
  ## Tests executed, ## Verification, ## Evidence, ## Related issue
  (reference `Fixes #{number}`).

## Output contract (IMPORTANT)
The orchestrator gates on your structured output:
- status = PR_OPENED only when the PR exists AND verification_passed is true
  AND reproduction_rerun_passed is true.
- If verification fails after reasonable iteration, status = VERIFICATION_FAILED
  and explain in blockers. Do not claim success without evidence.
- tests_executed must list the actual commands you ran and their results.
"""

# ---------------------------------------------------------------------------
# Dedup gate (cheap check before spending a remediation session)
# ---------------------------------------------------------------------------

DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "description": "One of: DUPLICATE, PROCEED, UNSURE.",
        },
        "duplicate_pr_url": {
            "type": ["string", "null"],
            "description": "When verdict is DUPLICATE: URL of the open PR already fixing "
            "this root cause.",
        },
        "rationale": {"type": "string", "description": "2-4 sentences justifying the verdict."},
    },
    "required": ["verdict", "rationale"],
}

DEDUP_PROMPT = """You are the DEDUP gate of an autonomous engineering remediation system for the
Apache Superset fork `{repo}`.

An Investigator has already reproduced issue #{number} and identified its root
cause. Before the system spends a remediation session, decide whether an
existing OPEN pull request already fixes the SAME root cause. Duplicate
remediation is the failure you exist to prevent.

## Issue #{number}: {title}
URL: {url}

Root cause: {root_cause}
Affected components: {affected_components}

## Candidate open PRs (pre-filtered for likely overlap)
{candidates}

## Task
- Read each candidate PR (title, body, linked issues, diff) — the repo and PRs
  are public. Decide if any of them already fixes THIS root cause, not merely
  a related symptom.
- DUPLICATE — an open PR already addresses the same root cause. Set
  duplicate_pr_url to it.
- PROCEED — no open PR covers this root cause; remediation should go ahead.
- UNSURE — evidence is genuinely ambiguous and a maintainer should decide.

Do NOT modify code and do NOT open PRs. Fill the structured output and finish.
"""


# ---------------------------------------------------------------------------
# Engineering analyst
# ---------------------------------------------------------------------------

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "similar_code_locations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "file:line or symbol references sharing the same faulty pattern.",
        },
        "related_issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Issue URLs/numbers with the same symptom or root cause.",
        },
        "systemic_risk": {
            "type": "string",
            "description": "One of: none, low, medium, high — with one sentence of justification.",
        },
        "missing_engineering_practice": {"type": "string"},
        "recommended_followup": {"type": "string"},
        "followup_type": {"type": "string", "description": "One of: NONE, ISSUE, PR."},
        "followup_issue_url": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    },
    "required": ["systemic_risk", "followup_type", "summary"],
}

ANALYST_PROMPT = """You are the ENGINEERING ANALYST for an autonomous engineering remediation
system working on the Apache Superset fork `{repo}`.

A bug has been reproduced and its root cause identified. A separate session is
implementing the fix — do NOT fix the bug yourself and do NOT modify code.

Question to answer: is this an isolated defect, or evidence of a wider
engineering problem?

## Issue #{number}: {title}
URL: {url}

Root cause: {root_cause}
Affected components: {affected_components}
Expected: {expected_behavior}
Observed: {observed_behavior}

{known_defects}
## Investigate
1. Does the same problematic code pattern exist elsewhere in the codebase?
   Search deliberately (grep/semantic) and list concrete locations.
2. Could other user-facing flows share this failure mode?
3. Are there related open or historical GitHub issues in `{repo}` or upstream
   apache/superset with the same symptom?
4. What engineering practice allowed this to escape: missing regression test,
   missing validation, missing invariant/abstraction/type contract, missing
   automated check?
5. Is there a BOUNDED preventative change worth making? Only recommend a
   follow-up if the evidence is strong; "NONE" is a valid, good answer.

If followup_type is ISSUE, create the issue on `{repo}` with a clear title,
the evidence, and the label `devin-analysis`, and put its URL in
followup_issue_url. Do not open PRs.

Be concrete and cite file paths. Fill the structured output and finish.
"""


def known_defects_block(analyses: list[dict]) -> str:
    """Prior defect-family findings, injected into later sessions' prompts
    so the system accumulates knowledge of recurring failure patterns."""
    if not analyses:
        return ""
    lines = ["## Defect families already found by this system (treat as prior knowledge)"]
    for item in analyses:
        analysis = item.get("analysis") or {}
        summary = (analysis.get("summary") or "").replace("\n", " ").strip()
        if not summary:
            continue
        risk = analysis.get("systemic_risk") or "unknown"
        lines.append(
            f"- issue #{item['issue_number']} (systemic risk: {risk}): {summary[:400]}"
        )
    if len(lines) == 1:
        return ""
    lines.append(
        "If this issue matches one of these families, say so explicitly in your "
        "output and link the pattern.\n"
    )
    return "\n".join(lines)


def render_comments(comments: list[dict]) -> str:
    if not comments:
        return "(none)"
    # Cap context size: the newest comments carry the signal for triage and
    # investigation; long threads otherwise bloat cheap sessions' prompts.
    comments = comments[-25:]
    lines = []
    for c in comments:
        user = (c.get("user") or {}).get("login", "?")
        lines.append(f"- **{user}**: {c.get('body', '').strip()}")
    return "\n".join(lines)


def _bullets(items) -> str:
    if not items:
        return "(none provided)"
    if isinstance(items, str):
        return items
    return "\n".join(f"- {i}" for i in items)


def triage_prompt(repo: str, issue: dict, comments: list[dict]) -> str:
    labels = [
        item if isinstance(item, str) else item.get("name", "")
        for item in issue.get("labels") or []
    ]
    return TRIAGE_PROMPT.format(
        repo=repo,
        number=issue["number"],
        title=issue.get("title", ""),
        author=(issue.get("user") or {}).get("login", "unknown"),
        labels=", ".join(labels) or "(none)",
        url=issue.get("html_url", ""),
        body=issue.get("body") or "(empty)",
        comments=render_comments(comments),
    )


def skip_comment(reason: str | None, rationale: str, session_url: str) -> str:
    label = {
        "NOT_ENGINEERING": "not engineering work for this system",
        "DUPLICATE": "a duplicate of existing work",
        "FEATURE_REQUEST": "a feature request rather than a defect",
        "INVALID": "not a valid defect report",
        "UNSUITABLE": "unsuitable for autonomous remediation",
    }.get(reason or "", "out of scope for autonomous remediation")
    return (
        f"**Skipped by automated triage** — this issue looks like {label} "
        f"([triage session]({session_url})).\n\n{rationale}\n\n"
        "_No Devin investigation was started. A maintainer can re-open this path by "
        "commenting with the missing context or handling it manually._"
    )


def investigator_prompt(
    repo: str, issue: dict, comments: list[dict], analyses: list[dict] | None = None
) -> str:
    return INVESTIGATOR_PROMPT.format(
        repo=repo,
        number=issue["number"],
        title=issue.get("title", ""),
        author=(issue.get("user") or {}).get("login", "unknown"),
        url=issue.get("html_url", ""),
        body=issue.get("body") or "(empty)",
        comments=render_comments(comments),
        skill=_skill("superset-bug-investigation"),
        known_defects=known_defects_block(analyses or []),
    )


def remediator_prompt(repo: str, issue: dict, inv: dict, investigator_url: str) -> str:
    return REMEDIATOR_PROMPT.format(
        repo=repo,
        number=issue["number"],
        title=issue.get("title", ""),
        url=issue.get("html_url", ""),
        body=issue.get("body") or "(empty)",
        investigator_url=investigator_url,
        expected_behavior=inv.get("expected_behavior") or "(not stated)",
        observed_behavior=inv.get("observed_behavior") or "(not stated)",
        root_cause=inv.get("root_cause") or "(not stated)",
        root_cause_confidence=inv.get("root_cause_confidence") or "unknown",
        affected_components=", ".join(inv.get("affected_components") or []) or "(none listed)",
        reproduction_steps=_bullets(inv.get("reproduction_steps")),
        reproduction_evidence=_bullets(inv.get("reproduction_evidence")),
        verification_plan=_bullets(inv.get("verification_plan")),
        suggested_tests=_bullets(inv.get("suggested_tests")),
        skill=_skill("superset-fix-verification"),
    )


def dedup_prompt(repo: str, issue: dict, inv: dict, candidates: list[str]) -> str:
    return DEDUP_PROMPT.format(
        repo=repo,
        number=issue["number"],
        title=issue.get("title", ""),
        url=issue.get("html_url", ""),
        root_cause=inv.get("root_cause") or "(not stated)",
        affected_components=", ".join(inv.get("affected_components") or []) or "(none listed)",
        candidates="\n".join(candidates) if candidates else "(none)",
    )


def analyst_prompt(
    repo: str, issue: dict, inv: dict, analyses: list[dict] | None = None
) -> str:
    return ANALYST_PROMPT.format(
        repo=repo,
        number=issue["number"],
        title=issue.get("title", ""),
        url=issue.get("html_url", ""),
        root_cause=inv.get("root_cause") or "(not stated)",
        affected_components=", ".join(inv.get("affected_components") or []) or "(none listed)",
        expected_behavior=inv.get("expected_behavior") or "(not stated)",
        observed_behavior=inv.get("observed_behavior") or "(not stated)",
        known_defects=known_defects_block(analyses or []),
    )


def clarification_comment(question: str, kind: str | None, session_url: str) -> str:
    kind_label = {
        "NEEDS_REPORTER_INFO": "the reporter",
        "NEEDS_PRODUCT_INPUT": "a product decision",
        "NEEDS_DESIGN_INPUT": "a design decision",
        "NEEDS_ENVIRONMENT_INFO": "environment details",
    }.get(kind or "", "clarification")
    return (
        f"**Devin needs {kind_label} before continuing** "
        f"([investigation session]({session_url})):\n\n"
        f"> {question}\n\n"
        "_Reply in this thread — your answer is forwarded into the same session "
        "and the workflow resumes automatically._"
    )
