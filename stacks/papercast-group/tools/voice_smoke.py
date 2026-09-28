#!/usr/bin/env python3
"""One short episode through the real voice worker and the real papercast-voice (Breeze on this
machine's GPU), with deploy/tests/fake_hub.py standing in for the hub: the check that the worker
and the installed voice agree (job.json, the voice's ids, status.json, the MP3 it hands back).

    python3 stacks/papercast-group/tools/voice_smoke.py [--voice CMD] [--keep]

It waits for the GPU the way every episode does (papercast-voice's own rule), so run it when the
card is idle: about three minutes on perov's A4000 (two of them loading the model). Prints one JSON
line: what the hub received (bytes, duration, sha256 match, the phases it was told), and exits 0
when the audio arrived and matches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PG / "deploy" / "tests"))
from fake_hub import FakeHub  # noqa: E402

SCRIPT = """# A short test of the voice

Flow matching trains a network to move noise toward data along straight paths. Instead of simulating a slow diffusion process, the model learns a velocity field, and sampling becomes a matter of following that field from start to finish.

The idea is simple, the training is stable, and the results rival the best diffusion models while needing far fewer steps. This paragraph exists only to check that the narrator sounds clear, calm and even.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--voice", default=str(Path.home() / "papercast-group/voice/bin/papercast-voice"))
    ap.add_argument("--keep", action="store_true", help="keep the job directory and logs")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="pcg-voice-smoke-", dir=str(Path.home() / "papercast-group")))
    token = "pcgw_smoke_" + os.urandom(8).hex()
    (tmp / "worker.token").write_text(token + "\n")
    hub = FakeHub(token=token).start()
    hub.add("e_smoketest0001", "Voice smoke test", SCRIPT, first_author="papercast-group")
    env = {"HOME": str(Path.home()), "USER": os.environ.get("USER", "leo"), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "PCG_HUB_URL": hub.url, "PCG_WORKER_TOKEN_FILE": str(tmp / "worker.token"),
           "PAPERCAST_VOICE_CMD": a.voice, "PCG_VOICE_JOBS": str(tmp / "episodes"),
           "PCG_WORKER_STATE": str(tmp / "worker"), "PCG_POLL_S": "1", "PCG_HEARTBEAT_S": "30",
           "PCG_WORKER_NAME": "voice-smoke"}
    t0 = time.time()
    with open(tmp / "worker.log", "ab") as log:
        rc = subprocess.run([sys.executable, str(PG / "deploy" / "voice_worker.py"), "--exit-when-idle"],
                            env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    hub.stop()
    got = hub.audio.get("e_smoketest0001")
    vdir = tmp / "episodes" / "e_smoketest0001" / "voice"
    st = json.loads((vdir / "status.json").read_text()) if (vdir / "status.json").exists() else {}
    out = {"worker_rc": rc, "wall_s": round(time.time() - t0, 1), "phases": hub.phases("e_smoketest0001"),
           "failure": hub.failures.get("e_smoketest0001"),
           "notes": sorted({s.get("note") for s in hub.statuses.get("e_smoketest0001", []) if s.get("note")})[:4],
           "audio": None if not got else {
               "bytes": len(got["bytes"]), "duration_s": got["duration_s"], "sha256_ok": got["ok"],
               "id3": got["bytes"][:3] == b"ID3", "ctype": got["ctype"]},
           "voice_output": st.get("output"), "job_dir": str(vdir)}
    if got:
        (tmp / "received.mp3").write_bytes(got["bytes"])
        out["received"] = str(tmp / "received.mp3")
    print(json.dumps(out))
    ok = rc == 0 and got is not None and got["ok"]
    if not a.keep and ok:
        shutil.rmtree(tmp, ignore_errors=True)
        out["job_dir"] = None
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
