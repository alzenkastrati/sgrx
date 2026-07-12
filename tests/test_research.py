from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.test_sgrx import DEPENDENCY, FIXTURE, FakeIndexRunner, sgrx


research = __import__("sgrx_research")


def candidate_data() -> dict:
    return {
        "question": "How should we build a local voice agent?",
        "requirements": ["Run locally", "Keep latency measurable"],
        "papers": [
            {
                "id": "paper-a",
                "title": "Efficient Local Speech Agents",
                "year": research.CURRENT_YEAR,
                "abstract": "A modular streaming speech architecture.",
                "relevance": 0.95,
                "citations": 100,
                "official_repository": True,
                "estimated_tokens": 1000,
            },
            {
                "id": "paper-b",
                "title": "Unrelated Baseline",
                "year": research.CURRENT_YEAR - 7,
                "relevance": 0.1,
                "estimated_tokens": 1000,
            },
        ],
        "repositories": [
            {
                "spec": "owner/voice-agent@v1.0.0",
                "source_path": str(DEPENDENCY),
                "paper_ids": ["paper-a"],
                "official": True,
                "license": "MIT",
                "relevance": 0.95,
                "architecture_fit": 0.9,
                "reproducibility": 0.8,
                "activity": 0.9,
                "estimated_tokens": 3000,
                "evidence_status": "EXTRACTED",
            },
            {
                "spec": "owner/weak-example@v0.1.0",
                "relevance": 0.1,
                "architecture_fit": 0.1,
                "reproducibility": 0.1,
                "activity": 0.1,
                "estimated_tokens": 3000,
            },
        ],
    }


class ResearchRankingTests(unittest.TestCase):
    def test_ranking_and_budget_select_best_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps(candidate_data()), encoding="utf-8")
            candidates = research.load_candidates(path)
        selected = research.select_with_budget(
            candidates["papers"], candidates["repositories"],
            token_budget=10_000, max_papers=1, max_repositories=1,
        )
        self.assertEqual(selected["selected_papers"][0]["id"], "paper-a")
        self.assertEqual(selected["selected_repositories"][0]["spec"], "owner/voice-agent@v1.0.0")
        self.assertLessEqual(selected["budget"]["estimated_used"], 10_000)

    def test_unknown_paper_link_is_rejected(self):
        data = candidate_data()
        data["repositories"][0]["paper_ids"] = ["missing"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(research.ResearchError):
                research.load_candidates(path)

    def test_invalid_evidence_status_and_limits_are_rejected(self):
        data = candidate_data()
        data["papers"][0]["evidence_status"] = "CERTAIN"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(research.ResearchError):
                research.load_candidates(path)
        with self.assertRaises(research.ResearchError):
            research.select_with_budget([], [], token_budget=10_000, max_papers=0, max_repositories=1)

    def test_build_plan_includes_graph_evidence_and_phases(self):
        data = {
            "question": "Build a system",
            "requirements": ["Portable"],
            "papers": [{"id": "p", "title": "Paper", "year": 2026, "score": 0.9}],
            "repositories": [{
                "spec": "owner/repo@v1", "score": 0.8, "license": "MIT",
                "graph_nodes": [{"label": "StreamingPipeline", "source": "src/pipeline.py", "location": "L10"}],
            }],
            "budget": {"total": 10000, "papers": 2000, "repositories": 6000, "synthesis": 2000},
            "excluded_papers": [{"id": "deferred", "score": 0.2}],
            "excluded_repositories": [],
            "limitations": [],
        }
        plan = research.build_plan_markdown(data)
        self.assertIn("StreamingPipeline", plan)
        self.assertIn("Phase 1", plan)
        self.assertIn("Decision gates", plan)
        self.assertIn("Evidence-to-component work packages", plan)
        self.assertIn("owner/repo@v1:src/pipeline.py:L10", plan)
        self.assertIn("Rejected or deferred evidence", plan)


class ResearchWorkflowTests(unittest.TestCase):
    def test_research_dry_run_records_paper_and_repository_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps(candidate_data()), encoding="utf-8")
            args = argparse.Namespace(
                project=str(FIXTURE), candidates=str(candidates), question=None,
                token_budget=10_000, max_papers=1, max_repositories=1,
                mode="standard", timeout=3.0, dry_run=True, json=True, output=None,
                allow_global_graph=False, allow_gitnexus_group=False, force=False,
            )
            runner = sgrx.CommandRunner(dry_run=True)
            payload = sgrx.research_mode(args, runner)
        self.assertTrue(payload["research_mode"])
        self.assertEqual(len(payload["papers"]), 1)
        self.assertEqual(len(payload["repositories"]), 1)
        self.assertEqual(payload["relationships"]["EXTRACTED"][0]["repository"], "owner/voice-agent@v1.0.0")
        commands = [item.args for item in runner.history]
        self.assertTrue(any(command[:2] == ["graphify", "extract"] for command in commands))
        self.assertTrue(any(command[:4] == ["npx", "--no-install", "gitnexus", "analyze"] for command in commands))
        self.assertIn("Implementation sequence", payload["_build_plan_markdown"])

    def test_external_repository_dry_run_uses_planned_paths_not_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = candidate_data()
            data["repositories"][0].pop("source_path")
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps(data), encoding="utf-8")
            args = argparse.Namespace(
                project=str(FIXTURE), candidates=str(candidates), question=None,
                token_budget=10_000, max_papers=1, max_repositories=1,
                mode="standard", timeout=3.0, dry_run=True, json=True, output=None,
                allow_global_graph=False, allow_gitnexus_group=False, force=False,
            )
            runner = sgrx.CommandRunner(dry_run=True)
            payload = sgrx.research_mode(args, runner)
        repository = payload["repositories"][0]
        self.assertEqual(repository["indexing"]["status"], "DRY_RUN")
        self.assertEqual(repository["source_profile"], "code-only")
        self.assertTrue(any(item.args[:2] == ["opensrc", "path"] for item in runner.history))
        self.assertTrue(any(item.args[:2] == ["graphify", "extract"] for item in runner.history))
        self.assertNotIn("could not be resolved", " ".join(repository["limitations"]))

    def test_research_cli_dry_run_json(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = Path(directory) / "candidates.json"
            candidates.write_text(json.dumps(candidate_data()), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = sgrx.main([
                    "research", "--dry-run", "--json", "--project", str(FIXTURE),
                    "--candidates", str(candidates), "--max-papers", "1",
                    "--max-repositories", "1", "--token-budget", "10000",
                ])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["research_mode"])

    def test_research_writes_manifest_and_build_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            data = candidate_data()
            data["repositories"][0]["source_path"] = str(DEPENDENCY)
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps(data), encoding="utf-8")
            args = argparse.Namespace(
                project=str(project), candidates=str(candidates), question=None,
                token_budget=10_000, max_papers=1, max_repositories=1,
                mode="standard", timeout=3.0, dry_run=False, json=True, output=None,
                allow_global_graph=False, allow_gitnexus_group=False, force=True,
            )
            payload = sgrx.research_mode(args, FakeIndexRunner())
            manifest = Path(payload["artifact_dir"]) / "research-manifest.json"
            plan = Path(payload["build_plan"])
            self.assertTrue(manifest.is_file())
            self.assertTrue(plan.is_file())
            self.assertIn("Implementation sequence", plan.read_text(encoding="utf-8"))
            self.assertEqual(payload["repositories"][0]["source_profile"], "code-only")
            self.assertGreater(payload["repositories"][0]["indexed_file_count"], 0)
            self.assertEqual(payload["budget"]["observed_graphify_input"], 240)

    def test_research_reuses_candidate_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            data = candidate_data()
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps(data), encoding="utf-8")
            args = argparse.Namespace(
                project=str(project), candidates=str(candidates), question=None,
                token_budget=10_000, max_papers=1, max_repositories=1,
                mode="standard", timeout=3.0, dry_run=False, json=True, output=None,
                allow_global_graph=False, allow_gitnexus_group=False, force=False,
            )
            first_runner = FakeIndexRunner()
            first = sgrx.research_mode(args, first_runner)
            second_runner = FakeIndexRunner()
            second = sgrx.research_mode(args, second_runner)
            self.assertEqual(first["papers"][0]["checkpoint"]["status"], "SAVED")
            self.assertEqual(second["papers"][0]["checkpoint"]["status"], "REUSED")
            self.assertEqual(second["repositories"][0]["checkpoint"]["status"], "REUSED")
            self.assertFalse(any(item.args[:2] == ["graphify", "extract"] for item in second_runner.history))


if __name__ == "__main__":
    unittest.main()
