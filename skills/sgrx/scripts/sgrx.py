#!/usr/bin/env python3
"""SGRX: safe orchestration for dependency source-graph research.

Use only the Python standard library. Treat fetched dependency source as data:
this module never imports it, runs it, installs its dependencies, or invokes its
build and test commands.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


BRAND = "SGRX — Source Graph Research eXplorer"
EVIDENCE_STATUSES = ("EXTRACTED", "INFERRED", "AMBIGUOUS")
MODES = ("quick", "standard", "deep")
REGISTRIES = ("npm", "pypi", "crates", "github")
MAX_OUTPUT = 200_000
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".mjs", ".py", ".rs", ".ts", ".tsx"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
PACKAGE_PATTERNS = {
    "npm": re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9._+~-]*)?$", re.I),
    "pypi": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:==[A-Za-z0-9][A-Za-z0-9._+!-]*)?$"),
    "crates": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:@[A-Za-z0-9][A-Za-z0-9._+~-]*)?$"),
}
REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+~-]{0,199}$")
GITHUB_PATTERN = re.compile(
    r"^(?:https://github\.com/)?(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?(?:@(?P<ref>[A-Za-z0-9][A-Za-z0-9._/+~-]{0,199}))?$"
)


class SGRXError(RuntimeError):
    """Represent a safe, user-facing orchestration failure."""


class BrandedParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return f"{BRAND}\n\n{super().format_help()}"


@dataclasses.dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    dry_run: bool = False
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.missing


class CommandRunner:
    """Run allow-listed CLI argument vectors without a shell."""

    def __init__(self, timeout: float = 60.0, dry_run: bool = False, max_output: int = MAX_OUTPUT):
        if timeout <= 0:
            raise SGRXError("Timeout must be greater than zero.")
        self.timeout = timeout
        self.dry_run = dry_run
        self.max_output = max_output
        self.history: list[CommandResult] = []

    def run(self, args: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        if isinstance(args, (str, bytes)) or not args or not all(isinstance(x, str) and x for x in args):
            raise SGRXError("Commands must be non-empty argument lists.")
        vector = list(args)
        if self.dry_run:
            result = CommandResult(vector, None, dry_run=True)
            self.history.append(result)
            return result
        executed = _platform_vector(vector)
        try:
            completed = subprocess.run(
                executed,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
                shell=False,
            )
            result = CommandResult(
                executed,
                completed.returncode,
                completed.stdout[: self.max_output],
                completed.stderr[: self.max_output],
            )
        except FileNotFoundError:
            result = CommandResult(executed, 127, stderr=f"Tool not found: {vector[0]}", missing=True)
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                executed,
                None,
                _bounded(exc.stdout, self.max_output),
                _bounded(exc.stderr, self.max_output),
                timed_out=True,
            )
        self.history.append(result)
        return result


def _platform_vector(vector: list[str]) -> list[str]:
    """Adapt Windows PowerShell command shims without enabling a shell."""
    if os.name != "nt":
        return vector
    resolved = shutil.which(vector[0])
    if not resolved:
        return vector
    if resolved.lower().endswith(".cmd"):
        sibling = resolved[:-4] + ".ps1"
        if os.path.isfile(sibling):
            resolved = sibling
    if not resolved.lower().endswith(".ps1"):
        return vector
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        return vector
    return [powershell, "-NoProfile", "-NonInteractive", "-File", resolved, *vector[1:]]


def _bounded(value: str | bytes | None, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value[:limit]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if not REF_PATTERN.fullmatch(ref) or ".." in ref or ref.startswith(("-", "/")):
        raise SGRXError(f"Invalid Git ref: {ref!r}")
    return ref


def validate_package(package: str, registry: str) -> str:
    if not package or len(package) > 240 or any(c in package for c in "\r\n\0;&|`$<>(){}[]\\\""):
        raise SGRXError("Package specification contains unsafe characters.")
    if registry == "github":
        match = GITHUB_PATTERN.fullmatch(package)
        if not match:
            raise SGRXError("GitHub specification must be owner/repo, a GitHub URL, and optionally @ref.")
        validate_ref(match.group("ref"))
        return package
    pattern = PACKAGE_PATTERNS[registry]
    if not pattern.fullmatch(package) or package.startswith("-"):
        raise SGRXError(f"Invalid {registry} package specification: {package!r}")
    return package


def safe_slug(package: str, version: str | None) -> str:
    raw = f"{package}@{version or 'resolved'}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    return slug[:120] or "dependency-resolved"


def artifact_dir(project: Path, package: str, version: str | None) -> Path:
    return project.resolve() / ".sgrx" / safe_slug(package, version)


def source_tree_identity(root: Path) -> str:
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.lower() in SENSITIVE_NAMES:
            continue
        relative = path.relative_to(root)
        if any(part in {".git", ".sgrx", "node_modules", "target", "dist", "build"} for part in relative.parts):
            continue
        try:
            stat = path.stat()
            entries.append(f"{relative.as_posix()}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def command_for_opensrc(package: str, registry: str, project: Path, ref: str | None = None) -> list[str]:
    spec = package
    if ref and registry != "github" and not re.search(r"(?:@|==)[^/]+$", package):
        spec += ("==" if registry == "pypi" else "@") + ref
    if ref and registry == "github" and "@" not in package.rsplit("/", 1)[-1]:
        spec += "@" + ref
    return ["opensrc", "path", spec, "--cwd", str(project.resolve())]


def graphify_command(source: Path, output_scope: Path, mode: str) -> list[str]:
    command = ["graphify", str(source.resolve()), "--no-viz"]
    if mode == "deep":
        command += ["--mode", "deep"]
    return command


def gitnexus_command(source: Path, alias: str) -> list[str]:
    return [
        "npx", "--no-install", "gitnexus", "analyze", str(source.resolve()),
        "--index-only", "--skip-git", "--name", alias,
    ]


def tool_specs() -> dict[str, list[str]]:
    return {
        "node": ["node", "--version"],
        "git": ["git", "--version"],
        "opensrc": ["opensrc", "--version"],
        "graphify": ["graphify", "--version"],
        "npx": ["npx", "--version"],
        "gitnexus": ["npx", "--no-install", "gitnexus", "--version"],
    }


def doctor(runner: CommandRunner) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for name, command in tool_specs().items():
        discovered = shutil.which(command[0]) is not None
        result = runner.run(command) if discovered or runner.dry_run else CommandResult(command, 127, missing=True)
        available = discovered and not result.missing
        version = (result.stdout or result.stderr).strip().splitlines()[:1]
        tools[name] = {
            "available": available if not runner.dry_run else None,
            "ok": result.ok if not runner.dry_run else None,
            "version": version[0] if version else None,
            "install_hint": install_hint(name) if not available else None,
        }
    node_version = tools["node"].get("version") or ""
    match = re.search(r"(\d+)", node_version)
    tools["node"]["meets_minimum"] = bool(match and int(match.group(1)) >= 18) if not runner.dry_run else None
    return {"brand": BRAND, "timestamp": utc_now(), "tools": tools, "commands": command_log(runner.history)}


def install_hint(tool: str) -> str:
    return {
        "node": "Install Node.js 18 or newer from the official Node.js distribution.",
        "git": "Install Git from the official Git distribution.",
        "opensrc": "Install opensrc using its documented CLI installation method.",
        "graphify": "Install Graphify using its documented CLI installation method.",
        "npx": "Install Node.js 18 or newer, which includes npx.",
        "gitnexus": "Install GitNexus using its documented CLI installation method.",
    }[tool]


def locate_source(output: str) -> Path | None:
    for line in reversed(output.splitlines()):
        candidate = line.strip().strip('"\'')
        if candidate.startswith("path:"):
            candidate = candidate[5:].strip()
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path.resolve()
    return None


def lockfile_for(project: Path, registry: str) -> str | None:
    names = {
        "npm": ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
        "pypi": ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt"),
        "crates": ("Cargo.lock",),
        "github": (),
    }[registry]
    for name in names:
        if (project / name).is_file():
            return name
    return None


def package_version(package: str, registry: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if registry == "pypi" and "==" in package:
        return package.rsplit("==", 1)[1]
    if registry in {"npm", "crates"} and "@" in package.lstrip("@"):
        return package.rsplit("@", 1)[1]
    if registry == "github":
        match = GITHUB_PATTERN.fullmatch(package)
        return match.group("ref") if match else None
    return None


def lockfile_version(project: Path, package: str, registry: str) -> str | None:
    if registry != "npm":
        return None
    path = project / "package-lock.json"
    if not path.is_file():
        return None
    base = package
    if package.startswith("@") and package.count("@") > 1:
        base = package.rsplit("@", 1)[0]
    elif not package.startswith("@") and "@" in package:
        base = package.rsplit("@", 1)[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get("packages", {}).get(f"node_modules/{base}", {})
        version = entry.get("version")
        return str(version) if version else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def resolve_dependency(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise SGRXError(f"Consumer project does not exist: {project}")
    package = validate_package(args.package, args.registry)
    ref = validate_ref(args.ref)
    command = command_for_opensrc(package, args.registry, project, ref)
    result = runner.run(command, cwd=project)
    source_path = locate_source(result.stdout) if result.ok else None
    version = package_version(package, args.registry, ref) or lockfile_version(project, package, args.registry)
    return {
        "requested_package": package,
        "registry": args.registry,
        "resolved_version": version,
        "ref": ref,
        "commit": None,
        "consumer_project": str(project),
        "lockfile": lockfile_for(project, args.registry),
        "cache_path": str(source_path) if source_path else None,
        "timestamp": utc_now(),
        "resolution_status": "DRY_RUN" if result.dry_run else ("RESOLVED" if source_path else "UNRESOLVED"),
        "tool_versions": {"opensrc": None, "graphify": None, "gitnexus": None},
        "tool_result": result_payload(result),
    }


def index_sources(args: argparse.Namespace, runner: CommandRunner, source_path: Path | None = None) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    package = validate_package(args.package, args.registry)
    version = getattr(args, "version", None) or getattr(args, "ref", None)
    scope = artifact_dir(project, package, version)
    source = source_path or (Path(args.source_path).expanduser().resolve() if getattr(args, "source_path", None) else None)
    if source is None or (not runner.dry_run and not source.is_dir()):
        return {"status": "UNAVAILABLE", "reason": "No dependency source path was resolved.", "artifact_dir": str(scope), "commands": []}
    if not runner.dry_run:
        scope.mkdir(parents=True, exist_ok=True)
    manifest_path = scope / "index-manifest.json"
    source_identity = "dry-run" if runner.dry_run else source_tree_identity(source)
    fingerprint = hashlib.sha256(f"{source}|{version}|{args.mode}|{source_identity}".encode()).hexdigest()
    stale_index_detected = False
    if manifest_path.is_file() and not getattr(args, "force", False):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("fingerprint") == fingerprint and manifest.get("status") == "INDEXED":
                return {"status": "REUSED", "artifact_dir": str(scope), "manifest": manifest, "commands": [], "stale_index_detected": False}
            stale_index_detected = True
        except (OSError, json.JSONDecodeError):
            stale_index_detected = True
    before = len(runner.history)
    graph = runner.run(graphify_command(source, scope / "graphify", args.mode), cwd=scope if not runner.dry_run else project)
    nexus = runner.run(gitnexus_command(source, safe_slug(package, version)), cwd=scope if not runner.dry_run else project)
    group_result = None
    if args.allow_gitnexus_group:
        group_result = runner.run(
            ["npx", "--no-install", "gitnexus", "group", "create", safe_slug(package, version)],
            cwd=scope if not runner.dry_run else project,
        )
    status = "DRY_RUN" if runner.dry_run else ("INDEXED" if graph.ok and nexus.ok else "PARTIAL")
    manifest = {
        "fingerprint": fingerprint,
        "status": status,
        "source_path": str(source),
        "separate_graphs": not args.allow_global_graph,
        "global_graph_opt_in": bool(args.allow_global_graph),
        "gitnexus_group_opt_in": bool(args.allow_gitnexus_group),
        "timestamp": utc_now(),
        "source_identity": source_identity,
        "stale_index_detected": stale_index_detected,
        "commands": command_log(runner.history[before:]),
    }
    if not runner.dry_run:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "status": status,
        "artifact_dir": str(scope),
        "manifest": manifest,
        "group": result_payload(group_result) if group_result else None,
        "stale_index_detected": stale_index_detected,
    }


def scan_consumer(project: Path, package: str) -> list[dict[str, Any]]:
    needle = package
    if package.startswith("https://github.com/"):
        needle = package.removeprefix("https://github.com/").split("@", 1)[0]
    elif package.startswith("@") and package.count("@") > 1:
        needle = package.rsplit("@", 1)[0]
    elif "@" in package:
        needle = package.rsplit("@", 1)[0]
    elif "==" in package:
        needle = package.rsplit("==", 1)[0]
    findings: list[dict[str, Any]] = []
    for path in sorted(project.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(project)
        if any(part in {".git", ".sgrx", "node_modules", ".venv", "venv", "target"} for part in relative.parts):
            continue
        if path.name.lower() in SENSITIVE_NAMES:
            continue
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if needle in line:
                    findings.append({
                        "consumer_location": f"{relative.as_posix()}:{number}",
                        "package": package,
                        "public_api": _api_hint(line, needle),
                        "dependency_location": None,
                        "gitnexus_symbol_or_process": None,
                        "graphify_relationship": "references package in source text",
                        "evidence_status": "EXTRACTED",
                        "confidence": 0.95,
                        "uncertainties": "Classify runtime reachability only after call and contract tracing.",
                    })
        except OSError:
            continue
    return findings


def source_fingerprints(root: Path) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part in {".git", ".sgrx", "node_modules", "target", "dist", "build"} for part in relative.parts):
            continue
        if path.name.lower() in SENSITIVE_NAMES:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            fingerprints[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return fingerprints


def compare_source_trees(left: Path, right: Path) -> dict[str, list[str]]:
    old = source_fingerprints(left)
    new = source_fingerprints(right)
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
    }


def _api_hint(line: str, package: str) -> str | None:
    match = re.search(rf"(?:from\s+['\"]{re.escape(package)}['\"]\s+import\s+|import\s+)([A-Za-z_$][\w$]*)", line)
    return match.group(1) if match else None


def analyze(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    provenance = resolve_dependency(args, runner)
    version_report = doctor(runner)
    provenance["tool_versions"] = _research_tool_versions(version_report["tools"])
    source = Path(provenance["cache_path"]) if provenance.get("cache_path") else None
    indexing = index_sources(args, runner, source)
    evidence = scan_consumer(project, args.package)
    limitations = []
    if not evidence:
        limitations.append("No consumer call site was found by the conservative source-text scan.")
    if provenance["resolution_status"] not in {"RESOLVED", "DRY_RUN"}:
        limitations.append("opensrc did not yield a usable dependency source path.")
    if indexing["status"] in {"UNAVAILABLE", "PARTIAL"}:
        limitations.append("Graphify or GitNexus indexing was unavailable or incomplete; no execution path is claimed.")
    else:
        limitations.append("Indexes were created, but index creation alone does not prove an implementation or runtime path; run evidence-bearing Graphify and GitNexus queries.")
    return {
        "format_version": "1.0",
        "brand": BRAND,
        "question": args.question,
        "short_answer": "SGRX recorded the available source evidence; inspect the evidence and limitations before drawing runtime conclusions.",
        "mode": args.mode,
        "provenance": provenance,
        "consumer_call_sites": evidence,
        "external_implementation": [],
        "end_to_end_path": [],
        "architecture_overview": [],
        "edge_cases": [],
        "deprecations": [],
        "change_risk": {"status": "NOT_REQUESTED", "risk": "UNKNOWN", "direct_callers": [], "processes": []},
        "evidence": evidence,
        "relationships": {status: [row for row in evidence if row["evidence_status"] == status] for status in EVIDENCE_STATUSES},
        "limitations": limitations,
        "recommended_next_steps": ["Use Graphify query/path/explain and GitNexus context/impact outputs to complete the trace without inventing links."],
        "tool_versions": version_report["tools"],
        "commands": command_log(runner.history),
        "indexing": indexing,
        "timestamp": utc_now(),
    }


def compare(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    base = vars(args).copy()
    records = []
    for version in (args.from_version, args.to_version):
        local = argparse.Namespace(**{**base, "ref": version})
        records.append(resolve_dependency(local, runner))
    limitations = []
    if any(item["resolution_status"] not in {"RESOLVED", "DRY_RUN"} for item in records):
        limitations.append("One or both versions could not be resolved; implementation differences are not asserted.")
    differences: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}
    if all(item.get("cache_path") for item in records):
        differences = compare_source_trees(Path(records[0]["cache_path"]), Path(records[1]["cache_path"]))
        limitations.append("File differences are direct source-hash evidence; assess behavioral meaning with isolated Graphify and GitNexus queries.")
    version_report = doctor(runner)
    for record in records:
        record["tool_versions"] = _research_tool_versions(version_report["tools"])
    return {
        "format_version": "1.0",
        "brand": BRAND,
        "question": args.question,
        "package": args.package,
        "from": records[0],
        "to": records[1],
        "differences": differences,
        "evidence": [],
        "limitations": limitations or ["Run source-level Graphify and GitNexus queries for each isolated version before classifying behavioral changes."],
        "tool_versions": version_report["tools"],
        "commands": command_log(runner.history),
        "timestamp": utc_now(),
    }


def _research_tool_versions(tools: dict[str, Any]) -> dict[str, Any]:
    return {name: tools.get(name, {}).get("version") for name in ("opensrc", "graphify", "gitnexus")}


def result_payload(result: CommandResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "args": redact_command(result.args),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "dry_run": result.dry_run,
        "missing": result.missing,
    }


def redact_command(args: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for value in args:
        if hide_next:
            redacted.append("[REDACTED]")
            hide_next = False
            continue
        lower = value.lower()
        if lower in {"--token", "--password", "--secret", "--api-key"}:
            redacted.append(value)
            hide_next = True
        elif re.search(r"(?i)(token|password|secret|api[_-]?key)=", value):
            redacted.append(value.split("=", 1)[0] + "=[REDACTED]")
        else:
            redacted.append(value)
    return redacted


def command_log(results: Iterable[CommandResult]) -> list[dict[str, Any]]:
    return [result_payload(item) for item in results]  # type: ignore[list-item]


def portable_location(value: Any) -> str:
    if not value:
        return "—"
    return str(value).replace("\\", "/")


def markdown_report(data: dict[str, Any]) -> str:
    relationships = data.get("relationships", {status: [] for status in EVIDENCE_STATUSES})
    lines = [
        f"# {BRAND} report",
        "",
        "## Question",
        "",
        str(data.get("question") or "Not supplied"),
        "",
        "## Short answer",
        "",
        str(data.get("short_answer") or "No supported conclusion is available."),
        "",
        "## Analyzed version and provenance",
        "",
        "```json",
        json.dumps(data.get("provenance", {}), indent=2, ensure_ascii=False),
        "```",
        "",
        "## Consumer call sites",
        "",
        evidence_table(data.get("consumer_call_sites", data.get("evidence", []))),
        "",
        "## External implementation",
        "",
        evidence_table(data.get("external_implementation", [])),
        "",
        "## End-to-end path",
        "",
        _list_or_none(data.get("end_to_end_path", [])),
        "",
        "## Architecture overview",
        "",
        _list_or_none(data.get("architecture_overview", [])),
        "",
        "## Edge cases",
        "",
        _list_or_none(data.get("edge_cases", [])),
        "",
        "## Deprecations",
        "",
        _list_or_none(data.get("deprecations", [])),
        "",
        "## Change risk",
        "",
        "```json",
        json.dumps(data.get("change_risk", {}), indent=2, ensure_ascii=False),
        "```",
        "",
        "## Evidence table",
        "",
        evidence_table(data.get("evidence", [])),
    ]
    for status in EVIDENCE_STATUSES:
        lines += ["", f"## {status} relationships", "", evidence_table(relationships.get(status, []))]
    lines += [
        "",
        "## Limitations",
        "",
        _list_or_none(data.get("limitations", [])),
        "",
        "## Recommended next steps",
        "",
        _list_or_none(data.get("recommended_next_steps", [])),
        "",
        "## Tool versions",
        "",
        "```json",
        json.dumps(data.get("tool_versions", {}), indent=2, ensure_ascii=False),
        "```",
        "",
        "## Executed commands",
        "",
        "```json",
        json.dumps(data.get("commands", []), indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(lines)


def _list_or_none(values: Iterable[Any]) -> str:
    values = list(values)
    return "\n".join(f"- {value}" for value in values) if values else "No supported findings."


def evidence_table(rows: Iterable[dict[str, Any]]) -> str:
    rows = list(rows)
    header = "| Consumer | Package | API | Dependency implementation | GitNexus | Graphify relation | Status | Confidence | Uncertainty |\n|---|---|---|---|---|---|---|---:|---|"
    if not rows:
        return header + "\n| — | — | — | — | — | — | AMBIGUOUS | 0.00 | No evidence available |"
    rendered = [header]
    for row in rows:
        status = row.get("evidence_status", "AMBIGUOUS")
        if status not in EVIDENCE_STATUSES:
            raise SGRXError(f"Invalid evidence status: {status}")
        cells = [
            portable_location(row.get("consumer_location")), row.get("package") or "—", row.get("public_api") or "—",
            portable_location(row.get("dependency_location")), row.get("gitnexus_symbol_or_process") or "—",
            row.get("graphify_relationship") or "—", status, f"{float(row.get('confidence', 0.0)):.2f}",
            row.get("uncertainties") or "—",
        ]
        rendered.append("| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
    return "\n".join(rendered)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print and record commands without executing tools.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown or human-readable text.")
    parser.add_argument("--output", help="Write output to this file.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-command timeout in seconds.")
    parser.add_argument("--mode", choices=MODES, default="standard")
    parser.add_argument("--allow-global-graph", action="store_true", help="Allow a merged Graphify graph.")
    parser.add_argument("--allow-gitnexus-group", action="store_true", help="Allow GitNexus group creation.")


def add_dependency(parser: argparse.ArgumentParser, *, project_required: bool = True) -> None:
    parser.add_argument("--package", required=True)
    parser.add_argument("--project", required=project_required, default=None if project_required else ".")
    parser.add_argument("--registry", choices=REGISTRIES, default="npm")
    parser.add_argument("--ref")


def build_parser() -> argparse.ArgumentParser:
    parser = BrandedParser(prog="sgrx.py", description="Orchestrate safe, version-accurate source graph research.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=BrandedParser)
    doctor_parser = sub.add_parser("doctor", help="Check local prerequisites without installing them.")
    add_common(doctor_parser)
    resolve_parser = sub.add_parser("resolve", help="Resolve and fetch exact dependency source with opensrc.")
    add_common(resolve_parser); add_dependency(resolve_parser)
    index_parser = sub.add_parser("index", help="Build isolated Graphify and GitNexus indexes.")
    add_common(index_parser); add_dependency(index_parser)
    index_parser.add_argument("--source-path"); index_parser.add_argument("--version"); index_parser.add_argument("--force", action="store_true")
    analyze_parser = sub.add_parser("analyze", help="Trace consumer usage into dependency source.")
    add_common(analyze_parser); add_dependency(analyze_parser)
    analyze_parser.add_argument("--question", required=True); analyze_parser.add_argument("--source-path"); analyze_parser.add_argument("--force", action="store_true")
    compare_parser = sub.add_parser("compare", help="Resolve two dependency versions for isolated comparison.")
    add_common(compare_parser); add_dependency(compare_parser, project_required=False)
    compare_parser.add_argument("--from", dest="from_version", required=True); compare_parser.add_argument("--to", dest="to_version", required=True); compare_parser.add_argument("--question", required=True)
    report_parser = sub.add_parser("report", help="Render a saved SGRX JSON result as Markdown or normalized JSON.")
    add_common(report_parser); report_parser.add_argument("--input", required=True)
    return parser


def write_output(payload: dict[str, Any], args: argparse.Namespace, *, report: bool = False) -> None:
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n" if args.json else markdown_report(payload)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    else:
        print(content, end="" if content.endswith("\n") else "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runner = CommandRunner(timeout=args.timeout, dry_run=args.dry_run)
        if args.command == "doctor":
            payload = doctor(runner)
        elif args.command == "resolve":
            provenance = resolve_dependency(args, runner)
            version_report = doctor(runner)
            provenance["tool_versions"] = _research_tool_versions(version_report["tools"])
            payload = {"brand": BRAND, "provenance": provenance, "tool_versions": version_report["tools"], "commands": command_log(runner.history)}
        elif args.command == "index":
            payload = {"brand": BRAND, "indexing": index_sources(args, runner), "commands": command_log(runner.history)}
        elif args.command == "analyze":
            payload = analyze(args, runner)
        elif args.command == "compare":
            payload = compare(args, runner)
        else:
            input_path = Path(args.input).expanduser()
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        write_output(payload, args, report=args.command == "report")
        return 0
    except (SGRXError, OSError, json.JSONDecodeError) as exc:
        print(f"SGRX error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
