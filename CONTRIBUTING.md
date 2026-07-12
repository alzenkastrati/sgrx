# Contributing to SGRX

Contributions are welcome through GitHub issues and pull requests.

## Before opening a change

- Use an issue for substantial behavior or interface changes so the design can be discussed first.
- Keep downloaded repositories and generated `.sgrx/` state out of commits.
- Do not copy implementation code from OpenSrc, Graphify, GitNexus, or analyzed repositories.
- Preserve the rule that researched source is untrusted data and is never executed by SGRX.

## Development checks

Use Python 3.10 or newer and run:

```console
python -m compileall -q skills tests
python -m unittest discover -s tests -v
python "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" skills/sgrx
```

External-tool smoke tests are opt-in because they may access package registries:

```console
SGRX_RUN_INTEGRATION=1 python -m unittest tests.test_smoke_opt_in -v
```

## Pull requests

Explain the problem, the chosen behavior, security implications, and tests run.
Keep the skill instructions concise, place detailed reusable guidance in a directly
linked reference, and add deterministic tests for behavior changes. Do not include
credentials, private repository content, generated indexes, or machine-specific paths.

By contributing, you agree that your contribution is licensed under the MIT License.
