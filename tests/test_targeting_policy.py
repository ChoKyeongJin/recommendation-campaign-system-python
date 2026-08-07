"""정책은 한 곳에서 정해지고, 모든 결정은 영수증을 남긴다.

같은 입력이 실행마다 다른 귀결을 내던 원인은 정책이 흩어져 있고 버전이 없다는 것이었다
(2026-08-06 실측 #5·#16·#26). 여기서 그 계약을 고정한다.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest

import audience_schema
import default_period_policy
import targeting_policy
from query_structurer.semantic_ir import extract_literal_bindings

REQUIREMENT_KEY = "audience_requirement"
REFERENCE_DATE = "2026-08-06"


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


# ── 지시문이 제시하는 창은 **툴 스키마가 받는 값**이어야 한다 ─────────────────────
# 애플리케이션이 모델에게 "이 창을 넣어라"라고 말하면서 스키마가 거절하는 값을 제시하면, 지시를
# 지키는 모델이 반드시 계약을 어긴다. 실측 로그에서 정확히 그 일이 있었다 — 교정 지시문이
# 리터럴 추출기의 복수형 단위(``"unit": "days"``)를 제시했고, 구조화 툴 스키마의 rolling.unit
# enum 은 ``day|week|month|year`` 였다. 모델은 지시문의 ``days`` 를 그대로 복사했고 그 라운드가
# 실패했다. 그래서 이 검사는 스키마를 손으로 베끼지 않고 **실제 스키마 객체**에서 읽어 대조한다.

_SCHEMA = audience_schema.audience_expression_json_schema()
_WINDOW_SCHEMA: dict[str, Any] = {"$ref": "#/$defs/window", "$defs": _SCHEMA["$defs"]}
_WINDOW_TAGS = frozenset(
    _SCHEMA["$defs"][ref["$ref"].rsplit("/", 1)[-1]]["properties"]["type"]["enum"][0]
    for ref in _SCHEMA["$defs"]["window"]["anyOf"]
)

STATED_ROLLING_QUERIES = (
    "최근 30일 구매한 회원",
    "최근 2주 동안 구매한 회원",
    "최근 3개월 동안 구매한 회원",
    "최근 1년 동안 구매한 회원",
)
STATED_CALENDAR_QUERIES = (
    ("지난달 구매한 회원", "지난달"),
    ("2026년 3월에 구매한 회원", "2026년 3월"),
    ("작년 1월 가입한 회원", "작년 1월"),
    ("올해 상반기 구매한 회원", "올해 상반기"),
)


def _embedded_objects(text: str) -> list[dict[str, Any]]:
    """지시문 본문에 찍힌 JSON 객체 전부(중첩 포함).

    렌더러가 어떤 dict 를 실었는지는 **나온 문자열**로만 알 수 있다. 렌더러 내부 값을 다시
    불러 검사하면 '찍은 것'과 '검사한 것'이 갈라질 수 있다.
    """

    found: list[dict[str, Any]] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        found.append(value)
                    break
    return found


def _stated_clauses(query: str, marker: str) -> list[tuple[dict[str, object], object]]:
    issues = [_period_issue(query, marker)]
    bindings = extract_literal_bindings(query, current_date=REFERENCE_DATE)
    stated, unstated = targeting_policy.split_period_issues(query, issues, bindings)
    assert unstated == [], f"거짓 결핍으로 갈리지 않았다: {query}"
    return stated


def _assert_windows_are_schema_valid(instruction: str) -> int:
    windows = [
        value for value in _embedded_objects(instruction) if value.get("type") in _WINDOW_TAGS
    ]
    for window in windows:
        jsonschema.validate(window, _WINDOW_SCHEMA)
    return len(windows)


@pytest.mark.parametrize("query", STATED_ROLLING_QUERIES)
def test_a_stated_rolling_correction_presents_only_schema_valid_windows(query: str) -> None:
    instruction = targeting_policy.stated_period_instruction(_stated_clauses(query, "최근"))

    assert _assert_windows_are_schema_valid(instruction) == 1, instruction
    # 복수형 단위는 스키마 enum 에 없다 — 제시되면 안 된다(회귀 방지의 핵심 표면).
    assert '"unit": "days"' not in instruction
    assert '"unit": "months"' not in instruction


@pytest.mark.parametrize(("query", "token"), STATED_CALENDAR_QUERIES)
def test_a_stated_calendar_correction_presents_only_schema_valid_windows(
    query: str, token: str
) -> None:
    instruction = targeting_policy.stated_period_instruction(_stated_clauses(query, token))

    assert _assert_windows_are_schema_valid(instruction) == 1, instruction
    # 달력 구간을 rolling 길이로 바꿔 제시하면 사용자의 창이 조용히 다른 뜻이 된다.
    assert '"type": "interval"' in instruction
    assert '"type": "rolling"' not in instruction


def test_the_default_period_instruction_presents_only_schema_valid_windows() -> None:
    """같은 결함이 다른 렌더러에 없는지도 같은 잣대로 본다(창 dict 를 찍는 자리는 둘뿐이다)."""

    query = "최근 구매한 회원"
    instruction = default_period_policy.render_default_period_instruction(
        default_period_policy.DefaultPeriod(value=5, unit="day", origin="test"),
        [_period_issue(query, "최근")],
    )

    assert _assert_windows_are_schema_valid(instruction) == 1, instruction


# ── 달력 구간도 '원문이 말한 기간'으로 교정된다 ───────────────────────────────────


def test_a_stated_calendar_period_is_repaired_not_asked_about() -> None:
    """``지난달`` 은 확정된 구간이다 — 되물을 것이 아니라 그 구간을 제자리에 넣게 한다."""

    query = "지난달 구매한 회원 수를 알려줘"
    plan = _plan(query, "지난달")
    calls: list[str] = []
    interval = {"type": "interval", "start": "2026-07-01", "end_exclusive": "2026-08-01"}

    def restructure(instruction: str) -> dict[str, object]:
        calls.append(instruction)
        return _repaired(dict(interval))

    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date=REFERENCE_DATE,
        restructure=restructure,
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )

    assert calls and json.dumps(interval) in calls[0]
    assert not _missing(result)
    decision = targeting_policy.decisions(result)[0]
    assert decision["reason_code"] == targeting_policy.REASON_PERIOD_STATED
    assert decision["detail"]["intervals"] == [
        {"start": "2026-07-01", "end_exclusive": "2026-08-01"}
    ]


def test_a_repair_that_replaces_the_calendar_period_is_rejected() -> None:
    """달력 구간을 롤링 길이로 바꿔 온 결과는 채택하지 않는다 — 지시는 '그대로 복사'였다."""

    query = "지난달 구매한 회원 수를 알려줘"
    plan = _plan(query, "지난달")
    result = targeting_policy.resolve_stated_period(
        plan,
        query=query,
        current_date=REFERENCE_DATE,
        restructure=lambda _instruction: _repaired(
            {"type": "rolling", "value": 30, "unit": "day"}
        ),
        missing_period_issues=_missing,
        requirement_key=REQUIREMENT_KEY,
    )

    assert result is plan
    reasons = [item["reason_code"] for item in targeting_policy.decisions(result)]
    assert targeting_policy.REASON_PERIOD_REPAIR_FAILED in reasons


def test_a_calendar_token_before_the_marker_stays_a_real_gap() -> None:
    """어순 규칙은 정책 계층에서도 같다 — 앞 절의 달력 창이 뒤의 맨 표지를 해결하지 않는다."""

    query = "2026년 3월 구매 이력이 있고 최근 가입한 회원"
    marker_start = query.rindex("최근")
    issue = {
        "code": "missing_argument",
        "argument": "period",
        "message": "no duration",
        "evidence": {"text": "최근", "start": marker_start, "end": marker_start + 2},
    }
    stated, unstated = targeting_policy.split_period_issues(
        query, [issue], extract_literal_bindings(query, current_date=REFERENCE_DATE)
    )

    assert stated == []
    assert [item["evidence"]["start"] for item in unstated] == [marker_start]


def test_split_period_issues_consumes_the_bindings_only_once() -> None:
    """``literal_bindings`` 는 Iterable 계약이다 — 결핍마다 다시 읽으므로 한 번만 소비한다.

    제너레이터가 들어오면 두 번째 결핍부터 빈 목록을 보게 되고, 그러면 원문이 말한 기간이
    **결핍 순서**에 따라 사라진다(맨 표지가 먼저 오면 뒤의 수량화된 절이 진짜 결핍으로 분류돼
    배포 기본값에 덮인다). 순서에 둔감한 검사로는 이 결함이 드러나지 않는다.
    """

    from query_structurer.semantic_ir import extract_literal_bindings

    query = "최근 3개월 캠페인 발송 성공 횟수가 3회 이상이고 최근 구매반응이 없는 회원"
    first = query.index("최근")
    second = query.index("최근", first + 1)
    bindings = list(extract_literal_bindings(query, current_date="2026-08-04"))
    issues = [
        {
            "code": "missing_argument", "argument": "period", "message": "?",
            "evidence": {"text": "최근", "start": second, "end": second + 2},
        },
        {
            "code": "missing_argument", "argument": "period", "message": "?",
            "evidence": {"text": "최근", "start": first, "end": first + 2},
        },
    ]

    from_list = targeting_policy.split_period_issues(query, issues, list(bindings))
    from_iterator = targeting_policy.split_period_issues(query, issues, iter(bindings))

    assert len(from_list[0]) == 1 and len(from_list[1]) == 1
    assert [len(part) for part in from_iterator] == [len(part) for part in from_list]
