"""A failed run in plain words: what kind of failure, and what the user can do next. Holds no stack trace and no document text."""
from __future__ import annotations

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app import llm

CATEGORIES = {
    "unreadable": ("The file has too little readable text", "If it is a scanned PDF, run text recognition on it or export it again as text, then upload it. You can also upload the DOCX."),
    "not_a_tor": ("This does not look like a software TOR or RFP", "Check that you uploaded the right file. If it is right, tell the team and give this reference."),
    "no_requirements": ("No requirements could be read from the file", "Check that the file holds the requirements as text and not as images. If it does, tell the team and give this reference: the format may be new to us."),
    "ai_unavailable": ("The AI service did not answer in time", "This is usually temporary. Wait a minute and retry. Nothing was saved."),
    "ai_busy": ("The AI service is busy", "Wait a minute or two and retry. Nothing was saved."),
    "cost_cap": ("The cost cap was reached", "Nothing more will be spent on this run. An administrator can review the limit; otherwise try again after the daily limit resets."),
    "extraction": ("The requirements could not be extracted", "Retry once. If it fails again, tell the team and give this reference."),
    "saving": ("The result could not be saved", "This is a problem on our side, and retrying will probably give the same result. Tell the team and give this reference. Nothing was changed."),
    "restarted": ("The app restarted during this run", "Retry. Nothing was changed."),
    "unexpected": ("Something unexpected went wrong", "Retry once. If it fails again, tell the team and give this reference. Nothing was changed."),
}
_DEFAULT = "unexpected"


def reference(run_id: int) -> str:
    return f"RUN-{run_id}"


def category_of(error: BaseException) -> str:
    if isinstance(error, llm.BudgetExceeded):
        return "cost_cap"
    if isinstance(error, (APITimeoutError, APIConnectionError, InternalServerError)):
        return "ai_unavailable"
    if isinstance(error, RateLimitError):
        return "ai_busy"
    if isinstance(error, (IntegrityError, OperationalError, SQLAlchemyError)):
        return "saving"
    if isinstance(error, (ValueError, KeyError, TypeError, AttributeError, IndexError)):
        return "extraction"
    return _DEFAULT


def constraint_of(error: BaseException) -> str | None:
    """The name of the database rule that was broken: a schema name, never a value."""
    return getattr(getattr(getattr(error, "orig", None), "diag", None), "constraint_name", None)


def detail(error: BaseException, stage: str, category: str | None = None) -> dict:
    category = category or category_of(error)
    title, next_step = CATEGORIES[category]
    out = {"error": type(error).__name__, "category": category, "stage": stage, "message": title, "next_step": next_step}
    constraint = constraint_of(error)
    if constraint:
        out["constraint"] = constraint
    return out


def text_of(run_id: int, run_detail: dict) -> tuple[str, str]:
    """(what happened, what to do next), for the screen. A run from before these categories reads as unexpected."""
    title, next_step = CATEGORIES.get(run_detail.get("category") or _DEFAULT, CATEGORIES[_DEFAULT])
    return f"{reference(run_id)}: {title}.", next_step
