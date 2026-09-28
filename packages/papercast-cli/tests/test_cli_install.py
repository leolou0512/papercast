"""The package installs with pip and its `papercast` entry point runs (as `pipx install .` and
`pip install .` will on a group member's laptop)."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("pip"), "pip is not installed for this Python")
class InstallTest(unittest.TestCase):
    def test_pip_install_target_then_run(self):
        tmp = Path(tempfile.mkdtemp(prefix="pcg-install-"))
        try:
            # Built from a copy, so setuptools' build/ and egg-info never land in the repo.
            src = tmp / "src"
            shutil.copytree(PKG, src, ignore=shutil.ignore_patterns(
                "tests", "__pycache__", "build", "*.egg-info", ".git"))
            site = tmp / "site"
            cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                   "--no-cache-dir", "--disable-pip-version-check", "--target", str(site)]
            # The build needs setuptools; use the one here when present (no network needed).
            if importlib.util.find_spec("setuptools"):
                cmd.append("--no-build-isolation")
            r = subprocess.run(cmd + [str(src)], capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            exe = site / "bin" / "papercast"
            self.assertTrue(exe.exists(), list(site.iterdir()))
            env = {**os.environ, "PYTHONPATH": str(site), "HOME": str(tmp),
                   "XDG_CONFIG_HOME": str(tmp / "cfg"), "XDG_STATE_HOME": str(tmp / "state")}
            r = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                               env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "papercast 0.1.0")
            r = subprocess.run([str(exe), "status"], capture_output=True, text=True, env=env,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Not logged in", r.stdout)

            # Package data and every subpackage are in.
            self.assertTrue((site / "papercast_cli" / "common" / "wording.json").exists())
            self.assertTrue((site / "papercast_cli" / "pipeline" / "__init__.py").exists())
            meta = next(site.glob("papercast-0.1.0.dist-info")) / "METADATA"
            text = meta.read_text()
            self.assertNotIn("Requires-Dist", text)          # stdlib only
            self.assertIn("Requires-Python: >=3.10", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
