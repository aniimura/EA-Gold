# -*- coding: utf-8 -*-
"""Run the whole v2 test suite and print a compact result table.

    python tests/run_all.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULES = [
    ("core mechanics    ", "tests.test_core_mechanics"),
    ("execution / stops ", "tests.test_execution"),
    ("financing         ", "tests.test_financing"),
    ("campaigns / stats ", "tests.test_analysis"),
    ("pine reconciliation", "tests.test_reconcile"),
]


def main() -> int:
    loader = unittest.TestLoader()
    total = fails = errors = 0
    rows = []
    t0 = time.time()
    for label, mod in MODULES:
        suite = loader.loadTestsFromName(mod)
        buf = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
        r = buf.run(suite)
        total += r.testsRun
        fails += len(r.failures)
        errors += len(r.errors)
        rows.append((label, r.testsRun, len(r.failures), len(r.errors)))
        for kind, lst in (("FAIL", r.failures), ("ERROR", r.errors)):
            for case, tb in lst:
                print("%s: %s\n%s" % (kind, case, tb))
    print("=" * 64)
    print("%-20s %6s %8s %8s" % ("module", "tests", "failures", "errors"))
    print("-" * 64)
    for label, n, f, e in rows:
        print("%-20s %6d %8d %8d" % (label, n, f, e))
    print("-" * 64)
    print("%-20s %6d %8d %8d   (%.1fs)" % ("TOTAL", total, fails, errors,
                                           time.time() - t0))
    print("=" * 64)
    return 0 if (fails == 0 and errors == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
