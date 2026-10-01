"""`papercast replace-script` (replace_script.py) against a fake hub API (the answers of
PUT /api/cli/episodes/<id>/script, hub/scriptswap.py): one version, the dry run, the batch (its
file, a few at a time, a line each), resuming it (FILE.done), refusals and busy ones, the undo,
and the command line. The real hub's side is in stacks/papercast-group/hub/tests/test_voices.py."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from papercast_cli import cli  # noqa: E402
from papercast_cli import replace_script as R  # noqa: E402
from papercast_cli.api import Response  # noqa: E402
from papercast_cli.errors import ApiError, PapercastError  # noqa: E402

GOOD = "# The method\n\nA long and good script.\n"
SHORT = "# Short\n\nToo short.\n"


class FakeApi:
    """The hub's answers: a script containing "Too short" fails the checks, an episode in `busy`
    is being voiced, one in `voiced` is voiced again, others are replaced in the queue; the same
    script twice is no change. Records every call; counts how many run at once."""

    def __init__(self, busy=(), voiced=()):
        self.busy, self.voiced = set(busy), set(voiced)
        self.calls, self.scripts, self.history = [], {}, {}
        self.lock = threading.Lock()
        self.now = self.most = 0

    def request(self, method, path, *, data=None, content_type=None, accept=(), **kw):
        assert (method, content_type) == ("PUT", "application/json"), (method, content_type)
        eid = path.split("/")[4]
        body = json.loads(data)
        with self.lock:
            self.calls.append((eid, body))
            self.now += 1
            self.most = max(self.most, self.now)
        try:
            status, ans = self._answer(eid, body)
        finally:
            with self.lock:
                self.now -= 1
        if status >= 400 and status not in accept:
            raise ApiError(f"hub: {ans.get('message')}", status=status, error=ans.get("error"), body=ans)
        return Response(status, ans, {})

    def _answer(self, eid, b):
        if eid == "e_missing":
            return 404, {"error": "no_such_episode", "message": f"no episode {eid}"}
        if eid in self.busy:
            return 409, {"error": "busy", "message": "it is being voiced right now; send the script again once that is done"}
        base = {"episode_id": eid, "dry_run": b.get("dry_run", False), "words": 2612, "minutes": 17.4}
        if "Too short" in b["script"]:
            p = ["the script is about 0.1 minutes; the base prompt wants 15 to 25"]
            if b.get("dry_run"):
                return 200, dict(base, ok=False, problems=p, how="queue", message="the hub's checks found 1 problem: it would be refused")
            return 422, dict(base, error="checks_failed", message="the hub's checks found 1 problem", ok=False, problems=p)
        if self.scripts.get(eid) == b["script"]:
            return 200, dict(base, ok=True, changed=False, how="unchanged", message="it has this script already: nothing to do")
        how = "revoice" if eid in self.voiced else "queue"
        msg = ("it will be voiced again in Clear female, 2nd in line; the old audio plays until then" if how == "revoice"
               else "the new script is in place; it keeps its place in the voice queue")
        if b.get("dry_run"):
            return 200, dict(base, ok=True, changed=False, how=how, message="the checks pass; " + msg.replace("will be", "would be"))
        self.history.setdefault(eid, []).append(self.scripts.get(eid, "the uploaded script"))
        self.scripts[eid] = b["script"]
        return 200, dict(base, ok=True, changed=True, how=how, change_id=len(self.history[eid]), message=msg)

    def get(self, path, params=None, **kw):
        eid = path.split("/")[4]
        hist = self.history.get(eid, [])
        if params and "before" in params:
            return {"episode_id": eid, "change_id": int(params["before"]), "script": hist[int(params["before"]) - 1]}
        return {"episode_id": eid, "changes": [{"id": i + 1, "state": "done", "at": "2026-10-01T10:00:00Z"}
                                               for i in range(len(hist))]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pc-replace-")
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name, text):
        p = self.d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def batch(self, api, tsv, **kw):
        out = io.StringIO()
        counts = R.run_batch(api, tsv, out=out, **kw)
        return counts, out.getvalue()


class OneVersion(Base):
    def test_replace_and_dry_run(self):
        api = FakeApi(voiced={"e_aaaa"})
        s = self.file("new.md", GOOD)
        h = self.file("x.html", "<!doctype html><p>new</p>")
        it = R.item("e_aaaa", s, explainer_html=h)
        self.assertEqual(it["body"], {"script": GOOD, "explainer_html": "<!doctype html><p>new</p>"})
        res = R.send(api, it, dry_run=True)
        self.assertEqual(res["result"], "would")
        self.assertIn("would be voiced again", res["line"])
        self.assertEqual(api.calls[-1][1]["dry_run"], True)
        self.assertNotIn("e_aaaa", api.scripts, "a dry run changes nothing")
        res = R.send(api, it, dry_run=False)
        self.assertEqual((res["result"], res["line"]),
                         ("replaced", "it will be voiced again in Clear female, 2nd in line; the old audio plays "
                                      "until then (2,612 words, 17.4 min)"))
        self.assertEqual(R.send(api, it, dry_run=False)["result"], "unchanged")

    def test_refusals(self):
        api = FakeApi(busy={"e_busy"})
        bad = R.item("e_aaaa", self.file("short.md", SHORT))
        res = R.send(api, bad, False)
        self.assertEqual((res["result"], res["line"]), ("refused", "REFUSED: the hub's checks found 1 problem"))
        self.assertEqual(res["problems"], ["the script is about 0.1 minutes; the base prompt wants 15 to 25"])
        self.assertEqual(R.send(api, bad, True)["result"], "refused", "the dry run says so too")
        res = R.send(api, R.item("e_busy", self.file("g.md", GOOD)), False)
        self.assertEqual(res["result"], "busy")
        self.assertTrue(res["line"].startswith("busy: it is being voiced right now"))
        self.assertEqual(R.send(api, R.item("e_missing", self.file("g.md", GOOD)), False)["result"], "error")
        with self.assertRaisesRegex(PapercastError, "no such file"):
            R.item("e_aaaa", self.d / "nope.md")
        self.file("latin1.md", "")
        (self.d / "latin1.md").write_bytes("caf\xe9".encode("latin-1"))
        with self.assertRaisesRegex(PapercastError, "not UTF-8"):
            R.item("e_aaaa", self.d / "latin1.md")
        with self.assertRaisesRegex(PapercastError, "not an episode id"):
            R.item("e aaaa", self.file("g.md", GOOD))

    def test_undo(self):
        api = FakeApi()
        R.send(api, R.item("e_aaaa", self.file("g.md", GOOD)), False)
        it, ch = R.undo_item(api, "e_aaaa")
        self.assertEqual((ch["id"], it["body"]), (1, {"script": "the uploaded script"}))
        with self.assertRaisesRegex(PapercastError, "nothing to undo"):
            R.undo_item(api, "e_bbbb")


class Batch(Base):
    def tsv(self, n=5, extra=""):
        lines = ["# the rewritten scripts", ""]
        for i in range(n):
            self.file(f"scripts/{i}.md", GOOD + f"Version {i}.\n")
            lines.append(f"e_ep{i:02d}\tscripts/{i}.md")
        return self.file("batch.tsv", "\n".join(lines) + "\n" + extra)

    def test_a_few_at_a_time_a_line_each_and_a_record(self):
        api = FakeApi(voiced={"e_ep01"})
        self.file("x/e.html", "<!doctype html>")
        tsv = self.tsv(5, extra=f"e_ep99\t{self.d / 'scripts/0.md'}\tx/e.html\n")
        counts, out = self.batch(api, tsv, parallel=3)
        self.assertEqual(counts["replaced"], 6)
        self.assertLessEqual(api.most, 3)
        lines = out.splitlines()
        self.assertEqual(sorted(l.split()[0] for l in lines[:6]), [f"e_ep{i:02d}" for i in range(5)] + ["e_ep99"])
        self.assertIn("e_ep01  it will be voiced again in Clear female", out)
        self.assertEqual(lines[-1], f"6 versions: 6 replaced (record: {tsv}.done)")
        sent = dict(api.calls)
        self.assertEqual(sent["e_ep03"]["script"], GOOD + "Version 3.\n")
        self.assertEqual(sent["e_ep99"]["explainer_html"], "<!doctype html>", "a relative path is from the file's folder")
        done = (self.d / "batch.tsv.done").read_text().splitlines()
        self.assertEqual(len(done), 6)
        self.assertTrue(all(len(l.split("\t")) == 4 for l in done))

    def test_resume_skips_what_was_done_and_tries_the_rest_again(self):
        api = FakeApi(busy={"e_ep02"})
        self.file("scripts/short.md", SHORT)
        tsv = self.tsv(4, extra="e_bad\tscripts/short.md\ne_gone\tscripts/nope.md\n")
        counts, out = self.batch(api, tsv)
        self.assertEqual((counts["replaced"], counts["busy"], counts["refused"], counts["error"]), (3, 1, 1, 1))
        self.assertIn("e_bad  REFUSED: the hub's checks found 1 problem\n    - the script is about 0.1 minutes", out)
        self.assertIn("e_ep02  busy: ", out)
        self.assertIn("run the same command again later", out)
        self.assertIn("e_gone  error: line", out)
        # stopped and run again: what the hub took is skipped; the busy one is sent again
        api.busy.clear()
        n = len(api.calls)
        counts, out = self.batch(api, tsv)
        self.assertEqual((counts["skipped"], counts["replaced"], counts["refused"]), (3, 1, 1))
        self.assertEqual(sorted(e for e, _b in api.calls[n:]), ["e_bad", "e_ep02"])
        self.assertIn("e_ep00  done in an earlier run (same script); skipped", out)
        # a script changed since is sent again
        self.file("scripts/1.md", GOOD + "Version 1, rewritten again.\n")
        n = len(api.calls)
        counts, _out = self.batch(api, tsv)
        self.assertEqual([e for e, _b in api.calls[n:] if e != "e_bad"], ["e_ep01"])
        self.assertEqual(counts["replaced"], 1)

    def test_the_dry_run_records_nothing(self):
        api = FakeApi()
        tsv = self.tsv(3)
        counts, out = self.batch(api, tsv, dry_run=True)
        self.assertEqual(counts["would"], 3)
        self.assertEqual(api.scripts, {})
        self.assertTrue(all(b["dry_run"] for _e, b in api.calls))
        self.assertFalse((self.d / "batch.tsv.done").exists())
        self.assertEqual(out.splitlines()[-1], "3 versions: 3 would")
        counts, _out = self.batch(api, tsv)
        self.assertEqual(counts["replaced"], 3, "a dry run is not a run to skip later")

    def test_a_bad_file_is_refused_before_anything_is_sent(self):
        api = FakeApi()
        for text, why in (("e_a\n", "want episode_id<TAB>script.md"), ("e a\tx.md\n", "not an episode id"),
                          ("e_a\tx.md\ne_a\ty.md\n", "on line 1 already")):
            with self.subTest(why=why), self.assertRaisesRegex(PapercastError, why):
                self.batch(api, self.file("bad.tsv", text))
        self.assertEqual(api.calls, [])
        with self.assertRaisesRegex(PapercastError, "--parallel is 1 to 8"):
            self.batch(api, self.tsv(1), parallel=9)


class CommandLine(Base):
    def run_cli(self, api, *argv):
        out = io.StringIO()
        with mock.patch.object(cli.config, "require_login", return_value={"server": "http://hub", "token": "t"}), \
                mock.patch.object(cli.Api, "from_config", return_value=api), redirect_stdout(out):
            rc = cli.main(["replace-script", *argv])
        return rc, out.getvalue()

    def test_the_command(self):
        api = FakeApi()
        s = self.file("new.md", GOOD)
        rc, out = self.run_cli(api, "e_aaaa", str(s), "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("e_aaaa  the checks pass; the new script is in place", out)
        rc, out = self.run_cli(api, "e_aaaa", str(s))
        self.assertEqual((rc, api.scripts["e_aaaa"]), (0, GOOD))
        rc, out = self.run_cli(api, "e_aaaa", str(self.file("short.md", SHORT)))
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", out)
        rc, out = self.run_cli(api, "e_aaaa", "--undo")
        self.assertEqual((rc, api.scripts["e_aaaa"]), (0, "the uploaded script"))
        self.assertIn("Undoing the replacement", out)
        self.file("s/0.md", GOOD + "x\n")
        tsv = self.file("b.tsv", "e_bbbb\ts/0.md\n")
        rc, out = self.run_cli(api, "--batch", str(tsv), "--parallel", "2")
        self.assertEqual(rc, 0)
        self.assertIn("1 version: 1 replaced", out)
        with redirect_stdout(io.StringIO()), mock.patch.object(cli, "_err") as err:
            with mock.patch.object(cli.config, "require_login", return_value={"server": "http://hub", "token": "t"}), \
                    mock.patch.object(cli.Api, "from_config", return_value=api):
                self.assertEqual(cli.main(["replace-script", "e_aaaa"]), 1)
                self.assertEqual(cli.main(["replace-script", "--batch", str(tsv), "e_aaaa"]), 1)
        self.assertIn("give the new script.md", err.call_args_list[0][0][0])


if __name__ == "__main__":
    unittest.main()
