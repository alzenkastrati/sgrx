from __future__ import annotations

import os
import json
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
        scratch = ROOT / ".sgrx"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
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
            payload = json.loads(completed.stdout)
            health = payload["indexing"]["health"]
            self.assertNotEqual(health["gitnexus_status_state"], "STALE", json.dumps(health, indent=2))
            self.assertTrue(health["gitnexus_status_ok"], json.dumps(health, indent=2))
            self.assertFalse((ROOT / "tests" / "fixtures" / "dependency" / ".gitnexus").exists())

    def test_research_mode_builds_paper_repo_graphs_and_plan(self):
        scratch = ROOT / ".sgrx"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps({
                "question": "How should we build a streaming validation system?",
                "requirements": ["Bound latency", "Preserve deterministic errors"],
                "papers": [{
                    "id": "fixture-paper",
                    "title": "Streaming Validation Systems",
                    "year": 2026,
                    "source_path": str(ROOT / "tests" / "fixtures" / "research" / "paper.md"),
                    "relevance": 1.0,
                    "official_repository": True,
                    "estimated_tokens": 1000,
                }],
                "repositories": [{
                    "spec": "owner/fixture-repository@v1.0.0",
                    "source_path": str(ROOT / "tests" / "fixtures" / "dependency"),
                    "paper_ids": ["fixture-paper"],
                    "official": True,
                    "license": "MIT",
                    "relevance": 1.0,
                    "architecture_fit": 1.0,
                    "reproducibility": 1.0,
                    "activity": 1.0,
                    "estimated_tokens": 3000,
                    "focus_terms": ["parse", "email"],
                    "evidence_status": "EXTRACTED"
                }]
            }), encoding="utf-8")
            command = [
                sys.executable, str(SCRIPT), "research", "--json",
                "--project", str(project), "--candidates", str(candidates),
                "--max-papers", "1", "--max-repositories", "1",
                "--token-budget", "10000", "--timeout", "180",
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False, shell=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(Path(payload["build_plan"]).is_file())
            paper_indexing = payload["papers"][0]["indexing"]
            paper_graph_exists = Path(paper_indexing["graph_path"]).is_file()
            if paper_graph_exists:
                self.assertIn(paper_indexing["status"], {"HEALTHY", "PARTIAL"})
            else:
                self.assertEqual(paper_indexing["status"], "PARTIAL")
                self.assertTrue(any("semantic backend" in item for item in paper_indexing["limitations"]))
            repository = payload["repositories"][0]
            self.assertTrue(Path(repository["indexing"]["graph_path"]).is_file())
            self.assertEqual(
                repository["indexing"]["health"]["gitnexus_status_state"],
                "ISOLATED",
                json.dumps(repository["indexing"]["health"], indent=2),
            )
            self.assertTrue(repository["graph_nodes"])
            self.assertIn("parseEmail", Path(payload["build_plan"]).read_text(encoding="utf-8"))
            self.assertTrue(payload["relationships"]["EXTRACTED"])


if __name__ == "__main__":
    unittest.main()
