# Examples

## Check prerequisites

The commands below use `<python>` for the verified launcher selected in `SKILL.md`, for example `py -3` on Windows or `python3` on macOS and Linux.

```console
<python> skills/sgrx/scripts/sgrx.py doctor
```

## Resolve npm source from a consumer lockfile

```console
<python> skills/sgrx/scripts/sgrx.py resolve --registry npm --package zod --project /workspace/consumer
```

## Trace a PyPI implementation

```console
<python> skills/sgrx/scripts/sgrx.py analyze --registry pypi --package "httpx==0.28.1" --project /workspace/consumer --question "How is redirect handling implemented?"
```

## Trace a crates.io implementation

```console
<python> skills/sgrx/scripts/sgrx.py analyze --registry crates --package "serde@1.0.228" --project /workspace/consumer --question "How does derive-generated deserialization reach visitor methods?"
```

## Inspect a GitHub ref

```console
<python> skills/sgrx/scripts/sgrx.py analyze --registry github --package "owner/repository@v2.1.0" --project /workspace/consumer --question "Which fallback handles an unavailable native backend?"
```

## Compare versions

```console
<python> skills/sgrx/scripts/sgrx.py compare --registry npm --package zod --from 3.22.0 --to 4.4.3 --project /workspace/consumer --question "What changed in email validation?"
```

## Preview commands

```console
<python> skills/sgrx/scripts/sgrx.py analyze --dry-run --json --registry npm --package zod --project /workspace/consumer --question "Trace parse() into its internal implementation."
```

## Research papers and implementation repositories

After current web discovery produces `research-candidates.json`:

```console
<python> skills/sgrx/scripts/sgrx.py research --project /workspace/product --candidates research-candidates.json --question "How should we build a local multimodal agent?" --max-papers 8 --max-repositories 4 --token-budget 30000 --mode deep
```

Treat all outputs above as command examples only. Derive every conclusion from the current consumer, resolved source, and tool evidence.
