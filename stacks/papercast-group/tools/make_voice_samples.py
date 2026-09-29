"""The sample clip of every preset voice (hub/voices.py PRESETS), for the voice lists on the page
(Settings > Voice, and a version's "Change"): one short paragraph voiced by papercast-voice in
that voice exactly as an episode is (its GPU line and waiting rule, loudness, MP3), one clip
at a time, niced; the CPU voice on the CPU.

    python3 tools/make_voice_samples.py [--data DIR] [--voice-cmd CMD] [--only ID ...]
                                        [--force] [--keep] [--text FILE]

Writes <data>/voices/<id>.mp3, which the hub serves at /api/voices/<id>/sample.mp3, and
<id>.json beside it (the voice the clip is in, its length, when, and the reference clip it was
voiced from: papercast-voice 1.2 designs a GPU voice once, saying this same paragraph, keeps that
clip and voices every chunk of every episode from it, so the sample is the episodes' narrator). A clip already there is kept
unless --force. A clip that came out in another voice than asked (papercast-voice fell back to
the CPU, say) is not kept. The job directories are <data>/voices/work/<id>/voice/, removed
afterwards unless --keep. Ctrl-C stops the clip being made (its `cancel`) and exits.

The defaults are perov's; there, once the new papercast-voice is installed (it must take
job.json `voice`), from ~/papercast-group/repo:
    python3 stacks/papercast-group/tools/make_voice_samples.py
Exit 0 when every clip asked for is there, 1 when one could not be made.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GROUP = Path(__file__).resolve().parent.parent
if str(GROUP) not in sys.path:
    sys.path.insert(0, str(GROUP))

from hub import voices  # noqa: E402

# The paragraph the hub's custom-voice previews say too (hub/voices.py SAMPLE_TEXT).
TEXT = voices.SAMPLE_TEXT


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def paper_id(vid: str) -> str:
    """papercast-voice takes only papercast ids (YYYY-MM-DD-<8 base32>, job.py ID_RE)."""
    b32 = base64.b32encode(hashlib.sha256(f"papercast-group voice sample {vid}".encode()).digest()).decode().lower()
    return f"{now_iso()[:10]}-{b32[:8]}"


class Stop(Exception):
    pass


class Maker:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.data) / "voices"
        self.work = self.out / "work"
        self.stopping = False
        self.env = {"HOME": os.environ.get("HOME", str(Path.home())), "USER": os.environ.get("USER", "leo"),
                    "LANG": "C.UTF-8", "PATH": os.environ.get("PCG_VOICE_PATH", "/usr/local/bin:/usr/bin:/bin")}

    def job_dir(self, p: dict, text: str) -> Path:
        vdir = self.work / p["id"] / "voice"
        shutil.rmtree(vdir, ignore_errors=True)
        vdir.mkdir(parents=True, mode=0o700)
        (vdir / "script.md").write_text(text.strip() + "\n", encoding="utf-8")
        claim = voices.for_claim(p)
        (vdir / "job.json").write_text(json.dumps({
            "interface": "1.0", "paper_id": paper_id(p["id"]), "script": "script.md", "output_dir": "out",
            "engine": "cpu" if claim["cpu"] else "auto", "voice": claim["spec"],
            "tags": {"title": f"Voice sample: {p['name']}", "album": "Papers", "artist": "papercast-group",
                     "albumartist": "Papers", "date": now_iso()[:10], "genre": "Podcast",
                     "comment": f"papercast-group voice sample {p['id']}"}}, indent=1) + "\n")
        return vdir

    def run(self, vdir: Path, name: str) -> dict:
        """papercast-voice run on vdir, as the worker runs it; its final status.json."""
        with open(vdir / "voice.log", "ab") as logf:
            proc = subprocess.Popen(["nice", "-n", "10", *shlex.split(self.a.voice_cmd), "run", str(vdir)],
                                    cwd=str(vdir), env=self.env, stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        said, last = None, 0.0
        try:
            while proc.poll() is None:
                if self.stopping:
                    raise Stop()
                st = read_json(vdir / "status.json") or {}
                what = st.get("phase"), st.get("wait_text") or st.get("note")
                if what != said or time.time() - last > 300:
                    said, last = what, time.time()
                    print(f"  {name}: {what[0] or 'starting'}" + (f" ({what[1]})" if what[1] else ""), flush=True)
                time.sleep(self.a.poll_s)
        except Stop:
            (vdir / "cancel").touch()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
            raise
        return read_json(vdir / "status.json") or {}

    def make(self, p: dict, text: str) -> bool:
        dest, meta = self.out / f"{p['id']}.mp3", self.out / f"{p['id']}.json"
        if dest.is_file() and not self.a.force:
            print(f"{p['id']}: there already ({dest}); --force to make it again")
            return True
        print(f"{p['id']}: {p['name']} ({'CPU' if p.get('cpu') else 'GPU'}, voice {p['key']})", flush=True)
        vdir = self.job_dir(p, text)
        t0 = time.time()
        st = self.run(vdir, p["id"])
        out = st.get("output") or {}
        mp3 = vdir / "out" / "episode.mp3"
        if st.get("phase") != "done" or out.get("path") != "out/episode.mp3" or not mp3.is_file():
            err = (st.get("error") or {})
            print(f"{p['id']}: not made: {err.get('code') or st.get('phase')}: {err.get('message') or 'see ' + str(vdir / 'voice.log')}")
            return False
        if out.get("voice") != p["key"]:
            print(f"{p['id']}: not kept: it came out in the voice {out.get('voice')!r} ({out.get('engine')}), "
                  f"not {p['key']!r}")
            return False
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(mp3, tmp)
        os.replace(tmp, dest)
        meta.write_text(json.dumps({"id": p["id"], "name": p["name"], "voice": out.get("voice"),
                                    "engine": out.get("engine"), "reference": out.get("reference"),
                                    "duration_s": out.get("duration_s"),
                                    "loudness_lufs": out.get("loudness_lufs"),
                                    "true_peak_db": out.get("true_peak_db"),
                                    "sha256": out.get("sha256"), "made_at": now_iso(),
                                    "took_s": round(time.time() - t0)}, indent=1) + "\n")
        print(f"{p['id']}: {out.get('duration_s')} s, {out.get('engine')}, {round(time.time() - t0)} s -> {dest}")
        if not self.a.keep:
            shutil.rmtree(self.work / p["id"], ignore_errors=True)
        return True


def main(argv=None) -> int:
    home = Path.home()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=os.environ.get("PCG_DATA") or str(home / "papercast-group" / "data"))
    ap.add_argument("--voice-cmd", default=os.environ.get("PAPERCAST_VOICE_CMD")
                    or str(home / "papercast-group" / "voice" / "bin" / "papercast-voice"))
    ap.add_argument("--only", nargs="+", metavar="ID", help="these voices only")
    ap.add_argument("--force", action="store_true", help="make clips that are there again")
    ap.add_argument("--keep", action="store_true", help="keep the job directories")
    ap.add_argument("--text", type=Path, help="the paragraph to voice (default: one about diffusion)")
    ap.add_argument("--poll-s", type=float, default=2.0, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    todo = voices.PRESETS
    if a.only:
        bad = [x for x in a.only if x not in voices.PRESET_BY_ID]
        if bad:
            print(f"no such voice: {', '.join(bad)} (the voices: {', '.join(voices.PRESET_BY_ID)})", file=sys.stderr)
            return 2
        todo = [voices.PRESET_BY_ID[x] for x in a.only]
    text = a.text.read_text(encoding="utf-8") if a.text else TEXT
    if os.nice(0) < 10:
        os.nice(10 - os.nice(0))
    m = Maker(a)

    def stop(_sig, _frame):
        m.stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    ok = True
    try:
        for p in todo:
            ok = m.make(p, text) and ok
    except Stop:
        print("stopped")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
