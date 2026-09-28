"""Where papercast keeps its settings and its jobs (SPEC.md section 11).

  config  $XDG_CONFIG_HOME/papercast/config.json  (default ~/.config/...), mode 600:
          {"server", "token", "device", "user"}; the token is the only secret
  state   $XDG_STATE_HOME/papercast/              (default ~/.local/state/...):
          jobs/<job_id>/, worker.lock, worker.log, pause.json
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import stat
import tempfile
from pathlib import Path

from .errors import NotLoggedIn, PapercastError


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var, "")
    # The XDG spec: a relative path is invalid and is ignored.
    return Path(v) if v and os.path.isabs(v) else Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "papercast"


def config_path() -> Path:
    return config_dir() / "config.json"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "papercast"


def jobs_dir() -> Path:
    return state_dir() / "jobs"


def private_dir(p: Path) -> Path:
    """mkdir -p with mode 700 for what papercast creates (the parents are the person's own)."""
    p.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_IMODE(p.stat().st_mode) & 0o077:
            os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Write via a temp file in the same directory and rename, so a reader never sees half."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict:
    """The config, {} when there is none. A file others can read is made 600 again (it holds the
    token)."""
    p = config_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise PapercastError(f"Cannot read {p}: {e.strerror}")
    try:
        cfg = json.loads(raw or "{}")
    except ValueError:
        raise PapercastError(f"{p} is not valid JSON: fix it, or delete it and run papercast login")
    if not isinstance(cfg, dict):
        raise PapercastError(f"{p} is not a JSON object: delete it and run papercast login")
    try:
        if stat.S_IMODE(p.stat().st_mode) & 0o077:
            os.chmod(p, 0o600)
    except OSError:
        pass
    return cfg


def save(cfg: dict) -> None:
    private_dir(config_dir())
    atomic_write(config_path(), json.dumps(cfg, indent=1, sort_keys=True) + "\n", 0o600)


def update(**fields) -> dict:
    """Set (or, with None, remove) keys and save."""
    cfg = load()
    for k, v in fields.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    save(cfg)
    return cfg


def require_login(cfg: dict | None = None) -> dict:
    cfg = load() if cfg is None else cfg
    if not cfg.get("server"):
        raise NotLoggedIn("Not logged in. Run: papercast login --server https://<your group's hub>")
    if not cfg.get("token"):
        raise NotLoggedIn(f"Not logged in to {cfg['server']}. Run: papercast login")
    return cfg


def default_device() -> str:
    """The name the hub's Devices list and approve page show: "leo@val-stibnite"."""
    try:
        user = getpass.getuser()
    except Exception:                       # noqa: BLE001  (no passwd entry: containers)
        user = os.environ.get("USER", "")
    host = socket.gethostname().split(".")[0] or "computer"
    return f"{user}@{host}" if user else host
