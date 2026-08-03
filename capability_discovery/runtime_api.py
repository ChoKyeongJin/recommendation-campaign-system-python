"""Read-only FastAPI surface for capability discovery diagnostics.

The router is presentation-only.  Services are supplied through ``app.state``
by the composition root, and every response is recursively forced to remain
non-executable.  Missing optional services return a 200 diagnostic envelope so
they can never change the behavior or availability of the campaign API.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request


LOGGER = logging.getLogger("campaign_api.capability_discovery")
PREFIX = "/api/capability-discovery"
SCHEMA_VERSION = "capability-discovery-runtime/v1"

router = APIRouter(prefix=PREFIX, tags=["capability-discovery"])


def diagnostics_enabled() -> bool:
    value = os.getenv("CAPABILITY_DISCOVERY_DIAGNOSTICS_ENABLED", "true")
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def _plain(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _diagnostic_only(value: Any) -> Any:
    """Detach a payload and remove any accidental execution authority."""

    if isinstance(value, Mapping):
        result = {
            str(key): _diagnostic_only(item)
            for key, item in value.items()
        }
        if "executable" in result:
            result["executable"] = False
        if "runtime_candidate" in result:
            result["runtime_candidate"] = False
        return result
    if isinstance(value, (list, tuple)):
        return [_diagnostic_only(item) for item in value]
    return value


def as_diagnostic_payload(value: Any) -> dict[str, Any]:
    """Return a detached mapping with execution authority stripped recursively."""

    payload = _diagnostic_only(_plain(value))
    if not isinstance(payload, dict):
        payload = {"results": payload}
    payload["diagnostic_only"] = True
    payload["executable"] = False
    return payload


def _envelope(
    *,
    enabled: bool,
    available: bool,
    reason: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "enabled": enabled,
        "available": available,
        "diagnostic_only": True,
        "executable": False,
        "reason": reason,
        **payload,
    }
    sanitized = _diagnostic_only(_plain(result))
    sanitized["diagnostic_only"] = True
    sanitized["executable"] = False
    return sanitized


def _service(request: Request, name: str) -> Any | None:
    return getattr(request.app.state, name, None)


def _search_status(request: Request, search: Any | None) -> tuple[bool, bool, str]:
    mode = str(
        getattr(request.app.state, "capability_search_mode", "") or ""
    )
    search_available = search is not None
    llm_available = (
        mode == "graph_llm_rerank" if mode else search_available
    )
    return search_available, llm_available, mode or "injected"


def _status_payload(service: Any) -> dict[str, Any]:
    status = getattr(service, "status", None)
    if callable(status):
        value = _plain(status())
        if isinstance(value, dict):
            return value
    initialize = getattr(service, "initialize", None)
    available = bool(initialize()) if callable(initialize) else service is not None
    return {"available": available, "reason": None if available else "unavailable"}


def _failure_rows(provider: Any, *, limit: int) -> tuple[Any, ...]:
    if provider is None:
        return ()
    loader = getattr(provider, "load_failure_rows", None)
    if not callable(loader):
        return ()
    try:
        return tuple(loader(limit=limit))
    except TypeError:
        return tuple(loader())[:limit]


@router.get("/status")
def capability_discovery_status(request: Request) -> dict[str, Any]:
    enabled = diagnostics_enabled()
    if not enabled:
        return _envelope(
            enabled=False,
            available=False,
            reason="diagnostics_disabled",
        )
    adapter = _service(request, "capability_diagnostic_adapter")
    search = _service(request, "capability_search_service")
    search_available, llm_available, search_mode = _search_status(
        request, search
    )
    if adapter is None:
        return _envelope(
            enabled=True,
            available=False,
            reason="diagnostic_adapter_unavailable",
            search_available=search_available,
            llm_search_available=llm_available,
            search_mode=search_mode,
        )
    try:
        status = _status_payload(adapter)
    except Exception as exc:  # noqa: BLE001 - optional diagnostics are fail-open
        LOGGER.warning(
            "capability_discovery_status_failed error=%s", exc.__class__.__name__
        )
        return _envelope(
            enabled=True,
            available=False,
            reason="diagnostic_status_failed",
            search_available=search_available,
            llm_search_available=llm_available,
            search_mode=search_mode,
        )
    available = bool(status.pop("available", False))
    reason = status.pop("reason", None)
    return _envelope(
        enabled=True,
        available=available,
        reason=reason,
        search_available=search_available,
        llm_search_available=llm_available,
        search_mode=search_mode,
        **status,
    )


@router.get("/diagnostics")
def capability_discovery_diagnostics(
    request: Request,
    failure_code: Annotated[str, Query(min_length=1, max_length=100)],
    received_symbol: Annotated[str, Query(min_length=1, max_length=256)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> dict[str, Any]:
    enabled = diagnostics_enabled()
    if not enabled:
        return _envelope(
            enabled=False,
            available=False,
            reason="diagnostics_disabled",
            failure_code=failure_code,
            received_symbol=received_symbol,
        )
    adapter = _service(request, "capability_diagnostic_adapter")
    if adapter is None:
        return _envelope(
            enabled=True,
            available=False,
            reason="diagnostic_adapter_unavailable",
            failure_code=failure_code,
            received_symbol=received_symbol,
        )
    provider = _service(request, "capability_failure_log_provider")
    try:
        rows = _failure_rows(provider, limit=1_000)
        diagnose = getattr(adapter, "diagnose_failure")
        result = diagnose(
            failure_code,
            received_symbol,
            limit=limit,
            failure_rows=rows,
        )
        payload = _plain(result)
        if not isinstance(payload, dict):
            payload = {"results": payload}
    except Exception as exc:  # noqa: BLE001 - optional diagnostics are fail-open
        LOGGER.warning(
            "capability_discovery_diagnose_failed error=%s", exc.__class__.__name__
        )
        return _envelope(
            enabled=True,
            available=False,
            reason="diagnostic_lookup_failed",
            failure_code=failure_code,
            received_symbol=received_symbol,
        )
    available = bool(payload.pop("available", True))
    reason = payload.pop("reason", None)
    index_revision = payload.pop(
        "index_revision", payload.pop("snapshot_content_sha256", None)
    )
    return _envelope(
        enabled=True,
        available=available,
        reason=reason,
        failure_code=failure_code,
        received_symbol=received_symbol,
        index_revision=index_revision,
        **payload,
    )


@router.get("/failures")
def capability_discovery_failures(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=5_000)] = 1_000,
) -> dict[str, Any]:
    enabled = diagnostics_enabled()
    if not enabled:
        return _envelope(
            enabled=False,
            available=False,
            reason="diagnostics_disabled",
        )
    adapter = _service(request, "capability_diagnostic_adapter")
    provider = _service(request, "capability_failure_log_provider")
    if adapter is None or provider is None:
        return _envelope(
            enabled=True,
            available=False,
            reason="failure_diagnostic_source_unavailable",
        )
    try:
        rows = _failure_rows(provider, limit=limit)
        summarize = getattr(adapter, "summarize_failures")
        summary = _plain(summarize(rows))
        if not isinstance(summary, dict):
            summary = {"repeated_failures": summary}
    except Exception as exc:  # noqa: BLE001 - optional diagnostics are fail-open
        LOGGER.warning(
            "capability_discovery_failure_summary_failed error=%s",
            exc.__class__.__name__,
        )
        return _envelope(
            enabled=True,
            available=False,
            reason="failure_summary_failed",
        )
    available = bool(summary.pop("available", True))
    reason = summary.pop("reason", None)
    return _envelope(
        enabled=True,
        available=available,
        reason=reason,
        rows_scanned=len(rows),
        **summary,
    )


@router.get("/search")
def capability_discovery_search(
    request: Request,
    query: Annotated[str, Query(min_length=1, max_length=512)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    approved_only: bool = False,
) -> dict[str, Any]:
    enabled = diagnostics_enabled()
    if not enabled:
        return _envelope(
            enabled=False,
            available=False,
            reason="diagnostics_disabled",
            query=query,
        )
    service = _service(request, "capability_search_service")
    if service is None:
        return _envelope(
            enabled=True,
            available=False,
            reason="search_service_unavailable",
            query=query,
        )
    try:
        result = service.search(
            query,
            limit=limit,
            approved_only=approved_only,
        )
        runtime_view = getattr(result, "to_runtime_dict", None)
        payload = _plain(runtime_view() if callable(runtime_view) else result)
        if isinstance(payload, list):
            payload = {"results": payload}
        if not isinstance(payload, dict):
            payload = {"results": [payload]}
    except Exception as exc:  # noqa: BLE001 - optional search is fail-open
        LOGGER.warning(
            "capability_discovery_search_failed error=%s", exc.__class__.__name__
        )
        return _envelope(
            enabled=True,
            available=False,
            reason="search_failed",
            query=query,
        )
    available = bool(payload.pop("available", True))
    reason = payload.pop("reason", None)
    payload.pop("query", None)
    return _envelope(
        enabled=True,
        available=available,
        reason=reason,
        query=query,
        **payload,
    )


__all__ = [
    "PREFIX",
    "SCHEMA_VERSION",
    "as_diagnostic_payload",
    "diagnostics_enabled",
    "router",
]
