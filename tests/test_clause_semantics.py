"""절 단위 typed semantics — 극성·수량자·기간 소유권의 결정론 계약.

라이브 코퍼스는 회귀 게이트가 아니다(같은 코드로 두 번 돌려도 귀결이 갈린다). 그래서 이
축의 안전망은 전부 여기 있다 — LLM 을 태우지 않고 원문에서 곧장 판정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import clause_semantics  # noqa: E402
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402

FIXED_TODAY = "2026-08-08"


def analyze(query: str) -> tuple[clause_semantics.ClauseSemantics, ...]:
    return clause_semantics.analyze_clauses(
        query,
        audience_runtime.catalog_snapshot(),
        extract_literal_bindings(query, current_date=FIXED_TODAY),
    )


# ── 극성 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("최근 90일 동안 주문하지 않은 회원", clause_semantics.Polarity.NEGATIVE),
        ("최근 90일 동안 주문한 회원", clause_semantics.Polarity.POSITIVE),
    ],
)
def test_polarity_follows_local_negation(query: str, expected) -> None:
    clauses = analyze(query)
    assert len(clauses) == 1
    assert clauses[0].polarity is expected


def test_negation_of_one_clause_does_not_leak_to_the_other() -> None:
    """한 절의 부정이 다른 절의 극성을 오염시키지 않는다."""

    clauses = analyze("최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원을 찾아줘.")
    assert [str(clause.polarity) for clause in clauses] == ["negative", "positive"]


# ── 절 단위 기간 소유권 ──────────────────────────────────────────────────────────


def test_window_belongs_to_the_negated_clause_only() -> None:
    """감사 #62 의 뿌리. ``최근 30일`` 은 **부정 절**의 것이고, 뒤 절은 무한 구간이다.

    이 사실이 없던 동안 기간 결핍이 문장 전체 단위로 판정돼, 원문이 말한 기간을 두고
    ``기간 값이 없습니다`` 로 되물었다.
    """

    clauses = analyze("최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원을 찾아줘.")
    negative, positive = clauses
    assert negative.has_period
    assert negative.temporal is not None
    assert negative.temporal.clause.wire_window == {
        "type": "rolling", "value": 30, "unit": "day"
    }
    assert not positive.has_period
    assert positive.temporal is None


def test_each_clause_keeps_its_own_window() -> None:
    """창이 둘이면 절도 둘이다 — 어순(수식어가 머리말 앞)이 소유권을 정한다."""

    clauses = analyze("2026년 2월에 가입하고 3월에 구매한 회원")
    windows = {clause.event: clause.temporal.clause.wire_window for clause in clauses}
    assert windows["signup"] == {
        "type": "interval", "start": "2026-02-01", "end_exclusive": "2026-03-01"
    }
    assert windows["purchase"] == {
        "type": "interval", "start": "2026-03-01", "end_exclusive": "2026-04-01"
    }


def test_period_deficit_is_settled_per_clause() -> None:
    """기간 결핍 신고는 **그 신고가 가리킨 절**이 답한다(문장 전체가 아니라)."""

    query = "최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원을 찾아줘."
    clauses = analyze(query)
    owner = clause_semantics.clause_owning_span(clauses, (0, 6))  # '최근 30일'
    assert owner is not None
    assert owner.has_period
    assert str(owner.quantifier) == "never"


def test_provenance_distinguishes_user_and_policy_windows() -> None:
    """정책이 채운 창과 사용자가 말한 창은 절 수준에서 구분된다."""

    clause = analyze("최근 90일 동안 주문하지 않은 회원")[0]
    assert clause.temporal is not None
    assert clause.temporal.provenance is clause_semantics.Provenance.USER_EXPLICIT
    policy = clause_semantics.ClauseTemporal(
        clause.temporal.clause, clause_semantics.Provenance.POLICY_DEFAULT
    )
    assert policy.provenance is clause_semantics.Provenance.POLICY_DEFAULT
    assert policy.to_dict()["provenance"] == "policy_default"


# ── 수량자 ──────────────────────────────────────────────────────────────────────


def test_absence_is_never_not_a_value_comparison() -> None:
    clause = analyze("앱으로 로그인하지 않은 회원")[0]
    assert clause.quantifier is clause_semantics.Quantifier.NEVER
    assert clause.event == "login"


def test_every_bucket_over_an_event_is_occurrence_not_state() -> None:
    """감사 #51. ``매월 한 번 이상 구매`` 는 **발생**의 칸별 전칭이다.

    상태 전칭과 같은 이름을 쓰면 사건 로그가 답할 수 있는 질문까지 함께 막힌다.
    """

    clause = analyze("최근 3개월 동안 매월 한 번 이상 구매한 회원")[0]
    assert clause.quantifier is clause_semantics.Quantifier.EVERY_BUCKET_OCCURRENCE
    assert clause.bucket_unit == "month"
    assert clause.required_capabilities == frozenset({"supports_all_occurrences"})


# ── 부재의 능력 계약(한정 여부가 가른다) ─────────────────────────────────────────


def test_bare_absence_can_be_answered_from_the_last_occurrence() -> None:
    """마지막 로그인이 창보다 앞이면 그 창에 로그인은 없다 — 단조성으로 답한다."""

    clause = analyze("최근 30일간 로그인하지 않은 회원")[0]
    assert not clause.qualified
    assert clause.required_capabilities == clause_semantics.BARE_ABSENCE_CAPABILITIES
    assert "supports_point_state" in clause.required_capabilities


def test_qualified_absence_strictly_requires_all_occurrences() -> None:
    """감사 #68. 마지막 로그인의 채널만 알면 그 **이전** 로그인의 채널은 모른다."""

    clause = analyze("앱으로 로그인하지 않은 회원")[0]
    assert clause.qualified
    assert clause.qualifier_evidence is not None
    assert clause.required_capabilities == clause_semantics.QUALIFIED_ABSENCE_CAPABILITIES
    assert clause.required_capabilities == frozenset({"supports_all_occurrences"})


def test_required_capability_names_match_the_declaration_keys() -> None:
    """능력 이름이 선언(``temporal_bindings.json``)의 키와 갈라지지 않게 고정한다."""

    import temporal_ir

    runtime = temporal_ir.create_temporal_runtime(
        audience_runtime.resolve_audience_catalog()
    )
    declared = set()
    for binding in runtime.temporal_catalog.bindings.values():
        declared.update(binding.observation_capabilities.to_dict())
    named = set(clause_semantics.BARE_ABSENCE_CAPABILITIES)
    named |= set(clause_semantics.QUALIFIED_ABSENCE_CAPABILITIES)
    for alternatives in clause_semantics.REQUIRED_CAPABILITY.values():
        named |= set(alternatives)
    assert named <= declared, f"선언에 없는 능력 이름: {sorted(named - declared)}"


# ── 추측 금지 ────────────────────────────────────────────────────────────────────


def test_no_event_means_no_clause() -> None:
    """이 계층은 '무엇이 없다'를 말하지 않는다 — 사건이 없으면 빈 결과다."""

    assert analyze("30대 여성 회원을 추출해줘.") == ()


def test_same_query_gives_the_same_clause_ids() -> None:
    """§63 재현성. 같은 원문은 같은 절 식별자를 낸다."""

    query = "최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원을 찾아줘."
    assert [item.clause_id for item in analyze(query)] == [
        item.clause_id for item in analyze(query)
    ]
