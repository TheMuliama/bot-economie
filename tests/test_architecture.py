"""Architecture boundary tests for the Phase 3.7 refactor."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PackageImportTests(unittest.TestCase):
    def test_dashboard_import_does_not_load_red_cog(self) -> None:
        code = """
import sys
import mcorp.dashboard.app

assert "mcorp.mcorp" not in sys.modules
assert "redbot" not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
