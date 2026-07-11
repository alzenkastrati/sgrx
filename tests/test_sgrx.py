from __future__ import annotations

import argparse
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "sgrx" / "scripts" / "sgrx.py"
SPEC = importlib.util.spec_from_file_location("sgrx_cli", SCRIPT)
assert SPEC and SPEC.loader
sgrx = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sgrx
SPEC.loader.exec_module(sgrx)
FIXTURE = ROOT / "tests" / "fixtures" / "consumer"
DEPENDENCY = ROOT / "tests" / "fixtures" / "dependency"


def namespace(**overrides):
    values = {
        "package": "zod",
        "project": str(FIXTURE),
        "registry": "npm",
        "ref": None,
        "version": None,
        "source_path": str(DEPENDENCY),
        "mode": "standard",
        "allow_global_graph": False,
        "allow_gitnexus_group": False,
        "force": False,
        "timeout": 3.0,
        "dry_run": True,
        "question": "How does validation work?",
        "from_version": "3.22.0",
        "to_version": "4.4.3",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RunnerSafetyTests(unittest.TestCase):
    def test_subprocess_uses_argument_list_and_shell_false(self):
        completed = subprocess.CompletedProcess(["tool", "arg"], 0, "ok", "")
        with mock.patch.object(sgrx.subprocess, "run", return_value=completed) as run:
            result = sgrx.CommandRunner(timeout=2).run(["tool", "arg"], cwd=FIXTURE)
        self.assertTrue(result.ok)
        positional, keywords = run.call_args
        self.assertEqual(positional[0], ["tool", "arg"])
        self.assertIs(keywords["shell"], False)
        self.assertEqual(keywords["timeout"], 2)

    def test_windows_powershell_shim_uses_file_argument_vector(self):
        completed = subprocess.CompletedProcess(["powershell"], 0, "ok", "")
        which = lambda name: {"tool": r"C:\bin\tool.ps1", "pwsh": r"C:\bin\pwsh.exe"}.get(name)
        with mock.patch.object(sgrx.os, "name", "nt"), mock.patch.object(sgrx.shutil, "which", side_effect=which), mock.patch.object(
            sgrx.subprocess, "run", return_value=completed
        ) as run:
            sgrx.CommandRunner().run(["tool", "value with spaces"])
        executed = run.call_args.args[0]
        self.assertEqual(executed[:5], [r"C:\bin\pwsh.exe", "-NoProfile", "-NonInteractive", "-File", r"C:\bin\tool.ps1"])
        self.assertEqual(executed[5], "value with spaces")
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_rejects_string_command(self):
        with self.assertRaises(sgrx.SGRXError):
            sgrx.CommandRunner().run("tool --version")

    def test_timeout_is_visible(self):
        expired = subprocess.TimeoutExpired(["tool"], 0.01, output=b"partial", stderr=b"late")
        with mock.patch.object(sgrx.subprocess, "run", side_effect=expired):
            result = sgrx.CommandRunner(timeout=0.01).run(["tool"])
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stdout, "partial")
        self.assertFalse(result.ok)

    def test_missing_tool_is_visible(self):
        with mock.patch.object(sgrx.subprocess, "run", side_effect=FileNotFoundError):
            result = sgrx.CommandRunner().run(["missing-tool", "--version"])
        self.assertTrue(result.missing)
        self.assertEqual(result.returncode, 127)

    def test_output_is_bounded(self):
        completed = subprocess.CompletedProcess(["tool"], 0, "x" * 100, "")
        with mock.patch.object(sgrx.subprocess, "run", return_value=completed):
            result = sgrx.CommandRunner(max_output=12).run(["tool"])
        self.assertEqual(len(result.stdout), 12)

    def test_secret_arguments_are_redacted(self):
        self.assertEqual(
            sgrx.redact_command(["tool", "--token", "secret-value", "password=hidden"]),
            ["tool", "--token", "[REDACTED]", "password=[REDACTED]"],
        )


class ValidationTests(unittest.TestCase):
    def test_valid_package_specs(self):
        cases = [
            ("npm", "@scope/package@1.2.3"),
            ("pypi", "httpx==0.28.1"),
            ("crates", "serde@1.0.228"),
            ("github", "owner/repository@v2.1.0"),
            ("github", "https://github.com/owner/repository"),
        ]
        for registry, package in cases:
            with self.subTest(registry=registry, package=package):
                self.assertEqual(sgrx.validate_package(package, registry), package)

    def test_rejects_manipulative_inputs(self):
        for package in ("zod;whoami", "zod\n--help", "$(whoami)", "../../secret", "zod | echo bad"):
            with self.subTest(package=package), self.assertRaises(sgrx.SGRXError):
                sgrx.validate_package(package, "npm")

    def test_github_ref_validation(self):
        self.assertEqual(sgrx.validate_ref("release/v1.2.0"), "release/v1.2.0")
        for ref in ("../main", "-danger", "main;echo", "/absolute"):
            with self.subTest(ref=ref), self.assertRaises(sgrx.SGRXError):
                sgrx.validate_ref(ref)

    def test_windows_and_posix_paths_remain_single_arguments(self):
        windows = Path(r"C:\Program Files\Consumer App")
        posix = Path("/tmp/consumer app")
        for path in (windows, posix):
            command = sgrx.command_for_opensrc("zod", "npm", path)
            self.assertEqual(command[0:3], ["opensrc", "path", "zod"])
            self.assertEqual(command[-2], "--cwd")
            self.assertEqual(len(command), 5)

    def test_paths_with_spaces_are_single_index_arguments(self):
        command = sgrx.gitnexus_command(Path("dependency source"), "zod-4.4.3")
        self.assertEqual(command[4], str(Path("dependency source").resolve()))
        self.assertEqual(command[-2:], ["--name", "zod-4.4.3"])
        self.assertIn("--skip-git", command)


class WorkflowTests(unittest.TestCase):
    def test_doctor_reports_missing_tools_without_installing(self):
        runner = sgrx.CommandRunner(dry_run=False)
        with mock.patch.object(sgrx.shutil, "which", return_value=None):
            payload = sgrx.doctor(runner)
        self.assertTrue(all(not item["available"] for item in payload["tools"].values()))
        self.assertEqual(runner.history, [])

    def test_tool_version_check_records_version(self):
        runner = sgrx.CommandRunner()
        completed = subprocess.CompletedProcess(["tool"], 0, "v20.0.0\n", "")
        with mock.patch.object(sgrx.shutil, "which", return_value="tool"), mock.patch.object(
            sgrx.subprocess, "run", return_value=completed
        ):
            payload = sgrx.doctor(runner)
        self.assertEqual(payload["tools"]["node"]["version"], "v20.0.0")
        self.assertTrue(payload["tools"]["node"]["meets_minimum"])

    def test_dry_run_resolve_records_provenance_and_lockfile(self):
        runner = sgrx.CommandRunner(dry_run=True)
        payload = sgrx.resolve_dependency(namespace(), runner)
        self.assertEqual(payload["resolution_status"], "DRY_RUN")
        self.assertEqual(payload["lockfile"], "package-lock.json")
        self.assertEqual(payload["resolved_version"], "4.4.3")
        self.assertEqual(payload["tool_result"]["args"][0], "opensrc")
        self.assertIsNone(payload["cache_path"])

    def test_dry_run_analyze_never_invokes_foreign_project_commands(self):
        runner = sgrx.CommandRunner(dry_run=True)
        payload = sgrx.analyze(namespace(), runner)
        executables = [item.args[0] for item in runner.history]
        self.assertEqual(executables[0], "opensrc")
        self.assertIn("graphify", executables)
        self.assertIn("npx", executables)
        forbidden = {"npm", "pnpm", "yarn", "cargo", "pytest"}
        self.assertFalse(forbidden.intersection(executables))
        self.assertTrue(payload["consumer_call_sites"])
        self.assertTrue(all(row["evidence_status"] == "EXTRACTED" for row in payload["consumer_call_sites"]))

    def test_index_uses_separate_state_by_default(self):
        runner = sgrx.CommandRunner(dry_run=True)
        payload = sgrx.index_sources(namespace(), runner, DEPENDENCY)
        self.assertTrue(payload["manifest"]["separate_graphs"])
        self.assertFalse(payload["manifest"]["global_graph_opt_in"])
        commands = payload["manifest"]["commands"]
        self.assertEqual([row["args"][0] for row in commands], ["graphify", "npx"])
        self.assertNotIn("group", json.dumps(commands))

    def test_gitnexus_group_requires_opt_in(self):
        args = namespace(allow_gitnexus_group=True)
        runner = sgrx.CommandRunner(dry_run=True)
        payload = sgrx.index_sources(args, runner, DEPENDENCY)
        self.assertTrue(payload["manifest"]["gitnexus_group_opt_in"])
        self.assertIn("group", json.dumps(payload["manifest"]["commands"]))

    def test_version_comparison_uses_source_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            left = Path(directory) / "left"
            right = Path(directory) / "right"
            left.mkdir(); right.mkdir()
            (left / "same.py").write_text("value = 1\n", encoding="utf-8")
            (right / "same.py").write_text("value = 2\n", encoding="utf-8")
            (right / "added.py").write_text("added = True\n", encoding="utf-8")
            result = sgrx.compare_source_trees(left, right)
        self.assertEqual(result["changed"], ["same.py"])
        self.assertEqual(result["added"], ["added.py"])
        self.assertEqual(result["removed"], [])

    def test_cli_dry_run_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = sgrx.main(["resolve", "--dry-run", "--json", "--package", "zod", "--project", str(FIXTURE)])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["provenance"]["resolution_status"], "DRY_RUN")

    def test_help_starts_with_brand(self):
        self.assertTrue(sgrx.build_parser().format_help().startswith(sgrx.BRAND))


class ReportTests(unittest.TestCase):
    def sample(self):
        row = {
            "consumer_location": "src/app.ts:3",
            "package": "zod@4.4.3",
            "public_api": "safeParse",
            "dependency_location": "zod@4.4.3:src/index.ts:9",
            "gitnexus_symbol_or_process": "safeParse",
            "graphify_relationship": "CALLS",
            "evidence_status": "EXTRACTED",
            "confidence": 0.98,
            "uncertainties": "Runtime observation not collected.",
        }
        return {
            "question": "How does it work?",
            "short_answer": "The static call is directly supported.",
            "provenance": {"resolved_version": "4.4.3"},
            "consumer_call_sites": [row],
            "external_implementation": [row],
            "evidence": [row],
            "relationships": {"EXTRACTED": [row], "INFERRED": [], "AMBIGUOUS": []},
            "limitations": ["No runtime trace."],
            "recommended_next_steps": ["Collect runtime evidence."],
            "tool_versions": {},
            "commands": [],
        }

    def test_markdown_report_has_required_sections_and_portable_citation(self):
        report = sgrx.markdown_report(self.sample())
        for section in ("Question", "External implementation", "Evidence table", "EXTRACTED relationships", "Limitations", "Executed commands"):
            self.assertIn(f"## {section}", report)
        self.assertIn("zod@4.4.3:src/index.ts:9", report)

    def test_json_report_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            target = Path(directory) / "output.json"
            source.write_text(json.dumps(self.sample()), encoding="utf-8")
            code = sgrx.main(["report", "--input", str(source), "--json", "--output", str(target)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["question"], "How does it work?")

    def test_evidence_status_validation(self):
        row = self.sample()["evidence"][0].copy()
        row["evidence_status"] = "CERTAIN"
        with self.assertRaises(sgrx.SGRXError):
            sgrx.evidence_table([row])


if __name__ == "__main__":
    unittest.main()
