"""papercast-voice: script.md -> a narrated, tagged, loudness-normalised episode (W2-papercast Part C)."""
import os as _os


def _version() -> str:
    # install.sh writes VERSION (git commit of the source it copied); a checkout has none.
    try:
        with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "VERSION"),
                  encoding="utf-8") as fh:
            return fh.read().strip() or "dev"
    except OSError:
        return "dev"


VERSION = _version()
