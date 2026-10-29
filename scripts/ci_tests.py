#!/usr/bin/env python3
"""Run all unittest suites; incomplete test execution fails CI."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.testsRun == 0 or result.skipped or result.expectedFailures:
    print("CI requires a nonempty test suite with zero skips and zero expected failures.", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0 if result.wasSuccessful() else 1)
