from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "sgrx" / "scripts" / "sgrx.py"


@unittest.skipUnless(os.environ.get("SGRX_RUN_INTEGRATION") == "1", "set SGRX_RUN_INTEGRATION=1 to run local CLI smoke tests")
class LocalIntegrationSmokeTests(unittest.TestCase):
    def test_isolated_fixture_index(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "consumer"
            project.mkdir()
            command = [
                sys.executable,
                str(SCRIPT),
                "index",
                "--json",
                "--registry",
                "npm",
                "--package",
                "fixture-package@1.0.0",
                "--project",
                str(project),
                "--source-path",
                str(ROOT / "tests" / "fixtures" / "dependency"),
                "--version",
                "1.0.0",
                "--timeout",
                "180",
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=240, check=False, shell=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('"source_unchanged": true', completed.stdout)
            self.assertFalse((ROOT / "tests" / "fixtures" / "dependency" / ".gitnexus").exists())


if __name__ == "__main__":
    unittest.main()
