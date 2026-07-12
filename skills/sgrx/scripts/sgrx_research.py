"""Deterministic candidate ranking and build-plan rendering for SGRX research mode."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


CURRENT_YEAR = datetime.now(timezone.utc).year
PAPER_DEFAULT_TOKENS = 1_200
REPOSITORY_DEFAULT_TOKENS = 3_500
EVIDENCE_STATUSES = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}


class ResearchError(ValueError):
    """Represent invalid or incomplete research-candidate data."""


def _score(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"{field} must be a number between 0 and 1.") from exc
    if not 0 <= number <= 1:
        raise ResearchError(f"{field} must be a number between 0 and 1.")
    return number


def _positive_int(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"{field} must be a positive integer.") from exc
    if number <= 0:
        raise ResearchError(f"{field} must be a positive integer.")
    return number


def _nonnegative_int(value: Any, field: str, default: int = 0) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"{field} must be a non-negative integer.") from exc
    if number < 0:
        raise ResearchError(f"{field} must be a non-negative integer.")
    return number


def _evidence_status(value: Any, field: str, default: str) -> str:
    status = str(value or default).upper()
    if status not in EVIDENCE_STATUSES:
        raise ResearchError(f"{field} must be EXTRACTED, INFERRED, or AMBIGUOUS.")
    return status


def _http_url(value: Any, field: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ResearchError(f"{field} is required.")
        return None
    text = str(value).strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResearchError(f"{field} must be an HTTP(S) URL.")
    return text


def load_candidates(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchError(f"Cannot read research candidates: {path}") from exc
    if not isinstance(data, dict):
        raise ResearchError("Research candidates must be a JSON object.")
    papers = data.get("papers", [])
    repositories = data.get("repositories", [])
    if not isinstance(papers, list) or not isinstance(repositories, list):
        raise ResearchError("papers and repositories must be arrays.")
    normalized_papers = [_normalize_paper(item, index, path.parent) for index, item in enumerate(papers)]
    normalized_repositories = [_normalize_repository(item, index, path.parent) for index, item in enumerate(repositories)]
    paper_ids = {item["id"] for item in normalized_papers}
    if len(paper_ids) != len(normalized_papers):
        raise ResearchError("Paper ids must be unique.")
    for repository in normalized_repositories:
        unknown = set(repository["paper_ids"]) - paper_ids
        if unknown:
            raise ResearchError(f"Repository {repository['spec']} references unknown paper ids: {sorted(unknown)}")
    return {
        "schema_version": "1.0",
        "question": str(data.get("question") or "").strip() or None,
        "requirements": [str(item).strip() for item in data.get("requirements", []) if str(item).strip()],
        "papers": normalized_papers,
        "repositories": normalized_repositories,
    }


def _normalize_paper(item: Any, index: int, base: Path) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ResearchError(f"papers[{index}] must be an object.")
    title = str(item.get("title") or "").strip()
    if not title:
        raise ResearchError(f"papers[{index}].title is required.")
    identifier = str(item.get("id") or hashlib.sha256(title.encode()).hexdigest()[:12]).strip()
    year = _positive_int(item.get("year"), f"papers[{index}].year", CURRENT_YEAR)
    citations = _nonnegative_int(item.get("citations"), f"papers[{index}].citations")
    local_path = item.get("source_path")
    resolved_path = str((base / str(local_path)).resolve()) if local_path and not Path(str(local_path)).is_absolute() else (str(Path(str(local_path)).resolve()) if local_path else None)
    return {
        "id": identifier,
        "title": title,
        "year": year,
        "abstract": str(item.get("abstract") or "").strip(),
        "url": _http_url(item.get("url"), f"papers[{index}].url"),
        "pdf_url": _http_url(item.get("pdf_url"), f"papers[{index}].pdf_url"),
        "source_path": resolved_path,
        "citations": citations,
        "relevance": _score(item.get("relevance", 0.5), f"papers[{index}].relevance"),
        "official_repository": bool(item.get("official_repository")),
        "estimated_tokens": _positive_int(item.get("estimated_tokens"), f"papers[{index}].estimated_tokens", PAPER_DEFAULT_TOKENS),
        "tags": [str(tag).strip() for tag in item.get("tags", []) if str(tag).strip()],
        "evidence_status": _evidence_status(item.get("evidence_status"), f"papers[{index}].evidence_status", "EXTRACTED"),
    }


def _normalize_repository(item: Any, index: int, base: Path) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ResearchError(f"repositories[{index}] must be an object.")
    spec = str(item.get("spec") or "").strip()
    if not spec:
        raise ResearchError(f"repositories[{index}].spec is required.")
    local_path = item.get("source_path")
    resolved_path = str((base / str(local_path)).resolve()) if local_path and not Path(str(local_path)).is_absolute() else (str(Path(str(local_path)).resolve()) if local_path else None)
    return {
        "spec": spec,
        "source_path": resolved_path,
        "url": _http_url(item.get("url"), f"repositories[{index}].url"),
        "paper_ids": [str(value).strip() for value in item.get("paper_ids", []) if str(value).strip()],
        "official": bool(item.get("official")),
        "license": str(item.get("license") or "").strip() or None,
        "relevance": _score(item.get("relevance", 0.5), f"repositories[{index}].relevance"),
        "architecture_fit": _score(item.get("architecture_fit", 0.5), f"repositories[{index}].architecture_fit"),
        "reproducibility": _score(item.get("reproducibility", 0.5), f"repositories[{index}].reproducibility"),
        "activity": _score(item.get("activity", 0.5), f"repositories[{index}].activity"),
        "estimated_tokens": _positive_int(item.get("estimated_tokens"), f"repositories[{index}].estimated_tokens", REPOSITORY_DEFAULT_TOKENS),
        "focus_terms": [str(term).strip() for term in item.get("focus_terms", []) if str(term).strip()],
        "notes": str(item.get("notes") or "").strip(),
        "evidence_status": _evidence_status(item.get("evidence_status"), f"repositories[{index}].evidence_status", "AMBIGUOUS"),
    }


def rank_papers(papers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for paper in papers:
        recency = max(0.0, min(1.0, 1.0 - max(0, CURRENT_YEAR - int(paper["year"])) / 8.0))
        citations = min(1.0, math.log1p(int(paper["citations"])) / math.log1p(1_000))
        open_access = 1.0 if paper.get("pdf_url") or paper.get("source_path") else 0.0
        official = 1.0 if paper.get("official_repository") else 0.0
        score = 0.45 * float(paper["relevance"]) + 0.2 * official + 0.15 * recency + 0.1 * citations + 0.1 * open_access
        ranked.append({**paper, "score": round(score, 6)})
    return sorted(ranked, key=lambda item: (-item["score"], item["id"]))


def rank_repositories(repositories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for repository in repositories:
        score = (
            0.35 * float(repository["relevance"])
            + 0.25 * float(repository["architecture_fit"])
            + 0.15 * float(repository["reproducibility"])
            + 0.15 * (1.0 if repository.get("official") else 0.0)
            + 0.05 * (1.0 if repository.get("license") else 0.0)
            + 0.05 * float(repository["activity"])
        )
        ranked.append({**repository, "score": round(score, 6)})
    return sorted(ranked, key=lambda item: (-item["score"], item["spec"]))


def select_with_budget(
    papers: Sequence[Mapping[str, Any]],
    repositories: Sequence[Mapping[str, Any]],
    *,
    token_budget: int,
    max_papers: int,
    max_repositories: int,
) -> dict[str, Any]:
    if token_budget < 2_000:
        raise ResearchError("token_budget must be at least 2000.")
    if max_papers <= 0 or max_repositories <= 0:
        raise ResearchError("max_papers and max_repositories must be positive.")
    ranked_papers = rank_papers(papers)
    ranked_repositories = rank_repositories(repositories)
    synthesis = max(1_000, int(token_budget * 0.2))
    available = token_budget - synthesis
    paper_budget = int(available * 0.3)
    repository_budget = available - paper_budget
    selected_papers, paper_used = _take(ranked_papers, max_papers, paper_budget)
    selected_repositories, repository_used = _take(ranked_repositories, max_repositories, repository_budget)
    return {
        "selected_papers": selected_papers,
        "selected_repositories": selected_repositories,
        "excluded_papers": [item for item in ranked_papers if item not in selected_papers],
        "excluded_repositories": [item for item in ranked_repositories if item not in selected_repositories],
        "budget": {
            "total": token_budget,
            "papers": paper_budget,
            "repositories": repository_budget,
            "synthesis": synthesis,
            "estimated_used": paper_used + repository_used + synthesis,
        },
    }


def _take(items: Sequence[Mapping[str, Any]], limit: int, budget: int) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    used = 0
    for item in items:
        cost = int(item["estimated_tokens"])
        if len(selected) >= limit:
            break
        if used + cost <= budget:
            selected.append(dict(item))
            used += cost
    return selected, used


def research_slug(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.casefold())
    prefix = "-".join(words[:8])[:56].strip("-") or "research"
    return f"{prefix}-{hashlib.sha256(question.encode()).hexdigest()[:8]}"


def build_plan_markdown(payload: Mapping[str, Any]) -> str:
    question = str(payload["question"])
    requirements = payload.get("requirements", [])
    papers = payload.get("papers", [])
    repositories = payload.get("repositories", [])
    excluded_papers = payload.get("excluded_papers", [])
    excluded_repositories = payload.get("excluded_repositories", [])
    budget = payload.get("budget", {})
    lines = [
        "# Evidence-backed build plan",
        "",
        "## Objective",
        "",
        question,
        "",
        "## Constraints and success criteria",
        "",
    ]
    lines.extend(f"- {item}" for item in requirements)
    if not requirements:
        lines.append("- Define measurable product, quality, latency, cost, privacy, and deployment constraints before implementation.")
    lines += ["", "## Selected research evidence", "", "### Papers", ""]
    for paper in papers:
        lines.append(f"- **{paper['title']}** ({paper['year']}, score {paper['score']:.3f}) — `{paper['id']}`")
    if not papers:
        lines.append("- No paper fit within the configured budget; expand the budget or improve candidate metadata.")
    lines += ["", "### Repositories", ""]
    for repository in repositories:
        lines.append(f"- **{repository['spec']}** (score {repository['score']:.3f}) — {repository.get('license') or 'license unverified'}")
    if not repositories:
        lines.append("- No repository fit within the configured budget; expand the budget or reduce candidate costs.")
    lines += ["", "## Architecture evidence from selected repository graphs", ""]
    for repository in repositories:
        lines.append(f"### {repository['spec']}")
        lines.append("")
        lines.append(
            f"Index status: `{repository.get('indexing', {}).get('status', 'unknown')}`; "
            f"source profile: `{repository.get('source_profile', 'unknown')}`; "
            f"indexed files: `{repository.get('indexed_file_count') if repository.get('indexed_file_count') is not None else 'unknown'}`."
        )
        lines.append("")
        nodes = repository.get("graph_nodes", [])[:10]
        for node in nodes:
            location = ":".join(part for part in (str(node.get("source") or ""), str(node.get("location") or "")) if part)
            lines.append(f"- `{node.get('label', 'unknown')}`" + (f" — `{location}`" if location else ""))
        if not nodes:
            lines.append("- Graph evidence unavailable; do not copy an architecture claim until indexing succeeds.")
        lines.append("")
    lines += ["## Evidence-to-component work packages", ""]
    work_package_count = 0
    for repository in repositories:
        for node in repository.get("graph_nodes", [])[:5]:
            label = str(node.get("label") or "unknown")
            source = str(node.get("source") or "")
            location = str(node.get("location") or "")
            if not source:
                continue
            work_package_count += 1
            anchor = f"{repository['spec']}:{source}" + (f":{location}" if location else "")
            lines += [
                f"### {work_package_count}. {label}",
                "",
                f"- **Evidence anchor:** `{anchor}`",
                f"- **Implementation task:** inspect the anchored node and its immediate graph neighbors, define the local contract for `{label}`, then implement only the behavior required by the stated constraints.",
                "- **Acceptance check:** add a contract test for inputs, outputs, errors, and resource limits; retain the evidence anchor in the design record.",
                "- **Decision rule:** keep the adaptation INFERRED until direct source or test evidence proves the intended behavior.",
                "",
            ]
    if not work_package_count:
        lines.append("- No source-located graph node was available; resolve index health before defining implementation work packages.")
        lines.append("")
    lines += ["## Rejected or deferred evidence", ""]
    for paper in excluded_papers:
        lines.append(f"- Paper `{paper.get('id', 'unknown')}` (score {float(paper.get('score', 0)):.3f}) was deferred by count or token budget.")
    for repository in excluded_repositories:
        lines.append(f"- Repository `{repository.get('spec', 'unknown')}` (score {float(repository.get('score', 0)):.3f}) was deferred by count, token budget, or a decision gate.")
    if not excluded_papers and not excluded_repositories:
        lines.append("- No candidate was deferred.")
    lines += [
        "## Recommended system decomposition",
        "",
        "1. **Contracts and boundaries** — define public interfaces, data models, invariants, failure behavior, and observability before adapting implementation details.",
        "2. **Minimal vertical slice** — implement one end-to-end path with the smallest selected pattern set and record baseline quality, latency, and cost.",
        "3. **Evidence-backed components** — adapt only graph-located components that support the requirements; preserve repository citations in design notes and tests.",
        "4. **Evaluation harness** — translate paper metrics and repository tests into repeatable acceptance checks without executing untrusted fetched code.",
        "5. **Hardening** — add fallbacks, resource limits, security controls, deployment packaging, monitoring, and rollback behavior.",
        "",
        "## Implementation sequence",
        "",
        "### Phase 1 — Decision records and interfaces",
        "",
        "- Convert every requirement into a measurable acceptance criterion.",
        "- Record selected and rejected papers/repositories with score, provenance, license, and uncertainty.",
        "- Define component interfaces independently of any single repository implementation.",
        "",
        "### Phase 2 — Foundation",
        "",
        "- Create the project skeleton, configuration model, typed domain objects, error taxonomy, and test harness.",
        "- Implement the smallest architecture path supported by at least one selected repository graph.",
        "- Add contract tests at every external boundary.",
        "",
        "### Phase 3 — Specialized capabilities",
        "",
    ]
    for repository in repositories:
        labels = ", ".join(str(node.get("label")) for node in repository.get("graph_nodes", [])[:5]) or "indexed architecture nodes"
        lines.append(f"- From `{repository['spec']}`, evaluate and adapt: {labels}. Verify behavior locally instead of copying repository assumptions.")
    lines += [
        "",
        "### Phase 4 — Evaluation and comparison",
        "",
        "- Reproduce only the metrics required by the product decision.",
        "- Compare selected approaches under identical datasets, hardware, limits, and failure cases.",
        "- Reject improvements that do not beat the baseline within the declared uncertainty.",
        "",
        "### Phase 5 — Production hardening",
        "",
        "- Add load, fault-injection, privacy, security, migration, rollback, and observability tests.",
        "- Run GitNexus impact before central interface changes and preserve Graphify/GitNexus evidence in the handoff.",
        "",
        "## Decision gates",
        "",
        "- Stop if paper-to-repository linkage is only name-based or AMBIGUOUS.",
        "- Stop if the repository license is missing or incompatible.",
        "- Stop if graph health is partial or source provenance cannot be reproduced.",
        "- Require a passing vertical slice before adding a second implementation approach.",
        "",
        "## Token and retrieval budget",
        "",
        f"- Total: {budget.get('total', 'unknown')}",
        f"- Papers: {budget.get('papers', 'unknown')}",
        f"- Repository graph queries: {budget.get('repositories', 'unknown')}",
        f"- Synthesis reserve: {budget.get('synthesis', 'unknown')}",
        f"- Observed Graphify extraction input: {budget.get('observed_graphify_input', 'unknown')}",
        f"- Observed Graphify extraction output: {budget.get('observed_graphify_output', 'unknown')}",
        f"- Observed input exceeded total planning budget: {budget.get('observed_exceeds_total', 'unknown')}",
        "",
        "## Remaining uncertainty",
        "",
    ]
    limitations = payload.get("limitations", [])
    lines.extend(f"- {item}" for item in limitations)
    if not limitations:
        lines.append("- No additional limitation was recorded; independently verify runtime behavior before production use.")
    return "\n".join(lines) + "\n"
