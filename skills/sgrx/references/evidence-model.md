# Evidence model

## Apply the three statuses

### EXTRACTED

Use `EXTRACTED` only when source, imports, calls, configuration, tests, tool output, or documentation directly establishes the relationship. Cite a file and line or preserve the exact tool record. Distinguish a static call edge from observed runtime execution.

### INFERRED

Use `INFERRED` when the conclusion follows reproducibly from extracted facts but no direct edge proves it. State the inference rule and inputs. Keep confidence below equivalent direct evidence. Never describe an inferred route as an observed execution path.

### AMBIGUOUS

Use `AMBIGUOUS` when several targets fit, dynamic behavior hides resolution, tool output conflicts, or evidence is incomplete. List the alternatives and the evidence needed to decide.

## Score confidence

Use a numeric confidence from 0.00 to 1.00. Treat confidence as support strength, not probability of runtime execution.

- Use 0.90–1.00 for direct, location-backed facts with no material conflict.
- Use 0.70–0.89 for direct facts with a small unresolved condition or strong multi-fact deductions.
- Use 0.40–0.69 for plausible deductions with meaningful gaps.
- Use 0.00–0.39 for weak or conflicting candidates and label them `AMBIGUOUS`.

## Prove project-boundary paths

Require a chain such as consumer import → local wrapper call → exported dependency contract → dependency export → internal implementation. Cite every transition. Accept static imports and calls as structural proof. Require runtime evidence before calling the path observed at runtime.

Reject links based only on matching names, similar signatures, nearby files, shared concepts, or graph proximity. Keep consumer and dependency nodes separate until an import, call, or contract bridges them.

## Record uncertainty

State what is missing, why it matters, and what command or source inspection can resolve it. Preserve disagreements between Graphify, GitNexus, and direct source inspection instead of averaging them away.
