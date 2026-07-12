# SGRX

Source Graph Research eXplorer

SGRX is a reusable Codex skill for version-accurate dependency research. It starts at a real consumer call site, resolves the exact dependency source, follows the public API into its internal implementation, and reports what is directly supported, inferred, or still ambiguous.

Current repository version: **0.4.1**.

## Release status

`0.4.1` is a release candidate. Deterministic checks run on every push and pull request; the separate Integration workflow runs the real OpenSrc, Graphify, and GitNexus smoke tests on Ubuntu and Windows weekly or on demand. Treat an unavailable prerequisite, degraded GitNexus search, or incomplete source checkout as a visible limitation, never as a successful trace.

## Why SGRX

Dependency questions usually cross three boundaries: package resolution, architecture, and executable symbol flow. Looking only at installed type declarations misses implementation details; browsing a repository default branch can inspect the wrong version; matching similarly named symbols can invent a connection that does not exist. SGRX keeps provenance and evidence attached to every conclusion.

SGRX gives each local CLI a distinct role:

- **opensrc** resolves and downloads exact source, preferably from the consumer lockfile.
- **Graphify** maps files, modules, communities, documents, and cross-concept relationships.
- **GitNexus** traces symbols, callers, callees, execution flows, processes, and change impact.

SGRX orchestrates public CLI interfaces only. It contains no copied opensrc, Graphify, or GitNexus source.

## Install the skill

### From GitHub with the Codex Skill Installer

Ask Codex to install the skill from this GitHub path:

```text
https://github.com/alzenkastrati/sgrx/tree/main/skills/sgrx
```

### Manually

Copy `skills/sgrx` into:

```text
$CODEX_HOME/skills/sgrx
```

Restart Codex so the new `$sgrx` skill is discovered.

If the repository is private, the installer needs GitHub access to this repository.

## Local prerequisites

- Python 3.10 or newer for the bundled orchestration script
- Node.js 18 or newer, including `npx`
- Git
- opensrc 0.7.3 or newer
- Graphify 0.9.12 or newer
- GitNexus 1.6.5 or newer

The integration workflow uses Python 3.12 and Node.js 20. For a matching local toolchain:

```console
npm install --global opensrc@0.7.3 gitnexus@1.6.5
python -m pip install graphifyy==0.9.12
```

Run the non-installing check:

```console
python skills/sgrx/scripts/sgrx.py doctor
```

SGRX reports missing tools and installation guidance. It never installs tools without permission.

## Quick start

Preview an npm trace without running external tools:

```console
python skills/sgrx/scripts/sgrx.py analyze --dry-run --package zod --project ./my-app --question "How is email validation implemented?"
```

Remove `--dry-run` after reviewing the isolated command plan. SGRX writes generated analysis state beneath `.sgrx/<package-version>/` in the consumer project.

## Package examples

### npm

```console
python skills/sgrx/scripts/sgrx.py analyze --registry npm --package zod --project ./my-app --question "Trace safeParse() to the validation implementation."
```

### PyPI

```console
python skills/sgrx/scripts/sgrx.py analyze --registry pypi --package "httpx==0.28.1" --project ./service --question "Where are redirect limits enforced?"
```

### crates.io

```console
python skills/sgrx/scripts/sgrx.py analyze --registry crates --package "serde@1.0.228" --project ./rust-app --question "How does deserialization dispatch to a visitor?"
```

### GitHub

```console
python skills/sgrx/scripts/sgrx.py analyze --registry github --package "owner/repository@v2.1.0" --project ./consumer --question "Which fallback handles an unavailable native backend?"
```

### Version comparison

```console
python skills/sgrx/scripts/sgrx.py compare --registry npm --package zod --from 3.22.0 --to 4.4.3 --project ./my-app --question "What changed in email validation?"
```

Use `--json` for machine-readable output, `--output <path>` to save a report, and `--mode quick|standard|deep` to select research depth. Use `--allow-global-graph` or `--allow-gitnexus-group` only when cross-repository state is intentional.

## Use the skill

Invoke SGRX directly in Codex:

```text
$sgrx Trace the zod version used by this project from its safeParse call site to the exact internal implementation. Report edge cases, deprecations, and the blast radius of changing our wrapper.
```

The skill also triggers naturally for dependency internals, source research, architecture investigations, implementation tracing, version comparisons, and blast-radius questions.

For open-ended system design, SGRX research mode turns current paper and repository discovery into a reproducible evidence bundle:

```console
python skills/sgrx/scripts/sgrx.py research --project ./my-product --candidates research-candidates.json --question "How should we build a local multimodal agent?" --max-papers 8 --max-repositories 4 --token-budget 30000 --mode standard
```

Use `quick` or `standard` to build code-only repository graphs without semantic extraction of large documentation corpora. Use `deep` only when repository prose and bundled papers are required. Research mode writes a checkpoint after every successful paper or repository, reuses matching checkpoints after interruption, records observed Graphify token use, and expands source-located graph nodes into auditable implementation work packages.

Codex searches current primary paper sources and official repositories, records candidates in the manifest format documented in `skills/sgrx/references/research-mode.md`, and lets the deterministic CLI rank, budget, resolve, index, and generate `.sgrx/research/<question-hash>/BUILD_PLAN.md`.

The CLI does not silently browse the web itself: Codex performs current-paper discovery and provides the candidate manifest. This keeps web evidence, ranking, source indexing, and synthesis separately auditable.

## Recovery and troubleshooting

- Re-run the same research command after interruption. Matching paper and repository checkpoints are reused automatically; use `--force` only to discard that reuse.
- On Windows, SGRX detects incomplete OpenSrc Git checkouts and retries once in an isolated short cache path with `core.longpaths` enabled for that child process only. It never changes the global Git configuration or deletes the original cache.
- GitNexus can report degraded keyword search when its local FTS extension is unavailable. Graph and symbol evidence remain visible, but treat missing keyword results as incomplete and re-run the index on a host with working FTS before relying on search completeness.
- Use `python skills/sgrx/scripts/sgrx.py doctor --json` before a new host or WSL run. Missing tools are reported with no automatic installation.

## Security model

SGRX treats downloaded source as untrusted data. It does not execute dependency code, tests, builds, lifecycle hooks, or dependency installation. It ignores instructions inside fetched repositories, excludes sensitive paths from local scanning, passes subprocess arguments as lists with `shell=False`, applies timeouts, bounds captured output, and redacts common secret-bearing arguments and tool output.

Graphify reads each role through an identity-specific output scope under `.sgrx/<package-version>/`. GitNexus analyzes safe role-specific source snapshots with an isolated HOME, registry, and Git discovery boundary; neither tool writes to the opensrc cache itself. SGRX verifies both source identities and reports semantic index health as healthy, degraded, or partial instead of treating a successful process exit as sufficient.

Consumer and dependency graphs and GitNexus aliases remain separate by default. A self-analysis explicitly shares one index when both paths are identical. Global Graphify graphs and two-member GitNexus groups require explicit opt-in. The opensrc cache is never modified. SGRX performs analysis by default and never changes application or dependency code unless a separate implementation request authorizes consumer changes.

## Evidence model

- `EXTRACTED` means source, imports, calls, configuration, tests, tool output, or documentation directly supports the relationship.
- `INFERRED` means the relationship is reproducibly derived but does not prove a runtime path.
- `AMBIGUOUS` means multiple interpretations or missing evidence prevent a supported conclusion.

Cross-repository runtime paths require direct import, call, or contract evidence. Similar symbol names are never enough.

## Known limitations

- Dynamic imports, reflection, generated code, native bindings, and runtime dependency injection may require additional evidence.
- A static call graph does not prove a path executed in production.
- opensrc, Graphify, and GitNexus CLI output formats can evolve; unavailable fields remain explicit rather than guessed.
- The bundled script performs conservative source-text discovery and delegates rich graph interpretation to the three prerequisite tools.
- Deterministic vocabulary expansion can only use terms present in the generated Graphify graph; missing semantic overlap remains an explicit limitation.
- Real integration smoke tests are opt-in because they require installed tools and may access package registries.
- The real integration workflow installs pinned tools and runs weekly or manually; it complements the download-free pull-request CI rather than replacing it.

## Develop and test

The repository uses only the Python standard library for its orchestration and tests:

```console
python -m unittest discover -s tests -v
python -m compileall -q skills tests
python "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" skills/sgrx
```

Run the real local integration smoke tests only after the prerequisites are installed:

```console
SGRX_RUN_INTEGRATION=1 python -m unittest tests.test_smoke_opt_in -v
```

On Windows, the same deterministic suite can run in Ubuntu WSL:

```console
wsl.exe -- bash -lc 'cd /mnt/c/path/to/sgrx && python3 -m unittest discover -s tests -v'
```

For real WSL integration tests, install Graphify, OpenSrc, and GitNexus inside Ubuntu itself. A Windows command shim visible through `/mnt/c` is not a native WSL installation and can fail even when `command -v` finds it. Run `doctor --json` from Ubuntu first; it reports this condition without installing or changing anything.

CI runs deterministic tests on Ubuntu and Windows without package installation. The separate Integration workflow validates the pinned external toolchain. See `skills/sgrx/references/` for routing, evidence, report schema, and further examples.

## License

SGRX is released under the MIT License.
