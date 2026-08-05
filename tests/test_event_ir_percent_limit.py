"""Canonical Event IR percentage ranking contract.

The percentage is a relation cardinality unit, not a numeric threshold and not
the root result limit.  These tests keep that unit intact from the LLM wire to
the T-SQL renderer and pin the catalog-owned member-ranking population policy.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import audience_runtime
import audience_schema
import canonical_audience_claims
import canonical_event_ir_grounding
import event_compiler
import event_ir
import graph_rag
import semantic_requirements
import sql_dialect
import sql_guard
from sql_ast import SelectAst, collect_aliases
from query_pipeline.event_query import event_ir_bridge
from query_pipeline.requirement.resolver import required_capabilities
from query_structurer.semantic_ir import extract_literal_bindings
from query_structurer import campaign_plan_v4


QUERY = "누적 구매금액 상위 10% 회원을 추출해줘"
LOWER_QUERY = "누적 구매금액 하위 10% 회원을 추출해줘"
SOURCE = "member_metric_total_buy_amt"
MEMBER_FIELD = f"{SOURCE}.member_id"
VALUE_FIELD = f"{SOURCE}.value"


def _evidence(query: str) -> event_ir.Evidence:
    return event_ir.Evidence(query, 0, len(query))


def _ranking_expression(
    query: str = QUERY,
    *,
    direction: str = "desc",
    count: int | None = None,
    percent: Decimal | None = Decimal("10"),
) -> event_ir.Exists:
    summary = event_ir.Summarize(
        relation=event_ir.Source(SOURCE, correlation="none"),
        keys=(event_ir.NamedExpression("member_id", event_ir.FieldRef(MEMBER_FIELD)),),
        measures=(
            event_ir.NamedMeasure(
                "metric_value", "sum", event_ir.FieldRef(VALUE_FIELD)
            ),
        ),
    )
    ranked = event_ir.Limit(
        relation=event_ir.Order(
            relation=summary,
            keys=(
                event_ir.SortKey("metric_value", direction),
                event_ir.SortKey("member_id", "asc"),
            ),
        ),
        count=count,
        percent=percent,
    )
    membership = event_ir.Join(
        left=event_ir.Source(SOURCE),
        right=ranked,
        on=event_ir.Comparison(
            "=",
            event_ir.FieldRef(MEMBER_FIELD),
            event_ir.FieldRef(MEMBER_FIELD),
            evidence=_evidence(query),
        ),
        kind="semi",
    )
    return event_ir.Exists(membership, evidence=_evidence(query))


def test_count_wire_stays_backward_compatible() -> None:
    relation = event_ir.Limit(event_ir.Source("purchase"), 10)

    assert relation.to_dict() == {
        "type": "limit",
        "relation": {"type": "source", "name": "purchase"},
        "count": 10,
    }
    assert event_ir.relation_from_dict(relation.to_dict()) == relation


def test_percent_wire_round_trips_without_losing_its_unit() -> None:
    relation = event_ir.Limit(
        event_ir.Source("purchase"), percent=Decimal("10.5")
    )

    wire = relation.to_dict()
    assert wire["percent"] == 10.5
    assert "count" not in wire
    assert event_ir.relation_from_dict(wire) == relation


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "limit", "relation": {"type": "source", "name": "purchase"}},
        {
            "type": "limit",
            "relation": {"type": "source", "name": "purchase"},
            "count": 10,
            "percent": 10,
        },
        *[
            {
                "type": "limit",
                "relation": {"type": "source", "name": "purchase"},
                "percent": value,
            }
            for value in (None, True, "10", 0, 100, float("inf"))
        ],
    ],
)
def test_limit_rejects_missing_ambiguous_or_invalid_units(payload: dict) -> None:
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.relation_from_dict(payload)


def test_audience_schema_has_closed_count_and_percent_limit_shapes() -> None:
    validator = Draft202012Validator(audience_schema.audience_expression_json_schema())
    percent_wire = _ranking_expression().to_dict()
    count_wire = _ranking_expression(count=10, percent=None).to_dict()

    validator.validate(percent_wire)
    validator.validate(count_wire)
    invalid = _ranking_expression().to_dict()
    limit = invalid["relation"]["right"]
    limit["count"] = 10
    assert list(validator.iter_errors(invalid))


def test_typed_event_query_bridge_preserves_percent_limit() -> None:
    wire = _ranking_expression().to_dict()

    typed = event_ir_bridge.from_wire(wire)

    assert event_ir_bridge.to_wire(typed) == wire


@pytest.mark.parametrize(
    ("query", "direction", "expected_order"),
    [
        (QUERY, "desc", "ORDER BY SUM(C.TOTAL_BUY_AMT) DESC, C.MEMBER_NO ASC"),
        (LOWER_QUERY, "asc", "ORDER BY SUM(C.TOTAL_BUY_AMT) ASC, C.MEMBER_NO ASC"),
    ],
)
def test_member_percent_ranking_compiles_with_declared_population_policy(
    query: str, direction: str, expected_order: str
) -> None:
    catalog = audience_runtime.resolve_audience_catalog()

    compiled = event_compiler.compile_expression(
        _ranking_expression(query, direction=direction),
        context=catalog.compile_context(literals=True),
    )

    assert "TOP 10 PERCENT" in compiled.sql
    assert expected_order in compiled.sql
    assert "C.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in compiled.sql
    assert "C_MEMBER.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'" in compiled.sql
    assert "C.TOTAL_BUY_AMT IS NOT NULL" in compiled.sql


def test_fractional_percent_uses_valid_tsql_top_expression_syntax() -> None:
    catalog = audience_runtime.resolve_audience_catalog()

    compiled = event_compiler.compile_expression(
        _ranking_expression(percent=Decimal("10.5")),
        context=catalog.compile_context(literals=True),
    )

    assert "TOP (10.5) PERCENT" in compiled.sql


@pytest.mark.parametrize("dialect_name", ["ansi", "mysql", "postgres"])
def test_percent_limit_fails_closed_on_unsupported_dialect(dialect_name: str) -> None:
    catalog = audience_runtime.resolve_audience_catalog()
    context = catalog.compile_context(
        literals=True, dialect=sql_dialect.get_dialect(dialect_name)
    )

    with pytest.raises(event_compiler.SqlCompileError, match="percent"):
        event_compiler.compile_expression(_ranking_expression(), context=context)


def test_percentage_binding_cannot_be_consumed_by_count_limit() -> None:
    bindings = extract_literal_bindings(QUERY, current_date="2026-08-04")
    count_expression = _ranking_expression(count=10, percent=None)

    count_issues = canonical_audience_claims.literal_claim_issues(
        QUERY, count_expression, bindings
    )
    percent_issues = canonical_audience_claims.literal_claim_issues(
        QUERY, _ranking_expression(), bindings
    )

    assert any(issue["argument"].startswith("literal_bindings[") for issue in count_issues)
    assert percent_issues == []


def test_member_metric_registry_materializes_canonical_rank_assets() -> None:
    snapshot = audience_runtime.catalog_snapshot()
    catalog = audience_runtime.resolve_audience_catalog()

    source = snapshot["sources"][SOURCE]
    recipe = snapshot["relation_recipes"]["member_metric_ranking"]
    assert source["table"] == "CRM_MB_MONTHCRMINFO"
    assert snapshot["fields"][VALUE_FIELD]["column"] == "TOTAL_BUY_AMT"
    assert snapshot["metrics"]["total_buy_amt"]["expression_field"] == VALUE_FIELD
    assert recipe["policy"] == {
        "population": "active_members",
        "tie_policy": "exact_count",
        "null_policy": "exclude",
        "missing_policy": "exclude",
        "small_population_policy": "ceil",
    }
    assert catalog.field(VALUE_FIELD).compiler_field.column == "TOTAL_BUY_AMT"


def test_member_metric_rank_is_an_immutable_source_obligation() -> None:
    obligations = [
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(QUERY)
        if requirement.base.get("name") == "member_metric_ranking"
    ]

    assert len(obligations) == 1
    assert obligations[0].value == {
        "direction": "top",
        "limit": {"type": "percent", "value": 10},
        "metric_id": "total_buy_amt",
        "source": SOURCE,
        "entity_field": MEMBER_FIELD,
        "measure_function": "sum",
        "measure_field": VALUE_FIELD,
        "population": "active_members",
        "tie_policy": "exact_count",
        "null_policy": "exclude",
        "missing_policy": "exclude",
        "small_population_policy": "ceil",
    }
    assert canonical_audience_claims.semantic_obligation_issues(
        QUERY, _ranking_expression()
    ) == []


@pytest.mark.parametrize(
    ("query", "expected_limit"),
    [
        (
            "누적 구매금액 상위 0.5% 회원을 추출해줘",
            {"type": "percent", "value": 0.5},
        ),
        (
            "누적 구매금액 상위 1,000명 회원을 추출해줘",
            {"type": "count", "value": 1000},
        ),
    ],
)
def test_member_metric_rank_limit_parser_preserves_decimal_and_grouping(
    query: str, expected_limit: dict[str, object]
) -> None:
    obligation = next(
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(
            query
        )
        if requirement.base.get("name") == "member_metric_ranking"
    )

    assert obligation.value["limit"] == expected_limit


def test_lower_member_metric_rank_uses_the_same_obligation_with_ascending_order() -> None:
    obligation = next(
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(
            LOWER_QUERY
        )
        if requirement.base.get("name") == "member_metric_ranking"
    )

    assert obligation.value["direction"] == "bottom"
    assert canonical_audience_claims.semantic_obligation_issues(
        LOWER_QUERY, _ranking_expression(LOWER_QUERY, direction="asc")
    ) == []


def test_exact_count_policy_requires_the_member_key_tie_break() -> None:
    wire = _ranking_expression().to_dict()
    wire["relation"]["right"]["relation"]["keys"] = [
        wire["relation"]["right"]["relation"]["keys"][0]
    ]

    issues = canonical_audience_claims.semantic_obligation_issues(
        QUERY, event_ir.condition_from_dict(wire)
    )

    assert any(issue["argument"] == "source_semantics.member_metric_ranking" for issue in issues)


def test_limit_capability_preserves_the_selected_unit() -> None:
    count = event_ir_bridge.from_wire(
        _ranking_expression(count=10, percent=None).to_dict()
    )
    percent = event_ir_bridge.from_wire(_ranking_expression().to_dict())

    assert "node.limit.count" in required_capabilities(count)
    assert "node.limit.percent" in required_capabilities(percent)


def test_adding_member_metric_is_registry_only(tmp_path: Path) -> None:
    raw_catalog = copy.deepcopy(audience_runtime.load_audience_catalog_config())
    registry_path = Path(
        audience_runtime.REPO_ROOT,
        "docs",
        "data",
        "runtime",
        "sql",
        "member_metrics.json",
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["metrics"].append(
        {
            "metric_id": "synthetic_value",
            "agg": "SUM",
            "column": "SYNTHETIC_VALUE",
            "ko_label": "합성 지표",
            "synonyms": ["합성 지표"],
        }
    )
    imported_path = tmp_path / "member_metrics.json"
    imported_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    raw_catalog["imports"] = {
        "member_metric_rankings": {
            "path": imported_path.name,
            "relative_to": "catalog",
        }
    }

    materialized = audience_runtime.materialize_member_metric_rankings(
        raw_catalog, catalog_path=tmp_path / "audience_catalog.json"
    )

    assert materialized["sources"]["member_metric_synthetic_value"]["table"] == registry["value_table"]
    assert materialized["fields"]["member_metric_synthetic_value.value"]["column"] == "SYNTHETIC_VALUE"
    assert materialized["metrics"]["synthetic_value"]["relation_recipe"] == "member_metric_ranking"


def test_registry_policy_drift_fails_closed(tmp_path: Path) -> None:
    raw_catalog = copy.deepcopy(audience_runtime.load_audience_catalog_config())
    registry_path = Path(
        audience_runtime.REPO_ROOT,
        "docs",
        "data",
        "runtime",
        "sql",
        "member_metrics.json",
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["canonical_event_ir"]["policy"]["null_policy"] = "include"
    imported_path = tmp_path / "member_metrics.json"
    imported_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    raw_catalog["imports"] = {
        "member_metric_rankings": {
            "path": imported_path.name,
            "relative_to": "catalog",
        }
    }

    with pytest.raises(audience_runtime.AudienceCatalogLoadError, match="policy"):
        audience_runtime.materialize_member_metric_rankings(
            raw_catalog, catalog_path=tmp_path / "audience_catalog.json"
        )


def test_nested_percent_top_does_not_hide_outer_result_limit() -> None:
    sql = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE EXISTS ("
        "SELECT TOP 10 PERCENT C.MEMBER_NO FROM CRM_MB_MONTHCRMINFO C "
        "ORDER BY C.TOTAL_BUY_AMT DESC)"
    )

    guarded = sql_guard._ensure_top(sql, 500)

    assert guarded.startswith("SELECT TOP 500 B.MEMBER_NO")
    assert "SELECT TOP 10 PERCENT" in guarded


def test_catalog_source_aliases_are_accepted_without_a_second_static_list() -> None:
    context = graph_rag._event_compile_context()
    spec = context.event_spec(SOURCE)
    source_sql = event_compiler.render_source_binding(spec, context)
    catalog_aliases = collect_aliases(
        SelectAst(columns=[], from_lines=[f"FROM {source_sql}"])
    )
    validation = graph_rag._sql_validation_config()

    assert validation is not None
    assert catalog_aliases <= set(validation["allowed_table_aliases"])
    assert "UNTRUSTED_PRODUCT" not in validation["allowed_table_aliases"]


def test_grounding_projects_catalog_declared_rank_entity_dependency() -> None:
    catalog = audience_runtime.resolve_audience_catalog()
    projection = canonical_event_ir_grounding.project_canonical_event_ir_grounding(
        QUERY,
        [
            {
                "id": "metric-evidence",
                "table_name": "CRM_MB_MONTHCRMINFO",
                "text_for_embedding": (
                    "누적 구매금액 CRM_MB_MONTHCRMINFO TOTAL_BUY_AMT YYYYMM"
                ),
            }
        ],
        audience_runtime.catalog_snapshot(),
        allowed_fields=catalog.compiler_fields,
        allowed_sources=catalog.compiler_events,
    )

    assert SOURCE in projection["canonical_sources"]
    assert {MEMBER_FIELD, VALUE_FIELD} <= set(projection["canonical_fields"])


def test_v4_normalizes_rank_membership_scope_from_catalog_recipe() -> None:
    expression = _ranking_expression().to_dict()
    join = expression["relation"]
    join["left"] = {"type": "source", "name": "member"}
    join["on"]["left"] = {"type": "field", "name": "member.member_id"}
    raw = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {"expression": expression, "issues": []},
        "semantic_plan": {"nodes": []},
    }

    enriched = campaign_plan_v4.attach_campaign_query_plan_v4_identity(
        raw, QUERY, current_date="2026-08-04"
    )
    validated = campaign_plan_v4.validate_campaign_query_plan_v4(
        enriched, query=QUERY, require_semantic=True
    )
    normalized_join = validated["audience_requirement"]["expression"]["relation"]

    assert normalized_join["left"] == {"type": "source", "name": SOURCE}
    assert normalized_join["on"]["left"]["name"] == MEMBER_FIELD
    assert normalized_join["on"]["right"]["name"] == MEMBER_FIELD


# `test_member_percent_ranking_reaches_sql_through_semantic_plan` 은 2026-08-05 삭제됐다 —
# ranked_set 노드를 플랜에 직접 심어 상위 N% SQL 까지 가는지를 고정하던 테스트다. 그 노드
# 타입은 비테스트 생산자가 처음부터 0 이었고, 노드를 실행 플랜에 싣던 경로도 폐기됐다.
# canonical Event IR 로 들어온 같은 요청의 퍼센트 한정 계약은 이 파일의 나머지가 고정한다.
