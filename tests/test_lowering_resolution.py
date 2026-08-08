"""판정자가 지원 여부의 **단일 답**을 내는지 — typed Resolution 과 모순 금지 불변식.

이 파일이 고정하는 것은 항목별 기대값이 아니라 **모순 금지**다. 같은 canonical 요청이
낮춰지는데 미지원으로 끝나면 그것은 도메인 한계가 아니라 버그다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import lowering_planner as planner  # noqa: E402
import semantic_diagnostics as sd  # noqa: E402

FIXED_TODAY = date(2026, 8, 8)


def resolve(query: str) -> planner.Resolution:
    return planner.resolve_executable(query, today=FIXED_TODAY)


# ── 낮출 수 있는 요구는 Executable 이다(감사 케이스 B) ────────────────────────────


@pytest.mark.parametrize(
    ("query", "must_contain"),
    [
        ("최근 90일 동안 주문하지 않은 회원을 추출해줘.", "NOT EXISTS"),
        ("최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원을 찾아줘.", "NOT EXISTS"),
        ("최근 3개월 동안 매월 한 번 이상 구매한 회원", "AND"),
    ],
)
def test_windowed_absence_and_bucket_occurrence_are_executable(
    query: str, must_contain: str
) -> None:
    resolution = resolve(query)
    assert isinstance(resolution, planner.Executable), type(resolution).__name__
    assert any(must_contain in plan.sql for plan in resolution.plans)


def test_bucket_occurrence_expands_to_one_exists_per_bucket() -> None:
    """감사 #51. 칸 전칭은 **발생**이므로 사건 로그로 답할 수 있다."""

    resolution = resolve("최근 3개월 동안 매월 한 번 이상 구매한 회원")
    assert isinstance(resolution, planner.Executable)
    sql = next(plan.sql for plan in resolution.plans)
    assert sql.count("EXISTS") == 3, sql
    for bucket in ("'20260601'", "'20260701'", "'20260801'"):
        assert bucket in sql, sql


# ── 부재는 근사되지 않는다(감사 #68) ────────────────────────────────────────────


def test_qualified_absence_is_missing_capability_not_a_value_comparison() -> None:
    resolution = resolve("앱으로 로그인하지 않은 회원")
    assert isinstance(resolution, planner.MissingCapability)
    diagnostic = resolution.diagnostic
    assert diagnostic.code == sd.MISSING_CAPABILITY
    assert diagnostic.outcome is sd.Outcome.UNSUPPORTED
    assert "supports_all_occurrences" in (diagnostic.required_capability or "")
    assert diagnostic.symbol == "login"


def test_bare_absence_still_lowers_from_the_last_occurrence() -> None:
    """살아 있던 미접속 경로를 능력 계약이 잡아먹지 않는다."""

    resolution = resolve("6개월 이상 접속하지 않은 휴면 고객")
    assert isinstance(resolution, planner.Executable)
    assert any("LAST_LOGIN_DATE" in plan.sql for plan in resolution.plans)


# ── 관할 밖은 아무것도 주장하지 않는다 ──────────────────────────────────────────


def test_requests_without_captured_obligations_are_undetermined() -> None:
    """판정자가 캡처하지 않은 모양을 '한계'로 보고하면 없는 한계를 광고한다."""

    resolution = resolve("30대 여성 회원을 추출해줘.")
    assert isinstance(resolution, planner.Undetermined)
    assert resolution.reason


def test_undetermined_never_contradicts_any_outcome() -> None:
    resolution = resolve("30대 여성 회원을 추출해줘.")
    for outcome in sd.Outcome:
        assert not planner.contradicts(resolution, str(outcome))


# ── 모순 금지 불변식(T6) ────────────────────────────────────────────────────────


CONTRADICTION_CORPUS = (
    "최근 90일 동안 주문하지 않은 회원을 추출해줘.",
    "최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원을 찾아줘.",
    "최근 3개월 동안 매월 한 번 이상 구매한 회원",
    "6개월 이상 접속하지 않은 휴면 고객",
    "앱으로 로그인하지 않은 회원",
    "30대 여성 회원을 추출해줘.",
)


def _model_reports_unsupported(query: str) -> dict:
    """구조화 모델이 문장 **전체**를 '표현할 수 없다'고 신고한 payload.

    감사 케이스 B 가 정확히 이 모양이었다 — 낮출 수 있는 요구인데 모델이 못 냈고, 그 신고가
    그대로 사용자 귀결이 됐다.
    """

    from query_structurer.campaign_plan_v4 import (
        attach_campaign_query_plan_v4_identity,
        validate_campaign_query_plan_v4,
    )

    enriched = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None, "offer_type": None,
                "channels": None, "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [{
                    "code": "unsupported_semantics",
                    "argument": "audience_condition",
                    "message": "표현할 수 없습니다",
                    "evidence": {"text": query, "start": 0, "end": len(query)},
                }],
            },
            "semantic_plan": {"nodes": []},
        },
        query,
        current_date=FIXED_TODAY.isoformat(),
    )
    return validate_campaign_query_plan_v4(
        enriched, query=query, raw_query=query, require_semantic=True
    )


@pytest.mark.parametrize("query", CONTRADICTION_CORPUS)
def test_executable_request_never_ends_as_unsupported(query: str) -> None:
    """``planner = Executable`` 과 ``final = unsupported`` 는 동시에 성립할 수 없다.

    모델이 문장 전체를 미지원으로 신고해도, 판정자가 그 자리를 낮출 수 있으면 귀결은
    미지원이 아니어야 한다. 항목별 기대값이 아니라 **모순 금지**를 고정하는 테스트다.
    """

    resolution = resolve(query)
    plan = _model_reports_unsupported(query)
    final = (plan.get("semantic_ir") or {}).get("status")
    assert not planner.contradicts(resolution, _outcome_of(final)), (
        f"판정자는 {type(resolution).__name__} 인데 귀결은 {final!r} 이다: {query}"
    )


def _outcome_of(status: str | None) -> str:
    """구조화 응답의 status → 이 저장소의 사용자 귀결 이름."""

    return (
        str(sd.Outcome.UNSUPPORTED)
        if status == "unsupported"
        else str(sd.Outcome.CLARIFICATION)
        if status == "needs_clarification"
        else "resolved"
    )


def test_planner_resolution_is_recorded_for_shadow_comparison() -> None:
    """legacy 게이트와 판정자가 갈릴 때 **무엇이 갈렸는지**가 응답에 남는다(Phase 3A)."""

    from query_structurer.audience_execution import PLANNER_RESOLUTION_KEY

    plan = _model_reports_unsupported("앱으로 로그인하지 않은 회원")
    record = plan.get(PLANNER_RESOLUTION_KEY)
    assert isinstance(record, dict)
    assert record["kind"] == "MissingCapability"
    assert record["diagnostic"]["code"] == sd.MISSING_CAPABILITY


def test_capability_gap_replaces_the_generic_unsupported_sentence() -> None:
    """이관된 첫 게이트. 귀결은 그대로 미지원이고 **이름만** 정확해진다(#68)."""

    plan = _model_reports_unsupported("앱으로 로그인하지 않은 회원")
    semantic = plan["semantic_ir"]
    assert semantic["status"] == "unsupported"
    assert semantic["message"] != "요청한 조건을 현재 실행 자산으로 표현할 수 없습니다."
    assert "supports_all_occurrences" in semantic["message"]


def test_executable_cannot_be_built_without_a_plan() -> None:
    """계획 없는 ``Executable`` 은 근거 없는 지원 선언이다."""

    with pytest.raises(ValueError):
        planner.Executable(())


# ── 소유 대장: 같은 자리를 두 의무가 주장하지 않는다 ────────────────────────────


def test_metric_comparison_keeps_ownership_of_its_span() -> None:
    """``구매금액`` 안의 ``구매`` 를 존재 조건으로 읽으면 비교 의무가 조용히 좁아진다."""

    obligations = planner.clause_obligations("2026년 2월과 3월의 구매금액이 증가한 회원")
    assert obligations == ()


def test_resolution_is_reproducible() -> None:
    """§63. 같은 입력·같은 기준 시각에서는 같은 답이 나온다."""

    query = "최근 3개월 동안 매월 한 번 이상 구매한 회원"
    first, second = resolve(query), resolve(query)
    assert type(first) is type(second)
    assert [plan.sql for plan in first.plans] == [plan.sql for plan in second.plans]  # type: ignore[attr-defined]


# ── 정직한 미지원: 주체와 이력 소스(#44 · #73) ──────────────────────────────────


def test_a_non_member_subject_is_unsupported_not_an_internal_failure() -> None:
    """감사 #44. 기준선의 귀결은 ``failure`` 였고 사용자에게 줄 문장이 **하나도 없었다**."""

    from query_structurer.audience_execution import DIAGNOSTIC_KEY

    query = "구매 회원이 100명 이상인 브랜드를 알려줘."
    resolution = resolve(query)
    assert isinstance(resolution, planner.InvalidSemantics)
    assert resolution.diagnostic.code == sd.UNSUPPORTED_SUBJECT
    assert resolution.diagnostic.outcome is sd.Outcome.UNSUPPORTED

    plan = _model_reports_unsupported(query)
    semantic = plan["semantic_ir"]
    assert semantic["status"] == "unsupported"
    assert "브랜드" in semantic["message"]
    assert plan[DIAGNOSTIC_KEY]["code"] == sd.UNSUPPORTED_SUBJECT


def test_a_member_subject_request_naming_a_brand_is_untouched() -> None:
    """축 개방과 결과 주체는 다른 축이다 — 조건에 브랜드가 나와도 주체는 회원이다."""

    resolution = resolve("서로 다른 브랜드를 정확히 두 개 구매한 회원을 추출해줘.")
    assert not isinstance(resolution, planner.InvalidSemantics)


def test_state_history_absence_names_the_missing_declaration() -> None:
    """감사 #73. 귀결은 미지원을 유지하고 **사유가 이력 소스 부재**로 바뀐다."""

    import audience_runtime
    import temporal_claims
    import temporal_ir

    catalog = audience_runtime.resolve_audience_catalog()
    outcome = temporal_claims.synthesize_temporal_claim(
        "여성이면서 정상에서 휴면으로 바뀐 회원",
        snapshot=audience_runtime.catalog_snapshot(),
        catalog=catalog,
        runtime=temporal_ir.create_temporal_runtime(catalog),
        context=temporal_claims.request_context_for(FIXED_TODAY.isoformat()),
        today=FIXED_TODAY,
    )
    assert isinstance(outcome, temporal_claims.TemporalClaimRejection)
    # 값이 모자란 것이 아니다 — 그 축에 시간 관측 지표가 없다.
    assert outcome.code == temporal_claims.METRIC_NOT_DECLARED
    assert outcome.disposition == temporal_claims.UNSUPPORTED
    assert "member_state" in outcome.message


def test_the_declared_disposition_maps_to_one_user_outcome() -> None:
    """선언된 사유마다 사용자 귀결이 **하나**로 파생된다(Outcome Mapper 단일화)."""

    from query_structurer.audience_execution import _TEMPORAL_DIAGNOSTIC_CODES

    for code in _TEMPORAL_DIAGNOSTIC_CODES.values():
        assert code in sd.declared_codes(), code
