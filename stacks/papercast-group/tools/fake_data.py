"""Fill a hub data dir with a fake library, for UI tests and the tunnel test: nothing in it is
real. Titles and authors are made up to be obviously fake, there are no PDFs and no real paper
text; emails are @example.org, arXiv ids have month 99, DOIs use the 10.5555 test prefix.

    python3 tools/fake_data.py --data DIR [--papers 60] [--users 5] [--seed 1] [--audio auto]

What it makes, all from --seed (the same seed gives the same library):
- users: an admin, contributors, viewers (a disabled one from six users up), each with prefs;
- papers in Leo's five topics (their tags, so the seed graphs gather them), one episode each,
  a second or third version by someone else for some; states mostly ready, some waiting for the
  GPU, one speaking, a few checking, rejected (with a check report) or failed; one soft-deleted;
- per episode: script.md, explainer.json, explainer.html (small, self-contained), claims.md,
  meta.json, bundle-manifest.json, and for a ready one audio.mp3: a short quiet tone made with
  ffmpeg (--audio auto uses ffmpeg from PATH or --ffmpeg; without it there is no audio and the
  ready episodes are waiting-for-gpu instead; --audio none forces that);
- voice_jobs rows that match the states; per user Listened ticks and player positions;
- links between papers, older to newer only (a DAG), graded e/s/w, mostly agent-made, some by
  people, a few removed; the five seed graphs, two made by people (one locked), members added
  to and removed from tag rules; graph_log rows for the people's edits; map positions.

It refuses a data dir that already has papers unless --wipe, which deletes that dir's hub.db
(and -wal, -shm) and episodes/ first.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._common import GROUP, open_hub  # noqa: E402
from hub import config as C  # noqa: E402
from hub import db  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)      # fixed, so a seed always gives the same rows
B32 = "abcdefghijklmnopqrstuvwxyz234567"

FIRST = ["Ada", "Bo", "Cy", "Dee", "Eli", "Flo", "Gus", "Hal", "Ivy", "Jo", "Kit", "Lu", "Mo", "Ned",
         "Oz", "Pip", "Quin", "Roo", "Sol", "Tam", "Uma", "Vic", "Wren", "Xan", "Yul", "Zed"]
LAST = ["Fakerson", "Mockley", "Stubbs", "Placeholder", "Dummington", "Samplewood", "Testova",
        "Notreal", "Madeupski", "Pretendo", "Fictionelli", "Shamworth", "Phonybrook", "Bogusova",
        "Imaginari", "Loremsen", "Ipsumova", "Nulloway", "Voidberg", "Makebelieve"]
USERS = [("Ada Fakerson", "admin"), ("Bo Mockley", "contributor"), ("Cy Stubbs", "contributor"),
         ("Dee Placeholder", "viewer"), ("Eli Dummington", "viewer"), ("Flo Samplewood", "viewer"),
         ("Gus Testova", "contributor"), ("Hal Notreal", "viewer")]

ADJ = ["Wobbly", "Sparkly", "Imaginary", "Pretend", "Toy", "Cardboard", "Fictional", "Mock",
       "Hypothetical", "Invisible", "Whimsical", "Rubber", "Paper-Mache", "Dreamt-Up", "Unlikely",
       "Hollow", "Lukewarm", "Upside-Down", "Pocket-Sized", "Fluffy"]
NOUN = {
    "rl": ["Policy Pudding", "Reward Juggling", "Q-Noodles", "Bandit Soup", "Value Marmalade",
           "Critic Confetti", "Exploration Hopscotch", "Replay Lemonade"],
    "gen": ["Diffusion Doodles", "Score Sprinkles", "Flow Spaghetti", "Denoising Daydreams",
            "Sampler Soup", "Latent Lollipops", "Noise Knitting", "Guidance Gumdrops"],
    "mat": ["Crystal Crumbs", "Lattice Lollipops", "Molecule Marbles", "Potential Pancakes",
            "Bond Origami", "Unit-Cell Umbrellas", "Phonon Popcorn", "Symmetry Sandcastles"],
    "lm": ["Token Towers", "Prompt Pebbles", "Attention Acrobatics", "Context Casseroles",
           "Decoder Dominoes", "Vocabulary Velcro"],
    "robot": ["Gripper Giggles", "Sim-to-Snack Transfer", "Robot Rollerskates", "Arm Wrestling Agents",
              "Teleoperated Teacups"],
}
TAIL = [" for Nonexistent Benchmarks", ": A Fake Study", " at Pretend Scale", " without Any Real Data",
        " Revisited, Not Really", ": Toward Imaginary Robustness", " in Theory Only", " on a Budget of Zero",
        " for the Placeholder Era", ", Explained to Nobody", " under Made-Up Assumptions", ""]
ROMAN = ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
TOPIC_WEIGHT = [("gen", 34), ("rl", 24), ("mat", 22), ("lm", 12), ("robot", 8)]

SUBJ = ["The wobbly estimator", "A pretend encoder", "The toy critic", "This made-up sampler",
        "The imaginary planner", "A cardboard transformer", "The fictional lattice", "Our invented baseline",
        "The rubber gradient", "A hollow decoder", "The placeholder policy", "An unlikely prior"]
VERB = ["nudges", "reshapes", "folds", "averages", "untangles", "stretches", "rearranges", "smooths",
        "balances", "counts", "compresses", "borrows"]
OBJ = ["the imaginary gradient", "a pile of synthetic tokens", "every pretend sample", "the fake loss",
       "a cloud of invented atoms", "the missing reward", "an empty dataset", "the made-up schedule",
       "a queue of dummy states", "the lukewarm noise"]
END = ["without any real data.", "in a world that does not exist.", "for no reason at all.",
       "one pretend step at a time.", "until nothing changes.", "exactly as nobody predicted.",
       "and then forgets about it.", "while the benchmark sleeps.", "because the story needs it.",
       "on a machine that was never built."]
HEAD = ["The setting", "The idea", "How it works", "What it shows", "Where it stops"]

CSS = """\
:root{--bg:#FFFFFF;--text:#111111;--text-2:#5C5C5C;--accent:#C8431A;\
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0E0E0E;--text:#F2F2F2;--text-2:#9E9E9E;--accent:#FF7A45;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:17px/1.55 var(--sans)}
main{max-width:720px;margin:0 auto;padding:48px 24px 64px}
h1{font-size:26px;line-height:1.25;font-weight:700;margin:0 0 28px}
ul{margin:0 0 44px;padding:0 0 0 1.15em}
li{margin:0 0 14px;padding-left:4px}
li::marker{color:var(--accent)}
figure{margin:0 0 44px}
figure svg{display:block;width:100%;height:auto;max-height:70vh;color:var(--text);overflow:visible}
figure svg .hl{color:var(--accent)}
figcaption{margin-top:14px;font-size:15px;line-height:1.5;color:var(--text-2)}
.fake{font-size:13px;color:var(--text-2)}
@media (max-width:480px){main{padding:28px 16px 48px}h1{font-size:22px}body{font-size:16px}}
"""


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake:
    def __init__(self, seed: int):
        self.r = random.Random(seed)
        self.seed = seed

    def rid(self, prefix: str, n: int = 12) -> str:
        return prefix + "".join(self.r.choice(B32) for _ in range(n))

    def sentence(self) -> str:
        r = self.r
        return f"{r.choice(SUBJ)} {r.choice(VERB)} {r.choice(OBJ)} {r.choice(END)}"

    def script(self, title: str, words: int) -> str:
        """Paragraphs of fake sentences under a few headings, about `words` long, spoken style
        (no digits, no symbols), so it would pass the script checks' shape."""
        out, n, h = [f"# {title}", ""], 0, 0
        while n < words:
            if n and h < len(HEAD) and n > (h + 1) * words / (len(HEAD) + 1):
                out += [f"# {HEAD[h]}", ""]
                h += 1
            para = " ".join(self.sentence() for _ in range(self.r.randint(4, 8)))
            out += [para, ""]
            n += len(para.split())
        return "\n".join(out)

    def svg(self) -> str:
        hs = [self.r.randint(20, 100) for _ in range(5)]
        hl = self.r.randrange(5)
        bars = "".join(f'<rect x="{20 + i * 56}" y="{120 - h}" width="36" height="{h}" fill="currentColor"'
                       + (' class="hl"' if i == hl else ' opacity="0.35"') + "/>" for i, h in enumerate(hs))
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 140" role="img">'
                f'<line x1="10" y1="120" x2="310" y2="120" stroke="currentColor" stroke-width="1"/>{bars}</svg>')


def explainer_html(title: str, ex: dict) -> str:
    e = lambda s: html.escape(s or "", quote=True)  # noqa: E731
    body = [f"<h1>{e(title)}</h1>", "<ul>" + "".join(f"<li>{e(p)}</li>" for p in ex["points"]) + "</ul>"]
    for f in ex["figures"]:
        body.append(f"<figure>{f['svg']}<figcaption>{e(f['caption'])}</figcaption></figure>")
    body.append('<p class="fake">Fake data for testing papercast-group: no real paper.</p>')
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>" + "".join(body)
            + "</main></body></html>\n")


_summary = None


def prefs_summary(settings: dict) -> str:
    """The library's few words for a version's preferences, from A9's common/prefs when present."""
    global _summary
    if _summary is None:
        try:
            pkg = str(GROUP.parent.parent / "packages" / "papercast-cli")
            if pkg not in sys.path:
                sys.path.append(pkg)
            from papercast_cli.common import prefs
            _summary = prefs.summary
        except Exception:                               # noqa: BLE001 - a stand-in is fine for fake data
            _summary = lambda s: " · ".join(v for k, v in sorted(s.items()) if v not in ("words", "balanced", "field"))  # noqa: E731
    return _summary(settings)


PREF_CHOICES = {"maths": ("words", "key-steps", "full"), "emphasis": ("balanced", "theory", "method", "practice"),
                "background": ("newcomer", "field", "specialist")}
NOTES = ["", "", "More time on the experiments, please.", "I like the intuition before the maths.",
         "Skip the related work.", "Spell out every symbol."]


def make_tones(ffmpeg: str, out: Path) -> list:
    """Three quiet tones as (seconds, mp3 bytes); [] when ffmpeg cannot make them."""
    tones = []
    for i, (hz, s) in enumerate([(330, 24), (440, 36), (262, 48)]):
        p = out / f"tone{i}.mp3"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
               "-i", f"sine=frequency={hz}:sample_rate=22050:duration={s}",
               "-af", f"volume=0.12,afade=t=in:d=1,afade=t=out:st={s - 1}:d=1",
               "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k", "-map_metadata", "-1",
               "-fflags", "+bitexact", "-flags:a", "+bitexact", str(p)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0 or not p.is_file() or p.stat().st_size < 1000:
            return []
        tones.append((float(s), p.read_bytes()))
    return tones


def wipe(data: Path) -> None:
    for n in ("hub.db", "hub.db-wal", "hub.db-shm"):
        (data / n).unlink(missing_ok=True)
    if (data / "episodes").is_dir():
        shutil.rmtree(data / "episodes")


def generate(data: Path, papers: int = 60, users: int = 5, seed: int = 1, audio: str = "auto",
             ffmpeg: str | None = None, do_wipe: bool = False, ready_without_audio: bool = False) -> dict:
    """The library above; `ready_without_audio` is for measurements only (tools/loadcheck.py):
    without tones, ready episodes stay ready, with a duration but no audio.mp3."""
    data = Path(data).expanduser().resolve()
    if (data / "hub.db").exists():
        db.init(C.Config(data=data))
        try:
            has = db.conn().execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        except Exception:                                  # noqa: BLE001 - not a hub database yet
            has = 0
        db.close()
        if has and not do_wipe:
            raise SystemExit(f"{data} already has {has} papers: pass --wipe to replace them")
        if do_wipe:
            wipe(data)
    cfg = open_hub(data)
    F = Fake(seed)
    r = F.r
    c = db.conn()

    # -- audio
    tones = []
    if audio != "none":
        exe = ffmpeg or shutil.which("ffmpeg")
        if exe:
            with tempfile.TemporaryDirectory(prefix="pcg-fake-tones-") as tmp:
                tones = make_tones(exe, Path(tmp))
        if audio == "tone" and not tones:
            raise SystemExit("--audio tone: ffmpeg (with libmp3lame) is needed; give --ffmpeg PATH")
    made_tones = bool(tones)
    if ready_without_audio and not tones:
        tones = [(24.0, None), (36.0, None), (48.0, None)]

    counts = {"users": 0, "papers": 0, "episodes": 0, "audio": 0, "links": 0, "listened": 0,
              "positions": 0, "graph_log": 0, "graphs": 0}
    with db.transaction():
        # the seeds' clock times, pinned so that one seed gives one library
        c.execute("UPDATE graphs SET created_at = ? WHERE created_by IS NULL", (iso(T0),))
        c.execute("UPDATE base_prompts SET created_at = ?", (iso(T0),))
        c.execute("UPDATE meta SET value = ? WHERE key = 'seeded.graphs'", (iso(T0),))
        # -- base prompt: the seeded one, else a fake v1 so the admin page has one
        if not c.execute("SELECT 1 FROM base_prompts").fetchone():
            src = db.seed_sources()
            wording = json.loads(src["wording"].read_text()) if "wording" in src else {}
            c.execute("INSERT INTO base_prompts(version, guideline, wording, created_by, created_at) VALUES (1, ?, ?, NULL, ?)",
                      ("# Fake base guideline\n\nFake data for UI tests: the real guideline is seeded by "
                       "`python3 -m hub.db migrate` once prompts/base-guideline.md exists.\n",
                       db.dumps(wording), iso(T0)))

        # -- users
        people = []
        for i in range(users):
            name, role = USERS[i] if i < len(USERS) else (f"{FIRST[i % len(FIRST)]} {LAST[i % len(LAST)]}", "viewer")
            if i >= len(USERS):
                name = f"{name} {ROMAN[(i // len(FIRST)) % len(ROMAN)]}"
            email = name.lower().replace(" ", ".") + "@example.org"
            disabled = 1 if users >= 6 and i == users - 1 else 0
            cur = c.execute("INSERT INTO users(email, name, role, disabled, created_at) VALUES (?, ?, ?, ?, ?)",
                            (email, name, role, disabled, iso(T0 + timedelta(hours=i))))
            uid = cur.lastrowid
            settings = {k: r.choice(v) for k, v in PREF_CHOICES.items()}
            c.execute("INSERT INTO prefs(user_id, settings, note, version, updated_at) VALUES (?, ?, ?, ?, ?)",
                      (uid, db.dumps(settings), r.choice(NOTES), r.randint(1, 4), iso(T0 + timedelta(hours=i, minutes=5))))
            people.append({"id": uid, "name": name, "role": role, "disabled": disabled, "settings": settings})
        counts["users"] = len(people)
        makers = [p for p in people if p["role"] in ("admin", "contributor") and not p["disabled"]] or people[:1]

        # -- papers, in the order they were added; years are independent of that order
        topics = [t for t, w in TOPIC_WEIGHT for _ in range(w)]
        tagsets = {k: tags for k, _, tags in db.SEED_GRAPHS}
        seen_titles: dict = {}
        plist = []
        span = timedelta(days=25) / max(1, papers)
        for i in range(papers):
            topic = r.choice(topics)
            base = f"{r.choice(ADJ)} {r.choice(NOUN[topic])}{r.choice(TAIL)}"
            k = seen_titles.get(base, 0)
            seen_titles[base] = k + 1
            title = base if not k else f"{base} {ROMAN[(k - 1) % len(ROMAN)]}" + ("" if k <= len(ROMAN) else f" {k}")
            tags = r.sample(tagsets[topic], k=min(len(tagsets[topic]), r.choice([1, 1, 2, 2, 3])))
            if r.random() < 0.15:                          # a paper in two topics
                other = r.choice([t for t in tagsets if t != topic])
                tags.append(r.choice(tagsets[other]))
            year = int(min(2026, max(1995, round(2026 - abs(r.gauss(0, 5))))))
            authors = [f"{r.choice(FIRST)} {r.choice(LAST)}" for _ in range(r.randint(1, 6))]
            pid = F.rid("p_")
            ax = f"{year % 100:02d}99.{i + 1:05d}" if r.random() < 0.85 else None
            doi = None if ax else f"10.5555/fake.{seed}.{i + 1}"
            words = title.replace(":", "").replace(",", "").split()
            label = " ".join(words[1:3]) if len(words) > 2 else title
            maker = r.choice(makers)
            at = T0 + span * i + timedelta(minutes=r.randint(0, 50))
            p = {"id": pid, "title": title, "authors": authors, "year": year, "arxiv_id": ax, "doi": doi,
                 "url": f"https://example.org/fake-papers/{pid}", "tags": tags, "label": label, "maker": maker,
                 "at": at, "topic": topic, "i": i}
            c.execute("INSERT INTO papers(id, title, title_norm, authors, year, arxiv_id, doi, url, source_sha256, s2_id, "
                      "tags, created_by, created_at, label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                      (pid, title, db.norm_title(title), db.dumps(authors), year, ax, doi, p["url"],
                       hashlib.sha256(f"fake paper {seed} {i}".encode()).hexdigest(), db.dumps(tags),
                       maker["id"], iso(at), label))
            plist.append(p)
        counts["papers"] = len(plist)

        # -- episodes
        eps = []
        speaking_left = 1
        for p in plist:
            n = 1 + (r.random() < 0.25) + (r.random() < 0.08)
            who = [p["maker"]] + r.sample([m for m in makers if m is not p["maker"]], k=min(n - 1, len(makers) - 1))
            for v, m in enumerate(who):
                x = r.random()
                if x < 0.72:
                    state = "ready"
                elif x < 0.86:
                    state = "waiting-for-gpu"
                elif x < 0.90:
                    state = "checking"
                elif x < 0.96:
                    state = "rejected"
                else:
                    state = "failed"
                if state == "waiting-for-gpu" and speaking_left:
                    state, speaking_left = "speaking", 0
                if state == "ready" and not tones:
                    state = "waiting-for-gpu"
                eps.append({"paper": p, "maker": m, "v": v, "state": state, "id": F.rid("e_"),
                            "at": p["at"] + timedelta(minutes=5 + 90 * v + r.randint(0, 30))})
        deleted_one = next((e for e in eps if e["v"] > 0), None)
        for e in eps:
            p, m = e["paper"], e["maker"]
            settings = {k: r.choice(v) for k, v in PREF_CHOICES.items()} if e["v"] else dict(m["settings"])
            est = round(r.uniform(15.5, 24.5), 1)
            if e["state"] == "rejected":
                est = round(r.choice([r.uniform(6, 12), r.uniform(28, 34)]), 1)
            words = int(est * 150)
            report, detail, dur = [], None, None
            if e["state"] == "rejected":
                report = [f"the script is about {round(est)} minutes; it needs 15-25 minutes"]
                if r.random() < 0.5:
                    report.append('names the listener, in 1 sentence: (1) "Thanks, Ada, for listening." (Ada)')
                detail = "the checks refused it"
            elif e["state"] == "failed":
                detail = "voice failed: the fake GPU ran out of memory"
            elif e["state"] == "speaking":
                detail = "speaking: part 12 of 40"
            tone = r.choice(tones) if tones else None
            if e["state"] == "ready":
                dur = tone[0]
            updated = e["at"] + timedelta(minutes=r.randint(10, 400))
            deleted = iso(updated + timedelta(days=1)) if e is deleted_one else None
            c.execute("INSERT INTO episodes(id, paper_id, made_by, state, state_detail, base_version, prefs, prefs_summary, "
                      "client_version, model, words, est_minutes, duration_s, check_report, created_at, updated_at, deleted_at) "
                      "VALUES (?, ?, ?, ?, ?, 1, ?, ?, '0.1.0', 'claude-opus-5-5', ?, ?, ?, ?, ?, ?, ?)",
                      (e["id"], p["id"], m["id"], e["state"], detail, db.dumps(settings), prefs_summary(settings),
                       words, est, dur, db.dumps(report), iso(e["at"]), iso(updated), deleted))
            # files
            d = cfg.episodes / e["id"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "script.md").write_text(F.script(p["title"], words), encoding="utf-8")
            ex = {"points": [F.sentence() for _ in range(r.randint(3, 5))],
                  "figures": [{"svg": F.svg(), "caption": F.sentence()} for _ in range(r.randint(1, 2))]}
            (d / "explainer.json").write_text(json.dumps(ex, indent=1), encoding="utf-8")
            (d / "explainer.html").write_text(explainer_html(p["title"], ex), encoding="utf-8")
            (d / "claims.md").write_text("".join(f"- {F.sentence()}\n" for _ in range(3)), encoding="utf-8")
            meta = {"title": p["title"], "authors": p["authors"], "first_author": p["authors"][0],
                    "arxiv_id": p["arxiv_id"], "doi": p["doi"], "year": p["year"], "venue": None, "fake": True}
            (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            manifest = {"manifest_version": 1, "client_version": "0.1.0", "base_version": 1,
                        "prefs": {"settings": settings, "note": "", "version": 1}, "model": "claude-opus-5-5",
                        "claim_id": None if e["v"] else F.rid("c_"), "paper_id": p["id"] if e["v"] else None,
                        "paper": {k: p[k] for k in ("title", "authors", "year", "arxiv_id", "doi", "url", "tags")},
                        "files": {"script": "script.md", "explainer_json": "explainer.json",
                                  "explainer_html": "explainer.html", "claims": "claims.md"},
                        "links": [], "stats": {"words": words, "est_minutes": est, "wall_s": r.randint(900, 2400)}}
            (d / "bundle-manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
            if e["state"] == "ready" and tone[1] is not None:
                (d / "audio.mp3").write_bytes(tone[1])
                counts["audio"] += 1
            e["duration"] = dur
            # the voice queue's row
            vj = {"waiting-for-gpu": ("queued", None, None, None, None, 0, None),
                  "speaking": ("claimed", iso(updated), iso(updated + timedelta(minutes=2)), None, "speaking", 1, None),
                  "ready": ("done", iso(updated - timedelta(minutes=30)), iso(updated), iso(updated), "done", 1, None),
                  "failed": ("failed", iso(updated - timedelta(minutes=20)), iso(updated), iso(updated), "speaking", 2,
                             "CUDA out of memory (fake)")}.get(e["state"])
            if vj:
                state, claimed, beat, fin, phase, attempts, err = vj
                progress = {"queued": None, "claimed": 0.3, "done": 1.0, "failed": 0.6}[state]
                c.execute("INSERT INTO voice_jobs(episode_id, user_id, state, queued_at, claimed_at, heartbeat_at, finished_at, "
                          "phase, progress, attempts, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (e["id"], m["id"], state, iso(e["at"] + timedelta(minutes=3)), claimed, beat, fin, phase,
                           progress, attempts, err))
        counts["episodes"] = len(eps)

        # -- listened and positions, per user
        ready_eps = [e for e in eps if e["state"] == "ready" and e is not deleted_one]
        ready_papers = sorted({e["paper"]["id"] for e in ready_eps})
        t_last = T0 + timedelta(days=26)
        for u in people:
            for pid in ready_papers:
                if r.random() < 0.3:
                    c.execute("INSERT INTO listened(user_id, paper_id, at) VALUES (?, ?, ?)",
                              (u["id"], pid, iso(t_last + timedelta(minutes=r.randint(0, 2000)))))
                    counts["listened"] += 1
            for e in ready_eps:
                if r.random() < 0.15:
                    s = round(r.uniform(2, max(3.0, e["duration"] - 2)), 1)
                    c.execute("INSERT INTO positions(user_id, episode_id, seconds, updated_at) VALUES (?, ?, ?, ?)",
                              (u["id"], e["id"], s, iso(t_last + timedelta(minutes=r.randint(0, 2000)))))
                    counts["positions"] += 1

        # -- links: older to newer only, so they form a DAG
        order = sorted(plist, key=lambda p: (p["year"], p["i"]))
        rank = {p["id"]: k for k, p in enumerate(order)}
        log = []                                           # (at, user, actor, op, target, before, after)
        links = []
        for k, p in enumerate(order):
            if k < 3:
                continue
            npar = r.choice([0, 1, 1, 1, 2, 2, 2, 3, 3])
            same = [q for q in order[:k] if q["topic"] == p["topic"] and q["year"] < p["year"]]
            anyp = [q for q in order[:k] if q["year"] < p["year"]]
            chosen = []
            for _ in range(npar):
                pool = same if same and r.random() < 0.8 else anyp
                pool = [q for q in pool if q not in chosen]
                if not pool:
                    break
                chosen.append(r.choice(pool[-40:]))       # mostly recent ancestors, as citations are
            for q in chosen:
                grade = r.choice("eessswww")
                human = r.random() < 0.15
                who = r.choice([u for u in people if not u["disabled"]]) if human else p["maker"]
                at = p["at"] + timedelta(minutes=r.randint(30, 3000))
                state = "removed" if (not human and r.random() < 0.05) else "active"
                cur = c.execute("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (q["id"], p["id"], grade, "human" if human else "agent", "active", who["id"], iso(at), iso(at)))
                lk = {"id": cur.lastrowid, "src": q["id"], "dst": p["id"], "grade": grade,
                      "origin": "human" if human else "agent", "state": "active"}
                links.append(lk)
                log.append((at, who["id"], "human" if human else "agent", "link.add", str(lk["id"]), None, dict(lk)))
                if state == "removed":
                    remover = r.choice([u for u in people if not u["disabled"]])
                    at2 = at + timedelta(hours=r.randint(1, 48))
                    after = dict(lk, state="removed")
                    c.execute("UPDATE links SET state = 'removed', updated_at = ? WHERE id = ?", (iso(at2), lk["id"]))
                    log.append((at2, remover["id"], "human", "link.remove", str(lk["id"]), dict(lk), after))
                    lk.update(after)
        active = [x for x in links if x["state"] == "active"]
        for lk in r.sample(active, k=min(6, len(active))):
            new = r.choice([g for g in "esw" if g != lk["grade"]])
            u = r.choice([u for u in people if not u["disabled"]])
            at = T0 + timedelta(days=26, minutes=r.randint(0, 1000))
            c.execute("UPDATE links SET grade = ?, updated_at = ? WHERE id = ?", (new, iso(at), lk["id"]))
            log.append((at, u["id"], "human", "link.grade", str(lk["id"]), dict(lk), dict(lk, grade=new)))
            lk["grade"] = new
        counts["links"] = len(links)
        assert all(rank[x["src"]] < rank[x["dst"]] for x in links)

        # -- graphs: the seeds, two made by people, members added to and removed from tag rules
        admin = next(u for u in people if u["role"] == "admin")
        g_rl, g_lm = db.seed_graph_id("rl"), db.seed_graph_id("lm")
        mine = F.rid("g_", 10)
        picks = F.rid("g_", 10)
        t = T0 + timedelta(days=26, hours=2)
        u2 = people[min(1, len(people) - 1)]
        c.execute("INSERT INTO graphs(id, name, rule_tags, locked, created_by, created_at) VALUES (?, ?, '[]', 0, ?, ?)",
                  (mine, "Reading group, autumn (fake)", u2["id"], iso(t)))
        log.append((t, u2["id"], "human", "graph.create", mine, None, {"name": "Reading group, autumn (fake)", "rule_tags": [], "locked": 0}))
        c.execute("INSERT INTO graphs(id, name, rule_tags, locked, created_by, created_at) VALUES (?, ?, ?, 0, ?, ?)",
                  (picks, "Admin's picks (fake)", db.dumps(["flow matching", "policy gradient"]), admin["id"], iso(t + timedelta(minutes=3))))
        log.append((t + timedelta(minutes=3), admin["id"], "human", "graph.create", picks, None,
                    {"name": "Admin's picks (fake)", "rule_tags": ["flow matching", "policy gradient"], "locked": 0}))
        c.execute("UPDATE graphs SET locked = 1 WHERE id = ?", (picks,))
        log.append((t + timedelta(minutes=4), admin["id"], "human", "graph.lock", picks, {"locked": 0}, {"locked": 1}))
        for k, p in enumerate(r.sample(plist, k=min(12, len(plist)))):
            c.execute("INSERT INTO graph_members(graph_id, paper_id, how) VALUES (?, ?, 'added')", (mine, p["id"]))
            log.append((t + timedelta(minutes=10 + k), u2["id"], "human", "graph.add_paper", f"{mine}/{p['id']}", None, {"how": "added"}))
        rl_members = [p for p in plist if set(p["tags"]) & set(tagsets["rl"])]
        for k, p in enumerate(r.sample(rl_members, k=min(2, len(rl_members)))):
            c.execute("INSERT INTO graph_members(graph_id, paper_id, how) VALUES (?, ?, 'removed')", (g_rl, p["id"]))
            log.append((t + timedelta(minutes=40 + k), admin["id"], "human", "graph.remove_paper", f"{g_rl}/{p['id']}", None, {"how": "removed"}))
        outside = [p for p in plist if not set(p["tags"]) & set(tagsets["lm"])]
        if outside:
            p = r.choice(outside)
            c.execute("INSERT INTO graph_members(graph_id, paper_id, how) VALUES (?, ?, 'added')", (g_lm, p["id"]))
            log.append((t + timedelta(minutes=50), u2["id"], "human", "graph.add_paper", f"{g_lm}/{p['id']}", None, {"how": "added"}))
        for at, uid, actor, op, target, before, after in sorted(log, key=lambda x: (x[0], x[4])):
            c.execute("INSERT INTO graph_log(at, user_id, actor, op, target, before, after) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (iso(at), uid, actor, op, target, None if before is None else db.dumps(before),
                       None if after is None else db.dumps(after)))
        counts["graph_log"] = len(log)

        # -- map positions: time runs left to right, a little spread top to bottom
        members = {}
        for row in c.execute("SELECT id, rule_tags FROM graphs ORDER BY id").fetchall():
            gid, rule = row["id"], json.loads(row["rule_tags"])
            how = {row["paper_id"]: row["how"] for row in c.execute("SELECT paper_id, how FROM graph_members WHERE graph_id = ?", (gid,))}
            members[gid] = [p for p in order if (set(p["tags"]) & set(rule) or how.get(p["id"]) == "added") and how.get(p["id"]) != "removed"]
        for gid in sorted(members):
            ms = members[gid]
            for k, p in enumerate(ms):
                x = round(-300 + 600 * (k / max(1, len(ms) - 1)), 1)
                y = round(r.uniform(-220, 220), 1)
                c.execute("INSERT INTO layout(graph_id, paper_id, x, y, updated_at) VALUES (?, ?, ?, ?, ?)",
                          (gid, p["id"], x, y, iso(T0 + timedelta(days=27))))
        counts["graphs"] = len(members)
    counts["data"] = str(data)
    counts["tones"] = made_tones
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="the hub data dir to fill")
    ap.add_argument("--papers", type=int, default=60)
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--audio", choices=["auto", "tone", "none"], default="auto",
                    help="auto: a tone per ready episode when ffmpeg is there; tone: insist; none: no audio")
    ap.add_argument("--ffmpeg", help="the ffmpeg to use (default: ffmpeg on PATH)")
    ap.add_argument("--wipe", action="store_true", help="delete this data dir's hub.db and episodes/ first")
    a = ap.parse_args(argv)
    if a.papers < 1 or a.users < 1:
        ap.error("--papers and --users must be at least 1")
    n = generate(Path(a.data), a.papers, a.users, a.seed, a.audio, a.ffmpeg, a.wipe)
    print(f"{n['data']}: {n['users']} users, {n['papers']} papers, {n['episodes']} episodes "
          f"({n['audio']} with audio{'' if n['tones'] else '; no ffmpeg, so none voiced'}), {n['links']} links, "
          f"{n['graphs']} graphs, {n['graph_log']} log rows, {n['listened']} listened, {n['positions']} positions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
