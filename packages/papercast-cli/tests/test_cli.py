"""cli.py: what each command prints and refuses (status with the hub's states, prefs, add's
checks with the hub, inputs, whoami)."""
import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cli_testlib import USER, CliTestCase

from papercast_cli import cli, config, jobs, util
from papercast_cli.errors import PapercastError


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class StatusTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.login_config(user={"name": "Ada", "email": "ada@example.org"})

    def done_job(self, eid, title, **kw):
        job = jobs.create(jobs.parse_input(str(self.pdf(f"{eid}.pdf"))))
        return jobs.update(jobs.job_dir(job["id"]), state="done", episode_id=eid, title=title,
                           paper_id=f"p_{eid}", finished_at=util.now_iso(), **kw)

    def test_hub_states_in_words(self):
        ready = self.done_job("e_1", "Flow Matching for Generative Modeling")
        rejected = self.done_job("e_2", "A Rejected Paper")
        waiting = self.done_job("e_3", "Waiting Paper")
        speaking = self.done_job("e_4", "Speaking Paper")
        checking = self.done_job("e_5", "Checking Paper")
        self.hub.episodes = [
            {"id": "e_1", "paper_id": "p_1", "state": "ready"},
            {"id": "e_2", "state": "rejected",
             "check_report": json.dumps(["the script is 9.1 minutes; 15-25 wanted"])},
            {"id": "e_3", "state": "waiting-for-gpu"},
            {"id": "e_4", "state": "speaking", "progress": 0.4},
            {"id": "e_5", "state": "checking"},
        ]
        rc, out, err = run_main("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("Ada on " + self.hub.url, out)
        self.assertIn(f"ready · {self.hub.url}/#p=p_1", out)
        self.assertIn("rejected by the hub: the script is 9.1 minutes; 15-25 wanted", out)
        self.assertIn(f"papercast retry {rejected['id']} makes it again", out)
        self.assertIn("waiting for the voice", out)
        self.assertIn("being voiced · 40%", out)
        self.assertIn("the hub is checking it", out)
        for j in (ready, waiting, speaking, checking):
            self.assertIn(j["id"], out)
        # The hub's state is remembered in job.json.
        self.assertEqual(self.job(ready["id"])["hub_state"], "ready")

    def test_a_link_the_hub_sends_is_used(self):
        self.done_job("e_1", "T")
        self.hub.episodes = [{"id": "e_1", "state": "ready", "url": "/#e=e_1"}]
        rc, out, _ = run_main("status")
        self.assertIn(f"ready · {self.hub.url}/#e=e_1", out)

    def test_json(self):
        self.done_job("e_1", "T")
        self.hub.episodes = [{"id": "e_1", "state": "ready", "paper_id": "p_1"}]
        rc, out, _ = run_main("status", "--json")
        data = json.loads(out)
        self.assertEqual(data["server"], self.hub.url)
        self.assertEqual(data["jobs"][0]["hub"]["state"], "ready")
        self.assertTrue(data["jobs"][0]["status"].startswith("ready"))

    def test_hub_down_still_shows_local_jobs(self):
        self.done_job("e_1", "T")
        self.hub.fail_next = [503] * 3
        rc, out, _ = run_main("status")
        self.assertEqual(rc, 0)
        self.assertIn("uploaded (episode e_1)", out)
        self.assertIn("The hub's side is unknown", out)

    def test_old_finished_jobs_hide_unless_all(self):
        j = self.done_job("e_1", "Old Paper", hub_state="ready")
        jobs.update(jobs.job_dir(j["id"]),
                    finished_at=util.now_iso(time.time() - 10 * 86400))
        self.hub.episodes = [{"id": "e_1", "state": "ready"}]
        rc, out, _ = run_main("status")
        self.assertNotIn("Old Paper", out)
        self.assertIn("1 older: papercast status --all", out)
        rc, out, _ = run_main("status", "--all")
        self.assertIn("Old Paper", out)

    def test_limited_and_queued_lines(self):
        a = jobs.create(jobs.parse_input(str(self.pdf("a.pdf"))))
        until = jobs.set_pause(time.time() + 3600)
        jobs.update(jobs.job_dir(a["id"]), state="limited", resume_at=util.now_iso(until))
        text = jobs.state_text(self.job(a["id"]), until=until)
        self.assertEqual(text, f"Claude limit · resumes {util.local_hhmm(until)}")
        b = jobs.create(jobs.parse_input(str(self.pdf("b.pdf"))))
        self.assertEqual(jobs.state_text(b, until=until),
                         f"queued · Claude limit · resumes {util.local_hhmm(until)}")
        self.assertIn("waits for a free slot", jobs.state_text(b, busy=2))
        run = {**b, "state": "running", "phase": "writing", "progress": 0.4,
               "detail": "drafting", "attempts": 2}
        self.assertEqual(jobs.state_text(run), "writing · 40% · drafting · attempt 2")
        jobs.clear_pause()

    def test_weekday_shown_when_a_day_away(self):
        now = time.time()
        self.assertRegex(util.local_hhmm(now + 2 * 86400, now), r"^[A-Z][a-z]{2} \d\d:\d\d$")
        self.assertRegex(util.local_hhmm(now + 600, now), r"^\d\d:\d\d$")

    def test_no_jobs(self):
        rc, out, _ = run_main("status")
        self.assertIn("No jobs", out)
        self.assertIn("papercast add", out)


class PrefsTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.login_config()

    def test_show(self):
        rc, out, _ = run_main("prefs")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"maths\s+full")
        self.assertRegex(out, r"emphasis\s+balanced .*\(default\)")
        self.assertIn("derivations", out)
        self.assertNotIn("PUT", [r["method"] for r in self.hub.requests])

    def test_change_merges_and_puts(self):
        rc, out, err = run_main("prefs", "--emphasis", "practice", "--note", "skip the history")
        self.assertEqual(rc, 0, err)
        put = [r for r in self.hub.requests if r["method"] == "PUT"][0]
        self.assertEqual(put["path"], "/api/cli/prefs")
        self.assertEqual(put["body"], {"settings": {"maths": "full", "emphasis": "practice"},
                                       "note": "skip the history"})
        self.assertIn("Saved", out)
        self.assertEqual(self.hub.prefs["version"], 2)

    def test_note_too_long_is_refused_here(self):
        rc, out, err = run_main("prefs", "--note", "x" * 501)
        self.assertEqual(rc, 1)
        self.assertIn("at most 500", err)
        self.assertNotIn("PUT", [r["method"] for r in self.hub.requests])

    def test_bad_choice_is_an_argparse_error(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.main(["prefs", "--maths", "lots"])

    def test_hub_without_put(self):
        self.hub.prefs_put = False
        rc, out, err = run_main("prefs", "--maths", "words")
        self.assertEqual(rc, 1)
        self.assertIn("web page", err)


class AddTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.login_config()

    def test_inputs(self):
        p = jobs.parse_input("2210.02747v2")
        self.assertEqual((p["kind"], p["arxiv_id"], p["url"]),
                         ("url", "2210.02747", "https://arxiv.org/abs/2210.02747"))
        self.assertEqual(jobs.parse_input("arXiv:hep-th/9901001")["arxiv_id"], "hep-th/9901001")
        p = jobs.parse_input("https://arxiv.org/pdf/2210.02747v1.pdf")
        self.assertEqual(p["arxiv_id"], "2210.02747")
        p = jobs.parse_input("10.1038/Nature14539")
        self.assertEqual((p["doi"], p["url"]), ("10.1038/nature14539",
                                                "https://doi.org/10.1038/nature14539"))
        self.assertEqual(jobs.parse_input("https://doi.org/10.1000/XYZ")["doi"], "10.1000/xyz")
        p = jobs.parse_input("https://openreview.net/forum?id=abc")
        self.assertEqual((p["kind"], p["arxiv_id"], p["doi"]), ("url", None, None))
        self.assertEqual(jobs.parse_input(str(self.pdf()))["kind"], "pdf")
        notpdf = self.tmp / "notes.txt"
        notpdf.write_text("hello")
        for bad, why in ((str(notpdf), "not a PDF"), (str(self.tmp), "folder"),
                         ("missing/paper.pdf", "No such file"), ("hello", "not a file")):
            with self.assertRaises(PapercastError) as cm:
                jobs.parse_input(bad)
            self.assertIn(why, str(cm.exception))

    def test_bad_input_does_not_stop_the_others(self):
        r = self.run_cli("add", "nope.pdf", self.pdf())
        self.assertEqual(r.returncode, 1)
        self.assertIn("No such file: nope.pdf", r.stderr)
        self.assertEqual(len(self.jobs()), 1)
        job = self.jobs()[0]
        self.assertEqual(job["kind"], "pdf")
        self.assertTrue((self.state / "jobs" / job["id"] / "source.pdf").exists())
        self.assertEqual(len(job["source_sha256"]), 64)
        self.wait_for(lambda: self.all_done(1), 20, "done")

    def test_model_and_version_of_reach_the_pipeline(self):
        r = self.run_cli("add", self.pdf(), "--version-of", "p_abc", "--model", "claude-sonnet-5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("your own version of p_abc", r.stdout)
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.all_done(1), 20, "done")
        ev = self.events(jid)[0]
        self.assertEqual((ev["version_of"], ev["model"], ev["yes"]),
                         ("p_abc", "claude-sonnet-5", True))
        self.assertNotIn("/api/cli/lookup", self.hub.paths())

    def test_a_bad_model_name_is_refused(self):
        r = self.run_cli("add", self.pdf(), "--model", "gpt 5; rm -rf")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a model name", r.stderr)
        self.assertEqual(self.jobs(), [])

    def test_version_of_takes_one_input(self):
        r = self.run_cli("add", self.pdf("a.pdf"), self.pdf("b.pdf"), "--version-of", "p_x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("one paper at a time", r.stderr)

    def test_existing_paper_is_skipped_without_yes(self):
        self.hub.lookup = {"paper": {"id": "p_flow", "title": "Flow Matching", "episodes": [
            {"id": "e_1", "made_by": {"id": 3, "name": "Alice"}, "prefs_summary": "derivations",
             "state": "ready"}]}, "claim": None}
        r = self.run_cli("add", "2210.02747")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already has an episode by Alice (derivations); skipped", r.stdout)
        self.assertIn("--yes", r.stdout)
        self.assertEqual(self.jobs(), [])
        look = [x for x in self.hub.requests if x["path"] == "/api/cli/lookup"][0]
        self.assertEqual(look["query"], {"arxiv_id": "2210.02747"})

    def test_existing_paper_with_yes_is_your_own_version(self):
        self.hub.lookup = {"paper": {"id": "p_flow", "title": "Flow Matching", "episodes": [
            {"id": "e_1", "made_by": {"id": 3, "name": "Alice"}}]}, "claim": None}
        r = self.run_cli("add", "--yes", "2210.02747")
        self.assertEqual(r.returncode, 0, r.stderr)
        job = self.jobs()[0]
        self.assertEqual((job["version_of"], job["yes"]), ("p_flow", True))

    def test_existing_paper_asks_at_a_terminal(self):
        self.hub.lookup = {"paper": {"id": "p_flow", "title": "Flow Matching", "episodes": [
            {"id": "e_1", "made_by": {"id": 3, "name": "Alice"}}]}, "claim": None}
        with mock.patch("sys.stdin") as stdin, \
                mock.patch.object(cli, "_ask", return_value=True) as ask, \
                mock.patch.object(jobs, "ensure_worker", return_value=False):
            stdin.isatty.return_value = True
            rc, out, err = run_main("add", "2210.02747")
        self.assertEqual(rc, 0, err)
        self.assertIn("Make your own version? [y/N]", ask.call_args[0][0])
        self.assertEqual(self.jobs()[0]["version_of"], "p_flow")

    def test_someone_elses_claim_skips(self):
        self.hub.lookup = {"paper": None, "claim": {"by": {"id": 3, "name": "Bob"},
                                                    "since": util.now_iso()}}
        r = self.run_cli("add", self.pdf())
        self.assertIn("Bob is making this paper right now", r.stdout)
        self.assertEqual(self.jobs(), [])
        look = [x for x in self.hub.requests if x["path"] == "/api/cli/lookup"][0]
        self.assertEqual(set(look["query"]), {"sha256"})

    def test_your_own_claim_goes_on(self):
        self.hub.lookup = {"paper": None, "claim": {"by": {"id": USER["id"], "name": "Ada"},
                                                    "since": util.now_iso()}}
        r = self.run_cli("add", self.pdf())
        self.assertEqual(len(self.jobs()), 1, r.stdout + r.stderr)

    def test_a_viewer_cannot_add(self):
        self.hub.tokens["pcg_valid"]["role"] = "viewer"
        r = self.run_cli("add", self.pdf())
        self.assertEqual(r.returncode, 1)
        self.assertIn("contributor", r.stderr)
        self.assertEqual(self.jobs(), [])

    def test_a_paper_already_in_flight_is_not_added_twice(self):
        pdf = self.pdf(sleep=2)
        self.run_cli("add", pdf)
        r = self.run_cli("add", pdf)
        self.assertIn("already added as", r.stdout)
        self.assertEqual(len(self.jobs()), 1)

    def test_not_logged_in(self):
        config.update(token=None)
        r = self.run_cli("add", self.pdf())
        self.assertEqual(r.returncode, 1)
        self.assertIn("papercast login", r.stderr)

    def test_hub_unreachable_still_makes_the_job(self):
        self.login_config(server="http://127.0.0.1:9")
        with mock.patch.object(jobs, "ensure_worker", return_value=False):
            rc, out, err = run_main("add", str(self.pdf()))
        self.assertEqual(rc, 0, err)
        self.assertIn("Cannot reach", err)
        self.assertEqual(len(self.jobs()), 1)


class WhoamiTest(CliTestCase):
    def test_whoami(self):
        self.login_config()
        rc, out, err = run_main("whoami")
        self.assertEqual(rc, 0, err)
        self.assertIn("Ada <ada@example.org> · contributor", out)
        self.assertIn("this device: ada@laptop", out)
        self.assertEqual(config.load()["user"]["name"], "Ada")

    def test_revoked(self):
        self.login_config(token="pcg_revoked")
        rc, out, err = run_main("whoami")
        self.assertEqual(rc, 1)
        self.assertIn("Run: papercast login", err)

    def test_version(self):
        r = self.run_cli("--version")
        self.assertEqual(r.stdout.strip(), "papercast 0.1.0")


if __name__ == "__main__":
    unittest.main()
