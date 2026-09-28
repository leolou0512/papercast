"""The local episode pipeline (A8): a full job with the fake `claude` and a fake hub, the resume
after a kill, the one repair turn, the usage limit, the explainer with and without poppler,
links from a Semantic Scholar fixture, the bundle, and the claude command line against Leo's
runner's. No network and no real Claude anywhere (PipelineCase names the fake explicitly)."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import unittest

from pipeline_helpers import (FAKE, FAKE_DIR, PKG, FakeApi, PipelineCase, S2Fixture,
                              default_wording)

from papercast_cli.common import bundle as cbundle
from papercast_cli.pipeline import claude as pclaude
from papercast_cli.pipeline import limits, pdf
from papercast_cli.pipeline import run as prun
from papercast_cli.pipeline.errors import PipelineError, UsageLimit

HAVE_POPPLER = pdf.available()["crops"]
LEO_RUNNER = os.path.join(os.path.dirname(os.path.dirname(PKG)), "stacks", "papercast", "runner")
NORMAL = ["identify", "episode", "cut", "grade"]


class Boom(Exception):
    """A worker killed between two steps."""


class FullJob(PipelineCase):
    def test_full_job_local_pdf(self):
        self.scenario()
        self.write_job()
        api = FakeApi()
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded", r)
        self.assertEqual(r["state"], "waiting-for-gpu")
        self.assertEqual(r["episode_id"], "e_test00000001")
        # the session: identify, then the episode, then the cut pass; one haiku grading call
        self.assertEqual(self.kinds(), NORMAL)
        calls = self.calls()
        sid = calls[0]["argv"][calls[0]["argv"].index("--session-id") + 1]
        for c in calls[1:3]:
            self.assertEqual(c["argv"][c["argv"].index("--resume") + 1], sid)
        # the agent works in work/, the grader in an empty directory
        self.assertTrue(all(c["cwd"] == os.path.realpath(self.jpath("work")) for c in calls[:3]))
        self.assertEqual(calls[3]["cwd"], os.path.realpath(self.jpath("grader")))
        # never passed to Claude Code: an API key, the parent's CLAUDECODE
        for c in calls:
            self.assertEqual(set(c["env"]) - {"HOME", "USER", "LOGNAME", "LANG", "PATH"}, set(), c["env"])
        # each step's files
        for p in ("work/paper.pdf", "work/paper.json", "work/guideline.md", "work/script.md",
                  "work/explainer.json", "work/claims.md", "meta.json", "lookup.json",
                  "claim.json", "prompt.json", "prefs.json", "out/script.md",
                  "out/explainer.json", "out/claims.md", "explainer.html", "links.json",
                  "manifest.json", "bundle.tar.gz", "upload.json", "result.json", "state.json"):
            self.assertTrue(os.path.isfile(self.jpath(p)), p)
        meta = self.jread("meta.json")
        self.assertEqual(meta["title"], "Flow Matching for Generative Modeling")
        self.assertEqual(meta["arxiv_id"], "2210.02747")
        self.assertEqual(meta["year"], 2022)
        self.assertEqual(len(meta["source_sha256"]), 64)
        lookup = [c for c in api.calls if c[0] == "GET" and c[1].startswith("/api/cli/lookup")][0][1]
        self.assertIn("arxiv_id=2210.02747", lookup)
        self.assertIn("sha256=" + meta["source_sha256"], lookup)
        claim_post = [c for c in api.calls if c[0] == "POST"][0]
        self.assertEqual(claim_post[2]["device"], "test-laptop")
        self.assertEqual(claim_post[2]["keys"]["arxiv_id"], "2210.02747")
        g = self.jread("work", "guideline.md")
        self.assertIn("# Base guideline", g)
        self.assertIn("This listener's preferences", g)
        self.assertLess(g.index("# Base guideline"), g.index("This listener's preferences"))
        st = self.jread("state.json")
        self.assertEqual(list(st["steps"]), ["source", "identify", "claim", "prompt", "episode",
                                             "explainer", "links", "bundle", "upload", "hub"])
        # progress only goes forward and ends at 1
        self.assertEqual(self.events[-1][:2], ("done", 1.0))
        fr = [e[1] for e in self.events]
        self.assertEqual(fr, sorted(fr))
        # the manifest as the hub receives it
        m = api.uploads[0]["manifest"]
        self.assertEqual(m["claim_id"], "c_test00000001")
        self.assertIsNone(m["paper_id"])
        self.assertEqual(m["model"], "claude-opus-5-5")
        self.assertEqual(m["base_version"], 2)
        self.assertEqual(m["prefs"], {"settings": {"maths": "full", "emphasis": "method"}, "note": "",
                                      "version": 3})
        self.assertEqual(m["paper"]["tags"], ["generative models", "flow matching"])
        self.assertEqual(m["stats"]["words"], 3005)
        self.assertEqual(m["stats"]["est_minutes"], 20.0)

    def test_rerun_after_done_changes_nothing(self):
        self.scenario()
        self.write_job()
        api = FakeApi()
        r1 = self.run_job(api)
        r2 = self.run_job(api)
        self.assertEqual(r1["episode_id"], r2["episode_id"])
        self.assertEqual(self.kinds(), NORMAL)
        self.assertEqual(len(api.uploads), 1)

    def test_bundle_valid_and_within_limits(self):
        self.scenario()
        self.write_job()
        api = FakeApi()
        self.run_job(api)
        with open(self.jpath("bundle.tar.gz"), "rb") as fh:
            data = fh.read()
        self.assertLessEqual(len(data), cbundle.MAX_BYTES)
        self.assertEqual(api.uploads[0]["size"], len(data))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = set(tf.getnames())
            m = json.load(tf.extractfile("manifest.json"))
            html = tf.extractfile("explainer.html").read()
            script = tf.extractfile("script.md").read()
        self.assertEqual(names, {"manifest.json", "script.md", "explainer.json", "explainer.html",
                                 "claims.md"})
        self.assertEqual(cbundle.validate(m, names), [])
        self.assertTrue(html.lower().startswith(b"<!doctype html"))
        self.assertLessEqual(len(html), 8 << 20)
        self.assertLessEqual(len(script), 60 * 1024)
        with open(self.jpath("out", "script.md"), "rb") as fh:
            self.assertEqual(script, fh.read())

    def test_arxiv_link_downloads_the_pdf(self):
        self.scenario()
        self.write_job(input="https://arxiv.org/abs/2210.02747v2")
        got = []

        def download(url, limit):
            got.append(url)
            with open(self.pdf_path, "rb") as fh:
                return fh.read()

        r = self.run_job(FakeApi(), download=download)
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(got, ["https://arxiv.org/pdf/2210.02747"])
        src = self.jread("state.json")["steps"]["source"]
        self.assertEqual((src["kind"], src["arxiv_id"], src["pdf"]), ("url", "2210.02747", True))
        first = self.calls()[0]["argv"][1]
        self.assertIn("`paper.pdf` (from https://arxiv.org/abs/2210.02747v2)", first)
        self.assertEqual(self.jread("meta.json")["url"], "https://arxiv.org/abs/2210.02747v2")

    def test_link_without_pdf_is_read_with_webfetch(self):
        self.scenario(paper={"title": "Flow Matching for Generative Modeling", "authors": ["Yaron Lipman"],
                             "year": "2023", "arxiv_id": None, "doi": "https://doi.org/10.1000/ABC.",
                             "url": None})
        self.write_job(input="https://openreview.net/forum?id=PqvMRDCJT9t")
        api = FakeApi()
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertFalse(os.path.exists(self.jpath("work", "paper.pdf")))
        ident, episode = self.calls()[0]["argv"][1], self.calls()[1]["argv"][1]
        self.assertIn("the paper at https://openreview.net/forum?id=PqvMRDCJT9t", ident)
        self.assertIn("Read it with WebFetch from https://openreview.net/forum?id=PqvMRDCJT9t", episode)
        self.assertNotIn("`crop`:", episode)         # no PDF: drawn figures only
        meta = self.jread("meta.json")
        self.assertEqual(meta["doi"], "10.1000/abc")
        self.assertEqual(meta["year"], 2023)
        self.assertIsNone(meta["source_sha256"])
        html = self.jread("explainer.html")
        self.assertNotIn("<img", html)
        self.assertIn("<svg", html)
        self.assertEqual(self.jread("state.json")["steps"]["explainer"]["dropped"], 1)


class Resume(PipelineCase):
    PHASES = ["claiming", "writing", "checking", "cutting", "explainer", "links", "uploading"]

    def test_kill_between_steps_repeats_nothing(self):
        for phase in self.PHASES:
            with self.subTest(phase=phase):
                self.new_job(phase)
                self.scenario()
                self.write_job()
                api = FakeApi()

                def die(p, f, d, phase=phase):
                    if p == phase:
                        raise Boom(phase)

                with self.assertRaises(Boom):
                    self.run_job(api, progress=die)
                before = self.kinds()
                r = self.run_job(api)
                self.assertEqual(r["status"], "uploaded")
                # every Claude call happened exactly once over the two runs
                self.assertEqual(self.kinds(), NORMAL, (phase, before))
                self.assertEqual(len(api.uploads), 1)
                self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 1)

    def test_kill_mid_episode_resumes_the_session(self):
        self.scenario(hang={"on": "episode", "runs": 1})
        self.write_job()
        code = ("import sys; sys.path[:0] = [%r, %r]\n"
                "from pipeline_helpers import FakeApi, S2Fixture\n"
                "from papercast_cli.pipeline.run import run_job\n"
                "run_job(%r, FakeApi(), lambda *a: None, fetch=S2Fixture())\n"
                % (os.path.dirname(os.path.abspath(__file__)), PKG, self.job))
        worker = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        hang = None
        for _ in range(300):
            hang = [f for f in os.listdir(self.fake) if f.startswith("hanging.")]
            try:
                seen = (self.jread("state.json").get("delivered") or {}).get("episode")
            except (OSError, ValueError):
                seen = False
            if hang and seen:           # the worker has seen the run start (its init event)
                break
            time.sleep(0.1)
        self.assertTrue(hang, "the fake episode run never started")
        stuck = int(hang[0].split(".")[1])
        worker.kill()                       # SIGKILL: the worker dies, its claude stays behind
        worker.wait()
        os.kill(stuck, 0)                   # still alive, orphaned
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        with self.assertRaises(OSError):    # the stale process was stopped first
            for _ in range(50):
                os.kill(stuck, 0)
                time.sleep(0.1)
        self.assertEqual(self.kinds(), ["identify", "episode", "interrupted", "cut", "grade"])
        again = self.calls()[2]["argv"]
        sid = self.calls()[0]["argv"][self.calls()[0]["argv"].index("--session-id") + 1]
        self.assertEqual(again[again.index("--resume") + 1], sid)
        self.assertIn("Your previous run was interrupted", again[1])


class Repair(PipelineCase):
    def test_one_repair_turn_fixes_the_script(self):
        self.scenario(script={"episode": "digits", "repair": "clean"})
        self.write_job()
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), ["identify", "episode", "repair", "cut", "grade"])
        rp = self.calls()[2]["argv"][1]
        self.assertTrue(rp.startswith("[papercast] The episode is not finished yet."))
        self.assertIn("digit(s): numbers must be written as words", rp)
        self.assertTrue(self.jread("state.json")["steps"]["episode"]["repaired"])

    def test_still_wrong_after_the_repair_turn_fails(self):
        self.scenario(script="digits")
        self.write_job()
        with self.assertRaises(PipelineError) as cm:
            self.run_job(FakeApi())
        self.assertEqual((cm.exception.step, cm.exception.code), ("checks", "script_invalid"))
        self.assertEqual(self.kinds(), ["identify", "episode", "repair"])
        self.assertEqual(self.jread("result.json")["code"], "script_invalid")
        # run again: the same verdict, no second repair turn
        with self.assertRaises(PipelineError):
            self.run_job(FakeApi())
        self.assertEqual(self.kinds(), ["identify", "episode", "repair"])

    def test_repair_names_every_problem(self):
        self.scenario(script={"episode": "short", "repair": "clean"},
                      explainer={"episode": "meta", "repair": "svg"})
        self.write_job()
        self.run_job(FakeApi())
        rp = self.calls()[2]["argv"][1]
        self.assertIn("words is about", rp)                       # too short
        self.assertIn("mentions the audio or the episode", rp)    # the GEODE failure
        self.assertIn("repeats the instructions", rp)

    def test_the_hubs_wording_names_this_listener(self):
        self.scenario(script={"episode": "listener", "repair": "clean"})
        self.write_job()
        self.run_job(FakeApi(wording=default_wording("Alice")))
        self.assertEqual(self.kinds()[:3], ["identify", "episode", "repair"])
        self.assertIn('"Alice"', self.calls()[2]["argv"][1])

    def test_cut_pass_deletion_kept_and_rewording_undone(self):
        self.scenario(cut="delete")
        self.write_job()
        self.run_job(FakeApi())
        cut = self.jread("state.json")["steps"]["episode"]["cut"]
        self.assertTrue(cut["script"]["accepted"])
        self.assertEqual(cut["script"]["deleted"], 1)
        self.assertEqual(cut["explainer"]["deleted"], 1)
        self.assertTrue(os.path.isfile(self.jpath("work", "script.precut.md")))
        self.assertEqual(len(self.jread("out", "explainer.json")["points"]), 2)

        self.new_job("reword")
        self.scenario(cut="reword")
        self.write_job()
        self.run_job(FakeApi())
        cut = self.jread("state.json")["steps"]["episode"]["cut"]
        self.assertFalse(cut["script"]["accepted"])
        self.assertIn("script.md", cut["restored"])
        self.assertNotIn("crisper", self.jread("out", "script.md"))


class Limit(PipelineCase):
    def test_usage_limit_raises_with_the_reset_time(self):
        resets = int(time.time()) + 3 * 3600
        self.scenario(limit={"on": "episode", "runs": 1, "resets_at": resets})
        self.write_job()
        with self.assertRaises(UsageLimit) as cm:
            self.run_job(FakeApi())
        e = cm.exception
        self.assertEqual(e.resets_at, resets)
        self.assertEqual(e.resume_at, resets + limits.MARGIN_S)
        self.assertFalse(e.guessed)
        self.assertEqual(self.events[-1][0], "paused")
        res = self.jread("result.json")
        self.assertEqual((res["status"], res["resume_at"]), ("paused", resets + limits.MARGIN_S))
        st = self.jread("state.json")
        self.assertEqual(st["retry_of"]["code"], "claude_usage_limit")
        self.assertNotIn("episode", st["steps"])
        # after the reset: the same session, told why, and the job finishes
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), ["identify", "episode", "retry", "cut", "grade"])
        again = self.calls()[2]["argv"]
        self.assertIn("--resume", again)
        self.assertIn("claude_usage_limit", again[1])

    def test_limit_text_with_a_clock_time(self):
        text = "You've hit your limit · resets 3:30pm (Europe/London)"
        self.scenario(limit={"on": "identify", "runs": 1, "shape": "text", "text": text})
        self.write_job()
        t0 = time.time()
        with self.assertRaises(UsageLimit) as cm:
            self.run_job(FakeApi())
        want = limits.reset_from_text(text, t0)
        self.assertIsNotNone(want)
        self.assertAlmostEqual(cm.exception.resets_at, want, delta=5)
        self.assertAlmostEqual(cm.exception.resume_at, max(want, t0) + limits.MARGIN_S, delta=5)

    def test_limit_without_a_reset_time_waits_an_hour(self):
        self.scenario(limit={"on": "identify", "runs": 1, "shape": "text"})
        self.write_job()
        t0 = time.time()
        with self.assertRaises(UsageLimit) as cm:
            self.run_job(FakeApi())
        self.assertTrue(cm.exception.guessed)
        self.assertAlmostEqual(cm.exception.resume_at, t0 + limits.NO_RESET_S, delta=10)

    def test_limit_while_grading_links(self):
        self.scenario(limit={"on": "grade", "runs": 1, "resets_at": int(time.time()) + 600})
        self.write_job()
        with self.assertRaises(UsageLimit):
            self.run_job(FakeApi())
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), ["identify", "episode", "cut", "grade", "grade"])
        self.assertEqual(len(self.jread("links.json")["links"]), 3)


class Explainer(PipelineCase):
    @unittest.skipUnless(HAVE_POPPLER, "poppler (pdftoppm, pdfinfo) is not installed")
    def test_with_pdftoppm_crops_are_inline_pngs(self):
        self.scenario()
        self.write_job()
        self.run_job(FakeApi())
        html = self.jread("explainer.html")
        self.assertIn('<img src="data:image/png;base64,', html)
        self.assertIn("<svg", html)
        for bad in ("onload", "onclick", "<script"):
            self.assertNotIn(bad, html)
        self.assertTrue(os.path.isfile(self.jpath("work", "pages", "p-001.png")))
        self.assertIn("`crop`:", self.calls()[1]["argv"][1])
        self.assertIn("`pages/p-NNN.png`", self.calls()[1]["argv"][1])

    def test_without_pdftoppm_svg_and_text_only(self):
        pdf.DISABLED.update({"pdftoppm", "pdfinfo", "pdftotext"})
        self.scenario()
        self.write_job()
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        episode = self.calls()[1]["argv"][1]
        self.assertNotIn("`crop`:", episode)
        self.assertNotIn("pages/p-NNN.png", episode)
        self.assertFalse(os.path.exists(self.jpath("work", "pages")))
        html = self.jread("explainer.html")
        self.assertNotIn("<img", html)
        self.assertIn("<svg", html)
        self.assertIn("<h1>Flow Matching for Generative Modeling</h1>", html)
        self.assertIn("Straight paths from noise to data", html)
        self.assertEqual(self.jread("state.json")["steps"]["explainer"]["dropped"], 1)


class Links(PipelineCase):
    def test_links_graded_from_semantic_scholar(self):
        self.scenario()
        self.write_job()
        fx = S2Fixture()
        self.run_job(FakeApi(), fetch=fx)
        got = {(l["other"]["paper_id"], l["direction"], l["grade"], l["source"])
               for l in self.jread("links.json")["links"]}
        self.assertEqual(got, {("p_ddpm00000001", "builds_on", "w", "s2"),     # a reference
                               ("p_sde000000001", "builds_on", "s", "s2"),     # arXiv v2 matched
                               ("p_sd3000000001", "built_on_by", "e", "s2")})  # a citation
        # Adam: graded none, dropped; Neural ODEs: not in the library; PPO: a "citing" paper
        # older than this one; the second page of references was read
        self.assertTrue(any("offset=3" in u for u in fx.urls))
        grade = [c for c in self.calls() if c["kind"] == "grade"]
        self.assertEqual(len(grade), 1)
        self.assertIn("Grade all 4 candidates.", grade[0]["argv"][1])
        self.assertIn("Regressing a vector field", grade[0]["argv"][1])   # the child's claims

    def test_batches_of_fifty(self):
        from papercast_cli.pipeline import links as plinks
        cands = [{"other": f"p{i}", "direction": "builds_on", "source": "s2"} for i in range(120)]
        bs = plinks.batches(cands, "this")
        self.assertEqual([len(b) for b in bs], [50, 50, 20])

    @unittest.skipUnless(pdf.available()["text"], "pdftotext is not installed")
    def test_text_fallback_when_semantic_scholar_has_nothing(self):
        self.scenario()
        self.write_job()
        self.run_job(FakeApi(), fetch=S2Fixture(status=404))
        links = self.jread("links.json")["links"]
        got = {(l["other"]["paper_id"], l["direction"], l["source"]) for l in links}
        # page two names both titles verbatim
        self.assertEqual(got, {("p_ddpm00000001", "builds_on", "text"),
                               ("p_sde000000001", "builds_on", "text")})

    def test_semantic_scholar_down_is_not_fatal(self):
        pdf.DISABLED.add("pdftotext")
        self.scenario()
        self.write_job()
        r = self.run_job(FakeApi(), fetch=S2Fixture(status=500))
        self.assertEqual(r["status"], "uploaded")
        lk = self.jread("links.json")
        self.assertEqual(lk["links"], [])
        self.assertIn("500", lk["info"]["s2_error"])
        self.assertNotIn("grade", self.kinds())

    def test_garbled_grading_answer_is_asked_again(self):
        self.scenario(grade_garbage=1)
        self.write_job()
        self.run_job(FakeApi())
        self.assertEqual(self.kinds(), NORMAL + ["grade"])
        self.assertEqual(len(self.jread("links.json")["links"]), 3)

    def test_a_new_version_never_links_to_itself(self):
        self.scenario()
        self.write_job(version_of="p_sde000000001")
        self.run_job(FakeApi())
        others = {l["other"]["paper_id"] for l in self.jread("links.json")["links"]}
        self.assertNotIn("p_sde000000001", others)


class Hub(PipelineCase):
    def test_existing_paper_needs_confirmation_then_yes(self):
        paper = {"id": "p_fm0000000001", "title": "Flow Matching for Generative Modeling",
                 "episodes": [{"id": "e_1", "made_by": {"id": 2, "name": "Alice"},
                               "prefs_summary": "", "state": "ready"}]}
        self.scenario()
        self.write_job()
        api = FakeApi(paper=paper)
        r = self.run_job(api)
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertEqual(r["question"], "made by Alice. Make your own version? [y/N]")
        self.assertEqual(self.kinds(), ["identify"])        # no episode run was spent
        self.assertFalse([c for c in api.calls if c[0] == "POST"])
        # the person said yes: the CLI sets it in job.json and runs the job again
        self.write_job(yes=True)
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), NORMAL)
        m = api.uploads[0]["manifest"]
        self.assertEqual((m["paper_id"], m["claim_id"]), ("p_fm0000000001", None))
        self.assertFalse([c for c in api.calls if c[0] == "POST"])

    def test_two_makers_are_named(self):
        paper = {"id": "p_x", "title": "T", "episodes": [
            {"made_by": {"name": "Alice"}}, {"made_by": {"name": "Bob"}}, {"made_by": {"name": "Cy"}}]}
        self.scenario()
        self.write_job()
        r = self.run_job(FakeApi(paper=paper))
        self.assertEqual(r["question"], "made by Alice, Bob and Cy. Make your own version? [y/N]")

    def test_version_of_needs_no_claim(self):
        self.scenario()
        self.write_job(version_of="p_given0000001")
        api = FakeApi()
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(api.uploads[0]["manifest"]["paper_id"], "p_given0000001")
        self.assertFalse([c for c in api.calls if c[0] == "POST"])

    def test_someone_else_is_making_it(self):
        self.scenario()
        self.write_job()
        r = self.run_job(FakeApi(conflict=True))
        self.assertEqual(r["status"], "in_progress")
        self.assertEqual(r["by"], {"name": "Bob"})
        self.assertEqual(self.kinds(), ["identify"])

    def test_rejected_by_the_hub(self):
        self.scenario()
        self.write_job()
        r = self.run_job(FakeApi(final_state="rejected"))
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["check_report"], ["script.md: 12 minutes; the range is 15-25"])

    def test_hub_down_at_upload_then_back(self):
        self.scenario()
        self.write_job()
        api = FakeApi()
        orig = api.upload
        api.upload = lambda *a: (_ for _ in ()).throw(ConnectionResetError("reset"))
        with self.assertRaises(PipelineError) as cm:
            self.run_job(api)
        self.assertEqual(cm.exception.code, "hub_unreachable")
        self.assertTrue(cm.exception.retryable)
        api.upload = orig
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), NORMAL)

    def test_answer_lost_after_upload_is_not_uploaded_twice(self):
        self.scenario()
        self.write_job()
        api = FakeApi()
        orig = api.upload

        def sent_then_lost(*a):
            orig(*a)                                     # the hub has the bundle ...
            raise ConnectionResetError("reset by peer")  # ... its answer never arrives

        api.upload = sent_then_lost
        with self.assertRaises(PipelineError) as cm:
            self.run_job(api)
        self.assertTrue(cm.exception.retryable)
        api.upload = orig
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(len(api.uploads), 1)
        self.assertTrue(self.jread("upload.json")["adopted"])

    def test_lapsed_claim_is_taken_again_before_upload(self):
        self.scenario()
        self.write_job()
        api = FakeApi(claim_expires_in=60)       # lapses during the job
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 2)
        self.assertEqual(api.uploads[0]["manifest"]["claim_id"], "c_test00000001")


class ClaudeRuns(PipelineCase):
    def test_lockdown_violation_stops_the_run(self):
        self.scenario(badtools=True)
        self.write_job()
        with self.assertRaises(PipelineError) as cm:
            self.run_job(FakeApi())
        self.assertEqual(cm.exception.code, "tool_violation")
        self.assertIn("Bash", cm.exception.detail)

    def test_overloaded_api_is_waited_out(self):
        self.scenario(transient={"on": "episode", "runs": 1})
        self.write_job()
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), ["identify", "episode", "retry", "cut", "grade"])

    def test_silent_run_is_stopped_and_run_again(self):
        self.scenario(hang={"on": "episode", "runs": 1})
        self.write_job()
        saved = prun.WATCHDOG_S
        prun.WATCHDOG_S = 1.5
        try:
            r = self.run_job(FakeApi())
        finally:
            prun.WATCHDOG_S = saved
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(self.kinds(), ["identify", "episode", "interrupted", "cut", "grade"])
        self.assertEqual(self.jread("state.json")["interrupted"], {"episode": 1})

    def test_lost_session_starts_the_episode_in_a_new_one(self):
        self.scenario(lose_session={"runs": 1})
        self.write_job()
        r = self.run_job(FakeApi())
        self.assertEqual(r["status"], "uploaded")
        calls = self.calls()
        self.assertEqual([c["kind"] for c in calls], ["identify", "episode", "episode", "cut", "grade"])
        a, b = calls[1]["argv"], calls[2]["argv"]
        self.assertIn("--resume", a)
        self.assertIn("--session-id", b)
        self.assertNotEqual(a[a.index("--resume") + 1], b[b.index("--session-id") + 1])

    def test_no_claude_installed(self):
        os.environ["PAPERCAST_CLAUDE"] = os.path.join(self.tmp, "no-such-claude")
        self.write_job()
        with self.assertRaises(PipelineError) as cm:
            self.run_job(FakeApi())
        self.assertEqual(cm.exception.code, "claude_missing")

    def test_the_command_line_is_the_lockdown(self):
        self.scenario()
        self.write_job(model="claude-sonnet-5")
        self.run_job(FakeApi())
        target = os.path.realpath(FAKE)
        for c in self.calls():
            a = c["argv"]
            if c["kind"] == "grade":
                self.assertEqual(a[2:], pclaude.lockdown_bare("claude-haiku-4-5"))
            else:
                self.assertEqual(a[0], "-p")
                self.assertIn(a[2], ("--session-id", "--resume"))
                self.assertEqual(a[4:], pclaude.lockdown("claude-sonnet-5", target))


@unittest.skipUnless(os.path.isdir(os.path.join(LEO_RUNNER, "papercast_runner")),
                     "Leo's runner is not next to this package")
class SameFlagsAsLeosRunner(unittest.TestCase):
    """The claude command line is Leo's runner's (stacks/papercast/runner/papercast_runner/
    claude.py), read, never edited: the episode lockdown minus the vault and library
    directories (the group has neither), and the bare lockdown for the link grader."""

    @classmethod
    def setUpClass(cls):
        sys.dont_write_bytecode = True               # nothing written into Leo's tree
        sys.path.insert(0, LEO_RUNNER)
        from papercast_runner import claude as leo, config as leo_config
        cls.leo, cls.leo_config = leo, leo_config

    def cfg(self):
        class Cfg:
            vault = "/v/vault"
            library_dir = "/v/library"
            path = FAKE_DIR
            claude_config_dir = ""
            fetch_domains = ([d.strip() for d in
                              self.leo_config.DEFAULTS["PAPERCAST_FETCH_DOMAINS"].split(",")], [])
        return Cfg()

    def test_episode_flags(self):
        cfg = self.cfg()
        theirs = self.leo.lockdown(cfg, "claude-opus-5-5")
        dropped = {f"Write(/{cfg.vault}/**)", f"Edit(/{cfg.vault}/**)", f"Write(/{cfg.library_dir}/**)",
                   f"Edit(/{cfg.library_dir}/**)"}
        out, skip = [], 0
        for i, a in enumerate(theirs):
            if skip:
                skip -= 1
                continue
            if a == "--add-dir":
                skip = 1
                continue
            if a in dropped:
                continue
            out.append(a)
        ours = pclaude.lockdown("claude-opus-5-5", os.path.realpath(FAKE))
        self.assertEqual(ours, out)

    def test_bare_flags_and_constants(self):
        self.assertEqual(pclaude.lockdown_bare("claude-haiku-4-5"),
                         self.leo.lockdown_bare(self.cfg(), "claude-haiku-4-5"))
        self.assertEqual(pclaude.TOOLS, self.leo.TOOLS)
        self.assertEqual(pclaude.EXPECTED_PERMISSION_MODE, self.leo.EXPECTED_PERMISSION_MODE)
        self.assertEqual(pclaude.PREAPPROVED_2_1_283, self.leo.PREAPPROVED_2_1_283)
        self.assertEqual(pclaude.FETCH_DOMAINS, self.cfg().fetch_domains[0])

    def test_init_checks_agree(self):
        ok = {"tools": list(pclaude.TOOLS), "mcp_servers": [], "permissionMode": "dontAsk"}
        bad = dict(ok, tools=ok["tools"] + ["Bash"])
        for ev in (ok, bad, dict(ok, mcp_servers=[{"name": "x"}]), dict(ok, permissionMode="default")):
            self.assertEqual(pclaude.check_init(ev) is None, self.leo.check_init(ev) is None)

    def test_limit_detection_agrees(self):
        from papercast_runner import limits as leo_limits
        texts = ["Claude AI usage limit reached|1790455200", "You've hit your limit · resets 3pm",
                 "resets in 2h 5m", "Context limit reached", "API Error: 529 overloaded"]
        now = 1790400000.0
        for t in texts:
            self.assertEqual(limits.reset_from_text(t, now), leo_limits.reset_from_text(t, now), t)
            self.assertEqual(limits.looks_like_limit(t), leo_limits.looks_like_limit(t), t)


class Units(unittest.TestCase):
    def test_arxiv_and_doi_forms(self):
        from papercast_cli.pipeline import title
        self.assertEqual(title.arxiv_id("arXiv:2210.02747v3"), "2210.02747")
        self.assertEqual(title.arxiv_id("https://arxiv.org/pdf/2210.02747v1.pdf"), "2210.02747")
        self.assertEqual(title.arxiv_from_url("https://example.org/abs/2210.02747"), None)
        self.assertEqual(title.doi("https://doi.org/10.1038/S41586-023-06735-9."), "10.1038/s41586-023-06735-9")
        self.assertIsNone(title.clean("paper_v3.pdf"))
        self.assertIsNone(title.clean("https://arxiv.org/abs/2210.02747"))

    def test_only_named_sites_are_downloaded(self):
        self.assertTrue(prun.pdf_link("https://openreview.net/pdf?id=abc"))
        self.assertTrue(prun.pdf_link("https://proceedings.neurips.cc/paper/2020/file/x-Paper.pdf"))
        self.assertFalse(prun.pdf_link("http://openreview.net/pdf?id=abc"))
        self.assertFalse(prun.pdf_link("https://evil.example/paper.pdf"))
        self.assertFalse(prun.pdf_link("https://openreview.net/forum?id=abc"))
        with self.assertRaises(OSError):
            prun.http_download("https://127.0.0.1/paper.pdf")

    def test_minutes_from_the_base_prompt(self):
        import tempfile
        d = tempfile.mkdtemp()
        try:
            j = prun.Job(d, None, None)
            prun.write_json(os.path.join(d, "prompt.json"), {"guideline": "An episode is 12 to 20 minutes."})
            self.assertEqual(j.minutes(), (12.0, 20.0))
            self.assertEqual(j.word_bounds(), (1800, 3000))
            prun.write_json(os.path.join(d, "prompt.json"), {"guideline": "x", "minutes": [10, 30], "wpm": 160})
            self.assertEqual(j.word_bounds(), (1600, 4800))
            prun.write_json(os.path.join(d, "prompt.json"), {"guideline": "no range here"})
            self.assertEqual(j.minutes(), (15.0, 25.0))
        finally:
            import shutil
            shutil.rmtree(d)

    def test_http_errors_of_any_shape(self):
        class E1(Exception):
            status, body = 409, '{"error": "in_progress"}'

        class E2(Exception):
            code = 403

            def read(self):
                return b'{"error": "forbidden"}'

        self.assertEqual((prun._http_status(E1()), prun._http_body(E1())), (409, {"error": "in_progress"}))
        self.assertEqual((prun._http_status(E2()), prun._http_body(E2())), (403, {"error": "forbidden"}))
        self.assertIsNone(prun._http_status(ConnectionRefusedError()))


if __name__ == "__main__":
    unittest.main()
