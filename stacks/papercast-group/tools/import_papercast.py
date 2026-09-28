"""Import Leo's papercast episodes into a hub data dir. A dry run unless --apply: it reads the
sources and prints what it would import, and writes nothing anywhere.

    python3 tools/import_papercast.py [--data DIR]                       # what would happen
    python3 tools/import_papercast.py --data DIR --maker EMAIL --apply   # do it

Sources, only ever read (never written, never moved):
- --state (/home/leo/papercast/state): one folder per paper. paper.json (meta: title, authors,
  year, arxiv_id, doi, tags; created_at; the runner's state), final/ (script.md,
  explainer.html, claims.md, meta.json; final/audio/episode.mp3 once voiced), cut/explainer.json
  (the explainer's source; final/ keeps only the built page), voice/status.json (the MP3's
  duration). paper.pdf, concepts.md, text, chat and agent logs are not imported.
- --lineage (NAS_setup/stacks/papercast/web/static/lineage.json): the links, graphs[].edges as
  [parent, child, grade, ...] (parent = the earlier paper), each graph's settled positions
  (nodes[].x/y), and papers{} for the cleaned title, the map label, the corrected year and url.
- --s2-from (papercast-itest/lineage/lineage.json, when there): only its papers{}.s2, the Semantic
  Scholar ids, which the CLI's link finder matches on (the NAS copy leaves them out).

What the hub gets: a paper per folder (or the paper already in the hub with the same arXiv id,
DOI, PDF sha256 or title, in that order: then the episode becomes a version of it), one episode
made by --maker (default: the first of $PCG_ADMIN_EMAILS; created as admin, named --maker-name,
when not in the hub yet), `ready` when the MP3 is there, else `waiting-for-gpu` without a voice
job (Leo's runner is voicing those on stibnite; run the import again later to bring their audio
in; --queue-voice queues them on the hub's voice worker instead); the lineage's links as agent
links (a link already in the hub, or one a person removed, is left as it is); Leo's five graphs
are the seed graphs, with members added or removed where his differ from the tag rule; his
positions as the map layout. No graph_log rows: the import is the starting state, not an edit.

Run again, it adds only what is new: ids come from Leo's folder names, files are copied only when
missing, rows only when absent (an episode that was waiting and now has its MP3 becomes ready).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._common import open_hub, stable_id  # noqa: E402
from hub import db  # noqa: E402

STATE = Path("/home/leo/papercast/state")
LINEAGE = Path("/home/leo/NAS_setup/stacks/papercast/web/static/lineage.json")
S2_FROM = Path("/home/leo/papercast-itest/lineage/lineage.json")
WPM = 150                                                     # the hub's estimate (SPEC.md section 6)
COPY = [("script.md", "final/script.md"), ("explainer.html", "final/explainer.html"),
        ("explainer.json", "cut/explainer.json"), ("claims.md", "final/claims.md"), ("meta.json", "final/meta.json")]


def _json(p: Path):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _words(text: str) -> int:
    return sum(len(line.lstrip("#").split()) for line in text.splitlines())


def paper_id(leo_id: str) -> str:
    return stable_id("p_", "papercast import " + leo_id)


def episode_id(leo_id: str) -> str:
    return stable_id("e_", "papercast import " + leo_id)


def _audio(d: Path, pj: dict):
    """(path, duration_s) of the finished MP3, or (None, None). final/audio first (the runner
    publishes there once the voice is done), else the voice job's output when its status says
    done; a file whose size differs from what the voice reported is still being written."""
    st = _json(d / "voice" / "status.json") or {}
    out = st.get("output") or ((pj.get("voice") or {}).get("output")) or {}
    for p in (d / "final" / "audio" / "episode.mp3", d / "voice" / (out.get("path") or "out/episode.mp3")):
        if p.is_file() and (p.parent.name == "audio" or st.get("phase") == "done"):
            if out.get("size") and p.stat().st_size != out["size"]:
                continue
            return p, out.get("duration_s")
    return None, None


def scan(state: Path, lineage: Path | None, s2_from: Path | None = None) -> dict:
    """Everything the import would bring in, read from Leo's papercast."""
    lin = _json(lineage) if lineage else None
    lpapers = (lin or {}).get("papers") or {}
    s2papers = ((_json(s2_from) or {}).get("papers") or {}) if s2_from else {}
    s2 = {k: v["s2"] for k, v in s2papers.items() if isinstance(v, dict) and v.get("s2")}
    out = {"entries": [], "skipped": [], "links": [], "graphs": [], "lineage": str(lineage) if lin else None}
    for d in sorted(p for p in state.iterdir() if p.is_dir()):
        pj = _json(d / "paper.json")
        if not isinstance(pj, dict):
            out["skipped"].append((d.name, "no paper.json"))
            continue
        meta = pj.get("meta") or {}
        if not (d / "final" / "script.md").is_file():
            out["skipped"].append((d.name, f"no final script (state {pj.get('state')})"))
            continue
        if not meta.get("title"):
            out["skipped"].append((d.name, "no title in paper.json"))
            continue
        lp = lpapers.get(d.name) or {}
        files = {name: d / src for name, src in COPY if (d / src).is_file()}
        audio, dur = _audio(d, pj)
        if audio:
            files["audio.mp3"] = audio
        final = pj.get("final") or {}
        sha = (final.get("paper.pdf") or {}).get("sha256") or pj.get("source_sha256") or pj.get("source_sha256_fetched")
        script = (d / "final" / "script.md").read_text(encoding="utf-8", errors="replace")
        warn = [f"final/{n} is {(d / 'final' / n).stat().st_size} bytes, paper.json says {final[n]['size']}"
                for n in ("script.md", "explainer.html") if n in final and (d / "final" / n).is_file()
                and final[n].get("size") and (d / "final" / n).stat().st_size != final[n]["size"]]
        src = pj.get("source") or {}
        out["entries"].append({
            "leo_id": d.name,
            "title": lp.get("title") or meta["title"],
            "authors": meta.get("authors") or [],
            "year": lp.get("year") or meta.get("year"),
            "arxiv_id": dict(db.identity({"arxiv_id": meta.get("arxiv_id")})).get("arxiv_id"),
            "doi": (meta.get("doi") or "").lower() or None,
            "url": lp.get("url") or src.get("url") or pj.get("fetched_from"),
            "source_sha256": sha if isinstance(sha, str) and len(sha) == 64 else None,
            "s2_id": lp.get("s2") or s2.get(d.name),
            "tags": pj.get("user_tags") or meta.get("tags") or [],
            "label": lp.get("label"),
            "created_at": pj.get("created_at") or db.now(),
            "updated_at": pj.get("state_since") or pj.get("updated_at") or pj.get("created_at") or db.now(),
            "leo_state": pj.get("state"),
            "model": pj.get("model"),
            "files": files,
            "duration_s": dur,
            "words": _words(script),
            "bytes": sum(p.stat().st_size for p in files.values()),
            "warnings": warn,
        })
    if lin:
        seen = set()
        for g in lin.get("graphs") or []:
            for e in g.get("edges") or []:
                if len(e) >= 3 and (e[0], e[1]) not in seen:
                    seen.add((e[0], e[1]))
                    out["links"].append((e[0], e[1], e[2]))
            out["graphs"].append({"key": g.get("key"), "name": g.get("name"), "tags": g.get("tags"),
                                  "nodes": {n["id"]: (n.get("x"), n.get("y")) for n in g.get("nodes") or [] if "id" in n}})
    return out


class Plan:
    """What --apply would do against this hub database (None: an empty hub)."""

    def __init__(self, found: dict, c: sqlite3.Connection | None):
        self.found, self.c = found, c
        self.papers = {}            # leo_id -> hub paper id
        self.new_papers, self.matched, self.new_eps, self.have_eps, self.gain_audio = [], [], [], [], []
        batch = {}                  # (column, value) -> paper id, for papers this import makes
        for e in found["entries"]:
            keys = db.identity(e)
            hit = None
            eid = episode_id(e["leo_id"])
            known = c.execute("SELECT paper_id, state FROM episodes WHERE id = ?", (eid,)).fetchone() if c else None
            if known:
                hit = known[0]
            else:
                for kv in keys:                             # the SPEC's order: the first key decides
                    row = c.execute(f"SELECT id FROM papers WHERE {kv[0]} = ? ORDER BY created_at, id LIMIT 1", (kv[1],)).fetchone() if c else None
                    if row or kv in batch:
                        hit = row[0] if row else batch[kv]
                        break
            if hit is None:
                pid = paper_id(e["leo_id"])
                self.new_papers.append(e)
                for kv in keys:
                    batch.setdefault(kv, pid)
            else:
                pid = hit
                if not known:
                    self.matched.append((e, pid))
            self.papers[e["leo_id"]] = pid
            if known:
                self.have_eps.append(e)
                if known[1] == "waiting-for-gpu" and "audio.mp3" in e["files"]:
                    self.gain_audio.append(e)
            else:
                self.new_eps.append(e)
        self.links_ok, self.links_missing_end, self.links_present, self.links_self = [], [], [], []
        for a, b, g in found["links"]:
            if a not in self.papers or b not in self.papers:
                self.links_missing_end.append((a, b, g))
            elif self.papers[a] == self.papers[b]:
                self.links_self.append((a, b, g))
            elif c and c.execute("SELECT 1 FROM links WHERE src = ? AND dst = ?", (self.papers[a], self.papers[b])).fetchone():
                self.links_present.append((a, b, g))
            elif g not in ("e", "s", "w"):
                self.links_missing_end.append((a, b, g))
            else:
                self.links_ok.append((a, b, g))
        self.graphs = []
        seeds = {k: (n, t) for k, n, t in db.SEED_GRAPHS}
        tags_of = {e["leo_id"]: set(e["tags"]) for e in found["entries"]}
        for g in found["graphs"]:
            if g["key"] not in seeds:
                self.graphs.append({"key": g["key"], "name": g["name"], "skip": "not one of the five seed graphs"})
                continue
            rule = set(g["tags"] or seeds[g["key"]][1])
            nodes = {i for i in g["nodes"] if i in self.papers}
            by_rule = {i for i, t in tags_of.items() if t & rule}
            self.graphs.append({"key": g["key"], "name": g["name"], "gid": db.seed_graph_id(g["key"]),
                                "members": len(nodes), "by_rule": len(by_rule & nodes),
                                "added": sorted(nodes - by_rule), "removed": sorted(by_rule - nodes),
                                "positions": {i: xy for i, xy in g["nodes"].items() if i in self.papers
                                              and xy[0] is not None and xy[1] is not None}})

    def report(self, maker: str | None, listing: bool = False) -> str:
        f, E = self.found, self.found["entries"]
        mb = lambda b: f"{b / 1e6:,.0f} MB"  # noqa: E731
        n = lambda k: sum(1 for e in E if k in e["files"])  # noqa: E731
        audio_b = sum(e["files"]["audio.mp3"].stat().st_size for e in E if "audio.mp3" in e["files"])
        hours = sum(e["duration_s"] or 0 for e in E if "audio.mp3" in e["files"]) / 3600
        skipped = ", ".join(f"{i} ({why})" for i, why in f["skipped"]) or "none"
        lines = [
            f"Leo's papercast: {len(E)} papers with a finished episode; skipped {len(f['skipped'])}: {skipped}",
            f"papers: {len(self.new_papers)} new, {len(self.matched)} already in the hub (the episode becomes a version of it)"
            + (f", {len(self.have_eps)} imported before" if self.have_eps else "")
            + f"; ids: arXiv {sum(1 for e in E if e['arxiv_id'])}, DOI {sum(1 for e in E if e['doi'])}, "
              f"PDF sha256 {sum(1 for e in E if e['source_sha256'])}, Semantic Scholar {sum(1 for e in E if e['s2_id'])}",
            f"episodes: {len(self.new_eps)} new, made by {maker or '(no --maker yet: needed for --apply)'}"
            + (f"; {len(self.gain_audio)} imported before now have their MP3" if self.gain_audio else ""),
            f"  with script {n('script.md')}, explainer page {n('explainer.html')}, explainer.json {n('explainer.json')}, "
            f"claims {n('claims.md')}, meta {n('meta.json')}",
            f"  with audio {n('audio.mp3')} ({mb(audio_b)}, {hours:.1f} h) -> ready; "
            f"{len(E) - n('audio.mp3')} without -> waiting-for-gpu",
            f"  Leo's states: " + ", ".join(f"{s} {sum(1 for e in E if e['leo_state'] == s)}"
                                            for s in sorted({str(e['leo_state']) for e in E})),
            f"links: {len(f['links'])} in {f['lineage'] or '(no lineage.json)'}; {len(self.links_ok)} to add, "
            f"{len(self.links_present)} already in the hub, {len(self.links_missing_end)} with an end not imported "
            f"(or a bad grade), {len(self.links_self)} between two folders of one paper",
            "  grades: " + ", ".join(f"{g} {sum(1 for x in self.links_ok if x[2] == g)}" for g in "esw"),
        ]
        for g in self.graphs:
            if g.get("skip"):
                lines.append(f"graph {g['name']}: skipped, {g['skip']}")
            else:
                lines.append(f"graph {g['name']}: {g['members']} papers ({g['by_rule']} by the tag rule, "
                             f"{len(g['added'])} added, {len(g['removed'])} removed), {len(g['positions'])} positions")
        lines.append(f"files to copy: {mb(sum(e['bytes'] for e in self.new_eps) + sum(e['files']['audio.mp3'].stat().st_size for e in self.gain_audio))}"
                     " (no PDFs)")
        warns = [(e["leo_id"], w) for e in E for w in e["warnings"]]
        for i, w in warns:
            lines.append(f"warning {i}: {w}")
        if listing:
            for e in E:
                flags = "".join(c if k in e["files"] else "-" for c, k in
                                (("S", "script.md"), ("P", "explainer.html"), ("J", "explainer.json"),
                                 ("C", "claims.md"), ("A", "audio.mp3")))
                lines.append(f"  {e['leo_id']}  {self.papers[e['leo_id']]}  {flags}  {e['year']}  {e['title'][:90]}")
        return "\n".join(lines)


def _copy(src: Path, dst: Path) -> bool:
    """src -> dst unless dst is there; through a .part file, so a crash leaves no half file."""
    if dst.exists():
        return False
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return True


def apply(plan: Plan, data: Path, maker_email: str, maker_name: str, queue_voice: bool = False) -> dict:
    cfg = open_hub(data)
    c = db.conn()
    admins = {e.strip().lower() for e in os.environ.get("PCG_ADMIN_EMAILS", "").split(",") if e.strip()}
    maker_email = maker_email.strip().lower()
    done = {"papers": 0, "episodes": 0, "files": 0, "audio_now": 0, "links": 0, "members": 0, "positions": 0, "voice_jobs": 0}
    # files first (outside the transaction); a crash before the rows leaves files a re-run reuses
    for e in plan.new_eps + plan.gain_audio:
        d = cfg.episodes / episode_id(e["leo_id"])
        d.mkdir(parents=True, exist_ok=True)
        for name, src in e["files"].items():
            done["files"] += _copy(src, d / name)
    with db.transaction():
        u = c.execute("SELECT id FROM users WHERE email = ?", (maker_email,)).fetchone()
        if u:
            uid = u[0]
        else:
            role = "admin" if (maker_email in admins or not admins) else "contributor"
            uid = c.execute("INSERT INTO users(email, name, role, created_at) VALUES (?, ?, ?, ?)",
                            (maker_email, maker_name, role, db.now())).lastrowid
        for e in plan.new_papers:
            cur = c.execute(
                "INSERT OR IGNORE INTO papers(id, title, title_norm, authors, year, arxiv_id, doi, url, source_sha256, s2_id, "
                "tags, created_by, created_at, label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (paper_id(e["leo_id"]), e["title"], db.norm_title(e["title"]), db.dumps(e["authors"]), e["year"],
                 e["arxiv_id"], e["doi"], e["url"],
                 e["source_sha256"], e["s2_id"], db.dumps(e["tags"]), uid, e["created_at"], e["label"]))
            done["papers"] += cur.rowcount
        for e in plan.new_eps:
            eid = episode_id(e["leo_id"])
            ready = "audio.mp3" in e["files"]
            cur = c.execute(
                "INSERT OR IGNORE INTO episodes(id, paper_id, made_by, state, state_detail, base_version, prefs, prefs_summary, "
                "client_version, model, words, est_minutes, duration_s, check_report, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, '{}', '', 'papercast-import', ?, ?, ?, ?, '[]', ?, ?)",
                (eid, plan.papers[e["leo_id"]], uid, "ready" if ready else "waiting-for-gpu",
                 f"imported from Leo's papercast ({e['leo_id']})", e["model"], e["words"], round(e["words"] / WPM, 1),
                 e["duration_s"] if ready else None, e["created_at"], e["updated_at"]))
            done["episodes"] += cur.rowcount
            if not ready and queue_voice:
                done["voice_jobs"] += c.execute("INSERT OR IGNORE INTO voice_jobs(episode_id, user_id, state, queued_at) "
                                                "VALUES (?, ?, 'queued', ?)", (eid, uid, db.now())).rowcount
        for e in plan.gain_audio:
            eid = episode_id(e["leo_id"])
            cur = c.execute("UPDATE episodes SET state = 'ready', duration_s = ?, updated_at = ? WHERE id = ? AND state = 'waiting-for-gpu'",
                            (e["duration_s"], db.now(), eid))
            done["audio_now"] += cur.rowcount
            c.execute("DELETE FROM voice_jobs WHERE episode_id = ? AND state = 'queued'", (eid,))
        t = db.now()
        for a, b, g in plan.links_ok:
            done["links"] += c.execute(
                "INSERT OR IGNORE INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 'agent', 'active', ?, ?, ?)", (plan.papers[a], plan.papers[b], g, uid, t, t)).rowcount
        for g in plan.graphs:
            if g.get("skip") or not c.execute("SELECT 1 FROM graphs WHERE id = ?", (g["gid"],)).fetchone():
                continue                                    # a seed graph an admin deleted stays deleted
            for how in ("added", "removed"):
                for i in g[how]:
                    done["members"] += c.execute("INSERT OR IGNORE INTO graph_members(graph_id, paper_id, how) VALUES (?, ?, ?)",
                                                 (g["gid"], plan.papers[i], how)).rowcount
            for i, (x, y) in g["positions"].items():
                done["positions"] += c.execute("INSERT OR IGNORE INTO layout(graph_id, paper_id, x, y, updated_at) VALUES (?, ?, ?, ?, ?)",
                                               (g["gid"], plan.papers[i], float(x), float(y), t)).rowcount
        db.meta_set("import.papercast", db.dumps({"at": t, "maker": maker_email, **done}))
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=str(STATE), help=f"Leo's papercast state dir (default {STATE})")
    ap.add_argument("--lineage", default=str(LINEAGE), help=f"lineage.json with the links and positions (default {LINEAGE})")
    ap.add_argument("--s2-from", default=str(S2_FROM) if S2_FROM.is_file() else None,
                    help="a lineage.json whose papers{}.s2 give the Semantic Scholar ids (default: the itest build's, if there)")
    ap.add_argument("--data", help="the hub data dir (a dry run reads it, if it exists, to show what is already there)")
    admins = [e.strip() for e in os.environ.get("PCG_ADMIN_EMAILS", "").split(",") if e.strip()]
    ap.add_argument("--maker", default=admins[0] if admins else None,
                    help="the email every episode is attributed to (default: the first of $PCG_ADMIN_EMAILS)")
    ap.add_argument("--maker-name", default="Leo", help="the maker's name, if the hub does not know them yet (default Leo)")
    ap.add_argument("--apply", action="store_true", help="write into --data (without it, nothing is written)")
    ap.add_argument("--queue-voice", action="store_true", help="queue the episodes without audio on the hub's voice worker")
    ap.add_argument("--list", action="store_true", help="one line per paper")
    a = ap.parse_args(argv)
    state = Path(a.state)
    if not state.is_dir():
        ap.error(f"--state {state}: no such folder")
    lineage = Path(a.lineage) if a.lineage and Path(a.lineage).is_file() else None
    s2_from = Path(a.s2_from) if a.s2_from and Path(a.s2_from).is_file() else None
    found = scan(state, lineage, s2_from)
    c = None
    if a.data and (Path(a.data) / "hub.db").is_file():
        c = sqlite3.connect(f"file:{Path(a.data).resolve() / 'hub.db'}?mode=ro", uri=True)
    plan = Plan(found, c)
    if c:
        c.close()
    print(plan.report(a.maker, a.list))
    if not a.apply:
        print("Dry run: nothing written. With --data DIR --maker EMAIL --apply it imports.")
        return 0
    if not a.data or not a.maker:
        ap.error("--apply needs --data and --maker (or $PCG_ADMIN_EMAILS)")
    done = apply(plan, Path(a.data), a.maker, a.maker_name, a.queue_voice)
    print("imported: " + ", ".join(f"{k} {v}" for k, v in done.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
