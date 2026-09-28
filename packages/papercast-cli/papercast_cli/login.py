"""`papercast login` and `logout`: the device flow of SPEC.md section 2.

  POST /api/cli/login/start {"device"}  -> {"code", "url", "poll", "interval", "expires_in"}
  the person opens url, logs in on the web as usual, approves "papercast on <device>"
  POST /api/cli/login/poll {"poll"}     -> 428 pending | 200 {"token", "user"} | 410 expired
                                           | 403 denied
The token goes to the config (mode 600). Nothing else is stored: no password, no Claude login.
"""
from __future__ import annotations

import os
import sys
import time
import webbrowser

from . import __version__
from . import config
from .api import Api, normalise_server
from .errors import ApiError, AuthError, NetworkError, PapercastError


def _can_open_browser() -> bool:
    """A browser opens where the person sits: not over ssh, and on Linux only with a display."""
    if os.environ.get("PAPERCAST_NO_BROWSER"):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform == "darwin":
        return True
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    return "microsoft" in os.uname().release.lower()           # WSL opens the Windows browser


def login(server: str | None = None, *, device: str | None = None, browser: bool = True,
          out=print, sleep=time.sleep, clock=time.time) -> dict:
    """Run the device flow and save the token. Returns the user the hub reports."""
    cfg = config.load()
    server = normalise_server(server or cfg.get("server") or os.environ.get("PAPERCAST_SERVER", ""))
    device = device or cfg.get("device") or config.default_device()
    old = cfg.get("token") if cfg.get("server") == server else None
    api = Api(server, device=device)
    start = api.post("/api/cli/login/start", {"device": device, "client_version": __version__},
                     auth=False)
    if not isinstance(start, dict) or not start.get("poll") or not start.get("code"):
        raise PapercastError(f"{server} did not start a login (is it a papercast hub?)")
    code, url = start["code"], start.get("url") or f"{server}/cli?code={start['code']}"
    interval = max(0.0, float(start.get("interval") or 2))
    deadline = clock() + float(start.get("expires_in") or 600)

    out(f"To log in {device} to {server}, open\n\n    {url}\n\n"
        f"and approve the code {code}.")
    if browser and _can_open_browser():
        try:
            if webbrowser.open(url, new=2):
                out("(Opened it in your browser.)")
        except Exception:                                   # noqa: BLE001  (no browser: fine)
            pass
    out("Waiting for approval... (Ctrl-C to stop)")

    net_note = False
    while True:
        if clock() > deadline:
            raise PapercastError("The code expired before it was approved. Run papercast login again.")
        try:
            r = api.request("POST", "/api/cli/login/poll", json_body={"poll": start["poll"]},
                            auth=False, accept=(428, 410, 403, 429), retries=0, timeout=30)
        except NetworkError as e:
            # A laptop changing networks mid-login: keep polling until the code expires.
            if not net_note:
                out(f"({e}; still trying)")
                net_note = True
            sleep(max(interval, 2.0))
            continue
        if r.status == 200:
            body = r.body if isinstance(r.body, dict) else {}
            token = body.get("token")
            if not isinstance(token, str) or not token:
                raise PapercastError(f"{server} approved the login but sent no token")
            user = body.get("user") if isinstance(body.get("user"), dict) else {}
            config.update(server=server, token=token, device=device, user=user or None)
            if old and old != token:
                _revoke_quietly(Api(server, old, device=device))
            who = user.get("name") or user.get("email") or "you"
            role = f" ({user['role']})" if user.get("role") else ""
            out(f"Logged in as {who}{role} on {server}.")
            if user.get("role") == "viewer":
                out("Your role is viewer: uploading papers needs contributor. Ask an admin.")
            return user
        if r.status == 428:
            sleep(interval)
            continue
        if r.status == 429:                                  # asked to slow down
            interval += 2
            sleep(interval)
            continue
        if r.status == 410:
            raise PapercastError("The code expired before it was approved. Run papercast login again.")
        if r.status == 403:
            raise PapercastError("The login was denied on the web page. Nothing was saved.")


def _revoke_quietly(api: Api) -> bool:
    try:
        api.logout()
        return True
    except (ApiError, PapercastError):
        return False


def logout(out=print) -> bool:
    """Forget the token here and revoke it on the hub when the hub can be reached. True when the
    hub revoked it (or had already)."""
    cfg = config.load()
    token, server = cfg.get("token"), cfg.get("server")
    if not token:
        out("Not logged in.")
        return True
    revoked = False
    why = ""
    try:
        Api(server, token, device=cfg.get("device")).logout()
        revoked = True
    except AuthError:
        revoked = True                                       # already revoked on the web
    except (ApiError, PapercastError) as e:
        why = str(e)
    config.update(token=None, user=None)
    if revoked:
        out(f"Logged out: this device's login is revoked on {server}.")
    else:
        out(f"Logged out here, but {server} could not revoke the login ({why}).\n"
            f"Revoke \"{cfg.get('device') or 'this device'}\" under Devices on the web page.")
    return revoked
