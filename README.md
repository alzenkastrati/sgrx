# SGRX

Source Graph Research eXplorer

SGRX is a reusable Codex skill for version-accurate dependency research. It starts at a real consumer call site, resolves the exact dependency source, follows the public API into its internal implementation, and reports what is directly supported, inferred, or still ambiguous.

Current repository version: **0.2.1**.

## Why SGRX

Dependency questions usually cross three boundaries: package resolution, architecture, and executable symbol flow. Looking only at installed type declarations misses implementation details; browsing a repository default branch can inspect the wrong version; matching similarly named symbols can invent a connection that does not exist. SGRX keeps provenance and evidence attached to every conclusion.

SGRX gives each local CLI a distinct role:

- **opensrc** resolves and downloads exact source, preferably from the consumer lockfile.
- **Graphify** maps files, modules, communities, documents, and cross-concept relationships.
- **GitNexus** traces symbols, callers, callees, execution flows, processes, and change impact.

SGRX orchestrates public CLI interfaces only. It contains no copied opensrc, Graphify, or GitNexus source.

## Install the skill

### From GitHub with the Codex Skill Installer

Ask Codex to install the skill from this GitHub path after replacing the owner component with the repository owner:

```text
https://github.com/<owner>/sgrx/tree/main/skills/sgrx
```

### Manually

Copy `skills/sgrx` into:

```text
$CODEX_HOME/skills/sgrx
```

Restart Codex so the new `$sgrx` skill is discovered.

## Local prerequisites

- Python 3.10 or newer for the bundled orchestration script
- Node.js 18 or newer
- Git
- opensrc
- Graphify
- npx
- GitNexus

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

## Security model

SGRX treats downloaded source as untrusted data. It does not execute dependency code, tests, builds, lifecycle hooks, or dependency installation. It ignores instructions inside fetched repositories, excludes sensitive paths from local scanning, passes subprocess arguments as lists with `shell=False`, applies timeouts, bounds captured output, and redacts common secret-bearing arguments and tool output.

Graphify writes only to an explicit `.sgrx/<package-version>/` scope. GitNexus analyzes a safe source snapshot with an isolated HOME and registry, never the opensrc cache itself. SGRX verifies source immutability and reports index health as healthy, degraded, or partial.

Consumer and dependency graphs remain separate by default. Global Graphify graphs and GitNexus groups require explicit opt-in. The opensrc cache is never modified. SGRX performs analysis by default and never changes application or dependency code unless a separate implementation request authorizes consumer changes.

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

## Develop and test

The repository uses only the Python standard library for its orchestration and tests:

```console
python -m unittest discover -s tests -v
python -m compileall -q skills tests
python "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" skills/sgrx
```

CI runs on Ubuntu and Windows without package installation. See `skills/sgrx/references/` for routing, evidence, report schema, and further examples.

## License

SGRX is released under the MIT License.
