"""Pure helpers for SGRX practice audits, corpus planning, and report gates."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping


CORPUS_PROFILES = ("code", "code-docs", "full")
CODE_SUFFIXES = {
    ".bash", ".c", ".cc", ".cpp", ".cs", ".fish", ".go", ".java", ".js", ".jsx",
    ".kt", ".kts", ".mjs", ".php", ".py", ".rb", ".rs", ".scala", ".sh", ".swift",
    ".ts", ".tsx", ".vue",
}
DOCUMENT_SUFFIXES = {".adoc", ".html", ".htm", ".json", ".md", ".mdx", ".rst", ".toml", ".txt", ".yaml", ".yml"}
PAPER_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}
SKIP_DIRS = {".git", ".gitnexus", ".sgrx", ".venv", "__pycache__", "graphify-out", "node_modules", "target", "venv"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_ed25519", "id_rsa"}

AUDIT_FACETS: dict[str, tuple[str, ...]] = {
    "lifecycle": ("research", "plan", "implement", "review", "ship", "workflow", "orchestration", "checkpoint"),
    "context": ("context", "session", "memory", "fresh", "compact", "handoff", "subagent", "agent"),
    "distribution": ("skill", "skills", "command", "commands", "plugin", "hooks", "marketplace", "distribution"),
    "validation": ("validation", "verification", "verify", "evidence", "test", "tests", "checklist", "review"),
    "reliability": ("budget", "cost", "health", "index", "provenance", "retry", "warning", "recovery"),
}

AUDIT_RECOMMENDATIONS = {
    "lifecycle": "Generalize durable phase checkpoints and resumable handoffs across analyze, compare, and audit.",
    "context": "Write a compact run manifest and keep fresh-context reviewers optional and bounded.",
    "distribution": "Keep SKILL.md canonical and expose only thin host-specific adapters and optional hooks.",
    "validation": "Apply a deterministic verification gate before a report can be classified as healthy.",
    "reliability": "Preflight corpus cost, repair isolated indexes once, and surface structured health failures.",
}

REQUIRED_REPORT_FIELDS = (
    "question", "short_answer", "provenance", "consumer_call_sites", "external_implementation",
    "end_to_end_path", "architecture_overview", "edge_cases", "deprecations", "change_risk",
    "evidence", "relationships", "limitations", "recommended_next_steps", "tool_versions", "commands",
)


def _category(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in CODE_SUFFIXES:
        return "code"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in PAPER_SUFFIXES:
        return "paper"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def _scopes(values: Iterable[str]) -> tuple[Path, ...]:
    scopes: list[Path] = []
    for value in values:
        scope = Path(value)
        if scope.is_absolute() or scope.drive or ".." in scope.parts:
            raise ValueError(f"corpus path must be relative and stay inside the source root: {value}")
        scopes.append(scope)
    return tuple(scopes)


def _matches_scope(relative: Path, scope: Path) -> bool:
    return scope == Path(".") or relative == scope or scope in relative.parents


def corpus_files(
    root: Path,
    *,
    include_paths: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
) -> list[tuple[Path, str]]:
    """Return supported, non-sensitive regular files and their corpus category."""

    includes = _scopes(include_paths)
    excludes = _scopes(exclude_paths)
    files: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name.casefold() in SENSITIVE_NAMES:
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if includes and not any(_matches_scope(relative, scope) for scope in includes):
            continue
        if any(_matches_scope(relative, scope) for scope in excludes):
            continue
        category = _category(path)
        if category:
            files.append((path, category))
    return files


def selected_corpus_files(
    root: Path,
    profile: str,
    *,
    include_paths: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
) -> list[tuple[Path, str]]:
    if profile not in CORPUS_PROFILES:
        raise ValueError(f"unsupported corpus profile: {profile}")
    allowed = {
        "code": {"code"},
        "code-docs": {"code", "document", "paper"},
        "full": {"code", "document", "paper", "image", "video"},
    }[profile]
    return [
        (path, category)
        for path, category in corpus_files(root, include_paths=include_paths, exclude_paths=exclude_paths)
        if category in allowed
    ]


def corpus_preflight(
    root: Path,
    *,
    profile: str,
    token_budget: int = 0,
    max_files: int = 0,
    max_images: int = 0,
    include_paths: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a deterministic, conservative corpus plan before Graphify runs."""

    include_paths = tuple(include_paths)
    exclude_paths = tuple(exclude_paths)
    all_files = corpus_files(root, include_paths=include_paths, exclude_paths=exclude_paths)
    selected = selected_corpus_files(root, profile, include_paths=include_paths, exclude_paths=exclude_paths)
    counts = {category: 0 for category in ("code", "document", "paper", "image", "video")}
    selected_counts = dict(counts)
    selected_bytes = 0
    for path, category in all_files:
        counts[category] += 1
    for path, category in selected:
        selected_counts[category] += 1
        try:
            selected_bytes += path.stat().st_size
        except OSError:
            pass
    estimated_tokens = (selected_bytes + 3) // 4
    violations: list[str] = []
    if not selected:
        violations.append("selected corpus is empty")
    if token_budget > 0 and estimated_tokens > token_budget:
        violations.append(f"estimated tokens {estimated_tokens} exceed budget {token_budget}")
    if max_files > 0 and len(selected) > max_files:
        violations.append(f"selected files {len(selected)} exceed limit {max_files}")
    if max_images >= 0 and selected_counts["image"] > max_images:
        violations.append(f"selected images {selected_counts['image']} exceed limit {max_images}")
    status = "WITHIN_BUDGET" if not violations else ("EMPTY" if not selected else "NARROW_REQUIRED")
    return {
        "status": status,
        "profile": profile,
        "root": str(root.resolve()),
        "counts": counts,
        "selected_counts": selected_counts,
        "total_files": len(all_files),
        "selected_files": len(selected),
        "excluded_files": len(all_files) - len(selected),
        "selected_bytes": selected_bytes,
        "estimated_tokens": estimated_tokens,
        "limits": {"token_budget": token_budget, "max_files": max_files, "max_images": max_images},
        "filters": {"include_paths": list(include_paths), "exclude_paths": list(exclude_paths)},
        "violations": violations,
    }


def prepare_corpus_snapshot(
    root: Path,
    destination: Path,
    profile: str,
    *,
    include_paths: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
) -> Path:
    """Copy only the selected corpus into an isolated Graphify input tree."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for path, _category_name in selected_corpus_files(
        root,
        profile,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    ):
        target = destination / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return destination


def audit_facets(question: str) -> dict[str, str]:
    return {name: " ".join((question, *terms)) for name, terms in AUDIT_FACETS.items()}


def audit_checkpoint_signature(values: Mapping[str, Any]) -> str:
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check(checks: list[dict[str, Any]], identifier: str, passed: bool, message: str) -> None:
    checks.append({"id": identifier, "passed": bool(passed), "message": message})


def verify_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Verify report structure, evidence honesty, provenance, and index health."""

    checks: list[dict[str, Any]] = []
    for field in REQUIRED_REPORT_FIELDS:
        _check(checks, f"field:{field}", field in payload, f"required report field {field!r} is present")

    evidence = list(payload.get("evidence", []))
    extracted = [row for row in evidence if row.get("evidence_status") == "EXTRACTED"]
    extracted_locations = all(
        row.get("consumer_location") or row.get("dependency_location") or row.get("path")
        for row in extracted
    )
    _check(checks, "evidence:extracted-location", extracted_locations, "every EXTRACTED row has a source location")
    ambiguous_confidence = all(
        float(row.get("confidence", 0.0)) <= 0.89
        for row in evidence
        if row.get("evidence_status") == "AMBIGUOUS"
    )
    _check(checks, "evidence:ambiguous-confidence", ambiguous_confidence, "AMBIGUOUS confidence does not exceed 0.89")

    commands_redacted = True
    for command in payload.get("commands", []):
        arguments = list(command.get("args", []))
        for index, value in enumerate(arguments):
            lower = str(value).casefold()
            if lower in {"--token", "--password", "--secret", "--api-key"}:
                commands_redacted = commands_redacted and index + 1 < len(arguments) and arguments[index + 1] == "[REDACTED]"
            if "=" in str(value) and any(name in lower.split("=", 1)[0] for name in ("token", "password", "secret", "api_key", "api-key")):
                commands_redacted = commands_redacted and str(value).endswith("=[REDACTED]")
        for field in ("stdout", "stderr"):
            text = str(command.get(field, ""))
            if any(marker in text for marker in ("Authorization: Bearer ", "authorization: bearer ")) and "Bearer [REDACTED]" not in text:
                commands_redacted = False
    _check(checks, "commands:redacted", commands_redacted, "command arguments and outputs retain secret redaction")

    provenance = payload.get("provenance", {})
    ref = str(provenance.get("ref") or "")
    registry = provenance.get("registry")
    exact_github_ref = registry != "github" or len(ref) != 40 or all(character in "0123456789abcdefABCDEF" for character in ref)
    commit_ok = registry != "github" or len(ref) != 40 or str(provenance.get("commit") or "").casefold() == ref.casefold()
    _check(checks, "provenance:exact-ref", exact_github_ref, "GitHub SHA refs are valid hexadecimal values")
    _check(checks, "provenance:commit", commit_ok, "an exact GitHub SHA ref is recorded as the commit")

    indexing = payload.get("indexing", {})
    if indexing.get("status") == "DRY_RUN":
        _check(checks, "index:dry-run", True, "index health is intentionally not asserted in dry-run mode")
        status = "DRY_RUN"
    else:
        effective_index_status = (
            indexing.get("manifest", {}).get("status")
            if indexing.get("status") == "REUSED"
            else indexing.get("status")
        )
        index_status_ok = effective_index_status == "HEALTHY"
        health = indexing.get("health") or indexing.get("manifest", {}).get("health", {})
        corpus = indexing.get("manifest", {}).get("corpus", {})
        budget_ok = all(not plan.get("observed_exceeds_budget", False) for plan in corpus.values())
        source_unchanged = bool(health.get("source_unchanged", True)) and bool(health.get("consumer", {}).get("source_unchanged", True))
        search_ok = bool(health.get("gitnexus_search_ok", True)) and bool(health.get("consumer", {}).get("gitnexus_search_ok", True))
        graph_ok = bool(health.get("graph_diagnostics_ok", True)) and bool(health.get("consumer", {}).get("graph_diagnostics_ok", True))
        _check(checks, "index:status", index_status_ok, "index status is healthy or a reusable healthy manifest")
        _check(checks, "index:source-unchanged", source_unchanged, "consumer and dependency sources remained unchanged")
        _check(checks, "index:gitnexus-search", search_ok, "GitNexus search health is usable after recovery")
        _check(checks, "index:graph-health", graph_ok, "Graphify diagnostics and structured warnings show no data-loss risk")
        _check(checks, "index:observed-budget", budget_ok, "observed Graphify input stayed within the configured hard budget")
        status = "HEALTHY" if all(item["passed"] for item in checks) else "DEGRADED"

    failures = [item for item in checks if not item["passed"]]
    return {"status": status, "checks": checks, "failures": failures}


def handoff_markdown(payload: Mapping[str, Any], artifact_paths: Iterable[Path]) -> str:
    provenance = payload.get("provenance", {})
    limitations = list(payload.get("limitations", []))
    lines = [
        "# SGRX run manifest",
        "",
        f"- Question: {payload.get('question', 'Not supplied')}",
        f"- Run status: {payload.get('run_status', 'UNKNOWN')}",
        f"- Package: {provenance.get('requested_package', 'local source')}",
        f"- Ref: {provenance.get('ref') or provenance.get('resolved_version') or 'unknown'}",
        f"- Commit: {provenance.get('commit') or 'unknown'}",
        f"- Verification: {payload.get('verification', {}).get('status', 'NOT_RUN')}",
        "",
        "## Durable artifacts",
        "",
        *(f"- {path.name}: `{path}`" for path in artifact_paths),
        "",
        "## Open limitations",
        "",
        *(f"- {item}" for item in limitations),
    ]
    if not limitations:
        lines.append("- None recorded.")
    return "\n".join(lines) + "\n"
