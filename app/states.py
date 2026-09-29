"""Workflow lifecycle vocabulary. The orchestrator (Python) owns every
transition; Devin only produces structured evidence that feeds the gates."""

from enum import Enum


class State(str, Enum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    NEEDS_INFO = "NEEDS_INFO"
    REPRODUCED = "REPRODUCED"
    ROOT_CAUSE_FOUND = "ROOT_CAUSE_FOUND"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    PR_OPENED = "PR_OPENED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    COMPLETED = "COMPLETED"
    NOT_REPRODUCIBLE = "NOT_REPRODUCIBLE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


TERMINAL_STATES = {
    State.COMPLETED,
    State.NOT_REPRODUCIBLE,
    State.FAILED,
    State.ESCALATED,
}

# States in which a Devin session is expected to be actively working.
ACTIVE_STATES = {
    State.TRIAGING,
    State.INVESTIGATING,
    State.REMEDIATING,
    State.VERIFYING,
}

WAITING_FOR_HUMAN_STATES = {State.NEEDS_INFO, State.BLOCKED}


class Role(str, Enum):
    INVESTIGATOR = "investigator"
    REMEDIATOR = "remediator"
    ANALYST = "analyst"


class NeedsInfoKind(str, Enum):
    REPORTER = "NEEDS_REPORTER_INFO"
    PRODUCT = "NEEDS_PRODUCT_INPUT"
    DESIGN = "NEEDS_DESIGN_INPUT"
    ENVIRONMENT = "NEEDS_ENVIRONMENT_INFO"


# Funnel stages, in order, for the dashboard. Each maps to the set of states
# that count as "reached at least this far".
FUNNEL = [
    ("Eligible", None),
    ("Investigated", {State.INVESTIGATING, State.NEEDS_INFO, State.REPRODUCED, State.ROOT_CAUSE_FOUND,
                      State.REMEDIATING, State.VERIFYING, State.PR_OPENED, State.READY_FOR_REVIEW,
                      State.COMPLETED, State.NOT_REPRODUCIBLE}),
    ("Reproduced", {State.REPRODUCED, State.ROOT_CAUSE_FOUND, State.REMEDIATING, State.VERIFYING,
                    State.PR_OPENED, State.READY_FOR_REVIEW, State.COMPLETED}),
    ("Fix attempted", {State.REMEDIATING, State.VERIFYING, State.PR_OPENED, State.READY_FOR_REVIEW,
                       State.COMPLETED}),
    ("Verified", {State.PR_OPENED, State.READY_FOR_REVIEW, State.COMPLETED}),
    ("PR opened", {State.PR_OPENED, State.READY_FOR_REVIEW, State.COMPLETED}),
    ("Ready for review", {State.READY_FOR_REVIEW, State.COMPLETED}),
]
