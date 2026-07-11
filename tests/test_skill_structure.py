from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sgrx"


class SkillStructureTests(unittest.TestCase):
    def test_required_files_exist(self):
        required = [
            ROOT / "README.md",
            ROOT / "LICENSE",
            ROOT / ".gitignore",
            ROOT / ".github" / "workflows" / "ci.yml",
            SKILL / "SKILL.md",
            SKILL / "agents" / "openai.yaml",
            SKILL / "scripts" / "sgrx.py",
            SKILL / "references" / "tool-routing.md",
            SKILL / "references" / "evidence-model.md",
            SKILL / "references" / "report-schema.md",
            SKILL / "references" / "examples.md",
            ROOT / "tests" / "fixtures" / "consumer" / ".gitignore",
        ]
        self.assertTrue(all(path.is_file() for path in required))

    def test_skill_has_only_valid_frontmatter_keys(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1].strip().splitlines()
        keys = {line.split(":", 1)[0].strip() for line in frontmatter if ":" in line}
        self.assertEqual(keys, {"name", "description"})
        self.assertIn("name: sgrx", frontmatter)
        self.assertLess(len(text.splitlines()), 500)

    def test_description_contains_triggers(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        required = [
            "dependency internals", "package source research", "cross-repository analysis",
            "implementation tracing", "architecture investigation", "version comparison",
            "blast-radius analysis", "opensrc", "Graphify", "GitNexus", "source graph research", "SGRX",
        ]
        for phrase in required:
            self.assertIn(phrase, text)

    def test_openai_yaml_is_quoted_and_exact(self):
        text = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        expected = [
            '  display_name: "SGRX — Source Graph Research eXplorer"',
            '  short_description: "Trace dependency source and execution graphs"',
            '  default_prompt: "Use $sgrx to trace this dependency from the consumer call site into its exact implementation and assess the change impact."',
        ]
        for line in expected:
            self.assertIn(line, text)
        self.assertNotIn("dependencies:", text)

    def test_skill_links_every_reference_directly(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for name in ("tool-routing.md", "evidence-model.md", "report-schema.md", "examples.md"):
            self.assertIn(f"references/{name}", text)

    def test_no_extraneous_skill_documents(self):
        markdown = {path.name for path in SKILL.glob("*.md")}
        self.assertEqual(markdown, {"SKILL.md"})

    def test_no_unfinished_markers_or_machine_paths(self):
        markers = ["TO" + "DO", "FIX" + "ME", "X" * 3]
        slash_users = "/" + "Users" + "/"
        slash_home = "/" + "home" + "/"
        machine_paths = [
            re.compile(r"[A-Za-z]:[/\\]" + "Users" + r"[/\\]"),
            re.compile(re.escape(slash_users) + r"[^/]+/"),
            re.compile(re.escape(slash_home) + r"[^/$]+/"),
        ]
        suffixes = {".md", ".py", ".yml", ".yaml", ".json", ".ts", ".txt"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes or any(part in {".git", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in markers:
                self.assertNotIn(marker, text, path)
            for pattern in machine_paths:
                self.assertIsNone(pattern.search(text), path)

    def test_no_secret_material(self):
        assignment = re.compile(r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}")
        for path in ROOT.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".py", ".yml", ".yaml", ".json"}:
                self.assertIsNone(assignment.search(path.read_text(encoding="utf-8", errors="replace")), path)

    def test_fixture_ignores_sgrx_artifacts(self):
        text = (ROOT / "tests" / "fixtures" / "consumer" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".sgrx/", text.splitlines())

    def test_brand_and_invocation_are_consistent(self):
        for path in (ROOT / "README.md", SKILL / "SKILL.md", SKILL / "agents" / "openai.yaml"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("SGRX", text)
        self.assertIn("$sgrx", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_ci_uses_no_package_downloads(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for command in ("pip install", "npm install", "cargo install"):
            self.assertNotIn(command, text)
        self.assertIn('"ubuntu-latest"', text)
        self.assertIn('"windows-latest"', text)


if __name__ == "__main__":
    unittest.main()
