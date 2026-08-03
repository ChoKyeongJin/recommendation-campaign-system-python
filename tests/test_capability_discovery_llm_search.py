from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from capability_discovery.corpus import EvidenceCorpus
from capability_discovery.domain import (
    Evidence,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    RepositoryRevision,
)
from capability_discovery.llm_search import (
    DEFAULT_MODEL,
    RERANK_TOOL_NAME,
    RERANK_TOOL_SCHEMA,
    CapabilityGraphRAGSearch,
    OpenAIStrictRerankCompletion,
    resolve_capability_discovery_model,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence(
    path: str,
    pointer: str,
    digest: str,
    *,
    trust_state: str = "observed",
) -> Evidence:
    return Evidence(
        source_type="test_source",
        source_path=path,
        source_pointer=pointer,
        content_hash=digest,
        trust_state=trust_state,
    )


def _fixture_snapshot(root: Path) -> GraphSnapshot:
    catalog = root / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "metrics": {
                    "purchase_total": {
                        "label": "Purchase total",
                        "window_days": 7,
                        "operator": "gte",
                    },
                    "visit_total": {"label": "Visit total"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = root / "bindings.py"
    source.write_text(
        "HEADER = 'safe'\nPURCHASE_TOTAL = 'orders.amount'\nTAIL = True\n",
        encoding="utf-8",
    )
    catalog_hash = _sha(catalog)
    source_hash = _sha(source)
    approved = GraphNode(
        id="metric:purchase_total",
        kind="metric",
        attributes={
            "label": "Purchase total",
            "trust_state": "approved_projection",
        },
        evidence=(
            _evidence(
                "catalog.json",
                "/metrics/purchase_total",
                catalog_hash,
                trust_state="approved_projection",
            ),
        ),
    )
    observed = GraphNode(
        id="binding:purchase_total",
        kind="compiler_binding",
        attributes={"label": "Purchase total compiler binding"},
        evidence=(
            _evidence("bindings.py", "symbol:PURCHASE_TOTAL#L2-L2", source_hash),
        ),
    )
    neighbor = GraphNode(
        id="source:orders",
        kind="source",
        attributes={"label": "Order fact table"},
        evidence=(
            _evidence(
                "catalog.json",
                "/metrics/purchase_total",
                catalog_hash,
                trust_state="approved_projection",
            ),
        ),
    )
    edge = GraphEdge(
        id="edge:metric-source",
        source=approved.id,
        target=neighbor.id,
        relation="reads_from",
        evidence=approved.evidence,
    )
    revision = RepositoryRevision(
        git_revision="test",
        dirty=False,
        content_sha256="a" * 64,
        source_hashes={
            "catalog.json": catalog_hash,
            "bindings.py": source_hash,
        },
    )
    return GraphSnapshot(
        revision=revision,
        nodes=(approved, observed, neighbor),
        edges=(edge,),
    )


class RecordingCompletion:
    def __init__(self, response: Any | None = None) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []
        self.model = "fake-reranker-v1"

    def __call__(
        self, messages: list[dict[str, str]], tool_schema: dict[str, Any]
    ) -> Any:
        self.calls.append((messages, tool_schema))
        if isinstance(self.response, BaseException):
            raise self.response
        payload = json.loads(messages[1]["content"])
        ids = [item["candidate_id"] for item in payload["candidates"]]
        if self.response is None:
            return {"ranked_candidate_ids": list(reversed(ids))}
        if callable(self.response):
            return self.response(ids)
        return self.response


def _ranked_ids(result: Any) -> list[str]:
    hits = [*result.approved_results, *result.discovery_results]
    return [hit.node.id for hit in sorted(hits, key=lambda item: item.rank)]


def test_graph_retrieval_evidence_chunks_and_valid_closed_set_rerank(
    tmp_path: Path,
) -> None:
    snapshot = _fixture_snapshot(tmp_path)
    completion = RecordingCompletion()
    search = CapabilityGraphRAGSearch(
        snapshot,
        repository_root=tmp_path,
        completion=completion,
    )

    result = search.search("purchase_total", limit=10)

    retrieved_ids = [node.id for node in result.retrieved_nodes]
    assert "metric:purchase_total" in retrieved_ids
    assert "binding:purchase_total" in retrieved_ids
    assert "source:orders" in retrieved_ids  # one-hop expansion, not generation
    assert [edge.id for edge in result.retrieved_edges] == ["edge:metric-source"]
    assert result.rerank_applied is True
    assert result.mode == "llm_rerank"
    assert _ranked_ids(result) == list(reversed(retrieved_ids))
    assert result.candidate_generated is False
    assert result.diagnostic_only is True
    assert result.executable is False
    assert result.cannot_execute_sql is True
    assert result.cannot_mutate_approved_registry is True
    assert search.cannot_execute_sql is True
    assert search.cannot_mutate_approved_registry is True
    assert result.model_version == "fake-reranker-v1"

    payload = json.loads(completion.calls[0][0][1]["content"])
    prompt_ids = {item["candidate_id"] for item in payload["candidates"]}
    assert prompt_ids == set(retrieved_ids)
    assert all("candidate_id" in item for item in payload["candidates"])
    assert payload["constraints"]["candidate_generation_allowed"] is False
    assert payload["evidence_chunks"]
    chunk_kinds = {item["excerpt_kind"] for item in payload["evidence_chunks"]}
    assert "json_pointer" in chunk_kinds
    assert "line_window" in chunk_kinds
    chunk_text = "\n".join(item["text"] for item in payload["evidence_chunks"])
    assert "window_days" in chunk_text
    assert "PURCHASE_TOTAL" in chunk_text

    schema = completion.calls[0][1]["function"]
    assert schema["strict"] is True
    assert schema["parameters"]["required"] == ["ranked_candidate_ids"]
    assert schema["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        {"ranked_candidate_ids": []},
        lambda ids: {"ranked_candidate_ids": [*ids[:-1], "unknown:id"]},
        lambda ids: {"ranked_candidate_ids": [ids[0], ids[0], *ids[2:]]},
        lambda ids: {"ranked_candidate_ids": ids, "extra": True},
    ],
)
def test_invalid_or_open_set_rerank_falls_back_deterministically(
    tmp_path: Path, response: Any
) -> None:
    snapshot = _fixture_snapshot(tmp_path)
    completion = RecordingCompletion(response)
    search = CapabilityGraphRAGSearch(
        snapshot, repository_root=tmp_path, completion=completion
    )

    result = search.search("purchase_total")

    assert result.rerank_applied is False
    assert result.mode == "deterministic_fallback"
    assert result.fallback_reason == "invalid_rerank_output"
    assert _ranked_ids(result) == [node.id for node in result.retrieved_nodes]


def test_provider_error_and_disabled_llm_use_deterministic_results(
    tmp_path: Path,
) -> None:
    snapshot = _fixture_snapshot(tmp_path)
    failed = CapabilityGraphRAGSearch(
        snapshot,
        repository_root=tmp_path,
        completion=RecordingCompletion(RuntimeError("provider unavailable")),
    ).search("purchase_total")
    disabled = CapabilityGraphRAGSearch(
        snapshot, repository_root=tmp_path
    ).search("purchase_total")

    assert failed.fallback_reason == "completion_failed"
    assert failed.mode == "deterministic_fallback"
    assert failed.rerank_applied is False
    assert disabled.mode == "deterministic"
    assert disabled.fallback_reason is None
    assert disabled.model_version == "deterministic-lexical-v1"
    assert _ranked_ids(failed) == _ranked_ids(disabled)


def test_approved_only_filters_after_closed_set_rerank_and_honors_limit(
    tmp_path: Path,
) -> None:
    snapshot = _fixture_snapshot(tmp_path)
    completion = RecordingCompletion()

    result = CapabilityGraphRAGSearch(
        snapshot,
        repository_root=tmp_path,
        completion=completion,
    ).search("purchase_total", approved_only=True, limit=1)

    assert len(result.approved_results) == 1
    assert result.approved_results[0].approved is True
    assert result.discovery_results == ()
    payload = json.loads(completion.calls[0][0][1]["content"])
    assert {item["candidate_id"] for item in payload["candidates"]} == {
        node.id for node in result.retrieved_nodes
    }


def test_no_match_does_not_call_completion(tmp_path: Path) -> None:
    completion = RecordingCompletion()
    result = CapabilityGraphRAGSearch(
        _fixture_snapshot(tmp_path), completion=completion
    ).search("definitely_absent_capability")

    assert result.retrieved_nodes == ()
    assert result.retrieved_edges == ()
    assert result.approved_results == ()
    assert result.discovery_results == ()
    assert completion.calls == []


def _walk_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [*value, *(key for child in value.values() for key in _walk_keys(child))]
    if isinstance(value, list):
        return [key for child in value for key in _walk_keys(child)]
    return []


def test_runtime_serialization_removes_chunk_text_and_node_attributes(
    tmp_path: Path,
) -> None:
    result = CapabilityGraphRAGSearch(
        _fixture_snapshot(tmp_path), repository_root=tmp_path
    ).search("purchase_total")

    full = result.to_dict()
    runtime = result.to_runtime_dict()

    assert any(chunk["text"] for chunk in full["retrieved_chunks"])
    runtime_keys = _walk_keys(runtime)
    assert "text" not in runtime_keys
    assert "attributes" not in runtime_keys
    assert runtime["candidate_generated"] is False
    assert runtime["diagnostic_only"] is True
    assert runtime["executable"] is False
    assert all(
        not item["source_path"].startswith(("/", ".."))
        for node in runtime["retrieved_nodes"]
        for item in node["evidence"]
    )


def test_model_environment_precedence() -> None:
    assert (
        resolve_capability_discovery_model(
            {
                "CAPABILITY_DISCOVERY_LLM_MODEL": "capability-fast",
                "OPENAI_FAST_MODEL": "shared-fast",
            }
        )
        == "capability-fast"
    )
    assert (
        resolve_capability_discovery_model({"OPENAI_FAST_MODEL": "shared-fast"})
        == "shared-fast"
    )
    assert resolve_capability_discovery_model({}) == DEFAULT_MODEL


class _FakeCompletions:
    def __init__(self) -> None:
        self.params: dict[str, Any] | None = None

    def create(self, **params: Any) -> Any:
        self.params = params
        function = SimpleNamespace(
            name=RERANK_TOOL_NAME,
            arguments=json.dumps({"ranked_candidate_ids": ["candidate:one"]}),
        )
        message = SimpleNamespace(
            tool_calls=[SimpleNamespace(function=function)]
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client() -> tuple[Any, _FakeCompletions]:
    completions = _FakeCompletions()
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_openai_adapter_forces_strict_tool_timeout_and_fast_reasoning() -> None:
    client, completions = _fake_client()
    adapter = OpenAIStrictRerankCompletion(
        model="gpt-5.6-mini", timeout=3.25, client=client
    )

    output = adapter([{"role": "user", "content": "rank"}], RERANK_TOOL_SCHEMA)

    assert json.loads(output)["ranked_candidate_ids"] == ["candidate:one"]
    params = completions.params
    assert params is not None
    assert params["timeout"] == 3.25
    assert params["parallel_tool_calls"] is False
    assert params["tool_choice"]["function"]["name"] == RERANK_TOOL_NAME
    assert params["tools"][0]["function"]["strict"] is True
    assert params["reasoning_effort"] == "none"
    assert "temperature" not in params


def test_openai_adapter_lazily_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, completions = _fake_client()
    constructor_args: list[dict[str, Any]] = []

    def fake_openai(**kwargs: Any) -> Any:
        constructor_args.append(kwargs)
        return client

    module = types.ModuleType("openai")
    module.OpenAI = fake_openai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)
    adapter = OpenAIStrictRerankCompletion(model="gpt-4o-mini", timeout=2)

    adapter([{"role": "user", "content": "rank"}], RERANK_TOOL_SCHEMA)

    assert constructor_args == [{"max_retries": 0}]
    assert completions.params is not None
    assert completions.params["temperature"] == 0
    assert "reasoning_effort" not in completions.params


@pytest.mark.parametrize(
    ("relative_path", "content", "max_file_bytes"),
    [
        (".env", b"TOKEN=secret", 1_000),
        ("binary.txt", b"safe\x00secret", 1_000),
        ("credentials.yaml", b"token: secret", 1_000),
        ("large.txt", b"x" * 64, 8),
        ("private.pem", b"PRIVATE KEY", 1_000),
    ],
)
def test_corpus_rejects_secret_binary_and_oversized_sources(
    tmp_path: Path,
    relative_path: str,
    content: bytes,
    max_file_bytes: int,
) -> None:
    path = tmp_path / relative_path
    path.write_bytes(content)
    digest = _sha(path)
    node = GraphNode(
        id="node:test",
        kind="test",
        evidence=(_evidence(relative_path, "", digest),),
    )
    snapshot = GraphSnapshot(
        revision=RepositoryRevision(
            git_revision=None,
            dirty=True,
            content_sha256="b" * 64,
            source_hashes={relative_path: digest},
        ),
        nodes=(node,),
    )

    chunks = EvidenceCorpus(
        tmp_path, max_file_bytes=max_file_bytes
    ).retrieve(snapshot, (node,))

    assert chunks == ()


def test_corpus_rejects_traversal_and_stale_hash(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-capability-source.txt"
    outside.write_text("outside", encoding="utf-8")
    safe = tmp_path / "safe.txt"
    safe.write_text("actual", encoding="utf-8")
    outside_hash = _sha(outside)
    claimed_hash = hashlib.sha256(b"claimed").hexdigest()
    traversal = GraphNode(
        id="node:traversal",
        kind="test",
        evidence=(_evidence("../outside-capability-source.txt", "", outside_hash),),
    )
    stale = GraphNode(
        id="node:stale",
        kind="test",
        evidence=(_evidence("safe.txt", "", claimed_hash),),
    )
    snapshot = GraphSnapshot(
        revision=RepositoryRevision(
            git_revision=None,
            dirty=True,
            content_sha256="c" * 64,
            source_hashes={
                "../outside-capability-source.txt": outside_hash,
                "safe.txt": claimed_hash,
            },
        ),
        nodes=(traversal, stale),
    )

    chunks = EvidenceCorpus(tmp_path).retrieve(snapshot, snapshot.nodes)

    assert chunks == ()


def test_corpus_honors_small_configured_total_bound(tmp_path: Path) -> None:
    source = tmp_path / "safe.txt"
    source.write_text("a long but safe evidence line", encoding="utf-8")
    digest = _sha(source)
    node = GraphNode(
        id="node:bounded",
        kind="test",
        attributes={"label": "bounded"},
        evidence=(_evidence("safe.txt", "", digest),),
    )
    snapshot = GraphSnapshot(
        revision=RepositoryRevision(
            git_revision=None,
            dirty=True,
            content_sha256="d" * 64,
            source_hashes={"safe.txt": digest},
        ),
        nodes=(node,),
    )

    chunks = EvidenceCorpus(
        tmp_path, max_chunk_chars=5, max_total_chars=5
    ).retrieve(snapshot, (node,))

    assert len(chunks) == 1
    assert len(chunks[0].text) <= 5
    assert chunks[0].truncated is True


def test_runtime_serialization_drops_non_relative_evidence_references() -> None:
    digest = "e" * 64
    node = GraphNode(
        id="node:unsafe_ref",
        kind="test",
        attributes={"label": "unsafe_ref"},
        evidence=(_evidence("../secret.txt", "", digest),),
    )
    snapshot = GraphSnapshot(
        revision=RepositoryRevision(
            git_revision=None,
            dirty=True,
            content_sha256="f" * 64,
            source_hashes={"../secret.txt": digest},
        ),
        nodes=(node,),
    )

    runtime = CapabilityGraphRAGSearch(snapshot).search("unsafe_ref").to_runtime_dict()

    assert runtime["retrieved_nodes"][0]["evidence"] == []
