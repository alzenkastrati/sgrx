# Research mode

Use this workflow when the user asks how best to design or build a system and wants current papers, implementation repositories, source graphs, trade-offs, and a detailed implementation plan.

## Discovery workflow

1. Decompose the request into capabilities, constraints, quality targets, deployment environment, and measurable acceptance criteria.
2. Search the web for current primary paper sources. Prefer arXiv, OpenReview, proceedings, DOI publisher pages, and official project pages. Read titles and abstracts first.
3. Search for repositories linked directly by the paper or authors. Treat name similarity, third-party lists, and unofficial reproductions as `AMBIGUOUS` until independently verified.
4. Build 8–20 paper candidates and 3–10 repository candidates. Record why each candidate is relevant, not only popularity.
5. Write the candidates to JSON using the schema below. Run `sgrx.py research --dry-run --json` before indexing.
6. Run the real research command only after checking the selected count, estimated token use, URLs, refs, licenses, paper-to-repository evidence, and planned repository index commands.
7. Inspect the generated paper and repository graphs. Query only relevant nodes and paths; never load whole repositories into the model context.
8. Refine `BUILD_PLAN.md` from graph evidence. Preserve the generated citations, limitations, rejected alternatives, and decision gates.

## Candidate schema

```json
{
  "question": "How should we build a local multimodal agent?",
  "requirements": [
    "Run on one consumer GPU",
    "Measure end-to-end latency"
  ],
  "papers": [
    {
      "id": "arxiv:2501.12345",
      "title": "Paper title",
      "year": 2026,
      "abstract": "Abstract text",
      "url": "https://arxiv.org/abs/2501.12345",
      "pdf_url": "https://arxiv.org/pdf/2501.12345",
      "source_path": null,
      "citations": 10,
      "relevance": 0.9,
      "official_repository": true,
      "estimated_tokens": 1200,
      "tags": ["streaming", "multimodal"],
      "evidence_status": "EXTRACTED"
    }
  ],
  "repositories": [
    {
      "spec": "owner/repository@v1.2.0",
      "url": "https://github.com/owner/repository",
      "source_path": null,
      "paper_ids": ["arxiv:2501.12345"],
      "official": true,
      "license": "Apache-2.0",
      "relevance": 0.9,
      "architecture_fit": 0.85,
      "reproducibility": 0.8,
      "activity": 0.8,
      "estimated_tokens": 3500,
      "focus_terms": ["streaming", "scheduler", "inference"],
      "notes": "Official implementation linked by the authors.",
      "evidence_status": "EXTRACTED"
    }
  ]
}
```

Use `source_path` for an already available local paper or repository. Otherwise SGRX resolves selected GitHub repositories through OpenSrc. A paper without `source_path` receives an abstract/metadata graph and must state that full-text evidence is unavailable.

## Ranking model

Paper ranking prioritizes question relevance, an official implementation, recency, citation signal, and accessible source text. Repository ranking prioritizes relevance, architecture fit, reproducibility, official paper linkage, verified license, and activity. Scores select evidence for inspection; they do not prove technical correctness.

## Token discipline

- Search metadata and abstracts before retrieving full text.
- Keep only the top candidates that fit `--max-papers`, `--max-repositories`, and `--token-budget`.
- Reserve 20% of the budget for synthesis; divide the remaining retrieval budget between papers and repository graph queries.
- Reuse content-addressed graphs and exact repository refs.
- Use code-only repository snapshots in `quick` and `standard` mode so Graphify performs structural extraction without spending semantic tokens on repository prose. Use `deep` only when documentation, bundled papers, or images are material evidence.
- Query Graphify vocabulary and GitNexus symbols instead of pasting complete documents or source trees.
- Store rankings, exclusions, commands, and evidence in `research-manifest.json` so later sessions do not repeat discovery.
- Record observed Graphify input/output tokens separately from the candidate planning budget and surface any overrun.

## Checkpoints and recovery

Write `checkpoint.json` immediately after each successfully indexed paper or repository. Reuse a checkpoint only when its candidate metadata, question, mode, SGRX version, graph, and isolated GitNexus index still match. Bypass checkpoints with `--force`. Never reuse `UNAVAILABLE` or `PARTIAL` repository results.

Emit progress to stderr while keeping JSON and Markdown output clean. After interruption, rerun the same command without `--force`; completed candidates must be reused instead of indexed again.

## Required outputs

The command writes `.sgrx/research/<question-hash>/research-manifest.json`, per-candidate checkpoints, and `BUILD_PLAN.md`. The plan must include objective, constraints, selected and rejected evidence, source-located graph components, evidence-to-component work packages, implementation phases, evaluation, production hardening, decision gates, estimated and observed token allocation, and unresolved uncertainty.

Do not present the generated plan as final engineering truth. Re-query relevant graph nodes, verify licenses and refs, and distinguish source-backed design facts from recommendations synthesized for the user's constraints.
