"""Measurements that set the voice's numbers, each written down with its conditions.

  measure-cpu --script F [--chunks N] [--configs 1x8,2x4,4x2] [--out FILE]
      Voice the first N chunks of F with Kokoro at each workers x threads split; report wall
      time, CPU seconds used, speed (audio seconds per wall second) and memory per worker.
  measure-cpu --apply WxT
      Store the chosen split in voice.json (cpu.workers, cpu.threads).
  measure-episode <job dir>
      From a finished full-length run: words per minute of the episode (what the runner's
      15-25 minute check needs) and seconds of synthesis per word (the ETA), into voice.json.
  measure-gpu --script F --need-mib N [--apply]
      Run the configured GPU voice on F as a real job while sampling NVML every 50 ms: peak GPU
      memory of the job's whole process tree (what another user loses), device peak, throughput.
      --apply stores peak_mib, sec_per_word and words_per_min for that engine.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import procs, textprep
from .config import engine_spec, load, save_measured
from .workers import Worker


def _cpu_s(pid: int) -> float:
    """utime + stime of one process, seconds. Raises if it is gone: a sweep with a dead
    worker measured nothing."""
    with open(f"/proc/{pid}/stat") as fh:
        f = fh.read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def _conditions() -> dict:
    la = os.getloadavg()
    return {"host": os.uname().nodename, "cpus": os.cpu_count(), "loadavg_1m": round(la[0], 2),
            "nice": os.nice(0), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _sweep_one(cfg: dict, spec: dict, chunks: list, workers: int, threads: int, out_dir: str) -> dict:
    cancel = threading.Event()
    ws = [Worker(spec, threads=threads, gpu_index=None, cfg=cfg, tag=f"m{i}") for i in range(workers)]
    t_load = time.time()
    ths = [threading.Thread(target=w.start, args=(cancel,)) for w in ws]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    load_s = time.time() - t_load
    if any(w.ready is None for w in ws):
        for w in ws:
            w.stop()
        raise RuntimeError("a worker did not start")
    cpu0 = sum(_cpu_s(w.pid) for w in ws)
    q = list(chunks)
    lock = threading.Lock()
    audio_s = [0.0]

    def loop(w: Worker) -> None:
        while True:
            with lock:
                if not q:
                    return
                ch = q.pop(0)
            msg = w.synth(ch.idx, ch.text, os.path.join(out_dir, f"{ch.idx:04d}.wav"), cancel)
            with lock:
                audio_s[0] += float(msg["audio_s"])

    t0 = time.time()
    ths = [threading.Thread(target=loop, args=(w,)) for w in ws]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0
    cpu = sum(_cpu_s(w.pid) for w in ws) - cpu0
    rss = [w.maxrss_mib for w in ws]
    for w in ws:
        w.stop()
    words = sum(c.words for c in chunks)
    return {"workers": workers, "threads": threads, "thread_budget": workers * threads,
            "load_s": round(load_s, 1), "wall_s": round(wall, 1), "cpu_s": round(cpu, 1),
            "audio_s": round(audio_s[0], 1), "speed_x": round(audio_s[0] / wall, 2),
            "cores_busy": round(cpu / wall, 1), "cpu_s_per_audio_s": round(cpu / audio_s[0], 3),
            "words": words, "sec_per_word": round(wall / words, 4),
            "maxrss_mib_per_worker": max(rss), "maxrss_mib_total": sum(rss)}


def measure_cpu(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="papercast-voice measure-cpu")
    ap.add_argument("--script")
    ap.add_argument("--chunks", type=int, default=24)
    ap.add_argument("--configs", default="1x8,2x4,4x2,4x4,8x2")
    ap.add_argument("--out")
    ap.add_argument("--apply", help="WxT: store this split in voice.json")
    a = ap.parse_args(argv)
    cfg = load()
    if a.apply:
        w, t = (int(x) for x in a.apply.lower().split("x"))
        save_measured({"cpu": {"workers": w, "threads": t}})
        print(json.dumps({"applied": {"workers": w, "threads": t}}))
        return 0
    if not a.script:
        ap.error("--script is required")
    spec = engine_spec(cfg, cfg["cpu_engine"])
    with open(a.script, encoding="utf-8") as fh:
        chunks = textprep.plan(fh.read(), int(spec["max_words"]), cfg["audio"])
    chunks = [c for c in chunks if c.kind == "para"][: a.chunks]
    res = {"engine": spec["name"], "voice": spec.get("voice"), "chunks": len(chunks),
           "conditions_before": _conditions(), "runs": []}
    with tempfile.TemporaryDirectory(prefix="pcv-measure-") as td:
        for c in a.configs.split(","):
            w, t = (int(x) for x in c.lower().split("x"))
            r = _sweep_one(cfg, spec, chunks, w, t, td)
            r["loadavg_1m_after"] = round(os.getloadavg()[0], 2)
            res["runs"].append(r)
            print(json.dumps(r), flush=True)
    res["conditions_after"] = _conditions()
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(res, fh, indent=1)
    return 0


def measure_episode(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="papercast-voice measure-episode")
    ap.add_argument("job_dir")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    st = json.load(open(os.path.join(a.job_dir, "status.json")))
    mt = json.load(open(os.path.join(a.job_dir, "metrics.json")))["runs"][-1]
    if st.get("phase") != "done":
        print("that job is not done", file=sys.stderr)
        return 2
    words, dur = mt["words"], st["output"]["duration_s"]
    speak = [w for w in mt.get("workers", []) if w.get("gen_s")]
    synth_s = mt["total_s"] - mt.get("encode_s", 0) - mt.get("wait_s", 0)
    name = st["output"]["engine"].split(":", 1)[1]
    cfg = load()
    engine = next(n for n, s in cfg["engines"].items() if s.get("label", n) == name)
    out = {"engine": engine, "words": words, "duration_s": dur,
           "words_per_min": round(words / (dur / 60), 1),
           "sec_per_word": round(synth_s / words, 4), "workers": len(speak),
           "measured": f"{mt['finished_at']} full episode, {words} words, {dur:.0f} s audio, "
                       f"{synth_s:.0f} s from start to encoding with {len(speak)} worker(s)"}
    print(json.dumps(out))
    if a.apply:
        save_measured({"engines": {engine: {k: out[k] for k in ("words_per_min", "sec_per_word",
                                                               "measured")}}})
    return 0


def measure_gpu(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="papercast-voice measure-gpu")
    ap.add_argument("--script", required=True)
    ap.add_argument("--need-mib", type=int, required=True,
                    help="admission threshold for this run (the previous, provisional figure)")
    ap.add_argument("--keep", help="keep the job directory here")
    ap.add_argument("--gpu-lock", default="/home/leo/papercast/state/voice-gpu.lock",
                    help="the GPU slot the runner's jobs use (INTERFACE §10.4), so a measurement "
                         "and a real episode never share the card")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    import pynvml
    cfg = load()
    name = cfg.get("gpu_engine")
    if not name:
        print("no GPU engine configured", file=sys.stderr)
        return 2
    spec = engine_spec(cfg, name)
    root = a.keep or tempfile.mkdtemp(prefix="pcv-gpu-")
    vdir = os.path.join(root, "state", "2026-01-01-measurea", "voice")
    os.makedirs(vdir, exist_ok=True)
    shutil.copyfile(a.script, os.path.join(vdir, "script.md"))
    with open(os.path.join(vdir, "job.json"), "w") as fh:
        json.dump({"interface": "1.0", "paper_id": "2026-01-01-measurea", "script": "script.md",
                   "output_dir": "out", "engine": "auto",
                   "tags": {"title": "GPU measurement", "artist": "papercast", "date": "2026-01-01"}}, fh)
    # this run's config: the provisional peak, so admission uses --need-mib
    over = json.load(open(cfg["config_file"])) if os.path.exists(cfg["config_file"]) else {}
    over.setdefault("engines", {}).setdefault(name, {})["peak_mib"] = a.need_mib - int(cfg["gpu"]["headroom_mib"])
    over["gpu_lock"] = (a.gpu_lock if os.path.isdir(os.path.dirname(a.gpu_lock))
                        else os.path.join(root, "state", "voice-gpu.lock"))
    # stibnite's card only, and its own line: it measures this GPU, not a remote one.
    over["hosts"] = {n: {"enabled": n == "stibnite" or (h or {}).get("kind", "local") == "local"}
                     for n, h in (cfg.get("hosts") or {}).items()}
    over["queue_dir"] = os.path.join(root, "queue")
    tmpcfg = os.path.join(root, "voice.json")
    with open(tmpcfg, "w") as fh:
        json.dump(over, fh)
    env = dict(os.environ, PAPERCAST_VOICE_CONFIG=tmpcfg)
    pynvml.nvmlInit()
    h = pynvml.nvmlDeviceGetHandleByIndex(int(cfg["gpu"]["index"]))
    before = pynvml.nvmlDeviceGetMemoryInfo(h).used / 2**20
    p = subprocess.Popen([sys.executable, "-I", "-m", "papercast_voice", "run", vdir], env=env,
                         cwd=vdir, stdout=open(os.path.join(vdir, "voice.log"), "ab"),
                         stderr=subprocess.STDOUT)
    peak_tree, peak_dev, samples, series, peak_at = 0.0, before, 0, [], None
    t0 = time.time()
    while p.poll() is None:
        tree = procs.descendants(p.pid)
        try:
            used = sum((x.usedGpuMemory or 0) for x in pynvml.nvmlDeviceGetComputeRunningProcesses(h)
                       if x.pid in tree) / 2**20
            dev = pynvml.nvmlDeviceGetMemoryInfo(h).used / 2**20
        except pynvml.NVMLError:
            used, dev = 0.0, 0.0
        if used > peak_tree:
            peak_tree, peak_at = used, round(time.time() - t0, 1)
        peak_dev = max(peak_dev, dev)
        samples += 1
        if samples % 100 == 0:
            series.append((round(time.time() - t0, 1), round(used)))
        time.sleep(0.05)
    st = json.load(open(os.path.join(vdir, "status.json")))
    mt = json.load(open(os.path.join(vdir, "metrics.json")))["runs"][-1]
    out = {"engine": name, "rc": p.returncode, "phase": st.get("phase"), "error": st.get("error"),
           "peak_process_tree_mib": round(peak_tree), "peak_at_s": peak_at,
           "peak_device_used_mib": round(peak_dev), "gpu_lock": over["gpu_lock"],
           "retried_chunks": mt.get("retried_chunks", []),
           "device_used_before_mib": round(before), "samples": samples, "poll_s": 0.05,
           "wall_s": round(time.time() - t0, 1), "wait_s": mt.get("wait_s"),
           "encode_s": mt.get("encode_s"), "workers": mt.get("workers"),
           "words": mt.get("words"), "duration_s": (st.get("output") or {}).get("duration_s"),
           "memory_over_time": series, "job_dir": vdir, "conditions": _conditions()}
    if out["duration_s"]:
        gen = sum(w.get("gen_s") or 0 for w in mt.get("workers", []))
        load_s = sum(w.get("load_s") or 0 for w in mt.get("workers", []))
        out.update(rtf=round(gen / out["duration_s"], 3), load_s=load_s,
                   words_per_min=round(out["words"] / (out["duration_s"] / 60), 1),
                   sec_per_word=round((gen + load_s) / out["words"], 4))
    print(json.dumps(out, indent=1))
    if a.apply and out["phase"] == "done":
        save_measured({"engines": {name: {
            "peak_mib": out["peak_process_tree_mib"], "words_per_min": out["words_per_min"],
            "sec_per_word": out["sec_per_word"],
            "measured": (f"{out['conditions']['at']}: {out['words']} words, "
                         f"{out['duration_s']:.0f} s audio; NVML every 50 ms, job process tree")}}})
    return 0 if out["phase"] == "done" else 1

