"""`papercast relink` (relink.py) against a fake hub (the rules of POST /api/cli/papers/<id>/links,
in small), a fake Semantic Scholar and a fake grader: missing links found both ways and added, a
person's links and removals never graded or sent, the agent's own links regraded or removed at
"none", the dry run, a second run that asks nothing and changes nothing, the usage limit, the
pacing, and the command line. No network, no real Claude."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from papercast_cli import cli, relink  # noqa: E402
from papercast_cli.errors import NotFound, PapercastError  # noqa: E402
from papercast_cli.pipeline import links as L  # noqa: E402
from papercast_cli.pipeline.errors import usage_limit  # noqa: E402

A, B, C, D, E = "p_ddpm", "p_sde", "p_fm", "p_rf", "p_cm"
PAPERS = [
    {"id": A, "title": "Denoising Diffusion Probabilistic Models", "year": 2020, "arxiv_id": "2006.11239",
     "claims": ["Diffusion models make high quality samples."], "text": True},
    {"id": B, "title": "Score-Based Generative Modeling through Stochastic Differential Equations", "year": 2021,
     "arxiv_id": "2011.13456", "claims": ["A reverse-time SDE generates data."], "text": True},
    {"id": C, "title": "Flow Matching for Generative Modeling", "year": 2023, "arxiv_id": "2210.02747", "text": True},
    {"id": D, "title": "Rectified Flow for Fast Sampling of Images", "year": 2023, "arxiv_id": "2209.03003", "text": True},
    {"id": E, "title": "Consistency Models for One Step Generation", "year": 2024, "arxiv_id": "2303.01469", "text": True},
    {"id": "p_hidden", "title": "A Paper Whose Only Episode Was Rejected Long Ago", "year": 2022, "visible": False},
]
TITLE = {p["id"]: p["title"] for p in PAPERS}


def s2_rec(pid):
    p = next(x for x in PAPERS if x["id"] == pid)
    return {"paperId": "S2" + pid, "title": p["title"], "year": p["year"], "externalIds": {"ArXiv": p["arxiv_id"]}}


class FakeS2:
    """Semantic Scholar: C's references hold A and B; B's hold A; D's hold A; A's citations hold
    C and E (E itself is unknown to it, so A -> E is found from A's side only)."""

    def __init__(self):
        refs = {C: [A, B], B: [A], D: [A]}
        cits = {A: [C, E]}
        self.answers = {}
        for p in (A, B, C, D):
            self.answers[f"/paper/ARXIV:{s2_rec(p)['externalIds']['ArXiv']}"] = s2_rec(p)
            self.answers[f"/paper/S2{p}/references"] = {"data": [{"isInfluential": False, "citedPaper": s2_rec(x)}
                                                                 for x in refs.get(p, [])], "next": None}
            self.answers[f"/paper/S2{p}/citations"] = {"data": [{"isInfluential": False, "citingPaper": s2_rec(x)}
                                                                for x in cits.get(p, [])], "next": None}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        u = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(u.path).replace("/graph/v1", "", 1)
        return (200, self.answers[path], {}) if path in self.answers else (404, None, {})


class FakeHub:
    """GET /api/cli/relink, /api/cli/mentions and POST /api/cli/papers/<id>/links, with the hub's
    rules in small: a person's link or a removed one is never touched, the agent's own is
    regraded or removed at "none", new ones added (suggested while "suggest"); dry_run keeps nothing."""

    server = "https://hub.test"

    def __init__(self, mentions=None, mode="auto"):
        self.papers = [dict(p) for p in PAPERS]
        self.links = {}
        self.suggestions = []
        self.mode = mode
        self.mentioned_by = mentions if mentions is not None else {B: [D, E]}   # D's and E's texts name B
        self.calls = []
        self.log = 0

    def link(self, src, dst, grade, origin="agent", state="active", person=None):
        self.links[(src, dst)] = {"id": len(self.links) + 1, "src": src, "dst": dst, "grade": grade, "origin": origin,
                                  "state": state, "person": origin == "human" if person is None else person}

    def get(self, path, params=None, **kw):
        self.calls.append(("GET", path, params))
        if path == "/api/cli/relink":
            return {"papers": [dict(p, visible=p.get("visible", True)) for p in self.papers],
                    "links": [dict(l) for l in self.links.values()], "suggestions": list(self.suggestions),
                    "agent_links": self.mode, "graphs": [], "log_max": self.log}
        if path == "/api/cli/mentions":
            q = params["exclude"]
            return {"mentions": [{"paper_id": p, "field": "title"} for p in self.mentioned_by.get(q, [])], "info": {}}
        raise NotFound(f"no {path}")

    def post(self, path, body, **kw):
        self.calls.append(("POST", path, body))
        if path == "/api/cli/relink/restructure":             # (its rules are the hub's own tests')
            n = sum(1 for l in self.links.values() if l["state"] == "active")
            return {"changes": [], "skipped": [], "unchanged": len(body["links"]), "papers": [], "graphs": [],
                    "totals": {"before": n, "after": n}, "log_ids": [], "dry_run": bool(body.get("dry_run")), "mode": self.mode}
        pid = re.match(r"^/api/cli/papers/([^/]+)/links$", path).group(1)
        changes, skipped, unchanged = [], [], 0
        snapshot = {k: dict(v) for k, v in self.links.items()}
        for k, it in enumerate(body["links"]):
            o = it["other"]["paper_id"]
            src, dst = (o, pid) if it["direction"] == "builds_on" else (pid, o)
            g, row = it["grade"], self.links.get((src, dst))
            if row is None:
                if g == "none":
                    unchanged += 1
                elif self.mode == "suggest":
                    self.suggestions.append({"src": src, "dst": dst, "grade": g, "state": "open"})
                    changes.append({"index": k, "op": "suggest", "src": src, "dst": dst, "grade": g})
                else:
                    self.link(src, dst, g)
                    self.log += 1
                    changes.append({"index": k, "op": "add", "src": src, "dst": dst, "grade": g})
            elif row["state"] == "removed" or row["person"]:
                skipped.append({"index": k, "src": src, "dst": dst, "reason": "a person's link"})
            elif row["grade"] == g:
                unchanged += 1
            else:
                op = "remove" if g == "none" else "regrade"
                changes.append({"index": k, "op": op, "src": src, "dst": dst, "grade": g, "was": row["grade"]})
                row.update(state="removed") if g == "none" else row.update(grade=g)
                self.log += 1
        if body.get("dry_run"):
            self.links = snapshot
            self.log -= sum(1 for c in changes if c["op"] != "suggest")
        return {"paper_id": pid, "mode": self.mode, "dry_run": bool(body.get("dry_run")), "changes": changes,
                "skipped": skipped, "unchanged": unchanged, "log_ids": []}

    def posts(self):
        return {c[1].split("/")[4]: sorted((l["other"]["paper_id"], l["grade"]) for l in c[2]["links"])
                for c in self.calls if c[0] == "POST"}


class FakeGrader:
    """Answers each prompt's candidates from `table` {(child, parent): grade} (weak otherwise)."""

    def __init__(self, table, fail_after=None):
        self.table, self.prompts, self.fail_after = table, [], fail_after
        self.by_title = {t: pid for pid, t in TITLE.items()}

    def pairs(self, prompt):
        out, child = [], None
        for line in prompt.splitlines():
            m = re.match(r'^CHILD: "(.*)" \(', line)
            if m:
                child = self.by_title[m.group(1)]
            m = re.match(r'^\s+\[(\d+)\] "(.*)" \(', line)
            if m:
                out.append((child, self.by_title[m.group(2)]))
        return out

    def __call__(self, prompt):
        if self.fail_after is not None and len(self.prompts) >= self.fail_after:
            raise usage_limit(1.9e9, None, True, "Claude usage limit reached", "test")
        self.prompts.append(prompt)
        ans = [{"i": i, "g": self.table.get(p, "weak")} for i, p in enumerate(self.pairs(prompt), 1)]
        return json.dumps({"answers": ans})

    def seen(self):
        return {p for pr in self.prompts for p in self.pairs(pr)}


GRADES = {(B, A): "strong", (C, A): "essential", (E, A): "weak", (D, B): "strong", (E, B): "none", (C, B): "strong"}


class RelinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "relink"
        self.hub = FakeHub()
        h = self.hub
        h.link(A, B, "w")                                   # the agent's, graded strong now: regraded
        h.link(B, C, "w", origin="human")                   # a person's: never graded, never sent
        h.link(A, D, "s", state="removed", person=True)     # removed by a person: stays removed
        h.link(B, E, "w")                                   # the agent's, graded none now: removed
        h.link(C, E, "s")                                   # the agent's, no candidate for it: left alone
        self.grader = FakeGrader(GRADES)

    def tearDown(self):
        self.tmp.cleanup()

    def run_relink(self, grader=None, **kw):
        kw.setdefault("fetch", FakeS2())
        return relink.run(self.hub, grade=grader or self.grader, root=self.root, parallel=2, **kw)

    def ops(self, res):
        return sorted((c["op"], c["src"], c["dst"], c["grade"]) for c in res["changes"])

    WANT = sorted([("add", A, C, "e"), ("add", A, E, "w"), ("add", B, D, "s"), ("regrade", A, B, "s"), ("remove", B, E, "none")])

    def test_adds_missing_links_both_ways_and_regrades_the_agents(self):
        res = self.run_relink()
        self.assertEqual(self.ops(res), self.WANT)
        ev = {(c["src"], c["dst"]): c["evidence"] for c in res["changes"]}
        self.assertEqual(ev[(A, E)], ["s2"])                        # from A's citations only
        self.assertEqual(ev[(B, D)], ["text:title"])                # D's text names B (the hub's index)
        self.assertEqual(ev[(B, E)], ["text:title"])
        self.assertEqual(ev[(A, C)], ["s2"])
        # each pair once, sent to the paper built on
        self.assertEqual(self.hub.posts(), {B: [(A, "s")], C: [(A, "e")], D: [(B, "s")], E: [(A, "w"), (B, "none")]})
        h = self.hub.links
        self.assertEqual((h[(A, B)]["grade"], h[(B, E)]["state"], h[(A, C)]["grade"]), ("s", "removed", "e"))
        self.assertEqual(res["unsupported_agent_links"], 1)            # C -> E: left as it is
        self.assertEqual(h[(C, E)], {**h[(C, E)], "grade": "s", "state": "active"})
        # the hidden paper is no candidate and no paper relinked
        self.assertNotIn("p_hidden", json.dumps(self.hub.calls))
        self.assertEqual((res["library"], res["papers"]), (5, 5))
        # the grader saw each child with its claims
        self.assertTrue(any("A reverse-time SDE generates data." in p for p in self.grader.prompts))
        text = relink.report(res)
        self.assertIn("## Consistency Models for One Step Generation (2024, p_cm)", text)
        self.assertIn("- - remove: builds on “Score-Based Generative Modeling through Stochastic Differential Equations” "
                      "(2021) · was weak, now graded none · evidence text:title", text)
        self.assertIn("~ regrade: builds on “Denoising Diffusion Probabilistic Models” (2020) · weak → strong", text)

    def test_a_persons_links_are_never_graded_or_sent(self):
        res = self.run_relink()
        self.assertNotIn((C, B), self.grader.seen())               # B -> C is a person's link
        self.assertNotIn((D, A), self.grader.seen())               # A -> D a person removed
        sent = {(l["other"]["paper_id"], c[1].split("/")[4]) for c in self.hub.calls if c[0] == "POST" for l in c[2]["links"]}
        self.assertFalse(sent & {(B, C), (A, D)})
        self.assertEqual(sorted((x["src"], x["dst"], x["reason"]) for x in res["held"]),
                         [(A, D, "removed by a person"), (B, C, "a person's link")])
        self.assertEqual(self.hub.links[(B, C)]["grade"], "w")

    def test_dry_run_changes_nothing_and_says_what_would(self):
        before = {k: dict(v) for k, v in self.hub.links.items()}
        res = self.run_relink(dry_run=True)
        self.assertEqual(self.hub.links, before)
        self.assertEqual(self.ops(res), self.WANT)
        self.assertTrue(all(c[2]["dry_run"] for c in self.hub.calls if c[0] == "POST"))
        text = relink.report(res)
        self.assertIn("dry run (nothing was changed)", text)
        self.assertIn("would add 3, would regrade 1, would remove 1", text)
        # the real run after it grades nothing again and does what the dry run said
        n = len(self.grader.prompts)
        real = self.run_relink()
        self.assertEqual((len(self.grader.prompts), self.ops(real)), (n, self.WANT))

    def test_a_second_run_asks_nothing_and_changes_nothing(self):
        self.run_relink()
        s2 = FakeS2()
        grader = FakeGrader(GRADES)
        before = {k: dict(v) for k, v in self.hub.links.items()}
        res = self.run_relink(grader=grader, fetch=s2)
        self.assertEqual((res["changes"], grader.prompts, s2.urls), ([], [], []))   # cached: no call anywhere
        self.assertEqual(self.hub.links, before)
        self.assertEqual(res["graded"]["before"], res["graded"]["pairs"])
        self.assertIn("Changes: none", relink.report(res))

    def test_only_the_papers_named(self):
        res = self.run_relink(ids=[E])
        self.assertEqual(self.ops(res), [("remove", B, E, "none")])     # E's own side: its text names B
        with self.assertRaises(PapercastError) as cm:
            self.run_relink(ids=["p_hidden", "p_nope"])
        self.assertIn("p_hidden (no live episode)", str(cm.exception))
        self.assertIn("p_nope (no such paper)", str(cm.exception))

    def test_suggest_only(self):
        self.hub.mode = "suggest"
        res = self.run_relink()
        self.assertEqual({c["op"] for c in res["changes"]}, {"suggest", "regrade", "remove"})   # the fake applies the rest
        self.assertIn("links from uploads: suggestions only", relink.report(res))
        again = self.run_relink()
        self.assertEqual([x["reason"] for x in again["held"] if x["reason"] == "suggested already"], ["suggested already"] * 3)

    def test_the_usage_limit_sends_what_was_graded(self):
        old = L.BATCH
        L.BATCH = 2                                              # several calls
        try:
            res = relink.run(self.hub, grade=FakeGrader(GRADES, fail_after=1), root=self.root, parallel=1,
                             fetch=FakeS2())
            self.assertIsNotNone(res["limit"])
            self.assertGreater(res["ungraded"], 0)
            self.assertEqual(len(res["changes"]), 2)             # the first call's two pairs
            g = FakeGrader(GRADES)
            res2 = relink.run(self.hub, grade=g, root=self.root, parallel=1, fetch=FakeS2())
            self.assertIsNone(res2["limit"])
            self.assertEqual(res2["ungraded"], 0)
            self.assertEqual(sum(len(g.pairs(p)) for p in g.prompts), res["ungraded"])   # only the rest
            self.assertEqual(sorted(self.ops(res) + self.ops(res2)), self.WANT)
        finally:
            L.BATCH = old

    def test_a_hub_without_relink(self):
        class Old(FakeHub):
            def get(self, path, params=None, **kw):
                raise NotFound("no")
        with self.assertRaises(PapercastError) as cm:
            relink.run(Old(), grade=self.grader, root=self.root, fetch=None)
        self.assertIn("needs its update", str(cm.exception))

    def test_every_paper_built_on_is_sent_with_isinfluential(self):
        self.hub.link(A, C, "s", origin="human")               # C's pairs now all a person's: nothing to send for it
        s2 = FakeS2()
        s2.answers[f"/paper/S2{C}/references"]["data"][0]["isInfluential"] = True     # C cites A influentially
        self.run_relink(fetch=s2)
        posts = {c[1].split("/")[4]: c[2]["links"] for c in self.hub.calls if c[0] == "POST"}
        self.assertEqual(posts[C], [])                         # sent anyway: the hub re-applies the rule to it
        flags = {(l["other"]["paper_id"], child): l["influential"] for child, ls in posts.items() for l in ls}
        self.assertEqual(set(flags.values()), {False})
        s2 = FakeS2()
        s2.answers[f"/paper/S2{B}/references"]["data"][0]["isInfluential"] = True     # B cites A influentially
        self.hub.calls.clear()
        self.run_relink(fetch=s2, refresh=True)
        posts = {c[1].split("/")[4]: c[2]["links"] for c in self.hub.calls if c[0] == "POST"}
        self.assertEqual([(l["other"]["paper_id"], l["influential"]) for l in posts[B]], [(A, True)])

    def test_restructure_sends_every_graded_pair_in_one_request(self):
        answer = {"changes": [{"index": None, "op": "remove", "src": C, "dst": E, "grade": "s", "was": "s", "why": "rule",
                               "link_id": 5}],
                  "skipped": [], "unchanged": 1, "log_ids": [],
                  "papers": [{"id": E, "parents": [
                      {"src": C, "grade": "s", "by": "agent", "influential": False, "status": "removed"},
                      {"src": A, "grade": "w", "by": "agent", "influential": True, "status": "added"},
                      {"src": D, "grade": "s", "by": "agent", "influential": False, "status": "not selected"}]},
                             {"id": C, "parents": [{"src": B, "grade": "w", "by": "person", "influential": False,
                                                    "status": "kept"}]}],
                  "totals": {"before": 4, "after": 4}, "graphs": [{"id": "g1", "name": "Diffusion", "n": 5, "before": 4, "after": 4}]}
        hub = self.hub
        sent = []

        def post(path, body, **kw):
            hub.calls.append(("POST", path, body))
            if path != "/api/cli/relink/restructure":
                raise AssertionError(f"a restructure posts once, not {path}")
            sent.append((body, kw))
            return dict(answer, dry_run=body["dry_run"], mode="auto")
        hub.post = post
        res = self.run_relink(restructure=True, dry_run=True)
        self.assertEqual(len(sent), 1)
        body, kw = sent[0]
        self.assertEqual((body["dry_run"], kw.get("retries")), (True, 0))
        self.assertEqual(sorted((l["src"], l["dst"], l["grade"]) for l in body["links"]),
                         sorted([(A, B, "s"), (A, C, "e"), (A, E, "w"), (B, D, "s"), (B, E, "none")]))
        self.assertTrue(all(set(l) == {"src", "dst", "grade", "influential", "source"} for l in body["links"]))
        self.assertEqual((res["totals"], res["changes"][0]["why"]), ({"before": 4, "after": 4}, "rule"))
        text = relink.restructure_report(res)
        self.assertIn("# papercast relink --restructure: dry run (nothing was changed)", text)
        self.assertIn("- Links in the library: 4 before, 4 after (if applied).", text)
        self.assertIn("| Diffusion | 5 | 4 | 4 |", text)
        self.assertIn(f"## {TITLE[E]} (2024, {E})", text)
        self.assertIn(f"- − remove: “{TITLE[C]}” (2023) · strong · not chosen by the rule", text)
        self.assertIn(f"- + add: “{TITLE[A]}” (2020) · weak · S2 influential", text)
        self.assertIn(f"- not chosen (not linked): “{TITLE[D]}” (2023) strong", text)
        self.assertIn(f"- kept: “{TITLE[B]}” (2021) · weak · a person's link", text)
        self.assertIn("not graph by graph", text)
        with self.assertRaises(PapercastError) as cm:
            self.run_relink(restructure=True, ids=[E])
        self.assertIn("name no papers", str(cm.exception))

        def old(path, body, **kw):
            raise NotFound("no")
        hub.post = old
        with self.assertRaises(PapercastError) as cm:
            self.run_relink(restructure=True)
        self.assertIn("needs its update", str(cm.exception))


class PacedTest(unittest.TestCase):
    def test_requests_are_spaced_whichever_thread_asks(self):
        t, waits = [0.0], []

        def sleep(s):
            waits.append(round(s, 3))
            t[0] += s
        p = relink.Paced(lambda url: (200, url), interval=1.0, clock=lambda: t[0], sleep=sleep)
        p("a")
        p("b")
        t[0] += 0.4
        p("c")
        t[0] += 5
        p("d")
        self.assertEqual((waits, p.calls), ([1.0, 0.6], 4))


class CommandTest(unittest.TestCase):
    def test_papercast_relink_dry_run(self):
        hub = FakeHub()
        hub.link(A, B, "w")
        grader = FakeGrader(GRADES)
        tmp = tempfile.TemporaryDirectory()
        saved = (cli.config.require_login, cli.Api.__dict__["from_config"], cli.jobs.check_claude, relink.claude_grader,
                 L.http_fetch, relink.S2_INTERVAL_S, os.environ.get("XDG_STATE_HOME"))
        hub.me = lambda: {"id": 1, "name": "Leo", "role": "admin"}
        try:
            cli.config.require_login = lambda cfg=None: {"server": hub.server, "token": "pcg_x"}
            cli.Api.from_config = classmethod(lambda cls, cfg=None, **kw: hub)
            cli.jobs.check_claude = lambda warn=None: None
            relink.claude_grader = lambda root: grader
            fake = FakeS2()
            L.http_fetch = fake
            relink.S2_INTERVAL_S = 0.0
            os.environ["XDG_STATE_HOME"] = tmp.name
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["relink", "--dry-run"])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("# papercast relink: dry run", out.getvalue())
            self.assertIn("grading", err.getvalue())
            self.assertTrue(fake.urls)
            self.assertEqual(hub.links[(A, B)]["grade"], "w")
            self.assertTrue(list((Path(tmp.name) / "papercast" / "relink" / "runs").glob("*-dry-run.json")))
            with redirect_stdout(io.StringIO()) as o2, redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["relink", "--json"]), 0)
            self.assertEqual(json.loads(o2.getvalue())["counts"]["regrade"], 1)
            self.assertEqual(hub.links[(A, B)]["grade"], "s")
            with redirect_stdout(io.StringIO()) as o4, redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["relink", "--restructure", "--dry-run"]), 0)
            self.assertIn("# papercast relink --restructure: dry run (nothing was changed)", o4.getvalue())
            self.assertIn("- Links in the library: 6 before, 6 after (if applied).", o4.getvalue())
            with redirect_stderr(io.StringIO()) as e3:
                self.assertEqual(cli.main(["relink", "--parallel", "99"]), 1)
            self.assertIn("--parallel is 1 to 8", e3.getvalue())
        finally:
            (cli.config.require_login, from_config, cli.jobs.check_claude, relink.claude_grader,
             L.http_fetch, relink.S2_INTERVAL_S, state) = saved
            cli.Api.from_config = from_config
            if state is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = state
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
