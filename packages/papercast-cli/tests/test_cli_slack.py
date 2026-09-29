"""`papercast add`'s Slack question: "Post to #t-machinelearning when it's ready? [Y/n]", asked
once per run after the checks and before any job is made, only when the hub says Slack is set up
(GET /api/cli/features); --slack / --no-slack answer it; without a terminal the person's default
does (papercast prefs --slack on|off). The answer goes into job.json and from there into the
bundle's manifest as "announce": {"slack": true}. Against the fake hub; a real pty for the
terminal case."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cli_testlib import CliTestCase
from pipeline_helpers import FakeApi, PipelineCase

from papercast_cli import cli, jobs, util
from papercast_cli.common import bundle

SLACK = {"enabled": True, "channel": "#t-machinelearning", "default": True}
QUESTION = "Post to #t-machinelearning when it's ready? [Y/n] "


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class AddQuestionTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.login_config()
        self.hub.slack = dict(SLACK)

    def at_terminal(self, *argv, answer=""):
        """`papercast add` in this process with a terminal on stdin; returns (rc, out, err, asked)
        where asked lists (question, jobs made by then, hub paths seen by then)."""
        asked = []

        def ask(q):
            asked.append((q, len(self.jobs()), list(self.hub.paths())))
            return answer

        with mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", side_effect=ask), \
                mock.patch.object(jobs, "ensure_worker", return_value=False):
            stdin.isatty.return_value = True
            rc, out, err = run_main("add", *argv)
        return rc, out, err, asked

    def test_enter_means_yes_and_it_is_asked_after_the_checks(self):
        rc, out, err, asked = self.at_terminal(str(self.pdf()))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(asked), 1)
        q, made, seen = asked[0]
        self.assertEqual(q, QUESTION)
        self.assertEqual(made, 0)                                   # before any job
        self.assertIn("/api/cli/lookup", seen)                      # after the hub's checks
        self.assertEqual(self.jobs()[0]["announce"], {"slack": True})

    def test_no_and_yes(self):
        for answer, want in (("n", False), ("No", False), ("y", True), ("yes", True), ("?", True)):
            with self.subTest(answer=answer):
                rc, out, err, asked = self.at_terminal(str(self.pdf(f"{answer}.pdf")), answer=answer)
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.jobs()[-1]["announce"], {"slack": want})

    def test_once_for_several_papers(self):
        rc, out, err, asked = self.at_terminal(str(self.pdf("a.pdf")), str(self.pdf("b.pdf")), answer="n")
        self.assertEqual(rc, 0, err)
        self.assertEqual([a[0] for a in asked], ["Post to #t-machinelearning when they're ready? [Y/n] "])
        self.assertEqual([j["announce"] for j in self.jobs()], [{"slack": False}, {"slack": False}])

    def test_the_default_off_shows_as_y_N(self):
        self.hub.slack["default"] = False
        rc, out, err, asked = self.at_terminal(str(self.pdf()))
        self.assertEqual(asked[0][0], "Post to #t-machinelearning when it's ready? [y/N] ")
        self.assertEqual(self.jobs()[0]["announce"], {"slack": False})

    def test_nothing_to_make_asks_nothing(self):
        self.hub.lookup = {"paper": None, "claim": {"by": {"id": 3, "name": "Bob"}, "since": util.now_iso()}}
        rc, out, err, asked = self.at_terminal(str(self.pdf()))
        self.assertEqual((asked, self.jobs()), ([], []))

    def test_the_flags_skip_the_question(self):
        for flag, want in (("--slack", True), ("--no-slack", False)):
            with self.subTest(flag=flag):
                rc, out, err, asked = self.at_terminal(flag, str(self.pdf(f"{flag}.pdf")))
                self.assertEqual((rc, asked), (0, []), err)
                self.assertEqual(self.jobs()[-1]["announce"], {"slack": want})

    def test_without_a_terminal_the_default_answers(self):
        r = self.run_cli("add", self.pdf("a.pdf"), input="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Post to", r.stdout + r.stderr)
        self.assertEqual(self.jobs()[0]["announce"], {"slack": True})
        self.hub.slack["default"] = False
        r = self.run_cli("add", self.pdf("b.pdf"), input="")
        self.assertEqual(self.jobs()[1]["announce"], {"slack": False})
        r = self.run_cli("add", "--slack", self.pdf("c.pdf"), input="")
        self.assertEqual(self.jobs()[2]["announce"], {"slack": True})
        self.wait_for(lambda: self.all_done(3), 30, "done")
        self.assertEqual(self.hub.paths("GET").count("/api/cli/features"), 3)

    def test_a_real_terminal(self):
        """A pty on stdin, as a person runs it: the question on stdout, Enter answers."""
        master, slave = os.openpty()
        try:
            p = subprocess.Popen([sys.executable, "-m", "papercast_cli", "add", str(self.pdf())],
                                 env=self.env, cwd=str(self.tmp), stdin=slave, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            os.write(master, b"\n")
            out, err = p.communicate(timeout=60)
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn(QUESTION, out)
        self.assertLess(out.index(QUESTION), out.index("Writing in the background"))
        self.assertEqual(self.jobs()[0]["announce"], {"slack": True})

    def test_a_hub_without_slack_asks_nothing(self):
        for slack in (None, {"enabled": False, "channel": "#t-machinelearning", "default": True}):
            with self.subTest(slack=slack):
                self.hub.slack = slack
                rc, out, err, asked = self.at_terminal(str(self.pdf(f"{bool(slack)}.pdf")))
                self.assertEqual((rc, asked, err), (0, [], ""))
                self.assertIsNone(self.jobs()[-1]["announce"])
        rc, out, err, asked = self.at_terminal("--slack", str(self.pdf("flag.pdf")))
        self.assertIn("no Slack channel set up", err)
        self.assertIsNone(self.jobs()[-1]["announce"])

    def test_a_hub_that_does_not_answer(self):
        self.login_config(server="http://127.0.0.1:9")
        rc, out, err, asked = self.at_terminal(str(self.pdf("a.pdf")))
        self.assertEqual((rc, asked), (0, []), err)
        self.assertIsNone(self.jobs()[0]["announce"])
        rc, out, err, asked = self.at_terminal("--slack", str(self.pdf("b.pdf")))
        self.assertEqual(self.jobs()[1]["announce"], {"slack": True})     # said outright: kept

    def test_a_rejected_job_made_again_keeps_the_answer(self):
        job = jobs.create(jobs.parse_input(str(self.pdf())), announce={"slack": True})
        jobs.update(jobs.job_dir(job["id"]), state="done", episode_id="e_x", hub_state="rejected")
        new = jobs.retry(jobs.read(jobs.job_dir(job["id"])))
        self.assertEqual(new["announce"], {"slack": True})
        with self.assertRaises(ValueError):                 # the pipeline cannot change it
            jobs.Progress(jobs.job_dir(new["id"]))(announce={"slack": False})


class PrefsSlackTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.login_config()
        self.hub.slack = dict(SLACK)

    def test_off_and_on(self):
        rc, out, err = run_main("prefs", "--slack", "off")
        self.assertEqual(rc, 0, err)
        put = [r for r in self.hub.requests if r["method"] == "PUT"]
        self.assertEqual([(r["path"], r["body"]) for r in put], [("/api/cli/slack", {"default": False})])
        self.assertIn("[y/N]", out)
        self.assertIn("means no", out)
        self.assertFalse(self.hub.slack["default"])
        rc, out, err = run_main("prefs", "--slack", "on")
        self.assertTrue(self.hub.slack["default"])
        self.assertIn("[Y/n]", out)
        self.assertNotIn("/api/cli/prefs", [r["path"] for r in self.hub.requests if r["method"] == "PUT"])

    def test_with_other_changes(self):
        rc, out, err = run_main("prefs", "--slack", "off", "--maths", "words")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(r["path"] for r in self.hub.requests if r["method"] == "PUT"),
                         ["/api/cli/prefs", "/api/cli/slack"])

    def test_shown(self):
        rc, out, err = run_main("prefs")
        self.assertRegex(out, r"slack\s+on\s+add asks \"Post to #t-machinelearning when it's ready\?\"; Enter means yes")
        self.hub.slack = None
        rc, out, err = run_main("prefs")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("slack", out)

    def test_a_hub_without_slack(self):
        self.hub.slack = None
        rc, out, err = run_main("prefs", "--slack", "on")
        self.assertEqual(rc, 1)
        self.assertIn("no Slack", err)

    def test_bad_value(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.main(["prefs", "--slack", "maybe"])


class ManifestTest(PipelineCase):
    def test_the_answer_reaches_the_manifest(self):
        for i, (announce, want) in enumerate((({"slack": True}, {"slack": True}),
                                              ({"slack": False}, {"slack": False}), (None, None))):
            with self.subTest(announce=announce):
                self.new_job(f"j{i}")
                self.scenario()
                self.write_job(announce=announce)
                api = FakeApi()
                r = self.run_job(api)
                self.assertEqual(r["status"], "uploaded", r)
                m = api.uploads[0]["manifest"]
                self.assertEqual(m.get("announce"), want)
                self.assertEqual("announce" in m, want is not None)

    def test_validate(self):
        base = {"manifest_version": 1, "client_version": "0.1.0", "base_version": 1,
                "prefs": {"settings": {}, "note": ""}, "model": "claude-opus-5-5", "claim_id": "c_abcd1234",
                "paper": {"title": "T"}, "files": {"script": "s.md", "explainer_json": "e.json",
                                                   "explainer_html": "e.html"}}
        names = {"s.md", "e.json", "e.html", "manifest.json"}
        self.assertEqual(bundle.validate(base, names), [])
        for ok in ({"slack": True}, {"slack": False}, {}):
            self.assertEqual(bundle.validate(dict(base, announce=ok), names), [], ok)
        for bad in ("yes", True, {"slack": "yes"}, {"slack": 1}):
            self.assertEqual(bundle.validate(dict(base, announce=bad), names),
                             ['announce: an object like {"slack": true}, or left out'], bad)
        self.assertEqual(bundle.announce({"slack": True}), {"announce": {"slack": True}})
        self.assertEqual(bundle.announce(None), {})
        self.assertEqual(bundle.announce({"slack": "yes"}), {})
        json.dumps(bundle.announce({"slack": False}))


if __name__ == "__main__":
    unittest.main()
