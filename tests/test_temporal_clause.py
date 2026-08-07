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


@pytest.mark.parametrize(
    "query",
    [
        "구매주기가 30일 이하이고 최근 구매가 없는 회원",
        "유효기간 30일 쿠폰을 최근 받은 회원",
        "환불까지 7일 걸린 최근 주문 회원",
    ],
)
def test_a_duration_before_the_marker_is_not_that_markers_window(query: str) -> None:
    """한국어 어순: 수량은 자기 표지 **뒤**에 온다('최근 30일').

    표지 앞의 duration 은 다른 절의 값(임계값·유효기간·소요일)이다. 근접성만 보고 뒤쪽 맨
    표지에 붙이면 ``구매주기가 30일 이하`` 의 30일이 ``최근`` 의 창이 되고, 그 순간 진짜
    결핍이 '원문이 이미 말했다'로 뒤집혀 되묻기가 재방출로 샌다.
    """

    clauses = _clauses(query)
    marker_start = query.rindex("최근")
    covering = [
        clause
        for clause in clauses
        if clause.is_quantified and clause.covers(marker_start, marker_start + 2)
    ]
    assert covering == [], [clause.to_dict() for clause in clauses]
    # 그 duration 은 사라지지 않는다 — 표지에 묶이지 않은 절로 남는다(추측하지 않는다).
    assert any(clause.is_quantified and not clause.marker_bound for clause in clauses)


def test_span_coverage_is_by_overlap_not_identity() -> None:
    """근거 구간은 표지 **또는** 수량 어느 쪽으로 지목돼도 같은 절을 가리켜야 한다."""

    query = "최근 30일 구매한 회원 수를 알려줘"
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    by_marker = temporal_clause.quantified_clause_for_span(query, (0, 2), bindings)
    by_amount = temporal_clause.quantified_clause_for_span(query, (3, 6), bindings)
    assert by_marker is not None and by_amount is not None
    assert by_marker == by_amount


# ── 달력·절대 창도 '원문이 말한 기간'이다 ─────────────────────────────────────────
# ``지난달 구매한 회원`` 은 기간을 **확정해** 말한 문장이다. 이 모듈이 duration 리터럴만 읽던
# 동안에는 그 사실을 담을 절이 없어서, 모델이 ``지난달`` 을 지목해 "기간이 없다"를 신고하면
# 반박할 재료가 없었다 — 사용자의 달력 창이 되묻기 또는 배포 기본 창에 덮였다.


@pytest.mark.parametrize(
    ("query", "token", "expected"),
    [
        ("지난달 구매한 회원", "지난달", ("2026-07-01", "2026-08-01")),
        ("2026년 3월에 구매한 회원", "2026년 3월", ("2026-03-01", "2026-04-01")),
        ("작년 1월 가입한 회원", "작년 1월", ("2025-01-01", "2025-02-01")),
        ("올해 상반기 구매한 회원", "올해 상반기", ("2026-01-01", "2026-07-01")),
    ],
)
def test_a_calendar_token_is_a_quantified_clause(
    query: str, token: str, expected: tuple[str, str]
) -> None:
    """달력 토큰 하나가 곧 확정된 구간이다(표지가 없어도 기간을 말한 것이다)."""

    quantified = [clause for clause in _clauses(query) if clause.is_quantified]
    assert len(quantified) == 1, [clause.to_dict() for clause in _clauses(query)]
    clause = quantified[0]
    assert clause.relation == temporal_clause.RELATION_ABSOLUTE
    assert (clause.start, clause.end) == expected
    assert [span.text for span in clause.source_spans] == [token]


@pytest.mark.parametrize(
    "query",
    [
        "지난달 구매한 회원",
        "2026년 3월에 구매한 회원",
        "작년 1월 가입한 회원",
        "올해 상반기 구매한 회원",
    ],
)
def test_a_calendar_clause_carries_the_bindings_window_verbatim(query: str) -> None:
    """절의 wire 창은 리터럴 바인딩의 ``event_ir_window`` 와 **같은 dict** 여야 한다.

    구조화 안내의 계약이 '그대로 복사'이므로, 여기서 다른 모양(예: rolling 길이)을 만들면
    지시문과 프롬프트가 같은 창을 놓고 서로 다른 것을 요구하게 된다.
    """

    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    windows = [
        binding["normalized"]["event_ir_window"]
        for binding in bindings
        if binding["kind"] == "date_window"
    ]
    assert len(windows) == 1
    clause = next(clause for clause in _clauses(query) if clause.is_quantified)
    assert clause.wire_window == windows[0]


@pytest.mark.parametrize(
    ("query", "token"),
    [
        ("지난달 구매한 회원", "지난달"),
        ("2026년 3월에 구매한 회원", "2026년 3월"),
        ("작년 1월 가입한 회원", "작년 1월"),
    ],
)
def test_a_stated_calendar_period_refutes_a_missing_period_claim(
    query: str, token: str
) -> None:
    clause = temporal_clause.stated_period_for_issue(
        query,
        _period_issue(query, token),
        extract_literal_bindings(query, current_date=REFERENCE_DATE),
    )
    assert clause is not None
    assert clause.wire_window is not None
    assert clause.wire_window["type"] == "interval"


def test_a_marker_inside_a_calendar_token_is_not_a_bare_marker() -> None:
    """``지난달`` 의 ``지난`` 은 표지가 아니라 그 토큰의 글자다.

    홀로 남기면 원문이 이미 확정한 기간을 '맨 표지'로 다시 신고하는 유령 절이 되고, 그 절을
    읽는 쪽에는 그것이 유령임을 알 재료가 없다.
    """

    clauses = _clauses("지난달 구매한 회원")
    assert [clause.is_quantified for clause in clauses] == [True]
    assert not any(clause.marker_bound for clause in clauses)


def test_a_calendar_token_before_the_marker_is_not_that_markers_window() -> None:
    """어순 규칙은 달력 창에도 그대로 적용된다.

    ``2026년 3월 구매 이력이 있고 최근 가입한 회원`` 의 ``2026년 3월`` 은 앞 절의 창이다.
    그것을 뒤의 맨 ``최근`` 이 말한 기간으로 읽으면 진짜 결핍이 '원문이 이미 말했다'로 뒤집힌다.
    """

    query = "2026년 3월 구매 이력이 있고 최근 가입한 회원"
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    marker_start = query.rindex("최근")
    assert (
        temporal_clause.quantified_clause_for_span(
            query, (marker_start, marker_start + 2), bindings
        )
        is None
    )
    # 달력 창 자체는 사라지지 않는다 — 자기 자리에 절로 남는다.
    assert temporal_clause.quantified_clause_for_span(query, (0, 8), bindings) is not None


def test_a_bare_marker_without_a_calendar_token_is_still_a_real_gap() -> None:
    """달력 창을 읽게 됐다고 맨 '최근' 이 해결되지는 않는다(fail-close 유지)."""

    query = "최근 구매한 회원"
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    assert (
        temporal_clause.stated_period_for_issue(query, _period_issue(query, "최근"), bindings)
        is None
    )
