"""jobs.py with real processes: `papercast add` starts a detached worker that runs the fake
pipeline, at most two at once; a killed worker or job resumes; Claude's limit pauses the queue;
cancel and retry."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

from cli_testlib import CliTestCase

from papercast_cli import jobs, util


def overlap(intervals):
    """The most intervals that are open at one time."""
    points = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals],
                    key=lambda p: (p[0], p[1]))
    cur = best = 0
    for _, d in points:
        cur += d
        best = max(best, cur)
    return best


class JobsTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.login_config()

    def add(self, *args, ok=True):
        r = self.run_cli("add", *args)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def span(self, jid):
        ev = self.events(jid)
        start = next(e["t"] for e in ev if e["ev"] == "start")
        end = next(e["t"] for e in ev if e["ev"] == "end")
        return start, end

    # ------------------------------------------------------------------ parallelism
    def test_two_run_at_once_and_a_third_waits(self):
        pdfs = [self.pdf(f"p{i}.pdf", sleep=1.5, title=f"Paper {i}") for i in range(3)]
        r = self.add(*pdfs)
        self.assertEqual(r.stdout.count("queued"), 3, r.stdout)
        self.assertIn("papercast status", r.stdout)
        # While two run, the third says why it waits.
        self.wait_for(lambda: sum(j["state"] == "running" for j in self.jobs()) == 2, 20,
                      "two running")
        st = self.run_cli("status")
        self.assertEqual(st.returncode, 0, st.stderr)
        if sum(j["state"] == "queued" for j in self.jobs()) == 1:
            self.assertIn("waits for a free slot", st.stdout)
        self.wait_for(lambda: self.all_done(3), 40, "all three done")
        js = self.jobs()
        self.assertEqual([j["state"] for j in js], ["done"] * 3)
        spans = [self.span(j["id"]) for j in js]            # in the order they were added
        self.assertEqual(overlap(spans), 2)
        third = max(spans, key=lambda s: s[0])              # the last to start...
        self.assertIs(third, spans[2])                      # ...is the last added (FIFO)
        self.assertGreaterEqual(third[0] + 0.05, min(spans[0][1], spans[1][1]))
        for j in js:
            self.assertTrue(j["episode_id"].startswith("e_up"))
            self.assertEqual(j["hub_state"], "checking")
            self.assertEqual(j["attempts"], 1)
            ev = self.events(j["id"])[0]
            self.assertGreaterEqual(ev["nice"], 10)        # niced
            self.assertEqual(ev["sid"], ev["pid"])         # its own session
        self.assertEqual(len(self.hub.uploads), 3)
        self.wait_worker_gone()                           # exits when idle
        self.assertIn("worker", self.worker_log())

    def test_worker_survives_the_terminal_closing(self):
        pdf = self.pdf(sleep=2)
        term = subprocess.Popen(
            ["/bin/sh", "-c", f'"{sys.executable}" -m papercast_cli add "{pdf}"; sleep 60'],
            env=self.env, start_new_session=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self._extra_pids = [term.pid]
        self.wait_for(lambda: any(j["state"] == "running" for j in self.jobs()), 20, "running")
        os.killpg(term.pid, signal.SIGHUP)                 # the terminal window closes
        os.killpg(term.pid, signal.SIGKILL)
        term.wait()
        self.wait_for(lambda: self.all_done(1), 30, "done after the terminal closed")
        self.assertEqual(self.jobs()[0]["state"], "done")

    # ------------------------------------------------------------------ resuming
    def test_killed_worker_and_job_resume_on_the_next_command(self):
        """As after a reboot: every process dies; the next papercast command resumes."""
        self.add(self.pdf(sleep=3))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: any(e["ev"] == "stage1" for e in self.events(jid)), 20, "stage1")
        wp = self.worker_pid()
        jp = self.job(jid)["pid"]
        self.assertTrue(wp and jp)
        os.killpg(wp, signal.SIGKILL)
        os.killpg(jp, signal.SIGKILL)
        self.wait_for(lambda: self.worker_pid() is None and
                      not jobs.lock_held(self.state / "jobs" / jid / "run.lock"), 10, "all dead")
        self.assertEqual(self.job(jid)["state"], "running")   # it never said it ended
        st = self.run_cli("status")
        self.assertIn("Resuming", st.stderr)
        self.wait_for(lambda: self.all_done(1), 30, "resumed and done")
        j = self.job(jid)
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(j["attempts"], 2)
        self.assertEqual(j["interruptions"], 1)
        ev = [e["ev"] for e in self.events(jid)]
        self.assertEqual(ev.count("start"), 2)
        self.assertIn("resumed", ev)                      # the pipeline kept its files
        self.assertEqual(ev.count("stage1"), 1)

    def test_a_job_whose_worker_died_is_adopted_not_run_twice(self):
        self.add(self.pdf(sleep=2.5))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(jid).get("pid"), 20, "the job's pid")
        os.killpg(self.worker_pid(), signal.SIGKILL)      # only the worker dies
        self.wait_for(lambda: self.worker_pid() is None, 10, "worker dead")
        self.run_cli("status")                            # starts a new worker
        self.wait_for(lambda: self.all_done(1), 30, "done")
        j = self.job(jid)
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["attempts"], 1)
        self.assertEqual([e["ev"] for e in self.events(jid)].count("start"), 1)

    # ------------------------------------------------------------------ Claude's usage limit
    def test_usage_limit_pauses_everything_then_resumes(self):
        self.add(self.pdf("a.pdf", limit_once=3, sleep=0.4))
        a = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(a)["state"] == "limited", 20, "limited")
        pause = jobs.read_pause()
        self.assertIsNotNone(pause)
        st = self.run_cli("status")
        want = f"Claude limit · resumes {util.local_hhmm(pause['until'])}"
        self.assertIn(want, st.stdout)
        # A job added during the pause waits for it.
        self.add(self.pdf("b.pdf", sleep=0.2))
        b = [j for j in self.jobs() if j["id"] != a][0]["id"]
        time.sleep(0.8)
        self.assertEqual(self.job(b)["state"], "queued")
        st = self.run_cli("status", "--json")
        data = json.loads(st.stdout)
        self.assertTrue(data["paused_until"])
        self.assertIn("Claude limit", {j["id"]: j["status"] for j in data["jobs"]}[b])
        self.wait_for(lambda: self.all_done(2), 30, "both done after the pause")
        self.assertEqual(self.job(a)["state"], "done")
        self.assertEqual(self.job(a)["attempts"], 2)
        b_start = self.span(b)[0]
        self.assertGreaterEqual(b_start, pause["until"] - 0.05)
        self.assertIsNone(jobs.read_pause())              # cleared once over

    def test_pause_times(self):
        now = 1_800_000_000.0
        os.environ["PAPERCAST_LIMIT_MARGIN_S"] = "60"
        self.assertEqual(jobs.set_pause(now + 600, now=now), now + 660)
        # A guess (no reset time) never replaces a real time...
        self.assertEqual(jobs.set_pause(None, now=now), now + 660)
        # ...a later real time extends it.
        self.assertEqual(jobs.set_pause(now + 900, now=now), now + 960)
        jobs.clear_pause()
        self.assertEqual(jobs.set_pause(None, now=now), now + jobs.NO_RESET_S)
        jobs.clear_pause()
        self.assertEqual(jobs.set_pause(now + 30 * 86400, now=now), now + jobs.NO_RESET_S)

    # ------------------------------------------------------------------ cancel, retry, asking
    def test_cancel_a_running_job_kills_its_children(self):
        self.add(self.pdf(sleep=60, child=1))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: any(e["ev"] == "child" for e in self.events(jid)), 20, "child")
        child = next(e["child"] for e in self.events(jid) if e["ev"] == "child")
        r = self.run_cli("cancel", jid[:4])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Cancelled", r.stdout)
        self.assertEqual(self.job(jid)["state"], "cancelled")
        self.assertFalse(jobs.lock_held(self.state / "jobs" / jid / "run.lock"))

        def gone():
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return True
            # a zombie until its parent's session is reaped: dead enough
            try:
                with open(f"/proc/{child}/stat") as fh:
                    return fh.read().split(")")[-1].split()[0] == "Z"
            except OSError:
                return True
        self.wait_for(gone, 10, "the pipeline's child to die")
        self.assertNotIn("end", [e["ev"] for e in self.events(jid)])
        r = self.run_cli("cancel", jid)
        self.assertIn("already cancelled", r.stdout)

    def test_cancel_a_queued_job(self):
        from papercast_cli import jobs as J
        job = J.create(J.parse_input(str(self.pdf())))
        msg = J.cancel(job)
        self.assertIn("Cancelled", msg)
        self.assertEqual(self.job(job["id"])["state"], "cancelled")
        self.assertFalse(J.ensure_worker())               # nothing pending

    def test_a_failed_job_is_retried(self):
        self.add(self.pdf(fail_once="the script is 9 minutes (15-25 wanted)"))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(jid)["state"] == "failed", 20, "failed")
        st = self.run_cli("status")
        self.assertIn("failed: the script is 9 minutes", st.stdout)
        self.assertIn(f"papercast retry {jid}", st.stdout)
        r = self.run_cli("retry", jid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_for(lambda: self.job(jid)["state"] == "done", 20, "done after retry")

    def test_a_crash_in_the_pipeline_fails_the_job_with_the_log(self):
        self.add(self.pdf(crash=1))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(jid)["state"] == "failed", 20, "failed")
        self.assertIn("RuntimeError: boom", self.job(jid)["error"])
        log = (self.state / "jobs" / jid / "log").read_text()
        self.assertIn("Traceback", log)

    def test_asking_then_retry_yes(self):
        self.add(self.pdf(ask=1))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(jid)["state"] == "asking", 20, "asking")
        st = self.run_cli("status")
        self.assertIn("already has an episode by Alice", st.stdout)
        self.assertIn(f"papercast retry {jid} --yes", st.stdout)
        self.assertIn(f"papercast cancel {jid}", st.stdout)
        self.wait_worker_gone()                          # asking is not pending
        r = self.run_cli("retry", jid, "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_for(lambda: self.job(jid)["state"] == "done", 20, "done")
        self.assertTrue(self.events(jid)[-1]["ev"] == "end")
        self.assertTrue(self.job(jid)["yes"])
        last_start = [e for e in self.events(jid) if e["ev"] == "start"][-1]
        self.assertEqual((last_start["yes"], last_start["version_of"]), (True, "p_alice"))

    def test_the_pipeline_finds_the_paper_on_the_hub_then_retry_yes(self):
        """The real pipeline answers needs_confirmation (not NeedsAnswer) when the hub has the
        paper once its title is known: the job asks; it is not marked uploaded."""
        self.add(self.pdf(answer="needs_confirmation"))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(jid)["state"] == "asking", 20, "asking")
        self.assertIsNone(self.job(jid).get("episode_id"))
        st = self.run_cli("status")
        self.assertIn("“A Paper” already has an episode made by Bob. Make your own version?", st.stdout)
        self.assertIn(f"papercast retry {jid} --yes", st.stdout)
        self.assertNotIn("uploaded", st.stdout)
        self.wait_worker_gone()
        r = self.run_cli("retry", jid, "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_for(lambda: self.job(jid)["state"] == "done", 20, "done")
        last_start = [e for e in self.events(jid) if e["ev"] == "start"][-1]
        self.assertEqual((last_start["yes"], last_start["version_of"]), (True, "p_bob"))

    def test_someone_else_making_it_fails_the_job_saying_who(self):
        self.add(self.pdf(answer="in_progress"))
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.job(jid)["state"] == "failed", 20, "failed")
        self.assertIn("Bob is making this paper right now", self.job(jid)["error"])
        self.assertIsNone(self.job(jid).get("episode_id"))
        st = self.run_cli("status")
        self.assertIn(f"papercast retry {jid}", st.stdout)

    def test_a_rejected_episode_is_made_again_as_a_new_job(self):
        self.add(self.pdf(title="Rejected Paper"))
        old = self.jobs()[0]["id"]
        self.wait_for(lambda: self.all_done(1), 20, "done")
        eid, pid = self.job(old)["episode_id"], self.job(old)["paper_id"]
        self.hub.episodes = [{"id": eid, "paper_id": pid, "state": "rejected",
                              "check_report": ["the script is 9.1 minutes"]}]
        r = self.run_cli("retry", old)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("made again as", r.stdout)
        new = [j for j in self.jobs() if j["id"] != old][0]["id"]
        self.wait_for(lambda: self.job(new)["state"] == "done", 20, "the new job done")
        start = self.events(new)[0]
        self.assertEqual((start["version_of"], start["yes"]), (pid, True))
        self.assertEqual(self.job(old)["superseded_by"], new)
        st = self.run_cli("status")
        self.assertNotIn(old, st.stdout)                 # the superseded one is hidden

    def test_retry_of_an_uploaded_job_is_refused(self):
        self.add(self.pdf())
        jid = self.jobs()[0]["id"]
        self.wait_for(lambda: self.all_done(1), 20, "done")
        self.hub.episodes = [{"id": self.job(jid)["episode_id"], "state": "ready"}]
        r = self.run_cli("retry", jid)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already uploaded", r.stderr)

    def test_worker_in_the_foreground_for_login_items(self):
        job = jobs.create(jobs.parse_input(str(self.pdf(sleep=1))))
        fg = subprocess.Popen([sys.executable, "-m", "papercast_cli", "worker"], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True)
        self._extra_pids = [fg.pid]
        self.wait_for(lambda: self.job(job["id"])["state"] == "running", 20, "running")
        second = self.run_cli("worker")
        self.assertIn("already running", second.stdout)
        out, err = fg.communicate(timeout=30)
        self.assertEqual(fg.returncode, 0, err)
        self.assertEqual(self.job(job["id"])["state"], "done")

    # ------------------------------------------------------------------ claude missing
    def test_add_refuses_without_claude(self):
        (self.bin / "claude").unlink()
        r = self.add(self.pdf(), ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Claude Code is not installed", r.stderr)
        self.assertIn("code.claude.com", r.stderr)
        self.assertEqual(self.jobs(), [])

    def test_add_refuses_when_claude_is_logged_out(self):
        env = {**self.env, "FAKE_CLAUDE_LOGGED_OUT": "1"}
        r = self.run_cli("add", self.pdf(), env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not logged in", r.stderr)
        self.assertIn("claude auth login", r.stderr)
        self.assertEqual(self.jobs(), [])

    def test_the_real_claude_is_never_run(self):
        self.add(self.pdf())
        self.wait_for(lambda: self.all_done(1), 20, "done")
        calls = (self.tmp / "claude-calls.log").read_text().splitlines()
        self.assertTrue(calls)
        self.assertTrue(all(c == "auth status --json" for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
