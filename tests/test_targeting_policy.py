"""정책은 한 곳에서 정해지고, 모든 결정은 영수증을 남긴다.

같은 입력이 실행마다 다른 귀결을 내던 원인은 정책이 흩어져 있고 버전이 없다는 것이었다
(2026-08-06 실측 #5·#16·#26). 여기서 그 계약을 고정한다.
"""

from __future__ import annotations

import pytest

import targeting_policy

REQUIREMENT_KEY = "audience_requirement"


def _period_issue(query: str, marker: str) -> dict[str, object]:
    start = query.find(marker)
    return {
        "code": "missing_argument",
        "argument": "period",
        "message": "no duration",
        "evidence": {"text": marker, "start": start, "end": start + len(marker)},
    }


def _plan(query: str, marker: str) -> dict[str, object]:
    return {REQUIREMENT_KEY: {"expression": None, "issues": [_period_issue(query, marker)]}}


def _missing(plan: object) -> list[dict[str, object]]:
    requirement = plan.get(REQUIREMENT_KEY) if isinstance(plan, dict) else None
    issues = requirement.get("issues") if isinstance(requirement, dict) else []
    return [
        dict(item)
        for item in issues or []
        if item.get("code") == "missing_argument" and item.get("argument") == "period"
    ]


def _repaired(window: dict[str, object]) -> dict[str, object]:
    return {
        REQUIREMENT_KEY: {
            "expression": {"type": "time_filter", "window": window},
            "issues": [],
        }
    }


def test_unknown_stage_or_decision_is_rejected() -> None:
    with pytest.raises(targeting_policy.PolicyContractError):
        targeting_policy.PolicyDecision(
            policy_id="x", stage="whenever", decision="allow", reason_code="r"
        )
    with pytest.raises(targeting_policy.PolicyContractError):
        targeting_policy.PolicyDecision(
            policy_id="x", stage="ambiguity", decision="maybe", reason_code="r"
        )


def test_precedence_is_declared_not_implied() -> None:
    order = sorted(targeting_policy.PRECEDENCE, key=targeting_policy.PRECEDENCE.get)
    assert order == [
        "safety",
        "data_availability",
        "ambiguity",
        "default_binding",
        "catalog_capability",
        "compilation",
    ]


def test_stated_period_is_repaired_not_asked_about() -> None:
    """원문이 이미 말한 기간은 되물을 것이 아니다 — 교정하고 영수증을 남긴다."""

    query = "최근 30일 구매한 회원 수를 알려줘"
    plan = _plan(query, "최근")
    calls: list[str] = []

    def restructure(instruction: str) -> dict[str, object]:
        calls.append(instruction)
        return _repaired({"type": "rolling", "value": 30, "unit": "day"})

    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date="2026-08-06",
        restructure=restructure,
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )
    assert calls and "30" in calls[0]
    assert not _missing(result)
    decisions = targeting_policy.decisions(result)
    assert decisions[0]["reason_code"] == targeting_policy.REASON_PERIOD_STATED
    assert decisions[0]["policy_version"] == targeting_policy.POLICY_VERSION


def test_a_repair_that_drops_the_stated_period_is_rejected() -> None:
    """모델이 다른 창을 지어내면 채택하지 않는다 — 원래 결핍을 지킨다."""

    query = "최근 30일 구매한 회원 수를 알려줘"
    plan = _plan(query, "최근")
    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date="2026-08-06",
        restructure=lambda _instruction: _repaired(
            {"type": "rolling", "value": 7, "unit": "day"}
        ),
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )
    assert result is plan, "채택되지 않았으면 원본 플랜이 그대로 남아야 한다"
    reasons = [item["reason_code"] for item in targeting_policy.decisions(result)]
    assert targeting_policy.REASON_PERIOD_REPAIR_FAILED in reasons


def test_an_honest_unsupported_repair_is_adopted_not_discarded() -> None:
    """교정이 '표현 불가'를 이름 대며 선언하면 그것이 정직한 귀결이다.

    버리고 원본을 돌려주면 사용자는 **답이 이미 있는 기간**을 되묻는 화면을 본다(실측 #25:
    '최근 6개월 매월 존재한 회원' 이 정확히 그 상태로 clarification 이 됐다).
    """

    query = "최근 6개월 매월 존재한 회원"
    plan = _plan(query, "최근")
    unsupported_candidate = {
        REQUIREMENT_KEY: {"expression": None, "issues": []},
        "semantic_ir": {
            "status": "unsupported",
            "unsupported_operations": [{"reason": "매월 존재한 을 표현할 수 없습니다"}],
        },
    }
    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date="2026-08-06",
        restructure=lambda _instruction: unsupported_candidate,
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )
    assert result is unsupported_candidate, "정직한 미지원 판정을 거짓 결핍으로 되돌리지 않는다"
    decision = targeting_policy.decisions(result)[0]
    assert decision["decision"] == targeting_policy.DECISION_UNSUPPORTED
    assert decision["reason_code"] == targeting_policy.REASON_PERIOD_REPAIR_UNSUPPORTED


def test_an_empty_repair_is_still_rejected() -> None:
    """'아무것도 못 만들었다'는 미지원 선언이 아니다 — 원래 결핍을 지킨다(퇴화 방지)."""

    query = "최근 30일 구매한 회원"
    plan = _plan(query, "최근")
    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date="2026-08-06",
        restructure=lambda _instruction: {REQUIREMENT_KEY: {"expression": None, "issues": []}},
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )
    assert result is plan
    reasons = [item["reason_code"] for item in targeting_policy.decisions(result)]
    assert targeting_policy.REASON_PERIOD_REPAIR_FAILED in reasons


def test_bare_recency_is_left_to_the_default_binding_stage() -> None:
    """원문이 정말 기간을 말하지 않았으면 이 단계는 아무것도 고치지 않는다."""

    query = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"
    plan = _plan(query, "최근")
    called = False

    def restructure(_instruction: str) -> dict[str, object]:
        nonlocal called
        called = True
        return plan

    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date="2026-08-06",
        restructure=restructure,
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )
    assert not called, "말하지 않은 기간을 재구조화로 지어내지 않는다"
    reasons = [item["reason_code"] for item in targeting_policy.decisions(result)]
    assert targeting_policy.REASON_PERIOD_ABSENT in reasons


def test_data_availability_decision_follows_the_declared_mode() -> None:
    advise = targeting_policy.decide_data_availability(
        {}, has_coverage_gap=True, catalog={"data_availability_policy": "advise"}
    )
    block = targeting_policy.decide_data_availability(
        {}, has_coverage_gap=True, catalog={"data_availability_policy": "block"}
    )
    assert advise.decision == targeting_policy.DECISION_ALLOW
    assert block.decision == targeting_policy.DECISION_UNSUPPORTED
    # 같은 입력에서 두 배포가 다르게 답하는 것은 정상이다 — 다만 그 차이가 **선언**에서 나와야 한다.
    assert advise.reason_code != block.reason_code


def test_decisions_are_ordered_by_precedence() -> None:
    plan: dict[str, object] = {}
    targeting_policy.record_decision(
        plan,
        targeting_policy.PolicyDecision(
            policy_id="late", stage="compilation", decision="allow", reason_code="ok"
        ),
    )
    targeting_policy.record_decision(
        plan,
        targeting_policy.PolicyDecision(
            policy_id="early", stage="safety", decision="allow", reason_code="ok"
        ),
    )
    assert [item["policy_id"] for item in targeting_policy.decisions(plan)] == ["early", "late"]


def test_digest_changes_with_the_decisions() -> None:
    empty = targeting_policy.digest({})
    plan: dict[str, object] = {}
    targeting_policy.decide_data_availability({}, has_coverage_gap=False)
    targeting_policy.decide_data_availability(plan, has_coverage_gap=True)
    assert targeting_policy.digest(plan) != empty
