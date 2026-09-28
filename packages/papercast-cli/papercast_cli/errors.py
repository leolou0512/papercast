"""The exceptions the CLI and the pipeline share (SPEC.md section 11). Owner: A7.

Every one carries a message written for the person at the terminal: `papercast` prints it as it
is, and a job that ends with one shows it in `papercast status`.
"""
from __future__ import annotations


class PapercastError(Exception):
    """An error with a message meant for the person at the terminal."""


class NotLoggedIn(PapercastError):
    """No hub or no token in the config."""


class ClaudeNotReady(PapercastError):
    """`claude` is not on PATH, or not logged in."""


# --------------------------------------------------------------------------- the hub

class ApiError(PapercastError):
    """The hub answered with an error, or could not be reached. `status` is the HTTP status (None
    for a network error), `error` the hub's error code ("in_progress", "denied", ...), `body` the
    parsed JSON answer (a dict, {} when there was none)."""

    def __init__(self, message: str, *, status: int | None = None, error: str = "",
                 body: dict | None = None):
        super().__init__(message)
        self.status, self.error, self.body = status, error, body or {}


class AuthError(ApiError):
    """401: not logged in, or the token was revoked."""


class Forbidden(ApiError):
    """403: the role does not allow it, or the account is disabled."""


class NotFound(ApiError):
    """404."""


class Conflict(ApiError):
    """409: e.g. someone else is making this paper (`error` "in_progress", `body` has by, since)."""


class ServerError(ApiError):
    """5xx, still failing after the retries."""


class NetworkError(ApiError):
    """The hub could not be reached (DNS, refused, timeout, TLS), still after the retries."""


# --------------------------------------------------------------------------- jobs

class UsageLimit(PapercastError):
    """Claude's usage limit ended a step. `resume_at`: epoch seconds when Claude said it resets,
    None when it did not say. The worker pauses every job until then (plus a minute) and runs the
    job again; the pipeline resumes from the files already in the job dir."""

    def __init__(self, resume_at: float | None = None, detail: str = ""):
        super().__init__(detail or "Claude usage limit")
        self.resume_at, self.detail = resume_at, detail


class NeedsAnswer(PapercastError):
    """The job cannot go on without the person, e.g. the paper already has an episode by someone
    else and `add` was not given --yes. The job waits in state `asking`; `papercast retry <job>
    --yes` answers yes (job.json `yes`: true), `papercast cancel <job>` no."""

    def __init__(self, question: str, *, paper_id: str | None = None):
        super().__init__(question)
        self.question, self.paper_id = question, paper_id


class Cancelled(PapercastError):
    """The job was cancelled (`papercast cancel`). progress() raises it; so does SIGTERM."""


class PipelineError(PapercastError):
    """A step of the pipeline failed for a reason the person should read (the job fails with the
    message; `papercast retry <job>` runs it again)."""
