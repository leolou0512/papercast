"""A3's CLI API against a real running hub (SPEC.md sections 4-6): who, prompt, prefs, library,
lookup, claims (and their race), the bundle upload (malicious members, size limits), paper
identity and dedupe, new versions, and the checks that reject a script."""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tarfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contrib_harness as H  # noqa: E402

from hub import contrib, db, graph  # noqa: E402


class ContribTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = H.Hub()

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def setUp(self):
        self.h = self.hub

    def paper(self, user, n: int):
        """A distinct paper made by `user`: (paper_id, episode)."""
        ep = self.h.make_paper(user, f"2101.{10000 + n}", f"A paper number {n} about scores")
        return ep["paper_id"], ep

    def version(self, user, paper_id, **kw):
        """Upload a new version of paper_id; the checked episode."""
        m = H.manifest(paper_id=paper_id, paper_over={"arxiv_id": None, **kw.pop("paper_over", {})})
        code, out = self.h.upload(user, H.bundle(m, **kw))
        self.assertEqual(code, 201, out)
        return self.h.wait_checked(user, out["episode_id"])

    def tmp_left(self):
        t = self.h.cfg.data / "tmp"
        return sorted(os.listdir(t)) if t.is_dir() else []

    # ---- who, prompt, prefs, library

    def test_me_prompt_prefs_library(self):
        a = self.h.user("Ada")
        code, me = self.h.request("GET", "/api/cli/me", user=a)
        self.assertEqual((code, me["name"], me["role"], me["email"]), (200, "Ada", "contributor", a["email"]))
        with db.transaction() as c:
            c.execute("DELETE FROM base_prompts")
        code, out = self.h.request("GET", "/api/cli/prompt", user=a)
        self.assertEqual((code, out["error"]), (404, "no_base_prompt"))
        self.h.base_prompt("old", version=1)
        self.h.base_prompt("The guideline, 15 to 25 minutes.", wording={"classes": [], "x": 1}, version=2)
        code, out = self.h.request("GET", "/api/cli/prompt", user=a)
        self.assertEqual((code, out["version"], out["guideline"], out["wording"]["x"]),
                         (200, 2, "The guideline, 15 to 25 minutes.", 1))

        code, pr = self.h.request("GET", "/api/cli/prefs", user=a)
        self.assertEqual((code, pr), (200, {"settings": {"maths": "words", "emphasis": "balanced",
                                                          "background": "field"}, "note": "", "version": 0}))
        code, pr = self.h.request("PUT", "/api/cli/prefs", {"settings": {"maths": "full"}, "note": "more code"}, user=a)
        self.assertEqual((code, pr["settings"]["maths"], pr["settings"]["emphasis"], pr["note"], pr["version"]),
                         (200, "full", "balanced", "more code", 1))
        code, pr = self.h.request("PUT", "/api/cli/prefs", {"settings": {"emphasis": "theory"}}, user=a)
        self.assertEqual((pr["settings"]["maths"], pr["settings"]["emphasis"], pr["note"], pr["version"]),
                         ("full", "theory", "more code", 2))
        code, out = self.h.request("PUT", "/api/cli/prefs", {"settings": {"maths": "lots"}}, user=a)
        self.assertEqual((code, out["error"]), (400, "bad_prefs"))
        code, pr = self.h.request("GET", "/api/cli/prefs", user=a)
        self.assertEqual((pr["settings"]["maths"], pr["version"]), ("full", 2))

        pid, _ = self.paper(a, 1)
        code, lib = self.h.request("GET", "/api/cli/library", user=a)
        self.assertEqual(code, 200)
        row = next(p for p in lib["papers"] if p["id"] == pid)
        self.assertEqual(set(row), {"id", "title", "year", "arxiv_id", "doi", "s2_id"})
        self.assertEqual((row["arxiv_id"], row["year"]), ("2101.10001", 2020))

    def test_levels(self):
        v = self.h.user("Vic", role="viewer")
        code, _ = self.h.request("GET", "/api/cli/me")
        self.assertEqual(code, 401)
        code, _ = self.h.request("GET", "/api/cli/me", worker=True)
        self.assertEqual(code, 401)
        code, out = self.h.request("GET", "/api/cli/lookup?arxiv_id=2101.99999", user=v)
        self.assertEqual((code, out["paper"], out["claim"]), (200, None, None))
        code, _ = self.h.claim(v, arxiv_id="2101.99999")
        self.assertEqual(code, 403)
        code, _ = self.h.upload(v, H.bundle())
        self.assertEqual(code, 403)
        code, out = self.h.request("GET", "/api/cli/lookup", user=v)
        self.assertEqual((code, out["error"]), (400, "no_keys"))

    # ---- claims

    def test_claim_race(self):
        """Two people claim the same new paper at the same moment: one gets it, the other is told who."""
        a, b = self.h.user("Ann"), self.h.user("Bob")
        barrier = threading.Barrier(2)
        got = {}

        def go(u):
            barrier.wait()
            got[u["name"]] = self.h.claim(u, arxiv_id="2203.00001v2", title="Racing for a paper")

        ts = [threading.Thread(target=go, args=(u,)) for u in (a, b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        codes = sorted(v[0] for v in got.values())
        self.assertEqual(codes, [201, 409], got)
        winner = next(n for n, v in got.items() if v[0] == 201)
        loser = next(n for n, v in got.items() if v[0] == 409)
        self.assertEqual(got[loser][1]["error"], "in_progress")
        self.assertEqual(got[loser][1]["by"], {"name": winner})
        self.assertTrue(got[loser][1]["since"])
        # the loser's lookup (by title only) reports the winner's claim
        lu = {"Ann": a, "Bob": b}[loser]
        code, out = self.h.request("GET", "/api/cli/lookup?title=racing+for+a+PAPER", user=lu)
        self.assertEqual((code, out["paper"], out["claim"]["by"]["name"], out["claim"]["mine"]),
                         (200, None, winner, False))
        # the winner claiming again gets the same claim back, renewed
        wu = {"Ann": a, "Bob": b}[winner]
        code, again = self.h.claim(wu, arxiv_id="arXiv:2203.00001")
        self.assertEqual((code, again["claim_id"], again["renewed"]), (201, got[winner][1]["claim_id"], True))
        code, out = self.h.request("GET", "/api/cli/lookup?arxiv_id=2203.00001", user=wu)
        self.assertEqual((out["claim"]["mine"], out["claim"]["claim_id"]), (True, again["claim_id"]))

    def test_claim_renew_release_expire(self):
        a, b = self.h.user("Cat"), self.h.user("Dan")
        code, cl = self.h.claim(a, doi="https://doi.org/10.1000/ABC.123")
        self.assertEqual(code, 201)
        with db.transaction() as c:
            r = c.execute("SELECT * FROM claims WHERE id = ?", (cl["claim_id"],)).fetchone()
        self.assertEqual(r["doi"], "10.1000/abc.123")
        code, out = self.h.request("PUT", f"/api/cli/claims/{cl['claim_id']}", user=a)
        self.assertEqual(code, 200)
        self.assertGreaterEqual(out["expires_at"], cl["expires_at"])
        code, _ = self.h.request("PUT", f"/api/cli/claims/{cl['claim_id']}", user=b)
        self.assertEqual(code, 404)
        code, out = self.h.claim(b, doi="10.1000/abc.123")
        self.assertEqual((code, out["error"], out["by"]["name"]), (409, "in_progress", "Cat"))
        # expired after six hours: someone else may claim it
        with db.transaction() as c:
            c.execute("UPDATE claims SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?", (cl["claim_id"],))
        code, out = self.h.request("GET", "/api/cli/lookup?doi=10.1000/abc.123", user=b)
        self.assertIsNone(out["claim"])
        code, bcl = self.h.claim(b, doi="10.1000/abc.123")
        self.assertEqual(code, 201)
        # now Cat's renewal is refused: Dan holds it
        code, out = self.h.request("PUT", f"/api/cli/claims/{cl['claim_id']}", user=a)
        self.assertEqual((code, out["error"]), (409, "in_progress"))
        # Dan gives it up; Cat may claim it again
        code, _ = self.h.request("DELETE", f"/api/cli/claims/{bcl['claim_id']}", user=b)
        self.assertEqual(code, 200)
        code, _ = self.h.claim(a, doi="10.1000/abc.123")
        self.assertEqual(code, 201)
        code, out = self.h.claim(a)
        self.assertEqual((code, out["error"]), (400, "no_keys"))

    # ---- upload: a new paper, its files, a new version, dedupe

    def test_new_paper_then_version(self):
        a, b = self.h.user("Eve"), self.h.user("Fay")
        code, cl = self.h.claim(a, arxiv_id="2011.13456", title="Score-Based Generative Modeling")
        self.assertEqual(code, 201)
        m = H.manifest(claim_id=cl["claim_id"], paper_over={"arxiv_id": "2011.13456v2"})
        code, out = self.h.upload(a, H.bundle(m))
        self.assertEqual((code, out["state"], out["new_paper"]), (201, "checking", True), out)
        ep = self.h.wait_checked(a, out["episode_id"])
        self.assertEqual(ep["state"], "waiting-for-gpu", ep)
        self.assertEqual((ep["check_report"], ep["client_version"], ep["base_version"], ep["model"]),
                         ([], "0.1.0", 1, "claude-opus-5-5"))
        self.assertEqual(ep["prefs_summary"], "derivations · practical")
        self.assertGreater(ep["words"], 2250)
        self.assertEqual((ep["voice"]["state"], ep["voice"]["queue_position"] is not None), ("queued", True))
        epdir = self.h.cfg.episodes / out["episode_id"]
        # links-pending.json only while the hub has no graph module to take the links
        self.assertEqual(sorted(set(os.listdir(epdir)) - {"links-pending.json"}),
                         ["bundle-manifest.json", "claims.md", "explainer.html", "explainer.json", "meta.json", "script.md"])
        meta = json.loads((epdir / "meta.json").read_text())
        self.assertEqual((meta["paper_id"], meta["made_by"]["name"]), (out["paper_id"], "Eve"))
        pf = epdir / "links-pending.json"
        if pf.exists():                     # no graph module: the links wait in the episode dir
            self.assertEqual(json.loads(pf.read_text())["links"][0]["other"], {"arxiv_id": "1907.05600"})
        else:                               # the graph keeps a link to a paper it has not got yet
            rows = [dict(r) for r in db.conn().execute("SELECT * FROM pending_links").fetchall()]
            self.assertTrue(any("1907.05600" in json.dumps(r) for r in rows), rows)
        c = db.conn()
        p = c.execute("SELECT * FROM papers WHERE id = ?", (out["paper_id"],)).fetchone()
        self.assertEqual((p["arxiv_id"], p["title_norm"], json.loads(p["authors"])[0], json.loads(p["tags"])),
                         ("2011.13456", "score based generative modeling", "Yang Song", ["diffusion", "generative models"]))
        self.assertIsNotNone(c.execute("SELECT done_at FROM claims WHERE id = ?", (cl["claim_id"],)).fetchone()[0])
        self.assertEqual(self.tmp_left(), [])

        # lookup by the arXiv id with a version, by someone else
        code, lu = self.h.request("GET", "/api/cli/lookup?arxiv_id=arXiv:2011.13456v1", user=b)
        self.assertEqual((lu["paper"]["id"], lu["claim"]), (out["paper_id"], None))
        self.assertEqual(lu["paper"]["episodes"][0]["made_by"]["name"], "Eve")
        # a claim on a paper that exists is refused: make your own version instead
        code, ex = self.h.claim(b, arxiv_id="2011.13456")
        self.assertEqual((code, ex["error"], ex["paper"]["id"]), (409, "exists", out["paper_id"]))

        # Fay's own version: fills the missing DOI, keeps the title
        ep2 = self.version(b, out["paper_id"], paper_over={"title": "Another title", "doi": "10.5555/Score"})
        self.assertEqual((ep2["state"], ep2["paper_id"]), ("waiting-for-gpu", out["paper_id"]))
        p = c.execute("SELECT * FROM papers WHERE id = ?", (out["paper_id"],)).fetchone()
        self.assertEqual((p["title"], p["doi"]), ("Score-Based Generative Modeling", "10.5555/score"))
        code, lu = self.h.request("GET", f"/api/cli/lookup?doi=10.5555/score", user=b)
        self.assertEqual([e["made_by"]["name"] for e in lu["paper"]["episodes"]], ["Eve", "Fay"])
        # a version of a paper that does not exist, or of the wrong arXiv id
        code, err = self.h.upload(b, H.bundle(H.manifest(paper_id="p_nosuchpaper00")))
        self.assertEqual((code, err["error"]), (404, "no_such_paper"))
        code, err = self.h.upload(b, H.bundle(H.manifest(paper_id=out["paper_id"], paper_over={"arxiv_id": "1111.22222"})))
        self.assertEqual((code, err["error"]), (409, "paper_mismatch"))
        # listing: mine only
        code, mine = self.h.request("GET", "/api/cli/episodes?mine=1", user=b)
        self.assertEqual([e["id"] for e in mine["episodes"]], [ep2["id"]])
        self.assertEqual(mine["episodes"][0]["made_by"]["name"], "Fay")

    def test_new_paper_needs_own_live_matching_claim(self):
        a, b = self.h.user("Gus"), self.h.user("Hal")
        m = H.manifest(paper_over={"arxiv_id": "2102.00001", "title": "Claimless"})
        m["claim_id"] = "c_nosuchclaim00"
        code, err = self.h.upload(a, H.bundle(m))
        self.assertEqual((code, err["error"]), (409, "no_claim"))
        code, bcl = self.h.claim(b, arxiv_id="2102.00001", title="Claimless")
        m["claim_id"] = bcl["claim_id"]                          # someone else's claim
        code, err = self.h.upload(a, H.bundle(m))
        self.assertEqual((code, err["error"]), (409, "no_claim"))
        code, acl = self.h.claim(a, arxiv_id="2102.00002", title="Mine")
        m = H.manifest(claim_id=acl["claim_id"], paper_over={"arxiv_id": "2102.00003", "title": "Other"})
        code, err = self.h.upload(a, H.bundle(m))
        self.assertEqual((code, err["error"]), (409, "claim_mismatch"))
        with db.transaction() as c:
            c.execute("UPDATE claims SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?", (acl["claim_id"],))
        m = H.manifest(claim_id=acl["claim_id"], paper_over={"arxiv_id": "2102.00002", "title": "Mine"})
        code, err = self.h.upload(a, H.bundle(m))
        self.assertEqual((code, err["error"]), (409, "claim_expired"))
        # nothing of these stayed behind
        self.assertIsNone(db.conn().execute("SELECT id FROM papers WHERE arxiv_id LIKE '2102.%'").fetchone())
        self.assertEqual(self.tmp_left(), [])

    def test_dedupe_by_doi(self):
        """Two claims that looked different (one knew the arXiv id, one only the DOI) land on one paper."""
        a, b = self.h.user("Ida"), self.h.user("Jon")
        code, acl = self.h.claim(a, arxiv_id="2104.00001", title="Twin paper")
        code, bcl = self.h.claim(b, doi="10.7777/twin", title="Twin paper (journal version)")
        self.assertEqual(code, 201, bcl)
        code, out_a = self.h.upload(a, H.bundle(H.manifest(claim_id=acl["claim_id"], paper_over={
            "arxiv_id": "2104.00001", "doi": "10.7777/TWIN", "title": "Twin paper"})))
        self.assertEqual(code, 201)
        code, out_b = self.h.upload(b, H.bundle(H.manifest(
            claim_id=bcl["claim_id"], prefs={"settings": {"emphasis": "weird", "maths": "key-steps"}, "note": 3},
            paper_over={"arxiv_id": None, "doi": "doi:10.7777/twin", "title": "Twin paper (journal version)",
                        "year": None})))
        self.assertEqual(code, 201, out_b)
        self.assertEqual((out_b["paper_id"], out_b["new_paper"]), (out_a["paper_id"], False))
        self.assertIsNotNone(db.conn().execute("SELECT done_at FROM claims WHERE id = ?", (bcl["claim_id"],)).fetchone()[0])
        for u, o in ((a, out_a), (b, out_b)):
            self.assertEqual(self.h.wait_checked(u, o["episode_id"])["state"], "waiting-for-gpu")
        self.assertEqual(self.h.wait_checked(b, out_b["episode_id"])["prefs_summary"], "key steps")

    # ---- upload: what a bundle may not be

    def test_malicious_members(self):
        a = self.h.user("Kim")
        pid, _ = self.paper(a, 2)
        outside = self.h.tmp / "outside-evil.txt"
        before = sorted(os.listdir(self.h.cfg.episodes))
        cases = {   # the members, and the reason the first guard gives
            "absolute path": ([({"name": str(outside)}, b"x")], "an absolute path"),
            "dot dot": ([("../../outside-evil.txt", b"x")], "climbs out with .."),
            "dot dot in the middle": ([("sub/../../../outside-evil.txt", b"x")], "climbs out with .."),
            "symlink": ([({"name": "evil-link", "type": tarfile.SYMTYPE, "linkname": str(self.h.tmp)}, None),
                         ("evil-link/outside-evil.txt", b"x")], "a link"),
            "hard link": ([({"name": "evil-hard", "type": tarfile.LNKTYPE, "linkname": "/etc/passwd"}, None)], "a link"),
            "char device": ([({"name": "evil-dev", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}, None)],
                            "a device or pipe"),
            "block device": ([({"name": "evil-blk", "type": tarfile.BLKTYPE, "devmajor": 8, "devminor": 0}, None)],
                             "a device or pipe"),
            "fifo": ([({"name": "evil-fifo", "type": tarfile.FIFOTYPE}, None)], "a device or pipe"),
            "backslash": ([("..\\..\\outside-evil.txt", b"x")], "backslashes"),
        }
        for what, (extra, why) in cases.items():
            with self.subTest(what):
                code, err = self.h.upload(a, H.bundle(H.manifest(paper_id=pid), extra=extra))
                self.assertEqual((code, err["error"]), (400, "bad_bundle"), err)
                self.assertTrue(any(why in p for p in err["problems"]), err)
                self.assertFalse(outside.exists())
                self.assertEqual(sorted(os.listdir(self.h.cfg.episodes)), before)
                self.assertEqual(self.tmp_left(), [])
        # nothing was written anywhere under the data dir by these
        for root, dirs, files in os.walk(self.h.tmp):
            self.assertFalse([f for f in files + dirs if "evil" in f], root)
        # a file listed twice
        code, err = self.h.upload(a, H.bundle(H.manifest(paper_id=pid), extra=[("script.md", b"again")]))
        self.assertEqual((code, err["error"]), (400, "bad_bundle"))

    def test_sizes_and_shapes(self):
        a = self.h.user("Lou")
        pid, _ = self.paper(a, 3)
        man = H.manifest(paper_id=pid)
        # a declared body over 50 MB is refused before it is read
        code, err = self.h.request("POST", "/api/cli/episodes", user=a, ctype="application/gzip",
                                   length=50 * 1024 * 1024 + 1)
        self.assertEqual((code, err["error"]), (413, "too_large"))
        code, _ = self.h.request("GET", "/api/cli/me", user=a)         # and the hub still answers
        self.assertEqual(code, 200)
        # more than 200 members
        many = [(f"crops/c{i}.txt", b"x") for i in range(200)]
        code, err = self.h.upload(a, H.bundle(man, extra=many))
        self.assertEqual((code, err["error"]), (400, "bad_bundle"))
        self.assertIn("more than 200 files", err["message"])
        # a small gzip that unpacks past 60 MB (a zip bomb)
        big = H.bundle(man, extra=[("padding.bin", b"\0" * (61 * 1024 * 1024))])
        self.assertLess(len(big), 1024 * 1024)
        code, err = self.h.upload(a, big)
        self.assertEqual((code, err["error"]), (400, "bad_bundle"))
        self.assertIn("more than 60 MB", err["message"])
        big = H.bundle(man, extra=[(f"pad{i}.bin", b"\0" * (20 * 1024 * 1024)) for i in range(3)])
        code, err = self.h.upload(a, big)             # 60 MB of files, over the limit with the rest
        self.assertEqual((code, err["message"]), (400, "the bundle unpacks to more than 60 MB"))
        # the same with a huge pax header instead of a file (tarfile would read it into memory)
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as g:
            hdr = tarfile.TarInfo("././@PaxHeader")
            hdr.type, hdr.size = tarfile.XHDTYPE, 200 * 1024 * 1024
            g.write(hdr.tobuf(format=tarfile.USTAR_FORMAT))
            for _ in range(70):
                g.write(b"\0" * (1024 * 1024))
        code, err = self.h.upload(a, buf.getvalue())
        self.assertEqual((code, err["message"]), (400, "the bundle unpacks to more than 60 MB"))
        # not a gzip, the wrong type, no manifest, a broken manifest
        code, err = self.h.upload(a, b"plain text, not gzip")
        self.assertEqual((code, err["error"]), (400, "bad_bundle"))
        code, err = self.h.upload(a, H.bundle(man), ctype="application/json")
        self.assertEqual((code, err["error"]), (415, "bad_type"))
        code, err = self.h.upload(a, H.tar_gz([("script.md", b"x")]))
        self.assertEqual((code, err["error"]), (400, "bad_bundle"))
        self.assertIn("manifest.json", err["message"])
        code, err = self.h.upload(a, H.tar_gz([("manifest.json", b"{not json")]))
        self.assertEqual((code, err["error"]), (400, "bad_bundle"))
        bad = H.manifest(paper_id=pid, manifest_version=9)
        del bad["files"]["explainer_html"]
        code, err = self.h.upload(a, H.bundle(bad))
        self.assertEqual((code, err["error"]), (400, "bad_manifest"))
        self.assertEqual(len(err["problems"]), 2, err)
        code, err = self.h.upload(a, b"")
        self.assertEqual((code, err["error"]), (400, "empty"))
        self.assertEqual(self.tmp_left(), [])

    # ---- the checks on the hub (section 6)

    def test_rejected_scripts(self):
        a = self.h.user("Max")
        pid, _ = self.paper(a, 4)
        cases = {
            "digits": (H.script(extra="The model has 12 layers and 3 heads."), "digit"),
            "never-wanted phrase": (H.script(extra="As you know, the score is a gradient."),
                                    "narrates what the listener knows"),
            "too short": (H.script(words=400), "needs 2250-3750 words"),
            "episode talk": (H.script(extra="In this episode the method is explained."), "episode"),
        }
        for what, (text, expect) in cases.items():
            with self.subTest(what):
                ep = self.version(a, pid, script_text=text)
                self.assertEqual(ep["state"], "rejected", ep)
                self.assertTrue(any(expect in p for p in ep["check_report"]), ep["check_report"])
                self.assertIsNone(ep["voice"])
                self.assertIsNone(db.conn().execute("SELECT 1 FROM voice_jobs WHERE episode_id = ?",
                                                    (ep["id"],)).fetchone())
        # several problems at once, each in plain words
        ep = self.version(a, pid, script_text=H.script(words=300, extra="Delve into the 3 results."),
                          explainer_html="<html>no doctype</html>", explainer_json={"points": []})
        report = ep["check_report"]
        self.assertEqual(ep["state"], "rejected")
        self.assertTrue(any("digit" in p for p in report), report)
        self.assertTrue(any("words is about" in p for p in report), report)
        self.assertTrue(any("delve" in p.lower() for p in report), report)
        self.assertTrue(any("<!doctype html>" in p for p in report), report)
        self.assertTrue(any(p.startswith("explainer.json") and "points" in p for p in report), report)
        # not UTF-8
        m = H.manifest(paper_id=pid, paper_over={"arxiv_id": None})
        data = H.tar_gz([("manifest.json", json.dumps(m).encode()), ("script.md", b"\xff\xfe bad bytes"),
                         ("explainer.json", json.dumps(H.EXPLAINER_JSON).encode()),
                         ("explainer.html", H.EXPLAINER_HTML.encode()), ("claims.md", b"- a claim\n")])
        code, out = self.h.upload(a, data)
        self.assertEqual(code, 201, out)
        ep = self.h.wait_checked(a, out["episode_id"])
        self.assertEqual((ep["state"], ep["check_report"]), ("rejected", ["script.md is not UTF-8 text"]))
        # a rejected episode is not shown to others in lookup
        other = self.h.user("Ned")
        code, lu = self.h.request("GET", f"/api/cli/lookup?arxiv_id=2101.10004", user=other)
        self.assertEqual([e["state"] for e in lu["paper"]["episodes"]], ["waiting-for-gpu"])

    def test_listener_name_is_the_makers(self):
        """The never-wanted list names Leo; on the group hub the listener is the maker."""
        alice = self.h.user("Alice")
        pid, _ = self.paper(alice, 5)
        ep = self.version(alice, pid, script_text=H.script(extra="Leo Breiman showed why random forests work."))
        self.assertEqual(ep["state"], "waiting-for-gpu", ep["check_report"])
        ep = self.version(alice, pid, script_text=H.script(extra="Alice, the score is a gradient."))
        self.assertEqual(ep["state"], "rejected")
        self.assertTrue(any(p.startswith("names the listener") and '("Alice")' in p for p in ep["check_report"]), ep["check_report"])
        leo = self.h.user("Leo")
        pid2, _ = self.paper(leo, 6)
        ep = self.version(leo, pid2, script_text=H.script(extra="Leo, the score is a gradient."))
        self.assertEqual(ep["state"], "rejected")
        self.assertTrue(any(p.startswith("names the listener") for p in ep["check_report"]), ep["check_report"])

    def test_minutes_from_the_base_prompt(self):
        a = self.h.user("Oli")
        pid, _ = self.paper(a, 7)
        v = self.h.base_prompt("One episode per paper, 8 to 12 minutes.", version=70)
        m = H.manifest(paper_id=pid, base_version=v, paper_over={"arxiv_id": None})
        code, out = self.h.upload(a, H.bundle(m))
        ep = self.h.wait_checked(a, out["episode_id"])
        self.assertEqual(ep["state"], "rejected")
        self.assertTrue(any("needs 1200-1800 words" in p for p in ep["check_report"]), ep["check_report"])
        code, out = self.h.upload(a, H.bundle(m, script_text=H.script(words=1500)))
        self.assertEqual(self.h.wait_checked(a, out["episode_id"])["state"], "waiting-for-gpu")
        self.assertEqual(contrib.minutes_range(None), (15.0, 25.0))

    def test_links_go_to_the_graph(self):
        a = self.h.user("Pam")
        calls = []
        had = getattr(graph, "apply_agent_links", None)
        try:
            graph.apply_agent_links = lambda episode_id, paper_id, user_id, links: calls.append(
                (episode_id, paper_id, user_id, links))
            pid, ep = self.paper(a, 8)
            self.assertEqual(calls, [(ep["id"], pid, a["id"], H.manifest()["links"])])
            self.assertFalse((self.h.cfg.episodes / ep["id"] / "links-pending.json").exists())
            graph.apply_agent_links = lambda episode, links: calls.append((dict(episode)["id"], links))
            ep2 = self.version(a, pid)
            self.assertEqual(calls[-1], (ep2["id"], H.manifest()["links"]))

            def broken(episode_id, paper_id, user_id, links):
                raise RuntimeError("graph is down")
            graph.apply_agent_links = broken
            ep3 = self.version(a, pid)
            self.assertEqual(ep3["state"], "waiting-for-gpu")
            self.assertTrue((self.h.cfg.episodes / ep3["id"] / "links-pending.json").exists())
        finally:
            if had is not None:             # the real one, once the graph module has it
                graph.apply_agent_links = had
            else:
                del graph.apply_agent_links

    def test_left_over_checking_is_checked_again(self):
        """A hub that stopped mid-check: the episode is checked on the next start's first request."""
        a = self.h.user("Quin")
        pid, ep = self.paper(a, 9)
        with db.transaction() as c:
            c.execute("UPDATE episodes SET state = 'checking' WHERE id = ?", (ep["id"],))
            c.execute("DELETE FROM voice_jobs WHERE episode_id = ?", (ep["id"],))
        contrib._recovered.clear()
        self.assertEqual(self.h.wait_checked(a, ep["id"])["state"], "waiting-for-gpu")


SDE = "Score-Based Generative Modeling through Stochastic Differential Equations"


class MentionsTest(unittest.TestCase):
    """GET /api/cli/mentions: the library papers whose own text mentions a new paper, by its
    arXiv id, DOI or title (across pdftotext's line breaks and hyphenation), never the paper
    itself or another version of it, never by a generic title. Fake papers and texts."""

    @classmethod
    def setUpClass(cls):
        from hub import search
        cls.hub = h = H.Hub()
        cls.u = h.user("Mia")
        h.base_prompt()
        filler = "Filler about sampling, noise levels and the rest of the method. " * 40

        def paper(arxiv_id, title, text):
            code, cl = h.claim(cls.u, arxiv_id=arxiv_id, title=title)
            assert code == 201, (code, cl)
            m = H.manifest(claim_id=cl["claim_id"], paper_over={"arxiv_id": arxiv_id, "title": title}, links=[])
            m["files"]["paper_text"] = "paper.txt"
            code, out = h.upload(cls.u, H.bundle(m, extra=[("paper.txt", (filler + text + filler).encode())]))
            assert code == 201, (code, out)
            assert h.wait_checked(cls.u, out["episode_id"])["state"] == "waiting-for-gpu"
            return out["paper_id"]

        # the title in a reference list, broken over lines and hyphenated by pdftotext, with the
        # venue after it; a hyphen at a line's end inside "Score-based" is joined by the index
        cls.by_title = paper("2201.00001", "A Later Fake Paper That Cites By Title",
                             "References\n[12] Y. Song, J. Sohl-Dickstein. Score-\nbased generative model-\ning "
                             "through Stochastic\nDifferential Equations. In ICLR, 2021.\n")
        cls.by_arxiv = paper("2201.00002", "A Fake Paper That Cites By Arxiv Id",
                             "[3] Song et al. Preprint, arXiv:2011.13456v2 [cs.LG], 2020.\n")
        cls.by_doi = paper("2201.00003", "A Fake Paper That Cites By Its Doi",
                           "[4] Song et al. https://doi.org/10.5555/SDE.\n2021.77 (2021).\n")
        cls.scattered = paper("2201.00004", "A Fake Paper With The Words Apart",
                              "Score matching is a generative idea. Modeling noise through time is stochastic; "
                              "the differential view helps. Equations follow.\n")
        cls.itself = paper("2011.13456", SDE, f"{SDE}\nYang Song. Abstract. We present a fake.\n")
        cls.improved = paper("2201.00005", "Improved " + SDE, "We extend earlier work.\n")
        cls.cites_improved = paper("2201.00006", "A Fake Paper That Cites Only The Improved One",
                                   f"[7] Improved {SDE}. 2022.\n")
        cls.deep = paper("2201.00007", "A Fake Paper About Deep Learning Things",
                         "Deep learning is used. Deep Learning, by some authors, 2016. Deep neural network models.\n")
        cls.viewer = h.user("Vic", "viewer")
        search.idle(h.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()

    def ask(self, user=None, **q):
        import urllib.parse
        return self.hub.request("GET", "/api/cli/mentions?" + urllib.parse.urlencode(q),
                                user=self.u if user is None else user)

    def found(self, **q) -> dict:
        code, out = self.ask(**q)
        self.assertEqual(code, 200, out)
        return {m["paper_id"]: m for m in out["mentions"]}

    def test_title_arxiv_and_doi(self):
        got = self.found(title=SDE, arxiv="2011.13456", doi="10.5555/sde.2021.77")
        self.assertEqual(set(got), {self.by_title, self.by_arxiv, self.by_doi})
        self.assertEqual((got[self.by_title]["field"], got[self.by_arxiv]["field"], got[self.by_doi]["field"]),
                         ("title", "arxiv", "doi"))
        self.assertIn("Scorebased generative modeling through Stochastic Differential Equations. In ICLR",
                      got[self.by_title]["snippet"])
        self.assertIn("arXiv:2011.13456v2", got[self.by_arxiv]["snippet"])
        self.assertLess(len(got[self.by_title]["snippet"]), 260)
        # each key alone
        self.assertEqual(set(self.found(title=SDE)), {self.by_title})
        self.assertEqual(set(self.found(arxiv="arXiv:2011.13456v1")), {self.by_arxiv})
        self.assertEqual(set(self.found(doi="https://doi.org/10.5555/SDE.2021.77")), {self.by_doi})
        self.assertEqual(self.found(doi="10.5555/sde.2021.7"), {})         # a shorter DOI is another one

    def test_the_paper_itself_and_its_versions_are_left_out(self):
        code, out = self.ask(title=SDE, arxiv="2011.13456")
        self.assertIn(self.itself, out["info"]["excluded"])
        self.assertNotIn(self.itself, {m["paper_id"] for m in out["mentions"]})
        # another version (the same title, no arXiv id given) is left out too; `exclude` works
        code, out = self.ask(title=SDE.upper())
        self.assertIn(self.itself, out["info"]["excluded"])
        self.assertNotIn(self.by_title, self.found(title=SDE, exclude=self.by_title))

    def test_inside_a_longer_library_title_is_not_a_mention(self):
        self.assertNotIn(self.cites_improved, self.found(title=SDE))
        self.assertIn(self.cites_improved, self.found(title="Improved " + SDE))

    def test_a_generic_title_is_refused(self):
        code, out = self.ask(title="Deep Learning")
        self.assertEqual((code, out["mentions"]), (200, []))
        self.assertIn("significant words", out["info"]["title"])
        code, out = self.ask(title="Deep Neural Network Models")
        self.assertEqual((out["mentions"], out["info"]["title"]), ([], "only common words"))
        self.assertEqual(set(self.found(title="Deep Learning", arxiv="2011.13456")), {self.by_arxiv})   # its id still counts

    def test_auth_and_arguments(self):
        self.assertEqual(self.ask(user={"token": "nope"}, title=SDE)[0], 401)
        code, _ = self.hub.request("GET", "/api/cli/mentions?title=x", headers={"X-Test-User": self.u["email"]})
        self.assertEqual(code, 401)                    # a browser's login is not the CLI's
        self.assertEqual(self.ask(user=self.viewer, title=SDE)[0], 200)     # cli level: any role
        code, out = self.ask()
        self.assertEqual((code, out["error"]), (400, "no_keys"))


class RelinkTest(unittest.TestCase):
    """GET /api/cli/relink and POST /api/cli/papers/<id>/links (`papercast relink`): the library's
    links found again, applied as the agent acting for the caller. New links both ways; a link a
    person made, changed, removed or dismissed never touched; the agent's own links regraded or
    removed at "none"; every change one log row the map's Undo reverts; dry run; suggest only;
    locked graphs. A fresh hub per test."""

    def setUp(self):
        self.h = H.Hub()
        graph.ensure_schema()
        self.leo = self.h.user("Leo", "admin")
        self.ann = self.h.user("Ann", "contributor")
        self.vic = self.h.user("Vic", "viewer")

    def tearDown(self):
        self.h.close()

    # -- data straight into the database, and a person's edits through the map's own API
    def paper(self, title, year, tags=(), claims=(), text=False, state="ready"):
        pid, eid, at = db.new_id("p_"), db.new_id("e_"), db.now()
        with db.transaction() as c:
            c.execute("INSERT INTO papers(id, title, title_norm, authors, year, tags, created_at) "
                      "VALUES (?, ?, ?, '[]', ?, ?, ?)", (pid, title, db.norm_title(title), year, json.dumps(list(tags)), at))
            c.execute("INSERT INTO episodes(id, paper_id, made_by, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (eid, pid, self.ann["id"], state, at, at))
        d = self.h.cfg.episodes / eid
        d.mkdir(parents=True, exist_ok=True)
        (d / "claims.md").write_text("---\ntitle: " + title + "\n---\n" + "".join(f"- {x}\n" for x in claims))
        if text:
            (d / "paper.txt").write_text(text if isinstance(text, str) else "the paper's own text")
        return pid

    def agent(self, src, dst, grade="s", who=None):
        """An upload's link, as contrib hands it to the graph."""
        out = graph.apply_agent_links("e_x", dst, (who or self.ann)["id"],
                                      [{"other": {"paper_id": src}, "direction": "builds_on", "grade": grade}])
        self.assertEqual(out["added"], 1, out)
        return self.row(src, dst)["id"]

    def browser(self, method, path, body=None, who=None):
        return self.h.request(method, path, body if body is not None else {}, headers={
            "X-Test-User": (who or self.ann)["email"], "X-PCG": "1"})

    def row(self, src, dst):
        return db.conn().execute("SELECT * FROM links WHERE src = ? AND dst = ?", (src, dst)).fetchone()

    def log_rows(self, after=0):
        return db.conn().execute("SELECT * FROM graph_log WHERE id > ? ORDER BY id", (after,)).fetchall()

    def log_max(self):
        return db.conn().execute("SELECT coalesce(max(id), 0) FROM graph_log").fetchone()[0]

    def relink(self, pid, items, who=None, dry_run=False, code=200):
        """items: (other, direction, grade)."""
        body = {"links": [{"other": {"paper_id": o}, "direction": d, "grade": g, "source": "s2"} for o, d, g in items],
                "dry_run": dry_run}
        st, out = self.h.request("POST", f"/api/cli/papers/{pid}/links", body, user=who or self.leo)
        self.assertEqual(st, code, out)
        return out

    def ops(self, out):
        return {(ch["op"], ch["src"], ch["dst"], ch["grade"]) for ch in out["changes"]}

    def reasons(self, out):
        return sorted(s["reason"] for s in out["skipped"])

    # -- the tests
    def test_levels_and_arguments(self):
        a = self.paper("Alpha paper about scores", 2019)
        self.assertEqual(self.h.request("GET", "/api/cli/relink")[0], 401)
        self.assertEqual(self.h.request("GET", "/api/cli/relink", user=self.vic)[0], 403)      # contributor and up
        self.assertEqual(self.h.request("GET", "/api/cli/relink", user=self.ann)[0], 200)
        self.assertEqual(self.h.request("GET", "/api/cli/relink?since=x", user=self.ann)[0], 400)
        body = {"links": []}
        self.assertEqual(self.h.request("POST", f"/api/cli/papers/{a}/links", body, user=self.vic)[0], 403)
        st, _ = self.h.request("POST", f"/api/cli/papers/{a}/links", body,
                               headers={"X-Test-User": self.leo["email"], "X-PCG": "1"})
        self.assertEqual(st, 401)                                   # a browser's login is not the CLI's
        st, out = self.h.request("POST", "/api/cli/papers/p_nothere/links", body, user=self.ann)
        self.assertEqual((st, out["error"]), (404, "no_such_paper"))
        st, out = self.h.request("POST", f"/api/cli/papers/{a}/links", {"links": "all"}, user=self.ann)
        self.assertEqual((st, out["error"]), (400, "bad_links"))
        b, later = self.paper("Beta paper about scores", 2020), self.paper("A later paper about scores", 2023)
        out = self.relink(b, [(a, "builds_on", "great"), (a, "sideways", "s"), ("p_nothere", "builds_on", "s"),
                              (b, "builds_on", "s"), (later, "builds_on", "s"), (a, "builds_on", "s"),
                              (a, "builds_on", "e")], who=self.ann)
        self.assertEqual(self.reasons(out), ["bad direction or grade", "bad direction or grade", "order", "self",
                                             "twice", "unknown paper"])
        self.assertEqual(self.ops(out), {("add", a, b, "s")})

    def test_state(self):
        a = self.paper("Alpha paper about scores", 2019, tags=["diffusion"], claims=["One", "Two", "Three", "Four"], text=True)
        b = self.paper("Beta paper about scores", 2020, tags=["diffusion"])
        gone = self.paper("A paper whose only episode was rejected", 2021, state="rejected")
        lid = self.agent(a, b, "w")
        st, out = self.h.request("GET", "/api/cli/relink", user=self.ann)
        self.assertEqual(st, 200, out)
        ps = {p["id"]: p for p in out["papers"]}
        self.assertEqual((ps[a]["claims"], ps[a]["text"], ps[a]["visible"]), (["One", "Two", "Three"], True, True))
        self.assertEqual((ps[b]["claims"], ps[b]["text"]), ([], False))
        self.assertFalse(ps[gone]["visible"])
        self.assertTrue(set(ps[a]) >= {"title", "year", "arxiv_id", "doi", "s2_id"})
        self.assertEqual([(l["id"], l["origin"], l["state"], l["person"]) for l in out["links"]],
                         [(lid, "agent", "active", False)])
        self.assertEqual(out["agent_links"], "auto")
        diff = {g["name"]: g for g in out["graphs"]}["Diffusion and generative models"]
        self.assertEqual((diff["n"], diff["links"], set(diff["layout"])), (2, 1, {"rev", "updated_at", "current"}))
        m = out["log_max"]
        self.assertEqual(self.browser("PUT", f"/api/links/{lid}", {"grade": "e"})[0], 200)     # a person regrades it
        st, out = self.h.request("GET", f"/api/cli/relink?since={m}", user=self.ann)
        self.assertEqual(out["links"][0]["person"], True)
        self.assertEqual(out["log_since"], {"since": m, "rows": {"link.grade human": 1}})

    def test_adds_both_ways_one_log_row_each_and_undoable(self):
        a, b, c = (self.paper("Alpha paper about scores", 2019), self.paper("Beta paper about scores", 2020),
                   self.paper("Gamma paper about scores", 2021))
        m = self.log_max()
        out = self.relink(b, [(a, "builds_on", "s"), (c, "built_on_by", "essential")])
        self.assertEqual(self.ops(out), {("add", a, b, "s"), ("add", b, c, "e")})
        for src, dst, g in ((a, b, "s"), (b, c, "e")):
            r = self.row(src, dst)
            self.assertEqual((r["grade"], r["origin"], r["state"], r["created_by"]), (g, "agent", "active", self.leo["id"]))
        rows = self.log_rows(m)
        self.assertEqual([(r["op"], r["actor"], r["user_id"]) for r in rows], [("link.add", "agent", self.leo["id"])] * 2)
        self.assertEqual(out["log_ids"], [r["id"] for r in rows])
        # the map's Undo (anyone's last) reverts the newest; "mine" is only a person's own edits
        st, log = self.browser("GET", "/api/graph-log", who=self.leo)
        self.assertIsNone(log["undo"]["mine"])
        self.assertEqual(log["undo"]["any"]["id"], rows[-1]["id"])
        st, js = self.browser("POST", "/api/graph-log/revert", {"scope": "any", "expect": rows[-1]["id"]}, who=self.leo)
        self.assertEqual(st, 200, js)
        self.assertEqual(self.row(b, c)["state"], "removed")
        # undone by a person, so a later relink leaves it removed
        out = self.relink(b, [(a, "builds_on", "s"), (c, "built_on_by", "e")])
        self.assertEqual((out["changes"], out["unchanged"], self.reasons(out)), ([], 1, ["removed by a person"]))

    def test_a_persons_links_are_never_touched(self):
        p = [self.paper(f"Paper number {i} about scores", 2010 + i) for i in range(8)]
        st, js = self.browser("POST", "/api/links", {"src": p[0], "dst": p[1], "grade": "w"})     # a person's link
        self.assertEqual(st, 201, js)
        regraded = self.agent(p[2], p[3], "w")
        self.assertEqual(self.browser("PUT", f"/api/links/{regraded}", {"grade": "s"})[0], 200)   # a person's regrade
        removed = self.agent(p[4], p[5], "s")
        self.assertEqual(self.browser("DELETE", f"/api/links/{removed}")[0], 200)                # a person's removal
        same = [self.paper("Same year paper one", 2030), self.paper("Same year paper two", 2030)]
        st, js = self.browser("POST", "/api/links", {"src": same[0], "dst": same[1], "grade": "s"})
        self.assertEqual(self.browser("DELETE", f"/api/links/{js['link']['id']}")[0], 200)
        # a suggestion dismissed while links from uploads were suggestions only, then automatic again
        self.assertEqual(self.browser("PUT", "/api/graph-settings", {"agent_links": "suggest"}, who=self.leo)[0], 200)
        graph.apply_agent_links("e_x", p[7], self.ann["id"], [{"other": {"paper_id": p[6]}, "direction": "builds_on", "grade": "s"}])
        sid = db.conn().execute("SELECT id FROM link_suggestions").fetchone()[0]
        self.assertEqual(self.browser("POST", f"/api/link-suggestions/{sid}/dismiss")[0], 200)
        self.assertEqual(self.browser("PUT", "/api/graph-settings", {"agent_links": "auto"}, who=self.leo)[0], 200)
        before = [dict(r) for r in db.conn().execute("SELECT * FROM links ORDER BY id")]
        m = self.log_max()
        for g in ("e", "none"):
            outs = [self.relink(p[1], [(p[0], "builds_on", g)]), self.relink(p[3], [(p[2], "builds_on", g)]),
                    self.relink(p[5], [(p[4], "builds_on", g)]), self.relink(same[0], [(same[1], "builds_on", g)]),
                    self.relink(p[7], [(p[6], "builds_on", g)])]
            self.assertEqual([o["changes"] for o in outs], [[]] * 5)
        self.assertEqual([self.reasons(o) for o in outs[:3]], [["a person's link"], ["a person's link"], ["removed by a person"]])
        out = self.relink(same[0], [(same[1], "builds_on", "e")])
        self.assertEqual(self.reasons(out), ["removed by a person (the other way)"])
        self.assertEqual(self.reasons(self.relink(p[7], [(p[6], "builds_on", "s")])), ["dismissed by a person"])
        self.assertEqual([dict(r) for r in db.conn().execute("SELECT * FROM links ORDER BY id")], before)
        self.assertEqual(self.log_max(), m)

    def test_the_agents_own_links_are_regraded_or_removed_and_undoable(self):
        a, b, c = (self.paper("Alpha paper about scores", 2019), self.paper("Beta paper about scores", 2020),
                   self.paper("Gamma paper about scores", 2021))
        ab, ac = self.agent(a, b, "w"), self.agent(a, c, "s")
        m = self.log_max()
        out = self.relink(a, [(b, "built_on_by", "e"), (c, "built_on_by", "none")])
        self.assertEqual(sorted((ch["op"], ch["was"], ch["grade"], ch["link_id"]) for ch in out["changes"]),
                         [("regrade", "w", "e", ab), ("remove", "s", "none", ac)])
        r = self.row(a, b)
        self.assertEqual((r["grade"], r["origin"], r["state"], r["created_by"]), ("e", "agent", "active", self.ann["id"]))
        self.assertEqual(self.row(a, c)["state"], "removed")
        rows = self.log_rows(m)
        self.assertEqual(sorted((r["op"], r["actor"], r["user_id"], r["target"]) for r in rows),
                         sorted([("link.grade", "agent", self.leo["id"], str(ab)), ("link.remove", "agent", self.leo["id"], str(ac))]))
        grade_row = next(r for r in rows if r["op"] == "link.grade")
        self.assertEqual((json.loads(grade_row["before"])["grade"], json.loads(grade_row["after"])["grade"]), ("w", "e"))
        st, log = self.browser("GET", "/api/graph-log", who=self.leo)
        self.assertEqual({e["text"] for e in log["log"][:2]}, {"regraded Alpha paper about → Beta paper about from weak to essential",
                                                             "removed Alpha paper about → Gamma paper about"})
        # each is undone from the map, newest first; a person's undo makes the link theirs
        for _ in rows:
            st, log = self.browser("GET", "/api/graph-log", who=self.leo)
            st, js = self.browser("POST", "/api/graph-log/revert", {"scope": "any", "expect": log["undo"]["any"]["id"]}, who=self.leo)
            self.assertEqual(st, 200, js)
        self.assertEqual((self.row(a, b)["grade"], self.row(a, c)["state"]), ("w", "active"))
        out = self.relink(a, [(b, "built_on_by", "e"), (c, "built_on_by", "none")])
        self.assertEqual((out["changes"], self.reasons(out)), ([], ["a person's link", "a person's link"]))

    def test_dry_run_changes_nothing_and_a_second_run_nothing_more(self):
        a, b, c, d = (self.paper("Alpha paper about scores", 2019, ["diffusion"]), self.paper("Beta paper about scores", 2020, ["diffusion"]),
                      self.paper("Gamma paper about scores", 2021, ["diffusion"]), self.paper("Delta paper about scores", 2022, ["diffusion"]))
        self.agent(a, b, "w")
        self.agent(a, c, "s")
        items = [(a, "builds_on", "s"), (c, "built_on_by", "w")]

        def snap():
            c_ = db.conn()
            return ([dict(r) for r in c_.execute("SELECT * FROM links ORDER BY id")], self.log_max(),
                    [dict(r) for r in c_.execute("SELECT * FROM graph_rev ORDER BY graph_id")],
                    c_.execute("SELECT count(*) FROM link_suggestions").fetchone()[0])
        s0 = snap()
        dry = self.relink(b, items, dry_run=True)
        self.assertEqual(snap(), s0)
        self.assertEqual((dry["dry_run"], dry["log_ids"]), (True, []))
        self.assertEqual(self.ops(dry), {("add", b, c, "w"), ("regrade", a, b, "s")})
        dry2 = self.relink(a, [(c, "built_on_by", "none"), (d, "built_on_by", "e")], dry_run=True)
        self.assertEqual(self.ops(dry2), {("add", a, d, "e"), ("remove", a, c, "none")})
        self.assertEqual(snap(), s0)
        real = self.relink(b, items)
        real2 = self.relink(a, [(c, "built_on_by", "none"), (d, "built_on_by", "e")])
        self.assertEqual((self.ops(real), self.ops(real2)), (self.ops(dry), self.ops(dry2)))
        self.assertNotEqual(snap()[2], s0[2])                     # the graphs went up a revision
        m = self.log_max()
        again = self.relink(b, items)
        again2 = self.relink(a, [(c, "built_on_by", "none"), (d, "built_on_by", "e")])
        self.assertEqual((again["changes"], again2["changes"], self.log_max()), ([], [], m))
        self.assertEqual((again["unchanged"], self.reasons(again2)), (2, ["removed"]))    # the agent's removal stays

    def test_suggest_only(self):
        a, b, c = (self.paper("Alpha paper about scores", 2019), self.paper("Beta paper about scores", 2020),
                   self.paper("Gamma paper about scores", 2021))
        self.agent(a, b, "w")
        self.assertEqual(self.browser("PUT", "/api/graph-settings", {"agent_links": "suggest"}, who=self.leo)[0], 200)
        m = self.log_max()
        out = self.relink(c, [(a, "builds_on", "s"), (b, "builds_on", "e")])
        out2 = self.relink(b, [(a, "builds_on", "e")])
        self.assertEqual((out["mode"], self.ops(out)), ("suggest", {("suggest", a, c, "s"), ("suggest", b, c, "e")}))
        self.assertEqual((out2["changes"], self.reasons(out2)), ([], ["suggest only"]))
        self.assertEqual((self.log_max(), self.row(a, b)["grade"]), (m, "w"))
        self.assertIsNone(self.row(a, c))
        self.assertEqual(db.conn().execute("SELECT count(*) FROM link_suggestions WHERE state = 'open'").fetchone()[0], 2)
        out = self.relink(c, [(a, "builds_on", "s")])
        self.assertEqual((out["changes"], self.reasons(out)), ([], ["suggested already"]))

    def test_the_cli_against_this_hub(self):
        """papercast relink (the CLI's own module) against this hub: the text index both ways, the
        dry run, the real run, a second run that asks nothing."""
        from papercast_cli import relink as R
        from papercast_cli.api import Api
        filler = "Filler about sampling, noise levels and the rest of the method. " * 30
        a = self.paper("Alpha Particle Scattering in Thin Gold Foils", 2019, text=filler)
        b = self.paper("Beta Decay Spectra of Heavy Nuclei Measured Again", 2020, claims=["Beta spectra are continuous."],
                       text=filler + "[1] E. Rutherford. Alpha particle scatter-\ning in thin gold foils. 1911.\n" + filler)
        c = self.paper("Gamma Ray Bursts from Distant Galaxies Observed", 2021, text=filler)
        self.agent(a, c, "w")                                   # no evidence for it now: left as it is
        from hub import search
        search.idle(self.h.cfg)
        api = Api(f"http://127.0.0.1:{self.h.port}", self.leo["token"], retries=0)
        asked = []

        def grade(prompt):
            asked.append(prompt)
            n = prompt.count("\n  [")
            return json.dumps({"answers": [{"i": i, "g": "strong"} for i in range(1, n + 1)]})
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            m = self.log_max()
            dry = R.run(api, dry_run=True, fetch=None, grade=grade, root=tmp)
            self.assertEqual([(x["op"], x["src"], x["dst"], x["grade"], x["evidence"]) for x in dry["changes"]],
                             [("add", a, b, "s", ["text:title"])])
            self.assertIsNone(self.row(a, b))
            self.assertEqual(self.log_max(), m)
            self.assertIn("Beta spectra are continuous.", asked[0])            # the child's claims from the hub
            real = R.run(api, fetch=None, grade=grade, root=tmp)
            self.assertEqual(len(asked), 1)                                  # graded once
            self.assertEqual([(x["op"], x["src"], x["dst"]) for x in real["changes"]], [("add", a, b)])
            r = self.row(a, b)
            self.assertEqual((r["origin"], r["grade"], r["created_by"]), ("agent", "s", self.leo["id"]))
            self.assertEqual(real["log_ids"], [x["id"] for x in self.log_rows(m)])
            self.assertEqual(real["unsupported_agent_links"], 1)
            again = R.run(api, fetch=None, grade=grade, root=tmp)
            self.assertEqual((again["changes"], len(asked), self.log_max()), ([], 1, real["log_ids"][-1]))
            self.assertIn("Changes: none", R.report(again))

    def test_locked_graph(self):
        a, b, c = (self.paper("Alpha paper about scores", 2019, ["lk"]), self.paper("Beta paper about scores", 2020, ["lk"]),
                   self.paper("Gamma paper about scores", 2021, ["lk"]))
        st, js = self.browser("POST", "/api/graphs", {"name": "Locked one", "tags": ["lk"]}, who=self.leo)
        self.assertEqual(st, 201, js)
        self.assertEqual(self.browser("PUT", f"/api/graphs/{js['graph']['id']}", {"locked": True}, who=self.leo)[0], 200)
        self.agent(a, b, "w")
        out = self.relink(b, [(a, "builds_on", "e"), (c, "built_on_by", "s")], who=self.ann)
        self.assertEqual((self.ops(out), self.reasons(out)), ({("add", b, c, "s")}, ["locked"]))   # agents still add
        out = self.relink(b, [(a, "builds_on", "e")], who=self.leo)
        self.assertEqual(self.ops(out), {("regrade", a, b, "e")})

    # -- Leo's per-paper rule on a relink, and the restructure of the whole map
    def legacy(self, src, dst, grade="s"):
        """An agent link as the live map has them from before the rule: straight into the database."""
        at = db.now()
        with db.transaction() as c:
            return c.execute("INSERT INTO links(src, dst, grade, origin, state, created_by, created_at, updated_at) "
                             "VALUES (?, ?, ?, 'agent', 'active', ?, ?, ?)", (src, dst, grade, self.ann["id"], at, at)).lastrowid

    def active(self, dst):
        return {r["src"]: r["grade"] for r in db.conn().execute(
            "SELECT src, grade FROM links WHERE dst = ? AND state = 'active'", (dst,))}

    def parents(self, n, tags=()):
        return [self.paper(f"Parent paper number {i} about scores", 2010 + i, tags) for i in range(n)]

    def restructure(self, items, who=None, dry_run=False, code=200):
        """items: (src, dst, grade, influential)."""
        body = {"links": [{"src": s, "dst": d, "grade": g, "influential": i, "source": "s2"} for s, d, g, i in items],
                "dry_run": dry_run}
        st, out = self.h.request("POST", "/api/cli/relink/restructure", body, user=who or self.leo)
        self.assertEqual(st, code, out)
        return out

    def snap(self):
        c = db.conn()
        return ([dict(r) for r in c.execute("SELECT * FROM links ORDER BY id")], self.log_max(),
                [dict(r) for r in c.execute("SELECT * FROM graph_rev ORDER BY graph_id")],
                [dict(r) for r in c.execute("SELECT * FROM link_candidates ORDER BY src, dst")])

    def test_relink_applies_the_rule_and_remembers_the_rest(self):
        ps = self.parents(7)
        child = self.paper("The child paper about scores", 2024)
        out = self.relink(child, [(p, "builds_on", "s") for p in ps])
        self.assertEqual(sorted(ch["src"] for ch in out["changes"] if ch["op"] == "add"), sorted(ps[2:]))
        self.assertEqual(self.reasons(out), [graph.NOT_CHOSEN] * 2)
        st, state = self.h.request("GET", "/api/cli/relink", user=self.leo)
        self.assertEqual(len([k for k in state["candidates"] if k["dst"] == child]), 7)     # the relink sees them
        self.assertEqual(state["rule"], {"target_strong": 5, "target_weak": 4})
        # again: nothing (the two left out are remembered, not proposed as new)
        m = self.log_max()
        again = self.relink(child, [(p, "builds_on", "s") for p in ps])
        self.assertEqual((again["changes"], again["unchanged"], self.log_max()), ([], 5, m))
        # the choice changes (Semantic Scholar flags the oldest one isInfluential): it comes in, the oldest of the rest goes
        items = [{"other": {"paper_id": p}, "direction": "builds_on", "grade": "s", "influential": p == ps[0]} for p in ps]
        st, out = self.h.request("POST", f"/api/cli/papers/{child}/links", {"links": items}, user=self.leo)
        self.assertEqual(sorted((ch["op"], ch["src"], ch.get("why")) for ch in out["changes"]),
                         sorted([("add", ps[0], None), ("remove", ps[2], "rule")]))
        rows = self.log_rows(m)
        self.assertEqual(sorted((r["op"], r["actor"], r["user_id"]) for r in rows),
                         [("link.add", "agent", self.leo["id"]), ("link.remove", "agent", self.leo["id"])])
        # a person removes one the rule chose: the next relink (nothing new to send) fills its slot with a
        # remembered one, the agent's own removal restored
        self.assertEqual(self.browser("DELETE", f"/api/links/{self.row(ps[3], child)['id']}")[0], 200)
        out = self.relink(child, [])
        self.assertEqual([(ch["op"], ch["src"], ch.get("restored")) for ch in out["changes"]], [("add", ps[2], True)])
        self.assertEqual((self.row(ps[3], child)["state"], len(self.active(child))), ("removed", 5))
        st, log = self.browser("GET", "/api/graph-log", who=self.leo)
        self.assertTrue(log["log"][0]["summary"].startswith("the agent for Leo restored Parent paper number"), log["log"][0])

    def test_relink_trims_a_paper_from_before_the_rule_never_a_persons_link(self):
        ps = self.parents(8)
        child = self.paper("The child paper about scores", 2024)
        st, js = self.browser("POST", "/api/links", {"src": ps[0], "dst": child, "grade": "w"})    # a person's, weak
        self.assertEqual(st, 201, js)
        for p in ps[1:]:
            self.legacy(p, child, "s")
        m = self.log_max()
        dry = self.relink(child, [], dry_run=True)
        self.assertEqual(self.log_max(), m)
        out = self.relink(child, [])
        self.assertEqual(self.ops(out), self.ops(dry))
        self.assertEqual(sorted((ch["op"], ch["src"], ch["why"]) for ch in out["changes"]),
                         sorted(("remove", p, "rule") for p in ps[1:4]))            # 1 + the 4 newest strong stay
        self.assertEqual(set(self.active(child)), {ps[0], *ps[4:]})
        self.assertEqual([(r["op"], r["actor"], r["user_id"]) for r in self.log_rows(m)], [("link.remove", "agent", self.leo["id"])] * 3)
        par = {e["src"]: e for e in out["papers"][0]["parents"]}
        self.assertEqual((par[ps[0]]["by"], par[ps[0]]["status"], par[ps[1]]["status"]), ("person", "kept", "removed"))

    def build_map(self):
        """A small map as the live one is from before the rule: C1 has a person's weak link and six of the
        agent's strong ones; C2 three weak ones of the agent (two of them joined by another path through C1),
        one a person removed, and an essential pair not linked yet."""
        P = [self.paper(f"Parent paper number {i} about scores", 2010 + i, ["diffusion"]) for i in range(7)]
        c1, c2 = self.paper("First child about scores", 2020, ["diffusion"]), self.paper("Second child about scores", 2022, ["diffusion"])
        st, js = self.browser("POST", "/api/links", {"src": P[0], "dst": c1, "grade": "w"})
        self.assertEqual(st, 201, js)
        for p in P[1:]:
            self.legacy(p, c1, "s")
        for p in (c1, P[6], P[5]):
            self.legacy(p, c2, "w")
        gone = self.legacy(P[3], c2, "s")
        self.assertEqual(self.browser("DELETE", f"/api/links/{gone}")[0], 200)
        items = ([(p, c1, "s", False) for p in P[1:]] + [(c1, c2, "w", False), (P[6], c2, "w", False), (P[5], c2, "w", False),
                 (P[4], c2, "e", True), (P[3], c2, "s", False), (P[2], c2, "none", False), (P[0], c1, "e", False)])
        return P, c1, c2, items

    def test_restructure_dry_run_real_and_again(self):
        P, c1, c2, items = self.build_map()
        s0 = self.snap()
        dry = self.restructure(items, dry_run=True)
        self.assertEqual(self.snap(), s0)
        self.assertEqual((dry["dry_run"], dry["log_ids"]), (True, []))
        want = {("remove", P[1], c1, "s"), ("remove", P[2], c1, "s"), ("add", P[4], c2, "e"),
                ("remove", P[6], c2, "w"), ("remove", P[5], c2, "w")}
        self.assertEqual(self.ops(dry), want)
        self.assertEqual(sorted(s["reason"] for s in dry["skipped"]), ["a person's link", "removed by a person"])
        self.assertEqual((dry["totals"], {g["name"]: (g["before"], g["after"]) for g in dry["graphs"]}["Diffusion and generative models"]),
                         ({"before": 10, "after": 7}, (10, 7)))
        m = self.log_max()
        real = self.restructure(items)
        self.assertEqual((self.ops(real), real["totals"]), (want, dry["totals"]))
        self.assertEqual(set(self.active(c1)), {P[0], *P[3:]})
        self.assertEqual(set(self.active(c2)), {P[4], c1})                   # P5, P6 -> c2: through c1 already
        rows = self.log_rows(m)
        self.assertEqual(sorted((r["op"], r["actor"], r["user_id"]) for r in rows),
                         sorted([("link.remove", "agent", self.leo["id"])] * 4 + [("link.add", "agent", self.leo["id"])]))
        self.assertEqual(real["log_ids"], [r["id"] for r in rows])
        self.assertEqual(self.row(P[0], c1)["origin"], "human")
        self.assertEqual(self.row(P[3], c2)["state"], "removed")
        # again, and a relink after it: nothing more
        m = self.log_max()
        again = self.restructure(items)
        self.assertEqual((again["changes"], again["totals"], self.log_max()), ([], {"before": 7, "after": 7}, m))
        self.assertEqual(self.relink(c2, [(c1, "builds_on", "w"), (P[6], "builds_on", "w")])["changes"], [])
        self.assertEqual(self.log_max(), m)
        # the newest change undone from the map (the essential link added): a person's removal now, and it stays so
        st, log = self.browser("GET", "/api/graph-log", who=self.leo)
        st, js = self.browser("POST", "/api/graph-log/revert", {"scope": "any", "expect": log["undo"]["any"]["id"]}, who=self.leo)
        self.assertEqual((st, js["reverted"]["op"]), (200, "link.add"), js)
        self.assertEqual(self.restructure(items)["changes"], [])
        self.assertEqual(self.row(P[4], c2)["state"], "removed")

    def test_restructure_levels_arguments_and_locked_graphs(self):
        P, c1, c2, items = self.build_map()
        self.assertEqual(self.h.request("POST", "/api/cli/relink/restructure", {"links": []}, user=self.vic)[0], 403)
        st, out = self.h.request("POST", "/api/cli/relink/restructure", {"links": "all"}, user=self.ann)
        self.assertEqual((st, out["error"]), (400, "bad_links"))
        out = self.restructure([(P[1], "p_nothere", "s", False), (P[1], P[1], "s", False), (P[1], c1, "great", False)],
                               dry_run=True)
        self.assertEqual(sorted(s["reason"] for s in out["skipped"]), ["bad pair or grade", "self", "unknown paper"])
        # a locked graph: a contributor's restructure leaves the agent's links in it (they count); an admin's trims them
        st, js = self.browser("POST", "/api/graphs", {"name": "Locked one", "tags": ["diffusion"]}, who=self.leo)
        self.assertEqual(self.browser("PUT", f"/api/graphs/{js['graph']['id']}", {"locked": True}, who=self.leo)[0], 200)
        out = self.restructure(items, who=self.ann, dry_run=True)
        self.assertEqual(self.ops(out), {("add", P[4], c2, "e")})             # agents still add; nothing removed
        self.assertEqual(len(self.restructure(items, who=self.leo, dry_run=True)["changes"]), 5)

    def test_the_cli_restructure_against_this_hub(self):
        """papercast relink --restructure (the CLI's own module) against this hub: a paper from before the
        rule with seven of the agent's strong links keeps the five newest; the dry run says so exactly."""
        from papercast_cli import relink as R
        from papercast_cli.api import Api
        titles = ["Alpha Particle Scattering in Thin Gold Foils", "Beta Decay Spectra of Heavy Nuclei Measured",
                  "Gamma Ray Bursts from Distant Galaxies Observed", "Delta Resonance Production in Pion Nucleon Collisions",
                  "Epsilon Expansion of Critical Exponents in Three Dimensions",
                  "Zeta Function Regularization of Quantum Field Determinants", "Eta Meson Decays into Three Pions Revisited"]
        filler = "Filler about sampling, noise levels and the rest of the method. " * 30
        ps = [self.paper(t, 2010 + i, text=filler) for i, t in enumerate(titles)]
        child = self.paper("Theta Oscillations in the Hippocampus of Freely Moving Rats", 2024,
                           text=filler + "".join(f"[{i}] A. Author. {t}. 20{10 + i}.\n" for i, t in enumerate(titles)) + filler)
        for p in ps:
            self.legacy(p, child, "s")
        from hub import search
        search.idle(self.h.cfg)
        api = Api(f"http://127.0.0.1:{self.h.port}", self.leo["token"], retries=0)

        def grade(prompt):
            n = prompt.count("\n  [")
            return json.dumps({"answers": [{"i": i, "g": "strong"} for i in range(1, n + 1)]})
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(R.PapercastError):
                R.run(api, ids=[child], restructure=True, fetch=None, grade=grade, root=tmp)
            m = self.log_max()
            dry = R.run(api, restructure=True, dry_run=True, fetch=None, grade=grade, root=tmp)
            self.assertEqual(self.log_max(), m)
            self.assertEqual(sorted((x["op"], x["src"], x.get("why")) for x in dry["changes"]),
                             sorted([("remove", ps[0], "rule"), ("remove", ps[1], "rule")]))
            text = R.restructure_report(dry)
            self.assertIn("dry run (nothing was changed)", text)
            self.assertIn("- Links in the library: 7 before, 5 after (if applied).", text)
            self.assertIn("## Theta Oscillations in the Hippocampus of Freely Moving Rats (2024, " + child + ")", text)
            self.assertIn("- − remove: “Alpha Particle Scattering in Thin Gold Foils” (2010) · strong · not chosen by the rule", text)
            self.assertIn("- kept: “Eta Meson Decays into Three Pions Revisited” (2016) · strong", text)
            real = R.run(api, restructure=True, fetch=None, grade=grade, root=tmp)
            self.assertEqual(sorted((x["op"], x["src"]) for x in real["changes"]), sorted([("remove", ps[0]), ("remove", ps[1])]))
            self.assertEqual(set(self.active(child)), set(ps[2:]))
            self.assertEqual(real["log_ids"], [r["id"] for r in self.log_rows(m)])
            again = R.run(api, restructure=True, fetch=None, grade=grade, root=tmp)
            self.assertEqual((again["changes"], again["totals"]), ([], {"before": 5, "after": 5}))
            plain = R.run(api, fetch=None, grade=grade, root=tmp)                  # a plain relink after it: nothing
            self.assertEqual(plain["changes"], [])
            self.assertIn("left out by the per-paper rule (the hub keeps its grade)", [x["reason"] for x in plain["held"]])


if __name__ == "__main__":
    unittest.main()
