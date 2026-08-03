"""Read-only GraphRAG search over a canonical capability snapshot.

The language model is allowed to reorder a closed set of graph node IDs.  It
cannot create capability candidates, approve observations, compile SQL, or
mutate any registry.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .corpus import EvidenceChunk, EvidenceCorpus
from .domain import GraphEdge, GraphNode, GraphSnapshot, thaw_json

MODEL_ENV = "CAPABILITY_DISCOVERY_LLM_MODEL"
FAST_MODEL_ENV = "OPENAI_FAST_MODEL"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS = 12.0
RERANK_TOOL_NAME = "rank_capability_candidates"
LOGGER = logging.getLogger("capability_discovery.llm_search")
RERANK_TOOL_SCHEMA: Mapping[str, Any] = {
    "type": "function",
    "function": {
        "name": RERANK_TOOL_NAME,
        "description": (
            "Rank every supplied canonical capability candidate exactly once. "
            "Never add, remove, rename, approve, or execute a candidate."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "ranked_candidate_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "All supplied candidate_id values in relevance order."
                    ),
                }
            },
            "required": ["ranked_candidate_ids"],
            "additionalProperties": False,
        },
    },
}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def resolve_capability_discovery_model(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the low-latency search model using the documented precedence."""

    values = os.environ if environ is None else environ
    for key in (MODEL_ENV, FAST_MODEL_ENV):
        value = values.get(key, "").strip()
        if value:
            return value
    return DEFAULT_MODEL


class StructuredRerankCompletion(Protocol):
    def __call__(
        self,
        messages: list[dict[str, str]],
        tool_schema: Mapping[str, Any],
    ) -> str | Mapping[str, Any]: ...


class OpenAIStrictRerankCompletion:
    """Minimal OpenAI adapter with a single strict, forced function call."""

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self.model = (model or resolve_capability_discovery_model()).strip()
        if not self.model:
            raise ValueError("model must not be empty")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self._client = client

    def __call__(
        self,
        messages: list[dict[str, str]],
        tool_schema: Mapping[str, Any],
    ) -> str | Mapping[str, Any]:
        if self._client is None:
            from openai import OpenAI

            # The search request owns its one-shot latency budget.  SDK retries
            # would silently multiply that timeout.
            self._client = OpenAI(max_retries=0)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": [copy.deepcopy(dict(tool_schema))],
            "tool_choice": {
                "type": "function",
                "function": {"name": RERANK_TOOL_NAME},
            },
            "parallel_tool_calls": False,
            "temperature": 0,
            "timeout": self.timeout,
        }
        lowered = self.model.casefold()
        if lowered.startswith(("gpt-5", "o1", "o3", "o4")):
            params.pop("temperature", None)
            params["reasoning_effort"] = "none"
        response = self._client.chat.completions.create(**params)
        choices = getattr(response, "choices", None) or []
        if len(choices) != 1:
            raise ValueError("capability rerank completion must return one choice")
        calls = getattr(choices[0].message, "tool_calls", None) or []
        if len(calls) != 1:
            raise ValueError("capability rerank completion must return one tool call")
        function = getattr(calls[0], "function", None)
        if function is None or getattr(function, "name", None) != RERANK_TOOL_NAME:
            raise ValueError("capability rerank tool call is missing or unexpected")
        arguments = getattr(function, "arguments", None)
        if not isinstance(arguments, (str, Mapping)):
            raise ValueError("capability rerank tool arguments are invalid")
        return arguments


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_TOKEN_RE.findall(_normalize(value))))


def _trust_state(node: GraphNode) -> str:
    declared = str(node.attributes.get("trust_state") or "")
    if declared:
        return declared
    states = {item.trust_state for item in node.evidence}
    return next(iter(states)) if len(states) == 1 else "mixed"


def _evidence_refs(node_or_edge: GraphNode | GraphEdge) -> list[dict[str, Any]]:
    refs = [
        {
            "source_path": evidence.source_path.replace("\\", "/"),
            "source_pointer": evidence.source_pointer,
            "source_type": evidence.source_type,
            "trust_state": evidence.trust_state,
            "content_hash": evidence.content_hash,
        }
        for evidence in node_or_edge.evidence
        if _is_repo_relative(evidence.source_path)
    ]
    return refs


def _is_repo_relative(source_path: str) -> bool:
    normalized = source_path.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    parts = normalized.split("/")
    lowered = tuple(part.casefold() for part in parts)
    name = lowered[-1]
    stem = Path(name).stem
    return (
        all(part not in {"", ".", ".."} for part in parts)
        and not re.match(r"^[A-Za-z]:", normalized)
        and not any(
            part
            in {
                ".git",
                ".ssh",
                ".env",
                "credentials",
                "secret",
                "secrets",
            }
            for part in lowered
        )
        and not any(part.startswith(".env.") for part in lowered)
        and name not in {
            "client_secret.json",
            "credentials",
            "credentials.json",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "id_rsa",
            "service-account.json",
            "service_account.json",
            "secrets",
            "secrets.json",
        }
        and stem not in {"credential", "credentials", "secret", "secrets"}
        and not name.endswith((".key", ".p12", ".pem", ".pfx"))
    )


def _bounded_json(value: Any, maximum: int = 2_400) -> Any:
    plain = thaw_json(value)
    rendered = json.dumps(plain, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= maximum:
        return plain
    return {"truncated_json": rendered[:maximum] + "…"}


@dataclass(frozen=True)
class _RetrievedCandidate:
    node: GraphNode
    lexical_score: float
    graph_distance: int
    matched_terms: tuple[str, ...]

    @property
    def deterministic_key(self) -> tuple[float, int, str]:
        return (-self.lexical_score, self.graph_distance, self.node.id)


@dataclass(frozen=True)
class CapabilitySearchHit:
    node: GraphNode
    rank: int
    lexical_score: float
    graph_distance: int
    matched_terms: tuple[str, ...]

    @property
    def trust_state(self) -> str:
        return _trust_state(self.node)

    @property
    def approved(self) -> bool:
        return self.trust_state == "approved_projection"

    @property
    def score(self) -> float:
        """Deterministic retrieval score; never an execution confidence."""

        return self.lexical_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.node.id,
            "kind": self.node.kind,
            "rank": self.rank,
            "score": self.score,
            "lexical_score": self.lexical_score,
            "graph_distance": self.graph_distance,
            "matched_terms": list(self.matched_terms),
            "trust_state": self.trust_state,
            "approved": self.approved,
            "diagnostic_only": True,
            "executable": False,
            "node": self.node.to_dict(),
        }

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.node.id,
            "kind": self.node.kind,
            "rank": self.rank,
            "score": self.score,
            "lexical_score": self.lexical_score,
            "graph_distance": self.graph_distance,
            "trust_state": self.trust_state,
            "approved": self.approved,
            "diagnostic_only": True,
            "executable": False,
        }


@dataclass(frozen=True)
class CapabilitySearchResult:
    query: str
    retrieved_nodes: tuple[GraphNode, ...]
    retrieved_edges: tuple[GraphEdge, ...]
    retrieved_chunks: tuple[EvidenceChunk, ...]
    approved_results: tuple[CapabilitySearchHit, ...]
    discovery_results: tuple[CapabilitySearchHit, ...]
    model_version: str
    index_revision: str
    duration_ms: float
    mode: str
    rerank_applied: bool
    fallback_reason: str | None = None
    candidate_generated: bool = field(default=False, init=False)
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    @property
    def cannot_execute_sql(self) -> bool:
        return True

    @property
    def cannot_mutate_approved_registry(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "retrieved_nodes": [node.to_dict() for node in self.retrieved_nodes],
            "retrieved_edges": [edge.to_dict() for edge in self.retrieved_edges],
            "retrieved_chunks": [chunk.to_dict() for chunk in self.retrieved_chunks],
            "approved_results": [item.to_dict() for item in self.approved_results],
            "discovery_results": [item.to_dict() for item in self.discovery_results],
            "model_version": self.model_version,
            "index_revision": self.index_revision,
            "duration_ms": self.duration_ms,
            "mode": self.mode,
            "rerank_applied": self.rerank_applied,
            "fallback_reason": self.fallback_reason,
            "candidate_generated": False,
            "diagnostic_only": True,
            "executable": False,
        }

    def to_runtime_dict(self) -> dict[str, Any]:
        """Sanitize the result for embedding in a runtime diagnostic record."""

        return {
            "retrieved_nodes": [
                {
                    "id": node.id,
                    "kind": node.kind,
                    "trust_state": _trust_state(node),
                    "evidence": _evidence_refs(node),
                }
                for node in self.retrieved_nodes
            ],
            "retrieved_edges": [
                {
                    "id": edge.id,
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.relation,
                    "evidence": _evidence_refs(edge),
                }
                for edge in self.retrieved_edges
            ],
            "retrieved_chunks": [
                chunk.to_runtime_ref() for chunk in self.retrieved_chunks
            ],
            "approved_results": [
                item.to_runtime_dict() for item in self.approved_results
            ],
            "discovery_results": [
                item.to_runtime_dict() for item in self.discovery_results
            ],
            "model_version": self.model_version,
            "index_revision": self.index_revision,
            "duration_ms": self.duration_ms,
            "mode": self.mode,
            "rerank_applied": self.rerank_applied,
            "fallback_reason": self.fallback_reason,
            "candidate_generated": False,
            "diagnostic_only": True,
            "executable": False,
        }


class CapabilityGraphRAGSearch:
    """Retrieve lexical seeds, expand one graph hop, then optionally rerank."""

    def __init__(
        self,
        snapshot: GraphSnapshot,
        *,
        repository_root: str | Path | None = None,
        completion: StructuredRerankCompletion | None = None,
        model_version: str | None = None,
        corpus: EvidenceCorpus | None = None,
        lexical_limit: int = 16,
        candidate_limit: int = 32,
        edge_limit: int = 64,
    ) -> None:
        if min(lexical_limit, candidate_limit, edge_limit) < 1:
            raise ValueError("search limits must be positive")
        self.snapshot = snapshot
        self.completion = completion
        self.lexical_limit = int(lexical_limit)
        self.candidate_limit = int(candidate_limit)
        self.edge_limit = int(edge_limit)
        if corpus is not None:
            self.corpus = corpus
        elif repository_root is not None:
            self.corpus = EvidenceCorpus(repository_root)
        else:
            self.corpus = None
        completion_model = getattr(completion, "model", None)
        self.model_version = (
            model_version
            or (str(completion_model) if completion_model else None)
            or (
                "injected-completion"
                if completion is not None
                else "deterministic-lexical-v1"
            )
        )

    @property
    def cannot_execute_sql(self) -> bool:
        return True

    @property
    def cannot_mutate_approved_registry(self) -> bool:
        return True

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        approved_only: bool = False,
    ) -> CapabilitySearchResult:
        started = time.perf_counter()
        query = str(query)
        requested = max(1, int(limit))
        candidates, edges = self._retrieve(query)
        nodes = tuple(candidate.node for candidate in candidates)
        chunks = (
            self.corpus.retrieve(self.snapshot, nodes, edges)
            if self.corpus is not None and nodes
            else ()
        )
        ordered = candidates
        rerank_applied = False
        fallback_reason: str | None = None
        mode = "deterministic"
        if candidates and self.completion is not None:
            try:
                ranked_ids = self._rerank(query, candidates, edges, chunks)
                by_id = {candidate.node.id: candidate for candidate in candidates}
                ordered = tuple(by_id[candidate_id] for candidate_id in ranked_ids)
                rerank_applied = True
                mode = "llm_rerank"
            except _InvalidRerankOutput:
                LOGGER.warning(
                    "capability_rerank_invalid_output; using deterministic fallback"
                )
                fallback_reason = "invalid_rerank_output"
                mode = "deterministic_fallback"
            except Exception as exc:
                LOGGER.warning(
                    "capability_rerank_failed error=%s; using deterministic fallback",
                    exc.__class__.__name__,
                )
                fallback_reason = "completion_failed"
                mode = "deterministic_fallback"

        ranked_hits = tuple(
            CapabilitySearchHit(
                node=candidate.node,
                rank=rank,
                lexical_score=candidate.lexical_score,
                graph_distance=candidate.graph_distance,
                matched_terms=candidate.matched_terms,
            )
            for rank, candidate in enumerate(ordered, start=1)
        )
        if approved_only:
            approved_results = tuple(
                hit for hit in ranked_hits if hit.approved
            )[:requested]
            discovery_results: tuple[CapabilitySearchHit, ...] = ()
        else:
            limited_hits = ranked_hits[:requested]
            approved_results = tuple(hit for hit in limited_hits if hit.approved)
            discovery_results = tuple(
                hit for hit in limited_hits if not hit.approved
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return CapabilitySearchResult(
            query=query,
            retrieved_nodes=nodes,
            retrieved_edges=edges,
            retrieved_chunks=chunks,
            approved_results=approved_results,
            discovery_results=discovery_results,
            model_version=self.model_version,
            index_revision=self.snapshot.revision.content_sha256,
            duration_ms=duration_ms,
            mode=mode,
            rerank_applied=rerank_applied,
            fallback_reason=fallback_reason,
        )

    def _retrieve(
        self, query: str
    ) -> tuple[tuple[_RetrievedCandidate, ...], tuple[GraphEdge, ...]]:
        query_terms = _tokens(query)
        if not query_terms:
            return (), ()
        scored: dict[str, _RetrievedCandidate] = {}
        for node in self.snapshot.nodes:
            normalized_id = _normalize(node.id)
            normalized_kind = _normalize(node.kind)
            normalized_attrs = _normalize(
                json.dumps(
                    thaw_json(node.attributes), ensure_ascii=False, sort_keys=True
                )
            )
            matched = tuple(
                term
                for term in query_terms
                if term in normalized_id
                or term in normalized_kind
                or term in normalized_attrs
            )
            if not matched:
                continue
            score = float(len(matched))
            score += sum(2.0 for term in matched if term in normalized_id)
            score += sum(0.5 for term in matched if term in normalized_kind)
            normalized_query = _normalize(query).strip()
            if normalized_query and normalized_query in normalized_attrs:
                score += 2.0
            scored[node.id] = _RetrievedCandidate(
                node=node,
                lexical_score=score,
                graph_distance=0,
                matched_terms=matched,
            )
        seeds = tuple(sorted(scored.values(), key=lambda item: item.deterministic_key))[
            : self.lexical_limit
        ]
        if not seeds:
            return (), ()

        nodes_by_id = {node.id: node for node in self.snapshot.nodes}
        selected: dict[str, _RetrievedCandidate] = {
            item.node.id: item for item in seeds
        }
        seed_ids = set(selected)
        incident = tuple(
            edge
            for edge in self.snapshot.edges
            if edge.source in seed_ids or edge.target in seed_ids
        )
        for edge in incident:
            neighbor_id = edge.target if edge.source in seed_ids else edge.source
            if neighbor_id in selected or len(selected) >= self.candidate_limit:
                continue
            neighbor = nodes_by_id[neighbor_id]
            selected[neighbor_id] = _RetrievedCandidate(
                node=neighbor,
                lexical_score=0.0,
                graph_distance=1,
                matched_terms=(),
            )
        candidates = tuple(
            sorted(selected.values(), key=lambda item: item.deterministic_key)
        )[: self.candidate_limit]
        selected_ids = {item.node.id for item in candidates}
        edges = tuple(
            edge
            for edge in incident
            if edge.source in selected_ids and edge.target in selected_ids
        )[: self.edge_limit]
        return candidates, edges

    def _rerank(
        self,
        query: str,
        candidates: Sequence[_RetrievedCandidate],
        edges: Sequence[GraphEdge],
        chunks: Sequence[EvidenceChunk],
    ) -> tuple[str, ...]:
        relations: dict[str, list[dict[str, str]]] = {
            candidate.node.id: [] for candidate in candidates
        }
        for edge in edges:
            if edge.source in relations:
                relations[edge.source].append(
                    {
                        "direction": "out",
                        "relation": edge.relation,
                        "other": edge.target,
                    }
                )
            if edge.target in relations:
                relations[edge.target].append(
                    {"direction": "in", "relation": edge.relation, "other": edge.source}
                )
        payload = {
            "request": _bounded_query(query),
            "constraints": {
                "purpose": "diagnostic capability discovery only",
                "must_return_every_candidate_once": True,
                "candidate_generation_allowed": False,
                "approval_allowed": False,
                "execution_allowed": False,
            },
            "candidates": [
                {
                    "candidate_id": candidate.node.id,
                    "kind": candidate.node.kind,
                    "trust_state": _trust_state(candidate.node),
                    "lexical_score": candidate.lexical_score,
                    "graph_distance": candidate.graph_distance,
                    "matched_terms": list(candidate.matched_terms),
                    "attributes": _bounded_json(candidate.node.attributes),
                    "evidence_refs": _evidence_refs(candidate.node),
                    "one_hop_relations": relations[candidate.node.id],
                }
                for candidate in candidates
            ],
            "evidence_chunks": [chunk.to_dict() for chunk in chunks],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You rank a closed candidate set for read-only capability "
                    "diagnostics. Use only supplied graph facts and evidence. Return "
                    "every candidate_id exactly once through the forced tool; never "
                    "invent IDs or capabilities."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        schema = copy.deepcopy(dict(RERANK_TOOL_SCHEMA))
        raw = self.completion(messages, schema)  # type: ignore[misc]
        try:
            return _validate_ranking(
                raw, tuple(candidate.node.id for candidate in candidates)
            )
        except (TypeError, ValueError) as exc:
            raise _InvalidRerankOutput from exc


def _bounded_query(query: str, maximum: int = 4_000) -> str:
    return query if len(query) <= maximum else query[:maximum] + "…"


class _InvalidRerankOutput(ValueError):
    pass


def _validate_ranking(
    raw: str | Mapping[str, Any], candidate_ids: tuple[str, ...]
) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("rerank output is not valid JSON") from exc
    elif isinstance(raw, Mapping):
        decoded = dict(raw)
    else:
        raise TypeError("rerank output must be JSON text or an object")
    if not isinstance(decoded, Mapping) or set(decoded) != {"ranked_candidate_ids"}:
        raise ValueError("rerank output has an invalid object shape")
    ranked = decoded["ranked_candidate_ids"]
    if (
        not isinstance(ranked, list)
        or any(not isinstance(item, str) for item in ranked)
        or len(ranked) != len(candidate_ids)
        or len(set(ranked)) != len(ranked)
        or set(ranked) != set(candidate_ids)
    ):
        raise ValueError(
            "rerank output must contain every input candidate exactly once"
        )
    return tuple(ranked)


def build_openai_capability_search(
    snapshot: GraphSnapshot,
    repository_root: str | Path,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Any | None = None,
) -> CapabilityGraphRAGSearch:
    """Build the production search adapter without executing a provider call."""

    completion = OpenAIStrictRerankCompletion(
        model=model or resolve_capability_discovery_model(),
        timeout=timeout,
        client=client,
    )
    return CapabilityGraphRAGSearch(
        snapshot,
        repository_root=repository_root,
        completion=completion,
        model_version=completion.model,
    )


__all__ = [
    "CapabilityGraphRAGSearch",
    "CapabilitySearchHit",
    "CapabilitySearchResult",
    "DEFAULT_MODEL",
    "EvidenceChunk",
    "OpenAIStrictRerankCompletion",
    "RERANK_TOOL_SCHEMA",
    "StructuredRerankCompletion",
    "build_openai_capability_search",
    "resolve_capability_discovery_model",
]
