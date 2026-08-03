from __future__ import annotations

from typing import Any

import api
import capability_discovery.llm_search as llm_search
import capability_discovery.postgres_failure_logs as failure_logs
import capability_discovery.runtime_diagnostics as runtime_diagnostics
import capability_discovery.service as discovery_service


def test_disabled_composition_resets_only_optional_services(
    monkeypatch,
) -> None:
    graph = object()
    monkeypatch.setattr(api.app.state, "graph", graph, raising=False)
    monkeypatch.setattr(
        api.app.state, "capability_search_service", object(), raising=False
    )
    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "false")

    api._initialize_capability_discovery()

    assert api.app.state.graph is graph
    assert api.app.state.capability_diagnostic_adapter is None
    assert api.app.state.capability_failure_log_provider is None
    assert api.app.state.capability_search_service is None
    assert api.app.state.capability_search_mode == "disabled"


def test_composition_builds_deterministic_graph_search_without_openai_key(
    monkeypatch,
) -> None:
    snapshot = object()
    provider = object()
    search = object()
    calls: dict[str, Any] = {}

    class Service:
        def __init__(self, repository_root: Any) -> None:
            calls["repository_root"] = repository_root

        @staticmethod
        def snapshot() -> object:
            return snapshot

    def provider_factory(conninfo: str) -> object:
        calls["conninfo"] = conninfo
        return provider

    def adapter_factory(**kwargs: Any) -> object:
        calls["adapter_kwargs"] = kwargs

        class Adapter:
            @staticmethod
            def initialize() -> bool:
                calls["initialized"] = True
                return True

        created = Adapter()
        calls["adapter"] = created
        return created

    def search_factory(received: object, **kwargs: Any) -> object:
        calls["search_snapshot"] = received
        calls["search_kwargs"] = kwargs
        return search

    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(api, "_metadata_conninfo", lambda: "test-conninfo")
    monkeypatch.setattr(discovery_service, "CapabilityDiscoveryService", Service)
    monkeypatch.setattr(
        failure_logs, "PsycopgFailureLogProvider", provider_factory
    )
    monkeypatch.setattr(
        runtime_diagnostics, "RuntimeDiagnosticAdapter", adapter_factory
    )
    monkeypatch.setattr(llm_search, "CapabilityGraphRAGSearch", search_factory)

    api._initialize_capability_discovery()

    assert calls["adapter_kwargs"]["snapshot"] is snapshot
    assert calls["adapter_kwargs"]["failure_log_provider"] is provider
    assert calls["initialized"] is True
    assert calls["search_snapshot"] is snapshot
    assert api.app.state.capability_diagnostic_adapter is calls["adapter"]
    assert api.app.state.capability_failure_log_provider is provider
    assert api.app.state.capability_search_service is search
    assert api.app.state.capability_search_mode == "graph_deterministic"
    assert api.app.state.capability_discovery_error is None


def test_composition_failure_does_not_change_main_graph(
    monkeypatch,
) -> None:
    graph = object()

    class BrokenService:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("projection unavailable")

    monkeypatch.setattr(api.app.state, "graph", graph, raising=False)
    monkeypatch.setenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.setattr(
        discovery_service, "CapabilityDiscoveryService", BrokenService
    )

    api._initialize_capability_discovery()

    assert api.app.state.graph is graph
    assert api.app.state.capability_diagnostic_adapter is None
    assert api.app.state.capability_search_service is None
    assert api.app.state.capability_discovery_error == "RuntimeError"
