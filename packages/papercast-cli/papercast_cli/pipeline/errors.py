"""What the pipeline raises to the CLI's worker.

UsageLimit belongs to the CLI (papercast_cli/errors.py, A7); it is imported from there when
that module exists, so the worker catches one class. Only when it does not (this part tested on
its own) is the same class defined here.
"""
from __future__ import annotations

try:                                            # A7's class, when the CLI shell is present
    from ..errors import UsageLimit             # type: ignore  # noqa: F401
except ImportError:                             # pragma: no cover - depends on the merge
    class UsageLimit(Exception):
        """Claude's usage limit: the job pauses until `resume_at` (epoch seconds), then resumes
        where it stopped (every finished step is kept in the job dir)."""

        def __init__(self, resume_at, message: str = ""):
            super().__init__(message or f"Claude usage limit; resumes at {resume_at}")
            self.resume_at = resume_at


class PipelineError(Exception):
    """A job that cannot go on: the step it stopped at, a short code, a message for
    `papercast status`. `retryable`: the cause is outside the job (the hub unreachable, an
    overloaded API) and running the job again later may finish it."""

    def __init__(self, step: str, code: str, message: str, detail: str = "",
                 retryable: bool = False):
        super().__init__(message)
        self.step, self.code, self.message, self.detail = step, code, message, detail
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"step": self.step, "code": self.code, "message": self.message,
                "detail": self.detail, "retryable": self.retryable}


def usage_limit(resume_at: float, resets_at: float | None, guessed: bool, message: str,
                how: str = "") -> Exception:
    """A UsageLimit carrying the details as attributes, whatever the class's own signature."""
    e = UsageLimit(resume_at)
    for k, v in (("resets_at", resets_at), ("guessed", guessed), ("message", message),
                 ("how", how)):
        try:
            setattr(e, k, v)
        except AttributeError:                  # pragma: no cover
            pass
    if not getattr(e, "resume_at", None):
        e.resume_at = resume_at
    return e
