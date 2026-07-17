# Report schema

## Contents

- Markdown report sections
- Standard JSON report
- Provenance and mapping rows
- Research-mode JSON report

## Write Markdown sections

Include these sections in order:

1. Question
2. Short answer
3. Analyzed version and provenance
4. Consumer call sites
5. External implementation
6. End-to-end path
7. Architecture overview
8. Edge cases
9. Deprecations
10. Change risk
11. Evidence table
12. EXTRACTED relationships
13. INFERRED relationships
14. AMBIGUOUS relationships
15. Limitations
16. Recommended next steps
17. Tool versions
18. Executed commands

Use portable dependency citations such as `zod@4.4.3:src/types.ts:810`. Add an absolute opensrc cache path only in provenance. Use repository-relative consumer locations.

## Emit JSON fields

Use this stable top-level shape:

```json
{
  "format_version": "1.0",
  "brand": "SGRX — Source Graph Research eXplorer",
  "question": "string",
  "short_answer": "string",
  "mode": "quick|standard|deep",
  "provenance": {},
  "consumer_call_sites": [],
  "external_implementation": [],
  "end_to_end_path": [],
  "architecture_overview": [],
  "edge_cases": [],
  "deprecations": [],
  "change_risk": {},
  "evidence": [],
  "relationships": {
    "EXTRACTED": [],
    "INFERRED": [],
    "AMBIGUOUS": []
  },
  "limitations": [],
  "recommended_next_steps": [],
  "tool_versions": {},
  "commands": [],
  "verification": {
    "status": "HEALTHY|DEGRADED|DRY_RUN",
    "checks": [],
    "failures": []
  },
  "run_status": "HEALTHY|DEGRADED|DRY_RUN",
  "timestamp": "RFC 3339 string"
}
```

## Populate provenance

Record the requested package, registry or repository, resolved version, tag/ref, commit, consumer project, lockfile, cache path, timestamp, opensrc version, Graphify version, GitNexus version, and exact redacted argument vectors. Use null for unknown values.

## Populate mapping rows

Record `consumer_location`, `package`, `public_api`, `dependency_location`, `gitnexus_symbol_or_process`, `graphify_relationship`, `evidence_status`, `confidence`, and `uncertainties`. Validate the status against `EXTRACTED`, `INFERRED`, and `AMBIGUOUS`.

Never omit a section to conceal unavailable evidence. Use an empty collection and explain the reason in limitations.

Practice audit mode additionally records `audit_mode`, `audit_facets`, `practice_mappings`, a reusable query `checkpoint`, and durable artifact paths. Every mapping records the facet, external practice, consumer equivalent, gap, recommendation, portable locations, evidence status, confidence, and uncertainty. A mapping may be `INFERRED` when both graphs provide relevant anchors, but never `EXTRACTED` merely because similar concepts occur in both repositories.

## Emit research-mode fields

Research mode extends the standard evidence rules with this top-level shape:

```json
{
  "format_version": "1.0",
  "brand": "SGRX — Source Graph Research eXplorer",
  "research_mode": true,
  "question": "string",
  "mode": "quick|standard|deep",
  "artifact_dir": "path",
  "candidates_path": "path",
  "budget": {},
  "papers": [],
  "repositories": [],
  "excluded_papers": [],
  "excluded_repositories": [],
  "relationships": {
    "EXTRACTED": [],
    "INFERRED": [],
    "AMBIGUOUS": []
  },
  "limitations": [],
  "build_plan": "path/to/BUILD_PLAN.md",
  "tool_versions": {},
  "commands": [],
  "timestamp": "RFC 3339 string"
}
```

Preserve the ranking score, estimated token cost, observed Graphify input/output tokens, source profile, indexed file count, checkpoint status, provenance, graph path, graph nodes, index health, and limitations for every selected candidate. Preserve excluded candidates so budget selection is auditable. Treat paper-to-repository linkage as `EXTRACTED` only when an official paper, author, or repository source directly establishes it.
