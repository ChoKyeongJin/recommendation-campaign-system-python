from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import semantic_requirements
from aggregation_requirements import (
    AggregationRequest,
    aggregation_request_json_schema,
    parse_aggregation_request,
    validate_aggregation_sql,
)


def test_fractional_ranking_limit_uses_exact_decimal_wire_value() -> None:
    hits = semantic_requirements._ranking_limit_hits(
        "상위 10.1234567890123456789%",
        frozenset({"percent"}),
    )

    assert hits[0][2] == {
        "type": "percent",
        "value": "10.1234567890123456789",
    }


def test_short_fractional_ranking_limit_keeps_numeric_wire_compatibility() -> None:
    hits = semantic_requirements._ranking_limit_hits(
        "상위 0.5%",
        frozenset({"percent"}),
    )

    assert hits[0][2] == {"type": "percent", "value": 0.5}


def _schema(tmp_path: Path) -> Path:
    path = tmp_path / "schema.json"
    path.write_text('{"tables": {}}', encoding="utf-8")
    return path


def _payload(multiply_by: Any) -> dict[str, Any]:
    return {
        "targetEntity": "customer",
        "outputColumns": [],
        "filters": [],
        "groupings": [],
        "aggregations": [
            {"id": "clicks", "function": "count", "distinct": False},
            {"id": "impressions", "function": "count", "distinct": False},
        ],
        "derivedMetrics": [
            {
                "id": "ctr",
                "type": "conversion_rate",
                "alias": "ctr",
                "numeratorMetricId": "clicks",
                "denominatorMetricId": "impressions",
                "multiplyBy": multiply_by,
                "zeroDivisionPolicy": "null",
            }
        ],
        "sorting": [],
        "ranking": {"enabled": False, "partitionBy": []},
        "postAggregationFilters": [],
        "relationConditions": [],
        "dateGrain": None,
        "comparison": None,
        "businessRules": {},
        "assumptions": [],
        "unresolvedFields": [],
    }


def _parse(payload: dict[str, Any], schema_path: Path) -> AggregationRequest:
    request, errors = parse_aggregation_request(payload, schema_path)

    assert request is not None
    assert errors == []
    return request


@pytest.mark.parametrize(
    ("raw", "expected", "wire_value"),
    [
        (0.1, Decimal("0.1"), "0.1"),
        ("0.1", Decimal("0.1"), "0.1"),
        (100, Decimal("100"), 100),
        ("100", Decimal("100"), 100),
    ],
)
def test_multiply_by_is_decimal_internally_and_uses_the_exact_wire_projection(
    tmp_path: Path,
    raw: int | float | str,
    expected: Decimal,
    wire_value: int | str,
) -> None:
    request = _parse(_payload(raw), _schema(tmp_path))

    assert request.derived_metrics[0].multiply_by == expected
    assert request.to_dict()["derivedMetrics"][0]["multiplyBy"] == wire_value
    json.dumps(request.to_dict(), allow_nan=False)


def test_long_fractional_multiplier_round_trips_without_precision_loss(
    tmp_path: Path,
) -> None:
    exact = "1.23456789012345678901234567890123456789"
    payload = _payload(exact)
    Draft202012Validator(aggregation_request_json_schema()).validate(payload)

    request = _parse(payload, _schema(tmp_path))
    wire = request.to_dict()
    assert request.derived_metrics[0].multiply_by == Decimal(exact)
    assert wire["derivedMetrics"][0]["multiplyBy"] == exact

    loaded = json.loads(json.dumps(wire, allow_nan=False))
    restored = _parse(loaded, _schema(tmp_path))
    assert restored.derived_metrics[0].multiply_by == Decimal(exact)
    assert restored.to_dict()["derivedMetrics"][0]["multiplyBy"] == exact


@pytest.mark.parametrize(
    "raw",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "NaN",
        "Inf",
        "Infinity",
        "-Infinity",
    ],
)
def test_non_finite_multiplier_is_rejected_by_the_runtime_parser(
    tmp_path: Path,
    raw: object,
) -> None:
    request, errors = parse_aggregation_request(_payload(raw), _schema(tmp_path))

    assert request is not None
    assert request.derived_metrics[0].multiply_by is None
    assert {
        (error.code, error.path)
        for error in errors
    } >= {("INVALID_DERIVED_METRIC_MULTIPLIER", "derivedMetrics[0].multiplyBy")}


@pytest.mark.parametrize("raw", ["NaN", "Inf", "Infinity", "-Infinity", "1e3", " 1.5"])
def test_non_decimal_strings_are_rejected_by_the_json_schema(raw: str) -> None:
    errors = list(
        Draft202012Validator(aggregation_request_json_schema()).iter_errors(
            _payload(raw)
        )
    )

    assert errors


def test_sql_multiplier_validation_uses_the_exact_decimal_not_a_float_round_trip(
    tmp_path: Path,
) -> None:
    schema_path = _schema(tmp_path)
    exact = Decimal("1.234567890123456789")
    request = _parse(_payload(exact), schema_path)

    exact_sql = (
        "SELECT COUNT(*) AS clicks, COUNT(*) AS impressions, "
        "COUNT(*) * 1.234567890123456789 / NULLIF(COUNT(*), 0) AS ctr"
    )
    exact_result = validate_aggregation_sql(
        request, exact_sql, schema_path, dialect="tsql"
    )
    assert exact_result["valid"] is True

    rounded_sql = exact_sql.replace(
        "1.234567890123456789", "1.2345678901234567"
    )
    rounded_result = validate_aggregation_sql(
        request, rounded_sql, schema_path, dialect="tsql"
    )
    assert "INVALID_PERCENT_MULTIPLIER" in {
        error["code"] for error in rounded_result["errors"]
    }
