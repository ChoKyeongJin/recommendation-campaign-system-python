from __future__ import annotations

import ast
import copy
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api


DISCOVERY_PREFIX = "/api/capability-discovery"
INDEX_REVISION = "a" * 64


@dataclass(frozen=True)
class _Payload:
    value: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.value)


class _FakeDiagnosticAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.initialize_calls = 0
        self.diagnostic_calls: list[dict[str, Any]] = []
        self.summary_calls: list[list[dict[str, Any]]] = []

    def initialize(self, **_: Any) -> bool:
        self.initialize_calls += 1
        return not self.fail

    def status(self) -> dict[str, Any]:
        return {
            "available": not self.fail,
            "reason": "fake_unavailable" if self.fail else None,
            "index_revision": INDEX_REVISION if not self.fail else None,
        }

    def diagnose_failure(
        self,
        failure_code: str,
        received_symbol: str,
        *,
        original_outcome: Any = None,
        limit: int = 5,
        failure_rows: Any = (),
    ) -> _Payload:
        if self.fail:
            raise OSError("diagnostic index unavailable")
        self.diagnostic_calls.append(
            {
                "failure_code": failure_code,
                "received_symbol": received_symbol,
                "original_outcome": original_outcome,
                "limit": limit,
                "failure_rows": list(failure_rows),
            }
        )
        return _Payload(
            {
                "available": True,
                "approved_candidates": [
                    {
                        "candidate_id": "edge:approved:grade",
                        "canonical_id": "member_grade",
                        "alias": "grade",
                        "match_type": "approved_alias_candidate",
                        "trust_state": "approved_projection",
                        "score": 1.0,
                        "executable": False,
                    }
                ],
                "discovery_only_candidates": [
                    {
                        "candidate_id": "edge:observed:customer-grade",
                        "canonical_id_candidate": "customer_grade",
                        "alias": "customer grade",
                        "match_type": "discovery_alias_candidate",
                        "trust_state": "discovery_only",
                        "score": 0.5,
                        "executable": False,
                    }
                ],
                "repeated_failures": [],
                "snapshot_content_sha256": INDEX_REVISION,
                "reason": None,
                "executable": False,
            }
        )

    def diagnose_signal(
        self,
        signal: Any,
        *,
        original_outcome: Any = None,
        limit: int = 5,
        failure_rows: Any = (),
    ) -> _Payload:
        return self.diagnose_failure(
            signal.failure_code,
            signal.received_symbol,
            original_outcome=original_outcome,
            limit=limit,
            failure_rows=failure_rows,
        )

    def summarize_failures(self, rows: Any) -> _Payload:
        if self.fail:
            raise OSError("failure diagnostics unavailable")
        materialized = list(rows)
        self.summary_calls.append(materialized)
        return _Payload(
            {
                "available": True,
                "total_rows": len(materialized),
                "accepted_rows": len(materialized),
                "ignored_rows": 0,
                "repeated_failures": [
                    {
                        "failure_code": "catalog_symbol_unresolved",
                        "subject": "grade",
                        "failure_frequency": len(materialized),
                        "priority_score": float(len(materialized)),
                        "review_only": True,
                        "executable": False,
                    }
                ],
                "reason": None,
                "review_only": True,
                "executable": False,
            }
        )


class _FakeFailureLogProvider:
    def __init__(self) -> None:
        self.limits: list[int] = []

    def load_failure_rows(self, limit: int = 1000, **_: Any) -> list[dict[str, Any]]:
        self.limits.append(limit)
        return [
            {
                "failure_code": "catalog_symbol_unresolved",
                "received_symbol": "grade",
                "prompt": "must not be returned",
                "generated_sql": "SELECT secret_column FROM secret_table",
            },
            {
                "failure_code": "catalog_symbol_unresolved",
                "received_symbol": "grade",
            },
        ][:limit]


class _FakeSearchService:
    unknown_id = "llm:unknown:invented-capability"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        approved_only: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "query": query,
                "limit": limit,
                "approved_only": approved_only,
            }
        )
        return [
            {
                "candidate_id": "canonical:metric:member_grade",
                "canonical_id": "member_grade",
                "trust_state": "approved_projection",
                "approved": True,
                # The HTTP boundary must force this untrusted service value off.
                "executable": True,
            },
            {
                "candidate_id": self.unknown_id,
                "canonical_id_candidate": "invented_capability",
                "trust_state": "llm_inference",
                "approved": False,
                "runtime_candidate": True,
                "executable": True,
            },
        ][:limit]


@pytest.fixture
def diagnostic_state(monkeypatch: pytest.MonkeyPatch) -> tuple[
    _FakeDiagnosticAdapter,
    _FakeFailureLogProvider,
    _FakeSearchService,
]:
    adapter = _FakeDiagnosticAdapter()
    failures = _FakeFailureLogProvider()
    search = _FakeSearchService()
    monkeypatch.setattr(
        api.app.state, "capability_diagnostic_adapter", adapter, raising=False
    )
    monkeypatch.setattr(
        api.app.state, "capability_failure_log_provider", failures, raising=False
    )
    monkeypatch.setattr(
        api.app.state, "capability_search_service", search, raising=False
    )
    monkeypatch.delenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", raising=False)
    return adapter, failures, search


def _client() -> TestClient:
    # Deliberately do not enter the context manager: these contract tests inject
    # app.state and do not need the unrelated runtime graph startup hook.
    return TestClient(api.app)


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


def _list_named(value: Any, name: str) -> list[Any]:
    for mapping in _walk_dicts(value):
        candidate = mapping.get(name)
        if isinstance(candidate, list):
            return candidate
    return []


def _assert_diagnostic_only(payload: dict[str, Any]) -> None:
    assert payload["diagnostic_only"] is True
    assert payload["executable"] is False
    for mapping in _walk_dicts(payload):
        if "executable" in mapping:
            assert mapping["executable"] is False


def test_capability_discovery_http_surface_is_read_only() -> None:
    expected = {
        f"{DISCOVERY_PREFIX}/status",
        f"{DISCOVERY_PREFIX}/diagnostics",
        f"{DISCOVERY_PREFIX}/failures",
        f"{DISCOVERY_PREFIX}/search",
    }
    # FastAPI 0.139 keeps included routers as a lazy ``_IncludedRouter`` in
    # ``app.routes``.  Inspect the router's public route list so this contract
    # remains independent of that internal flattening detail.
    routes = {
        route.path: set(getattr(route, "methods", set()) or set())
        for route in api.capability_discovery_router.routes
        if getattr(route, "path", "").startswith(DISCOVERY_PREFIX)
    }

    assert expected <= set(routes)
    for path, methods in routes.items():
        assert methods <= {"GET", "HEAD"}, f"mutation route exposed: {path} {methods}"


def test_status_endpoint_is_non_executable(
    diagnostic_state: tuple[Any, Any, Any],
) -> None:
    response = _client().get(f"{DISCOVERY_PREFIX}/status")

    assert response.status_code == 200
    payload = response.json()
    _assert_diagnostic_only(payload)
    assert payload["enabled"] is True
    assert payload["available"] is True
    assert payload["index_revision"] == INDEX_REVISION


def test_diagnostics_endpoint_keeps_approved_and_discovery_candidates_separate(
    diagnostic_state: tuple[_FakeDiagnosticAdapter, Any, Any],
) -> None:
    adapter, _, _ = diagnostic_state
    response = _client().get(
        f"{DISCOVERY_PREFIX}/diagnostics",
        params={
            "failure_code": "catalog_symbol_unresolved",
            "received_symbol": "grade",
            "limit": 5,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    _assert_diagnostic_only(payload)
    assert payload["failure_code"] == "catalog_symbol_unresolved"
    assert payload["received_symbol"] == "grade"
    assert payload["index_revision"] == INDEX_REVISION
    assert payload["approved_candidates"][0]["canonical_id"] == "member_grade"
    assert payload["approved_candidates"][0]["trust_state"] == "approved_projection"
    assert payload["discovery_only_candidates"][0]["trust_state"] != "approved_projection"
    assert adapter.diagnostic_calls[-1]["limit"] == 5


@pytest.mark.parametrize(
    "path",
    (
        f"{DISCOVERY_PREFIX}/diagnostics?failure_code=catalog_symbol_unresolved",
        f"{DISCOVERY_PREFIX}/diagnostics?received_symbol=grade",
        (
            f"{DISCOVERY_PREFIX}/diagnostics?failure_code="
            "catalog_symbol_unresolved&received_symbol=grade&limit=0"
        ),
    ),
)
def test_diagnostics_query_contract_rejects_missing_or_invalid_values(
    diagnostic_state: tuple[Any, Any, Any], path: str
) -> None:
    assert _client().get(path).status_code == 422


def test_failure_summary_is_sanitized_and_respects_the_read_limit(
    diagnostic_state: tuple[
        _FakeDiagnosticAdapter,
        _FakeFailureLogProvider,
        Any,
    ],
) -> None:
    adapter, provider, _ = diagnostic_state
    response = _client().get(f"{DISCOVERY_PREFIX}/failures", params={"limit": 7})

    assert response.status_code == 200
    payload = response.json()
    _assert_diagnostic_only(payload)
    repeated = _list_named(payload, "repeated_failures")
    assert repeated and repeated[0]["failure_code"] == "catalog_symbol_unresolved"
    assert repeated[0]["review_only"] is True
    assert provider.limits == [7]
    assert len(adapter.summary_calls) == 1
    serialized = response.text.casefold()
    assert "select secret_column" not in serialized
    assert "must not be returned" not in serialized


def test_search_results_are_diagnostic_and_unknown_llm_ids_are_not_runtime_candidates(
    diagnostic_state: tuple[Any, Any, _FakeSearchService],
) -> None:
    _, _, search = diagnostic_state
    response = _client().get(
        f"{DISCOVERY_PREFIX}/search",
        params={"query": "grade", "limit": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    _assert_diagnostic_only(payload)
    mappings = _walk_dicts(payload)
    unknown = [
        item
        for item in mappings
        if item.get("candidate_id") == search.unknown_id
        or item.get("node_id") == search.unknown_id
    ]
    # The search UI may show an unknown LLM hypothesis, but it can never be an
    # executable/runtime candidate.
    for item in unknown:
        assert item["executable"] is False
        assert item.get("runtime_candidate", False) is False
    runtime_candidate_ids = {
        str(item.get("candidate_id") or item.get("node_id"))
        for key in ("approved_candidates", "discovery_only_candidates")
        for item in _list_named(payload, key)
        if isinstance(item, dict)
    }
    assert search.unknown_id not in runtime_candidate_ids
    assert search.calls == [{"query": "grade", "limit": 5, "approved_only": False}]


def test_search_endpoint_prefers_sanitized_runtime_serialization(
    diagnostic_state: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        @staticmethod
        def to_dict() -> dict[str, Any]:
            return {
                "query": "grade",
                "retrieved_nodes": [{"attributes": {"private": "value"}}],
                "retrieved_chunks": [{"text": "repository excerpt"}],
            }

        @staticmethod
        def to_runtime_dict() -> dict[str, Any]:
            return {
                # Including query verifies the envelope de-duplicates it.
                "query": "grade",
                "retrieved_nodes": [{"id": "canonical:field:grade"}],
                "retrieved_chunks": [{"chunk_id": "chunk:grade"}],
                "approved_results": [],
                "discovery_results": [],
            }

    class Search:
        @staticmethod
        def search(*_: Any, **__: Any) -> Result:
            return Result()

    monkeypatch.setattr(
        api.app.state, "capability_search_service", Search(), raising=False
    )

    response = _client().get(
        f"{DISCOVERY_PREFIX}/search", params={"query": "grade"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "grade"
    assert not any("text" in item for item in _walk_dicts(payload))
    assert not any("attributes" in item for item in _walk_dicts(payload))


def test_disabled_endpoints_return_200_without_calling_services(
    diagnostic_state: tuple[_FakeDiagnosticAdapter, Any, _FakeSearchService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, search = diagnostic_state
    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "false")

    responses = (
        _client().get(f"{DISCOVERY_PREFIX}/status"),
        _client().get(
            f"{DISCOVERY_PREFIX}/diagnostics",
            params={
                "failure_code": "catalog_symbol_unresolved",
                "received_symbol": "grade",
            },
        ),
        _client().get(f"{DISCOVERY_PREFIX}/failures"),
        _client().get(f"{DISCOVERY_PREFIX}/search", params={"query": "grade"}),
    )

    for response in responses:
        assert response.status_code == 200
        payload = response.json()
        _assert_diagnostic_only(payload)
        assert payload["enabled"] is False
    assert adapter.diagnostic_calls == []
    assert adapter.summary_calls == []
    assert search.calls == []


def test_endpoint_dependency_failures_stay_in_a_200_diagnostic_envelope(
    diagnostic_state: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = _FakeDiagnosticAdapter(fail=True)

    class FailingSearch:
        def search(self, *_: Any, **__: Any) -> Any:
            raise TimeoutError("optional LLM search unavailable")

    monkeypatch.setattr(
        api.app.state, "capability_diagnostic_adapter", failing, raising=False
    )
    monkeypatch.setattr(
        api.app.state, "capability_search_service", FailingSearch(), raising=False
    )

    responses = (
        _client().get(f"{DISCOVERY_PREFIX}/status"),
        _client().get(
            f"{DISCOVERY_PREFIX}/diagnostics",
            params={
                "failure_code": "catalog_symbol_unresolved",
                "received_symbol": "grade",
            },
        ),
        _client().get(f"{DISCOVERY_PREFIX}/failures"),
        _client().get(f"{DISCOVERY_PREFIX}/search", params={"query": "grade"}),
    )

    for response in responses:
        assert response.status_code == 200
        payload = response.json()
        _assert_diagnostic_only(payload)
        assert payload["available"] is False
        assert payload["reason"]


def test_postgres_failure_provider_executes_only_read_only_selects() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "capability_discovery"
        / "postgres_failure_logs.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            statements.append(" ".join(first.value.split()))

    assert any(statement.upper() == "SET TRANSACTION READ ONLY" for statement in statements)
    selects = [statement for statement in statements if statement.upper().startswith("SELECT ")]
    assert len(selects) == 1
    assert "campaign_query_failure_logs" in selects[0]
    assert "prompt" not in selects[0].casefold()
    assert "generated_sql" not in selects[0].casefold()
    mutation = re.compile(r"\b(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", re.I)
    assert not [statement for statement in statements if mutation.search(statement)]


def _runtime_result(
    *,
    status: str = "unsupported",
    failure_reason: str | None = "catalog_symbol_unresolved",
    include_symbol: bool = True,
) -> dict[str, Any]:
    unsupported = ["grade"] if include_symbol else []
    semantic_ir = {
        "status": "unsupported",
        "unsupported_operations": (
            [
                {
                    "kind": "catalog_symbol_unresolved",
                    "received_symbol": "grade",
                    "source_span": "grade",
                }
            ]
            if include_symbol
            else []
        ),
    }
    return {
        "api_response": {
            "status": status,
            "failure_reason": failure_reason,
            "error_code": failure_reason,
            "received_symbol": "grade" if include_symbol else None,
            "sql": "SELECT 1" if status == "success" else None,
            "blocked_sql": None,
            "selected_route": "semantic_plan",
            "unsupported_condition_labels": unsupported,
            "dropped_condition_labels": [],
            "missing_input_conditions": [],
            "clarification_questions": ["회원 등급을 확인해 주세요."],
            "semantic_ir": copy.deepcopy(semantic_ir),
            "capability_check": {"is_supported": False, "reasons": unsupported},
        },
        "query_plan": {
            "semantic_ir": copy.deepcopy(semantic_ir),
            "capability_check": {"is_supported": False, "reasons": unsupported},
        },
        "sql_result": {"is_success": False, "selected": None, "candidates": []},
        "message_generation": {},
        "timings_ms": {},
        "stage_log": [],
        "context_assembly": {},
        "vector_matches": [],
        "keyword_matches": [],
    }


def _install_target_sql_fakes(
    monkeypatch: pytest.MonkeyPatch,
    result_factory: Any,
) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []

    def retrieve(**_: Any) -> dict[str, Any]:
        result = result_factory()
        emitted.append(result)
        return result

    monkeypatch.setattr(api.app.state, "graph", object(), raising=False)
    monkeypatch.setattr(api, "retrieve", retrieve)
    monkeypatch.setattr(api, "rag_llm_run_scope", lambda: nullcontext())
    monkeypatch.setattr(
        api,
        "execute_target_sql",
        lambda *args, **kwargs: {
            "is_success": False,
            "status": "skipped",
            "result_type": "not_executed",
            "audience": {},
            "targeting_result": {},
            "segment_composition": {},
            "segment_presentation": {},
        },
    )
    monkeypatch.setattr(
        api, "refresh_message_generation_from_database", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(api, "_save_target_sql_failure_log", lambda *args: None)
    monkeypatch.setattr(api, "_log_timing_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_elapsed_ms", lambda *_: 1.0)
    return emitted


def _target_sql() -> dict[str, Any]:
    return api.target_sql(
        api.TargetSqlRequest(
            prompt="grade 회원을 찾아줘",
            query_parser="rules",
            execute_sql=False,
            persist_targeting=False,
        )
    )


def test_target_sql_diagnostics_are_strictly_additive_and_do_not_mutate_the_plan(
    diagnostic_state: tuple[_FakeDiagnosticAdapter, Any, _FakeSearchService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, search = diagnostic_state
    source = _runtime_result()
    original_plan = copy.deepcopy(source["query_plan"])
    emitted = _install_target_sql_fakes(monkeypatch, lambda: copy.deepcopy(source))

    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "false")
    without_diagnostics = _target_sql()
    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "true")
    with_diagnostics = _target_sql()

    annotation = with_diagnostics.pop("capability_diagnostics")
    _assert_diagnostic_only(annotation)
    assert with_diagnostics == without_diagnostics
    for key in (
        "status",
        "selected_route",
        "sql",
        "blocked_sql",
        "failure_reason",
        "error_code",
        "semantic_ir",
        "capability_check",
        "clarification_questions",
    ):
        assert with_diagnostics.get(key) == without_diagnostics.get(key)
    assert all(result["query_plan"] == original_plan for result in emitted)
    assert adapter.diagnostic_calls[-1]["failure_code"] == "catalog_symbol_unresolved"
    assert adapter.diagnostic_calls[-1]["received_symbol"] == "grade"
    # Runtime diagnostics must not call the optional/LLM search surface.
    assert search.calls == []
    candidate_ids = {
        item["candidate_id"]
        for key in ("approved_candidates", "discovery_only_candidates")
        for item in annotation[key]
    }
    assert search.unknown_id not in candidate_ids


@pytest.mark.parametrize(
    ("status", "failure_reason", "include_symbol"),
    (
        ("success", None, True),
        ("unsupported", "not_allowlisted", True),
        ("unsupported", "catalog_symbol_unresolved", False),
    ),
)
def test_target_sql_does_not_attach_diagnostics_without_an_exact_eligible_failure(
    diagnostic_state: tuple[_FakeDiagnosticAdapter, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    failure_reason: str | None,
    include_symbol: bool,
) -> None:
    adapter, _, _ = diagnostic_state
    _install_target_sql_fakes(
        monkeypatch,
        lambda: _runtime_result(
            status=status,
            failure_reason=failure_reason,
            include_symbol=include_symbol,
        ),
    )
    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "true")

    response = _target_sql()

    assert "capability_diagnostics" not in response
    assert adapter.diagnostic_calls == []


def test_target_sql_diagnostic_failure_is_fail_open(
    diagnostic_state: tuple[Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = _FakeDiagnosticAdapter(fail=True)
    monkeypatch.setattr(
        api.app.state, "capability_diagnostic_adapter", failing, raising=False
    )
    source = _runtime_result()
    _install_target_sql_fakes(monkeypatch, lambda: copy.deepcopy(source))

    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "false")
    expected = _target_sql()
    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "true")
    actual = _target_sql()

    assert "capability_diagnostics" not in actual
    assert actual == expected
