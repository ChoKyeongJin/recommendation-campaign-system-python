from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

import analytical_intent
import entity_set
import targeting_expression
from reference_time import ReferenceTimeError


FIXED_REFERENCE_DATE = date(2026, 8, 4)
EXPECTED_30_DAY_CUTOFF = "20260705"
ENTITY_SET_CONFIG_PATH = Path(
    "docs/data/runtime/sql/member_target_filters.json"
)


@pytest.fixture(scope="module")
def entity_set_config() -> dict[str, Any]:
    payload = json.loads(ENTITY_SET_CONFIG_PATH.read_text(encoding="utf-8"))
    return payload["entity_set_targets"]


def _relative_analytical_request() -> tuple[dict[str, Any], dict[str, Any]]:
    intent: dict[str, Any] = {
        "query_type": "aggregate",
        "aggregate_function": "SUM",
        "metric": "purchase_amount",
        "dimensions": [],
        "filters": [{"id": "recent_days", "days": 30}],
        "scopes": [],
        "window_days": 30,
        "source_id": "purchase_detail",
        "member_policy": None,
    }
    return intent, analytical_intent.build_aggregation_request(intent)


def _relative_scope_analytical_request() -> tuple[dict[str, Any], dict[str, Any]]:
    intent: dict[str, Any] = {
        "query_type": "aggregate",
        "aggregate_function": "COUNT",
        "metric": "member_count",
        "dimensions": [],
        "filters": [{"id": "recent_days", "days": 30}],
        "scopes": [{"id": "purchase", "label": "purchase", "negated": False}],
        "window_days": 30,
        "source_id": "member_base",
        "member_policy": None,
    }
    return intent, analytical_intent.build_aggregation_request(intent)


def _entity_set_node(window: dict[str, Any]) -> dict[str, Any]:
    ast = entity_set.build_derived_set_ast(
        member_relation="purchase",
        rank_relation="purchase",
        entity="product",
        measure="sales_quantity",
        direction="top",
        limit=10,
        window=window,
    )
    return {entity_set.DERIVED_SET_AST_FIELD: ast}


def _compile_targeting(
    relation: dict[str, Any],
    config: dict[str, Any],
    *,
    reference_date: date | datetime | None = None,
) -> str:
    return targeting_expression.compile_targeting_expression(
        {"relation": relation},
        config,
        member_predicate=lambda _canonical: None,
        member_alias="B",
        member_key="MEMBER_NO",
        age_column="AGE",
        reference_date=reference_date,
    )


def test_analytical_relative_window_is_deterministic_from_reference_date() -> None:
    intent, request = _relative_analytical_request()

    first = analytical_intent.compile_aggregation_ast(
        intent, request, reference_date=FIXED_REFERENCE_DATE
    )
    second = analytical_intent.compile_aggregation_ast(
        intent, request, reference_date=FIXED_REFERENCE_DATE
    )

    assert first == second
    assert any(EXPECTED_30_DAY_CUTOFF in predicate for predicate in first.where)
    assert all("GETDATE(" not in predicate.upper() for predicate in first.where)


def test_analytical_relative_window_requires_reference_date() -> None:
    intent, request = _relative_analytical_request()

    with pytest.raises(ReferenceTimeError, match="requires reference_date"):
        analytical_intent.compile_aggregation_ast(intent, request)


def test_analytical_relative_scope_uses_the_same_reference_date() -> None:
    intent, request = _relative_scope_analytical_request()

    ast = analytical_intent.compile_aggregation_ast(
        intent, request, reference_date=FIXED_REFERENCE_DATE
    )

    scope = next(predicate for predicate in ast.where if predicate.startswith("EXISTS"))
    assert f"OH.ORDER_DATE >= '{EXPECTED_30_DAY_CUTOFF}'" in scope
    assert "GETDATE(" not in scope.upper()


def test_entity_set_relative_window_is_deterministic_from_reference_date(
    entity_set_config: dict[str, Any],
) -> None:
    node = _entity_set_node({"days": 30})

    first = entity_set.compile_entity_set_predicate(
        node,
        entity_set_config,
        "B",
        "MEMBER_NO",
        reference_date=FIXED_REFERENCE_DATE,
    )
    second = entity_set.compile_entity_set_predicate(
        node,
        entity_set_config,
        "B",
        "MEMBER_NO",
        reference_date=FIXED_REFERENCE_DATE,
    )

    assert first == second
    assert first is not None
    assert EXPECTED_30_DAY_CUTOFF in first
    assert "GETDATE(" not in first.upper()


def test_entity_set_relative_window_without_reference_date_fails_closed(
    entity_set_config: dict[str, Any],
) -> None:
    assert (
        entity_set.compile_entity_set_predicate(
            _entity_set_node({"days": 30}),
            entity_set_config,
            "B",
            "MEMBER_NO",
        )
        is None
    )


def test_targeting_relative_window_is_deterministic_from_reference_date(
    entity_set_config: dict[str, Any],
) -> None:
    relation = {"name": "purchase", "exists": True, "windowDays": 30}

    first = _compile_targeting(
        relation, entity_set_config, reference_date=FIXED_REFERENCE_DATE
    )
    second = _compile_targeting(
        relation, entity_set_config, reference_date=FIXED_REFERENCE_DATE
    )

    assert first == second
    assert EXPECTED_30_DAY_CUTOFF in first
    assert "GETDATE(" not in first.upper()


def test_targeting_relative_window_requires_reference_date(
    entity_set_config: dict[str, Any],
) -> None:
    relation = {"name": "purchase", "exists": True, "windowDays": 30}

    with pytest.raises(
        targeting_expression.TargetingExpressionError,
        match="requires a valid reference_date",
    ):
        _compile_targeting(relation, entity_set_config)


def test_targeting_entity_set_threads_the_reference_date(
    entity_set_config: dict[str, Any],
) -> None:
    relation = {
        "name": "purchase",
        "exists": True,
        "entitySet": {
            "entity": "product",
            "measure": "sales_quantity",
            "direction": "top",
            "limit": 10,
            "windowDays": 30,
        },
    }

    predicate = _compile_targeting(
        relation, entity_set_config, reference_date=FIXED_REFERENCE_DATE
    )
    assert EXPECTED_30_DAY_CUTOFF in predicate
    assert "GETDATE(" not in predicate.upper()

    with pytest.raises(
        targeting_expression.TargetingExpressionError,
        match="requires a valid reference_date",
    ):
        _compile_targeting(relation, entity_set_config)


def test_absolute_windows_remain_reference_free(
    entity_set_config: dict[str, Any],
) -> None:
    entity_predicate = entity_set.compile_entity_set_predicate(
        _entity_set_node({"from": "20190101", "to": "20191231"}),
        entity_set_config,
        "B",
        "MEMBER_NO",
    )
    targeting_predicate = _compile_targeting(
        {"name": "purchase", "exists": True, "year": 2019},
        entity_set_config,
    )

    assert entity_predicate is not None
    assert "BETWEEN '20190101' AND '20191231'" in entity_predicate
    assert "BETWEEN '20190101' AND '20191231'" in targeting_predicate
    assert analytical_intent._filter_sql(
        {"operator": "gte", "value": "20190101"}, "D.ORDER_DATE"
    ) == "D.ORDER_DATE >= '20190101'"


def test_aware_instant_is_supported_but_naive_instant_is_rejected() -> None:
    intent, request = _relative_analytical_request()
    aware = datetime(2026, 8, 4, 5, 0, tzinfo=UTC)

    ast = analytical_intent.compile_aggregation_ast(
        intent, request, reference_date=aware
    )
    assert any(EXPECTED_30_DAY_CUTOFF in predicate for predicate in ast.where)

    with pytest.raises(ReferenceTimeError, match="timezone-aware"):
        analytical_intent.compile_aggregation_ast(
            intent,
            request,
            reference_date=datetime(2026, 8, 4, 5, 0),
        )
