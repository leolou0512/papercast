"""A fake pipeline for the CLI's tests: run_job(job_dir, api, progress) as A8's will be called.

The job's "PDF" holds lines key=value that say what to do:
  sleep=S         take S seconds (in two halves: stage1, stage2; a resumed run skips stage1)
  limit_once=S    the first attempt stops with UsageLimit(resume in S seconds)
  ask=1           raise NeedsAnswer unless job.json has yes: true
  answer=K        answer as the real pipeline does when the hub has the paper after all
                  (K = needs_confirmation, unless yes) or someone makes it (K = in_progress)
  fail_once=TEXT  the first attempt fails with PipelineError(TEXT)
  crash=1         a bug: RuntimeError
  child=1         start `sleep 60` (as claude would be) and record its pid
  title=TEXT      report the title
Every attempt appends events to <job_dir>/fake-events (start, stage1, resumed, end).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from papercast_cli.errors import NeedsAnswer, PipelineError, UsageLimit


def _directives(job_dir: Path, job: dict) -> dict:
    out = {}
    src = job.get("source")
    if src:
        for line in (job_dir / src).read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("%"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _event(job_dir: Path, **ev):
    ev.setdefault("t", time.time())
    ev.setdefault("pid", os.getpid())
    with open(job_dir / "fake-events", "a") as fh:
        fh.write(json.dumps(ev) + "\n")


def run_job(job_dir, api, progress):
    d = Path(job_dir)
    job = progress.job
    dv = _directives(d, job)
    starts = sum(1 for l in (d / "fake-events").read_text().splitlines()
                 if '"start"' in l) if (d / "fake-events").exists() else 0
    attempt = starts + 1
    _event(d, ev="start", attempt=attempt, nice=os.nice(0), sid=os.getsid(0),
           yes=job.get("yes"), version_of=job.get("version_of"), model=job.get("model"))
    progress("reading", 0.05, title=dv.get("title"))
    if dv.get("child"):
        child = subprocess.Popen(["sleep", "60"])
        _event(d, ev="child", child=child.pid)
    if dv.get("ask") and not job.get("yes"):
        raise NeedsAnswer("“A Paper” already has an episode by Alice. Make your own "
                          "version?", paper_id="p_alice")
    if dv.get("answer") == "needs_confirmation" and not job.get("yes"):
        return {"status": "needs_confirmation", "made_by": ["Bob"],
                "paper": {"id": "p_bob", "title": "A Paper", "episodes": [{"made_by": {"name": "Bob"}}]},
                "question": "made by Bob. Make your own version? [y/N]"}
    if dv.get("answer") == "in_progress":
        return {"status": "in_progress", "by": {"name": "Bob"}, "since": "2026-09-28T04:00:00Z",
                "message": "Bob is making this paper now"}
    if dv.get("fail_once") and attempt == 1:
        raise PipelineError(dv["fail_once"])
    if dv.get("crash"):
        raise RuntimeError("boom")
    if dv.get("limit_once") and attempt == 1:
        progress("writing", 0.3, detail="drafting the script")
        raise UsageLimit(time.time() + float(dv["limit_once"]), "You've hit your limit")
    total = float(dv.get("sleep", "0.2"))
    if (d / "stage1.done").exists():
        _event(d, ev="resumed", skipped="stage1")
    else:
        progress("writing", 0.2, detail="stage 1")
        _sleep(total / 2, progress)
        (d / "stage1.done").write_text("ok")
        _event(d, ev="stage1")
    progress("checking", 0.6, detail="stage 2")
    _sleep(total / 2, progress)
    me = api.me()                               # the api works inside the job's process
    bundle = d / "bundle.tar.gz"
    bundle.write_bytes(b"\x1f\x8b fake bundle")
    progress("uploading", 0.95)
    r = api.upload(str(bundle))
    _event(d, ev="end", me=me.get("email"))
    return {"episode_id": r["episode_id"], "paper_id": r["paper_id"], "state": r["state"]}


def _sleep(s: float, progress):
    end = time.time() + s
    while time.time() < end:
        time.sleep(min(0.1, max(0.0, end - time.time())))
        progress()                              # a cancel stops us here
