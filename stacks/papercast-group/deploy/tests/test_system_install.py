"""The system install (perov since 2026-09-28: the account papercast, /srv/papercast, units
papercast-*), without sudo: install.sh --render-units writes the units, which are checked for
their paths and settings (and by systemd-analyze verify when it is there); papercastctl and
cloudflare.sh run in system mode against a stand-in /srv/papercast in a temporary directory, as
the test's own user (PCG_NO_SUDO=1), with fakes of systemctl, journalctl and curl on PATH.
Run:
    cd stacks/papercast-group && python3 -m unittest discover -s deploy/tests"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
GROUP = DEPLOY.parent                                   # stacks/papercast-group
CLI = GROUP.parents[1] / "packages" / "papercast-cli"
H = "/srv/papercast"
LONG = ("papercast-hub.service", "papercast-voice.service", "papercast-tunnel.service")
ONESHOT = ("papercast-backup.service", "papercast-layout.service")
TIMERS = {"papercast-backup.timer": "*-*-* 03:30:00", "papercast-layout.timer": "*-*-* 04:15:00"}


def parse_unit(text: str) -> dict:
    """{section: {key: [values]}} (keys repeat: DeviceAllow, ReadWritePaths)."""
    out: dict = {}
    sec = None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("[") and ln.endswith("]"):
            sec = out.setdefault(ln[1:-1], {})
            continue
        k, _, v = ln.partition("=")
        sec.setdefault(k, []).append(v)
    return out


def render(home: str | None = None) -> tuple[Path, dict]:
    tmp = Path(tempfile.mkdtemp(prefix="pcg-units-"))
    env = {**os.environ}
    if home:
        env["PCG_SYS_HOME"] = home
    else:
        env.pop("PCG_SYS_HOME", None)
    p = subprocess.run(["bash", str(DEPLOY / "install.sh"), "--render-units", str(tmp)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return tmp, {f.name: f.read_text() for f in sorted(tmp.iterdir())}


class RenderedUnits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir, cls.text = render()
        cls.units = {n: parse_unit(t) for n, t in cls.text.items()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def svc(self, name: str) -> dict:
        return self.units[name]["Service"]

    def one(self, name: str, key: str) -> str:
        vals = self.svc(name).get(key)
        self.assertIsNotNone(vals, f"{name} has no {key}")
        self.assertEqual(len(vals), 1, f"{name}: {key} more than once")
        return vals[0]

    def test_the_seven_units_and_nothing_left_to_fill_in(self):
        self.assertEqual(sorted(self.text), sorted([*LONG, *ONESHOT, *TIMERS]))
        for name, t in self.text.items():
            self.assertNotIn("@H@", t, name)
            self.assertNotIn("@USER@", t, name)
            self.assertNotIn("/home", t, name)                   # nothing of anyone's home
            self.assertNotIn("%h", t, name)
            self.assertNotIn("--user", t, name)

    def test_all_services_run_as_papercast_niced_and_sandboxed(self):
        for name in (*LONG, *ONESHOT):
            s = self.svc(name)
            self.assertEqual(self.one(name, "User"), "papercast", name)
            self.assertEqual(self.one(name, "Group"), "papercast", name)
            self.assertGreaterEqual(int(self.one(name, "Nice")), 10, name)
            for k, v in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"), ("ProtectHome", "yes"),
                         ("PrivateTmp", "yes"), ("ReadWritePaths", H), ("CapabilityBoundingSet", ""),
                         ("RestrictSUIDSGID", "yes"), ("ProtectKernelModules", "yes")):
                self.assertEqual(s.get(k), [v], f"{name}: {k}")
            self.assertIn(self.one(name, "UMask"), ("0027", "0077"), name)
            for f in s.get("EnvironmentFile", []):
                self.assertTrue(f.startswith(f"{H}/etc/"), f)

    def test_the_long_running_ones_restart_and_start_at_boot(self):
        for name in LONG:
            self.assertEqual(self.one(name, "Restart"), "always", name)
            self.assertEqual(self.one(name, "Type"), "simple", name)
            self.assertEqual(self.units[name]["Install"]["WantedBy"], ["multi-user.target"], name)
            self.assertEqual(self.units[name]["Unit"].get("StartLimitIntervalSec"), ["0"], name)
        for name in ONESHOT:
            self.assertEqual(self.one(name, "Type"), "oneshot", name)
            self.assertNotIn("Install", self.units[name])        # started by their timers only

    def test_hub(self):
        n = "papercast-hub.service"
        self.assertEqual(self.one(n, "ExecStart"), f"{H}/venv/bin/python {H}/app/current/papercast-group/deploy/run_hub.py")
        self.assertEqual(self.one(n, "EnvironmentFile"), f"{H}/etc/hub.env")
        self.assertEqual(self.one(n, "WorkingDirectory"), f"{H}/app/current/papercast-group")
        self.assertEqual(self.one(n, "PrivateDevices"), "yes")
        self.assertEqual(self.one(n, "MemoryMax"), "4G")

    def test_voice_gets_the_gpu_and_a_writable_home(self):
        n = "papercast-voice.service"
        s = self.svc(n)
        self.assertEqual(self.one(n, "ExecStart"), f"{H}/venv/bin/python {H}/app/current/papercast-group/deploy/voice_worker.py")
        self.assertEqual(self.one(n, "EnvironmentFile"), f"{H}/etc/worker.env")
        self.assertNotIn("PrivateDevices", s)                   # it would hide /dev/nvidia*
        self.assertEqual(self.one(n, "DevicePolicy"), "closed")
        self.assertEqual(s["DeviceAllow"], ["char-nvidia rw", "char-nvidia-uvm rw", "char-nvidia-caps r"])
        env = " ".join(s["Environment"])
        self.assertIn(f"HOME={H}/cache", env)
        self.assertIn(f"HF_HOME={H}/cache/huggingface", env)
        self.assertEqual(self.one(n, "KillMode"), "control-group")
        self.assertEqual(self.one(n, "MemoryMax"), "24G")
        self.assertIn("papercast-hub.service", self.units[n]["Unit"]["After"][0])

    def test_tunnel_reads_its_token_from_a_file(self):
        n = "papercast-tunnel.service"
        self.assertEqual(self.one(n, "ExecStart"),
                         f"{H}/bin/cloudflared tunnel --no-autoupdate run --token-file {H}/etc/tunnel.token")
        self.assertEqual(self.one(n, "PrivateDevices"), "yes")
        self.assertIn("network-online.target", self.units[n]["Unit"]["Wants"][0])

    def test_timers(self):
        for name, when in TIMERS.items():
            t = self.units[name]
            self.assertEqual(t["Timer"]["OnCalendar"], [when])
            self.assertEqual(t["Timer"]["Persistent"], ["true"])
            self.assertEqual(t["Install"]["WantedBy"], ["timers.target"])
        self.assertIn(f"tools/backup.py --data \"$$PCG_DATA\" --dest \"$$PCG_BACKUP_DIR\"",
                      self.one("papercast-backup.service", "ExecStart"))
        self.assertEqual(self.svc("papercast-backup.service")["Environment"], [f"PCG_BACKUP_DIR={H}/backups"])

    def test_what_the_units_run_is_in_the_release(self):
        # /srv/papercast/app/current/papercast-group/X is this checkout's stacks/papercast-group/X
        pre = f"{H}/app/current/papercast-group/"
        for name in (*LONG, *ONESHOT):
            for word in " ".join(self.svc(name)["ExecStart"]).replace("'", " ").split():
                if word.startswith(pre):
                    self.assertTrue((GROUP / word[len(pre):]).is_file(), word)
        for rel in ("tools/backup.py", "hub/layout.py", "deploy/papercastctl", "HANDOVER.md"):
            self.assertTrue((GROUP / rel).is_file(), rel)

    def test_another_home(self):
        d, text = render("/opt/pcg")
        try:
            hub = parse_unit(text["papercast-hub.service"])["Service"]
            self.assertEqual(hub["ReadWritePaths"], ["/opt/pcg"])
            self.assertEqual(hub["EnvironmentFile"], ["/opt/pcg/etc/hub.env"])
            settings = [ln for t in text.values() for ln in t.splitlines() if not ln.startswith("#")]
            self.assertNotIn(H, "\n".join(settings))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "no systemd-analyze")
    def test_systemd_analyze_verify(self):
        # the units for a home in a temporary directory, with stand-ins for what they run
        tmp = Path(tempfile.mkdtemp(prefix="pcg-verify-"))
        try:
            h = tmp / "h"
            (h / "venv" / "bin").mkdir(parents=True)
            (h / "venv" / "bin" / "python").symlink_to(sys.executable)
            (h / "bin").mkdir()
            shutil.copy("/bin/true", h / "bin" / "cloudflared")
            (h / "app").mkdir()
            (h / "app" / "current").symlink_to(GROUP.parent)       # app/current/papercast-group
            d, _ = render(str(h))
            p = subprocess.run(["systemd-analyze", "verify", *[str(f) for f in sorted(d.iterdir())]],
                               capture_output=True, text=True, timeout=60)
            ours = [ln for ln in (p.stdout + p.stderr).splitlines() if "papercast-" in ln]
            self.assertEqual(ours, [], "\n".join(ours))
            shutil.rmtree(d, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
case "$*" in
  *InvocationID*) echo 0123456789abcdef ;;
  is-active*) echo active ;;
  is-enabled*) echo enabled ;;
esac
exit 0
"""
JOURNALCTL = """#!/bin/sh
echo "journalctl $*" >> "$FAKE_LOG"
case "$*" in *_SYSTEMD_INVOCATION_ID*) [ -n "$FAKE_NO_TUNNEL" ] || echo "INF Registered tunnel connection connIndex=0" ;; esac
exit 0
"""
CURL = """#!/bin/sh
for a; do url=$a; done
echo "curl $url" >> "$FAKE_LOG"
case "$url" in */signin) printf 200 ;; *) printf "${FAKE_ROOT_CODE:-303}" ;; esac
"""


class SystemModeScripts(unittest.TestCase):
    """papercastctl and cloudflare.sh with PCG_MODE=system, PCG_NO_SUDO=1 and a stand-in home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcg-sys-test-")
        t = Path(self.tmp.name)
        self.h = t / "srv-papercast"
        rel = self.h / "app" / "releases" / "r1"
        rel.mkdir(parents=True)
        (rel / "papercast-group").symlink_to(GROUP)
        (rel / "papercast-cli").symlink_to(CLI)
        (rel / "VERSION").write_text("test (installed now)\n")
        (self.h / "app" / "current").symlink_to("releases/r1")
        (self.h / "venv" / "bin").mkdir(parents=True)
        (self.h / "venv" / "bin" / "python").symlink_to(sys.executable)
        for d in ("etc", "data", "backups", "worker", "units"):
            (self.h / d).mkdir()
        self.env_file = self.h / "etc" / "hub.env"
        self.env_file.write_text(f"PCG_DATA={self.h / 'data'}\nPCG_SECRET=0123456789abcdef0123456789\nPCG_AUTH=local\n"
                                 "PCG_BIND=127.0.0.1\nPCG_PORT=8400\nPCG_PUBLIC_URL=https://papercast.example.org\nPCG_ADMIN_EMAILS=\n")
        self.env_file.chmod(0o600)
        self.bin = t / "bin"
        self.bin.mkdir()
        for name, body in (("systemctl", SYSTEMCTL), ("journalctl", JOURNALCTL), ("curl", CURL)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.log = t / "calls.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_sh(self, script, *args, stdin=None, **extra):
        env = {**os.environ, "PCG_MODE": "system", "PCG_NO_SUDO": "1", "PCG_SYS_HOME": str(self.h),
               "PCG_SYS_UNITS": str(self.h / "units"), "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE_LOG": str(self.log), "HOME": self.tmp.name, "PCG_LIB": str(DEPLOY / "lib.sh"),
               "PYTHONPATH": str(GROUP) + os.pathsep + str(CLI), **extra}
        return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True,
                              timeout=120, input=stdin)

    def ctl(self, *args, **kw):
        return self.run_sh(DEPLOY / "papercastctl", *args, **kw)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def env(self) -> dict:
        return dict(ln.split("=", 1) for ln in self.env_file.read_text().splitlines() if "=" in ln)

    def test_allow_add_list_remove_as_the_hub(self):
        p = self.ctl("allow", "add", "zz123@ic.ac.uk", "--note", "test")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("added zz123@ic.ac.uk: username zz123", p.stdout)
        imp = Path(self.tmp.name) / "list.txt"
        imp.write_text("# the group\nab1@ic.ac.uk\ncd2@imperial.ac.uk\n")
        p = self.ctl("allow", "import", str(imp))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.ctl("allow", "list")
        self.assertEqual(p.returncode, 0, p.stderr)
        for e in ("zz123@ic.ac.uk", "ab1@ic.ac.uk", "cd2@imperial.ac.uk"):
            self.assertIn(e, p.stdout)
        p = self.ctl("allow", "remove", "zz123@ic.ac.uk")
        self.assertEqual(p.returncode, 0, p.stderr)
        c = sqlite3.connect(str(self.h / "data" / "hub.db"))
        try:
            self.assertEqual(sorted(r[0] for r in c.execute("SELECT email FROM allowed_emails")),
                             ["ab1@ic.ac.uk", "cd2@imperial.ac.uk"])
        finally:
            c.close()

    def test_tunnel_token_replaced_quietly_and_the_tunnel_restarted(self):
        secret = "eyJhIjoiZmFrZS10b2tlbi1mb3ItdGhlLXRlc3QifQ"
        p = self.ctl("tunnel-token", "-", stdin=secret + "\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tok = self.h / "etc" / "tunnel.token"
        self.assertEqual(tok.read_text().strip(), secret)
        self.assertEqual(oct(tok.stat().st_mode & 0o777), "0o600")
        self.assertNotIn(secret, p.stdout + p.stderr)
        self.assertIn("systemctl restart papercast-tunnel", self.calls())
        self.assertIn("1 connection(s)", p.stdout)
        # a token that does not register: the old one stays aside, the command fails
        p = self.ctl("tunnel-token", "-", stdin="eyJiIjoic2Vjb25kLWZha2UtdG9rZW4tZm9yLXRlc3QifQ\n", FAKE_NO_TUNNEL="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((self.h / "etc" / "tunnel.token.before").read_text().strip(), secret)
        self.assertNotIn("second-fake", p.stdout + p.stderr)
        self.assertNotEqual(self.ctl("tunnel-token", "-", stdin="\n").returncode, 0)   # empty

    def test_cloudflare_url_and_status_use_the_system_units(self):
        p = self.run_sh(DEPLOY / "cloudflare.sh", "url", "--url", "https://papercast.virtualatoms.org")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.env()["PCG_PUBLIC_URL"], "https://papercast.virtualatoms.org")
        self.assertIn("systemctl restart papercast-hub.service", self.calls())
        self.assertNotIn("--user", self.calls())
        self.assertIn("curl http://127.0.0.1:8400/", self.calls())
        p = self.run_sh(DEPLOY / "cloudflare.sh", "status")
        self.assertIn("papercast-hub     active", p.stdout)
        p = self.ctl("auth", "password", "--admin", "zz123@ic.ac.uk")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.env()["PCG_AUTH"], "password")
        self.assertIn("zz123@ic.ac.uk is an admin", p.stdout)

    def test_email_takes_the_password_on_stdin_into_etc(self):
        p = self.ctl("email", "--host", "smtp.gmail.com", "--port", "587", "--user", "pc@gmail.com",
                     "--from", "pc@gmail.com", "--password-file", "-", stdin="wxqz vyuk tsrp onml\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pw = self.h / "etc" / "smtp.password"
        self.assertEqual(oct(pw.stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.env()["PCG_SMTP_PASSWORD_FILE"], str(pw))
        self.assertNotIn("wxqz", self.env_file.read_text())
        self.assertEqual(pw.read_text().strip(), "wxqz vyuk tsrp onml")

    def test_backup_then_restore(self):
        self.assertEqual(self.ctl("allow", "add", "ab1@ic.ac.uk").returncode, 0)
        ep = self.h / "data" / "episodes" / "e_1"
        ep.mkdir(parents=True)
        (ep / "episode.mp3").write_bytes(b"ID3 first")
        p = subprocess.run([sys.executable, str(GROUP / "tools" / "backup.py"), "--data", str(self.h / "data"),
                            "--dest", str(self.h / "backups")], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        name = next(d.name for d in (self.h / "backups").iterdir() if d.name.startswith("2"))
        self.assertEqual(self.ctl("allow", "add", "cd2@ic.ac.uk").returncode, 0)     # after the backup
        (ep / "episode.mp3").write_bytes(b"ID3 changed")
        self.assertNotEqual(self.ctl("restore", "no-such-backup", "--yes").returncode, 0)
        p = self.ctl("restore", name, "--yes")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((ep / "episode.mp3").read_bytes(), b"ID3 first")
        c = sqlite3.connect(str(self.h / "data" / "hub.db"))
        try:
            self.assertEqual([r[0] for r in c.execute("SELECT email FROM allowed_emails")], ["ab1@ic.ac.uk"])
        finally:
            c.close()
        kept = [d for d in self.h.iterdir() if d.name.startswith("data.before-restore-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / "episodes" / "e_1" / "episode.mp3").read_bytes(), b"ID3 changed")
        calls = self.calls()
        self.assertLess(calls.index("systemctl stop papercast-voice papercast-hub"),
                        calls.index("systemctl start papercast-hub papercast-voice"))

    def test_rollback_and_unknown_commands(self):
        p = self.ctl("rollback")
        self.assertNotEqual(p.returncode, 0)                     # no release before
        (self.h / "app" / "releases" / "r0").mkdir()
        (self.h / "app" / "previous").symlink_to("releases/r0")
        p = self.ctl("rollback")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(os.readlink(self.h / "app" / "current"), "releases/r0")
        self.assertEqual(os.readlink(self.h / "app" / "previous"), "releases/r1")
        self.assertNotEqual(self.ctl("frobnicate").returncode, 0)
        self.assertNotEqual(self.ctl("logs", "nosuchunit").returncode, 0)


if __name__ == "__main__":
    unittest.main()
