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
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import default_period_policy  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import plan_decisions  # noqa: E402
import targeting_policy  # noqa: E402
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
SPECIFIED_CONTACT_EVIDENCE = "최근 90일 동안 캠페인 발송 성공 횟수가 3회 이상"
# 한 문장에 두 갈래가 함께 있다 — 앞의 '최근'은 '3개월'이 수량화했고 뒤의 '최근'은 맨 표지다.
MIXED_QUERY = (
    "최근 3개월 캠페인 발송 성공 횟수가 3회 이상이고 최근 구매반응이 없는 회원을 대상으로 "
    "재반응 유도 캠페인을 만들어줘."
)
MIXED_CONTACT_EVIDENCE = "최근 3개월 캠페인 발송 성공 횟수가 3회 이상"
MIXED_BARE_EVIDENCE = "최근 구매반응이 없는"
DEFAULT_PERIOD = default_period_policy.DefaultPeriod(value=5, unit="day", origin="test")

# 애플리케이션이 구조화기에 넘기는 두 지시문의 제목. 어느 쪽이 나갔는지가 곧 이 라운드가
# 무엇을 요구했는지다(기본 창을 넣어라 / 원문이 말한 값을 제자리에 넣어라).
DEFAULT_PERIOD_BLOCK = "[Application-owned Default Period Policy]"
STATED_PERIOD_BLOCK = "[Application-owned Stated Period Correction]"


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _campaign_audience_expression(
    query: str,
    *,
    contact_evidence: str,
    window: event_ir.TimeWindow | None,
    response_window: event_ir.TimeWindow | None = None,
    absence_evidence: str = "구매반응이 없는",
) -> event_ir.Condition:
    """모델이 내는 모양: 접촉 성공 건수 임계 + 구매반응 부재.

    ``response_window`` 는 구매반응 절에도 창이 붙는 문장('… 최근 구매반응이 없는')용이다 —
    창이 둘인 후보에서 어느 창이 어느 절의 것인지가 채택 판정의 재료가 된다. 그 문장에서는
    부재 원자의 근거 구간도 표지('최근')를 덮어야 한다. 덮지 않으면 그 표지를 소유한 원자가
    없어서 애플리케이션이 같은 결핍을 다시 신고한다
    (``test_a_model_window_guess_is_still_rejected_by_the_validators`` 와 같은 규칙).
    """

    contact_relation: event_ir.Relation = event_ir.Source(name="campaign_contact_success")
    if window is not None:
        contact_relation = event_ir.Filter(
            relation=contact_relation,
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="campaign_contact_success.occurred_at"),
                window=window,
            ),
        )
    response_relation: event_ir.Relation = event_ir.Source(name="campaign_purchase_response")
    if response_window is not None:
        response_relation = event_ir.Filter(
            relation=response_relation,
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="campaign_purchase_response.occurred_at"),
                window=response_window,
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
                    relation=response_relation,
                    evidence=_evidence(query, absence_evidence),
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


def _period_issue_for(query: str, evidence_text: str) -> dict[str, Any]:
    """모델이 **그 자리**를 지목한 기간 결핍 신고(근거 구간은 원문 좌표계다).

    ``_missing_period_issue`` 는 맨 '최근'[0,2] 하나만 만든다. 거짓 결핍/진짜 결핍이 한 문장에
    같이 있는 입력에서는 어느 구간을 지목했는지가 판정을 가르므로 좌표를 원문에서 얻는다.
    """

    span = _evidence(query, evidence_text)
    return {
        "code": "missing_argument",
        "argument": "period",
        "message": f"The term '{evidence_text}' requires a specific period.",
        "evidence": {"text": span.text, "start": span.start, "end": span.end},
    }


def _policy_log_sink(events: list[dict[str, Any]]) -> Callable[[str, dict[str, Any]], None]:
    """정책 로그만 모으는 write_log — 무엇이 왜 채택됐는지는 로그만 보고도 알아야 한다."""

    def write_log(event: str, payload: dict[str, Any]) -> None:
        if event == "audience_default_period_policy":
            events.append(payload)

    return write_log


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


# ── 거짓 결핍: 원문이 이미 말한 기간에는 기본값을 지시하지도 요구하지도 않는다 ─────
#
# 실측(2026-08-07) `최근 30일 구매한 회원 수를 알려줘`: 모델이 '최근' 만 근거로 기간 결핍을
# 신고했고, 이 정책의 재구조화가 rolling 30 day 로 **정확히** 해결했는데 기본 창(5일)이 없다는
# 이유로 폐기돼 되묻기로 되돌아갔다. 신고가 거짓인 라운드는 다른 계약을 따른다:
#   * 지시문 — 기본 창이 아니라 `targeting_policy` 의 명시 기간 교정을 넘긴다.
#   * 채택   — 기본 창 포함을 요구하지 않는다(나머지 검사는 그대로다).
#   * 영수증 — 남기지 않는다. 그 값의 출처는 제품 설정이 아니라 사용자다.
# 갈래를 가르는 판정자는 `temporal_clause.stated_period_for_issue` 하나다.


def test_a_candidate_that_resolved_the_stated_period_is_adopted_without_the_default() -> None:
    """원문이 말한 90일로 해결한 후보는 기본 창이 없어도 채택된다(실측 결함의 본체)."""

    blocked = _structured(
        SPECIFIED_QUERY, _raw(issues=[_missing_period_issue(SPECIFIED_QUERY)])
    )
    resolved = _windowed_plan(
        SPECIFIED_QUERY, contact_evidence=SPECIFIED_CONTACT_EVIDENCE, days=90
    )
    calls: list[str] = []
    events: list[dict[str, Any]] = []

    def restructure(instruction: str) -> dict[str, Any]:
        calls.append(instruction)
        return resolved

    applied = default_period_policy.apply_default_period(
        blocked,
        query=SPECIFIED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=restructure,
        write_log=_policy_log_sink(events),
    )

    # 지시문은 '사용자가 말한 값을 제자리에 넣어라'다 — 기본 창을 넣으라고 말하지 않는다.
    assert len(calls) == 1
    assert STATED_PERIOD_BLOCK in calls[0]
    assert DEFAULT_PERIOD_BLOCK not in calls[0]
    assert '"value": 90' in calls[0]
    assert '"value": 5' not in calls[0]

    assert applied is resolved
    # 값의 출처가 사용자이므로 기본값 영수증도, policy_applied 도 붙지 않는다.
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied
    assert applied["semantic_ir"]["status"] == "resolved"
    assert applied["semantic_ir"]["policy_applications"] == []

    # 로그만 보고도 무엇이 왜 채택됐는지 알 수 있어야 한다.
    assert len(events) == 1
    assert events[0]["applied"] is False
    assert events[0]["adopted"] is True
    assert events[0]["reason"] is None
    assert events[0]["stated_period_issues"] == 1


def test_a_stated_period_round_that_lost_the_period_keeps_the_clarification() -> None:
    """같은 라운드에서 후보가 90일을 5일로 덮으면 채택하지 않고 원본을 지킨다.

    ``test_a_candidate_that_overwrote_a_stated_period_is_rejected`` 가 결말을 고정하고,
    여기서는 그 거절이 로그에서 **거짓 결핍 라운드의 거절**로 읽히는지를 고정한다.
    거절 사유 코드까지 고정하지는 않는다 — 90일 절을 덮은 후보는 정책의 보존 검사보다
    플랜 신원 부착 단계의 검증기가 먼저 잡을 수 있고, 어느 쪽이 잡든 결말은 같다.
    """

    blocked = _structured(
        SPECIFIED_QUERY, _raw(issues=[_missing_period_issue(SPECIFIED_QUERY)])
    )
    events: list[dict[str, Any]] = []

    applied = default_period_policy.apply_default_period(
        blocked,
        query=SPECIFIED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _windowed_plan(
            SPECIFIED_QUERY, contact_evidence=SPECIFIED_CONTACT_EVIDENCE, days=5
        ),
        write_log=_policy_log_sink(events),
    )

    assert applied is blocked
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied
    assert applied["semantic_ir"]["status"] == "needs_clarification"
    assert events[0]["adopted"] is False
    assert events[0]["applied"] is False
    assert events[0]["reason"] is not None
    assert (90, "day") in events[0]["stated_periods"]
    assert events[0]["stated_period_issues"] == 1


def test_a_false_gap_round_rejects_a_candidate_that_lost_the_stated_period() -> None:
    """정책 자신의 보존 검사도 안전망 없이 성립한다 — 실측 프롬프트 그대로.

    위 테스트의 후보는 플랜 신원 부착 단계의 검증기가 먼저 잡는다. 그 안전망이 없더라도
    기본값 라운드가 사용자의 30일을 5일로 바꾼 결과를 채택하면 안 되므로, 정책 계층이
    매핑만 보고 내리는 판정을 따로 고정한다(정책은 플랜 타입 전체를 요구하지 않는다).
    """

    query = "최근 30일 구매한 회원 수를 알려줘"
    blocked: dict[str, Any] = {
        AUDIENCE_REQUIREMENT_KEY: {
            "expression": None,
            "issues": [_period_issue_for(query, "최근")],
        }
    }
    events: list[dict[str, Any]] = []

    applied = default_period_policy.apply_default_period(
        blocked,  # type: ignore[arg-type]
        query=query,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: {
            AUDIENCE_REQUIREMENT_KEY: {
                "expression": {"window": {"type": "rolling", "value": 5, "unit": "day"}},
                "issues": [],
            }
        },
        write_log=_policy_log_sink(events),
    )

    assert applied is blocked
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied
    assert str(events[0]["reason"]).startswith("candidate_dropped_stated_period")


def test_the_measured_prompt_keeps_its_own_thirty_day_window() -> None:
    """실측 실패 프롬프트: 30일로 해결한 후보가 기본 창(5일) 없이 채택된다."""

    query = "최근 30일 구매한 회원 수를 알려줘"
    blocked: dict[str, Any] = {
        AUDIENCE_REQUIREMENT_KEY: {
            "expression": None,
            "issues": [_period_issue_for(query, "최근")],
        }
    }
    resolved: dict[str, Any] = {
        AUDIENCE_REQUIREMENT_KEY: {
            "expression": {"window": {"type": "rolling", "value": 30, "unit": "day"}},
            "issues": [],
        }
    }
    events: list[dict[str, Any]] = []

    applied = default_period_policy.apply_default_period(
        blocked,  # type: ignore[arg-type]
        query=query,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: resolved,
        write_log=_policy_log_sink(events),
    )

    assert applied is resolved
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied
    assert "semantic_ir" not in applied, "사용자가 말한 기간을 정책의 판정으로 덮었다"
    assert (events[0]["applied"], events[0]["adopted"]) == (False, True)


def test_an_unconfigured_policy_leaves_a_false_gap_alone() -> None:
    """설정이 없으면 거짓 결핍이어도 이 모듈은 아무 것도 하지 않는다.

    거짓 결핍 교정의 1차 소유자는 앞 단계(``targeting_policy.resolve_stated_period``)다.
    이 모듈이 정책 없이도 재구조화를 부르기 시작하면 꺼진 배포의 예산과 결말이 달라진다.
    """

    def restructure(instruction: str) -> dict[str, Any]:
        raise AssertionError(f"정책이 꺼져 있는데 재구조화가 돌았다: {instruction}")

    blocked = _structured(
        SPECIFIED_QUERY, _raw(issues=[_missing_period_issue(SPECIFIED_QUERY)])
    )
    applied = default_period_policy.apply_default_period(
        blocked,
        query=SPECIFIED_QUERY,
        current_date=CURRENT_DATE,
        period=None,
        restructure=restructure,
    )

    assert applied is blocked
    assert applied["semantic_ir"]["status"] == "needs_clarification"


def test_a_genuine_gap_still_demands_the_default_window() -> None:
    """맨 '최근'(진짜 결핍)의 계약은 그대로다 — 기본 창을 지시하고, 없으면 거절한다."""

    calls: list[str] = []
    events: list[dict[str, Any]] = []

    def restructure(instruction: str) -> dict[str, Any]:
        calls.append(instruction)
        return _windowed_plan(
            BARE_RECENT_QUERY, contact_evidence=CONTACT_EVIDENCE, days=30
        )

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=restructure,
        write_log=_policy_log_sink(events),
    )

    assert calls and DEFAULT_PERIOD_BLOCK in calls[0]
    assert STATED_PERIOD_BLOCK not in calls[0]
    assert applied[AUDIENCE_REQUIREMENT_KEY]["expression"] is None
    assert applied["semantic_ir"]["status"] == "needs_clarification"
    assert events[0]["reason"] == "candidate_ignored_the_default_window"
    assert events[0]["adopted"] is False
    assert events[0]["stated_period_issues"] == 0


# ── 혼합: 한 문장에 거짓 결핍과 진짜 결핍이 함께 있다 ─────────────────────────────


def _mixed_blocked_plan() -> dict[str, Any]:
    """두 결핍이 함께 신고된 플랜('최근 3개월' 쪽은 거짓, '최근 구매반응' 쪽은 진짜)."""

    return _structured(
        MIXED_QUERY,
        _raw(
            issues=[
                _period_issue_for(MIXED_QUERY, "최근"),
                _period_issue_for(MIXED_QUERY, MIXED_BARE_EVIDENCE),
            ]
        ),
    )


def _mixed_candidate(*, contact_days: int | None, response_days: int) -> dict[str, Any]:
    """재구조화 후보: 접촉 절은 ``contact_days``(None 이면 창 없음), 부재 절은 기본 창."""

    return _structured(
        MIXED_QUERY,
        _raw(
            expression=_campaign_audience_expression(
                MIXED_QUERY,
                contact_evidence=MIXED_CONTACT_EVIDENCE,
                window=(
                    None
                    if contact_days is None
                    else event_ir.RollingWindow(value=contact_days, unit="month")
                ),
                response_window=event_ir.RollingWindow(value=response_days, unit="day"),
                absence_evidence=MIXED_BARE_EVIDENCE,
            ).to_dict()
        ),
    )


def test_a_mixed_query_defaults_only_the_unquantified_span() -> None:
    """기본 창 지시는 맨 표지 구간만 지목한다 — 수량화된 구간은 교정 블록이 가져간다."""

    calls: list[str] = []

    def restructure(instruction: str) -> dict[str, Any]:
        calls.append(instruction)
        return _mixed_candidate(contact_days=3, response_days=5)

    default_period_policy.apply_default_period(
        _mixed_blocked_plan(),
        query=MIXED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=restructure,
    )

    assert len(calls) == 1
    default_block, stated_block = calls[0].split(STATED_PERIOD_BLOCK)
    # 기본 창은 원문이 기간을 말하지 않은 구간에만 붙는다.
    assert DEFAULT_PERIOD_BLOCK in default_block
    assert f"'{MIXED_BARE_EVIDENCE}'" in default_block
    assert '"value": 5' in default_block
    assert "3개월" not in default_block
    # 수량화된 구간은 '사용자가 말한 값 그대로'를 지시받는다.
    assert "'3개월'" in stated_block
    # 단위는 **툴 스키마가 받는 표기**여야 한다. 이 단언은 예전에 리터럴 추출기의 복수형
    # ('months')을 고정하고 있었는데, 그 값은 구조화 툴 스키마의 rolling.unit enum
    # (event_ir.WINDOW_UNITS = day|week|month|year)에 없다 — 지시문을 그대로 따르는 모델이
    # 반드시 스키마를 어기게 되는 값이었다(실측 로그에서 모델이 'days' 를 그대로 복사한 응답이
    # 교정 라운드를 실패시켰다). 이 파일의 다른 단언은 그대로 두고 이 표기만 바로잡는다.
    assert '"value": 3' in stated_block and '"unit": "month"' in stated_block


def test_a_mixed_round_requires_the_default_and_the_stated_period_together() -> None:
    """혼합 문장의 채택 조건은 둘 다다 — 기본 창이 있고, 명시 기간을 잃지 않았을 것."""

    events: list[dict[str, Any]] = []
    blocked = _mixed_blocked_plan()

    dropped = default_period_policy.apply_default_period(
        blocked,
        query=MIXED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _mixed_candidate(
            contact_days=None, response_days=5
        ),
        write_log=_policy_log_sink(events),
    )

    assert dropped is blocked, "명시 기간(3개월)을 잃은 후보가 채택됐다"
    assert events[0]["adopted"] is False
    assert (3, "month") in events[0]["stated_periods"]

    both = _mixed_candidate(contact_days=3, response_days=5)
    applied = default_period_policy.apply_default_period(
        _mixed_blocked_plan(),
        query=MIXED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: both,
        write_log=_policy_log_sink(events),
    )

    assert applied is both
    assert events[-1]["applied"] is True
    assert events[-1]["stated_period_issues"] == 1
    # 영수증은 기본값이 **실제로 채운** 구간만 인용한다.
    receipt = applied[default_period_policy.DEFAULT_PERIOD_KEY]
    assert [span["text"] for span in receipt["evidence"]] == [MIXED_BARE_EVIDENCE]
    assert applied["semantic_ir"]["status"] == "policy_applied"


# ── 채택된 플랜에도 정책 영수증이 남는다 ─────────────────────────────────────────
# 거짓 결핍 라운드는 플랜을 **바꿔서** 채택한다(다른 객체를 돌려준다). 그런데 앞 단계
# (`targeting_policy.resolve_stated_period`)가 교정 실패를 적어 둔 영수증은 버려지는 플랜에
# 붙어 있으므로 함께 사라진다. 그 결과 배송된 플랜에는 "기간을 무엇으로 어떻게 확정했는가"의
# 기록이 하나도 남지 않는다 — 감사 원장(`plan_decisions`/`targeting_policy.decisions`)을 읽는
# 쪽에서 이 라운드는 일어나지 않은 일이 된다.


def test_the_false_gap_adoption_leaves_a_policy_receipt() -> None:
    """성공 경로에도 영수증이 있어야 한다 — 같은 교정이 어느 단계에서 성공했든 기록은 같다."""

    blocked = _structured(
        SPECIFIED_QUERY, _raw(issues=[_missing_period_issue(SPECIFIED_QUERY)])
    )
    resolved = _windowed_plan(
        SPECIFIED_QUERY, contact_evidence=SPECIFIED_CONTACT_EVIDENCE, days=90
    )

    applied = default_period_policy.apply_default_period(
        blocked,
        query=SPECIFIED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: resolved,
    )

    assert applied is resolved
    receipts = targeting_policy.decisions(applied)
    period_receipts = [
        item for item in receipts if item["policy_id"] == targeting_policy.PERIOD_POLICY_ID
    ]
    assert period_receipts, "채택된 플랜에 기간 정책 결정이 하나도 남지 않았다."
    assert period_receipts[-1]["decision"] == targeting_policy.DECISION_ALLOW
    assert period_receipts[-1]["reason_code"] == targeting_policy.REASON_PERIOD_STATED
    # 값의 출처가 사용자라는 사실이 영수증에서도 읽혀야 한다.
    assert "period_stated=true" in period_receipts[-1]["input_facts"]


# ── 두 지시문이 같은 표면어를 놓고 다투지 않는다 ──────────────────────────────────
# 혼합 라운드는 기본 창 지시문과 명시 기간 교정문을 **함께** 넘긴다. 두 결핍의 근거 텍스트가
# 같으면(가장 흔한 '최근') 두 블록이 같은 문자열을 지목하면서 서로 다른 창을 요구하게 되고,
# 모델에게는 어느 '최근'이 어느 쪽인지 가릴 재료가 없다. 근거 구간은 원문 좌표계의 값이므로
# 좌표를 함께 인용하면 같은 표면어도 서로 구분된다.


def test_two_instruction_blocks_never_address_the_same_span_ambiguously() -> None:
    """같은 표면어를 쓰는 혼합 문장에서 두 블록이 서로 다른 구간을 지목한다."""

    first = MIXED_QUERY.index("최근")
    second = MIXED_QUERY.index("최근", first + 1)
    plan = _structured(
        MIXED_QUERY,
        _raw(
            issues=[
                {
                    "code": "missing_argument",
                    "argument": "period",
                    "message": "?",
                    "evidence": {"text": "최근", "start": first, "end": first + 2},
                },
                {
                    "code": "missing_argument",
                    "argument": "period",
                    "message": "?",
                    "evidence": {"text": "최근", "start": second, "end": second + 2},
                },
            ]
        ),
    )
    calls: list[str] = []

    default_period_policy.apply_default_period(
        plan,
        query=MIXED_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda instruction: (calls.append(instruction), plan)[1],
    )

    assert len(calls) == 1
    default_block, stated_block = calls[0].split(STATED_PERIOD_BLOCK)
    # 두 블록이 각자 자기 구간의 좌표를 인용한다 — 표면어만으로는 구분되지 않기 때문이다.
    assert f"[{second}, {second + 2})" in default_block, default_block
    assert f"[{first}, {first + 2})" in stated_block, stated_block
    assert f"[{first}, {first + 2})" not in default_block
    assert f"[{second}, {second + 2})" not in stated_block


# ── 달력·절대 창으로 해결된 라운드 ────────────────────────────────────────────────
# '지난달 …' 은 기간을 **확정해** 말한 문장이다. 그 신고가 거짓 결핍으로 갈리지 않으면 배포
# 기본 창(5일)이 사용자의 달력 창을 덮고, 갈리더라도 채택 검사가 롤링 창만 비교하면 달력 창으로
# 정확히 해결한 후보가 '명시 기간을 잃었다'로 폐기된다. 두 자리를 함께 고정한다.

CALENDAR_QUERY = (
    "지난달 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 "
    "재반응 유도 캠페인을 만들어줘."
)
CALENDAR_CONTACT_EVIDENCE = "지난달 캠페인 발송 성공 횟수가 3회 이상"
# CURRENT_DATE(2026-08-02) 기준 '지난달' = 2026년 7월.
CALENDAR_INTERVAL = event_ir.AbsoluteInterval(
    start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)
)


def _calendar_blocked_plan() -> dict[str, Any]:
    """모델이 '지난달'을 지목해 기간 결핍을 신고한 상태(거짓 결핍)."""

    return _structured(
        CALENDAR_QUERY,
        _raw(issues=[_period_issue_for(CALENDAR_QUERY, "지난달")]),
    )


def _calendar_candidate(window: event_ir.TimeWindow) -> dict[str, Any]:
    return _structured(
        CALENDAR_QUERY,
        _raw(
            expression=_campaign_audience_expression(
                CALENDAR_QUERY,
                contact_evidence=CALENDAR_CONTACT_EVIDENCE,
                window=window,
            ).to_dict()
        ),
    )


def test_a_stated_calendar_period_is_not_overwritten_by_the_default_window() -> None:
    """달력 창으로 해결된 후보가 채택된다 — 기본 창을 요구하지도, 영수증을 남기지도 않는다."""

    calls: list[str] = []
    resolved = _calendar_candidate(CALENDAR_INTERVAL)

    applied = default_period_policy.apply_default_period(
        _calendar_blocked_plan(),
        query=CALENDAR_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda instruction: (calls.append(instruction), resolved)[1],
    )

    assert len(calls) == 1
    # 이 라운드가 요구한 것은 '원문이 말한 구간을 제자리에'뿐이다.
    assert STATED_PERIOD_BLOCK in calls[0]
    assert DEFAULT_PERIOD_BLOCK not in calls[0]
    assert '"type": "interval"' in calls[0]

    assert applied is resolved
    # 이 창의 출처는 제품 설정이 아니라 사용자다 — 기본값 영수증이 붙으면 안 된다.
    assert default_period_policy.DEFAULT_PERIOD_KEY not in applied
    receipts = [
        item
        for item in targeting_policy.decisions(applied)
        if item["policy_id"] == targeting_policy.PERIOD_POLICY_ID
    ]
    assert receipts[-1]["reason_code"] == targeting_policy.REASON_PERIOD_STATED
    assert receipts[-1]["detail"]["intervals"] == [
        {"start": "2026-07-01", "end_exclusive": "2026-08-01"}
    ]


def test_a_candidate_that_drops_the_stated_calendar_period_is_rejected() -> None:
    """지시는 '그 구간을 그대로'였다 — 다른 창으로 바꿔 온 결과는 원래 결핍을 지킨다.

    거부가 어느 검사에서 나오는지는 고정하지 않는다. 창을 바꾼 후보는 검증기가 같은 결핍을
    다시 신고하기도 하고(``candidate_reports_other_issues``) 채택 검사가 잃어버린 창을 세기도
    한다 — 둘 다 사용자의 달력 창을 지킨다는 같은 결말이다.
    """

    events: list[dict[str, Any]] = []
    blocked = _calendar_blocked_plan()

    applied = default_period_policy.apply_default_period(
        blocked,
        query=CALENDAR_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda _instruction: _calendar_candidate(
            event_ir.RollingWindow(value=5, unit="day")
        ),
        write_log=_policy_log_sink(events),
    )

    assert applied is blocked
    assert events[-1]["adopted"] is False
    assert events[-1]["reason"]


def test_admit_accepts_a_round_resolved_only_by_a_calendar_window() -> None:
    """채택 검사가 롤링 창만 비교하면, 달력 창으로 정확히 해결한 후보가 폐기된다."""

    resolved = _calendar_candidate(CALENDAR_INTERVAL)

    assert (
        default_period_policy._admit(
            resolved,
            required_window=None,
            stated=set(),
            instructed_windows=frozenset({("interval", "2026-07-01", "2026-08-01")}),
        )
        is None
    )


def test_admit_still_counts_a_lost_calendar_window() -> None:
    """일반화가 검사를 무르게 하지는 않는다 — 지시한 구간이 없으면 잃은 것이다."""

    resolved = _calendar_candidate(CALENDAR_INTERVAL)

    assert default_period_policy._admit(
        resolved,
        required_window=None,
        stated=set(),
        instructed_windows=frozenset({("interval", "2026-06-01", "2026-07-01")}),
    ) == "candidate_dropped_stated_period:['2026-06-01~2026-07-01']"


def test_a_bare_marker_without_a_calendar_token_still_takes_the_default() -> None:
    """달력 창을 읽게 됐다고 맨 '최근'이 해결되지는 않는다 — 진짜 결핍은 그대로다."""

    calls: list[str] = []

    applied = default_period_policy.apply_default_period(
        _bare_recent_plan(),
        query=BARE_RECENT_QUERY,
        current_date=CURRENT_DATE,
        period=DEFAULT_PERIOD,
        restructure=lambda instruction: (
            calls.append(instruction),
            _windowed_plan(BARE_RECENT_QUERY, contact_evidence=CONTACT_EVIDENCE, days=5),
        )[1],
    )

    assert len(calls) == 1
    assert DEFAULT_PERIOD_BLOCK in calls[0]
    assert STATED_PERIOD_BLOCK not in calls[0]
    assert applied[default_period_policy.DEFAULT_PERIOD_KEY]["source"] == (
        default_period_policy.POLICY_SOURCE
    )
