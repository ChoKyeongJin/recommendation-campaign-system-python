"""기간 결핍 신고의 소유권 — 맨 '최근'이 언제 결핍이고 언제 동어반복인가.

배경(2026-08-08 실측). ``최근에 등급이 승급한 회원`` 이 되묻기로 닫혔다. 구조화기는 규칙대로
"기간 값이 없다"를 신고했는데, 그 절은 기간이 없어도 뜻이 정해진다 — 월 스냅샷의 전이는
'직전 관측 대비 이번 관측'이고 '최근'은 그 선택의 동어반복이다. 반대로 같은 낱말이 구매 집계
절에 붙으면 진짜 결핍이다(창이 없으면 집합이 정해지지 않는다).

그래서 이 파일이 고정하는 것은 낱말이 아니라 **근거**다.

1. 소유 판정의 근거는 낱말 목록이 아니라 *그 절이 실제로 낮춰지는가* 다.
2. 판정 단위는 절이다 — 옆 절이 낮춰졌다고 이 절의 결핍이 사라지지 않는다.
3. 기간을 품은 신고는 이 판정의 소관이 아니다(그 반박의 소유자는 :mod:`temporal_clause`).
4. 귀결이 모델의 신고 **코드**에 따라 갈리지 않는다 — 같은 문장은 같은 답으로 간다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_issue_contract  # noqa: E402
from query_structurer import audience_execution  # noqa: E402

REFERENCE_DATE = "2026-08-08"


def _issue(query: str, marker: str, *, code: str = "missing_argument", argument: str = "period"):
    start = query.find(marker)
    assert start >= 0, f"표지 {marker!r} 가 원문에 없다"
    return {
        "code": code,
        "argument": argument,
        "message": "기간을 확정할 수 없습니다.",
        "evidence": {"text": marker, "start": start, "end": start + len(marker)},
    }


# ── 1. 소유 판정 ────────────────────────────────────────────────────────────────

OWNERSHIP_CASES: tuple[tuple[str, str, bool], ...] = (
    # 스스로 창을 확정하는 절(월 스냅샷 전이) — '최근'은 as_of 의 동어반복이다.
    ("최근에 등급이 승급한 회원", "최근", True),
    ("최근에 가치등급이 강등된 회원", "최근", True),
    ("최근에 골드에서 VIP로 바뀐 회원", "최근", True),
    # 창이 없으면 뜻이 정해지지 않는 절 — 진짜 결핍이므로 신고가 그대로 남는다.
    ("최근에 3회 이상 구매한 회원", "최근", False),
    ("최근에 가입한 회원", "최근", False),
    # 낮춰진 절은 **옆 절**이다. 절 경계를 넘어 소유권을 주장하지 않는다.
    ("등급이 승급한 회원 중 최근 3회 이상 구매한 회원", "최근", False),
)


@pytest.mark.parametrize(
    ("query", "marker", "owned"), OWNERSHIP_CASES, ids=[case[0] for case in OWNERSHIP_CASES]
)
def test_a_bare_period_report_is_owned_only_by_a_clause_that_lowers(
    query: str, marker: str, owned: bool
) -> None:
    assert (
        audience_issue_contract.period_issue_owned_by_lowered_clause(query, _issue(query, marker))
        is owned
    )


def test_a_report_that_quotes_a_duration_belongs_to_another_judge() -> None:
    """'최근 30일'을 인용한 신고는 이 판정의 소관이 아니다 — 반박 근거가 둘이면 갈라진다."""

    query = "최근 30일 동안 구매한 회원"
    assert not audience_issue_contract.period_issue_owned_by_lowered_clause(
        query, _issue(query, "최근 30일")
    )


def test_ownership_needs_a_span_not_a_word() -> None:
    """근거 구간이 없으면 아무것도 주장하지 않는다(단방향 증거)."""

    query = "최근에 등급이 승급한 회원"
    assert not audience_issue_contract.bare_period_issue_owned_by_spans(
        query, _issue(query, "최근"), ()
    )


# ── 2. 배선: 판정이 실제로 SQL 을 되살리는가 ────────────────────────────────────


def _synthesis(query: str, issue: dict):
    return audience_execution._application_owned_synthesis(
        query, [issue], [], current_date=REFERENCE_DATE
    )


@pytest.mark.parametrize(
    "query",
    ["최근에 등급이 승급한 회원", "최근에 가치등급이 승급한 회원", "최근에 등급이 강등된 회원"],
)
def test_a_period_report_on_a_self_windowing_clause_is_discharged(query: str) -> None:
    """되묻는 대신 그 절을 낮춰 신고를 방면한다 — 표현이 나오고 신고가 닫힌다."""

    issue = _issue(query, "최근")
    synthesis = _synthesis(query, issue)
    assert synthesis is not None, "기간 결핍 신고가 합성에 도달하지 못했다"
    assert synthesis.issue_keys == (audience_execution._audience_issue_key(issue),)
    assert synthesis.expression.type == "exists"


@pytest.mark.parametrize("query", ["최근에 3회 이상 구매한 회원", "최근에 가입한 회원"])
def test_a_real_period_gap_still_closes(query: str) -> None:
    """진짜 결핍은 그대로 남는다 — 열린 것은 되묻기가 아니라 **답할 수 있는 절**뿐이다."""

    assert _synthesis(query, _issue(query, "최근")) is None


def test_the_outcome_does_not_depend_on_which_code_the_model_reported() -> None:
    """같은 문장이 신고 코드에 따라 갈리지 않는다.

    실측 #17 에서는 같은 요청이 ``unsupported_semantics`` 로 신고되면 되살아나고
    ``missing_argument(period)`` 로 신고되면 되묻기로 닫혔다. 귀결이 방출 편차를 따라가면
    회귀 판정 자체가 흔들리므로, 두 코드가 같은 합성에 도달하는 것을 계약으로 고정한다.
    """

    query = "최근에 등급이 승급한 회원"
    by_period = _synthesis(query, _issue(query, "최근"))
    by_unsupported = _synthesis(
        query,
        _issue(query, "등급이 승급한", code="unsupported_semantics", argument="member_state_history"),
    )
    assert by_period is not None and by_unsupported is not None
    assert by_period.expression.to_dict() == by_unsupported.expression.to_dict()
