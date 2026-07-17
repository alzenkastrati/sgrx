from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "sgrx" / "scripts" / "sgrx.py"
SPEC = importlib.util.spec_from_file_location("sgrx_audit_cli", SCRIPT)
assert SPEC and SPEC.loader
sgrx = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sgrx
SPEC.loader.exec_module(sgrx)
FIXTURE = ROOT / "tests" / "fixtures" / "consumer"
DEPENDENCY = ROOT / "tests" / "fixtures" / "dependency"


def namespace(**overrides):
    values = {
        "package": "owner/benchmark",
        "project": str(FIXTURE),
        "registry": "github",
        "ref": "a" * 40,
        "version": None,
        "source_path": None,
        "mode": "standard",
        "allow_global_graph": False,
        "allow_gitnexus_group": False,
        "corpus_profile": "full",
        "token_budget": 0,
        "max_files": 0,
        "max_images": -1,
        "force": False,
        "timeout": 3.0,
        "dry_run": True,
        "question": "Which practices improve validation and reliability?",
        "facet_budget": 800,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RecoveryRunner(sgrx.CommandRunner):
    def __init__(self):
        super().__init__(timeout=1)
        self.repaired: set[str] = set()

    def run(self, args, *, cwd=None, env=None):
        vector = list(args)
        stdout = ""
        if vector[:2] == ["graphify", "extract"]:
            output = Path(vector[vector.index("--out") + 1]) / "graphify-out"
            output.mkdir(parents=True, exist_ok=True)
            (output / "graph.json").write_text('{"nodes": [], "links": []}', encoding="utf-8")
        elif vector[:4] == ["npx", "--no-install", "gitnexus", "analyze"]:
            source = Path(vector[4])
            source.joinpath(".gitnexus").mkdir(parents=True, exist_ok=True)
            if "--force" in vector:
                self.repaired.add(str(source))
        elif vector[-1:] == ["status"]:
            stdout = "Not a git repository.\n"
        elif "query" in vector:
            stdout = (
                '{"processes": [], "definitions": []}'
                if str(cwd) in self.repaired
                else '{"processes": [], "definitions": [], "warning": "FTS indexes missing"}'
            )
        result = sgrx.CommandResult(vector, 0, stdout=stdout)
        self.history.append(result)
        return result


class FacetRunner(sgrx.CommandRunner):
    def __init__(self):
        super().__init__(timeout=1)

    def run(self, args, *, cwd=None, env=None):
        vector = list(args)
        result = sgrx.CommandResult(
            vector,
            0,
            stdout="NODE Validation [src=workflow.md loc=L12 community=1]\n",
        )
        self.history.append(result)
        return result


class CorpusPlanningTests(unittest.TestCase):
    def test_code_docs_profile_excludes_images_and_honors_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("value = 1\n", encoding="utf-8")
            (root / "README.md").write_text("workflow " * 30, encoding="utf-8")
            (root / "diagram.png").write_bytes(b"image" * 100)
            (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            plan = sgrx.corpus_preflight(root, profile="code-docs", token_budget=10, max_files=10, max_images=0)

            self.assertEqual(plan["selected_counts"]["image"], 0)
            self.assertEqual(plan["selected_files"], 2)
            self.assertEqual(plan["status"], "NARROW_REQUIRED")
            self.assertTrue(any("estimated tokens" in item for item in plan["violations"]))

    def test_corpus_snapshot_preserves_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            destination = Path(directory) / "snapshot"
            root.joinpath("docs").mkdir(parents=True)
            root.joinpath("docs", "guide.md").write_text("guide\n", encoding="utf-8")
            root.joinpath("docs", "screen.png").write_bytes(b"png")
            sgrx.prepare_corpus_snapshot(root, destination, "code-docs")

            self.assertTrue(destination.joinpath("docs", "guide.md").is_file())
            self.assertFalse(destination.joinpath("docs", "screen.png").exists())

    def test_relative_include_and_exclude_paths_narrow_the_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("reports").mkdir()
            root.joinpath("tips").mkdir()
            root.joinpath("reports", "workflow.md").write_text("workflow\n", encoding="utf-8")
            root.joinpath("reports", "draft.md").write_text("draft\n", encoding="utf-8")
            root.joinpath("tips", "context.md").write_text("context\n", encoding="utf-8")
            plan = sgrx.corpus_preflight(
                root,
                profile="code-docs",
                include_paths=["reports"],
                exclude_paths=["reports/draft.md"],
            )

            self.assertEqual(plan["selected_files"], 1)
            self.assertEqual(plan["filters"]["include_paths"], ["reports"])
            with self.assertRaises(ValueError):
                sgrx.corpus_preflight(root, profile="code-docs", include_paths=["../outside"])

    def test_index_stops_before_tools_when_corpus_budget_is_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "consumer"
            source = Path(directory) / "dependency"
            project.mkdir(); source.mkdir()
            project.joinpath("app.py").write_text("value = 1\n", encoding="utf-8")
            source.joinpath("guide.md").write_text("large corpus " * 100, encoding="utf-8")
            runner = sgrx.CommandRunner(timeout=1)
            payload = sgrx.index_sources(
                namespace(
                    project=str(project),
                    dry_run=False,
                    corpus_profile="code-docs",
                    token_budget=1,
                    max_images=0,
                ),
                runner,
                source,
            )

            self.assertEqual(payload["status"], "PARTIAL")
            self.assertEqual(payload["manifest"]["corpus"]["dependency"]["status"], "NARROW_REQUIRED")
            self.assertFalse(any(item.args[:2] == ["graphify", "extract"] for item in runner.history))


class ReliabilityTests(unittest.TestCase):
    def test_exact_github_sha_is_preserved_without_git_metadata(self):
        sha = "b" * 40
        payload = sgrx.resolve_dependency(namespace(ref=sha), sgrx.CommandRunner(dry_run=True))
        self.assertEqual(payload["commit"], sha)

    def test_graphify_data_loss_warnings_are_structured(self):
        result = sgrx.CommandResult(
            ["graphify", "extract"],
            0,
            stderr=(
                "warning: 4 source file(s) produced zero nodes\n"
                "cross-chunk ID collision caused by two files\n"
                "[graphify] Extraction warning (13 issues): missing source_file\n"
            ),
        )
        issues = sgrx.graphify_health_issues(result)
        self.assertEqual(issues["zero_node_sources"], 4)
        self.assertEqual(issues["cross_chunk_id_collisions"], 1)
        self.assertEqual(issues["extraction_issues"], 13)
        self.assertTrue(issues["data_loss_risk"])
        self.assertTrue(issues["degraded"])

    def test_missing_fts_is_rebuilt_once_inside_each_isolated_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "consumer"
            source = Path(directory) / "dependency"
            project.mkdir(); source.mkdir()
            project.joinpath("app.py").write_text("value = 1\n", encoding="utf-8")
            source.joinpath("lib.py").write_text("value = 2\n", encoding="utf-8")
            runner = RecoveryRunner()
            payload = sgrx.index_sources(namespace(project=str(project), dry_run=False), runner, source)

            self.assertEqual(payload["status"], "HEALTHY")
            for role in ("dependency", "consumer"):
                recovery = payload["manifest"]["indexes"][role]["gitnexus_recovery"]
                self.assertTrue(recovery["attempted"])
                self.assertTrue(recovery["succeeded"])
            repair_commands = [item.args for item in runner.history if "--force" in item.args]
            self.assertEqual(len(repair_commands), 2)

    def test_report_gate_rejects_unredacted_commands_and_observed_budget_overrun(self):
        payload = {
            "question": "Question",
            "short_answer": "Answer",
            "provenance": {"registry": "github", "ref": "a" * 40, "commit": "a" * 40},
            "consumer_call_sites": [],
            "external_implementation": [],
            "end_to_end_path": [],
            "architecture_overview": [],
            "edge_cases": [],
            "deprecations": [],
            "change_risk": {},
            "evidence": [],
            "relationships": {"EXTRACTED": [], "INFERRED": [], "AMBIGUOUS": []},
            "limitations": [],
            "recommended_next_steps": [],
            "tool_versions": {},
            "commands": [{"args": ["tool", "--token", "plain-secret"], "stdout": "", "stderr": ""}],
            "indexing": {
                "status": "HEALTHY",
                "health": {
                    "source_unchanged": True,
                    "gitnexus_search_ok": True,
                    "graph_diagnostics_ok": True,
                    "consumer": {"source_unchanged": True, "gitnexus_search_ok": True, "graph_diagnostics_ok": True},
                },
                "manifest": {"corpus": {"dependency": {"observed_exceeds_budget": True}}},
            },
        }
        verification = sgrx.verify_report(payload)

        self.assertEqual(verification["status"], "DEGRADED")
        failed = {item["id"] for item in verification["failures"]}
        self.assertIn("commands:redacted", failed)
        self.assertIn("index:observed-budget", failed)


class AuditWorkflowTests(unittest.TestCase):
    def test_analyze_queries_are_checkpointed_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency_graph = root / "dependency" / "graphify-out" / "graph.json"
            consumer_graph = root / "consumer" / "graphify-out" / "graph.json"
            for graph in (dependency_graph, consumer_graph):
                graph.parent.mkdir(parents=True)
                graph.write_text(json.dumps({"nodes": [{"label": "Validation Workflow"}]}), encoding="utf-8")
            indexing = {
                "artifact_dir": str(root / "artifacts"),
                "manifest": {
                    "graph_path": str(dependency_graph),
                    "consumer_graph_path": str(consumer_graph),
                    "source_identity": "dependency-id",
                    "consumer_source_identity": "consumer-id",
                },
            }
            first_runner = FacetRunner()
            _research, first = sgrx.analysis_index_research(
                indexing, "Improve validation", [], first_runner, mode="standard", force=False
            )
            second_runner = FacetRunner()
            _research, second = sgrx.analysis_index_research(
                indexing, "Improve validation", [], second_runner, mode="standard", force=False
            )

            self.assertEqual(first["status"], "SAVED")
            self.assertEqual(second["status"], "REUSED")
            self.assertEqual(second_runner.history, [])

    def test_faceted_queries_are_checkpointed_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency_graph = root / "dependency" / "graphify-out" / "graph.json"
            consumer_graph = root / "consumer" / "graphify-out" / "graph.json"
            for graph in (dependency_graph, consumer_graph):
                graph.parent.mkdir(parents=True)
                graph.write_text(json.dumps({"nodes": [{"label": "Validation Workflow"}]}), encoding="utf-8")
            indexing = {
                "artifact_dir": str(root / "artifacts"),
                "manifest": {
                    "graph_path": str(dependency_graph),
                    "consumer_graph_path": str(consumer_graph),
                    "source_identity": "dependency-id",
                    "consumer_source_identity": "consumer-id",
                    "consumer_source_path": str(root),
                },
            }
            first_runner = FacetRunner()
            first = sgrx.audit_graph_facets(indexing, "Improve validation", first_runner, mode="standard", facet_budget=400, force=False)
            second_runner = FacetRunner()
            second = sgrx.audit_graph_facets(indexing, "Improve validation", second_runner, mode="standard", facet_budget=400, force=False)

            self.assertEqual(first["checkpoint"]["status"], "SAVED")
            self.assertEqual(second["checkpoint"]["status"], "REUSED")
            self.assertEqual(second_runner.history, [])
            self.assertIn("validation", second["facets"])

    def test_audit_cli_dry_run_plans_facets_without_runtime_dependency_claims(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = sgrx.main([
                "audit", "--dry-run", "--json",
                "--benchmark", "owner/benchmark",
                "--project", str(FIXTURE),
                "--source-path", str(DEPENDENCY),
                "--question", "Which practices improve validation?",
            ])
        payload = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertTrue(payload["audit_mode"])
        self.assertEqual(payload["verification"]["status"], "DRY_RUN")
        self.assertEqual(payload["indexing"]["manifest"]["corpus"]["dependency"]["profile"], "code-docs")
        self.assertEqual(set(payload["audit_facets"]), {"lifecycle", "context", "distribution", "validation", "reliability"})
        self.assertTrue(all(item["evidence_status"] == "AMBIGUOUS" for item in payload["practice_mappings"]))
        self.assertGreaterEqual(sum(1 for item in payload["commands"] if item["args"][:2] == ["graphify", "query"]), 10)


if __name__ == "__main__":
    unittest.main()
