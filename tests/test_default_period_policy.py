"""기간 없는 '최근'의 계층별 계약 — 코어는 되묻고, 기본값은 호출 계층이 준다.

한 문장('최근 …')에 대해 서로 다른 두 결말이 옳을 수 있다. 되묻기와 기본값 중 무엇이 옳은지는
원문이 아니라 **제품 설정**이 정하기 때문이다. 예전에는 그 결정이 구조화기 프롬프트 안에 있었고
(그 자리에 '맨 최근 = 최근 5일'이 적혀 있었다), 그래서 같은 저장소의 두 테스트가 서로 반대를
주장했다. 여기서는 그 둘을 계층으로 갈라 각각 고정한다.

* **단위(구조화기)** — 기간 없는 '최근'은 값을 지어내지 않고 clarification 으로 닫는다.
* **통합(제품)** — 기본 기간이 설정된 배포에서는 그 값으로 채워 SQL 까지 간다.
* **불변식** — 사용자가 말한 기간은 언제나 이긴다(정책은 결핍이 있을 때만 돌고, 명시값을
  잃은 재구조화 결과는 채택하지 않는다).

세 번째 결말(설정이 없는 배포의 종단 fail-close)은
``tests/test_retired_axes_fail_close.py::test_bare_recent_campaign_count_without_a_default_window_remains_blocked``
가 이미 래칫으로 잡고 있다 — ``docs/data/test_baselines/live_prompts.json`` 78번이 그 이름을
인용하므로 여기로 옮기지 않는다.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import default_period_policy  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import plan_decisions  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    AUDIENCE_REQUIREMENT_KEY,
    EVENT_EXPRESSION_KEY,
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.prompt import build_campaign_query_plan_v4_user_prompt  # noqa: E402
from query_structurer.types import QueryStructuringInput, StructuringContext  # noqa: E402


CURRENT_DATE = "2026-08-02"
BARE_RECENT_QUERY = (
    "최근 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 "
    "재반응 유도 캠페인을 만들어줘."
)
SPECIFIED_QUERY = (
    "최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 "
    "재반응 유도 캠페인을 만들어줘."
)
CONTACT_EVIDENCE = "최근 캠페인 발송 성공 횟수가 3회 이상"
DEFAULT_PERIOD = default_period_policy.DefaultPeriod(value=5, unit="day", origin="test")


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _campaign_audience_expression(
    query: str,
    *,
    contact_evidence: str,
    window: event_ir.TimeWindow | None,
) -> event_ir.Condition:
    """모델이 내는 모양: 접촉 성공 건수 임계 + 구매반응 부재."""

    contact_relation: event_ir.Relation = event_ir.Source(name="campaign_contact_success")
    if window is not None:
        contact_relation = event_ir.Filter(
            relation=contact_relation,
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="campaign_contact_success.occurred_at"),
                window=window,
            ),
        )
    return event_ir.And(
        operands=(
            event_ir.Comparison(
                operator=">=",
                left=event_ir.Aggregate(
                    function="count",
                    relation=contact_relation,
                    expression=event_ir.FieldRef(
                        name="campaign_contact_success.execution_id"
                    ),
                    distinct=True,
                ),
                right=event_ir.Literal(value=3),
                evidence=_evidence(query, contact_evidence),
            ),
            event_ir.Not(
                operand=event_ir.Exists(
                    relation=event_ir.Source(name="campaign_purchase_response"),
                    evidence=_evidence(query, "구매반응이 없는"),
                )
            ),
        )
    )


def _structured(query: str, raw: dict[str, Any]) -> dict[str, Any]:
    return attach_campaign_query_plan_v4_identity(
        copy.deepcopy(raw), query, current_date=CURRENT_DATE
    )


def _raw(
    *,
    expression: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LLM 도구 계약 그대로의 4필드 payload."""

    return {
        "intent": "recommend_campaign",
        "campaign_constraints": {
            "objective": "reactivation",
            "offer_type": None,
            "channels": [],
            "sell_object": None,
        },
        "result_limit": 100,
        AUDIENCE_REQUIREMENT_KEY: {
            "expression": expression,
            "issues": list(issues or []),
        },
    }


def _missing_period_issue(query: str) -> dict[str, Any]:
    return {
        "code": "missing_argument",
        "argument": "period",
        "message": (
            "The term '최근' requires a specific period and the query states none."
        ),
        "evidence": {"text": "최근", "start": 0, "end": 2},
    }


def _bare_recent_plan() -> dict[str, Any]:
    """구조화기가 되물은 상태의 플랜(표현 없음 + 기간 결핍)."""

    return _structured(
        BARE_RECENT_QUERY,
        _raw(issues=[_missing_period_issue(BARE_RECENT_QUERY)]),
    )


def _windowed_plan(query: str, *, contact_evidence: str, days: int) -> dict[str, Any]:
    """기간이 채워진 재구조화 결과(정책이 받는 후보)."""

    return _structured(
        query,
        _raw(
            expression=_campaign_audience_expression(
                query,
                contact_evidence=contact_evidence,
                window=event_ir.RollingWindow(value=days, unit="day"),
            ).to_dict()
        ),
    )


# ── 단위: 구조화기는 기간을 지어내지 않는다 ─────────────────────────────────────────


def test_structuring_prompt_never_supplies_a_default_period() -> None:
    """구조화기 프롬프트에 기본 창이 없다(예전 'five days' 지시의 자리).

    `tests/test_canonical_audience_path.py::test_bare_recent_prompt_uses_the_five_day_rolling_default`
    에서 옮겨 오며 주장을 뒤집었다 — 기본 기간은 프롬프트가 아니라 호출 계층이 소유한다.
    """

    prompt = build_campaign_query_plan_v4_user_prompt(
        QueryStructuringInput(
            query=BARE_RECENT_QUERY,
            context=StructuringContext(current_date=CURRENT_DATE),
        )
    )

    assert '"type": "rolling", "value": 5, "unit": "day"' not in prompt
    assert "Do not return a missing_argument issue for period." not in prompt
    assert "do not substitute a default window" in prompt
    assert "missing_argument with argument='period'" in prompt


def test_bare_recent_without_a_period_stays_a_clarification() -> None:
    """기간 없는 '최근'은 표현이 서지 않고 되묻기로 닫힌다."""

    structured = _bare_recent_plan()

    assert structured[AUDIENCE_REQUIREMENT_KEY]["expression"] is None
    assert structured.get(EVENT_EXPRESSION_KEY) is None
    assert structured["semantic_ir"]["status"] == "needs_clarification"
    assert default_period_policy.missing_period_issues(structured)


def test_a_model_window_guess_is_still_rejected_by_the_validators() -> None:
    """프롬프트가 아니라 **검증기**가 fail-close 를 강제한다.

    모델이 지시를 어기고 기간 없는 '최근' 위에 창 없는 표현을 세워도, 그 절을 덮는 원자에
    TimeFilter 가 없으므로 애플리케이션이 같은 결핍을 다시 신고한다.
    """

    structured = _structured(
        BARE_RECENT_QUERY,
        _raw(
            expression=_campaign_audience_expression(
                BARE_RECENT_QUERY,
                contact_evidence=CONTACT_EVIDENCE,
                window=None,
            ).to_dict()
        ),
    )

    assert default_period_policy.missing_period_issues(structured)
    assert structured["semantic_ir"]["status"] == "needs_clarification"


# ── 정책 해석: 설정이 없으면 정책도 없다 ───────────────────────────────────────────


def test_the_policy_is_off_unless_it_is_configured(monkeypatch: Any) -> None:
    monkeypatch.delenv(default_period_policy.DEFAULT_PERIOD_ENV, raising=False)

    assert default_period_policy.resolve_default_period() is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("5 day", (5, "day")),
        ("5days", (5, "day")),
        (" 5 DAYS ", (5, "day")),
        ("2 weeks", (2, "week")),
    ],
)
def test_a_configured_period_is_read_with_its_unit(
    monkeypatch: Any, configured: str, expected: tuple[int, str]
) -> None:
    """단위 어휘는 event_ir 이 소유한다 — 설정도 그 이름으로 말한다."""

    monkeypatch.setenv(default_period_policy.DEFAULT_PERIOD_ENV, configured)
    period = default_period_policy.resolve_default_period()

    assert period is not None
    assert period.key == expected


@pytest.mark.parametrize(
    "configured", ["", "  ", "5", "day", "0 day", "-3 day", "5 parsec", "5 hour", "5일"]
)
def test_an_unreadable_setting_never_becomes_a_silent_default(
    monkeypatch: Any, configured: str
) -> None:
    """오타 하나가 조용히 '최근 5일'이 되면 이 모듈이 막으려던 상태가 된다."""

    monkeypatch.setenv(default_period_policy.DEFAULT_PERIOD_ENV, configured)

    assert default_period_policy.resolve_default_period() is None


# ── 통합: 기본 기간이 설정된 제품은 최근 5일로 성공한다 ───────────────────────────


def test_configured_default_period_fills_the_window_and_compiles() -> None:
    """기본 기간이 설정된 배포에서 맨 '최근'이 최근 5일로 실행된다.

    `tests/test_canonical_audience_path.py::test_bare_recent_uses_the_default_five_day_window_and_compiles`
    에서 옮겨 왔다 — 그 판에서는 5일 창이 **이미** 들어 있는 payload 를 넣어 컴파일만 봤고,
    그래서 그 창을 누가 골랐는지는 아무것도 고정하지 못했다.
    """

    calls: list[str] = []

    def restructure(instruction: str) -> dict[str, Any]:
        calls.append(instruction)
        return _windowed_plan(
            BARE_RECENT_QUERY, contact_evidence=CONTACT_EVIDENCE, days=5
        )

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=restructure,
    )

    # 지시문은 애플리케이션 소유다 — 값과 그 값이 붙을 원문 구간을 함께 지목한다.
    assert len(calls) == 1
    assert '{"type": "rolling", "value": 5, "unit": "day"}' in calls[0]
    assert "'최근'" in calls[0]

    assert applied[AUDIENCE_REQUIREMENT_KEY]["issues"] == []
    # 'resolved'(사용자 문장만으로 확정)가 아니라 '정책이 채운 성공'이다.
    assert applied["semantic_ir"]["status"] == "policy_applied"
    assert applied["semantic_ir"]["policy_applications"] == [
        {"policy_id": default_period_policy.POLICY_OWNER, "fields": ["audience.period"]}
    ]
    assert list(
        event_ir.time_windows(
            event_ir.condition_from_dict(applied[AUDIENCE_REQUIREMENT_KEY]["expression"])
        )
    ) == [event_ir.RollingWindow(value=5, unit="day")]

    plan = graph_rag.build_query_plan(
        BARE_RECENT_QUERY, parser="llm", query_plan_v4=applied
    )
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        BARE_RECENT_QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=BARE_RECENT_QUERY,
    )

    assert result["sql"] is not None

    # 성공 응답이다 — 기본값이 채운 성공은 되묻기가 아니다.
    api_response = graph_rag.build_recommendation_api_response(
        BARE_RECENT_QUERY, plan, result, {}
    )
    assert api_response["status"] == "success"


def test_the_applied_default_reaches_the_api_response_without_debug() -> None:
    """구분 표식이 debug 뒤에만 있으면 보통의 소비자는 여전히 구분할 수 없다.

    ``semantic_ir`` 와 ``interpretation_status`` 는 debug 게이트 밖의 상시 응답 필드다.
    """

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _windowed_plan(
            BARE_RECENT_QUERY, contact_evidence=CONTACT_EVIDENCE, days=5
        ),
    )
    plan = graph_rag.build_query_plan(
        BARE_RECENT_QUERY, parser="llm", query_plan_v4=applied
    )
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        BARE_RECENT_QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=BARE_RECENT_QUERY,
    )
    api_response = graph_rag.build_recommendation_api_response(
        BARE_RECENT_QUERY, plan, result, {}
    )

    assert api_response["interpretation_status"] == "policy_applied"
    assert api_response["semantic_ir"]["policy_applications"] == [
        {"policy_id": default_period_policy.POLICY_OWNER, "fields": ["audience.period"]}
    ]


def test_a_user_stated_period_reports_a_plain_resolved_interpretation() -> None:
    """대조군: 사용자가 기간을 말했으면 정책 표식 없이 그냥 resolved 다."""

    structured = _windowed_plan(
        SPECIFIED_QUERY,
        contact_evidence="최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상",
        days=90,
    )
    plan = graph_rag.build_query_plan(
        SPECIFIED_QUERY, parser="llm", query_plan_v4=structured
    )
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        SPECIFIED_QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=SPECIFIED_QUERY,
    )
    api_response = graph_rag.build_recommendation_api_response(
        SPECIFIED_QUERY, plan, result, {}
    )

    assert api_response["interpretation_status"] == "resolved"
    assert api_response["semantic_ir"]["policy_applications"] == []


def test_an_applied_default_is_distinguishable_from_a_user_stated_period() -> None:
    """기본값이 적용되면 그 출처가 결과에 남는다 — 없으면 사용자의 말과 구분되지 않는다."""

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _windowed_plan(
            BARE_RECENT_QUERY, contact_evidence=CONTACT_EVIDENCE, days=5
        ),
    )

    receipt = applied[default_period_policy.DEFAULT_PERIOD_KEY]
    assert receipt["source"] == default_period_policy.POLICY_SOURCE
    assert (receipt["value"], receipt["unit"]) == (5, "day")
    assert receipt["window"] == {"type": "rolling", "value": 5, "unit": "day"}
    assert [span["text"] for span in receipt["evidence"]] == ["최근"]

    # 그 표식은 플랜까지 살아 나가고 감사 로그에도 사유와 함께 남는다.
    plan = graph_rag.build_query_plan(
        BARE_RECENT_QUERY, parser="llm", query_plan_v4=applied
    )
    assert plan[default_period_policy.DEFAULT_PERIOD_KEY]["source"] == (
        default_period_policy.POLICY_SOURCE
    )
    assert plan["semantic_ir"]["status"] == "policy_applied"
    assert plan["semantic_ir"]["policy_applications"] == [
        {"policy_id": default_period_policy.POLICY_OWNER, "fields": ["audience.period"]}
    ]
    owners = [
        entry["filter"]
        for entry in plan_decisions.decisions(plan)
        if entry.get("slot") == "audience_requirement.window"
    ]
    assert default_period_policy.POLICY_OWNER in owners


def test_a_plan_without_the_policy_carries_no_default_receipt() -> None:
    """사용자가 기간을 말한 플랜에는 기본값 표식이 붙지 않는다(구분의 반대편)."""

    structured = _windowed_plan(
        SPECIFIED_QUERY,
        contact_evidence="최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상",
        days=90,
    )
    plan = graph_rag.build_query_plan(
        SPECIFIED_QUERY, parser="llm", query_plan_v4=structured
    )

    assert default_period_policy.DEFAULT_PERIOD_KEY not in plan
    assert not [
        entry
        for entry in plan_decisions.decisions(plan)
        if entry.get("filter") == default_period_policy.POLICY_OWNER
    ]


# ── 불변식: 사용자가 명시한 기간이 언제나 이긴다 ───────────────────────────────────


def test_a_stated_period_never_reaches_the_policy() -> None:
    """기간 결핍이 없으면 정책은 아예 돌지 않는다(구조화기를 다시 부르지 않는다)."""

    def restructure(instruction: str) -> dict[str, Any]:
        raise AssertionError(f"명시 기간이 있는데 재구조화가 돌았다: {instruction}")

    structured = _windowed_plan(
        SPECIFIED_QUERY,
        contact_evidence="최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상",
        days=90,
    )
    applied = default_period_policy.apply_default_period(
        structured,
        query=SPECIFIED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=restructure,
    )

    assert applied is structured
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied


def test_a_candidate_that_overwrote_a_stated_period_is_rejected() -> None:
    """재구조화가 사용자의 90일을 기본 5일로 덮으면 채택하지 않는다.

    한 문장에 명시 기간과 맨 '최근'이 함께 있을 수 있다. 그때 기본값이 명시값을 삼키면 조건이
    조용히 뒤바뀌므로, 원문이 말한 기간을 잃은 후보는 버리고 원래 결핍을 지킨다.
    """

    blocked = _structured(
        SPECIFIED_QUERY,
        _raw(issues=[_missing_period_issue(SPECIFIED_QUERY)]),
    )
    overwriting = _windowed_plan(
        SPECIFIED_QUERY,
        contact_evidence="최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상",
        days=5,
    )

    applied = default_period_policy.apply_default_period(
        blocked,
        query=SPECIFIED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: overwriting,
    )

    assert applied is blocked
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied
    assert applied["semantic_ir"]["status"] == "needs_clarification"


def test_a_candidate_that_ignored_the_default_is_rejected() -> None:
    """지시한 기본값이 아닌 다른 기간을 지어낸 후보도 채택하지 않는다."""

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _windowed_plan(
            BARE_RECENT_QUERY, contact_evidence=CONTACT_EVIDENCE, days=30
        ),
    )

    assert applied[AUDIENCE_REQUIREMENT_KEY]["expression"] is None
    assert applied["semantic_ir"]["status"] == "needs_clarification"


def test_a_candidate_that_still_reports_the_gap_is_rejected() -> None:
    """재구조화가 여전히 되물으면 그 되묻기가 결말이다(정책이 그것을 덮지 않는다)."""

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _bare_recent_plan(),
    )

    assert applied[AUDIENCE_REQUIREMENT_KEY]["expression"] is None
    assert default_period_policy.missing_period_issues(applied)


def test_an_unconfigured_policy_leaves_the_clarification_alone() -> None:
    """설정이 없으면 코어 fail-close 가 그대로 결말이다."""

    def restructure(instruction: str) -> dict[str, Any]:
        raise AssertionError(f"정책이 꺼져 있는데 재구조화가 돌았다: {instruction}")

    blocked = _bare_recent_plan()
    applied = default_period_policy.apply_default_period(
        blocked,
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=None,
        restructure=restructure,
    )

    assert applied is blocked
    assert applied["semantic_ir"]["status"] == "needs_clarification"
