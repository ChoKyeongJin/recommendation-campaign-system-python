"""결과 형태가 **canonical 계약**이라는 것, 그리고 투영이 실제로 만들어진다는 것.

``어제 하루 동안 구매한 회원 수를 알려줘`` 는 오디언스 필터를 정확히 컴파일하고도
``intent_sql_contract_failed`` 로 닫혔다(2026-08-06 실측). 검증기는 형태 불일치를 잡았지만
형태를 **만드는** 단계가 없었다. 여기서 그 단계를 고정한다.
"""

from __future__ import annotations

import pytest

import result_shape

AUDIENCE_SQL = "\n".join(
    [
        "SELECT DISTINCT B.MEMBER_NO",
        "FROM CRM_MB_BASEINFO B",
        "WHERE B.MEMBER_STAT_CD = 1",
    ]
)


def test_scalar_shape_requires_a_metric() -> None:
    """지표 없는 스칼라는 '무엇을' 세는지 모르는 형태다 — 선언 자체를 막는다."""

    with pytest.raises(result_shape.ResultShapeError):
        result_shape.ResultShape(kind=result_shape.KIND_SCALAR)


def test_analytical_intent_drives_the_shape() -> None:
    plan = {
        "analytical_intent": {
            "result_shape": "scalar",
            "aggregate_function": "COUNT",
            "target_entity": "member",
        }
    }
    shape = result_shape.resolve_result_shape(plan)
    assert shape.kind == result_shape.KIND_SCALAR
    assert shape.metric == "count"
    assert shape.distinct is True
    assert shape.is_scalar_entity_count


def test_output_contract_alone_can_declare_a_scalar_count() -> None:
    """지표 레지스트리를 타지 않는 경로(조건 판정 IR)도 형태를 선언할 수 있어야 한다."""

    plan = {
        "output_contract": {
            "expected_grain": "analytical",
            "requires_member_id": False,
            "source": "condition_evaluations",
        }
    }
    shape = result_shape.resolve_result_shape(plan)
    assert shape.is_scalar_entity_count


def test_default_is_an_entity_list() -> None:
    assert result_shape.resolve_result_shape({}).kind == result_shape.KIND_ENTITY_LIST


def test_scalar_projection_keeps_the_physical_member_column() -> None:
    """집계는 **물리 컬럼**을 세야 한다.

    파생 테이블로 감싸 ``COUNT(DISTINCT audience.MEMBER_NO)`` 를 만들면 집계 검증기가
    그 컬럼을 어느 테이블 것인지 잃고 정상 SQL 을 MISSING_DISTINCT 로 떨어뜨린다(실측).
    """

    shape = result_shape.ResultShape(
        kind="scalar", metric="count", entity="member", distinct=True
    )
    plan = result_shape.plan_projection(shape, entity_expression="B.MEMBER_NO")
    assert plan.applied
    assert plan.select_columns == ("COUNT(DISTINCT B.MEMBER_NO) AS MEMBER_COUNT",)
    assert plan.artifacts and plan.artifacts[0]["kind"] == "aggregation"


def test_projection_refuses_a_non_identifier_entity_expression() -> None:
    """임의 문자열을 SELECT 에 넣는 자리를 만들지 않는다."""

    shape = result_shape.ResultShape(
        kind="scalar", metric="count", entity="member", distinct=True
    )
    plan = result_shape.plan_projection(
        shape, entity_expression="B.MEMBER_NO) FROM X--"
    )
    assert not plan.applied
    assert plan.reason.startswith("entity_expression_not_an_identifier")


def test_entity_list_needs_no_projection() -> None:
    plan = result_shape.plan_projection(
        result_shape.ENTITY_LIST_DEFAULT, entity_expression="B.MEMBER_NO"
    )
    assert not plan.applied
    assert plan.reason == "entity_list_needs_no_projection"
    assert plan.select_columns == ()


def test_round_trip_through_the_plan_preserves_the_shape() -> None:
    plan: dict[str, object] = {}
    shape = result_shape.ResultShape(
        kind="scalar", metric="count", entity="member", distinct=True, source="test"
    )
    result_shape.write_result_shape(plan, shape)
    assert result_shape.read_result_shape(plan) == shape
