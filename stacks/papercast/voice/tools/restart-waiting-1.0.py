#!/usr/bin/env python3
"""One-off: move voice jobs that are waiting under voice 1.0 (which cannot re-execute itself) onto
the installed 1.1, through the runner's own "interrupted" path: the voice process group
(launch.sh + papercast_voice) is stopped with SIGTERM, so launch.sh writes no rc; the runner,
which adopted these processes after its own restart (adopt=True), sees the process gone with no
rc and the phase not done, raises Interrupted and restarts the voice step once, with the new
code. The new process keeps the paper's place in line (status.json "since").

    python3 tools/restart-waiting-1.0.py            # which papers it would restart, and why not others
    python3 tools/restart-waiting-1.0.py --apply    # restart them, oldest first, 10 at a time

Run it as leo on stibnite, after install.sh has put 1.1 in place (`papercast-voice info` says
1.1). It stops after any batch in which a paper did not come back on 1.1 within 90 s.

Only jobs that: are waiting-for-gpu under 1.0 with a live papercast_voice pid; whose runner record
is waiting-for-gpu with no earlier interruption of the speaking step (a second one fails the
paper); whose launch.sh is its own process group leader, alive with the recorded start time, and
NOT a child of the running runner (a child means adopt=False: a kill would fail the paper); and
that have no rc file.
"""
import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import time

STATE = "/home/leo/papercast/state"
LOG = "/home/leo/papercast/voice/run/restart-waiting-1.0.log"


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def stat(pid):
    try:
        data = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    rest = data[data.rindex(")") + 2:].split()
    return {"state": rest[0], "ppid": int(rest[1]), "pgrp": int(rest[2]), "start": int(rest[19])}


def alive(pid):
    s = stat(pid)
    return bool(s) and s["state"] not in ("Z", "X")


def cmdline(pid):
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def runner_pid():
    out = subprocess.run(["pgrep", "-u", "leo", "-f", "papercast_runner.server"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if len(out) == 1 else None


def candidates():
    rp = runner_pid()
    if not rp:
        sys.exit("runner not found (or more than one)")
    out, skipped = [], {}
    for sj in glob.glob(f"{STATE}/*/voice/status.json"):
        vdir = os.path.dirname(sj)
        pdir = os.path.dirname(vdir)
        try:
            st = json.load(open(sj))
            paper = json.load(open(os.path.join(pdir, "paper.json")))
            pj = json.load(open(os.path.join(vdir, "pid.json")))
        except (OSError, ValueError):
            continue
        if st.get("phase") != "waiting-for-gpu":
            continue

        def skip(why):
            skipped[why] = skipped.get(why, 0) + 1

        if not str(st.get("voice_version", "")).startswith("1.0"):
            skip("not 1.0")
            continue
        if not (alive(st.get("pid")) and "papercast_voice" in cmdline(st["pid"])):
            skip("voice pid not alive")
            continue
        if paper.get("state") != "waiting-for-gpu" or (paper.get("interrupted") or {}).get("speaking"):
            skip("runner state/interrupted")
            continue
        lp = int(pj["pid"])
        s = stat(lp)
        if not s or s["start"] != pj.get("start_ticks") or s["pgrp"] != lp:
            skip("launcher identity")
            continue
        if s["ppid"] == rp:
            skip("child of the runner (not adopted)")
            continue
        if stat(st["pid"])["pgrp"] != lp:
            skip("voice not in launcher group")
            continue
        if os.path.exists(os.path.join(vdir, "rc")):
            skip("rc exists")
            continue
        out.append({"vdir": vdir, "id": os.path.basename(pdir), "since": st.get("since"),
                    "launcher": lp, "voice": st["pid"]})
    return out, skipped


def migrate_one(c):
    os.killpg(c["launcher"], signal.SIGTERM)
    t = time.time()
    while time.time() - t < 5 and (alive(c["launcher"]) or alive(c["voice"])):
        time.sleep(0.1)
    if alive(c["launcher"]) or alive(c["voice"]):
        os.killpg(c["launcher"], signal.SIGKILL)
        log(f"{c['id']}: SIGKILL needed")


def restarted(c):
    try:
        st = json.load(open(os.path.join(c["vdir"], "status.json")))
    except (OSError, ValueError):
        return None
    if st.get("pid") != c["voice"] and str(st.get("voice_version", "")).startswith("1.1"):
        return st
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--youngest-first", action="store_true")
    ap.add_argument("--batch", type=int, default=10)
    a = ap.parse_args()
    cs, skipped = candidates()
    cs.sort(key=lambda c: c["since"] or "", reverse=a.youngest_first)
    if a.limit:
        cs = cs[:a.limit]
    log(f"{len(cs)} to migrate; skipped {skipped}")
    if not a.apply:
        for c in cs[:5]:
            print(c)
        return
    done = failed = 0
    for i in range(0, len(cs), a.batch):
        batch = cs[i:i + a.batch]
        for c in batch:
            migrate_one(c)
        t = time.time()
        pending = list(batch)
        while pending and time.time() - t < 90:
            time.sleep(1)
            pending = [c for c in pending if not restarted(c)]
        for c in batch:
            st = restarted(c)
            if st:
                done += 1
            else:
                failed += 1
                log(f"{c['id']}: NOT restarted within 90 s")
        log(f"batch {i // a.batch + 1}: {len(batch) - len(pending)}/{len(batch)} restarted "
            f"(total {done}, not {failed})")
        if failed:
            log("stopping: a job did not come back")
            return


if __name__ == "__main__":
    main()
