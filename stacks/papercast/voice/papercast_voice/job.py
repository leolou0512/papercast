"""`papercast-voice run <dir>`: one episode, script.md -> out/episode.mp3 (INTERFACE.md §10).

The runner starts this detached (setsid, nice 10) and re-runs it on the same directory after a
reboot or a Retry. Everything that must survive is on disk in <dir>:

  job.json, script.md   the runner's hand-off (read only)
  status.json           written atomically on every change and every heartbeat_s (§10.3)
  use-cpu, cancel       the runner's controls (§10.4), checked at least every second
  chunks/<engine key>/  one WAV per finished chunk; a re-run never voices a chunk twice
  out/episode.mp3       the result; status.json `output` carries its sha256
  metrics.json          timings, memory, rates of every run (voice-internal)

One process per directory (flock on .voice.lock). At most one GPU job on the machine (flock on
state/voice-gpu.lock, held from admission to the end of GPU synthesis) and one CPU job (the
voice's own run/voice-cpu.lock).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback

from . import VERSION, audio, gpu, procs, tags, textprep
from .config import engine_spec, gpu_need_mib
from .locks import FileLock
from .textprep import ScriptInvalid
from .workers import Cancelled, EngineError, EngineOOM, Worker

INTERFACE = "1.4"   # this document version (INTERFACE.md); readers compare the major
ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z2-7]{8}$")
PLAN_VERSION = 1          # bump when chunking changes, so old chunks are not reused
EXIT_OK, EXIT_SCRIPT, EXIT_FAILED, EXIT_CANCELLED, EXIT_BUSY = 0, 2, 3, 4, 5


class JobInvalid(Exception):
    pass


class GpuOOMFailed(Exception):
    pass


class Yielded(Exception):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(now_iso(), msg, file=sys.stderr, flush=True)


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Status:
    """status.json (§10.3). Extra fields (ignored by readers that do not know them):
    wait_reason / wait_text (why it is waiting, in plain words), note, voice_version."""

    def __init__(self, vdir: str, heartbeat_s: float):
        self.path = os.path.join(vdir, "status.json")
        self.hb = float(heartbeat_s)
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        t = now_iso()
        self.d = {"interface": INTERFACE, "paper_id": None, "phase": "preparing", "engine": None,
                  "need_mib": None, "free_mib": None, "chunks_done": 0, "chunks_total": 0,
                  "audio_s": 0, "eta_s": None, "pid": os.getpid(), "since": t, "updated_at": t,
                  "error": None, "output": None, "wait_reason": None, "wait_text": None,
                  "note": None, "voice_version": VERSION}

    def set(self, **kw) -> None:
        with self.lock:
            if "phase" in kw and kw["phase"] != self.d["phase"]:
                self.d["since"] = now_iso()
            self.d.update(kw)
            self._write()

    def get(self, k):
        with self.lock:
            return self.d.get(k)

    def _write(self) -> None:
        self.d["updated_at"] = now_iso()
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh)
        os.replace(tmp, self.path)

    def _beat(self) -> None:
        while not self.stop_evt.wait(self.hb):
            with self.lock:
                self._write()

    def start(self) -> None:
        threading.Thread(target=self._beat, daemon=True).start()

    def stop(self) -> None:
        self.stop_evt.set()


class Job:
    def __init__(self, vdir: str, cfg: dict):
        self.vdir = os.path.realpath(vdir)
        self.cfg = cfg
        self.prev = read_json(os.path.join(self.vdir, "status.json"))
        self.status = Status(self.vdir, cfg["heartbeat_s"])
        self.cancel = threading.Event()
        self.done_evt = threading.Event()
        state_dir = os.path.dirname(os.path.dirname(self.vdir))
        self.gpu_lock = FileLock(cfg.get("gpu_lock") or os.path.join(state_dir, "voice-gpu.lock"))
        self.cpu_lock = FileLock(cfg["cpu_lock"])
        self.workers: list[Worker] = []
        self.plock = threading.Lock()
        self.plan: list[textprep.Chunk] = []
        self.spec: dict | None = None
        self.done: dict[int, float] = {}
        self.run_words = 0
        self.run_t0: float | None = None
        self.m: dict = {"started_at": now_iso(), "pid": os.getpid(), "version": VERSION,
                        "wait_s": 0.0, "events": []}

    # --------------------------------------------------------------- helpers
    def p(self, *parts: str) -> str:
        return os.path.join(self.vdir, *parts)

    def control(self, name: str) -> bool:
        return os.path.exists(self.p(name))

    def _check_cancel(self) -> None:
        if self.cancel.is_set() or self.control("cancel"):
            self.cancel.set()
            raise Cancelled()

    def _watch(self) -> None:
        while not self.done_evt.wait(1.0):
            if self.control("cancel"):
                self.cancel.set()
                return

    def event(self, msg: str) -> None:
        log(msg)
        self.m["events"].append(f"{now_iso()} {msg}")

    # --------------------------------------------------------------- job files
    def _load_job(self) -> dict:
        path = self.p("job.json")
        if not os.path.isfile(path) or os.path.getsize(path) > 64 * 1024:
            raise JobInvalid("job.json missing or over 64 KiB")
        job = read_json(path)
        if not job:
            raise JobInvalid("job.json is not a JSON object")
        if str(job.get("interface", "")).split(".")[0] != INTERFACE.split(".")[0]:
            raise JobInvalid(f"job.json speaks interface {job.get('interface')!r}, voice {INTERFACE}")
        if not ID_RE.match(str(job.get("paper_id", ""))):
            raise JobInvalid("job.json paper_id is not a papercast id")
        if job.get("script") != "script.md" or job.get("output_dir") != "out":
            raise JobInvalid("job.json script/output_dir must be script.md and out")
        if job.get("engine") not in ("auto", "cpu"):
            raise JobInvalid(f"job.json engine {job.get('engine')!r} is not auto or cpu")
        if not isinstance(job.get("tags"), dict):
            raise JobInvalid("job.json has no tags")
        return job

    def _read_script(self) -> str:
        path = self.p("script.md")
        if not os.path.isfile(path) or os.path.getsize(path) > 1024 * 1024:
            raise ScriptInvalid("script.md missing or over 1 MB")
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except UnicodeDecodeError as e:
            raise ScriptInvalid(f"script.md is not UTF-8: {e}") from e

    def _previous_output(self) -> dict | None:
        prev = self.prev or {}
        out = prev.get("output") or {}
        if prev.get("phase") != "done" or not re.fullmatch(r"out/episode\.(mp3|opus)", out.get("path", "")):
            return None
        path = self.p(out["path"])
        if os.path.isfile(path) and out.get("sha256") == sha256_file(path):
            return out
        return None

    # --------------------------------------------------------------- chunks
    @staticmethod
    def engine_key(spec: dict) -> str:
        raw = f"{spec['name']}-{spec.get('voice', '')}-{spec.get('speed', 1.0)}-w{spec['max_words']}-p{PLAN_VERSION}"
        return re.sub(r"[^A-Za-z0-9._-]", "_", raw)

    def chunk_path(self, spec: dict, ch: textprep.Chunk) -> str:
        h = hashlib.sha1(ch.text.encode()).hexdigest()[:12]
        return self.p("chunks", self.engine_key(spec), f"{ch.idx:04d}-{h}.wav")

    def _prepare(self, spec: dict, script: str) -> None:
        self.spec = spec
        self.plan = textprep.plan(script, int(spec["max_words"]), self.cfg["audio"])
        d = self.p("chunks", self.engine_key(spec))
        os.makedirs(d, mode=0o700, exist_ok=True)
        for name in os.listdir(d):
            if name.endswith(".tmp"):
                os.unlink(os.path.join(d, name))
        with open(os.path.join(d, "plan.json"), "w", encoding="utf-8") as fh:
            json.dump({"engine": spec["name"], "chunks": [c.as_dict() for c in self.plan]}, fh)
        self.done = {}
        for ch in self.plan:
            path = self.chunk_path(spec, ch)
            if os.path.isfile(path) and os.path.getsize(path) > 44:
                try:
                    info = audio.sf.info(path)
                    if info.frames > 0:
                        self.done[ch.idx] = info.frames / info.samplerate
                except RuntimeError:
                    os.unlink(path)
        if self.done:
            self.event(f"resuming: {len(self.done)} of {len(self.plan)} chunks already voiced "
                       f"with {spec['name']}")
        self.run_words, self.run_t0 = 0, None
        self._progress()

    def todo(self) -> list[textprep.Chunk]:
        return [c for c in self.plan if c.idx not in self.done]

    def _eta(self) -> float | None:
        """Seconds to done: the rest of the speaking at this run's pace (or the measured pace
        before the first chunk), plus encoding, about 0.06 s per second of audio (measured on a
        full-length episode: 62 s for 1024 s)."""
        left = sum(c.words for c in self.plan if c.idx not in self.done)
        wpm = (self.spec or {}).get("words_per_min") or 165
        encode = 10 + 0.06 * sum(c.words for c in self.plan) * 60 / wpm
        if self.run_words and self.run_t0:
            return round((time.time() - self.run_t0) / self.run_words * left + encode)
        spw = (self.spec or {}).get("sec_per_word")
        return round(spw * left + encode) if spw else None

    def _progress(self) -> None:
        self.status.set(chunks_done=len(self.done), chunks_total=len(self.plan),
                        audio_s=round(sum(self.done.values()), 1), eta_s=self._eta())

    def _synth_one(self, w: Worker, ch: textprep.Chunk) -> None:
        msg = w.synth(ch.idx, ch.text, self.chunk_path(self.spec, ch), self.cancel)
        with self.plock:
            if self.run_t0 is None:
                self.run_t0 = time.time() - float(msg.get("gen_s") or 0)
            self.done[ch.idx] = float(msg.get("audio_s") or 0)
            self.run_words += ch.words
            if int(msg.get("attempts") or 1) > 1 or msg.get("warning"):
                self.m.setdefault("retried_chunks", []).append(
                    {"idx": ch.idx, "words": ch.words, "attempts": msg.get("attempts"),
                     "seed": msg.get("seed"), "warning": msg.get("warning"),
                     "tries": msg.get("tries")})
                if msg.get("warning"):
                    self.event(f"chunk {ch.idx}: {msg['warning']}")
            self._progress()

    def _stop_workers(self) -> None:
        for w in list(self.workers):
            w.stop(grace_s=5)
            self.m.setdefault("workers", []).append(
                {"tag": w.tag, "engine": w.spec["name"], "threads": w.threads,
                 "maxrss_mib": w.maxrss_mib, "gen_s": round(w.gen_s, 1),
                 "load_s": (w.ready or {}).get("load_s")})
            self.workers.remove(w)

    # --------------------------------------------------------------- GPU
    def _wait_for_gpu(self, need: int) -> bool:
        g = self.cfg["gpu"]
        adm = gpu.Admission(need, g["util_max_pct"], g["stable_polls"])
        self.status.set(phase="waiting-for-gpu", engine=None, need_mib=need)
        t0, next_poll = time.time(), 0.0
        try:
            while True:
                self._check_cancel()
                if self.control("use-cpu"):
                    self.gpu_lock.release()
                    self.event("Use CPU voice pressed while waiting for the GPU")
                    return False
                if time.time() >= next_poll:
                    next_poll = time.time() + float(g["poll_s"])
                    have = self.gpu_lock.try_acquire()
                    r = gpu.read(self.cfg["nvidia_smi"], g["index"], g["nvidia_smi_timeout_s"])
                    if not have:
                        adm.streak = 0
                        self.status.set(free_mib=r.free_mib, wait_reason="slot",
                                        wait_text="waiting for GPU: another paper is being voiced on it")
                    else:
                        ok, code, text = adm.step(r)
                        self.status.set(free_mib=r.free_mib, wait_reason=code, wait_text=text)
                        if ok:
                            self.event(f"GPU admitted: {r.free_mib} MiB free, need {need}, "
                                       f"utilisation {r.util_pct}%")
                            return True
                time.sleep(min(0.5, float(g["poll_s"])))
        finally:
            self.m["wait_s"] += time.time() - t0

    def _speak_gpu(self, spec: dict) -> None:
        g = self.cfg["gpu"]
        self.status.set(phase="speaking", engine=f"gpu:{spec['label']}", wait_reason=None,
                        wait_text=None)
        w = Worker(spec, threads=int(spec.get("threads", 4)), gpu_index=int(g["index"]),
                   cfg=self.cfg, tag="gpu")
        self.workers.append(w)
        w.start(self.cancel)
        yr = gpu.YieldRule(g["yield_other_sm_pct"], g["yield_polls"], g["yield_free_floor_mib"])
        last = time.time()
        for ch in self.todo():
            self._check_cancel()
            self._synth_one(w, ch)
            if time.time() - last >= float(g["poll_s"]):
                last = time.time()
                r = gpu.read(self.cfg["nvidia_smi"], g["index"], g["nvidia_smi_timeout_s"],
                             with_pmon=True)
                if r.ok:
                    self.status.set(free_mib=r.free_mib)
                y, why = yr.step(r, procs.descendants(w.pid))
                if y:
                    raise Yielded(why)

    def _gpu_pass(self, script: str, name: str) -> dict | None:
        spec = engine_spec(self.cfg, name)
        need = gpu_need_mib(self.cfg)
        if need is None:
            raise EngineError(f"GPU voice {name} has no measured peak memory in "
                              f"{self.cfg['config_file']}; run install.sh --gpu {name}")
        self._prepare(spec, script)
        ooms, max_ooms = 0, int(self.cfg["gpu"]["max_ooms"])
        while self.todo():
            if not self._wait_for_gpu(need):
                return None
            try:
                self._speak_gpu(spec)
            except EngineOOM as e:
                ooms += 1
                self.event(f"CUDA out of memory ({ooms} of {max_ooms}): {e}")
                if ooms >= max_ooms:
                    raise GpuOOMFailed(f"ran out of GPU memory {ooms} times; last: {e}") from e
                self.status.set(note=f"ran out of GPU memory ({ooms} of {max_ooms}); waiting "
                                     "for the GPU again, finished chunks kept")
            except Yielded as y:
                self.event(f"yielded the GPU: {y}")
                self.status.set(note=str(y))
            finally:
                self._stop_workers()
                self.gpu_lock.release()
        return spec

    # --------------------------------------------------------------- CPU
    def _cpu_pass(self, script: str, why: str) -> dict:
        spec = engine_spec(self.cfg, self.cfg["cpu_engine"])
        self._prepare(spec, script)
        self.event(f"CPU voice ({spec['name']}): {why}")
        t0 = time.time()
        while not self.cpu_lock.try_acquire():
            self._check_cancel()
            self.status.set(phase="preparing", wait_reason="cpu_slot",
                            wait_text="waiting for the CPU voice: another paper is being voiced")
            time.sleep(1.0)
        self.m["cpu_slot_wait_s"] = round(time.time() - t0, 1)
        try:
            self.status.set(phase="speaking", engine=f"cpu:{spec['label']}", wait_reason=None,
                            wait_text=None, need_mib=None, note=why)
            todo = self.todo()
            if not todo:
                return spec
            n = max(1, min(int(self.cfg["cpu"]["workers"]), len(todo)))
            threads = int(self.cfg["cpu"]["threads"])
            ws = [Worker(spec, threads=threads, gpu_index=None, cfg=self.cfg, tag=f"cpu{i}")
                  for i in range(n)]
            self.workers.extend(ws)
            queue_ = list(todo)
            qlock = threading.Lock()
            errors: list[BaseException] = []

            def loop(w: Worker) -> None:
                try:
                    w.start(self.cancel)
                    while True:
                        with qlock:
                            if errors or not queue_:
                                return
                            ch = queue_.pop(0)
                        self._synth_one(w, ch)
                except BaseException as e:  # noqa: BLE001
                    with qlock:
                        errors.append(e)
                    self.cancel.set() if isinstance(e, Cancelled) else None

            ts = [threading.Thread(target=loop, args=(w,), daemon=True) for w in ws]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if errors:
                cancelled = [e for e in errors if isinstance(e, Cancelled)]
                raise cancelled[0] if cancelled else errors[0]
            return spec
        finally:
            self._stop_workers()
            self.cpu_lock.release()

    # --------------------------------------------------------------- encode
    def _encode(self, spec: dict, job: dict) -> dict:
        a, ff = self.cfg["audio"], self.cfg["ffmpeg"]
        self.status.set(phase="encoding", eta_s=round(10 + 0.02 * sum(self.done.values())))
        t0 = time.time()
        missing = [c.idx for c in self.plan if not os.path.isfile(self.chunk_path(spec, c))]
        if missing:
            raise EngineError(f"chunks missing before encoding: {missing[:10]}")
        os.makedirs(self.p("work"), mode=0o700, exist_ok=True)
        os.makedirs(self.p("out"), mode=0o700, exist_ok=True)
        joined = self.p("work", "joined.wav")
        j = audio.join([self.chunk_path(spec, c) for c in self.plan],
                       [c.gap_after_s for c in self.plan], joined, lead_in_s=a["lead_in_s"],
                       threshold_db=a["trim_threshold_db"], pad_s=a["trim_pad_s"])
        part = self.p("out", ".episode.mp3.part")
        n = audio.normalise_encode(ff, joined, part, a, self.p("work"))
        self.m["normalization"] = {"type": n["normalization_type"], "aims": n["aims"]}
        self.m["encode_s"] = round(time.time() - t0, 1)
        written = tags.write(part, job["tags"], replaygain_db=-18.0 - float(n["output_i"]),
                             peak=10 ** (n["output_tp"] / 20))
        m = audio.measure(ff, part, int(a["sample_rate"]))
        self.m["measured"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()}
        problems = []
        if abs(m["lufs"] - a["lufs"]) > 1.0:
            problems.append(f"loudness {m['lufs']:.1f} LUFS, target {a['lufs']}")
        if m["true_peak_db"] > a["true_peak_db"]:
            problems.append(f"true peak {m['true_peak_db']:.1f} dBTP, limit {a['true_peak_db']}")
        if abs(m["duration_s"] - j["duration_s"]) > 0.5 + 0.005 * j["duration_s"]:
            problems.append(f"duration {m['duration_s']:.1f} s, joined {j['duration_s']:.1f} s")
        if (m["codec"], m["channels"], m["sample_rate"], m["bitrate_kbps"]) != (
                "mp3", "mono", int(a["sample_rate"]), int(a["bitrate_kbps"])):
            problems.append(f"stream is {m['codec']} {m['channels']} {m['sample_rate']} Hz "
                            f"{m['bitrate_kbps']} kb/s")
        for reader, got in (("mutagen", tags.read_mutagen(part)), ("ffmpeg", tags.read_ffmpeg(ff, part))):
            problems += [f"tag read back by {reader}: {x}" for x in tags.mismatches(written, got)]
        if problems:
            raise audio.EncodeError("; ".join(problems))
        final = self.p("out", "episode.mp3")
        os.replace(part, final)
        size = os.path.getsize(final)
        if size > 200 * 1024 * 1024:
            raise audio.EncodeError(f"episode is {size} bytes, over 200 MB")
        self.m["encode_s"] = round(time.time() - t0, 1)
        try:
            os.unlink(joined)
        except OSError:
            pass
        return {"path": "out/episode.mp3", "format": "mp3", "bitrate_kbps": m["bitrate_kbps"],
                "duration_s": round(m["duration_s"], 1), "loudness_lufs": round(m["lufs"], 1),
                "true_peak_db": round(m["true_peak_db"], 1), "sha256": sha256_file(final),
                "size": size, "engine": f"{spec['kind']}:{spec['label']}",
                "voice": spec.get("voice"), "tags": written}

    def _cleanup_after_done(self) -> None:
        """Chunk audio is only needed until the episode exists; plan.json files stay."""
        root = self.p("chunks")
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.endswith(".wav"):
                    os.unlink(os.path.join(dirpath, f))
        shutil.rmtree(self.p("work"), ignore_errors=True)

    # --------------------------------------------------------------- run
    def _fail(self, code: str, message: str, rc: int) -> int:
        self.event(f"failed: {code}: {message}")
        self.status.set(phase="failed", error={"code": code, "message": message[:600]}, eta_s=None)
        return rc

    def run(self) -> int:
        dirlock = FileLock(self.p(".voice.lock"))
        if not dirlock.try_acquire():
            log(f"another voice process is working in {self.vdir}; leaving it alone")
            return EXIT_BUSY
        try:
            return self._run()
        finally:
            dirlock.release()

    def _run(self) -> int:
        cur = os.nice(0)
        if cur < int(self.cfg["nice"]):
            os.nice(int(self.cfg["nice"]) - cur)
        self.status.set(phase="preparing", pid=os.getpid())
        self.status.start()
        threading.Thread(target=self._watch, daemon=True).start()
        t0 = time.time()
        log(f"papercast-voice {VERSION} run {self.vdir} (pid {os.getpid()}, nice {os.nice(0)})")
        try:
            job = self._load_job()
            self.status.set(paper_id=job["paper_id"])
            prev = self._previous_output()
            if prev:
                self.event("already done: out/episode.mp3 matches status.json; nothing to do")
                self.status.set(phase="done", output=prev, engine=prev.get("engine"), error=None,
                                eta_s=0)
                return EXIT_OK
            script = self._read_script()
            cpu_spec = engine_spec(self.cfg, self.cfg["cpu_engine"])
            textprep.plan(script, int(cpu_spec["max_words"]), self.cfg["audio"])  # fail early
            self._check_cancel()
            gpu_name = self.cfg.get("gpu_engine")
            if job["engine"] == "cpu":
                spec = self._cpu_pass(script, "the job asked for the CPU voice")
            elif self.control("use-cpu"):
                spec = self._cpu_pass(script, "Use CPU voice (Kokoro) was pressed")
            elif not gpu_name:
                spec = self._cpu_pass(script, "no GPU voice is installed yet, so there is "
                                              "nothing to wait for")
            else:
                spec = self._gpu_pass(script, gpu_name)
                if spec is None:
                    spec = self._cpu_pass(script, "Use CPU voice (Kokoro) was pressed")
            self._check_cancel()
            out = self._encode(spec, job)
            self.status.set(phase="done", output=out, error=None, eta_s=0, wait_reason=None,
                            wait_text=None)
            self.event(f"done: {out['duration_s']} s, {out['loudness_lufs']} LUFS, "
                       f"{out['engine']}, {time.time() - t0:.0f} s in all")
            self._cleanup_after_done()
            return EXIT_OK
        except ScriptInvalid as e:
            return self._fail("script_invalid", str(e), EXIT_SCRIPT)
        except Cancelled:
            return self._fail("cancelled", "stopped because the paper was dismissed", EXIT_CANCELLED)
        except GpuOOMFailed as e:
            return self._fail("gpu_oom", str(e), EXIT_FAILED)
        except (EngineError, JobInvalid) as e:
            return self._fail("engine_failed", str(e), EXIT_FAILED)
        except audio.EncodeError as e:
            return self._fail("encode_failed", str(e), EXIT_FAILED)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._fail("engine_failed", f"{e.__class__.__name__}: {e}", EXIT_FAILED)
        finally:
            self.done_evt.set()
            self._stop_workers()
            self.gpu_lock.release()
            self.cpu_lock.release()
            self.m.update(total_s=round(time.time() - t0, 1), wait_s=round(self.m["wait_s"], 1),
                          finished_at=now_iso(), phase=self.status.get("phase"),
                          engine=self.status.get("engine"), chunks=len(self.plan),
                          words=sum(c.words for c in self.plan),
                          orchestrator_maxrss_mib=round(_maxrss_self_mib()))
            self._write_metrics()
            self.status.stop()

    def _write_metrics(self) -> None:
        path = self.p("metrics.json")
        cur = read_json(path) or {"runs": []}
        cur.setdefault("runs", []).append(self.m)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=1)
        os.replace(tmp, path)


def _maxrss_self_mib() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
