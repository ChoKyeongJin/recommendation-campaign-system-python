"""출고 전 커버리지 게이트: **증명할 수 있는 누락만** 막는다.

두 방향을 함께 잰다. 누락을 놓치면 부분 SQL 이 성공으로 나가고(#19 계열), 증명 없이 막으면
정상 SQL 이 가짜 회귀가 된다(코퍼스 #78 의 required_clauses 가 그렇게 틀렸다).
"""

from __future__ import annotations

from datetime import date

import coverage_gate
import event_ir
import result_shape
import semantic_ledger

SCALAR_SHAPE = result_shape.ResultShape(
    kind="scalar", metric="count", entity="member", distinct=True
)

MEMBER_LIST_SQL = "\n".join(
    [
        "SELECT DISTINCT B.MEMBER_NO AS CUST_ID",
        "FROM CRM_MB_BASEINFO B",
        "WHERE B.MEMBER_STATE_CD = 'NORMAL'",
        "  AND EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O"
        " WHERE O.MEMBER_NO = B.MEMBER_NO"
        " AND O.ORDER_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112))",
    ]
)

SCALAR_SQL = MEMBER_LIST_SQL.replace(
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID",
    "SELECT COUNT(DISTINCT B.MEMBER_NO) AS MEMBER_COUNT",
)


def _purchase_expression(days: int = 30) -> event_ir.Condition:
    return event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="purchase"),
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="purchase.order_date"),
                window=event_ir.RollingWindow(value=days, unit="day"),
            ),
        ),
        evidence=event_ir.Evidence(text="최근 30일 구매", start=0, end=9),
    )


def test_member_list_does_not_satisfy_a_scalar_request() -> None:
    """'회원 수' 요청에 회원 목록을 내면 게이트가 막는다 — 이것이 #71 계열의 안전망이다."""

    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원 수를 알려줘",
        sql=MEMBER_LIST_SQL,
        expression=_purchase_expression(),
        shape=SCALAR_SHAPE,
        literal_bindings=[],
    )
    assert not gate.verdict.is_shippable
    assert gate.verdict.reason_code == semantic_ledger.REASON_OPEN_REQUIREMENTS
    assert coverage_gate.KIND_RESULT_SHAPE in gate.verdict.missing_canonical_kinds


def test_scalar_projection_satisfies_the_shape_requirement() -> None:
    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원 수를 알려줘",
        sql=SCALAR_SQL,
        expression=_purchase_expression(),
        shape=SCALAR_SHAPE,
        literal_bindings=[],
    )
    assert gate.verdict.is_shippable, gate.verdict.to_dict()


def test_a_dropped_window_blocks_the_sql() -> None:
    """원문이 말한 기간이 IR 의 어떤 창으로도 남지 않으면 부분 SQL 이다."""

    bindings = [
        {
            "kind": "duration",
            "text": "30일",
            "start": 3,
            "end": 6,
            "normalized": {
                "value": 30,
                "semantic_unit": "days",
                "temporal_kind": "rolling_duration",
            },
        }
    ]
    windowless = event_ir.Exists(
        relation=event_ir.Source(name="purchase"),
        evidence=event_ir.Evidence(text="구매", start=7, end=9),
    )
    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원",
        sql=MEMBER_LIST_SQL,
        expression=windowless,
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=bindings,
    )
    assert not gate.verdict.is_shippable
    assert coverage_gate.KIND_TEMPORAL_WINDOW in gate.verdict.missing_canonical_kinds
    assert coverage_gate.clarification_questions(gate)


def test_a_preserved_window_ships_even_with_a_different_representation() -> None:
    """물리 표현(rolling/absolute)이 달라도 **길이**가 같으면 같은 뜻이다(§5)."""

    bindings = [
        {
            "kind": "duration",
            "text": "30일",
            "start": 3,
            "end": 6,
            "normalized": {
                "value": 30,
                "semantic_unit": "days",
                "temporal_kind": "rolling_duration",
            },
        }
    ]
    absolute = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="purchase"),
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="purchase.order_date"),
                window=event_ir.AbsoluteInterval(
                    start=date(2026, 7, 8), end_exclusive=date(2026, 8, 7)
                ),
            ),
        ),
        evidence=event_ir.Evidence(text="최근 30일 구매", start=0, end=9),
    )
    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원",
        sql=MEMBER_LIST_SQL,
        expression=absolute,
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=bindings,
    )
    assert gate.verdict.is_shippable, gate.verdict.to_dict()


def test_entity_list_request_needs_no_projection_artifact() -> None:
    """회원 목록 요청까지 투영 artifact 를 요구하면 정상 SQL 이 전부 막힌다."""

    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원",
        sql=MEMBER_LIST_SQL,
        expression=_purchase_expression(),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=[],
    )
    assert gate.verdict.is_shippable


def test_accounted_requirements_keep_their_own_disposition() -> None:
    """기존 회계의 귀결을 게이트가 다시 판정하지 않는다 — 한 사실에 답이 둘이면 안 된다."""

    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원",
        sql=MEMBER_LIST_SQL,
        expression=_purchase_expression(),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=[],
        accounted_requirements=[
            {
                "status": "unsupported",
                "path": "target_user.brand",
                "source_span": {"start": 0, "end": 3},
                "source_text": "브랜드",
            }
        ],
    )
    counts = gate.ledger.counts()
    assert counts[semantic_ledger.DISPOSITION_UNSUPPORTED] == 1
    assert counts["open"] == 0


def test_provenance_links_ir_nodes_to_sql_artifacts() -> None:
    gate = coverage_gate.evaluate(
        query="최근 30일 구매한 회원",
        sql=MEMBER_LIST_SQL,
        expression=_purchase_expression(),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=[],
    )
    graph = gate.graph
    assert graph.ir_nodes, "IR 노드가 없으면 provenance 는 공허하다"
    assert graph.artifacts, "SQL artifact 가 없으면 provenance 는 공허하다"
    assert graph.receipts, "노드와 artifact 가 있는데 영수증이 없으면 연결이 끊긴 것이다"
    assert graph.extraction_error is None
