"""메타모픽 계약 — 원문을 한 조각 바꾸면 **무엇이 달라져야 하는가**.

절대적 정답(이 문장은 이 SQL 이어야 한다)을 적는 대신 관계를 적는다. 관계는 LLM 방출 편차에
휘둘리지 않고, 새 문장이 늘어도 규칙이 늘지 않는다. 여기서 재는 계층은 전부 결정론이다
(구조화기를 부르지 않는다).

    조건 추가 → 요구가 하나 늘거나 typed clause 가 명시적으로 바뀐다
    조건 삭제 → 그 요구와 이어진 artifact 가 사라진다
    COUNT 추가 → 필터는 유지되고 result shape 만 스칼라가 된다
    기간 추가 → temporal requirement 와 SQL 기간 술어가 함께 는다
"""

from __future__ import annotations

from datetime import date

import coverage_gate
import event_ir
import provenance
import result_shape
import temporal_clause
from query_structurer.semantic_ir import extract_literal_bindings

REFERENCE_DATE = "2026-08-06"

BASE_SQL = "\n".join(
    [
        "SELECT DISTINCT B.MEMBER_NO AS CUST_ID",
        "FROM CRM_MB_BASEINFO B",
        "WHERE B.MEMBER_STATE_CD = 'NORMAL'",
        "  AND EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)",
    ]
)
WINDOWED_SQL = BASE_SQL.replace(
    "WHERE O.MEMBER_NO = B.MEMBER_NO",
    "WHERE O.MEMBER_NO = B.MEMBER_NO"
    " AND O.ORDER_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112)",
)
COUNTED_SQL = WINDOWED_SQL.replace(
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID",
    "SELECT COUNT(DISTINCT B.MEMBER_NO) AS MEMBER_COUNT",
)


def _purchase(window: event_ir.TimeWindow | None = None) -> event_ir.Condition:
    relation: event_ir.Relation = event_ir.Source(name="purchase")
    if window is not None:
        relation = event_ir.Filter(
            relation=relation,
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="purchase.order_date"), window=window
            ),
        )
    return event_ir.Exists(
        relation=relation, evidence=event_ir.Evidence(text="구매", start=7, end=9)
    )


def _bindings(query: str) -> list[dict]:
    return extract_literal_bindings(query, current_date=REFERENCE_DATE)


def test_adding_a_period_adds_a_temporal_requirement() -> None:
    without = temporal_clause.combine_temporal_clauses("구매한 회원", _bindings("구매한 회원"))
    with_period = temporal_clause.combine_temporal_clauses(
        "최근 30일 구매한 회원", _bindings("최근 30일 구매한 회원")
    )
    quantified_before = [clause for clause in without if clause.is_quantified]
    quantified_after = [clause for clause in with_period if clause.is_quantified]
    assert len(quantified_after) == len(quantified_before) + 1


def test_adding_a_period_requires_a_period_predicate_in_the_sql() -> None:
    """기간 요구가 늘었는데 SQL 이 그대로면 그 SQL 은 부분 SQL 이다."""

    query = "최근 30일 구매한 회원"
    dropped = coverage_gate.evaluate(
        query=query,
        sql=BASE_SQL,
        expression=_purchase(),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=_bindings(query),
    )
    kept = coverage_gate.evaluate(
        query=query,
        sql=WINDOWED_SQL,
        expression=_purchase(event_ir.RollingWindow(value=30, unit="day")),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=_bindings(query),
    )
    assert not dropped.verdict.is_shippable
    assert kept.verdict.is_shippable


def test_removing_a_condition_removes_its_sql_artifact() -> None:
    """조건을 빼면 그 조건과 이어진 artifact 도 사라져야 한다."""

    with_window = provenance.extract_sql_artifacts(WINDOWED_SQL)
    without_window = provenance.extract_sql_artifacts(BASE_SQL)
    assert with_window.parsed and without_window.parsed
    date_artifacts = [
        artifact
        for artifact in with_window.artifacts
        if "ORDER_DATE" in artifact.symbols
    ]
    assert date_artifacts, "기간 술어가 artifact 로 잡혀야 한다"
    assert not [
        artifact for artifact in without_window.artifacts if "ORDER_DATE" in artifact.symbols
    ]


def test_adding_a_count_keeps_the_filters_and_changes_only_the_shape() -> None:
    listed = provenance.extract_sql_artifacts(WINDOWED_SQL)
    counted = provenance.extract_sql_artifacts(COUNTED_SQL)
    predicates = lambda extraction: sorted(  # noqa: E731 — 두 줄짜리 지역 헬퍼
        artifact.sql_fragment
        for artifact in extraction.artifacts
        if artifact.kind == provenance.ARTIFACT_PREDICATE
    )
    assert predicates(listed) == predicates(counted), "필터는 그대로여야 한다"
    assert not listed.by_kind(provenance.ARTIFACT_AGGREGATION)
    assert counted.by_kind(provenance.ARTIFACT_AGGREGATION), "형태만 집계로 바뀐다"


def test_same_meaning_different_window_representation_is_one_meaning() -> None:
    """롤링 30일과 그에 해당하는 절대 구간은 같은 요구를 만족한다(§5 물리 독립)."""

    query = "최근 30일 구매한 회원"
    rolling = coverage_gate.evaluate(
        query=query,
        sql=WINDOWED_SQL,
        expression=_purchase(event_ir.RollingWindow(value=30, unit="day")),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=_bindings(query),
    )
    absolute = coverage_gate.evaluate(
        query=query,
        sql=WINDOWED_SQL,
        expression=_purchase(
            event_ir.AbsoluteInterval(start=date(2026, 7, 8), end_exclusive=date(2026, 8, 7))
        ),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=_bindings(query),
    )
    assert rolling.verdict.is_shippable and absolute.verdict.is_shippable
    assert rolling.ledger.counts() == absolute.ledger.counts()


def test_a_wider_window_is_still_one_requirement() -> None:
    """기간 값만 바뀌면 요구 **개수**는 그대로이고 typed value 만 달라진다."""

    narrow = coverage_gate.evaluate(
        query="최근 7일 구매한 회원",
        sql=WINDOWED_SQL,
        expression=_purchase(event_ir.RollingWindow(value=7, unit="day")),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=_bindings("최근 7일 구매한 회원"),
    )
    wide = coverage_gate.evaluate(
        query="최근 30일 구매한 회원",
        sql=WINDOWED_SQL,
        expression=_purchase(event_ir.RollingWindow(value=30, unit="day")),
        shape=result_shape.ENTITY_LIST_DEFAULT,
        literal_bindings=_bindings("최근 30일 구매한 회원"),
    )
    assert len(narrow.ledger) == len(wide.ledger)
    narrow_values = [
        item.typed_value
        for item in narrow.ledger
        if item.canonical_kind == coverage_gate.KIND_TEMPORAL_WINDOW
    ]
    wide_values = [
        item.typed_value
        for item in wide.ledger
        if item.canonical_kind == coverage_gate.KIND_TEMPORAL_WINDOW
    ]
    assert narrow_values != wide_values
