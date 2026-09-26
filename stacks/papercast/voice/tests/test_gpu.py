"""The wait-for-free-GPU rule and the give-it-back rule, against a fake nvidia-smi."""
import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import FAKE_NVSMI
from papercast_voice import gpu

# Captured on stibnite 2026-09-26 (`nvidia-smi pmon -c 1 -s um`, driver 535.288.01) while a
# test matmul ran: the parser must read this exact shape, including graphics rows and "-".
REAL_PMON = """# gpu         pid  type    sm    mem    enc    dec     fb   ccpm    command
# Idx           #   C/G     %      %      %      %     MB     MB    name
    0     155367     G      -      -      -      -     34      0    firefox
    0    1360690     C     99     30      -      -    382      0    python
    0    3160582     G      -      -      -      -    106      0    Xorg
    0    3444723     C      -      -      -      -    158      0    python
"""


class FakeSmi:
    def __init__(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.td.name, "sc.json")
        os.environ["FAKE_NVSMI"] = self.path

    def set(self, **sc):
        with open(self.path, "w") as fh:
            json.dump(sc, fh)

    def read(self, **kw):
        return gpu.read(FAKE_NVSMI, 0, 10, **kw)


class Reading(unittest.TestCase):
    def setUp(self):
        self.smi = FakeSmi()
        self.addCleanup(self.smi.td.cleanup)

    def test_parses_memory_util_apps_and_pmon(self):
        self.smi.set(free=11200, total=16376, util=3, apps=[[42, 158], [77, 9000]],
                     pmon=[[42, "C", None], [77, "C", 85], [9, "G", 50]])
        r = self.smi.read(with_pmon=True)
        self.assertTrue(r.ok, r.error)
        self.assertEqual((r.free_mib, r.total_mib, r.util_pct), (11200, 16376, 3))
        self.assertEqual(r.apps, [(42, 158), (77, 9000)])
        self.assertEqual(r.sm, {42: 0, 77: 85})       # graphics-only rows ignored

    def test_real_pmon_shape(self):
        def canned(argv, _t):
            if "pmon" in argv:
                return REAL_PMON
            if any("query-gpu" in a for a in argv):
                return "15325, 16376, 100\n"
            return "3444723, 158\n1360690, 382\n"
        with mock.patch.object(gpu, "_run", canned):
            r = gpu.read("nvidia-smi", 0, 10, with_pmon=True)
        self.assertEqual(r.sm, {1360690: 99, 3444723: 0})
        self.assertEqual(r.util_pct, 100)

    def test_failure_is_not_free(self):
        self.smi.set(fail=True)
        r = self.smi.read()
        self.assertFalse(r.ok)
        ok, code, _ = gpu.Admission(1000, 20, 1).step(r)
        self.assertEqual((ok, code), (False, "nvidia_smi"))

    def test_missing_binary_is_not_free(self):
        r = gpu.read("/nonexistent/nvidia-smi", 0, 5)
        self.assertFalse(r.ok)

    def test_na_memory_is_not_free(self):
        self.smi.set(free=None)
        self.assertFalse(self.smi.read().ok)


class AdmissionRule(unittest.TestCase):
    def r(self, free, util=0, ok=True):
        return gpu.Reading(ok=ok, free_mib=free, total_mib=16376, util_pct=util)

    def test_needs_memory_idle_and_stable(self):
        a = gpu.Admission(9024, 20, 3)
        seq = [(8000, 0, "memory"), (12000, 0, "settling"), (12000, 0, "settling"),
               (12000, 0, "admitted")]
        for free, util, code in seq:
            ok, got, text = a.step(self.r(free, util))
            self.assertEqual(got, code, text)
        self.assertTrue(ok)

    def test_one_bad_poll_restarts_the_count(self):
        a = gpu.Admission(9024, 20, 3)
        for free, util in [(12000, 0), (12000, 0), (12000, 90), (12000, 0), (12000, 0)]:
            ok, code, _ = a.step(self.r(free, util))
            self.assertFalse(ok)
        ok, code, _ = a.step(self.r(12000, 0))
        self.assertTrue(ok)

    def test_busy_card_waits_even_with_memory(self):
        a = gpu.Admission(9024, 20, 1)
        ok, code, text = a.step(self.r(15000, 60))
        self.assertEqual((ok, code), (False, "busy"))
        self.assertIn("slow", text)

    def test_page_sentence_has_the_numbers(self):
        _, _, text = gpu.Admission(7800, 20, 1).step(self.r(3174))
        self.assertEqual(text, "waiting for GPU: 3.1 GB free, needs 7.6 GB")


class YieldRule(unittest.TestCase):
    def r(self, sm, free=6000, ok=True):
        return gpu.Reading(ok=ok, free_mib=free, total_mib=16376, util_pct=90, sm=sm)

    def test_own_processes_never_count(self):
        y = gpu.YieldRule(20, 2, 256)
        for _ in range(5):
            self.assertFalse(y.step(self.r({100: 99, 101: 80}), ours={100, 101})[0])

    def test_someone_elses_job_after_n_checks(self):
        y = gpu.YieldRule(20, 2, 256)
        self.assertFalse(y.step(self.r({100: 99, 555: 70}), ours={100})[0])
        go, why = y.step(self.r({100: 99, 555: 70}), ours={100})
        self.assertTrue(go)
        self.assertIn("555", why)

    def test_idle_neighbour_is_fine_and_resets(self):
        y = gpu.YieldRule(20, 2, 256)
        y.step(self.r({555: 70}), ours=set())
        self.assertFalse(y.step(self.r({555: 0}), ours=set())[0])
        self.assertFalse(y.step(self.r({555: 70}), ours=set())[0])

    def test_memory_floor(self):
        y = gpu.YieldRule(20, 1, 256)
        self.assertTrue(y.step(self.r({}, free=100), ours=set())[0])

    def test_unreadable_while_speaking_keeps_going(self):
        y = gpu.YieldRule(20, 1, 256)
        self.assertFalse(y.step(gpu.Reading(ok=False, error="x"), ours=set())[0])


if __name__ == "__main__":
    unittest.main()
