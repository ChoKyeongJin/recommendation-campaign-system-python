from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import api
import networkx as nx
import pytest
from pydantic import ValidationError


EVENT_AT = datetime(2026, 8, 4, 5, 0, tzinfo=UTC)


def test_health_reports_registry_status_without_exposing_error_details(monkeypatch) -> None:
    monkeypatch.setattr(api.app.state, "graph", nx.Graph(), raising=False)
    monkeypatch.setattr(
        api,
        "REGISTRY_HEALTH",
        {
            "member_target_filters": None,
            "metric": "sensitive internal failure detail",
        },
    )

    payload = api.health()

    assert payload["status"] == "error"
    assert payload["registry_health"] == {
        "member_target_filters": "ok",
        "metric": "error",
    }
    assert "sensitive internal failure detail" not in repr(payload)


def test_webhook_conversion_value_keeps_decimal_precision() -> None:
    exact = "12345678901234567890.1234567890123456789"

    request = api.MessageEventWebhookRequest(
        eventType="conversion",
        eventAt=EVENT_AT,
        conversionValueKrw=exact,
    )

    assert request.conversion_value_krw == Decimal(exact)
    assert not isinstance(request.conversion_value_krw, float)


def test_webhook_event_time_must_be_explicit() -> None:
    with pytest.raises(ValidationError) as exc_info:
        api.MessageEventWebhookRequest(eventType="delivered")

    assert any(
        error["loc"] == ("eventAt",) and error["type"] == "missing"
        for error in exc_info.value.errors()
    )


def test_webhook_event_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError) as exc_info:
        api.MessageEventWebhookRequest(
            eventType="delivered",
            eventAt=datetime(2026, 8, 4, 5, 0),
        )

    assert any(
        error["loc"] == ("eventAt",) and error["type"] == "timezone_aware"
        for error in exc_info.value.errors()
    )


def test_webhook_event_time_preserves_explicit_instant() -> None:
    request = api.MessageEventWebhookRequest(
        eventType="delivered",
        eventAt=EVENT_AT,
    )

    assert request.event_at == EVENT_AT


def test_jsonable_decimal_projection_never_uses_binary_float() -> None:
    exact = Decimal("12345678901234567890.1234567890123456789")

    assert api._jsonable_value(exact) == format(exact, "f")
    assert api._jsonable_value(Decimal("100")) == 100
