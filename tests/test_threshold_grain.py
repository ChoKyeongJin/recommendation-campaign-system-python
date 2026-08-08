"""임계값의 **적용 grain** 계약 — 뜻이 다른 두 문장은 같은 SQL 로 접히지 않는다.

실측(2026-08-08, 라이브 id 42/43)::

    최근 90일 동안 총 구매금액이 30만원 이상인 회원          → SUM(PAYMENT_AMT) >= 300000
    최근 90일 동안 구매금액이 30만원 이상인 주문을 한 회원    → SUM(PAYMENT_AMT) >= 300000   ← 같다

두 SQL 은 **바이트 동일**했고 segment_label 도 같았다. 실DB(CRMDW)에서 두 해석의 크기는
9,585명 대 688명 — 14배다.

이 파일은 개별 문장의 정답 SQL 을 적지 않는다(그건 LLM 방출에 묶인다). 적는 것은 관계다:
**원문이 grain 을 말했으면 트리가 그 grain 을 실현해야 한다.**
"""

from __future__ import annotations

from datetime import date

import event_compiler
import event_ir
import grain_claims
from sql_dialect import get_dialect

_SUBJECT_QUERY = "최근 90일 동안 총 구매금액이 30만원 이상인 회원을 추출해줘."
_ROW_QUERY = "최근 90일 동안 구매금액이 30만원 이상인 주문을 한 회원을 추출해줘."
# 원문이 실제로 모호한 자리 — 라이브 id 40. 여기서 기본값을 고르면 그것이 추측이다.
_AMBIGUOUS_QUERY = "2019년에 이십만원 이상을 구매한 고객"


def _registry() -> dict[str, event_compiler.EventSpec]:
    return event_compiler.resolve_registry({
        "purchase": event_compiler.EventSpec(
            table="orders", alias="EO", subject_key="MEMBER_NO",
            event_subject_key="MEMBER_NO", time_column="ORDER_DATE", time_format="date",
        ),
    })


def _context() -> event_compiler.CompileContext:
    registry = _registry()
    return event_compiler.CompileContext(
        subject=event_compiler.SubjectSpec(),
        registry=registry,
        fields=event_compiler.resolve_fields(registry, {
            "purchase.amount": event_compiler.FieldSpec(
                source="purchase", column="PAYMENT_AMT", data_type="number"
            ),
        }),
        dialect=get_dialect("ansi"), literals=True, today=date(2026, 8, 8),
    )


def _subject_tree() -> event_ir.Condition:
    """모델이 두 문장 모두에 대해 실제로 내던 트리(회원별 합계)."""
    return event_ir.Comparison(
        left=event_ir.Aggregate(
            function="sum",
            expression=event_ir.FieldRef(name="purchase.amount"),
            relation=event_ir.Filter(
                relation=event_ir.Source(name="purchase", correlation="subject"),
                where=event_ir.TimeFilter(
                    field=event_ir.FieldRef(name="purchase.occurred_at"),
                    window=event_ir.RollingWindow(value=90, unit="day"),
                ),
            ),
        ),
        operator=">=",
        right=event_ir.Literal(value=300000),
    )


# ── 표면 주장 ────────────────────────────────────────────────────────────────────


def test_total_marker_claims_subject_grain() -> None:
    claims = grain_claims.detect_grain_claims(_SUBJECT_QUERY)

    assert [claim.grain for claim in claims] == [grain_claims.SUBJECT]


def test_event_noun_head_claims_row_grain() -> None:
    """임계의 머리가 사건 명사면 그 임계는 사건 하나를 서술한다."""
    claims = grain_claims.detect_grain_claims(_ROW_QUERY)

    assert [claim.grain for claim in claims] == [grain_claims.ROW]
    assert claims[0].head == "주문"


def test_a_longer_word_starting_with_a_count_unit_is_not_a_head() -> None:
    """'이상인 **회**원'의 ``회`` 는 계수 단위가 아니다 — 한글에는 낱말 경계가 없다.

    이 경계가 없던 동안 ``총 …이상인 회원``(주체)이 row 주장을 함께 만들어 두 주장이
    충돌했다. 조용한 오답을 막으려던 장치가 스스로 오답을 만드는 자리다.
    """
    claims = grain_claims.detect_grain_claims(_SUBJECT_QUERY)

    assert all(claim.grain != grain_claims.ROW for claim in claims)


def test_an_ambiguous_sentence_claims_nothing() -> None:
    """말하지 않은 것을 지어내지 않는다. 빈 주장은 '주체 집계'라는 뜻이 아니다."""
    assert grain_claims.detect_grain_claims(_AMBIGUOUS_QUERY) == ()


# ── IR 이 실현한 grain ───────────────────────────────────────────────────────────


def test_realized_grain_is_read_from_the_tree_not_the_text() -> None:
    assert grain_claims.expression_grains(_subject_tree()) == frozenset(
        {grain_claims.SUBJECT}
    )


def test_a_population_filter_inside_an_aggregate_is_not_a_row_threshold() -> None:
    """집계 **안쪽** 비교는 모집단을 좁히는 조건이지 행 임계가 아니다."""
    tree = event_ir.Comparison(
        left=event_ir.Aggregate(
            function="count",
            expression=None,
            relation=event_ir.Filter(
                relation=event_ir.Source(name="purchase", correlation="subject"),
                where=event_ir.Comparison(
                    left=event_ir.FieldRef(name="purchase.amount"),
                    operator=">=",
                    right=event_ir.Literal(value=1000),
                ),
            ),
        ),
        operator=">=",
        right=event_ir.Literal(value=3),
    )

    assert grain_claims.expression_grains(tree) == frozenset({grain_claims.SUBJECT})


# ── 낮추기와 대비 ────────────────────────────────────────────────────────────────


def test_row_and_subject_grain_do_not_compile_to_the_same_sql() -> None:
    """#42 와 #43 의 회귀 — 지금은 바이트 동일이므로 이 검사가 그 상태를 실패로 만든다."""
    context = _context()
    subject_tree = _subject_tree()
    row_tree = grain_claims.regrain_to_row(subject_tree)
    assert row_tree is not None, "합계 임계를 행 임계로 낮추지 못했다"

    subject_sql = event_compiler.compile_condition(subject_tree, context).sql
    row_sql = event_compiler.compile_condition(row_tree, context).sql

    assert subject_sql != row_sql
    assert "SUM(" in subject_sql and "SUM(" not in row_sql
    assert row_sql.startswith("EXISTS (")
    # 모집단 필터(기간 창)는 낮춘 뒤에도 남아 있어야 한다 — grain 과 무관한 조건이다.
    assert "ORDER_DATE" in row_sql


def test_regrain_keeps_the_window_and_the_threshold() -> None:
    row_tree = grain_claims.regrain_to_row(_subject_tree())

    assert isinstance(row_tree, event_ir.Exists)
    assert grain_claims.expression_grains(row_tree) == frozenset({grain_claims.ROW})


def test_regrain_refuses_functions_whose_row_meaning_differs() -> None:
    """``COUNT`` 의 '3건 이상'은 본래 주체 단위 세기다 — 행 대응이 없으면 낮추지 않는다."""
    counted = event_ir.Comparison(
        left=event_ir.Aggregate(
            function="count",
            expression=None,
            relation=event_ir.Source(name="purchase", correlation="subject"),
        ),
        operator=">=",
        right=event_ir.Literal(value=3),
    )

    assert grain_claims.regrain_to_row(counted) is None


def test_conflict_is_reported_only_when_the_source_text_declared_a_grain() -> None:
    tree = _subject_tree()

    assert grain_claims.conflicting_grain(_ROW_QUERY, tree) is not None
    assert grain_claims.conflicting_grain(_SUBJECT_QUERY, tree) is None
    assert grain_claims.conflicting_grain(_AMBIGUOUS_QUERY, tree) is None


def test_regrain_rewrites_only_the_conflicting_clause() -> None:
    """절 소유권 — 같은 트리의 다른 절은 그대로 둔다."""
    other = event_ir.Exists(
        relation=event_ir.Source(name="purchase", correlation="subject")
    )
    combined = event_ir.And(operands=(other, _subject_tree()))

    rewritten = grain_claims.regrain_to_row(combined)

    assert isinstance(rewritten, event_ir.And)
    assert rewritten.operands[0] == other
    assert grain_claims.expression_grains(rewritten.operands[1]) == frozenset(
        {grain_claims.ROW}
    )
