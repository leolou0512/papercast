"""Time the search (hub/search.py) on a big fake library: tools/fake_data.py's papers, each with
a synthetic paper text as long as a real one (Leo's 296 texts: 94 kB on average, 27.7 MB in all),
indexed from nothing, then each query --repeat times.

    python3 tools/searchcheck.py [--papers 1000] [--kb 94] [--repeat 20] [--keep DIR]

For each query: how many papers match, and the median and slowest milliseconds of
- search: hub/search.py's run() (the index, the ranking, the snippets of the first 50),
- answer: that plus the paper views for the matches (web.library) and the JSON the page gets.
Also: how long the first build took and how big search.db is. Nothing in it is real: the text
is made of a small vocabulary of research words and invented ones, drawn at random.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import fake_data  # noqa: E402
from hub import config as C  # noqa: E402
from hub import db, search, web  # noqa: E402

FUNCTION = ("the of and a to in is we that for with as on by this are be an which from our it at can not or its "
            "these their have has was were each where when then than also both such only more most all").split()
RESEARCH = ("model models learning data training network networks neural function loss gradient distribution sample "
            "samples noise policy reward value state action energy structure crystal atoms token tokens attention layer "
            "transformer diffusion score flow matching denoising reinforcement optimization parameters inference generative "
            "latent encoder decoder representation embedding benchmark dataset performance experiments results method "
            "approach algorithm theorem proof equation variance expectation probability entropy kernel graph node edge "
            "message passing equivariant symmetry lattice molecule protein robot manipulation control planning agent "
            "language prompt context sequence convolution image video audio classifier guidance sampling schedule step "
            "continuous discrete stochastic deterministic differential markov chain monte carlo langevin dynamics "
            "hamiltonian variational bound likelihood posterior prior bayesian uncertainty robustness adversarial "
            "regularization normalization batch optimizer momentum convergence generalization supervised unsupervised "
            "contrastive pretraining finetuning evaluation baseline ablation architecture residual recurrent memory "
            "retrieval reasoning trajectory simulation physics interatomic potential forces materials synthesis").split()
SYLL = ("ka lo mi ne tu ra si po va de gri tho quen bla stu mor fen zi pla cro dran sel vek hu jor "
        "pri tas nel om ul ber gan tor lex vin sar quo dex mul rin").split()


class TextMaker:
    """Paper-like text, fast: a long run of words drawn once (function words, research words,
    and a long tail of invented ones with Zipf-like weights), then cut into papers at random."""

    def __init__(self, seed: int = 7, tail: int = 30000, pool_words: int = 3_000_000):
        r = self.r = random.Random(seed)
        words = set()
        while len(words) < tail:
            words.add("".join(r.choice(SYLL) for _ in range(r.randint(2, 4))))
        self.tail = sorted(words)
        vocab = FUNCTION + RESEARCH + self.tail
        w = ([3000.0 / (i + 1) for i in range(len(FUNCTION))] + [60.0 / (1 + i % 40) for i in range(len(RESEARCH))]
             + [40.0 / (i + 1) ** 1.05 for i in range(len(self.tail))])
        draw = r.choices(vocab, weights=w, k=pool_words)
        for i in range(0, len(draw), 17):              # sentences, and some numbers
            draw[i] = draw[i].capitalize()
            if i % 5 == 0:
                draw[i] += f" {r.randint(1, 2025)}"
            if i:
                draw[i - 1] += "."
        self.pool = " ".join(draw)

    def text(self, kb: float, extra: list | None = None) -> str:
        """About kb kilobytes of it, with `extra` words put in here and there."""
        r, n = self.r, int(kb * 1024)
        parts, left = [], n
        while left > 0:
            size = min(left, r.randint(4000, 16000))
            a = r.randrange(0, len(self.pool) - size - 1)
            a = self.pool.find(" ", a) + 1
            parts.append(self.pool[a:a + size])
            left -= size
        out = " ".join(parts)
        for w in extra or []:
            k = r.randrange(0, len(out))
            k = out.find(" ", k) + 1 or len(out)
            out = out[:k] + w + " " + out[k:]
        return out


QUERIES = [
    ("a common word", "learning"),
    ("a topic word", "diffusion"),
    ("two words", "reinforcement learning"),
    ("a phrase", '"denoising diffusion"'),
    ("a rare word in a few texts", "{rare}"),
    ("a person", "{person}"),
    ("typing: a prefix", "diffu"),
    ("typing: a short prefix", "re"),
    ("a typo, corrected", "difusion"),
    ("two typos, corrected", "reinforcment learnig"),
    ("a transposition, corrected", "trasnformer"),
    ("no such word", "zzzzqqqq"),
    ("a word and a filter (a graph)", "attention|graph"),
    ("a filter alone (a graph)", "|graph"),
    ("a filter alone (a year range)", "|years"),
]


def build(data: Path, papers: int, kb: float, seed: int = 1) -> dict:
    """The fake library with a paper text for every paper's first version, indexed."""
    t = time.perf_counter()
    n = fake_data.generate(data, papers=papers, users=5, seed=seed, audio="none", ready_without_audio=True)
    made = time.perf_counter() - t
    cfg = C.Config(data=data)
    db.init(cfg)
    tm = TextMaker(seed)
    t = time.perf_counter()
    rows = db.conn().execute("SELECT paper_id, min(id) FROM episodes WHERE deleted_at IS NULL AND state != 'rejected' "
                             "GROUP BY paper_id").fetchall()
    rare = tm.tail[5000]
    for k, (pid, eid) in enumerate(rows):
        extra = ([rare] if k % 97 == 0 else []) + (["denoising diffusion"] if k % 7 == 0 else [])
        (cfg.episodes / eid / "paper.txt").write_text(tm.text(kb, extra), encoding="utf-8")
    texts = time.perf_counter() - t
    t = time.perf_counter()
    search.sync(cfg, full=True, fuzzy=False)
    index = time.perf_counter() - t
    t = time.perf_counter()
    search.sync(cfg, fuzzy=True)
    vocab = time.perf_counter() - t
    return {"cfg": cfg, "fake": n, "rare": rare, "made_s": made, "texts_s": texts, "index_s": index, "vocab_s": vocab}


def run_queries(cfg, repeat: int, rare: str) -> list:
    c = db.conn()
    uid = c.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]
    person = c.execute("SELECT authors FROM papers LIMIT 1").fetchone()[0]
    person = json.loads(person)[0].split()[-1]
    gid = c.execute("SELECT g.id FROM graphs g JOIN graph_members m ON m.graph_id = g.id GROUP BY g.id "
                    "ORDER BY count(*) DESC LIMIT 1").fetchone()
    gid = gid[0] if gid else c.execute("SELECT id FROM graphs LIMIT 1").fetchone()[0]
    out = []
    for name, q in QUERIES:
        q = q.replace("{rare}", rare).replace("{person}", person)
        f = {}
        if "|" in q:
            q, what = q.split("|")
            f = {"graph": gid} if what == "graph" else {"year_from": 2019, "year_to": 2022}
        ts, ta = [], []
        res = None
        for _ in range(repeat):
            t = time.perf_counter()
            res = search.run(cfg, uid, q, f)
            t1 = time.perf_counter()
            views = {v["id"]: v for v in web.library(cfg, uid, False, pids=res["ids"])} if res["ids"] else {}
            body = json.dumps({"papers": [dict(views[i], match=res["match"].get(i)) for i in res["ids"] if i in views],
                               "search": res["info"]}, ensure_ascii=False).encode()
            t2 = time.perf_counter()
            ts.append((t1 - t) * 1000)
            ta.append((t2 - t) * 1000)
        out.append({"name": name, "q": q, "filters": f, "n": len(res["ids"]), "used": res["info"].get("used"),
                    "search_ms": statistics.median(ts), "search_max": max(ts),
                    "answer_ms": statistics.median(ta), "answer_max": max(ta), "bytes": len(body)})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--papers", type=int, default=1000)
    ap.add_argument("--kb", type=float, default=94.0, help="paper text per paper, kB (Leo's average: 94)")
    ap.add_argument("--repeat", type=int, default=20)
    ap.add_argument("--keep", help="build the library here and keep it (default: a temp dir, removed after)")
    a = ap.parse_args(argv)
    tmp = None
    try:
        if a.keep:
            data = Path(a.keep)
        else:
            tmp = tempfile.mkdtemp(prefix="pcg-searchcheck-")
            data = Path(tmp) / "data"
        b = build(data, a.papers, a.kb)
        cfg = b["cfg"]
        st = search.status(cfg)
        print(f"fake library: {b['fake']['papers']} papers, {b['fake']['episodes']} episodes; paper texts "
              f"{a.kb:g} kB each ({b['texts_s']:.1f} s to write)")
        print(f"first build: {b['index_s']:.1f} s; correction vocabulary: {b['vocab_s']:.1f} s, {st['words']} words; "
              f"search.db {st['bytes'] / 1e6:.0f} MB")
        rows = run_queries(cfg, a.repeat, b["rare"])
        print(f"ms = median of {a.repeat} (slowest); search = search.run, answer = + paper views + JSON")
        print(f"{'query':32} {'q':30} {'papers':>6} {'search':>16} {'answer':>16}  corrected to")
        for r in rows:
            print(f"{r['name'][:32]:32} {r['q'][:30]:30} {r['n']:>6} {r['search_ms']:7.1f} ({r['search_max']:6.1f}) "
                  f"{r['answer_ms']:7.1f} ({r['answer_max']:6.1f})  {r['used'] or ''}")
    finally:
        db.close()
        search.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
