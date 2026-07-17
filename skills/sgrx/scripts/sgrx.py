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
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sgrx_research import (  # noqa: E402
    ResearchError,
    build_plan_markdown,
    load_candidates,
    research_slug,
    select_with_budget,
)
from sgrx_audit import (  # noqa: E402
    AUDIT_RECOMMENDATIONS,
    CORPUS_PROFILES,
    audit_checkpoint_signature,
    audit_facets,
    corpus_preflight,
    handoff_markdown,
    prepare_corpus_snapshot,
    verify_report,
)


BRAND = "SGRX — Source Graph Research eXplorer"
VERSION = "0.4.1"
EVIDENCE_STATUSES = ("EXTRACTED", "INFERRED", "AMBIGUOUS")
MODES = ("quick", "standard", "deep")
REGISTRIES = ("npm", "pypi", "crates", "github")
MAX_OUTPUT = 200_000
HASH_CHUNK_SIZE = 1024 * 1024
WINDOWS_LONG_PATH_PATTERN = re.compile(r"(?:filename|path) too long|long path", re.I)
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".mjs", ".py", ".rs", ".ts", ".tsx"}
RESEARCH_CODE_SUFFIXES = SOURCE_SUFFIXES | {".bash", ".fish", ".kt", ".kts", ".php", ".rb", ".scala", ".sh", ".swift", ".vue"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
QUERY_INTENT_EXPANSIONS = {
    "improve": ("analyze", "architecture", "quality", "research", "safety", "validation"),
    "improvement": ("analyze", "architecture", "quality", "research", "safety", "validation"),
    "verbessern": ("analyze", "architecture", "quality", "research", "safety", "validation"),
    "verbesserung": ("analyze", "architecture", "quality", "research", "safety", "validation"),
    "correctness": ("identity", "provenance", "validation"),
    "korrektheit": ("identity", "provenance", "validation"),
    "maintainability": ("architecture", "command", "module"),
    "wartbarkeit": ("architecture", "command", "module"),
    "sicherheit": ("safety", "security", "validation"),
    "testing": ("test", "tests", "validation"),
    "testen": ("test", "tests", "validation"),
}
QUERY_STOPWORDS = {
    "and", "are", "can", "does", "for", "from", "how", "into", "the", "this", "what", "with",
    "das", "der", "die", "ist", "kann", "können", "mit", "und", "was", "wie", "wir",
}
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


def is_windows() -> bool:
    """Return whether SGRX is running on Windows.

    This wrapper avoids mutating interpreter-global ``os.name`` in tests.
    """

    return os.name == "nt"


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
        if is_windows():
            result = self._run_windows_process_tree(executed, cwd, process_env)
            self.history.append(result)
            return result
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

    def _run_windows_process_tree(
        self,
        executed: list[str],
        cwd: Path | None,
        process_env: Mapping[str, str],
    ) -> CommandResult:
        """Terminate the complete SGRX-owned process tree when a Windows tool times out."""
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                executed,
                cwd=str(cwd) if cwd else None,
                env=dict(process_env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            return CommandResult(executed, 127, stderr=f"Tool not found: {executed[0]}", missing=True)
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
            return CommandResult(executed, process.returncode, stdout[: self.max_output], stderr[: self.max_output])
        except subprocess.TimeoutExpired as exc:
            self._terminate_windows_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            stdout = _bounded(stdout or exc.stdout, self.max_output)
            stderr = _bounded(stderr or exc.stderr, self.max_output)
            return CommandResult(executed, None, stdout, stderr, timed_out=True)

    @staticmethod
    def _terminate_windows_process_tree(process: subprocess.Popen[str]) -> None:
        """Kill only the process tree rooted at an SGRX-created child process."""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            process.kill()


def _platform_vector(vector: list[str]) -> list[str]:
    """Adapt Windows PowerShell command shims without enabling a shell."""
    if not is_windows():
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


def _update_digest_from_file(digest: Any, path: Path) -> None:
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    _update_digest_from_file(digest, path)
    return digest.hexdigest()


def source_tree_identity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or is_sensitive_name(path.name):
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
            _update_digest_from_file(digest, path)
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


def gitnexus_env(scope: Path, source: Path | None = None, *, create: bool = True) -> dict[str, str]:
    home = (scope / "gitnexus-home").resolve()
    if create:
        home.mkdir(parents=True, exist_ok=True)
    environment = {"HOME": str(home), "USERPROFILE": str(home)}
    if source is not None:
        # The safe snapshot intentionally has no .git directory. Stop Git from
        # walking into an unrelated parent worktree and reporting its commit.
        environment["GIT_CEILING_DIRECTORIES"] = str(source.resolve().parent)
    return environment


def gitnexus_status_state(result: CommandResult) -> str:
    if not result.ok:
        return "ERROR"
    text = f"{result.stdout}\n{result.stderr}".casefold()
    if "not a git repository" in text:
        return "ISOLATED"
    if re.search(r"(?m)^status:\s*.*stale", text):
        return "STALE"
    if re.search(r"(?m)^status:\s*.*(?:up[ -]?to[ -]?date|healthy|current)", text):
        return "HEALTHY"
    return "UNKNOWN"


def gitnexus_status_ok(result: CommandResult) -> bool:
    return result.ok and gitnexus_status_state(result) in {"HEALTHY", "ISOLATED"}


def gitnexus_fts_missing(result: CommandResult) -> bool:
    return "fts indexes missing" in f"{result.stdout}\n{result.stderr}".casefold()


def graphify_health_issues(result: CommandResult) -> dict[str, Any]:
    """Parse Graphify's structured-enough warnings into an explicit health record."""

    text = f"{result.stdout}\n{result.stderr}"
    zero_match = re.search(r"warning:\s*(\d+) source file\(s\) produced zero nodes", text, re.I)
    extraction_match = re.search(r"Extraction warning \((\d+) issues?\)", text, re.I)
    collision_count = len(re.findall(r"cross-chunk ID collision", text, re.I))
    warnings = [
        line.strip()
        for line in text.splitlines()
        if "warning" in line.casefold() or "collides with node" in line.casefold()
    ]
    zero_node_sources = int(zero_match.group(1)) if zero_match else 0
    extraction_issues = int(extraction_match.group(1)) if extraction_match else 0
    return {
        "zero_node_sources": zero_node_sources,
        "cross_chunk_id_collisions": collision_count,
        "extraction_issues": extraction_issues,
        "data_loss_risk": collision_count > 0,
        "degraded": bool(zero_node_sources or collision_count or extraction_issues),
        "warnings": warnings[:20],
    }


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


def prepare_research_graph_snapshot(source: Path, scope: Path, identity: str) -> tuple[Path, int]:
    """Copy only structurally extractable code for token-efficient research graphs."""
    del scope  # Durable outputs stay scoped; Graphify ignores sources nested under `.sgrx`.
    snapshot = (Path(tempfile.gettempdir()) / "sgrx-research-source" / identity[:24]).resolve()
    if snapshot.is_dir():
        count = sum(1 for path in snapshot.rglob("*") if path.is_file() and path.suffix.lower() in RESEARCH_CODE_SUFFIXES)
        return snapshot, count
    blocked = {".git", ".gitnexus", ".sgrx", "graphify-out", "node_modules", "target", "__pycache__"}
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in RESEARCH_CODE_SUFFIXES:
            continue
        relative = path.relative_to(source)
        if any(part in blocked for part in relative.parts) or is_sensitive_name(path.name):
            continue
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        count += 1
    return snapshot, count


def graphify_token_usage(results: Iterable[CommandResult]) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for result in results:
        if not result.args or "graphify" not in Path(result.args[0]).name.casefold() or "extract" not in result.args:
            continue
        match = re.search(r"tokens:\s*([\d,]+)\s+in\s*/\s*([\d,]+)\s+out", result.stdout, re.I)
        if match:
            input_tokens += int(match.group(1).replace(",", ""))
            output_tokens += int(match.group(2).replace(",", ""))
    return {"input": input_tokens, "output": output_tokens}


def opensrc_checkout_integrity(source: Path, runner: CommandRunner) -> dict[str, Any]:
    """Reject a partially checked out OpenSrc Git cache without changing it."""
    if not source.joinpath(".git").is_dir():
        return {"status": "CACHE_SNAPSHOT", "usable": True, "details": None}
    head = runner.run(["git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"])
    status = runner.run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"])
    if not head.ok or not status.ok:
        return {"status": "UNVERIFIED", "usable": False, "details": "Git could not verify the cached checkout."}
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        return {
            "status": "DIRTY_OR_INCOMPLETE",
            "usable": False,
            "details": f"Git reported {len(changed)} tracked checkout differences; the cache may be incomplete.",
        }
    return {"status": "VERIFIED_CLEAN", "usable": True, "details": None, "commit": head.stdout.strip().lower()}


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
    tools["node"]["meets_minimum"] = bool(match and int(match.group(1)) >= 24) if not runner.dry_run else None
    return {"brand": BRAND, "timestamp": utc_now(), "tools": tools, "commands": command_log(runner.history)}


def install_hint(tool: str) -> str:
    return {
        "node": "Install Node.js 24 or newer from the official Node.js distribution.",
        "git": "Install Git from the official Git distribution.",
        "opensrc": "Install opensrc using its documented CLI installation method.",
        "graphify": "Install Graphify using its documented CLI installation method.",
        "npx": "Install Node.js 24 or newer, which includes npx.",
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


def is_windows_long_path_failure(result: CommandResult) -> bool:
    return is_windows() and not result.ok and bool(WINDOWS_LONG_PATH_PATTERN.search(f"{result.stdout}\n{result.stderr}"))


def windows_long_path_recovery_root(project: Path, command: Sequence[str]) -> Path:
    identity = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()[:16]
    return (Path(tempfile.gettempdir()) / "sgrx-opensrc" / identity).resolve()


def retry_opensrc_with_long_paths(command: Sequence[str], project: Path, runner: CommandRunner) -> tuple[CommandResult, Path | None, Path]:
    """Fetch into a short SGRX-owned cache with Windows long paths enabled per process."""
    cache_root = windows_long_path_recovery_root(project, command)
    environment = {
        "OPENSRC_HOME": str(cache_root),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.longpaths",
        "GIT_CONFIG_VALUE_0": "true",
    }
    result = runner.run(command, cwd=project, env=environment)
    return result, (locate_source(result.stdout, cache_root=cache_root) if result.ok else None), cache_root


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
    attempts = [result_payload(result)]
    recovery: dict[str, Any] | None = None
    if is_windows_long_path_failure(result):
        retry, recovered_source, cache_root = retry_opensrc_with_long_paths(command, project, runner)
        attempts.append(result_payload(retry))
        result = retry
        source_path = recovered_source
        recovery = {
            "reason": "windows_long_path",
            "cache_root": str(cache_root),
            "succeeded": recovered_source is not None,
        }
    version = package_version(package, args.registry, ref) or lockfile_version(project, package, args.registry)
    commit = ref.lower() if args.registry == "github" and ref and re.fullmatch(r"[0-9a-fA-F]{40}", ref) else None
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
        "resolution_status": "DRY_RUN" if result.dry_run else ("RESOLVED_LONG_PATH_RECOVERY" if recovery and source_path else ("RESOLVED" if source_path else "UNRESOLVED")),
        "tool_versions": {"opensrc": None, "graphify": None, "gitnexus": None},
        "tool_result": result_payload(result),
        "attempts": attempts,
        "recovery": recovery,
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


def _run_isolated_index(
    *,
    role: str,
    source: Path,
    identity: str,
    alias: str,
    scope: Path,
    project: Path,
    mode: str,
    allow_global_graph: bool,
    corpus_profile: str,
    token_budget: int,
    max_files: int,
    max_images: int,
    include_paths: Sequence[str],
    exclude_paths: Sequence[str],
    runner: CommandRunner,
) -> dict[str, Any]:
    role_scope = scope if role == "dependency" else scope / role
    graph_scope = role_scope / "graphify" / identity[:16]
    snapshot = source if runner.dry_run else prepare_source_snapshot(source, role_scope, identity)
    environment = gitnexus_env(scope, snapshot, create=not runner.dry_run)
    if runner.dry_run and not source.is_dir():
        preflight = {
            "status": "WITHIN_BUDGET",
            "profile": corpus_profile,
            "root": str(source.resolve()),
            "counts": {},
            "selected_counts": {},
            "total_files": 0,
            "selected_files": 0,
            "excluded_files": 0,
            "selected_bytes": 0,
            "estimated_tokens": 0,
            "limits": {"token_budget": token_budget, "max_files": max_files, "max_images": max_images},
            "filters": {"include_paths": list(include_paths), "exclude_paths": list(exclude_paths)},
            "violations": [],
            "planned": True,
        }
    else:
        try:
            preflight = corpus_preflight(
                source,
                profile=corpus_profile,
                token_budget=token_budget,
                max_files=max_files,
                max_images=max_images,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
            )
        except ValueError as exc:
            raise SGRXError(str(exc)) from exc
    graph_source = source
    temporary_graph_root: Path | None = None
    if corpus_profile != "full":
        if runner.dry_run:
            graph_source = role_scope / "graphify-source" / identity[:16]
        elif preflight["status"] == "WITHIN_BUDGET":
            temporary_graph_root = Path(tempfile.mkdtemp(prefix=f"sgrx-graphify-{role}-"))
            graph_source = temporary_graph_root / "source"
            prepare_corpus_snapshot(
                source,
                graph_source,
                corpus_profile,
                include_paths=include_paths,
                exclude_paths=exclude_paths,
            )
    graph_path = graph_scope / "graphify-out" / "graph.json"
    if preflight["status"] != "WITHIN_BUDGET":
        source_unchanged = True if runner.dry_run else source_tree_identity(source) == identity
        return {
            "role": role,
            "status": "PARTIAL",
            "source_path": str(source),
            "source_identity": identity,
            "graph_path": str(graph_path),
            "graph_source": str(graph_source),
            "gitnexus_source": str(snapshot),
            "gitnexus_alias": alias,
            "gitnexus_home": environment["HOME"],
            "preflight": preflight,
            "graphify_issues": {},
            "gitnexus_recovery": {"attempted": False, "succeeded": False},
            "health": {
                "preflight_ok": False,
                "graph_exists": False,
                "graph_diagnostics_ok": False,
                "gitnexus_index_exists": False,
                "gitnexus_status_ok": False,
                "gitnexus_status_state": "NOT_RUN",
                "gitnexus_search_ok": False,
                "source_unchanged": source_unchanged,
            },
        }
    graph = runner.run(
        graphify_command(graph_source, graph_scope, mode, allow_global=allow_global_graph, alias=alias),
        cwd=scope if not runner.dry_run else project,
    )
    observed_tokens = graphify_token_usage([graph])
    preflight["observed_graphify_tokens"] = observed_tokens
    preflight["observed_exceeds_budget"] = token_budget > 0 and observed_tokens["input"] > token_budget
    if temporary_graph_root is not None:
        shutil.rmtree(temporary_graph_root, ignore_errors=True)
    nexus = runner.run(
        gitnexus_command(snapshot, alias),
        cwd=snapshot if not runner.dry_run else project,
        env=environment,
    )
    graph_health = runner.run(
        ["graphify", "diagnose", "multigraph", "--graph", str(graph_path), "--json"],
        cwd=scope if not runner.dry_run else project,
    )
    nexus_status = runner.run(
        ["npx", "--no-install", "gitnexus", "status"],
        cwd=snapshot if not runner.dry_run else project,
        env=environment,
    )
    nexus_search = runner.run(
        ["npx", "--no-install", "gitnexus", "query", "main", "--repo", alias, "--limit", "1"],
        cwd=snapshot if not runner.dry_run else project,
        env=environment,
    )
    recovery = {"attempted": False, "succeeded": False}
    if not runner.dry_run and gitnexus_fts_missing(nexus_search):
        recovery["attempted"] = True
        repair = runner.run(
            [*gitnexus_command(snapshot, alias), "--force"],
            cwd=snapshot,
            env=environment,
        )
        if repair.ok:
            nexus_status = runner.run(
                ["npx", "--no-install", "gitnexus", "status"],
                cwd=snapshot,
                env=environment,
            )
            nexus_search = runner.run(
                ["npx", "--no-install", "gitnexus", "query", "main", "--repo", alias, "--limit", "1"],
                cwd=snapshot,
                env=environment,
            )
            recovery["succeeded"] = nexus_search.ok and not gitnexus_fts_missing(nexus_search)
    source_unchanged = True if runner.dry_run else source_tree_identity(source) == identity
    nexus_index_exists = runner.dry_run or (snapshot / ".gitnexus").is_dir()
    graph_exists = runner.dry_run or graph_path.is_file()
    search_warning = "warning" in f"{nexus_search.stdout}\n{nexus_search.stderr}".casefold()
    graph_issues = graphify_health_issues(graph)
    if preflight["observed_exceeds_budget"]:
        graph_issues["degraded"] = True
        graph_issues["warnings"].append(
            f"Observed Graphify input {observed_tokens['input']} exceeded budget {token_budget}."
        )
    graph_diagnostics_ok = graph_health.ok and not graph_issues["degraded"]
    status_state = "DRY_RUN" if runner.dry_run else gitnexus_status_state(nexus_status)
    status_ok = True if runner.dry_run else gitnexus_status_ok(nexus_status)
    if runner.dry_run:
        status = "DRY_RUN"
    elif not (graph.ok and nexus.ok and graph_exists and nexus_index_exists and source_unchanged):
        status = "PARTIAL"
    elif graph_diagnostics_ok and status_ok and nexus_search.ok and not search_warning:
        status = "HEALTHY"
    else:
        status = "DEGRADED"
    return {
        "role": role,
        "status": status,
        "source_path": str(source),
        "source_identity": identity,
        "graph_path": str(graph_path),
        "graph_source": str(graph_source),
        "gitnexus_source": str(snapshot),
        "gitnexus_alias": alias,
        "gitnexus_home": environment["HOME"],
        "preflight": preflight,
        "graphify_issues": graph_issues,
        "gitnexus_recovery": recovery,
        "health": {
            "preflight_ok": True,
            "graph_exists": graph_exists,
            "graph_diagnostics_ok": graph_diagnostics_ok,
            "gitnexus_index_exists": nexus_index_exists,
            "gitnexus_status_ok": status_ok,
            "gitnexus_status_state": status_state,
            "gitnexus_search_ok": nexus_search.ok and not search_warning,
            "source_unchanged": source_unchanged,
        },
    }


def index_sources(args: argparse.Namespace, runner: CommandRunner, source_path: Path | None = None) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    package = validate_package(args.package, args.registry)
    version = getattr(args, "version", None) or getattr(args, "ref", None) or package_version(package, args.registry, None)
    corpus_profile = getattr(args, "corpus_profile", "full")
    token_budget = int(getattr(args, "token_budget", 0) or 0)
    max_files = int(getattr(args, "max_files", 0) or 0)
    max_images = int(getattr(args, "max_images", -1))
    include_paths = tuple(getattr(args, "include_path", None) or ())
    exclude_paths = tuple(getattr(args, "exclude_path", None) or ())
    if corpus_profile not in CORPUS_PROFILES:
        raise SGRXError(f"Unsupported corpus profile: {corpus_profile}")
    if token_budget < 0 or max_files < 0 or max_images < -1:
        raise SGRXError("Corpus limits must be non-negative; max-images may be -1 for unlimited.")
    scope = artifact_dir(project, package, version)
    source = source_path or (Path(args.source_path).expanduser().resolve() if getattr(args, "source_path", None) else None)
    if source is None or (not runner.dry_run and not source.is_dir()):
        return {"status": "UNAVAILABLE", "reason": "No dependency source path was resolved.", "artifact_dir": str(scope), "commands": []}
    if not runner.dry_run and not project.is_dir():
        return {"status": "UNAVAILABLE", "reason": "Consumer project does not exist.", "artifact_dir": str(scope), "commands": []}
    if not runner.dry_run:
        scope.mkdir(parents=True, exist_ok=True)
    manifest_path = scope / "index-manifest.json"
    dependency_identity = "dry-run" if runner.dry_run else source_tree_identity(source)
    consumer_identity = dependency_identity if source == project else ("dry-run" if runner.dry_run else source_tree_identity(project))
    shared_consumer_index = source == project
    alias_base = safe_slug(package, version)
    dependency_alias = f"{alias_base}-{dependency_identity[:8]}"
    consumer_alias = dependency_alias if shared_consumer_index else f"consumer-{safe_slug(project.name, None)}-{consumer_identity[:8]}"
    fingerprint = hashlib.sha256(
        (
            f"{source}|{project}|{version}|{args.mode}|{dependency_identity}|{consumer_identity}|"
            f"{corpus_profile}|{token_budget}|{max_files}|{max_images}|{include_paths}|{exclude_paths}"
        ).encode()
    ).hexdigest()
    stale_index_detected = False
    if manifest_path.is_file() and not getattr(args, "force", False):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            required_paths = [
                Path(manifest.get("graph_path", "")),
                Path(manifest.get("gitnexus_source", "")),
                Path(manifest.get("consumer_graph_path", "")),
                Path(manifest.get("consumer_gitnexus_source", "")),
            ]
            if (
                manifest.get("fingerprint") == fingerprint
                and manifest.get("status") in {"HEALTHY", "DEGRADED"}
                and required_paths[0].is_file()
                and required_paths[1].is_dir()
                and required_paths[2].is_file()
                and required_paths[3].is_dir()
            ):
                return {"status": "REUSED", "artifact_dir": str(scope), "manifest": manifest, "commands": [], "stale_index_detected": False}
            stale_index_detected = True
        except (OSError, json.JSONDecodeError):
            stale_index_detected = True
    before = len(runner.history)
    dependency = _run_isolated_index(
        role="dependency", source=source, identity=dependency_identity, alias=dependency_alias,
        scope=scope, project=project, mode=args.mode, allow_global_graph=args.allow_global_graph,
        corpus_profile=corpus_profile, token_budget=token_budget, max_files=max_files, max_images=max_images,
        include_paths=include_paths, exclude_paths=exclude_paths,
        runner=runner,
    )
    consumer = dependency if shared_consumer_index else _run_isolated_index(
        role="consumer", source=project, identity=consumer_identity, alias=consumer_alias,
        scope=scope, project=project, mode=args.mode, allow_global_graph=args.allow_global_graph,
        corpus_profile=corpus_profile, token_budget=token_budget, max_files=max_files, max_images=max_images,
        include_paths=include_paths, exclude_paths=exclude_paths,
        runner=runner,
    )
    group_alias = f"{alias_base}-group"
    group_results: list[CommandResult] = []
    group_environment = gitnexus_env(scope, Path(dependency["gitnexus_source"]), create=not runner.dry_run)
    group_cwd = Path(dependency["gitnexus_source"]) if not runner.dry_run else project
    indexes_usable_for_group = dependency["status"] != "PARTIAL" and consumer["status"] != "PARTIAL"
    if args.allow_gitnexus_group and indexes_usable_for_group:
        group_results.append(runner.run(
            ["npx", "--no-install", "gitnexus", "group", "create", group_alias], cwd=group_cwd, env=group_environment,
        ))
        if not shared_consumer_index:
            group_results.append(runner.run(
                ["npx", "--no-install", "gitnexus", "group", "add", group_alias, "consumer", consumer_alias],
                cwd=group_cwd, env=group_environment,
            ))
        group_results.append(runner.run(
            ["npx", "--no-install", "gitnexus", "group", "add", group_alias, "dependency", dependency_alias],
            cwd=group_cwd, env=group_environment,
        ))
        group_results.append(runner.run(
            ["npx", "--no-install", "gitnexus", "group", "sync", group_alias, "--skip-embeddings", "--json"],
            cwd=group_cwd, env=group_environment,
        ))
    group_ok = not args.allow_gitnexus_group or (indexes_usable_for_group and all(result.ok for result in group_results))
    role_statuses = {dependency["status"], consumer["status"]}
    if runner.dry_run:
        status = "DRY_RUN"
    elif "PARTIAL" in role_statuses or not group_ok:
        status = "PARTIAL"
    elif role_statuses == {"HEALTHY"}:
        status = "HEALTHY"
    else:
        status = "DEGRADED"
    health = {
        **dependency["health"],
        "consumer": consumer["health"],
        "dependency": dependency["health"],
        "consumer_index_shared": shared_consumer_index,
        "gitnexus_group_ok": group_ok,
    }
    manifest = {
        "fingerprint": fingerprint,
        "status": status,
        "source_path": dependency["source_path"],
        "graph_path": dependency["graph_path"],
        "gitnexus_source": dependency["gitnexus_source"],
        "gitnexus_alias": dependency_alias,
        "gitnexus_home": dependency["gitnexus_home"],
        "consumer_source_path": consumer["source_path"],
        "consumer_graph_path": consumer["graph_path"],
        "consumer_gitnexus_source": consumer["gitnexus_source"],
        "consumer_gitnexus_alias": consumer_alias,
        "consumer_index_shared": shared_consumer_index,
        "separate_graphs": not args.allow_global_graph and not shared_consumer_index,
        "global_graph_opt_in": bool(args.allow_global_graph),
        "gitnexus_group_opt_in": bool(args.allow_gitnexus_group),
        "gitnexus_group_alias": group_alias if args.allow_gitnexus_group else None,
        "timestamp": utc_now(),
        "source_identity": dependency_identity,
        "consumer_source_identity": consumer_identity,
        "corpus": {"dependency": dependency.get("preflight", {}), "consumer": consumer.get("preflight", {})},
        "stale_index_detected": stale_index_detected,
        "health": health,
        "indexes": {"consumer": consumer, "dependency": dependency},
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
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(root)
        if any(part in {".git", ".gitnexus", ".sgrx", "graphify-out", "node_modules", "target"} for part in relative.parts):
            continue
        if is_sensitive_name(path.name):
            continue
        try:
            fingerprints[relative.as_posix()] = file_sha256(path)
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


def comparison_evidence(
    differences: Mapping[str, Sequence[str]],
    from_version: str,
    to_version: str,
    research: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    evidence = [
        {
            "kind": "source_file_difference",
            "change": change,
            "path": path,
            "from_version": from_version,
            "to_version": to_version,
            "evidence_status": "EXTRACTED",
            "confidence": 1.0,
            "uncertainties": "This proves a source-content difference, not a behavioral change.",
        }
        for change in ("added", "removed", "changed")
        for path in differences.get(change, [])
    ]
    if len(research) == 2:
        old_nodes = {str(node.get("label")) for node in research[0].get("graph_nodes", []) if node.get("label")}
        new_nodes = {str(node.get("label")) for node in research[1].get("graph_nodes", []) if node.get("label")}
        for change, labels in (("graph_node_added", new_nodes - old_nodes), ("graph_node_removed", old_nodes - new_nodes)):
            evidence.extend({
                "kind": "graph_query_difference",
                "change": change,
                "label": label,
                "from_version": from_version,
                "to_version": to_version,
                "evidence_status": "AMBIGUOUS",
                "confidence": 0.4,
                "uncertainties": "Query-result presence is relevance evidence and does not prove an API or runtime change.",
            } for label in sorted(labels))
    return evidence


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


def _json_output_with_error(result: CommandResult) -> tuple[dict[str, Any], str | None]:
    if not result.stdout.strip():
        return {}, "tool returned no JSON output"
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON output at line {exc.lineno}, column {exc.colno}"
    if not isinstance(value, dict):
        return {}, "JSON output was not an object"
    return value, None


def _json_output(result: CommandResult) -> dict[str, Any]:
    return _json_output_with_error(result)[0]


def _graph_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for word in re.findall(r"[^\W\d_]+", value, re.UNICODE):
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", word) or [word]
        tokens.extend(part.lower() for part in parts if 3 <= len(part) <= 30)
    return tokens


def graph_query_terms(graph_path: Path, question: str, hints: Iterable[str | None] = ()) -> list[str]:
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    vocabulary: set[str] = set()
    for node in data.get("nodes", []):
        vocabulary.update(_graph_tokens(str(node.get("label", ""))))
    requested = [token for token in _graph_tokens(question) if token not in QUERY_STOPWORDS]
    for hint in hints:
        if hint:
            requested.extend(token for token in _graph_tokens(str(hint)) if token not in QUERY_STOPWORDS)
    expanded = list(requested)
    for token in requested:
        expanded.extend(QUERY_INTENT_EXPANSIONS.get(token, ()))
    return list(dict.fromkeys(token for token in expanded if token in vocabulary))[:12]


def _parse_graph_query_nodes(result: CommandResult | None) -> tuple[list[dict[str, Any]], str | None]:
    if result is None or not result.ok:
        return [], None
    nodes = [
        {"label": match.group(1), "source": match.group(2), "location": match.group(3)}
        for match in re.finditer(r"(?m)^NODE (.+?) \[src=(.*?) loc=(.*?) community=", result.stdout)
    ]
    if result.stdout.strip() and not nodes:
        return [], "Graphify query output contained no parseable structured node lines."
    return nodes, None


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
    consumer_graph_path = Path(manifest.get("consumer_graph_path", ""))
    consumer_alias = manifest.get("consumer_gitnexus_alias")
    consumer_snapshot = Path(manifest.get("consumer_gitnexus_source", ""))
    consumer_shared = bool(manifest.get("consumer_index_shared"))
    hints = [row.get("public_api") for row in evidence]
    terms = graph_query_terms(graph_path, question, hints)
    consumer_terms = terms if consumer_shared else graph_query_terms(consumer_graph_path, question, hints)
    graph_result = None
    consumer_graph_result = None
    parse_errors: list[str] = []
    if terms and graph_path.is_file():
        graph_result = runner.run(
            ["graphify", "query", " ".join(terms), "--graph", str(graph_path), "--budget", "3000" if mode == "deep" else "1800"],
            cwd=graph_path.parent.parent,
        )
    if not consumer_shared and consumer_terms and consumer_graph_path.is_file():
        consumer_graph_result = runner.run(
            ["graphify", "query", " ".join(consumer_terms), "--graph", str(consumer_graph_path), "--budget", "3000" if mode == "deep" else "1800"],
            cwd=consumer_graph_path.parent.parent,
        )
    graph_nodes, graph_error = _parse_graph_query_nodes(graph_result)
    consumer_graph_nodes, consumer_graph_error = (
        (graph_nodes, graph_error) if consumer_shared else _parse_graph_query_nodes(consumer_graph_result)
    )
    if graph_error:
        parse_errors.append(f"Dependency {graph_error}")
    if consumer_graph_error:
        parse_errors.append(f"Consumer {consumer_graph_error}")
    nexus_result = None
    consumer_nexus_result = None
    group_result = None
    nexus_payload: dict[str, Any] = {}
    consumer_nexus_payload: dict[str, Any] = {}
    group_payload: dict[str, Any] = {}
    contexts: list[dict[str, Any]] = []
    change_risk: dict[str, Any] = {"status": "NOT_REQUESTED", "risk": "UNKNOWN", "direct_callers": [], "processes": []}
    if alias and snapshot.is_dir() and home:
        environment = gitnexus_env(home.parent, snapshot, create=False)
        nexus_result = runner.run(
            [
                "npx", "--no-install", "gitnexus", "query", question,
                "--repo", str(alias), "--context", "Trace the consumer API into exact dependency implementation",
                "--goal", "Return evidence-bearing definitions and execution flows", "--limit", "10",
            ],
            cwd=snapshot,
            env=environment,
        )
        nexus_payload, nexus_error = _json_output_with_error(nexus_result)
        if nexus_error:
            parse_errors.append(f"GitNexus query: {nexus_error}.")
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
            payload, context_error = _json_output_with_error(context_result)
            if payload:
                contexts.append(payload)
            elif context_error:
                parse_errors.append(f"GitNexus context {hint}: {context_error}.")
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
            impact, impact_error = _json_output_with_error(impact_result)
            if impact:
                change_risk = {
                    "status": "ANALYZED",
                    "risk": impact.get("risk", "UNKNOWN"),
                    "direct_callers": impact.get("byDepth", {}).get("1", []),
                    "processes": impact.get("affected_processes", []),
                    "raw": impact,
                }
            elif impact_error:
                parse_errors.append(f"GitNexus impact {target}: {impact_error}.")
        if not consumer_shared and consumer_alias and consumer_snapshot.is_dir():
            consumer_environment = gitnexus_env(home.parent, consumer_snapshot, create=False)
            consumer_nexus_result = runner.run(
                [
                    "npx", "--no-install", "gitnexus", "query", question,
                    "--repo", str(consumer_alias), "--context", "Find consumer imports, wrappers, calls, and tests",
                    "--goal", "Return evidence-bearing consumer definitions and execution flows", "--limit", "10",
                ],
                cwd=consumer_snapshot,
                env=consumer_environment,
            )
            consumer_nexus_payload, consumer_error = _json_output_with_error(consumer_nexus_result)
            if consumer_error:
                parse_errors.append(f"Consumer GitNexus query: {consumer_error}.")
        group_alias = manifest.get("gitnexus_group_alias")
        if manifest.get("gitnexus_group_opt_in") and group_alias:
            group_result = runner.run(
                ["npx", "--no-install", "gitnexus", "group", "query", str(group_alias), question, "--limit", "10", "--json"],
                cwd=snapshot,
                env=environment,
            )
            group_payload, group_error = _json_output_with_error(group_result)
            if group_error:
                parse_errors.append(f"GitNexus group query: {group_error}.")
    return {
        "query_terms": terms,
        "consumer_query_terms": consumer_terms,
        "consumer_index_shared": consumer_shared,
        "graphify": result_payload(graph_result),
        "graph_nodes": graph_nodes,
        "consumer_graphify": result_payload(graph_result if consumer_shared else consumer_graph_result),
        "consumer_graph_nodes": consumer_graph_nodes,
        "gitnexus": result_payload(nexus_result),
        "gitnexus_payload": nexus_payload,
        "consumer_gitnexus": result_payload(nexus_result if consumer_shared else consumer_nexus_result),
        "consumer_gitnexus_payload": nexus_payload if consumer_shared else consumer_nexus_payload,
        "gitnexus_group": result_payload(group_result),
        "gitnexus_group_payload": group_payload,
        "contexts": contexts,
        "change_risk": change_risk,
        "parse_errors": parse_errors,
    }


def _write_json_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _audit_event(scope: Path, event: str, **details: Any) -> None:
    scope.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": utc_now(), "event": event, **details}
    with (scope / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def audit_graph_facets(
    indexing: Mapping[str, Any],
    question: str,
    runner: CommandRunner,
    *,
    mode: str,
    facet_budget: int,
    force: bool,
) -> dict[str, Any]:
    """Run small, explicit Graphify questions and checkpoint their results."""

    manifest = indexing.get("manifest", {})
    dependency_graph = Path(str(manifest.get("graph_path", "")))
    consumer_graph = Path(str(manifest.get("consumer_graph_path", "")))
    scope = Path(str(indexing.get("artifact_dir", "."))) / "audit" / safe_slug(question, None)
    checkpoint_path = scope / "04-query-results.json"
    signature = audit_checkpoint_signature({
        "question": question,
        "mode": mode,
        "facet_budget": facet_budget,
        "source_identity": manifest.get("source_identity"),
        "consumer_source_identity": manifest.get("consumer_source_identity"),
        "facets": audit_facets(question),
        "sgrx_version": VERSION,
    })
    if not runner.dry_run and not force and checkpoint_path.is_file() and dependency_graph.is_file() and consumer_graph.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("signature") == signature and isinstance(checkpoint.get("facets"), dict):
                _audit_event(scope, "checkpoint_reused", path=str(checkpoint_path))
                return {
                    "facets": checkpoint["facets"],
                    "checkpoint": {"status": "REUSED", "path": str(checkpoint_path), "signature": signature},
                    "scope": str(scope),
                }
        except (OSError, json.JSONDecodeError):
            pass

    facets: dict[str, Any] = {}
    for name, facet_question in audit_facets(question).items():
        dependency_terms = graph_query_terms(dependency_graph, facet_question)
        consumer_terms = graph_query_terms(consumer_graph, facet_question)
        if runner.dry_run and not dependency_terms:
            dependency_terms = list(dict.fromkeys(token for token in _graph_tokens(facet_question) if token not in QUERY_STOPWORDS))[:12]
        if runner.dry_run and not consumer_terms:
            consumer_terms = list(dependency_terms)
        dependency_result = None
        consumer_result = None
        if dependency_terms and (runner.dry_run or dependency_graph.is_file()):
            dependency_result = runner.run(
                ["graphify", "query", " ".join(dependency_terms), "--graph", str(dependency_graph), "--budget", str(facet_budget)],
                cwd=dependency_graph.parent.parent if dependency_graph.is_file() else Path(args_project(indexing)),
            )
        if consumer_terms and (runner.dry_run or consumer_graph.is_file()):
            consumer_result = runner.run(
                ["graphify", "query", " ".join(consumer_terms), "--graph", str(consumer_graph), "--budget", str(facet_budget)],
                cwd=consumer_graph.parent.parent if consumer_graph.is_file() else Path(args_project(indexing)),
            )
        dependency_nodes, dependency_error = _parse_graph_query_nodes(dependency_result)
        consumer_nodes, consumer_error = _parse_graph_query_nodes(consumer_result)
        requested_tokens = set(_graph_tokens(facet_question)) - QUERY_STOPWORDS
        facets[name] = {
            "question": facet_question,
            "dependency_terms": dependency_terms,
            "consumer_terms": consumer_terms,
            "dependency_coverage": round(len(dependency_terms) / max(1, len(requested_tokens)), 4),
            "consumer_coverage": round(len(consumer_terms) / max(1, len(requested_tokens)), 4),
            "dependency_nodes": dependency_nodes[:40],
            "consumer_nodes": consumer_nodes[:40],
            "errors": [error for error in (dependency_error, consumer_error) if error],
            "commands": [item for item in (result_payload(dependency_result), result_payload(consumer_result)) if item],
        }
    checkpoint = {"signature": signature, "saved_at": utc_now(), "facets": facets}
    if not runner.dry_run:
        _write_json_artifact(checkpoint_path, checkpoint)
        _audit_event(scope, "queries_completed", facets=len(facets), path=str(checkpoint_path))
    return {
        "facets": facets,
        "checkpoint": {"status": "DRY_RUN" if runner.dry_run else "SAVED", "path": str(checkpoint_path), "signature": signature},
        "scope": str(scope),
    }


def args_project(indexing: Mapping[str, Any]) -> str:
    manifest = indexing.get("manifest", {})
    return str(manifest.get("consumer_source_path") or manifest.get("source_path") or ".")


def audit_practice_mappings(
    facets: Mapping[str, Any],
    package: str,
    version: str,
) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for name, facet in facets.items():
        dependency_nodes = list(facet.get("dependency_nodes", []))
        consumer_nodes = list(facet.get("consumer_nodes", []))
        dependency = dependency_nodes[0] if dependency_nodes else {}
        consumer = consumer_nodes[0] if consumer_nodes else {}
        status = "INFERRED" if dependency and consumer else "AMBIGUOUS"
        dependency_location = None
        if dependency.get("source"):
            dependency_location = f"{package}@{version}:{dependency['source']}:{dependency.get('location') or 'unknown'}"
        consumer_location = None
        if consumer.get("source"):
            consumer_location = f"{consumer['source']}:{consumer.get('location') or 'unknown'}"
        mappings.append({
            "facet": name,
            "practice": dependency.get("label") or f"{name} practice not located",
            "consumer_equivalent": consumer.get("label") or None,
            "recommendation": AUDIT_RECOMMENDATIONS[name],
            "gap": (
                "Both graphs contain relevant structures; verify semantic equivalence before implementation."
                if dependency and consumer
                else "One side has no graph evidence for this facet; retain the recommendation as a hypothesis."
            ),
            "consumer_location": consumer_location,
            "package": package,
            "public_api": dependency.get("label") or name,
            "dependency_location": dependency_location,
            "gitnexus_symbol_or_process": None,
            "graphify_relationship": f"faceted {name} relevance; cross-repository transfer is inferred",
            "evidence_status": status,
            "confidence": 0.76 if status == "INFERRED" else 0.42,
            "uncertainties": "Graph relevance does not prove that adopting the practice improves SGRX.",
        })
    return mappings


def audit_index_context(
    indexing: Mapping[str, Any],
    question: str,
    runner: CommandRunner,
    *,
    mode: str,
    force: bool,
    facet_result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Checkpoint the broader Graphify/GitNexus context used by an audit."""

    scope = Path(str(facet_result["scope"]))
    path = scope / "04-index-context.json"
    signature = str(facet_result.get("checkpoint", {}).get("signature") or "")
    if not runner.dry_run and not force and signature and path.is_file():
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            if checkpoint.get("signature") == signature and isinstance(checkpoint.get("research"), dict):
                _audit_event(scope, "checkpoint_reused", path=str(path))
                return checkpoint["research"], {"status": "REUSED", "path": str(path), "signature": signature}
        except (OSError, json.JSONDecodeError):
            pass
    research = research_indexes(dict(indexing), question, [], runner, mode)
    if not runner.dry_run:
        _write_json_artifact(path, {"signature": signature, "saved_at": utc_now(), "research": research})
        _audit_event(scope, "index_context_completed", path=str(path))
    return research, {
        "status": "DRY_RUN" if runner.dry_run else "SAVED",
        "path": str(path),
        "signature": signature,
    }


def analysis_index_research(
    indexing: Mapping[str, Any],
    question: str,
    evidence: Sequence[Mapping[str, Any]],
    runner: CommandRunner,
    *,
    mode: str,
    force: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Checkpoint analyze-mode graph and symbol queries across identical runs."""

    manifest = indexing.get("manifest", {})
    scope = Path(str(indexing.get("artifact_dir", "."))) / "analyze" / safe_slug(question, None)
    path = scope / "04-query-results.json"
    signature = audit_checkpoint_signature({
        "question": question,
        "mode": mode,
        "source_identity": manifest.get("source_identity"),
        "consumer_source_identity": manifest.get("consumer_source_identity"),
        "consumer_evidence": [
            {"location": row.get("consumer_location"), "api": row.get("public_api")}
            for row in evidence
        ],
        "sgrx_version": VERSION,
    })
    if not runner.dry_run and not force and path.is_file():
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            if checkpoint.get("signature") == signature and isinstance(checkpoint.get("research"), dict):
                _audit_event(scope, "checkpoint_reused", path=str(path))
                return checkpoint["research"], {"status": "REUSED", "path": str(path), "signature": signature}
        except (OSError, json.JSONDecodeError):
            pass
    research = research_indexes(dict(indexing), question, list(evidence), runner, mode)
    if not runner.dry_run:
        _write_json_artifact(path, {"signature": signature, "saved_at": utc_now(), "research": research})
        _audit_event(scope, "analysis_queries_completed", path=str(path))
    return research, {
        "status": "DRY_RUN" if runner.dry_run else "SAVED",
        "path": str(path),
        "signature": signature,
    }


def _apply_report_verification(payload: dict[str, Any]) -> dict[str, Any]:
    verification = verify_report(payload)
    payload["verification"] = verification
    payload["run_status"] = verification["status"]
    failures = [item["message"] for item in verification.get("failures", [])]
    if failures:
        payload.setdefault("limitations", []).extend(
            f"Verification gate failed: {message}" for message in failures
        )
    return payload


def _write_audit_artifacts(payload: dict[str, Any], facet_result: Mapping[str, Any], runner: CommandRunner) -> None:
    if runner.dry_run:
        return
    scope = Path(str(facet_result["scope"]))
    artifacts = [
        scope / "01-resolution.json",
        scope / "02-corpus-plan.json",
        scope / "03-index-manifest.json",
        scope / "04-query-results.json",
        scope / "04-index-context.json",
        scope / "05-evidence.json",
        scope / "06-verification.json",
        scope / "REPORT.md",
    ]
    _write_json_artifact(artifacts[0], payload.get("provenance", {}))
    _write_json_artifact(artifacts[1], payload.get("indexing", {}).get("manifest", {}).get("corpus", {}))
    _write_json_artifact(artifacts[2], payload.get("indexing", {}).get("manifest", {}))
    if not artifacts[3].is_file():
        _write_json_artifact(artifacts[3], {"facets": payload.get("audit_facets", {})})
    if not artifacts[4].is_file():
        _write_json_artifact(artifacts[4], {"research": payload.get("research", {})})
    _write_json_artifact(artifacts[5], {"practice_mappings": payload.get("practice_mappings", []), "evidence": payload.get("evidence", [])})
    _write_json_artifact(artifacts[6], payload.get("verification", {}))
    artifacts[7].write_text(markdown_report(payload), encoding="utf-8")
    manifest_path = scope / "RUN_MANIFEST.md"
    manifest_path.write_text(handoff_markdown(payload, [*artifacts, manifest_path]), encoding="utf-8")
    payload["audit_artifacts"] = {"scope": str(scope), "run_manifest": str(manifest_path), "report": str(artifacts[7])}
    _audit_event(scope, "report_verified", status=payload.get("run_status"), report=str(artifacts[7]))


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


def _candidate_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    values = vars(args).copy()
    values.update({
        "registry": "github",
        "ref": None,
        "version": None,
        "source_path": None,
        "allow_global_graph": False,
        "allow_gitnexus_group": False,
        "force": getattr(args, "force", False),
    })
    values.update(overrides)
    return argparse.Namespace(**values)


def _research_checkpoint_signature(kind: str, item: Mapping[str, Any], question: str, mode: str) -> str:
    payload = json.dumps(
        {"kind": kind, "item": item, "question": question, "mode": mode, "sgrx_version": VERSION},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checkpoint_reusable(kind: str, result: Mapping[str, Any]) -> bool:
    if kind == "paper":
        indexing = result.get("indexing", {})
        return indexing.get("status") in {"HEALTHY", "PARTIAL"} and Path(str(indexing.get("graph_path", ""))).is_file()
    indexing = result.get("indexing", {})
    graph_path = Path(str(indexing.get("graph_path", "")))
    nexus_source = Path(str(indexing.get("gitnexus_source", "")))
    return (
        indexing.get("status") in {"HEALTHY", "DEGRADED"}
        and graph_path.is_file()
        and nexus_source.joinpath(".gitnexus").is_dir()
    )


def _load_research_checkpoint(path: Path, signature: str, kind: str) -> dict[str, Any] | None:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = checkpoint.get("result")
    if checkpoint.get("signature") != signature or not isinstance(result, dict) or not _checkpoint_reusable(kind, result):
        return None
    reused = dict(result)
    reused["checkpoint"] = {"status": "REUSED", "path": str(path), "signature": signature}
    return reused


def _write_research_checkpoint(path: Path, signature: str, kind: str, result: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "kind": kind,
        "signature": signature,
        "saved_at": utc_now(),
        "result": result,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _research_progress(message: str, runner: CommandRunner) -> None:
    if not runner.dry_run:
        print(f"[SGRX research] {message}", file=sys.stderr, flush=True)


def _run_research_index(
    *,
    source: Path,
    identity: str,
    alias: str,
    scope: Path,
    mode: str,
    force: bool,
    runner: CommandRunner,
) -> dict[str, Any]:
    """Resume Graphify and GitNexus independently for one research repository."""
    role_scope = scope / "repository"
    graph_scope = role_scope / "graphify" / identity[:16]
    graph_path = graph_scope / "graphify-out" / "graph.json"
    snapshot = role_scope / "gitnexus-source" / identity[:16]
    graph_reused = graph_path.is_file() and not force
    nexus_reused = snapshot.joinpath(".gitnexus").is_dir() and not force
    graph_result: CommandResult | None = None
    nexus_result: CommandResult | None = None
    if not graph_reused:
        graph_result = runner.run(graphify_command(source, graph_scope, mode), cwd=scope)
    if not nexus_reused:
        snapshot = prepare_source_snapshot(source, role_scope, identity)
        environment = gitnexus_env(scope, snapshot)
        nexus_result = runner.run(gitnexus_command(snapshot, alias), cwd=snapshot, env=environment)
    environment = gitnexus_env(scope, snapshot)
    graph_health = runner.run(
        ["graphify", "diagnose", "multigraph", "--graph", str(graph_path), "--json"],
        cwd=scope,
    )
    nexus_status = runner.run(
        ["npx", "--no-install", "gitnexus", "status"],
        cwd=snapshot,
        env=environment,
    )
    nexus_search = runner.run(
        ["npx", "--no-install", "gitnexus", "query", "main", "--repo", alias, "--limit", "1"],
        cwd=snapshot,
        env=environment,
    )
    source_unchanged = source_tree_identity(source) == identity
    graph_exists = graph_path.is_file()
    nexus_index_exists = snapshot.joinpath(".gitnexus").is_dir()
    search_warning = "warning" in f"{nexus_search.stdout}\n{nexus_search.stderr}".casefold()
    status_state = gitnexus_status_state(nexus_status)
    status_ok = gitnexus_status_ok(nexus_status)
    attempted_ok = (graph_result is None or graph_result.ok) and (nexus_result is None or nexus_result.ok)
    if not (attempted_ok and graph_exists and nexus_index_exists and source_unchanged):
        status = "PARTIAL"
    elif graph_health.ok and status_ok and nexus_search.ok and not search_warning:
        status = "HEALTHY"
    else:
        status = "DEGRADED"
    return {
        "role": "repository",
        "status": status,
        "source_path": str(source),
        "source_identity": identity,
        "graph_path": str(graph_path),
        "gitnexus_source": str(snapshot),
        "gitnexus_alias": alias,
        "gitnexus_home": environment["HOME"],
        "resume": {"graphify_reused": graph_reused, "gitnexus_reused": nexus_reused},
        "health": {
            "graph_exists": graph_exists,
            "graph_diagnostics_ok": graph_health.ok,
            "gitnexus_index_exists": nexus_index_exists,
            "gitnexus_status_ok": status_ok,
            "gitnexus_status_state": status_state,
            "gitnexus_search_ok": nexus_search.ok and not search_warning,
            "source_unchanged": source_unchanged,
        },
    }


def _paper_graph(
    paper: Mapping[str, Any],
    scope: Path,
    question: str,
    mode: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    source_value = paper.get("source_path")
    temporary: tempfile.TemporaryDirectory[str] | None = None
    materialization = "full_source"
    try:
        if source_value:
            source = Path(str(source_value)).expanduser().resolve()
            if not runner.dry_run and not source.exists():
                return {"status": "UNAVAILABLE", "reason": f"Paper source does not exist: {source}", "graph_nodes": []}
        else:
            materialization = "metadata_abstract"
            if runner.dry_run:
                source = scope / "paper-metadata.md"
            else:
                temporary = tempfile.TemporaryDirectory(prefix="sgrx-paper-")
                source = Path(temporary.name) / f"{safe_slug(str(paper['id']), None)}.md"
                source.write_text(
                    f"# {paper['title']}\n\nYear: {paper['year']}\n\n{paper.get('abstract') or 'Abstract unavailable.'}\n\nSource: {paper.get('url') or paper.get('pdf_url') or 'unknown'}\n",
                    encoding="utf-8",
                )
        identity = "dry-run" if runner.dry_run else (file_sha256(source) if source.is_file() else source_tree_identity(source))
        graph_source = source
        if source.is_file():
            if runner.dry_run:
                graph_source = source.parent
            else:
                if temporary is None:
                    temporary = tempfile.TemporaryDirectory(prefix="sgrx-paper-")
                staged = Path(temporary.name) / source.name
                if staged.resolve() != source.resolve():
                    shutil.copy2(source, staged)
                graph_source = Path(temporary.name)
        graph_scope = scope / "graphify" / identity[:16]
        graph = runner.run(graphify_command(graph_source, graph_scope, mode), cwd=scope.parent if runner.dry_run else scope)
        graph_path = graph_scope / "graphify-out" / "graph.json"
        health = runner.run(
            ["graphify", "diagnose", "multigraph", "--graph", str(graph_path), "--json"],
            cwd=scope.parent if runner.dry_run else scope,
        )
        terms = graph_query_terms(graph_path, question, paper.get("tags", [])) if graph_path.is_file() else []
        query = runner.run(
            ["graphify", "query", " ".join(terms), "--graph", str(graph_path), "--budget", "2400" if mode == "deep" else "1200"],
            cwd=graph_scope,
        ) if terms else None
        nodes, parse_error = _parse_graph_query_nodes(query)
        status = "DRY_RUN" if runner.dry_run else ("HEALTHY" if graph.ok and graph_path.is_file() and health.ok else "PARTIAL")
        limitations = []
        if materialization == "metadata_abstract":
            limitations.append("Paper graph uses supplied metadata and abstract, not verified full text.")
        if not runner.dry_run and not graph_path.is_file():
            limitations.append(
                "Paper graph extraction produced no graph. Configure a supported Graphify semantic backend for document and paper inputs."
            )
        if parse_error:
            limitations.append(parse_error)
        return {
            "status": status,
            "materialization": materialization,
            "source_identity": identity,
            "graph_path": str(graph_path),
            "query_terms": terms,
            "graph_nodes": nodes,
            "graphify_tokens": graphify_token_usage([graph]),
            "limitations": limitations,
            "graphify": result_payload(graph),
            "query": result_payload(query),
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def _repository_research(
    repository: Mapping[str, Any],
    scope: Path,
    args: argparse.Namespace,
    runner: CommandRunner,
) -> dict[str, Any]:
    spec = validate_package(str(repository["spec"]), "github")
    local_source = repository.get("source_path")
    repo_args = _candidate_args(args, package=spec, source_path=local_source)
    if local_source:
        provenance = local_source_provenance(repo_args, runner)
    else:
        provenance = resolve_dependency(repo_args, runner)
    source = Path(provenance["cache_path"]) if provenance.get("cache_path") else None
    planned_source = False
    if source is None and runner.dry_run:
        source = scope / "planned-resolved-source"
        planned_source = True
    if source is None or (not runner.dry_run and not source.is_dir()):
        return {
            **repository,
            "provenance": provenance,
            "indexing": {"status": "UNAVAILABLE"},
            "research": {},
            "graph_nodes": [],
            "limitations": ["Repository source could not be resolved; no implementation evidence is asserted."],
        }
    integrity = {"status": "DRY_RUN", "usable": True, "details": None}
    if not runner.dry_run and not local_source:
        integrity = opensrc_checkout_integrity(source, runner)
        if not integrity["usable"] and integrity["status"] == "DIRTY_OR_INCOMPLETE" and is_windows():
            command = command_for_opensrc(spec, "github", Path(args.project).expanduser().resolve(), repo_args.ref)
            retry, recovered_source, cache_root = retry_opensrc_with_long_paths(command, Path(args.project).expanduser().resolve(), runner)
            provenance["attempts"] = [*provenance.get("attempts", []), result_payload(retry)]
            provenance["recovery"] = {
                "reason": "incomplete_opensrc_checkout",
                "cache_root": str(cache_root),
                "succeeded": recovered_source is not None,
            }
            if recovered_source is not None:
                source = recovered_source
                provenance["cache_path"] = str(source)
                provenance["resolution_status"] = "RESOLVED_LONG_PATH_RECOVERY"
                provenance["tool_result"] = result_payload(retry)
                integrity = opensrc_checkout_integrity(source, runner)
        provenance["source_integrity"] = integrity
        if not integrity["usable"]:
            return {
                **repository,
                "provenance": provenance,
                "indexing": {"status": "UNAVAILABLE"},
                "research": {},
                "graph_nodes": [],
                "source_profile": "rejected-incomplete-cache",
                "indexed_file_count": 0,
                "graphify_tokens": {"input": 0, "output": 0},
                "limitations": [str(integrity["details"])],
            }
    elif local_source:
        provenance["source_integrity"] = {"status": "USER_PROVIDED", "usable": True, "details": None}
    source_identity = "dry-run" if runner.dry_run else source_tree_identity(source)
    source_profile = "full-source"
    indexed_file_count: int | None = None
    research_source = source
    profile_limitations: list[str] = []
    if args.mode in {"quick", "standard"}:
        source_profile = "code-only"
        if runner.dry_run:
            research_source = scope / "planned-code-source"
        else:
            research_source, indexed_file_count = prepare_research_graph_snapshot(source, scope, source_identity)
            if indexed_file_count == 0:
                source_profile = "full-source-fallback"
                research_source = source
                profile_limitations.append("No supported code files were found; Graphify used the full repository source.")
    identity = "dry-run" if runner.dry_run else source_tree_identity(research_source)
    alias = f"{safe_slug(spec, package_version(spec, 'github', None))}-{identity[:8]}"
    before_index = len(runner.history)
    if runner.dry_run:
        indexed = _run_isolated_index(
            role="repository",
            source=research_source,
            identity=identity,
            alias=alias,
            scope=scope,
            project=Path(args.project).expanduser().resolve(),
            mode=args.mode,
            allow_global_graph=False,
            corpus_profile="full",
            token_budget=0,
            max_files=0,
            max_images=-1,
            include_paths=(),
            exclude_paths=(),
            runner=runner,
        )
    else:
        indexed = _run_research_index(
            source=research_source,
            identity=identity,
            alias=alias,
            scope=scope,
            mode=args.mode,
            force=args.force,
            runner=runner,
        )
    manifest = {
        "graph_path": indexed["graph_path"],
        "gitnexus_alias": indexed["gitnexus_alias"],
        "gitnexus_source": indexed["gitnexus_source"],
        "gitnexus_home": indexed["gitnexus_home"],
        "consumer_graph_path": indexed["graph_path"],
        "consumer_gitnexus_alias": indexed["gitnexus_alias"],
        "consumer_gitnexus_source": indexed["gitnexus_source"],
        "consumer_index_shared": True,
        "gitnexus_group_opt_in": False,
    }
    focus_evidence = [{"public_api": term} for term in repository.get("focus_terms", [])]
    research = research_indexes(
        {"manifest": manifest},
        args.question,
        focus_evidence,
        runner,
        args.mode,
    ) if indexed["status"] in {"HEALTHY", "DEGRADED"} else {}
    limitations = list(research.get("parse_errors", []))
    limitations.extend(profile_limitations)
    if planned_source:
        limitations.append("Dry-run planned repository resolution and isolated indexes with placeholder paths; no source was fetched.")
    warning = research.get("gitnexus_payload", {}).get("warning")
    if warning:
        limitations.append(f"GitNexus warning: {warning}")
    return {
        **repository,
        "provenance": provenance,
        "indexing": indexed,
        "research": research,
        "graph_nodes": research.get("graph_nodes", []),
        "source_profile": source_profile,
        "indexed_file_count": indexed_file_count,
        "graphify_tokens": graphify_token_usage(runner.history[before_index:]),
        "limitations": limitations,
    }


def research_mode(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise SGRXError(f"Consumer project does not exist: {project}")
    candidate_path = Path(args.candidates).expanduser().resolve()
    candidates = load_candidates(candidate_path)
    question = args.question or candidates.get("question")
    if not question:
        raise SGRXError("Research question is required in --question or the candidates manifest.")
    args.question = question
    selection = select_with_budget(
        candidates["papers"],
        candidates["repositories"],
        token_budget=args.token_budget,
        max_papers=args.max_papers,
        max_repositories=args.max_repositories,
    )
    scope = project / ".sgrx" / "research" / research_slug(question)
    before = len(runner.history)
    version_report = doctor(runner)
    papers: list[dict[str, Any]] = []
    for position, paper in enumerate(selection["selected_papers"], 1):
        paper_scope = scope / "papers" / safe_slug(str(paper["id"]), None)
        checkpoint_path = paper_scope / "checkpoint.json"
        signature = _research_checkpoint_signature("paper", paper, question, args.mode)
        cached = None if runner.dry_run or args.force else _load_research_checkpoint(checkpoint_path, signature, "paper")
        if cached is not None:
            _research_progress(f"paper {position}/{len(selection['selected_papers'])}: reused {paper['id']}", runner)
            papers.append(cached)
            continue
        _research_progress(f"paper {position}/{len(selection['selected_papers'])}: indexing {paper['id']}", runner)
        if not runner.dry_run:
            paper_scope.mkdir(parents=True, exist_ok=True)
        result = {**paper, "indexing": _paper_graph(paper, paper_scope, question, args.mode, runner)}
        if not runner.dry_run and _checkpoint_reusable("paper", result):
            _write_research_checkpoint(checkpoint_path, signature, "paper", result)
            result["checkpoint"] = {"status": "SAVED", "path": str(checkpoint_path), "signature": signature}
        papers.append(result)
    repositories: list[dict[str, Any]] = []
    for position, repository in enumerate(selection["selected_repositories"], 1):
        repo_scope = scope / "repositories" / safe_slug(str(repository["spec"]), None)
        checkpoint_path = repo_scope / "checkpoint.json"
        signature = _research_checkpoint_signature("repository", repository, question, args.mode)
        cached = None if runner.dry_run or args.force else _load_research_checkpoint(checkpoint_path, signature, "repository")
        if cached is not None:
            _research_progress(f"repository {position}/{len(selection['selected_repositories'])}: reused {repository['spec']}", runner)
            repositories.append(cached)
            continue
        _research_progress(f"repository {position}/{len(selection['selected_repositories'])}: indexing {repository['spec']}", runner)
        if not runner.dry_run:
            repo_scope.mkdir(parents=True, exist_ok=True)
        result = _repository_research(repository, repo_scope, args, runner)
        if not runner.dry_run and _checkpoint_reusable("repository", result):
            _write_research_checkpoint(checkpoint_path, signature, "repository", result)
            result["checkpoint"] = {"status": "SAVED", "path": str(checkpoint_path), "signature": signature}
        repositories.append(result)
    paper_by_id = {paper["id"]: paper for paper in papers}
    relationships: list[dict[str, Any]] = []
    for repository in repositories:
        for paper_id in repository.get("paper_ids", []):
            paper = paper_by_id.get(paper_id)
            if not paper:
                continue
            extracted = bool(repository.get("official")) and repository.get("evidence_status") == "EXTRACTED"
            relationships.append({
                "paper_id": paper_id,
                "repository": repository["spec"],
                "relationship": "implements_or_accompanies",
                "evidence_status": "EXTRACTED" if extracted else "AMBIGUOUS",
                "confidence": 0.95 if extracted else 0.45,
                "uncertainty": "Official linkage supplied by discovery evidence." if extracted else "Verify the link from the paper or official repository metadata.",
            })
    limitations = [
        limitation
        for paper in papers
        for limitation in paper.get("indexing", {}).get("limitations", [])
    ]
    limitations.extend(limitation for repository in repositories for limitation in repository.get("limitations", []))
    if selection["excluded_papers"] or selection["excluded_repositories"]:
        limitations.append("Lower-ranked candidates were excluded by count or token budget; they were not silently treated as disproven.")
    observed_input = sum(int(paper.get("indexing", {}).get("graphify_tokens", {}).get("input", 0)) for paper in papers)
    observed_input += sum(int(repository.get("graphify_tokens", {}).get("input", 0)) for repository in repositories)
    observed_output = sum(int(paper.get("indexing", {}).get("graphify_tokens", {}).get("output", 0)) for paper in papers)
    observed_output += sum(int(repository.get("graphify_tokens", {}).get("output", 0)) for repository in repositories)
    selection["budget"]["observed_graphify_input"] = observed_input
    selection["budget"]["observed_graphify_output"] = observed_output
    selection["budget"]["observed_exceeds_total"] = observed_input > args.token_budget
    if observed_input > args.token_budget:
        limitations.append(
            f"Observed Graphify extraction input ({observed_input} tokens) exceeded the planning budget ({args.token_budget}); narrow the corpus or use quick/standard code-only indexing."
        )
    plan_payload = {
        "question": question,
        "requirements": candidates["requirements"],
        "papers": papers,
        "repositories": repositories,
        "excluded_papers": selection["excluded_papers"],
        "excluded_repositories": selection["excluded_repositories"],
        "budget": selection["budget"],
        "limitations": limitations,
    }
    plan = build_plan_markdown(plan_payload)
    manifest_path = scope / "research-manifest.json"
    plan_path = scope / "BUILD_PLAN.md"
    payload = {
        "format_version": "1.0",
        "brand": BRAND,
        "research_mode": True,
        "question": question,
        "mode": args.mode,
        "artifact_dir": str(scope),
        "candidates_path": str(candidate_path),
        "budget": selection["budget"],
        "papers": papers,
        "repositories": repositories,
        "excluded_papers": selection["excluded_papers"],
        "excluded_repositories": selection["excluded_repositories"],
        "relationships": {
            "EXTRACTED": [item for item in relationships if item["evidence_status"] == "EXTRACTED"],
            "INFERRED": [],
            "AMBIGUOUS": [item for item in relationships if item["evidence_status"] == "AMBIGUOUS"],
        },
        "limitations": limitations,
        "build_plan": str(plan_path),
        "tool_versions": version_report["tools"],
        "commands": command_log(runner.history[before:]),
        "timestamp": utc_now(),
    }
    if not runner.dry_run:
        scope.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        plan_path.write_text(plan, encoding="utf-8")
    payload["_build_plan_markdown"] = plan
    return payload


def _write_analysis_artifacts(payload: dict[str, Any], runner: CommandRunner) -> None:
    if runner.dry_run or not payload.get("indexing", {}).get("artifact_dir"):
        return
    scope = Path(payload["indexing"]["artifact_dir"]) / "analyze" / safe_slug(str(payload["question"]), None)
    artifacts = [
        scope / "01-resolution.json",
        scope / "02-corpus-plan.json",
        scope / "03-index-manifest.json",
        scope / "04-query-results.json",
        scope / "05-evidence.json",
        scope / "06-verification.json",
        scope / "REPORT.md",
    ]
    payload["analysis_artifacts"] = {"scope": str(scope), "report": str(artifacts[6]), "run_manifest": str(scope / "RUN_MANIFEST.md")}
    _write_json_artifact(artifacts[0], payload.get("provenance", {}))
    _write_json_artifact(artifacts[1], payload.get("indexing", {}).get("manifest", {}).get("corpus", {}))
    _write_json_artifact(artifacts[2], payload.get("indexing", {}).get("manifest", {}))
    if not artifacts[3].is_file():
        _write_json_artifact(artifacts[3], {"research": payload.get("research", {})})
    _write_json_artifact(artifacts[4], {"evidence": payload.get("evidence", []), "relationships": payload.get("relationships", {})})
    _write_json_artifact(artifacts[5], payload.get("verification", {}))
    artifacts[6].parent.mkdir(parents=True, exist_ok=True)
    artifacts[6].write_text(markdown_report(payload), encoding="utf-8")
    manifest_path = scope / "RUN_MANIFEST.md"
    manifest_path.write_text(handoff_markdown(payload, [*artifacts, manifest_path]), encoding="utf-8")
    _audit_event(scope, "report_verified", status=payload.get("run_status"), report=str(artifacts[6]))


def analyze(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    project = Path(args.project).expanduser().resolve()
    version_report = doctor(runner)
    provenance = local_source_provenance(args, runner) if getattr(args, "source_path", None) else resolve_dependency(args, runner)
    provenance["tool_versions"] = _research_tool_versions(version_report["tools"])
    source = Path(provenance["cache_path"]) if provenance.get("cache_path") else None
    indexing = index_sources(args, runner, source)
    evidence = scan_consumer(project, args.package)
    if indexing["status"] in {"HEALTHY", "DEGRADED", "REUSED"}:
        research, analysis_checkpoint = analysis_index_research(
            indexing,
            args.question,
            evidence,
            runner,
            mode=args.mode,
            force=args.force,
        )
    else:
        research = {}
        analysis_checkpoint = {"status": "NOT_WRITTEN"}
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
        limitations.append("The question and constrained intent expansion had no overlap with Graphify vocabulary; no graph traversal was fabricated.")
    limitations.extend(research.get("parse_errors", []))
    nexus_payload = research.get("gitnexus_payload", {})
    if nexus_payload.get("warning"):
        limitations.append(f"GitNexus warning: {nexus_payload['warning']}")
    paths = list(nexus_payload.get("processes", []))
    if not research.get("consumer_index_shared"):
        paths.extend(research.get("consumer_gitnexus_payload", {}).get("processes", []))
    paths.extend(research.get("gitnexus_group_payload", {}).get("processes", []))
    for context in research.get("contexts", []):
        paths.extend(context.get("processes", []))
    payload = {
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
        "checkpoint": analysis_checkpoint,
        "timestamp": utc_now(),
    }
    _apply_report_verification(payload)
    _write_analysis_artifacts(payload, runner)
    return payload


def audit(args: argparse.Namespace, runner: CommandRunner) -> dict[str, Any]:
    """Compare benchmark practices with the consumer without inventing a runtime boundary."""

    if args.facet_budget <= 0:
        raise SGRXError("Facet budget must be greater than zero.")
    version_report = doctor(runner)
    provenance = local_source_provenance(args, runner) if getattr(args, "source_path", None) else resolve_dependency(args, runner)
    provenance["tool_versions"] = _research_tool_versions(version_report["tools"])
    source = Path(provenance["cache_path"]) if provenance.get("cache_path") else None
    indexing = index_sources(args, runner, source)
    queryable = indexing.get("status") in {"DRY_RUN", "HEALTHY", "DEGRADED", "REUSED"}
    if queryable:
        facet_result = audit_graph_facets(
            indexing,
            args.question,
            runner,
            mode=args.mode,
            facet_budget=args.facet_budget,
            force=args.force,
        )
        research, context_checkpoint = audit_index_context(
            indexing,
            args.question,
            runner,
            mode=args.mode,
            force=args.force,
            facet_result=facet_result,
        )
    else:
        scope = Path(str(indexing.get("artifact_dir", Path(args.project) / ".sgrx"))) / "audit" / safe_slug(args.question, None)
        facet_result = {"facets": {}, "checkpoint": {"status": "NOT_WRITTEN"}, "scope": str(scope)}
        research = {}
        context_checkpoint = {"status": "NOT_WRITTEN", "path": str(scope / "04-index-context.json")}
    version = str(provenance.get("resolved_version") or provenance.get("ref") or "resolved")
    mappings = audit_practice_mappings(facet_result["facets"], args.package, version)
    external_symbols = implementation_rows(research, args.package, version)
    evidence = [*mappings, *external_symbols]
    limitations: list[str] = []
    if indexing.get("status") in {"UNAVAILABLE", "PARTIAL"}:
        limitations.append("Corpus preflight or isolated indexing did not complete; practice mappings remain incomplete.")
    if indexing.get("status") == "DEGRADED" or (
        indexing.get("status") == "REUSED" and indexing.get("manifest", {}).get("status") == "DEGRADED"
    ):
        limitations.append("At least one index is degraded; query completeness is not claimed.")
    manifest = indexing.get("manifest", {})
    for role, plan in manifest.get("corpus", {}).items():
        for violation in plan.get("violations", []):
            limitations.append(f"{role} corpus preflight: {violation}.")
    for name, facet in facet_result["facets"].items():
        if not facet.get("dependency_nodes") or not facet.get("consumer_nodes"):
            limitations.append(f"Facet {name!r} lacks graph evidence on one side.")
        limitations.extend(f"Facet {name!r}: {error}" for error in facet.get("errors", []))
    limitations.extend(research.get("parse_errors", []))
    nexus_payload = research.get("gitnexus_payload", {})
    if nexus_payload.get("warning"):
        limitations.append(f"GitNexus warning: {nexus_payload['warning']}")
    paths = list(nexus_payload.get("processes", []))
    paths.extend(research.get("consumer_gitnexus_payload", {}).get("processes", []))
    architecture = [
        f"{item['facet']}: {item['practice']} -> {item['recommendation']} ({item['evidence_status']})"
        for item in mappings
    ]
    payload = {
        "format_version": "1.0",
        "brand": BRAND,
        "audit_mode": True,
        "question": args.question,
        "short_answer": "SGRX compared benchmark and consumer indexes by explicit practice facets; transfer recommendations remain inferred rather than runtime-proven.",
        "mode": args.mode,
        "provenance": provenance,
        "consumer_call_sites": [row for row in mappings if row.get("consumer_location")],
        "external_implementation": [row for row in evidence if row.get("dependency_location")],
        "end_to_end_path": paths or ["audit -> corpus preflight -> isolated indexes -> faceted queries -> evidence mapping -> verification gate"],
        "architecture_overview": architecture,
        "edge_cases": [
            "A documentation benchmark may have no GitNexus execution flow.",
            "A missing facet on either graph keeps the mapping AMBIGUOUS.",
            "Images and media remain excluded unless the corpus profile explicitly includes them.",
        ],
        "deprecations": [],
        "change_risk": {"status": "NOT_REQUESTED", "risk": "UNKNOWN", "direct_callers": [], "processes": []},
        "evidence": evidence,
        "relationships": {status: [row for row in evidence if row["evidence_status"] == status] for status in EVIDENCE_STATUSES},
        "limitations": list(dict.fromkeys(limitations)),
        "recommended_next_steps": list(dict.fromkeys(item["recommendation"] for item in mappings)),
        "tool_versions": version_report["tools"],
        "commands": command_log(runner.history),
        "indexing": indexing,
        "research": research,
        "audit_facets": facet_result["facets"],
        "practice_mappings": mappings,
        "checkpoint": facet_result["checkpoint"],
        "context_checkpoint": context_checkpoint,
        "timestamp": utc_now(),
    }
    _apply_report_verification(payload)
    _write_audit_artifacts(payload, facet_result, runner)
    return payload


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
    evidence = comparison_evidence(differences, args.from_version, args.to_version, research)
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
        "evidence": evidence,
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
        *(["", f"**Run status:** `{data['run_status']}`"] if data.get("run_status") else []),
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


def add_common(parser: argparse.ArgumentParser, *, include_cross_repository_options: bool = True) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print and record commands without executing tools.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown or human-readable text.")
    parser.add_argument("--output", help="Write output to this file.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-command timeout in seconds.")
    parser.add_argument("--mode", choices=MODES, default="standard")
    if include_cross_repository_options:
        parser.add_argument("--allow-global-graph", action="store_true", help="Allow a merged Graphify graph.")
        parser.add_argument("--allow-gitnexus-group", action="store_true", help="Allow GitNexus group creation.")


def add_dependency(parser: argparse.ArgumentParser, *, project_required: bool = True) -> None:
    parser.add_argument("--package", required=True)
    parser.add_argument("--project", required=project_required, default=None if project_required else ".")
    parser.add_argument("--registry", choices=REGISTRIES, default="npm")
    parser.add_argument("--ref")


def add_corpus_options(parser: argparse.ArgumentParser, *, audit_defaults: bool = False) -> None:
    parser.add_argument(
        "--corpus-profile",
        choices=CORPUS_PROFILES,
        default="code-docs" if audit_defaults else "full",
        help="Select Graphify inputs; practice audits exclude images and media by default.",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=300_000 if audit_defaults else 0,
        help="Stop before Graphify when the conservative corpus estimate exceeds this value; 0 disables the limit.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=300 if audit_defaults else 0,
        help="Maximum selected corpus files; 0 disables the limit.",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=0 if audit_defaults else -1,
        help="Maximum selected images; -1 disables the limit.",
    )
    parser.add_argument(
        "--include-path",
        action="append",
        help="Include only this repository-relative file or directory; repeat for multiple scopes.",
    )
    parser.add_argument(
        "--exclude-path",
        action="append",
        help="Exclude this repository-relative file or directory; repeat for multiple scopes.",
    )


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
    add_corpus_options(index_parser)
    index_parser.add_argument("--source-path"); index_parser.add_argument("--version"); index_parser.add_argument("--force", action="store_true")
    analyze_parser = sub.add_parser("analyze", help="Trace consumer usage into dependency source.")
    add_common(analyze_parser); add_dependency(analyze_parser)
    add_corpus_options(analyze_parser)
    analyze_parser.add_argument("--question", required=True); analyze_parser.add_argument("--source-path"); analyze_parser.add_argument("--force", action="store_true")
    audit_parser = sub.add_parser("audit", help="Compare benchmark practices with a consumer without claiming a runtime dependency.")
    add_common(audit_parser)
    audit_parser.add_argument("--benchmark", dest="package", required=True)
    audit_parser.add_argument("--project", required=True)
    audit_parser.add_argument("--registry", choices=REGISTRIES, default="github")
    audit_parser.add_argument("--ref")
    audit_parser.add_argument("--question", required=True)
    audit_parser.add_argument("--source-path")
    audit_parser.add_argument("--force", action="store_true")
    audit_parser.add_argument("--facet-budget", type=int, default=1_200)
    add_corpus_options(audit_parser, audit_defaults=True)
    compare_parser = sub.add_parser("compare", help="Resolve two dependency versions for isolated comparison.")
    add_common(compare_parser); add_dependency(compare_parser, project_required=False)
    add_corpus_options(compare_parser)
    compare_parser.add_argument("--from", dest="from_version", required=True); compare_parser.add_argument("--to", dest="to_version", required=True); compare_parser.add_argument("--question", required=True)
    research_parser = sub.add_parser("research", help="Rank paper/repository candidates, index selected evidence, and generate a build plan.")
    add_common(research_parser, include_cross_repository_options=False)
    research_parser.add_argument("--project", required=True)
    research_parser.add_argument("--candidates", required=True, help="JSON manifest produced by current paper/repository discovery.")
    research_parser.add_argument("--question", help="Override the research question in the candidates manifest.")
    research_parser.add_argument("--max-papers", type=int, default=8)
    research_parser.add_argument("--max-repositories", type=int, default=4)
    research_parser.add_argument("--token-budget", type=int, default=30_000)
    research_parser.add_argument("--force", action="store_true")
    report_parser = sub.add_parser("report", help="Render a saved SGRX JSON result as Markdown or normalized JSON.")
    add_common(report_parser); report_parser.add_argument("--input", required=True)
    return parser


def write_output(payload: dict[str, Any], args: argparse.Namespace, *, report: bool = False) -> None:
    normalized = {key: value for key, value in payload.items() if not key.startswith("_")}
    if args.json:
        content = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    elif payload.get("research_mode"):
        content = str(payload.get("_build_plan_markdown") or "")
    else:
        content = markdown_report(normalized)
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
        elif args.command == "audit":
            payload = audit(args, runner)
        elif args.command == "compare":
            payload = compare(args, runner)
        elif args.command == "research":
            payload = research_mode(args, runner)
        else:
            input_path = Path(args.input).expanduser()
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        write_output(payload, args, report=args.command == "report")
        return 0
    except (SGRXError, ResearchError, OSError, json.JSONDecodeError) as exc:
        print(f"SGRX error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
