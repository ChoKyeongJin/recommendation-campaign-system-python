"""Focused contracts for the catalog-backed canonical audience execution path."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    AUDIENCE_REQUIREMENT_KEY,
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
    EVENT_EXPRESSION_KEY,
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.prompt import build_campaign_query_plan_v4_user_prompt  # noqa: E402
from query_structurer.types import QueryStructuringInput, StructuringContext  # noqa: E402


CURRENT_DATE = "2026-08-02"
BARE_RECENT_QUERY = (
    "최근 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 "
    "재반응 유도 캠페인을 만들어줘."
)
SPECIFIED_QUERY = (
    "최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 "
    "재반응 유도 캠페인을 만들어줘."
)


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _campaign_audience_expression(
    query: str,
    *,
    contact_evidence: str,
    window: event_ir.TimeWindow | None,
) -> event_ir.Condition:
    contact_relation: event_ir.Relation = event_ir.Source(
        name="campaign_contact_success"
    )
    if window is not None:
        contact_relation = event_ir.Filter(
            relation=contact_relation,
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(
                    name="campaign_contact_success.occurred_at"
                ),
                window=window,
            ),
        )
    return event_ir.And(
        operands=(
            event_ir.Comparison(
                operator=">=",
                left=event_ir.Aggregate(
                    function="count",
                    relation=contact_relation,
                    expression=event_ir.FieldRef(
                        name="campaign_contact_success.execution_id"
                    ),
                    distinct=True,
                ),
                right=event_ir.Literal(value=3),
                evidence=_evidence(query, contact_evidence),
            ),
            event_ir.Not(
                operand=event_ir.Exists(
                    relation=event_ir.Source(
                        name="campaign_purchase_response"
                    ),
                    evidence=_evidence(query, "구매반응이 없는"),
                )
            ),
        )
    )


def _canonical_payload(query: str, expression: event_ir.Condition) -> dict[str, Any]:
    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "recommend_campaign",
            "campaign_constraints": {
                "objective": "reactivation",
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "result_limit": 100,
            AUDIENCE_REQUIREMENT_KEY: {
                "expression": expression.to_dict(),
                "issues": [],
            },
        },
        query,
        current_date=CURRENT_DATE,
    )


def _contains_meaning(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_meaning(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_meaning(item) for item in value)
    return value not in (None, "", False)


def test_llm_contract_exposes_only_one_audience_requirement() -> None:
    properties = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]

    assert set(properties) == {
        "intent",
        "campaign_constraints",
        "result_limit",
        AUDIENCE_REQUIREMENT_KEY,
    }
    assert set(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["required"]) == set(
        properties
    )
    # 오디언스 술어의 소유자는 audience_requirement 하나다. 두 번째 의미 표면
    # (semantic_plan)은 2026-08-05 폐기됐고, 다시 열리면 같은 문장을 두 계약이 해석한다.
    assert {
        "target_user",
        "exclude",
        "semantic_plan",
        EVENT_EXPRESSION_KEY,
    }.isdisjoint(properties)


def test_the_structuring_prompt_agrees_with_the_fail_close_policy() -> None:
    """한 프롬프트 안의 세 지시가 같은 말을 한다(예전에는 갈라져 있었다).

    시스템 메시지(:mod:`query_structurer.structurer`)와 카탈로그 안내
    (:func:`audience_runtime.audience_catalog_guidance`)는 기간 없는 '최근'을 되묻으라고
    했는데, 사용자 프롬프트만 '최근 5일로 채우라'고 말하고 있었다. 기본 기간의 소유자는
    호출 계층(:mod:`default_period_policy`)이고, 계층별 계약은
    ``tests/test_default_period_policy.py`` 가 고정한다.
    """

    prompt = build_campaign_query_plan_v4_user_prompt(
        QueryStructuringInput(
            query=BARE_RECENT_QUERY,
            context=StructuringContext(current_date=CURRENT_DATE),
        )
    )

    assert "do not substitute a default window" in prompt
    assert "missing_argument with argument='period'" in prompt
    assert '"type": "rolling", "value": 5, "unit": "day"' not in prompt


def test_runtime_catalog_extension_uses_the_same_ir_and_compiler(tmp_path: Path) -> None:
    config = audience_runtime.catalog_snapshot()
    config["sources"]["synthetic_signal"] = {
        "table": "FACT_SYNTHETIC_SIGNAL",
        "alias": "SS",
        "subject_key": "MEMBER_NO",
        "event_subject_key": "MEMBER_NO",
        "time_column": "OCCURRED_ON",
        "time_format": "date",
        "binding": "fact_table",
        "grain": "event",
        "label": "synthetic signal",
    }
    config["fields"]["synthetic_signal.score"] = {
        "source": "synthetic_signal",
        "column": "SCORE",
        "data_type": "number",
        "label": "synthetic score",
    }
    catalog_path = tmp_path / "audience_catalog.json"
    catalog_path.write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )

    catalog = audience_runtime.resolve_audience_catalog(catalog_path)
    expression = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="synthetic_signal"),
            where=event_ir.Comparison(
                operator=">=",
                left=event_ir.FieldRef(name="synthetic_signal.score"),
                right=event_ir.Literal(value=10),
            ),
        )
    )
    compiled = event_compiler.compile_expression(
        expression, context=catalog.compile_context(literals=True)
    )

    assert catalog.source("synthetic_signal").event.table == "FACT_SYNTHETIC_SIGNAL"
    assert catalog.field("synthetic_signal.score").compiler_field.column == "SCORE"
    assert event_ir.node_type_names(expression) == {
        "exists",
        "filter",
        "source",
        "comparison",
        "field",
        "literal",
    }
    assert "FROM FACT_SYNTHETIC_SIGNAL SS" in compiled.sql
    assert "SS.MEMBER_NO = B.MEMBER_NO" in compiled.sql
    assert "SS.SCORE >= 10" in compiled.sql


def test_exists_inherits_exact_provenance_from_its_relation_predicate() -> None:
    query = "구매 금액 10 이상"
    expression = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("purchase"),
            event_ir.Comparison(
                ">=",
                event_ir.FieldRef("purchase.amount"),
                event_ir.Literal(10),
                evidence=_evidence(query, query),
            ),
        )
    )

    structured = _canonical_payload(query, expression)

    projected = structured[EVENT_EXPRESSION_KEY]["expression"]
    assert projected["evidence"] == {
        "text": query,
        "start": 0,
        "end": len(query),
    }


# 기간이 채워진 맨 '최근'이 SQL 까지 가는 계약은 계층이 갈렸다 — 그 창을 **누가 골랐는가**를
# 함께 고정해야 의미가 있기 때문이다. 통합 판은
# ``tests/test_default_period_policy.py::test_configured_default_period_fills_the_window_and_compiles``
# 이 소유한다(기본 기간 정책을 통과시키고 source=default_policy 영수증까지 확인한다).


def test_canonical_campaign_expression_is_the_only_sql_authority() -> None:
    expression = _campaign_audience_expression(
        SPECIFIED_QUERY,
        contact_evidence="최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상",
        window=event_ir.RollingWindow(value=90, unit="day"),
    )
    structured = _canonical_payload(SPECIFIED_QUERY, expression)
    plan = graph_rag.build_query_plan(
        SPECIFIED_QUERY,
        parser="llm",
        query_plan_v4=structured,
    )

    assert plan[EVENT_EXPRESSION_KEY]["expression"] == expression.to_dict()
    assert not _contains_meaning(plan["target_user"])
    assert plan["campaign_constraints"]["objective"] == "reactivation"

    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        SPECIFIED_QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=SPECIFIED_QUERY,
    )

    assert result["sql"] is not None
    assert result["selected"]["id"] == "sql_template:event_expression"
    sql = result["sql"]
    assert "COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO))" in sql
    assert "M.CELL_TYPE_CD = 'T'" in sql
    assert "M.CONTAC_SUCC_YN = 'Y'" in sql
    assert "NOT EXISTS" in sql
    assert "MCS_CAMP_MBR_RSPN_FT" in sql
    assert "R.BUY_RSPN_YN = 'Y'" in sql
    assert "'reactivation' AS objective" in sql
    assert "c.objective = 'reactivation'" not in sql
    assert {
        token["path"] for token in result["condition_tokens"]
    } == {"plan.event_expression[0]", "plan.event_expression[1]"}
