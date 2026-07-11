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
from typing import Any, Iterable, Mapping, Sequence


BRAND = "SGRX — Source Graph Research eXplorer"
VERSION = "0.2.1"
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

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if isinstance(args, (str, bytes)) or not args or not all(isinstance(x, str) and x for x in args):
            raise SGRXError("Commands must be non-empty argument lists.")
        vector = list(args)
        if self.dry_run:
            result = CommandResult(vector, None, dry_run=True)
            self.history.append(result)
            return result
        executed = _platform_vector(vector)
        process_env = os.environ.copy()
        if env:
            process_env.update({str(key): str(value) for key, value in env.items()})
        try:
            completed = subprocess.run(
                executed,
                cwd=str(cwd) if cwd else None,
                env=process_env,
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
    suffixes = (f"@{version}", f"=={version}") if version else ()
    raw = package if version and package.endswith(suffixes) else f"{package}@{version or 'resolved'}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    return slug[:120] or "dependency-resolved"


def artifact_dir(project: Path, package: str, version: str | None) -> Path:
    return project.resolve() / ".sgrx" / safe_slug(package, version)


def is_sensitive_name(name: str) -> bool:
    lower = name.lower()
    return lower in SENSITIVE_NAMES or lower.startswith(".env.") or lower.endswith((".pem", ".key"))


def source_tree_identity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or is_sensitive_name(path.name):
            continue
        relative = path.relative_to(root)
        if any(part in {".git", ".gitnexus", ".sgrx", "graphify-out", "node_modules", "target"} for part in relative.parts):
            continue
        try:
            stat = path.stat()
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b"\0")
            if stat.st_size <= 2_000_000:
                digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def command_for_opensrc(package: str, registry: str, project: Path, ref: str | None = None) -> list[str]:
    spec = package
    if ref and registry != "github" and not re.search(r"(?:@|==)[^/]+$", package):
        spec += ("==" if registry == "pypi" else "@") + ref
    if ref and registry == "github" and "@" not in package.rsplit("/", 1)[-1]:
        spec += "@" + ref
    return ["opensrc", "path", spec, "--cwd", str(project.resolve())]


def graphify_command(
    source: Path,
    output_scope: Path,
    mode: str,
    *,
    allow_global: bool = False,
    alias: str | None = None,
) -> list[str]:
    command = ["graphify", "extract", str(source.resolve()), "--out", str(output_scope.resolve())]
    if mode == "deep":
        command += ["--mode", "deep"]
    if allow_global:
        command += ["--global", "--as", alias or source.name]
    return command


def gitnexus_command(source: Path, alias: str) -> list[str]:
    return [
        "npx", "--no-install", "gitnexus", "analyze", str(source.resolve()),
        "--index-only", "--skip-git", "--name", alias,
    ]


def gitnexus_env(scope: Path, *, create: bool = True) -> dict[str, str]:
    home = (scope / "gitnexus-home").resolve()
    if create:
        home.mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "USERPROFILE": str(home)}


def _snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    blocked = {".git", ".gitnexus", ".sgrx", "graphify-out", "node_modules", "target", "__pycache__"}
    ignored = {name for name in names if name in blocked or is_sensitive_name(name)}
    base = Path(directory)
    for name in names:
        try:
            if (base / name).is_symlink():
                ignored.add(name)
        except OSError:
            ignored.add(name)
    return ignored


def prepare_source_snapshot(source: Path, scope: Path, identity: str) -> Path:
    snapshot = scope / "gitnexus-source" / identity[:16]
    if snapshot.is_dir():
        return snapshot
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, snapshot, ignore=_snapshot_ignore)
    return snapshot


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


def opensrc_cache_root() -> Path:
    configured = os.environ.get("OPENSRC_CACHE") or os.environ.get("OPENSRC_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".opensrc" / "repos").resolve()


def locate_source(output: str, *, cache_root: Path | None = None) -> Path | None:
    allowed_root = (cache_root or opensrc_cache_root()).resolve()
    for line in reversed(output.splitlines()):
        candidate = line.strip().strip('"\'')
        if candidate.startswith("path:"):
            candidate = candidate[5:].strip()
        path = Path(candidate).expanduser()
        resolved = path.resolve()
        if resolved.is_dir() and resolved.is_relative_to(allowed_root):
            return resolved
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


def base_package_name(package: str, registry: str) -> str:
    base = package
    if package.startswith("@") and package.count("@") > 1:
        base = package.rsplit("@", 1)[0]
    elif registry in {"npm", "crates"} and not package.startswith("@") and "@" in package:
        base = package.rsplit("@", 1)[0]
    elif registry == "pypi" and "==" in package:
        base = package.rsplit("==", 1)[0]
    return base


def _toml_lock_version(text: str, package: str) -> str | None:
    for block in re.split(r"(?m)^\[\[package\]\]\s*$", text)[1:]:
        name = re.search(r'(?m)^name\s*=\s*["\']([^"\']+)["\']', block)
        version = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', block)
        if name and version and name.group(1).lower() == package.lower():
            return version.group(1)
    return None


def lockfile_version(project: Path, package: str, registry: str) -> str | None:
    base = base_package_name(package, registry)
    try:
        if registry == "npm":
            package_lock = project / "package-lock.json"
            if package_lock.is_file():
                data = json.loads(package_lock.read_text(encoding="utf-8"))
                entry = data.get("packages", {}).get(f"node_modules/{base}", {})
                version = entry.get("version")
                if version:
                    return str(version)
            pnpm = project / "pnpm-lock.yaml"
            if pnpm.is_file():
                match = re.search(rf"(?m)^\s{{2,}}/?{re.escape(base)}@([^:\s]+):", pnpm.read_text(encoding="utf-8"))
                if match:
                    return match.group(1)
            yarn = project / "yarn.lock"
            if yarn.is_file():
                text = yarn.read_text(encoding="utf-8")
                pattern = rf'(?ms)^["\']?{re.escape(base)}@[^\n]+:\s*\n\s+version\s+["\']([^"\']+)["\']'
                match = re.search(pattern, text)
                if match:
                    return match.group(1)
            bun = project / "bun.lock"
            if bun.is_file():
                data = json.loads(bun.read_text(encoding="utf-8"))
                value = data.get("packages", {}).get(base)
                if isinstance(value, list) and value:
                    return str(value[0])
        elif registry == "pypi":
            pipfile = project / "Pipfile.lock"
            if pipfile.is_file():
                data = json.loads(pipfile.read_text(encoding="utf-8"))
                for section in ("default", "develop"):
                    value = data.get(section, {}).get(base, {}).get("version")
                    if value:
                        return str(value).removeprefix("==")
            requirements = project / "requirements.txt"
            if requirements.is_file():
                match = re.search(rf"(?im)^\s*{re.escape(base)}\s*==\s*([^\s;#]+)", requirements.read_text(encoding="utf-8"))
                if match:
                    return match.group(1)
            for name in ("uv.lock", "poetry.lock"):
                path = project / name
                if path.is_file():
                    version = _toml_lock_version(path.read_text(encoding="utf-8"), base)
                    if version:
                        return version
        elif registry == "crates":
            cargo = project / "Cargo.lock"
            if cargo.is_file():
                return _toml_lock_version(cargo.read_text(encoding="utf-8"), base)
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None
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
    commit = None
    if source_path and (source_path / ".git").exists():
        commit_result = runner.run(["git", "-C", str(source_path), "rev-parse", "HEAD"])
        if commit_result.ok and re.fullmatch(r"[0-9a-fA-F]{40}", commit_result.stdout.strip()):
            commit = commit_result.stdout.strip().lower()
    return {
        "requested_package": package,
        "registry": args.registry,
        "resolved_version": version,
        "ref": ref,
        "commit": commit,
        "consumer_project": str(project),
        "lockfile": lockfile_for(project, args.registry),
        "cache_path": str(source_path) if source_path else None,
        "timestamp": utc_now(),
        "resolution_status": "DRY_RUN" if result.dry_run else ("RESOLVED" if source_path else "UNRESOLVED"),
        "tool_versions": {"opensrc": None, "graphify": None, "gitnexus": None},
        "tool_result": result_payload(result),
    }


def local_source_provenance(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    source = Path(args.source_path).expanduser().resolve()
    if not project.is_dir() or not source.is_dir():
        raise SGRXError("Consumer project and explicit source path must both be existing directories.")
    package = validate_package(args.package, args.registry)
    ref = validate_ref(args.ref)
    commit = None
    if (source / ".git").exists():
        result = runner.run(["git", "-C", str(source), "rev-parse", "HEAD"])
        if result.ok and re.fullmatch(r"[0-9a-fA-F]{40}", result.stdout.strip()):
            commit = result.stdout.strip().lower()
    return {
        "requested_package": package,
        "registry": args.registry,
        "resolved_version": package_version(package, args.registry, ref) or getattr(args, "version", None),
        "ref": ref,
        "commit": commit,
        "consumer_project": str(project),
        "lockfile": lockfile_for(project, args.registry),
        "cache_path": str(source),
        "timestamp": utc_now(),
        "resolution_status": "LOCAL_SOURCE",
        "tool_versions": {"opensrc": None, "graphify": None, "gitnexus": None},
        "tool_result": None,
    }


def index_sources(args: argparse.Namespace, runner: CommandRunner, source_path: Path | None = None) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    package = validate_package(args.package, args.registry)
    version = (
        getattr(args, "version", None)
        or getattr(args, "ref", None)
        or package_version(package, args.registry, None)
    )
    scope = artifact_dir(project, package, version)
    source = source_path or (Path(args.source_path).expanduser().resolve() if getattr(args, "source_path", None) else None)
    if source is None or (not runner.dry_run and not source.is_dir()):
        return {"status": "UNAVAILABLE", "reason": "No dependency source path was resolved.", "artifact_dir": str(scope), "commands": []}
    alias_base = safe_slug(package, version)
    graph_scope = scope / "graphify"
    if not runner.dry_run:
        scope.mkdir(parents=True, exist_ok=True)
    manifest_path = scope / "index-manifest.json"
    source_identity = "dry-run" if runner.dry_run else source_tree_identity(source)
    alias = f"{alias_base}-{source_identity[:8]}"
    fingerprint = hashlib.sha256(f"{source}|{version}|{args.mode}|{source_identity}".encode()).hexdigest()
    stale_index_detected = False
    if manifest_path.is_file() and not getattr(args, "force", False):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            graph_path = Path(manifest.get("graph_path", ""))
            snapshot_path = Path(manifest.get("gitnexus_source", ""))
            if (
                manifest.get("fingerprint") == fingerprint
                and manifest.get("status") in {"HEALTHY", "DEGRADED"}
                and graph_path.is_file()
                and snapshot_path.is_dir()
            ):
                return {"status": "REUSED", "artifact_dir": str(scope), "manifest": manifest, "commands": [], "stale_index_detected": False}
            stale_index_detected = True
        except (OSError, json.JSONDecodeError):
            stale_index_detected = True
    before = len(runner.history)
    snapshot = source if runner.dry_run else prepare_source_snapshot(source, scope, source_identity)
    nexus_environment = gitnexus_env(scope, create=not runner.dry_run)
    graph = runner.run(
        graphify_command(
            source,
            graph_scope,
            args.mode,
            allow_global=args.allow_global_graph,
            alias=alias,
        ),
        cwd=scope if not runner.dry_run else project,
    )
    nexus = runner.run(
        gitnexus_command(snapshot, alias),
        cwd=snapshot if not runner.dry_run else project,
        env=nexus_environment,
    )
    graph_path = graph_scope / "graphify-out" / "graph.json"
    graph_health = runner.run(
        ["graphify", "diagnose", "multigraph", "--graph", str(graph_path), "--json"],
        cwd=scope if not runner.dry_run else project,
    )
    nexus_status = runner.run(
        ["npx", "--no-install", "gitnexus", "status"],
        cwd=snapshot if not runner.dry_run else project,
        env=nexus_environment,
    )
    nexus_search = runner.run(
        ["npx", "--no-install", "gitnexus", "query", "main", "--repo", alias, "--limit", "1"],
        cwd=snapshot if not runner.dry_run else project,
        env=nexus_environment,
    )
    group_results: list[CommandResult] = []
    if args.allow_gitnexus_group:
        group_results.append(runner.run(
            ["npx", "--no-install", "gitnexus", "group", "create", alias],
            cwd=snapshot if not runner.dry_run else project,
            env=nexus_environment,
        ))
        group_results.append(runner.run(
            ["npx", "--no-install", "gitnexus", "group", "add", alias, "dependency", alias],
            cwd=snapshot if not runner.dry_run else project,
            env=nexus_environment,
        ))
        group_results.append(runner.run(
            ["npx", "--no-install", "gitnexus", "group", "sync", alias, "--skip-embeddings", "--json"],
            cwd=snapshot if not runner.dry_run else project,
            env=nexus_environment,
        ))
    source_unchanged = True if runner.dry_run else source_tree_identity(source) == source_identity
    nexus_index_exists = runner.dry_run or (snapshot / ".gitnexus").is_dir()
    graph_exists = runner.dry_run or graph_path.is_file()
    search_warning = "warning" in (nexus_search.stdout or "").lower()
    group_ok = not args.allow_gitnexus_group or all(result.ok for result in group_results)
    if runner.dry_run:
        status = "DRY_RUN"
    elif not (graph.ok and nexus.ok and graph_exists and nexus_index_exists and source_unchanged and group_ok):
        status = "PARTIAL"
    elif graph_health.ok and nexus_status.ok and nexus_search.ok and not search_warning:
        status = "HEALTHY"
    else:
        status = "DEGRADED"
    health = {
        "graph_exists": graph_exists,
        "graph_diagnostics_ok": graph_health.ok,
        "gitnexus_index_exists": nexus_index_exists,
        "gitnexus_status_ok": nexus_status.ok,
        "gitnexus_search_ok": nexus_search.ok and not search_warning,
        "source_unchanged": source_unchanged,
        "gitnexus_group_ok": group_ok,
    }
    manifest = {
        "fingerprint": fingerprint,
        "status": status,
        "source_path": str(source),
        "graph_path": str(graph_path),
        "gitnexus_source": str(snapshot),
        "gitnexus_alias": alias,
        "gitnexus_home": nexus_environment["HOME"],
        "separate_graphs": not args.allow_global_graph,
        "global_graph_opt_in": bool(args.allow_global_graph),
        "gitnexus_group_opt_in": bool(args.allow_gitnexus_group),
        "timestamp": utc_now(),
        "source_identity": source_identity,
        "stale_index_detected": stale_index_detected,
        "health": health,
        "commands": command_log(runner.history[before:]),
    }
    if not runner.dry_run:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "status": status,
        "artifact_dir": str(scope),
        "manifest": manifest,
        "group": [result_payload(result) for result in group_results],
        "health": health,
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
        if is_sensitive_name(path.name):
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
        if any(part in {".git", ".gitnexus", ".sgrx", "graphify-out", "node_modules", "target"} for part in relative.parts):
            continue
        if is_sensitive_name(path.name):
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
    escaped = re.escape(package)
    patterns = (
        rf"from\s+{escaped}\s+import\s+([A-Za-z_$][\w$]*)",
        rf"import\s+\{{\s*([A-Za-z_$][\w$]*)[^}}]*\}}\s+from\s+['\"]{escaped}['\"]",
        rf"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]{escaped}['\"]",
        rf"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(['\"]{escaped}['\"]\)",
        rf"import\s+([A-Za-z_$][\w$]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(1)
    return None


def _json_output(result: CommandResult) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def graph_query_terms(graph_path: Path, question: str, hints: Iterable[str | None] = ()) -> list[str]:
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    vocabulary: set[str] = set()
    for node in data.get("nodes", []):
        label = str(node.get("label", ""))
        vocabulary.update(token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,29}", label))
    requested = [token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,29}", question)]
    requested.extend(str(hint).lower() for hint in hints if hint)
    return list(dict.fromkeys(token for token in requested if token in vocabulary))[:12]


def research_indexes(
    indexing: dict[str, Any],
    question: str,
    evidence: list[dict[str, Any]],
    runner: CommandRunner,
    mode: str,
) -> dict[str, Any]:
    manifest = indexing.get("manifest") or {}
    graph_path = Path(manifest.get("graph_path", ""))
    alias = manifest.get("gitnexus_alias")
    snapshot = Path(manifest.get("gitnexus_source", ""))
    home = Path(manifest.get("gitnexus_home", ""))
    hints = [row.get("public_api") for row in evidence]
    terms = graph_query_terms(graph_path, question, hints)
    graph_result = None
    if terms and graph_path.is_file():
        graph_result = runner.run(
            ["graphify", "query", " ".join(terms), "--graph", str(graph_path), "--budget", "3000" if mode == "deep" else "1800"],
            cwd=graph_path.parent.parent,
        )
    graph_nodes: list[dict[str, Any]] = []
    if graph_result and graph_result.ok:
        for match in re.finditer(r"(?m)^NODE (.+?) \[src=(.*?) loc=(.*?) community=", graph_result.stdout):
            graph_nodes.append({"label": match.group(1), "source": match.group(2), "location": match.group(3)})
    nexus_result = None
    nexus_payload: dict[str, Any] = {}
    contexts: list[dict[str, Any]] = []
    change_risk: dict[str, Any] = {"status": "NOT_REQUESTED", "risk": "UNKNOWN", "direct_callers": [], "processes": []}
    if alias and snapshot.is_dir() and home:
        environment = {"HOME": str(home), "USERPROFILE": str(home)}
        nexus_result = runner.run(
            [
                "npx", "--no-install", "gitnexus", "query", question,
                "--repo", str(alias), "--context", "Trace the consumer API into exact dependency implementation",
                "--goal", "Return evidence-bearing definitions and execution flows", "--limit", "10",
            ],
            cwd=snapshot,
            env=environment,
        )
        nexus_payload = _json_output(nexus_result)
        context_hints = list(dict.fromkeys(str(item) for item in hints if item))
        if not context_hints:
            for node in graph_nodes:
                candidate = node["label"].removesuffix("()")
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", candidate) and candidate not in {"Path", "Any", "Namespace"}:
                    context_hints.append(candidate)
                if len(context_hints) >= 5:
                    break
        for hint in context_hints[:5]:
            context_result = runner.run(
                ["npx", "--no-install", "gitnexus", "context", hint, "--repo", str(alias)],
                cwd=snapshot,
                env=environment,
            )
            payload = _json_output(context_result)
            if payload:
                contexts.append(payload)
        if re.search(r"(?i)\b(change|modify|rename|break|impact|blast|ändern|änderung|umbenennen)\b", question) and context_hints:
            target = context_hints[0]
            for context in contexts:
                symbol = context.get("symbol", {})
                if symbol.get("name") == target and symbol.get("uid"):
                    target = str(symbol["uid"])
                    break
            impact_result = runner.run(
                ["npx", "--no-install", "gitnexus", "impact", target, "--direction", "upstream", "--depth", "3", "--include-tests", "--repo", str(alias)],
                cwd=snapshot,
                env=environment,
            )
            impact = _json_output(impact_result)
            if impact:
                change_risk = {
                    "status": "ANALYZED",
                    "risk": impact.get("risk", "UNKNOWN"),
                    "direct_callers": impact.get("byDepth", {}).get("1", []),
                    "processes": impact.get("affected_processes", []),
                    "raw": impact,
                }
    return {
        "query_terms": terms,
        "graphify": result_payload(graph_result),
        "graph_nodes": graph_nodes,
        "gitnexus": result_payload(nexus_result),
        "gitnexus_payload": nexus_payload,
        "contexts": contexts,
        "change_risk": change_risk,
    }


def implementation_rows(
    research: dict[str, Any],
    package: str,
    version: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    candidates = list(research.get("gitnexus_payload", {}).get("definitions", []))
    candidates.extend(item.get("symbol", {}) for item in research.get("contexts", []) if item.get("symbol"))
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        name = str(item.get("name") or item.get("label") or "unknown")
        path = str(item.get("filePath") or item.get("file_path") or "")
        line = item.get("startLine") or item.get("line")
        key = (name, path)
        if key in seen:
            continue
        seen.add(key)
        location = f"{package}@{version}:{path}" + (f":{line}" if line else "") if path else None
        rows.append({
            "consumer_location": None,
            "package": package,
            "public_api": name,
            "dependency_location": location,
            "gitnexus_symbol_or_process": name,
            "graphify_relationship": "GitNexus query/context match",
            "evidence_status": "AMBIGUOUS",
            "confidence": 0.45,
            "uncertainties": "The dependency symbol is extracted, but its connection to the consumer call site is not yet directly proven.",
        })
    return rows


def analyze(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    version_report = doctor(runner)
    provenance = local_source_provenance(args, runner) if getattr(args, "source_path", None) else resolve_dependency(args, runner)
    provenance["tool_versions"] = _research_tool_versions(version_report["tools"])
    source = Path(provenance["cache_path"]) if provenance.get("cache_path") else None
    indexing = index_sources(args, runner, source)
    evidence = scan_consumer(project, args.package)
    research = research_indexes(indexing, args.question, evidence, runner, args.mode) if indexing["status"] in {"HEALTHY", "DEGRADED", "REUSED"} else {}
    graph_evidence = [
        {
            "consumer_location": None,
            "package": args.package,
            "public_api": node["label"],
            "dependency_location": f"{args.package}@{provenance.get('resolved_version') or provenance.get('ref') or 'resolved'}:{node['source']}:{node['location']}",
            "gitnexus_symbol_or_process": None,
            "graphify_relationship": "query relevance; not a proven runtime edge",
            "evidence_status": "AMBIGUOUS",
            "confidence": 0.35,
            "uncertainties": "Graph traversal relevance does not prove the consumer-to-dependency runtime path.",
        }
        for node in research.get("graph_nodes", [])[:20]
        if node.get("source")
    ]
    external = implementation_rows(
        research,
        args.package,
        str(provenance.get("resolved_version") or provenance.get("ref") or "resolved"),
    )
    all_evidence = evidence + graph_evidence + external
    limitations = []
    if not evidence:
        limitations.append("No consumer call site was found by the conservative source-text scan.")
    if provenance["resolution_status"] not in {"RESOLVED", "DRY_RUN", "LOCAL_SOURCE"}:
        limitations.append("opensrc did not yield a usable dependency source path.")
    if indexing["status"] in {"UNAVAILABLE", "PARTIAL"}:
        limitations.append("Graphify or GitNexus indexing was unavailable or incomplete; no execution path is claimed.")
    if indexing["status"] == "DEGRADED" or (
        indexing["status"] == "REUSED" and indexing.get("manifest", {}).get("status") == "DEGRADED"
    ):
        limitations.append("At least one index health check is degraded; inspect indexing.health before relying on query completeness.")
    if not research.get("query_terms"):
        limitations.append("The question had no exact overlap with Graphify vocabulary; no graph traversal was fabricated.")
    nexus_payload = research.get("gitnexus_payload", {})
    if nexus_payload.get("warning"):
        limitations.append(f"GitNexus warning: {nexus_payload['warning']}")
    paths = list(nexus_payload.get("processes", []))
    for context in research.get("contexts", []):
        paths.extend(context.get("processes", []))
    return {
        "format_version": "1.0",
        "brand": BRAND,
        "question": args.question,
        "short_answer": "SGRX queried the isolated indexes and classified the available findings; ambiguous graph relevance remains separate from proven runtime paths.",
        "mode": args.mode,
        "provenance": provenance,
        "consumer_call_sites": evidence,
        "external_implementation": external,
        "end_to_end_path": paths,
        "architecture_overview": research.get("graph_nodes", []),
        "edge_cases": [],
        "deprecations": [],
        "change_risk": research.get("change_risk", {"status": "NOT_REQUESTED", "risk": "UNKNOWN", "direct_callers": [], "processes": []}),
        "evidence": all_evidence,
        "relationships": {status: [row for row in all_evidence if row["evidence_status"] == status] for status in EVIDENCE_STATUSES},
        "limitations": limitations,
        "recommended_next_steps": ["Use Graphify query/path/explain and GitNexus context/impact outputs to complete the trace without inventing links."],
        "tool_versions": version_report["tools"],
        "commands": command_log(runner.history),
        "indexing": indexing,
        "research": research,
        "timestamp": utc_now(),
    }


def compare(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    base = vars(args).copy()
    version_report = doctor(runner)
    records = []
    indexes = []
    research = []
    for version in (args.from_version, args.to_version):
        local = argparse.Namespace(**{**base, "ref": version})
        record = resolve_dependency(local, runner)
        records.append(record)
        source = Path(record["cache_path"]) if record.get("cache_path") else None
        indexed = index_sources(local, runner, source)
        indexes.append(indexed)
        research.append(research_indexes(indexed, args.question, [], runner, args.mode) if indexed["status"] in {"HEALTHY", "DEGRADED", "REUSED"} else {})
    limitations = []
    if any(item["resolution_status"] not in {"RESOLVED", "DRY_RUN"} for item in records):
        limitations.append("One or both versions could not be resolved; implementation differences are not asserted.")
    differences: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}
    if all(item.get("cache_path") for item in records):
        differences = compare_source_trees(Path(records[0]["cache_path"]), Path(records[1]["cache_path"]))
        limitations.append("File differences are direct source-hash evidence; assess behavioral meaning with isolated Graphify and GitNexus queries.")
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
        "indexes": indexes,
        "research": research,
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
        "stdout": redact_text(result.stdout),
        "stderr": redact_text(result.stderr),
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


def redact_text(value: str) -> str:
    value = re.sub(
        r"(?im)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=[REDACTED]",
        value,
    )
    return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)


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
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
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
            version_report = doctor(runner)
            provenance = resolve_dependency(args, runner)
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
