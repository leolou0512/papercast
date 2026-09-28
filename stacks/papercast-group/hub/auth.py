"""Identity, roles, sessions, the CLI's device login and tokens (SPEC.md section 2). Owner: A2.

Until A2's version lands, authenticate() lets only `public` routes through (and, in `header`
mode for tests, trusts X-Test-User from 127.0.0.1), so nothing is open by accident."""
from __future__ import annotations

LEVELS = ("public", "viewer", "contributor", "admin", "cli", "cli-contributor", "worker")
ROUTES: list = []


def authenticate(req, level: str):
    from .app import HTTPError
    if level not in LEVELS:
        raise HTTPError(500, "bad_level", level)
    if level == "public":
        return None
    raise HTTPError(401, "login_required", "log in first")
