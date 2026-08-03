# Canonical Capability Discovery: offline G0 architecture

## Status and scope

This document defines the first, offline capability-discovery layer. It measures differences between the project's approved canonical declarations, legacy assets, and observed implementation evidence. It produces reproducible gap reports and review candidates; it does not participate in request parsing, planning, SQL compilation, or execution.

The G0 layer has three hard boundaries:

1. Existing runtime registries remain the only sources of truth (SSOT).
2. `ApprovedCapabilityProjection` is a read-only view derived from those registries, never a second writable registry.
3. Discovery facts, gaps, and candidates are non-executable evidence. Runtime code must not read them to decide support, routing, lowering, SQL, or failure outcomes.

The current authorities and implementation evidence projected by G0 are:

- `docs/data/runtime/semantics/audience_catalog.json`;
- `docs/data/runtime/semantics/semantic_capabilities.json`;
- `docs/data/runtime/sql/member_target_filters.json` and metric registries;
- `semantic_plan.py` and `semantic_plan_event_lowering.py`;
- `targeting_ir.py` and `capability_validation.py`;
- `tools/canonical_coverage_inventory.py` (the existing P4 difference measurement).

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
                         |
                         v
             gap report -> review candidate

runtime request -> canonical pipeline -> compiler/builder
        (never reads the discovery graph or its reports)
```

Facts retain their authority class. An approved declaration, an observed code path, a legacy mapping, a test assertion, and an inference are different evidence even when they mention the same capability.

Namespaced, typed identifiers prevent similarly named concepts from collapsing. Semantic-plan node types, query-plan slots, condition kinds, compiler lanes, builders, precedence rules, exclusive routes, grains, time and coverage policies, and validation gates remain distinct entity kinds. Symbol aliases, surface terms, operator aliases, and value aliases are also distinct.

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

These classifications are deliberately separate from `semantic_plan.FAILURE_CODES` and must never be returned as runtime failure codes. The initial analyzer emits only the subset supported by deterministic evidence: P4 axes currently become `LEGACY_ONLY` records with the missing physical columns, provenance, and blocking questions. It does not manufacture the other classifications merely to populate the taxonomy.

The current `data_availability_policy` is `advise`. Discovery reports unavailable or incomplete coverage as advice and evidence; it does not convert it into an unconditional runtime block.

## Candidate contract and review

A candidate is tied to one gap and source revision. It contains a suggested canonical ID, proposed fields, field-level evidence references, conflicts, blocking questions, and validation results. It is not a patch application mechanism.

Review actions are `new_capability`, `alias_addition`, `operator_alias_addition`, `physical_binding_addition`, `value_mapping_addition`, `lowering_required`, `compiler_required`, `expressible_false_declaration`, `conflict_resolution`, or `stale_asset_removal`.

G0 never promotes a candidate, writes to an approved registry, or mutates runtime code. `tools.capability_review promote` returns an explicit boundary error. Applying a candidate means creating a separate reviewed source patch/PR and then running the normal validation and regression suite.

Files are written only when the caller supplies `--output`; there is no implicit output directory and no default under `artifacts/`.

Machine-readable contracts:

- `docs/data/schemas/capability_gap_report.schema.json`
- `docs/data/schemas/capability_candidate.schema.json`

## CLI

```bash
# full, deterministic in-memory build; summary only
python -m tools.capability_graphrag index --repo-root .

# persist only to an explicit path
python -m tools.capability_graphrag index --repo-root . --output reports/capability-snapshot.json

python -m tools.capability_graphrag gaps --repo-root . --format json
python -m tools.capability_graphrag explain canonical:metric:member_grade --repo-root .
python -m tools.capability_graphrag aliases grade --repo-root . --approved-only
python -m tools.capability_graphrag candidate active_state --repo-root .
python -m tools.capability_graphrag verify --repo-root . --fail-on-conflict

python -m tools.capability_review show path/to/candidate.json --format json
python -m tools.capability_review validate path/to/candidate.json --format json
# always blocked in G0
python -m tools.capability_review promote path/to/candidate.json --format json
```

`verify` fails only for approved/deterministic projection errors (or deterministic conflicts when requested). Newly observed legacy gaps remain warnings and do not change runtime support.

## Optional GraphRAG decision

G0 uses no embeddings, vector database, or LLM. Deterministic inventory, typed traversal, exact search, and provenance are measured first. GraphRAG remains an optional later search layer over a separate discovery corpus.

A GraphRAG phase requires an explicit go/no-go review showing:

- representative questions deterministic traversal cannot answer adequately;
- a versioned evaluation set and measurable relevance/recall improvement;
- reproducible answers retaining source-level provenance;
- no runtime parsing, planning, compilation, or execution dependency;
- a separate index/collection and deletion/re-index lifecycle;
- acceptable cost and absence behavior.

Without that evidence, the MultiDiGraph and deterministic report are the complete implementation.
