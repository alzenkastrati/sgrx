# Changelog

All notable changes to SGRX are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-07-17

### Added

- Portable multi-agent installer for Codex, Claude Code, Cline, and clients that discover the shared `.agents/skills` location.
- First-class `audit` workflow for comparing benchmark practices with a consumer without inventing runtime dependency edges.
- Corpus profiles and pre-extraction file, image, and token gates for `index`, `analyze`, `compare`, and `audit`.
- Faceted lifecycle, context, distribution, validation, and reliability queries with reusable audit checkpoints.
- Deterministic report verification, durable phase artifacts, JSONL run events, and `RUN_MANIFEST.md` handoffs.

### Changed

- Documented that `agents/openai.yaml` is optional Codex UI metadata while `SKILL.md` is the cross-client Agent Skills interface.
- Treat Graphify zero-node files, extraction issues, and cross-chunk ID collisions as structured degraded health.

### Fixed

- Preserve an exact GitHub SHA ref as commit provenance even when the OpenSrc cache has no `.git` directory.
- Retry missing GitNexus FTS indexes exactly once inside the isolated snapshot and retain degraded status when recovery fails.

## [0.4.1] - 2026-07-12

### Added

- Research mode for ranking current AI papers and exact GitHub implementations.
- Evidence-backed build plans with paper, repository, Graphify, and GitNexus provenance.
- Token budgets, code-only repository snapshots, checkpoints, and resumable research runs.
- Native Windows and Ubuntu integration workflow using pinned OpenSrc, Graphify, and GitNexus versions.
- Automatic Windows long-path recovery for incomplete OpenSrc checkouts.

### Changed

- Hardened subprocess timeouts so Windows process trees started by SGRX are terminated together.
- Expanded README guidance for installation, research mode, recovery, WSL, and known limitations.
- Strengthened source-integrity, index-health, secret-redaction, and cross-platform validation.

### Fixed

- Rejected incomplete OpenSrc Git checkouts instead of treating them as valid source evidence.
- Isolated GitNexus state and prevented parent-repository discovery.
- Reduced Graphify input size for normal research runs by excluding unrelated prose and generated state.
- Reported missing Graphify semantic backends as an explicit partial paper graph instead of requiring a nonexistent artifact.

## [0.2.1] - 2026-07-12

### Fixed

- Improved release validation and repository installation guidance.

## [0.2.0] - 2026-07-12

### Added

- Initial public repository structure, orchestration CLI, deterministic tests, and Codex skill metadata.

[Unreleased]: https://github.com/alzenkastrati/sgrx/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/alzenkastrati/sgrx/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/alzenkastrati/sgrx/compare/v0.2.1...v0.4.1
[0.2.1]: https://github.com/alzenkastrati/sgrx/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/alzenkastrati/sgrx/releases/tag/v0.2.0
