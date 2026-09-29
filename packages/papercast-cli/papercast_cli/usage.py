"""How much of the person's Claude limits a paper takes, from their own finished jobs.

Every `claude -p --output-format stream-json` run the pipeline makes logs, in
<job>/logs/run-N.jsonl, Claude Code's `rate_limit_event` lines (the plan's five-hour and
seven-day windows: utilization 0 to 1, measured against this person's own plan, and when the
window resets) and one `result` line with the run's API-equivalent cost (total_cost_usd).

A paper's share of the five-hour limit = its cost x (the rise in utilization over a window /
the cost of the runs logged in that window). Anything else on the same Claude login during that
window (an interactive Claude Code session, say) counts too, so it is an upper estimate.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from . import config

MIN_RISE = 0.03          # a window whose utilization rose less than 3 points says too little
MIN_PAPER_COST = 0.5     # a job under $0.50 did not get to writing
RECENT_JOBS = 60


def _runs(job_dir: Path):
    for f in sorted((job_dir / "logs").glob("*.jsonl")):
        cost, utils, week, win, reset = 0.0, [], None, None, None
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"rate_limit_event"' not in line and '"total_cost_usd"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "result" and isinstance(e.get("total_cost_usd"), (int, float)):
                cost += float(e["total_cost_usd"])
            elif e.get("type") == "rate_limit_event":
                w = (e.get("rate_limit_info") or {}).get("unifiedWindows") or {}
                five, seven = w.get("five_hour") or {}, w.get("seven_day") or {}
                if isinstance(five.get("utilization"), (int, float)):
                    utils.append(float(five["utilization"]))
                    win = reset = five.get("resetsAt")
                if isinstance(seven.get("utilization"), (int, float)):
                    week = float(seven["utilization"])
        try:
            t = f.stat().st_mtime
        except OSError:
            t = 0.0
        yield {"cost": cost, "utils": utils, "week": week, "win": win, "reset": reset, "t": t}


def estimate(jobs_root: Path | None = None) -> dict | None:
    """{"per_paper": fraction of the five-hour limit, "now": latest five-hour utilization,
    "resets": unix time, "week": latest seven-day utilization, "papers": how many it is from},
    or None without enough history. Fractions are 0 to 1."""
    root = jobs_root or config.jobs_dir()
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)[-RECENT_JOBS:]
    except OSError:
        return None
    per_job, windows, latest = {}, defaultdict(lambda: {"cost": 0.0, "lo": 1.0, "hi": 0.0}), None
    for d in dirs:
        total = 0.0
        for r in _runs(d):
            total += r["cost"]
            if r["win"] is not None and r["utils"]:
                w = windows[r["win"]]
                w["cost"] += r["cost"]
                w["lo"], w["hi"] = min(w["lo"], *r["utils"]), max(w["hi"], *r["utils"])
            if r["utils"] and (latest is None or r["t"] >= latest["t"]):
                latest = r
        if total >= MIN_PAPER_COST:
            per_job[d.name] = total
    rates = [(w["hi"] - w["lo"]) / w["cost"] for w in windows.values()
             if w["cost"] > 0 and w["hi"] - w["lo"] >= MIN_RISE]
    if not per_job or not rates or latest is None:
        return None
    return {"per_paper": statistics.median(per_job.values()) * statistics.median(rates),
            "now": latest["utils"][-1], "resets": latest["reset"], "week": latest["week"],
            "papers": len(per_job)}
