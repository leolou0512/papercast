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
        text = {"paper.txt"} if pdf.available()["text"] else set()          # the PDF's text, for the hub's search
        self.assertEqual(names, {"manifest.json", "script.md", "explainer.json", "explainer.html",
                                 "claims.md"} | text)
        self.assertEqual(m["files"].get("paper_text"), "paper.txt" if text else None)
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


class HubPromptDown(FakeApi):
    """A hub that answers everything except the base prompt."""

    def get(self, path: str) -> dict:
        if path == "/api/cli/prompt":
            self.calls.append(("GET", path))
            raise ConnectionRefusedError("hub down")
        return super().get(path)


class NewestPrompt(PipelineCase):
    """Every job writes from the group's newest base guideline, asked of the hub when the job
    gets to writing; never a copy kept from an earlier job, bundled with the package or fetched
    for a run Claude never saw. Once Claude has the episode instruction the job keeps it."""
    G1 = "# Base guideline, version one\n\n- One episode per paper, 15 to 25 minutes.\n"
    G2 = "# Base guideline, version two\n\n- One episode per paper, 15 to 25 minutes.\n"

    def prompt_gets(self, api) -> int:
        return len([c for c in api.calls if c[:2] == ("GET", "/api/cli/prompt")])

    def test_every_job_asks_the_hub(self):
        from papercast_cli import common
        bundled = [ln for ln in common.base_guideline().splitlines() if ln.startswith("## ")][0]
        for name, text, version in (("one", self.G1, 4), ("two", self.G2, 5)):
            with self.subTest(job=name):
                self.new_job(name)
                self.scenario()
                self.write_job()
                api = FakeApi(guideline=text)
                api.prompt_version = version
                self.assertEqual(self.run_job(api)["status"], "uploaded")
                self.assertEqual(self.prompt_gets(api), 1)
                g = self.jread("work", "guideline.md")
                self.assertTrue(g.startswith(text.strip().splitlines()[0]), g[:80])
                self.assertNotIn(bundled, g)
                self.assertEqual(self.jread("state.json")["steps"]["prompt"]["base_version"], version)
                self.assertEqual(api.uploads[0]["manifest"]["base_version"], version)
        self.assertNotIn("version one", self.jread("work", "guideline.md"))

    def test_hub_unreachable_fails_the_job(self):
        self.scenario()
        self.write_job()
        with self.assertRaises(PipelineError) as cm:
            self.run_job(HubPromptDown())
        e = cm.exception
        self.assertEqual((e.step, e.code, e.retryable), ("prompt", "hub_unreachable", True))
        self.assertIn("current guideline", e.message)
        self.assertIn("never started from an older copy", e.message)
        self.assertFalse(os.path.exists(self.jpath("work", "guideline.md")))
        self.assertNotIn("prompt", self.jread("state.json")["steps"])
        self.assertEqual(self.kinds(), ["identify"])          # no episode was started
        self.assertEqual(self.jread("result.json")["code"], "hub_unreachable")

    def stop_before_the_episode(self, api):
        """Run the job until the prompt step is done and stop it before Claude is given the
        episode instruction."""
        def die(p, f, d):
            if d == "reading the paper":
                raise Boom("before the episode")
        with self.assertRaises(Boom):
            self.run_job(api, progress=die)
        st = self.jread("state.json")
        self.assertIn("prompt", st["steps"])
        self.assertFalse((st.get("delivered") or {}).get("episode"))

    def test_asked_again_until_claude_has_the_instruction(self):
        self.scenario()
        self.write_job()
        api = FakeApi(guideline=self.G1)
        self.stop_before_the_episode(api)
        self.assertIn("version one", self.jread("work", "guideline.md"))
        api.guideline, api.prompt_version = self.G2, 3        # an admin saves a new version
        self.assertEqual(self.run_job(api)["status"], "uploaded")
        self.assertEqual(self.prompt_gets(api), 2)
        g = self.jread("work", "guideline.md")
        self.assertIn("version two", g)
        self.assertNotIn("version one", g)
        self.assertEqual(api.uploads[0]["manifest"]["base_version"], 3)
        self.assertEqual(self.kinds(), NORMAL)

    def test_no_older_copy_when_the_hub_is_down_before_the_episode(self):
        self.scenario()
        self.write_job()
        self.stop_before_the_episode(FakeApi(guideline=self.G1))
        with self.assertRaises(PipelineError) as cm:
            self.run_job(HubPromptDown())
        self.assertEqual((cm.exception.step, cm.exception.code), ("prompt", "hub_unreachable"))
        self.assertEqual(self.kinds(), ["identify"])

    def test_resumed_after_the_limit_keeps_its_guideline(self):
        self.scenario(limit={"on": "episode", "runs": 1, "resets_at": int(time.time()) + 600})
        self.write_job()
        api = FakeApi(guideline=self.G1)
        with self.assertRaises(UsageLimit):
            self.run_job(api)
        api.guideline, api.prompt_version = self.G2, 3
        self.assertEqual(self.run_job(api)["status"], "uploaded")
        self.assertEqual(self.prompt_gets(api), 1)
        self.assertIn("version one", self.jread("work", "guideline.md"))
        self.assertEqual(api.uploads[0]["manifest"]["base_version"], 2)
        self.assertEqual(self.kinds(), ["identify", "episode", "retry", "cut", "grade"])


class QueueInputs(PipelineCase):
    """What `papercast add` was given, as its queue writes job.json (jobs.create): the job runs
    in its own directory, so a relative PDF path, a bare arXiv id or a DOI must still work."""

    def queue_job(self, typed: str) -> None:
        from papercast_cli import jobs
        inp = jobs.parse_input(typed)
        j = {"input": inp["input"], "kind": inp["kind"], "url": inp.get("url"),
             "arxiv_id": inp.get("arxiv_id"), "doi": inp.get("doi"), "source": None}
        if inp["kind"] == "pdf":
            import shutil
            shutil.copyfile(inp["path"], self.jpath("source.pdf"))
            j.update(source="source.pdf", source_name=inp["name"])
        self.write_job(**j)

    def test_a_relative_pdf_path(self):
        self.scenario()
        here = os.getcwd()
        os.chdir(os.path.dirname(self.pdf_path))
        try:
            self.queue_job(os.path.basename(self.pdf_path))
        finally:
            os.chdir(here)
        os.remove(self.pdf_path)                    # the original may move: the job has a copy
        self.assertEqual(self.run_job(FakeApi())["status"], "uploaded")
        self.assertEqual(self.jread("state.json")["steps"]["source"]["kind"], "pdf")
        self.assertTrue(os.path.isfile(self.jpath("work", "paper.pdf")))

    def test_a_bare_arxiv_id_and_a_doi(self):
        with open(self.pdf_path, "rb") as fh:
            body = fh.read()
        arxiv = ("https://arxiv.org/abs/2210.02747", ["https://arxiv.org/pdf/2210.02747"])
        for typed, (url, get) in (("2210.02747", arxiv), ("arXiv:2210.02747", arxiv),
                                  ("10.1000/ABC", ("https://doi.org/10.1000/abc", []))):
            with self.subTest(typed=typed):
                self.new_job(typed.replace("/", "_").replace(":", "_"))
                self.scenario()
                self.queue_job(typed)
                got = []

                def download(u, limit, got=got):
                    got.append(u)
                    return body
                self.assertEqual(self.run_job(FakeApi(), download=download)["status"], "uploaded")
                src = self.jread("state.json")["steps"]["source"]
                self.assertEqual((src["kind"], src["url"]), ("url", url))
                self.assertEqual(got, get)


class PaperText(PipelineCase):
    """The paper's own text goes to the hub for its search (never shown): what pdftotext made
    of the PDF, as UTF-8, at most 2 MB; without pdftotext the bundle simply has none."""

    @unittest.skipUnless(pdf.available()["text"], "pdftotext is not installed")
    def test_the_pdfs_text_is_in_the_bundle(self):
        self.scenario()
        self.write_job()
        api = FakeApi()
        self.run_job(api)
        with tarfile.open(self.jpath("bundle.tar.gz"), "r:gz") as tf:
            sent = tf.extractfile("paper.txt").read()
        with open(self.jpath("text.txt"), "rb") as fh:
            self.assertEqual(sent, fh.read())
        self.assertIn(b"Flow Matching", sent)
        self.assertEqual(api.uploads[0]["manifest"]["files"]["paper_text"], "paper.txt")

    def test_a_long_text_is_cut_to_2_mb(self):
        self.scenario()
        self.write_job()
        from papercast_cli.pipeline.run import Job
        job = Job(self.job, FakeApi(), lambda *a: None)
        with open(self.jpath("text.txt"), "wb") as fh:
            fh.write(("word " * 500_000).encode())
        got = job._paper_text()
        self.assertEqual(len(got), cbundle.PAPER_TEXT_MAX)
        os.unlink(self.jpath("text.txt"))
        self.assertIsNone(job._paper_text())

    def test_without_pdftotext_there_is_none(self):
        pdf.DISABLED.add("pdftotext")
        self.scenario()
        self.write_job()
        api = FakeApi()
        r = self.run_job(api)
        self.assertEqual(r["status"], "uploaded")
        self.assertNotIn("paper_text", api.uploads[0]["manifest"]["files"])
        with tarfile.open(self.jpath("bundle.tar.gz"), "r:gz") as tf:
            self.assertNotIn("paper.txt", tf.getnames())


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
        # page two of the PDF names both references too: "both"
        self.assertEqual(got, {("p_ddpm00000001", "builds_on", "w", "both"),   # a reference
                               ("p_sde000000001", "builds_on", "s", "both"),   # arXiv v2 matched
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

    # ---- both ways, every source, then the grader

    MORE = [
        # named in this paper's text by its arXiv id only
        {"id": "p_otp000000001", "title": "Optimal Transport Paths for Fake Generative Flows", "year": 2021,
         "arxiv_id": "2101.04444", "doi": None, "s2_id": None},
        # names this paper in its own text (the hub's mentions); Semantic Scholar does not know it
        {"id": "p_later0000001", "title": "A Later Fake Paper Citing Flow Matching Carefully", "year": 2024,
         "arxiv_id": "2405.00001", "doi": None, "s2_id": None},
        # nowhere at all
        {"id": "p_fold00000001", "title": "An Unrelated Fake Paper About Protein Folding Kinetics", "year": 2021,
         "arxiv_id": None, "doi": None, "s2_id": None},
        # a generic title the text does contain
        {"id": "p_deep00000001", "title": "Deep Learning", "year": 2016, "arxiv_id": None, "doi": None, "s2_id": None},
        # this paper again (another version: the same title)
        {"id": "p_fmv000000001", "title": "Flow Matching for Generative Modeling", "year": 2022,
         "arxiv_id": None, "doi": None, "s2_id": None},
    ]

    def both_ways(self, api=None, **kw):
        from pipeline_helpers import LIBRARY, PAGE1, PAGE2, make_pdf
        make_pdf(self.pdf_path, [PAGE1, PAGE2 + ["Our paths follow arXiv:2101.04444v3, as deep learning does."]])
        self.scenario()
        self.write_job()
        if api is None:
            api = FakeApi(library=LIBRARY + self.MORE,
                          mentions=[{"paper_id": "p_sd3000000001", "field": "title"},
                                    {"paper_id": "p_later0000001", "field": "arxiv"},
                                    {"paper_id": "p_fmv000000001", "field": "title"}])
        r = self.run_job(api, **kw)
        return api, r, self.jread("links.json")

    @unittest.skipUnless(pdf.available()["text"], "pdftotext is not installed")
    def test_candidates_from_both_directions_and_every_source(self):
        api, r, lk = self.both_ways()
        self.assertEqual(r["status"], "uploaded")
        got = {(l["other"]["paper_id"], l["direction"], l["source"]) for l in lk["links"]}
        self.assertEqual(got, {("p_ddpm00000001", "builds_on", "both"),       # S2 and the title in the text
                               ("p_sde000000001", "builds_on", "both"),
                               ("p_otp000000001", "builds_on", "text"),       # the arXiv id in the text
                               ("p_sd3000000001", "built_on_by", "both"),     # S2 and the hub, one candidate
                               ("p_later0000001", "built_on_by", "text")})    # only the hub knows
        cands = {(c["other"], c["direction"]): c for c in lk["info"]["candidate_list"]}
        self.assertEqual(len(cands), len(lk["info"]["candidate_list"]))     # deduplicated
        self.assertEqual(cands[("p_otp000000001", "builds_on")]["via"], ["text:arxiv"])
        self.assertEqual(cands[("p_sd3000000001", "built_on_by")]["via"], ["s2", "mention:title"])
        self.assertEqual(lk["info"]["versions"], ["p_fmv000000001"])
        # the hub was asked with this paper's keys
        q = [c[1] for c in api.calls if c[0] == "GET" and c[1].startswith("/api/cli/mentions?")]
        self.assertEqual(len(q), 1)
        self.assertIn("arxiv=2210.02747", q[0])
        self.assertIn("title=Flow+Matching+for+Generative+Modeling", q[0])
        # the grader saw the filtered candidates only: Adam (an S2 reference, graded none) and
        # these five; never the unrelated paper, the generic title, or this paper's other version
        grade = [c["argv"][1] for c in self.calls() if c["kind"] == "grade"]
        self.assertEqual(len(grade), 1)
        self.assertIn("Grade all 6 candidates.", grade[0])
        for t in ("Optimal Transport Paths", "A Later Fake Paper", "Scaling Rectified Flow"):
            self.assertIn(t, grade[0])
        for t in ("Protein Folding", '"Deep Learning"', "Proximal Policy"):
            self.assertNotIn(t, grade[0])
        self.assertNotIn("p_fmv000000001", {c["other"] for c in lk["info"]["candidate_list"]})
        # the bundle carries the sources
        man = api.uploads[-1]["manifest"]
        self.assertEqual({l["source"] for l in man["links"]}, {"both", "text"})

    @unittest.skipUnless(pdf.available()["text"], "pdftotext is not installed")
    def test_a_hub_without_mentions_gets_s2_not_both(self):
        from pipeline_helpers import LIBRARY
        api, r, lk = self.both_ways(FakeApi(library=LIBRARY + self.MORE, mentions=None))
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual({l["source"] for l in lk["links"]}, {"s2", "text"})
        self.assertIn("404", lk["info"]["mentions_error"])
        self.assertNotIn("p_later0000001", {l["other"]["paper_id"] for l in lk["links"]})

    @unittest.skipUnless(pdf.available()["text"], "pdftotext is not installed")
    def test_semantic_scholar_rate_limited_falls_back_to_the_texts(self):
        from papercast_cli.pipeline import links as plinks
        waits = []
        fx = S2Fixture(status=429)
        api, r, lk = self.both_ways(fetch=plinks.retrying(fx, sleep=waits.append))
        self.assertEqual(r["status"], "uploaded")
        self.assertEqual(len(fx.urls), plinks.RETRY_TRIES)          # one request, every try, then given up
        self.assertEqual(len(waits), plinks.RETRY_TRIES - 1)
        self.assertIn("429", lk["info"]["s2_error"])
        self.assertIn("429", self.jread("state.json")["steps"]["links"]["s2_error"])
        self.assertTrue(any("links from the texts only" in e[2] for e in self.events))
        got = {(l["other"]["paper_id"], l["direction"], l["source"]) for l in lk["links"]}
        self.assertEqual(got, {("p_ddpm00000001", "builds_on", "text"), ("p_sde000000001", "builds_on", "text"),
                               ("p_otp000000001", "builds_on", "text"), ("p_sd3000000001", "built_on_by", "text"),
                               ("p_later0000001", "built_on_by", "text")})

    def test_citations_rate_limited_keep_the_references(self):
        pdf.DISABLED.add("pdftotext")
        fx = S2Fixture()

        def fetch(url):
            return (429, None, {"Retry-After": "1"}) if "/citations" in url else fx(url)
        self.scenario()
        self.write_job()
        self.run_job(FakeApi(), fetch=fetch)
        lk = self.jread("links.json")
        self.assertEqual({(l["other"]["paper_id"], l["direction"]) for l in lk["links"]},
                         {("p_ddpm00000001", "builds_on"), ("p_sde000000001", "builds_on")})
        self.assertIn("citations", lk["info"]["s2_error"])

    def test_candidates_are_capped_per_direction(self):
        from papercast_cli.pipeline import links as plinks
        lib = [{"id": f"p_x{i:011d}", "title": f"Fake Paper Number {i} On Something Quite Specific",
                "year": 2020, "arxiv_id": f"2001.{10000 + i}", "doi": None} for i in range(12)]
        text = " ".join(f"arXiv:2001.{10000 + i}" for i in range(12))
        old = plinks.MAX_PER_DIRECTION
        plinks.MAX_PER_DIRECTION = 5
        try:
            cands, info = plinks.candidates({"title": "This Fake Paper", "year": 2022}, plinks.Library(lib), None,
                                            text, set(), [{"paper_id": f"p_x{i:011d}"} for i in range(3)])
        finally:
            plinks.MAX_PER_DIRECTION = old
        self.assertEqual(sum(c["direction"] == "builds_on" for c in cands), 5)
        self.assertEqual(len(info["dropped"]["builds_on"]), 7)
        self.assertNotIn("built_on_by", info["dropped"])         # 3 each way: under the cap

    def test_a_new_version_never_links_to_itself(self):
        self.scenario()
        self.write_job(version_of="p_sde000000001")
        self.run_job(FakeApi())
        others = {l["other"]["paper_id"] for l in self.jread("links.json")["links"]}
        self.assertNotIn("p_sde000000001", others)



class Retrying(unittest.TestCase):
    """Semantic Scholar's rate limit (many jobs on one machine): 429 and 5xx tried again with
    backoff and jitter, Retry-After respected, no answer at all tried less, then given up."""

    def fetcher(self, answers):
        seen = []

        def fetch(url):
            seen.append(url)
            return answers[min(len(seen), len(answers)) - 1]
        return fetch, seen

    def test_waits_back_off_with_jitter_and_retry_after(self):
        from papercast_cli.pipeline import links as plinks
        fetch, seen = self.fetcher([(429, None, {"Retry-After": "7"}), (503, None, {}), (429, None),
                                    (200, {"ok": 1}, {})])
        waits = []
        get = plinks.retrying(fetch, sleep=waits.append, rand=lambda: 0.5)
        self.assertEqual(get("u"), (200, {"ok": 1}))
        self.assertEqual(len(seen), 4)
        self.assertGreaterEqual(waits[0], 7.0)                  # Retry-After, over the backoff
        self.assertEqual(waits[1:], [3.0, 6.0])                 # 4 s and 8 s, half of each random
        # jitter: two jobs do not wait the same
        a, b = [], []
        for w, r in ((a, lambda: 0.0), (b, lambda: 0.99)):
            plinks.retrying(self.fetcher([(429, None)])[0], sleep=w.append, rand=r)("u")
        self.assertTrue(all(x < y for x, y in zip(a, b)))
        self.assertLessEqual(max(b), plinks.BACKOFF_CAP_S)

    def test_s2_citation_pages_stop_at_its_window_and_keep_what_came(self):
        import tempfile, urllib.parse as up
        from papercast_cli.pipeline import links as plinks
        asked = []

        def fetch(url):
            q = dict(up.parse_qsl(up.urlsplit(url).query))
            off, lim = int(q["offset"]), int(q["limit"])
            asked.append((off, lim))
            if off + lim >= plinks.S2_WINDOW:
                return 400, {"error": "offset + limit must be < 10000"}
            nxt = off + lim if off + lim < 9_500 else None
            return 200, {"data": [{"citingPaper": {"paperId": f"p{off + k}"}} for k in range(lim)], "next": nxt}

        s2 = plinks.S2(fetch, tempfile.mkdtemp(prefix="pcg-s2-"))
        got = s2.edges("X", "citations")
        self.assertEqual(asked[-1], (9000, 999))            # the last page shrinks to fit the window
        self.assertEqual(len(got), 9999)
        # a page refused after the first keeps the pages already fetched
        def flaky(url):
            off = int(dict(up.parse_qsl(up.urlsplit(url).query))["offset"])
            return (200, {"data": [{"citingPaper": {"paperId": "a"}}], "next": 1}) if off == 0 else (500, None)
        self.assertEqual(len(plinks.S2(flaky, tempfile.mkdtemp(prefix="pcg-s2-")).edges("Y", "citations")), 1)
        with self.assertRaises(OSError):                     # nothing at all: still an error
            plinks.S2(lambda url: (500, None), tempfile.mkdtemp(prefix="pcg-s2-")).edges("Z", "references")

    def test_gives_up_and_says_so(self):
        from papercast_cli.pipeline import links as plinks
        fetch, seen = self.fetcher([(429, None)])
        waits = []
        self.assertEqual(plinks.retrying(fetch, sleep=waits.append)("u"), (429, None))
        self.assertEqual((len(seen), len(waits)), (plinks.RETRY_TRIES, plinks.RETRY_TRIES - 1))
        fetch, seen = self.fetcher([(0, None)])                 # the network: fewer tries
        plinks.retrying(fetch, sleep=lambda s: None)("u")
        self.assertEqual(len(seen), plinks.NET_TRIES)
        fetch, seen = self.fetcher([(404, None)])               # an answer: never again
        self.assertEqual(plinks.retrying(fetch, sleep=lambda s: None)("u"), (404, None))
        self.assertEqual(len(seen), 1)
        s2 = plinks.S2(plinks.retrying(self.fetcher([(429, None)])[0], sleep=lambda s: None),
                       os.path.join(os.environ.get("TMPDIR", "/tmp"), f"pc-s2-{os.getpid()}"))
        with self.assertRaisesRegex(OSError, "429 .rate limited"):
            s2.get("/paper/x", {})

    def test_retry_after_forms(self):
        from email.utils import formatdate
        from papercast_cli.pipeline import links as plinks
        self.assertEqual(plinks.retry_after({"retry-after": "12"}), 12.0)
        self.assertAlmostEqual(plinks.retry_after({"Retry-After": formatdate(time.time() + 30, usegmt=True)}), 30, delta=2)
        self.assertIsNone(plinks.retry_after({"Retry-After": "soon"}))
        self.assertIsNone(plinks.retry_after(None))


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
