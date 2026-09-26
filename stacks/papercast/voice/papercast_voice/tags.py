"""ID3v2.4 tags (spec §2.10, INTERFACE §10.2), written with mutagen, read back two ways.

Navidrome reads the file's tags (TagLib); the phone app gets them from Navidrome. ID3v2.4 keeps
the full date (TDRC `2026-09-26`) and UTF-8 text. Tag values come from paper metadata, which is
hostile text: control characters are removed and lengths capped; nothing becomes a path.
"""
from __future__ import annotations

import re
import subprocess
import time
import unicodedata

from mutagen.id3 import COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TXXX

FIELDS = ("title", "artist", "album", "albumartist", "date", "genre", "comment")


def clean(value, cap: int = 400) -> str:
    s = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in str(value or ""))
    s = " ".join(s.split())
    return s[:cap]


def normalise(tags: dict) -> dict:
    t = {k: clean(tags.get(k)) for k in FIELDS}
    t["album"] = t["album"] or "Papers"
    t["albumartist"] = t["albumartist"] or "Papers"
    t["artist"] = t["artist"] or "Unknown author"
    t["title"] = t["title"] or "Untitled paper"
    t["genre"] = t["genre"] or "Podcast"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t["date"]):
        t["date"] = time.strftime("%Y-%m-%d", time.gmtime())
    return t


def write(path: str, tags: dict, replaygain_db: float | None = None,
          peak: float | None = None) -> dict:
    t = normalise(tags)
    id3 = ID3()
    id3.add(TIT2(encoding=3, text=[t["title"]]))
    id3.add(TPE1(encoding=3, text=[t["artist"]]))
    id3.add(TALB(encoding=3, text=[t["album"]]))
    id3.add(TPE2(encoding=3, text=[t["albumartist"]]))
    id3.add(TDRC(encoding=3, text=[t["date"]]))
    id3.add(TCON(encoding=3, text=[t["genre"]]))
    if t["comment"]:
        id3.add(COMM(encoding=3, lang="eng", desc="", text=[t["comment"]]))
    if replaygain_db is not None:
        # So a player in ReplayGain mode levels the episode with music (reference -18 LUFS).
        id3.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[f"{replaygain_db:+.2f} dB"]))
        if peak is not None:
            id3.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=[f"{peak:.6f}"]))
    id3.save(path, v2_version=4)
    return t


def read_mutagen(path: str) -> dict:
    id3 = ID3(path)

    def one(fid: str) -> str:
        f = id3.getall(fid)
        return str(f[0].text[0]) if f and f[0].text else ""
    comm = id3.getall("COMM")
    return {"title": one("TIT2"), "artist": one("TPE1"), "album": one("TALB"),
            "albumartist": one("TPE2"), "date": one("TDRC"), "genre": one("TCON"),
            "comment": str(comm[0].text[0]) if comm and comm[0].text else "",
            "id3_version": f"2.{id3.version[1]}"}


def read_ffmpeg(ffmpeg: str, path: str) -> dict:
    """libavformat's ID3 parser: a second reader that shares no code with mutagen."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path, "-f", "ffmetadata", "-"],
                       capture_output=True, text=True, timeout=60)
    kv = {}
    for line in r.stdout.splitlines():
        if "=" in line and not line.startswith(";"):
            k, v = line.split("=", 1)
            kv[k.strip().lower()] = re.sub(r"\\(.)", r"\1", v)
    return {"title": kv.get("title", ""), "artist": kv.get("artist", ""),
            "album": kv.get("album", ""), "albumartist": kv.get("album_artist", ""),
            "date": kv.get("date", ""), "genre": kv.get("genre", ""),
            "comment": kv.get("comment", "")}


def mismatches(expected: dict, got: dict) -> list[str]:
    return [f"{k}: wrote {expected[k]!r}, read {got.get(k)!r}" for k in FIELDS
            if expected[k] != got.get(k, "")]
