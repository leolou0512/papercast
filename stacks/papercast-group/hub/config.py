"""The hub's settings, from the environment (SPEC.md sections 0 and 2)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass
class Config:
    data: Path                      # $PCG_DATA: hub.db and episodes/
    bind: str = "127.0.0.1"
    port: int = 8480
    auth: str = "local"             # cf-access | local | header (tests only)
    cf_team: str = ""               # <team>.cloudflareaccess.com
    cf_aud: str = ""
    admin_emails: set = field(default_factory=set)
    public_url: str = "http://127.0.0.1:8480"
    secret: bytes = b""             # cookie HMAC key
    worker_token_sha256: str = ""
    static: Path = HERE / "static"

    @property
    def episodes(self) -> Path:
        return self.data / "episodes"


def load(env=None) -> Config:
    env = os.environ if env is None else env
    data = Path(env.get("PCG_DATA", str(Path.home() / "papercast-group" / "data")))
    cfg = Config(
        data=data,
        bind=env.get("PCG_BIND", "127.0.0.1"),
        port=int(env.get("PCG_PORT", "8480")),
        auth=env.get("PCG_AUTH", "local"),
        cf_team=env.get("PCG_CF_TEAM", ""),
        cf_aud=env.get("PCG_CF_AUD", ""),
        admin_emails={e.strip().lower() for e in env.get("PCG_ADMIN_EMAILS", "").split(",") if e.strip()},
        public_url=env.get("PCG_PUBLIC_URL", "").rstrip("/") or f"http://127.0.0.1:{env.get('PCG_PORT', '8480')}",
        secret=env.get("PCG_SECRET", "").encode(),
        worker_token_sha256=env.get("PCG_WORKER_TOKEN_SHA256", "").lower(),
        static=Path(env.get("PCG_STATIC", str(HERE / "static"))),
    )
    if cfg.auth not in ("cf-access", "local", "header"):
        raise SystemExit(f"PCG_AUTH must be cf-access, local or header, not {cfg.auth!r}")
    if cfg.auth != "header" and len(cfg.secret) < 16:
        raise SystemExit("PCG_SECRET must be set (at least 16 characters)")
    return cfg
