"""What the tools share: opening a hub data dir outside the hub, and ids made from a name."""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

GROUP = Path(__file__).resolve().parent.parent              # stacks/papercast-group
if str(GROUP) not in sys.path:                              # run as `python3 tools/x.py` too
    sys.path.insert(0, str(GROUP))

from hub import config as C  # noqa: E402
from hub import db  # noqa: E402


def open_hub(data, seed: bool = True) -> C.Config:
    """A Config for this data dir, its database migrated (and seeded), as the hub would have it."""
    cfg = C.Config(data=Path(data).expanduser().resolve())
    cfg.data.mkdir(parents=True, exist_ok=True)
    cfg.episodes.mkdir(exist_ok=True)
    db.init(cfg)
    db.migrate()
    if seed:
        db.seed_defaults(cfg)
    return cfg


def stable_id(prefix: str, name: str, n: int = 12) -> str:
    """prefix + n base32 characters from a hash of `name`: the same thing gets the same id on
    every run, so a tool run twice finds what it made the first time."""
    h = hashlib.sha256(name.encode()).digest()
    return prefix + base64.b32encode(h).decode().lower()[:n]
