# Canonical Capability Discovery: G0 inventory, G1 search, and G3 diagnostics

## Status and scope

This document defines the canonical capability-discovery layer now implemented in three bounded stages. G0 measures differences between approved canonical declarations, legacy assets, and observed implementation evidence. G1 searches that typed graph and may use an LLM only to rerank a closed set of retrieved IDs. G3 aggregates technical failure evidence and appends a diagnostic annotation after a runtime outcome is final. None of the stages participates in request parsing, planning, SQL compilation, SQL selection, or execution.

The layer has five hard boundaries:

1. Existing runtime registries remain the only sources of truth (SSOT).
2. `ApprovedCapabilityProjection` is a read-only view derived from those registries, never a second writable registry.
3. Discovery facts, gaps, search hits, and candidates are non-executable evidence. Runtime code must not read them to decide support, routing, lowering, SQL, or failure outcomes.
4. The LLM can reorder every ID in a graph-retrieved closed set exactly once. It cannot invent an ID, approve an observation, generate a promotion candidate, or make a runtime candidate.
5. Runtime diagnostics are additive and fail-open. They run only after the final failure status and SQL/execution correction, preserve the original outcome, and disappear cleanly when disabled or unavailable.

The current authorities and implementation evidence projected by G0 are:

- `docs/data/runtime/semantics/audience_catalog.json`;
- `docs/data/runtime/semantics/semantic_capabilities.json`;
- `docs/data/runtime/sql/member_target_filters.json` and metric registries;
- `targeting_ir.py` and `capability_validation.py`;
- `tools/canonical_coverage_inventory.py` (the existing P4 difference measurement).

`semantic_plan.py` and `semantic_plan_event_lowering.py` were removed from this list on
2026-08-05: the SemanticPlanV2 intermediate representation was retired and both files were
deleted, so the AST extractors that projected their node classes and lowering dispatch were
deleted with them. Projecting a file that does not exist is not evidence — it is advertising.
The node-kind declaration in `semantic_capabilities.json` (`node_types`) was emptied for the
same reason; audience expressibility is now answered by the compiler itself
(`event_compiler.validate_compiler_capability`), not by a declaration.

No ingestor imports `graph_rag.py`. Builder declarations extracted from `capability_validation.py` prove only the declared routing contract; a symbol hit never proves an end-to-end binding, lowering, compiler, and SQL-builder path.

## Data flow and authority

```text
approved registries + implementation evidence + P4 inventory
                         |
                         v  read only
              ApprovedCapabilityProjection
                         |
                         v
        isolated discovery MultiDiGraph / GraphStore
              |                         |
              v                         v
   gap report -> review draft    lexical seeds + one-hop graph facts
                                          |
                          bounded evidence chunks + optional closed-set rerank
                                          |
                                          v
                              diagnostic-only search result

runtime request -> canonical pipeline -> compiler/builder -> final outcome
                                                             |
                                      allowlisted exact failure signal only
                                                             |
                           technical failure history + exact alias evidence
                                                             |
                                                             v
                                         additive capability_diagnostics
```

The canonical pipeline never reads a discovery result. `api.py` is the sole composition and presentation boundary allowed to import this package: it initializes optional services and appends an annotation after the outcome is fixed. Planner, routing, lowering, compiler, and SQL modules remain dependency-barred by tests.

Facts retain their authority class. An approved declaration, an observed code path, a legacy mapping, a test assertion, and an inference are different evidence even when they mention the same capability.

Namespaced, typed identifiers prevent similarly named concepts from collapsing. Semantic-plan node types, query-plan slots, condition kinds, compiler lanes, builders, precedence rules, exclusive routes, grains, time and coverage policies, and validation gates remain distinct entity kinds. Symbol aliases, surface terms, operator aliases, and value aliases are also distinct.

## G1 graph-first search and bounded LLM reranking

`CapabilityGraphRAGSearch` always retrieves before it calls a model:

1. Normalize and tokenize the query.
2. Select deterministic lexical seed nodes from IDs, kinds, and bounded attributes.
3. Expand at most one graph hop within fixed node and edge limits.
4. Build bounded evidence chunks only from repository-relative, hash-matching text sources already referenced by those nodes or edges.
5. Optionally ask one strict tool call to return every retrieved `candidate_id` exactly once in relevance order.
6. Reject missing, duplicate, unknown, or extra IDs and fall back to the deterministic order on malformed output, timeout, SDK error, or provider absence.

This is GraphRAG in the narrow retrieval-augmented sense: graph facts determine the candidate universe and source excerpts ground relevance ranking. It does not use embeddings or a vector database. Secret-like paths, traversal paths, binary files, oversized files, and stale content hashes are excluded from the evidence corpus. Runtime serialization removes excerpt text and node attributes while retaining bounded evidence references.

Approved projection hits and discovery-only observations remain separate result lists. `candidate_generated=false`, `diagnostic_only=true`, and `executable=false` are enforced on the result and every nested hit. An observed or model-ranked item cannot become approved or executable through search.

When LLM search is enabled, model resolution is:

1. `CAPABILITY_DISCOVERY_LLM_MODEL`
2. `OPENAI_FAST_MODEL`
3. `gpt-4o-mini`

The model is used as a latency-sensitive reranker, not as the project's reasoning or structuring model. The adapter forces a strict function schema, disables SDK retries, owns a single timeout budget, and uses deterministic graph order as its fallback.

## Reuse of the deterministic P4 inventory

The legacy-versus-canonical physical coverage measurement belongs to `tools/canonical_coverage_inventory.py`. G0 calls its `scan()` function and records the result and source hash; it does not copy or reinterpret those scanning heuristics.

The inventory's `legacy - canonical` result is the deterministic starting point for `LEGACY_ONLY` gaps. Any exemption remains an explicit inventory input. An extractor may add provenance or graph relations, but it cannot silently change P4 membership.

## Isolated graph store

The discovery graph is separate from the runtime RAG graph and its index or collection. G0 uses a directed multigraph because multiple independently sourced relations may connect the same typed nodes. `NetworkXGraphStore` encapsulates the initial NetworkX `MultiDiGraph`; callers receive domain records, not NetworkX objects.

Each node and edge has a stable ID, typed attributes, and evidence. Evidence records include:

- repository-relative source path;
- JSON Pointer or code symbol identifying the source field;
- extraction method and authority/trust state;
- SHA-256 content hash;
- repository revision when available.

Snapshots record the Git revision, dirty-worktree state, per-source hashes, and a deterministic aggregate hash. The projection implementation files and both JSON Schemas are hashed as inputs too, so changing an uncommitted extractor cannot retain the old snapshot identity. Wall-clock time does not affect identity. A dirty snapshot is valid for local analysis but cannot be treated as reproducible promotion evidence.

## Snapshot and incremental lifecycle

A full rebuild is the reference behavior:

1. Read configured sources at one repository/worktree state.
2. Build a staging projection and graph.
3. Validate IDs, source pointers, conflicts, and referential integrity.
4. Sort and hash the snapshot.
5. Publish only to a caller-selected output path.

Source replacement is atomic in the in-memory store. Replacing or removing a source drops only that source's evidence; a shared fact survives while another source still supports it. This supplies the tombstone semantics needed for deleted and renamed declarations.

Incremental indexing is not exposed as a separate correctness mode in G0. `--changed-since` is accepted as an audit hint but still performs a full rebuild. A future incremental optimizer must be observationally identical to a full build at the same revision; otherwise its snapshot must not be published.

## Gap model

The offline taxonomy is:

- `LEGACY_ONLY`
- `CANONICAL_ONLY`
- `ALIAS_MISSING`
- `PHYSICAL_BINDING_MISSING`
- `OPERATOR_MISMATCH`
- `VALUE_MAPPING_MISSING`
- `LOWERING_MISSING`
- `COMPILER_MISSING`
- `TEST_COVERAGE_MISSING`
- `EXPRESSIBILITY_UNDECLARED`
- `CONFLICTING_DEFINITION`
- `STALE_APPROVED_ASSET`

These classifications are deliberately separate from runtime failure codes and must never be returned as one. (They were originally contrasted with `semantic_plan.FAILURE_CODES`; that module was deleted on 2026-08-05 and the runtime failure vocabulary now lives in `semantic_outcome.py` and `failure_messages.py`.) The initial analyzer emits only the subset supported by deterministic evidence: P4 axes currently become `LEGACY_ONLY` records with the missing physical columns, provenance, and blocking questions. It does not manufacture the other classifications merely to populate the taxonomy.

The current `data_availability_policy` is `advise`. Discovery reports unavailable or incomplete coverage as advice and evidence; it does not convert it into an unconditional runtime block.

## Candidate contract and review

A candidate is tied to one gap and source revision. It contains a suggested canonical ID, proposed fields, field-level evidence references, conflicts, blocking questions, and validation results. It is not a patch application mechanism.

Review actions are `new_capability`, `alias_addition`, `operator_alias_addition`, `physical_binding_addition`, `value_mapping_addition`, `lowering_required`, `compiler_required`, `expressible_false_declaration`, `conflict_resolution`, or `stale_asset_removal`.

G0 never promotes a candidate, writes to an approved registry, or mutates runtime code. `tools.capability_review promote` returns an explicit boundary error. Applying a candidate means creating a separate reviewed source patch/PR and then running the normal validation and regression suite.

Files are written only when the caller supplies `--output`; there is no implicit output directory and no default under `artifacts/`.

Machine-readable contracts:

- `docs/data/schemas/capability_gap_report.schema.json`
- `docs/data/schemas/capability_candidate.schema.json`

## G3 technical failure-log aggregation

`PsycopgFailureLogProvider` opens a read-only PostgreSQL transaction and selects only these technical fields from `campaign_query_failure_logs`:

- `failure_log_id`, `failure_reason`, and `created_at`;
- `query_plan`, `missing_input_conditions`, and `clarification_questions`;
- `stage_log` and `context_metadata`.

The provider does not select the raw prompt, generated SQL, error detail, selected candidate, database result rows, or campaign output. It defensively projects the returned mappings a second time at the trust boundary. Read errors are sanitized and the optional diagnostic path fails open.

The ingestor accepts only the closed failure-code allowlist maintained by `runtime_diagnostics.py`. It extracts exact structured subjects, groups equivalent `(failure_code, subject)` records, and reports repeated failures only when frequency is at least two. Review priority is deterministic:

```text
frequency × user-impact weight × evidence completeness × legacy-asset availability
```

This score prioritizes investigation; it does not change runtime support. Prompt text, free-form model prose, and generated SQL are neither matching keys nor evidence returned by the aggregation API.

## G3 final-failure runtime annotation

The `/target-sql` seam runs after SQL generation, optional database execution, and final status correction. It does nothing for success, non-allowlisted failures, failures without an exact structured subject, disabled diagnostics, or an unavailable adapter. For an eligible failure it may append `capability_diagnostics` to the response and to the failure row's detached `context_metadata`.

Alias suggestions are stricter than general G1 search. They are produced only for exact `catalog_symbol_unresolved` signals from the deterministic approved/observed alias snapshot. The `/target-sql` path does not call the LLM search service. Other allowlisted failures may receive repeated-failure review evidence but never alias candidates.

The annotation code snapshots protected response fields before invoking the adapter. It restores and discards the annotation if an injected adapter attempts to change status, failure reason, error code, SQL, blocked SQL, selected route, semantic IR, capability verdict, or clarification questions. Exceptions are logged by class name and never replace the original response.

## Read-only runtime API

The following router is mounted under `/api/capability-discovery`; all routes are GET-only:

- `/status` — optional subsystem and snapshot readiness;
- `/search` — graph-first search with approved and discovery results kept separate;
- `/failures` — allowlisted technical failure aggregation;
- `/diagnostics` — exact failure-code and received-symbol lookup.

Unavailable and disabled states return HTTP 200 diagnostic envelopes instead of affecting the campaign API's availability. Every envelope is recursively sanitized so any accidental `executable` or `runtime_candidate` field is forced to `false`.

Runtime configuration:

| Variable | Default | Effect |
|---|---:|---|
| `CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED` | `true` | Master switch for service initialization, diagnostic routes, and `/target-sql` annotation |
| `CAPABILITY_DISCOVERY_LLM_SEARCH_ENABLED` | `true` | Enables the LLM reranker only when `OPENAI_API_KEY` is also present; otherwise search stays deterministic |
| `CAPABILITY_DISCOVERY_LLM_MODEL` | unset | Capability reranker override; falls back to `OPENAI_FAST_MODEL`, then `gpt-4o-mini` |
| `CAPABILITY_DISCOVERY_LLM_TIMEOUT_SECONDS` | `12.0` | One-shot runtime rerank timeout before deterministic fallback |
| `CAPABILITY_DISCOVERY_BUILD_TIMEOUT_SECONDS` | `3.0` | Optional snapshot/adapter startup budget |

The discovery startup block is isolated from the existing graph startup. Any snapshot, database, SDK, credential, or provider failure leaves the main graph health and `/target-sql` behavior unchanged.

## CLI

```bash
# full, deterministic in-memory build; summary only
python -m tools.capability_graphrag index --repo-root .

# persist only to an explicit path
python -m tools.capability_graphrag index --repo-root . --output reports/capability-snapshot.json

python -m tools.capability_graphrag gaps --repo-root . --format json
python -m tools.capability_graphrag explain canonical:metric:member_grade --repo-root .
python -m tools.capability_graphrag aliases grade --repo-root . --approved-only

# deterministic graph-first search (default)
python -m tools.capability_graphrag search grade --repo-root . --format json

# explicit closed-set LLM rerank; provider failure falls back deterministically
python -m tools.capability_graphrag search grade --repo-root . --llm --format json

python -m tools.capability_graphrag candidate active_state --repo-root .
python -m tools.capability_graphrag verify --repo-root . --fail-on-conflict

python -m tools.capability_review show path/to/candidate.json --format json
python -m tools.capability_review validate path/to/candidate.json --format json
# promotion remains blocked
python -m tools.capability_review promote path/to/candidate.json --format json
```

`verify` fails only for approved/deterministic projection errors (or deterministic conflicts when requested). Newly observed legacy gaps remain warnings and do not change runtime support.

The CLI never enables the LLM implicitly: `search --llm` is required. `--model` is rejected unless `--llm` is present. Full CLI output may include local bounded excerpts for review; the runtime API uses the sanitized serialization without excerpt text or node attributes.

## Rollout, rollback, and evaluation

Deployment can be reduced in two independent steps without code or schema changes:

1. Set `CAPABILITY_DISCOVERY_LLM_SEARCH_ENABLED=false` to retain deterministic graph search and all G0/G3 functions without provider calls.
2. Set `CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED=false` to disable initialization, search/failure diagnostics, and `/target-sql` annotations. Core parsing, support decisions, compilation, SQL, execution, and existing failure logging continue unchanged.

There is no discovery-owned database schema or runtime registry to roll back. Persisted annotations are non-authoritative historical metadata and may be ignored by older code.

Before expanding beyond closed-set reranking, a go/no-go review must still show a versioned evaluation set, measurable relevance or recall improvement over deterministic traversal, reproducible source provenance, acceptable latency and cost, deletion/re-index lifecycle, and continued absence from runtime decision paths. Embeddings, a vector store, model-generated capability IDs, and automatic promotion remain out of scope.
