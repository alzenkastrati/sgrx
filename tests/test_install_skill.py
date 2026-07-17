from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sgrx"
SCRIPT = SKILL / "scripts" / "install_skill.py"
SPEC = importlib.util.spec_from_file_location("sgrx_skill_installer", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class MultiAgentInstallerTests(unittest.TestCase):
    def test_default_targets_cover_supported_agent_families(self):
        self.assertEqual(set(installer.TARGETS), {"shared", "codex", "claude", "cline"})

    def test_installs_the_complete_skill_into_every_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            results = installer.install_skill(SKILL, home, installer.TARGETS)

            self.assertTrue(all(status == "installed" for _, _, status in results))
            for relative in installer.TARGETS.values():
                destination = home / relative
                self.assertTrue((destination / "SKILL.md").is_file())
                self.assertTrue((destination / "scripts" / "sgrx.py").is_file())
                self.assertTrue((destination / "scripts" / "sgrx_audit.py").is_file())
                self.assertTrue((destination / "references" / "tool-routing.md").is_file())
                self.assertTrue((destination / "agents" / "openai.yaml").is_file())
                self.assertFalse((destination / "scripts" / "__pycache__").exists())

    def test_dry_run_and_target_selection_do_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            results = installer.install_skill(SKILL, home, ["codex"], dry_run=True)

            self.assertEqual(results[0][2], "planned")
            self.assertFalse((home / installer.TARGETS["codex"]).exists())


if __name__ == "__main__":
    unittest.main()
