"""`papercast`: the command line (SPEC.md section 11).

  papercast login [--server URL]      log this computer in to the group's hub
  papercast add <pdf|url|arXiv id>... make episodes (in the background, at most 2 at once);
                                      asks whether to post them to the group's Slack channel
  papercast status [--all] [--json]   how far each one is, and the hub's side
  papercast prefs [--maths ...]       your listening preferences (stored on the hub), and
                  [--slack on|off]    whether add's Slack question defaults to yes
  papercast cancel|retry <job>        stop a job, or run it again
  papercast whoami | logout | worker
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from . import __version__
from . import config
from . import jobs
from . import login as login_mod
from . import util
from .api import Api
from .common import prefs as prefs_mod
from .errors import ApiError, AuthError, NetworkError, NotFound, PapercastError, ServerError


# A full id (claude-opus-5-5, claude-haiku-4-5-20251001, ...[1m]) or an alias claude takes.
MODEL_RE = re.compile(r"^(?:claude-[a-z0-9.-]+|opus|sonnet|haiku|fable)(?:\[1m\])?$")


def _err(msg: str) -> None:
    print(f"papercast: {msg}", file=sys.stderr)


def _indent(text: str, pad: str = "         ") -> str:
    return text.replace("\n", "\n" + pad)


# --------------------------------------------------------------------------- login, whoami

def cmd_login(args) -> int:
    login_mod.login(args.server, device=args.device, browser=not args.no_browser)
    return 0


def cmd_logout(args) -> int:
    login_mod.logout()
    return 0


def cmd_whoami(args) -> int:
    cfg = config.require_login()
    me = Api.from_config(cfg, retries=1, timeout=20).me()
    if isinstance(me, dict):
        config.update(user=me)
    else:
        me = {}
    print(f"{me.get('name') or '?'} <{me.get('email') or '?'}> · {me.get('role') or '?'}")
    print(f"{cfg['server']} · this device: {cfg.get('device') or config.default_device()}")
    return 0


# --------------------------------------------------------------------------- add

def _makers(paper: dict) -> str:
    out = []
    for e in paper.get("episodes") or []:
        who = (e.get("made_by") or {}).get("name") if isinstance(e.get("made_by"), dict) else None
        s = who or "someone"
        if e.get("prefs_summary"):
            s += f" ({e['prefs_summary']})"
        out.append(s)
    return ", ".join(out) or "someone"


def _ask(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _ask_default(question: str, default: bool) -> bool:
    """[Y/n] or [y/N]: Enter (or no more input) takes the default."""
    try:
        a = input(question).strip().lower()
    except EOFError:
        return default
    if a.startswith("y"):
        return True
    if a.startswith("n"):
        return False
    return default


def _slack_features(api: Api) -> dict:
    """The hub's Slack: {"enabled", "channel", "default"}; {} for a hub without it."""
    try:
        f = api.get("/api/cli/features", retries=0, timeout=10)
    except (ApiError, PapercastError):
        return {}
    f = f.get("slack") if isinstance(f, dict) else None
    return f if isinstance(f, dict) else {}


def _announce(api: Api, flag: bool | None, me, n: int) -> dict | None:
    """Post to the group's Slack channel when the episode is ready? Asked once for all of this
    run's papers, and only when the hub has Slack set up. --slack / --no-slack answer it; without
    a terminal, the person's default does (papercast prefs --slack). The hub posts, so the
    answer goes into job.json and from there into the bundle's manifest."""
    if not isinstance(me, dict):                 # the hub did not answer: only a flag counts
        return None if flag is None else {"slack": flag}
    slack = _slack_features(api)
    if not slack.get("enabled"):
        if flag:
            _err("This hub has no Slack channel set up, so nothing will be posted there.")
        return None
    if flag is not None:
        return {"slack": flag}
    default = slack.get("default") is not False
    if not sys.stdin.isatty():
        return {"slack": default}
    where = slack.get("channel") or "the group's Slack channel"
    when = "it's" if n == 1 else "they're"
    return {"slack": _ask_default(f"Post to {where} when {when} ready? "
                                  f"{'[Y/n]' if default else '[y/N]'} ", default)}


def _check_hub(api: Api, inp: dict, me: dict, yes: bool) -> tuple[bool, str | None]:
    """Before a job is made: is this paper on the hub already, or being made? (go, version_of).
    Only what is known without reading the paper (arXiv id, DOI, the PDF's hash); a match by
    title is found by the pipeline, which then asks through `papercast status`."""
    keys = jobs.keys_of(inp)
    if not keys:
        return True, None
    try:
        r = api.lookup(**keys)
    except (ApiError, PapercastError):
        return True, None                    # the pipeline looks again before it claims
    r = r if isinstance(r, dict) else {}
    paper, claim = r.get("paper"), r.get("claim")
    name = inp["input"]
    if isinstance(paper, dict) and paper.get("id"):
        if not paper.get("episodes"):
            return True, paper["id"]
        title = paper.get("title") or name
        mine = [e for e in paper["episodes"] if isinstance(e.get("made_by"), dict)
                and me.get("id") is not None and e["made_by"].get("id") == me.get("id")]
        what = "your episode" if mine and len(mine) == len(paper["episodes"]) \
            else f"an episode by {_makers(paper)}"
        if yes:
            return True, paper["id"]
        if not sys.stdin.isatty():
            print(f"{name}: “{title}” already has {what}; skipped. Add --yes to make "
                  "your own version.")
            return False, None
        if _ask(f"“{title}” already has {what}. Make your own version? [y/N] "):
            return True, paper["id"]
        print(f"{name}: skipped.")
        return False, None
    if isinstance(claim, dict):
        by = claim.get("by")
        by_id = by.get("id") if isinstance(by, dict) else None
        if by_id is not None and by_id == me.get("id"):
            return True, None                # your own earlier attempt holds it
        who = (by.get("name") if isinstance(by, dict) else by) or "Someone"
        since = util.parse_iso(claim.get("since"))
        when = f" (since {util.local_hhmm(since)})" if since else ""
        print(f"{name}: {who} is making this paper right now{when}; skipped. Try again once "
              "theirs is up.")
        return False, None
    return True, None


def cmd_add(args) -> int:
    cfg = config.require_login()
    if args.version_of and len(args.inputs) > 1:
        raise PapercastError("--version-of takes one paper at a time")
    if args.model is not None and not MODEL_RE.match(args.model):
        raise PapercastError(f"--model {args.model!r} is not a model name like claude-opus-5-5 "
                             "or sonnet")
    inputs, problems = [], 0
    for s in args.inputs:
        try:
            inputs.append(jobs.parse_input(s))
        except PapercastError as e:
            _err(str(e))
            problems += 1
    if not inputs:
        return 1
    jobs.check_claude(warn=_err)

    api = Api.from_config(cfg, retries=1, timeout=20)
    me = None
    try:
        me = api.me()
        if isinstance(me, dict):
            config.update(user=me)
    except (NetworkError, ServerError) as e:
        _err(f"{e}\nThe jobs are made anyway; each checks with the hub before it uploads.")
    if isinstance(me, dict) and me.get("role") == "viewer":
        raise PapercastError(f"Uploading needs the contributor role on {cfg['server']}; yours is "
                             "viewer. Ask an admin of the group.")

    todo = []
    for inp in inputs:
        dup = jobs.duplicate_of(inp)
        if dup:
            print(f"{inp['input']}: already added as {dup['id']} ({dup['state']}); skipped.")
            continue
        version_of = args.version_of
        if not version_of and isinstance(me, dict):
            go, version_of = _check_hub(api, inp, me, args.yes)
            if not go:
                continue
        todo.append((inp, version_of))
    announce = _announce(api, args.slack, me, len(todo)) if todo else None
    made = []
    for inp, version_of in todo:
        job = jobs.create(inp, version_of=version_of, model=args.model,
                          yes=args.yes or bool(version_of), announce=announce)
        made.append(job)
        print(f"{job['id']}  queued  {jobs.label(job)}"
              + (f"  (your own version of {version_of})" if version_of else ""))
    if made:
        jobs.ensure_worker()
        print(f"Working on {'it' if len(made) == 1 else 'them'} in the background (at most "
              f"{jobs.MAX_PARALLEL} at once); closing this terminal is fine. "
              "Follow with: papercast status")
    return 1 if problems else 0


# --------------------------------------------------------------------------- status

def _short_input(job: dict, width: int = 60) -> str:
    s = job.get("source_name") or job.get("input") or ""
    s = s.split("://", 1)[-1]
    return s if len(s) <= width else s[:width - 1] + "…"


def cmd_status(args) -> int:
    cfg = config.load()
    server = cfg.get("server") or ""
    everything = jobs.list_jobs()
    shown = everything if args.all else [j for j in everything if jobs.visible(j)]
    hub: dict[str, dict] = {}
    hub_err = None
    need = [j for j in shown if j.get("state") == "done" and j.get("episode_id")]
    if need and cfg.get("token"):
        try:
            for ep in Api.from_config(cfg, retries=0, timeout=10).episodes(mine=True):
                if isinstance(ep, dict) and (ep.get("id") or ep.get("episode_id")):
                    hub[ep.get("id") or ep.get("episode_id")] = ep
        except PapercastError as e:
            hub_err = str(e)
        for j in need:                          # remembered, so old finished jobs can be hidden
            ep = hub.get(j["episode_id"])
            if ep and ep.get("state") and ep["state"] != j.get("hub_state"):
                try:
                    jobs.update(jobs.job_dir(j["id"]), hub_state=ep["state"])
                except PapercastError:
                    pass
                j["hub_state"] = ep["state"]
    until = jobs.pause_until()
    busy = sum(1 for j in everything if j.get("state") == "running")
    wpid = jobs.worker_pid()
    now = time.time()

    if args.json:
        out = {"server": server or None, "user": cfg.get("user"), "worker_pid": wpid,
               "paused_until": util.now_iso(until) if until else None, "hub_error": hub_err,
               "jobs": [{**j, "hub": hub.get(j.get("episode_id") or ""),
                         "status": jobs.state_text(j, ep=hub.get(j.get("episode_id") or ""),
                                                   server=server, until=until, busy=busy,
                                                   now=now)} for j in shown]}
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0

    user = cfg.get("user") or {}
    if cfg.get("token"):
        print(f"{user.get('name') or user.get('email') or 'Logged in'} on {server}")
    else:
        print("Not logged in (papercast login)" + (f" to {server}" if server else ""))
    pending = [j for j in everything if j.get("state") in jobs.PENDING]
    if pending:
        counts = {}
        for j in pending:
            counts[j["state"]] = counts.get(j["state"], 0) + 1
        bits = [f"{n} {st}" for st, n in counts.items()]
        bits.append(f"worker pid {wpid}" if wpid else "worker starting")
        print(" · ".join(bits))
    if until:
        print(jobs.limit_text(until, now))
    if not shown:
        hidden = len(everything) - len(shown)
        print("No jobs." + (f" ({hidden} older: papercast status --all)" if hidden else
                            " Add a paper: papercast add <pdf, web address or arXiv id>"))
        return 0
    print()
    for j in shown:
        head = f"{j['id']}  {jobs.label(j)}"
        if j.get("title"):
            head += f"  ({_short_input(j, 40)})"
        print(head)
        text = jobs.state_text(j, ep=hub.get(j.get("episode_id") or ""), server=server,
                               until=until, busy=busy, now=now)
        print("         " + _indent(text))
    hidden = len(everything) - len(shown)
    if hidden:
        print(f"\n({hidden} older: papercast status --all)")
    if hub_err:
        print(f"\n(The hub's side is unknown: {hub_err})")
    return 0


# --------------------------------------------------------------------------- prefs

def _show_prefs(p: dict) -> None:
    settings = prefs_mod.full(p.get("settings") or {})
    given = p.get("settings") or {}
    for k, choices in prefs_mod.SCHEMA.items():
        mark = "" if k in given else "  (default)"
        print(f"{k:<11} {settings[k]:<10} one of {' | '.join(choices)}{mark}")
    note = (p.get("note") or "").strip()
    print(f"{'note':<11} {note if note else '(none)'}")
    if prefs_mod.summary(settings):
        print(f"\nShown on your episodes as: {prefs_mod.summary(settings)}")


def _show_slack(slack: dict) -> None:
    if slack.get("enabled"):
        on = slack.get("default") is not False
        print(f"{'slack':<11} {'on' if on else 'off':<10} add asks \"Post to "
              f"{slack.get('channel') or 'Slack'} when it's ready?\"; Enter means "
              f"{'yes' if on else 'no'} (--slack on|off)")


def _set_slack(api: Api, cfg: dict, on: bool) -> None:
    """papercast prefs --slack on|off: add's default answer, kept on the hub."""
    try:
        r = api.put("/api/cli/slack", {"default": on})
    except NotFound:
        raise PapercastError(f"{cfg['server']} cannot post to Slack (this hub has no Slack "
                             "yet), so there is no default to set.")
    r = r.get("slack", r) if isinstance(r, dict) else {}
    where = r.get("channel") or "the group's Slack channel"
    print(f"Saved. papercast add asks \"Post to {where} when it's ready? "
          f"{'[Y/n]' if on else '[y/N]'}\"; Enter, or running without a terminal, means "
          f"{'yes' if on else 'no'}.")
    if r.get("enabled") is False:
        print(f"(Slack is not set up on {cfg['server']} yet, so nothing is posted for now.)")


def cmd_prefs(args) -> int:
    cfg = config.require_login()
    api = Api.from_config(cfg, retries=1, timeout=20)
    cur = api.prefs()
    cur = cur if isinstance(cur, dict) else {}
    changes = {k: getattr(args, k) for k in prefs_mod.SCHEMA if getattr(args, k, None)}
    if args.slack is not None:
        _set_slack(api, cfg, args.slack == "on")
    if not changes and args.note is None:
        if args.slack is None:
            _show_prefs(cur)
            _show_slack(_slack_features(api))
            print("\nChange with e.g.: papercast prefs --maths full --note \"skip the history\"")
        return 0
    settings = {**(cur.get("settings") or {}), **changes}
    note = (cur.get("note") or "") if args.note is None else args.note
    problems = prefs_mod.validate(settings, note)
    if problems:
        raise PapercastError("; ".join(problems))
    try:
        new = api.set_prefs(settings, note)
    except NotFound:
        raise PapercastError(f"{cfg['server']} does not take preference changes from the CLI "
                             "yet: set them on the web page (Preferences).")
    except ApiError as e:
        if e.status == 405:
            raise PapercastError(f"{cfg['server']} does not take preference changes from the "
                                 "CLI yet: set them on the web page (Preferences).")
        raise
    if not isinstance(new, dict) or "settings" not in new:
        new = {"settings": settings, "note": note}
    print("Saved. Your next episodes are made with:")
    _show_prefs(new)
    return 0


# --------------------------------------------------------------------------- cancel, retry, worker

def cmd_cancel(args) -> int:
    job = jobs.find(args.job)
    api = None
    try:
        api = Api.from_config(retries=0, timeout=10)
    except PapercastError:
        pass
    print(jobs.cancel(job, api=api))
    return 0


def cmd_retry(args) -> int:
    job = jobs.find(args.job)
    if job.get("state") == "done" and job.get("episode_id"):
        try:                                   # is it really rejected? ask the hub
            ep = Api.from_config(retries=1, timeout=15).episode(job["episode_id"])
            if isinstance(ep, dict) and ep.get("state"):
                jobs.update(jobs.job_dir(job["id"]), hub_state=ep["state"])
                job["hub_state"] = ep["state"]
        except (AuthError, NotFound):
            raise
        except PapercastError:
            pass
    jobs.check_claude(warn=_err)
    new = jobs.retry(job, yes=args.yes)
    jobs.ensure_worker()
    if new["id"] != job["id"]:
        print(f"{job['id']} was rejected; made again as {new['id']} (papercast status).")
    else:
        print(f"{job['id']} queued again (papercast status).")
    return 0


def cmd_worker(args) -> int:
    if args.detach:
        if jobs.ensure_worker():
            print("Started the worker in the background.")
        else:
            pid = jobs.worker_pid()
            print(f"A worker is already running (pid {pid})." if pid else "Nothing to do.")
        return 0
    return jobs.Worker().run()


# --------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="papercast",
        description="Make podcast episodes of papers for your group's papercast. Claude Code "
                    "runs on this computer under your own login; the hub gets the finished "
                    "episode.",
        epilog="Start with: papercast login --server https://<hub>, then papercast add paper.pdf")
    p.add_argument("--version", action="version", version=f"papercast {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("login", help="log this computer in to the hub")
    s.add_argument("--server", metavar="URL", help="the hub's address (remembered)")
    s.add_argument("--device", metavar="NAME", help="this computer's name on the hub's Devices "
                                                    "list (default user@host)")
    s.add_argument("--no-browser", action="store_true", help="do not open the browser")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", help="forget the login here and revoke it on the hub")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("whoami", help="who the hub says you are")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("status", help="how far each job is, and the hub's side")
    s.add_argument("--all", action="store_true", help="old finished jobs too")
    s.add_argument("--json", action="store_true", help="machine-readable")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("add", help="make episodes of papers (PDF, web address, arXiv id, DOI)")
    s.add_argument("inputs", nargs="+", metavar="PDF-OR-URL")
    s.add_argument("--version-of", metavar="PAPER_ID",
                   help="your own version of a paper already on the hub (one input)")
    s.add_argument("--model", metavar="M", help="the Claude model for the episode "
                                                "(default: the pipeline's)")
    s.add_argument("--yes", "-y", action="store_true",
                   help="make your own version when the paper is on the hub already")
    s.add_argument("--slack", action=argparse.BooleanOptionalAction, default=None,
                   help="post (or not) to the group's Slack channel when it is ready, without "
                        "asking (default: ask; without a terminal, your papercast prefs --slack)")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("prefs", help="show or change your listening preferences")
    s.add_argument("--maths", choices=prefs_mod.SCHEMA["maths"])
    s.add_argument("--emphasis", choices=prefs_mod.SCHEMA["emphasis"])
    s.add_argument("--background", choices=prefs_mod.SCHEMA["background"])
    s.add_argument("--note", metavar="TEXT",
                   help=f"a note for the writer, at most {prefs_mod.NOTE_MAX} characters "
                        "(\"\" clears it)")
    s.add_argument("--slack", choices=("on", "off"),
                   help="add's default answer to \"Post to the Slack channel when it's ready?\"")
    s.set_defaults(func=cmd_prefs)

    s = sub.add_parser("cancel", help="stop a job for good")
    s.add_argument("job", help="its id (or the start of it), from papercast status")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("retry", help="run a failed, cancelled or waiting job again")
    s.add_argument("job")
    s.add_argument("--yes", "-y", action="store_true",
                   help="answer yes to the job's question (make your own version)")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("worker", help="run waiting jobs now (for login items; exits when done)")
    s.add_argument("--detach", action="store_true", help="in the background")
    s.set_defaults(func=cmd_worker)
    return p


# Commands after which interrupted jobs are resumed (the others touch the queue themselves or
# are about the login).
RESUME_AFTER = ("whoami", "status", "prefs", "cancel", "retry")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_job"] and len(argv) == 2:
        return jobs.run_one(argv[1])             # a job's own process (started by the worker)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        if args.cmd == "status":
            if jobs.ensure_worker():
                print("(Resuming unfinished jobs in the background.)", file=sys.stderr)
        rc = args.func(args) or 0
        if args.cmd in RESUME_AFTER and args.cmd != "status":
            jobs.ensure_worker()
        return rc
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except PapercastError as e:
        _err(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
