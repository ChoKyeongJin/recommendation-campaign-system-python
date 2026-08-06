"""떨어진 구간 여러 개가 시간 절 **하나**를 이룬다는 계약.

이 파일이 지키는 것은 하나다: ``최근 30일`` 에서 ``최근`` 만 보고 "기간이 없다"고 말할 수 없다.
그 판정이 없던 동안 ``최근 30일 구매한 회원 수를 알려줘`` 가 되묻기로 닫혔다(2026-08-06 실측).
"""

from __future__ import annotations

import pytest

import temporal_clause
from query_structurer.semantic_ir import extract_literal_bindings

REFERENCE_DATE = "2026-08-06"


def _clauses(query: str) -> tuple[temporal_clause.TemporalClause, ...]:
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    return temporal_clause.combine_temporal_clauses(query, bindings)


def _period_issue(query: str, marker: str) -> dict[str, object]:
    start = query.find(marker)
    assert start >= 0, f"표지 '{marker}' 가 원문에 없다"
    return {
        "code": "missing_argument",
        "argument": "period",
        "evidence": {"text": marker, "start": start, "end": start + len(marker)},
    }


@pytest.mark.parametrize(
    ("query", "amount", "unit"),
    [
        ("최근 30일 구매한 회원 수를 알려줘", 30, "days"),
        ("최근 5일 동안 캠페인 발송 성공 횟수가 3회 이상인 회원", 5, "days"),
        ("지난 3개월 동안 구매한 회원", 3, "months"),
    ],
)
def test_recency_marker_and_duration_form_one_clause(
    query: str, amount: int, unit: str
) -> None:
    clauses = [clause for clause in _clauses(query) if clause.is_quantified]
    assert len(clauses) == 1, [clause.to_dict() for clause in _clauses(query)]
    clause = clauses[0]
    assert (clause.amount, clause.unit) == (amount, unit)
    # 구간은 둘이다 — 표지와 수량. 하나로 접으면 이 결합의 근거가 사라진다.
    assert len(clause.source_spans) == 2
    assert {span.role for span in clause.source_spans} == {"marker", "amount"}


def test_two_markers_each_keep_their_own_duration() -> None:
    """'최근 3개월 … 최근 30일' — 리터럴이 가장 가까운 표지에 붙는다(공유되지 않는다)."""

    query = "최근 3개월 내 주문한 적은 있지만 최근 30일간 구매가 없는 회원"
    quantified = [clause for clause in _clauses(query) if clause.is_quantified]
    assert sorted((clause.amount, clause.unit) for clause in quantified) == [
        (3, "months"),
        (30, "days"),
    ]


def test_bare_recency_marker_stays_unquantified() -> None:
    """숫자가 없으면 기본값을 **지어내지 않는다**. 정책이 정할 자리를 남긴다."""

    clauses = _clauses("최근 캠페인 발송 성공 횟수가 3회 이상인 회원")
    assert clauses, "표지 자체는 절로 남아야 한다"
    assert not any(clause.is_quantified for clause in clauses)


def test_stated_period_refutes_a_missing_period_claim() -> None:
    query = "최근 30일 구매한 회원 수를 알려줘"
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    clause = temporal_clause.stated_period_for_issue(
        query, _period_issue(query, "최근"), bindings
    )
    assert clause is not None
    assert (clause.amount, clause.unit) == (30, "days")


def test_bare_recency_claim_is_not_refuted() -> None:
    """원문이 정말 기간을 말하지 않았으면 반박하지 않는다 — 그 신고는 유효하다."""

    query = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    assert (
        temporal_clause.stated_period_for_issue(query, _period_issue(query, "최근"), bindings)
        is None
    )


def test_count_literal_is_not_mistaken_for_a_period() -> None:
    """'3회' 는 기간이 아니다. 수량 리터럴을 창으로 읽으면 조용히 다른 모수가 된다."""

    query = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"
    assert not any(clause.is_quantified for clause in _clauses(query))


def test_clause_boundary_blocks_combination() -> None:
    """절 경계를 넘어선 숫자는 그 표지의 기간이 아니다."""

    query = "최근, 30일 무이자 행사에 반응한 회원"
    clauses = _clauses(query)
    # 30일은 표지에 묶이지 않은 채 남고, '최근' 은 수량을 얻지 못한다.
    assert any(clause.is_quantified and not clause.marker_bound for clause in clauses)
    assert any(clause.marker_bound and not clause.is_quantified for clause in clauses)


def test_an_unmarked_duration_is_not_a_window() -> None:
    """'구매주기가 30일 이하' 의 30일은 임계값이지 기간이 아니다.

    표지 없이 창으로 단정하면 그 값이 '사라진 기간'으로 오탐돼 정상 SQL 이 부분 SQL 로
    강등된다(실측: 프로필 스칼라 지표 두 축이 그렇게 막혔다).
    """

    for query in ("구매주기가 30일 이하인 회원", "활동 개월 수가 6개월 이상인 회원"):
        clauses = _clauses(query)
        assert not any(clause.marker_bound for clause in clauses), query


def test_span_coverage_is_by_overlap_not_identity() -> None:
    """근거 구간은 표지 **또는** 수량 어느 쪽으로 지목돼도 같은 절을 가리켜야 한다."""

    query = "최근 30일 구매한 회원 수를 알려줘"
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    by_marker = temporal_clause.quantified_clause_for_span(query, (0, 2), bindings)
    by_amount = temporal_clause.quantified_clause_for_span(query, (3, 6), bindings)
    assert by_marker is not None and by_amount is not None
    assert by_marker == by_amount
