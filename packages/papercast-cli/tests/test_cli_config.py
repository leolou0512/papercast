"""config.py: where things live, and the token file's permissions."""
import os
import stat
import unittest
from pathlib import Path

from cli_testlib import CliTestCase

from papercast_cli import config
from papercast_cli.errors import NotLoggedIn, PapercastError


class ConfigTest(CliTestCase):
    hub_needed = False

    def test_xdg_dirs_are_honoured(self):
        self.assertEqual(config.config_path(), self.tmp / "config" / "papercast" / "config.json")
        self.assertEqual(config.jobs_dir(), self.tmp / "state" / "papercast" / "jobs")

    def test_defaults_without_xdg_and_relative_xdg_ignored(self):
        os.environ.pop("XDG_CONFIG_HOME")
        os.environ["XDG_STATE_HOME"] = "relative/state"      # invalid per the XDG spec
        self.assertEqual(config.config_dir(), Path(self.home) / ".config" / "papercast")
        self.assertEqual(config.state_dir(), Path(self.home) / ".local" / "state" / "papercast")

    def test_parallel_setting(self):
        self.assertEqual(config.parallel(), config.PARALLEL_DEFAULT)
        config.update(parallel=80)
        self.assertEqual(config.parallel(), 80)
        for bad, got in ((0, 1), (10_000, config.PARALLEL_MAX), ("x", config.PARALLEL_DEFAULT)):
            config.update(parallel=bad)
            self.assertEqual(config.parallel(), got, bad)
        r = self.run_cli("config", "--parallel", "0")
        self.assertNotEqual(r.returncode, 0)
        r = self.run_cli("config", "--parallel", "80")
        self.assertEqual((r.returncode, r.stdout.strip().split()[:2]), (0, ["parallel", "80"]))
        self.assertEqual(config.load()["parallel"], 80)

    def test_saved_with_mode_600_in_a_700_dir(self):
        config.save({"server": "https://hub.example.org", "token": "pcg_x", "device": "d"})
        p = config.config_path()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        self.assertEqual(config.load()["token"], "pcg_x")
        config.update(token=None)
        self.assertNotIn("token", config.load())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_a_readable_config_is_made_private_again(self):
        config.save({"server": "https://h", "token": "pcg_x"})
        p = config.config_path()
        os.chmod(p, 0o644)
        config.load()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_broken_config_says_what_to_do(self):
        config.private_dir(config.config_dir())
        config.config_path().write_text("{not json")
        with self.assertRaises(PapercastError) as cm:
            config.load()
        self.assertIn("papercast login", str(cm.exception))

    def test_require_login(self):
        with self.assertRaises(NotLoggedIn) as cm:
            config.require_login()
        self.assertIn("papercast login --server", str(cm.exception))
        config.save({"server": "https://h"})
        with self.assertRaises(NotLoggedIn) as cm:
            config.require_login()
        self.assertIn("Run: papercast login", str(cm.exception))

    def test_default_device_names_user_and_host(self):
        self.assertRegex(config.default_device(), r"^[^@\s]+@[^@\s.]+$")


if __name__ == "__main__":
    unittest.main()
