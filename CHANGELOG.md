# Changelog

All notable changes to SGRX are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

## [0.2.1] - 2026-07-12

### Fixed

- Improved release validation and repository installation guidance.

## [0.2.0] - 2026-07-12

### Added

- Initial public repository structure, orchestration CLI, deterministic tests, and Codex skill metadata.

[0.4.1]: https://github.com/alzenkastrati/sgrx/compare/v0.2.1...v0.4.1
[0.2.1]: https://github.com/alzenkastrati/sgrx/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/alzenkastrati/sgrx/releases/tag/v0.2.0
