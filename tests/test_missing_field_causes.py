"""결핍 판정은 리터럴 색인과 대조한다 — 원문에 있는 값을 되묻지 않는다 (P3).

실측된 최악의 형태(코퍼스 #3 `누적 구매금액 상위 10% 회원을 추출해줘`):

    literal_bindings = [{"id":"percentage_1","kind":"percentage","text":"10%","value":10,
                         "normalized":{"unit":"percent","value":10}}]
    semantic_ir.missing_fields = ["audience.percentage"]
    → 사용자에게 "몇 퍼센트인가요?" 되묻기

**시스템이 이미 결정론으로 추출해 정규화까지 마친 값을 사용자에게 되묻는다.**
원인은 `missing_field_causes` 가 라이브 경로에서 구조적으로 항상 `[]` 였기 때문이다 —
아키텍처가 정의한 두 원인축(`user_omission`=물어봐야 함 / `model_omission`=재방출)이
채워지지 않으니 모든 결핍이 '사용자에게 묻기'로 귀결됐다.

반대 방향도 함께 고정한다. 구간을 보지 않고 종류만 맞추면 **다른 절의 값** 때문에 진짜
결핍이 재방출로 새고, 사용자는 답할 기회를 잃는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import canonical_audience_claims  # noqa: E402
import semantic_outcome  # noqa: E402
from query_structurer import structurer  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402
from query_structurer.structurer import (  # noqa: E402
    LLMCampaignQueryPlanV4Structurer,
)
from query_structurer.types import (  # noqa: E402
    QueryStructuringInput,
    StructuringContext,
)


def _nth_index(query: str, span: str, occurrence: int) -> int:
    """``span`` 의 ``occurrence`` 번째 등장 위치.

    한 문장에 같은 표지가 둘 있으면(``최근 3개월 … 최근 구매``) 어느 쪽을 지목했는지가
    판정을 가른다 — 첫 번째는 기간을 말한 절의 표지이고 두 번째는 맨 표지다.
    """
    start = -1
    for _ in range(occurrence):
        start = query.index(span, start + 1)
    return start


def _plan_with_missing(query: str, argument: str, span: str, occurrence: int = 1) -> dict:
    start = _nth_index(query, span, occurrence)
    raw = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None, "offer_type": None, "channels": [], "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": "missing_argument",
                "argument": argument,
                "message": f"{argument} 값을 확정하지 못했습니다.",
                "evidence": {"text": span, "start": start, "end": start + len(span)},
            }],
        },
    }
    return attach_campaign_query_plan_v4_identity(raw, query, current_date="2026-08-03")


def _causes(plan: dict) -> list[dict]:
    return plan["semantic_ir"]["missing_field_causes"]


def test_live_path_actually_fills_the_cause_axis() -> None:
    """causes 가 비면 원인 축이 존재하지 않는 것과 같다 — 모든 결핍이 되묻기가 된다."""
    plan = _plan_with_missing("누적 구매금액 상위 10% 회원을 추출해줘", "percentage", "상위 10%")
    assert _causes(plan), "라이브 경로에서 missing_field_causes 가 여전히 비어 있다."


def test_extracted_percentage_is_not_asked_back() -> None:
    """P3 의 가드: 원문에 값이 있는데 그 값을 되묻는 응답은 red."""
    query = "누적 구매금액 상위 10% 회원을 추출해줘"
    plan = _plan_with_missing(query, "percentage", "상위 10%")

    extracted = [item for item in plan["literal_bindings"] if item["kind"] == "percentage"]
    assert extracted, "전제 확인: 추출기가 '10%' 를 이미 원자로 뽑는다."

    causes = _causes(plan)
    assert [record["cause"] for record in causes] == [semantic_outcome.CAUSE_MODEL_OMISSION], (
        f"추출된 값이 있는 결핍이 사용자 누락으로 분류됐다: {causes}"
    )
    assert plan["semantic_ir"]["failure_kind"] != "user_clarification", (
        "모델이 놓친 값을 사용자에게 묻고 있다 — 재방출로 고쳐야 할 결핍이다."
    )


def test_bare_recency_is_still_a_user_question() -> None:
    """되묻기를 줄이는 것이 목표가 아니다 — **틀린** 되묻기를 없애는 것이 목표다.

    맨 '최근'은 원문에 값이 정말 없다. 다른 절에 '3개월'이 있다고 재방출로 보내면
    재시도만 소모하고 사용자는 답할 기회를 잃는다(구간을 보지 않던 특례의 실제 오탐).

    2026-08-07 지목 구간을 **두 번째** '최근'으로 바로잡았다. 이 문장에는 '최근'이 둘인데
    헬퍼가 첫 번째를 집고 있었고, 그 자리는 원문이 ``최근 3개월`` 로 기간을 말한 절의
    표지다 — 맨 '최근'의 사례가 아니다. 두 표지가 각각 어떻게 갈리는지는
    :func:`test_each_recency_marker_is_judged_by_its_own_clause` 가 함께 고정한다.
    """
    query = "최근 3개월 내 주문한 적은 있지만 최근 구매가 없는 회원"
    plan = _plan_with_missing(query, "period", "최근", occurrence=2)

    causes = _causes(plan)
    assert [record["cause"] for record in causes] == [semantic_outcome.CAUSE_USER_OMISSION], (
        f"다른 절의 기간 때문에 진짜 결핍이 재방출로 샜다: {causes}"
    )
    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"


def test_placeholder_is_always_a_user_question() -> None:
    """'특정 브랜드'는 어떤 추출값으로도 못 채운다 — 오직 사용자만 값을 줄 수 있다."""
    plan = _plan_with_missing("특정 브랜드를 2회 이상 구매한 회원", "brand", "특정 브랜드")
    causes = _causes(plan)
    assert causes[0]["cause"] == semantic_outcome.CAUSE_USER_OMISSION
    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"


@pytest.mark.parametrize(
    ("query", "argument", "span", "expected"),
    [
        # 구간 안에 값이 있다 → 모델이 지목해 놓고 못 본 것이다.
        ("최근 30일 동안 구매한 회원", "period", "최근 30일", semantic_outcome.CAUSE_MODEL_OMISSION),
        # 구간 안에 값이 없다 → 사용자만 답할 수 있다.
        ("최근 구매한 회원", "period", "최근", semantic_outcome.CAUSE_USER_OMISSION),
    ],
)
def test_the_span_is_the_join_key(query: str, argument: str, span: str, expected: str) -> None:
    """인자 이름 → 리터럴 종류의 손 매핑 대신 **근거 구간**으로 잇는다.

    모델의 `argument` 는 닫힌 어휘가 아니므로 그런 표는 곧 낡는다.
    """
    plan = _plan_with_missing(query, argument, span)
    assert [record["cause"] for record in _causes(plan)] == [expected]


def test_reemission_trigger_reads_the_computed_cause() -> None:
    """재방출 트리거가 손코딩 특례가 아니라 계산된 원인을 읽는지 고정한다.

    예전 특례는 `argument == "period"` 와 date_window/duration 두 종류만 알았다 —
    그래서 percentage 는 재방출 대상이 아니었고, 그것이 #3 이 되묻기로 끝난 이유다.
    """
    query = "누적 구매금액 상위 10% 회원을 추출해줘"
    plan = _plan_with_missing(query, "percentage", "상위 10%")
    raw = {"audience_requirement": {
        "expression": None,
        "issues": plan["audience_requirement"]["issues"],
    }}

    repair = structurer._audience_repair_error(raw, plan)
    assert repair and "percentage" in repair, (
        f"percentage 결핍이 재방출로 라우팅되지 않는다: {repair!r}"
    )


def test_cause_records_are_pure_and_need_no_llm() -> None:
    """판정자는 순수 함수다 — 라이브 LLM 없이도 같은 답을 낸다."""
    query = "상위 20% 회원을 뽑아줘"
    issues = [{
        "code": "missing_argument", "argument": "percentage", "message": "?",
        "evidence": {"text": "상위 20%", "start": 0, "end": 6},
    }]
    bindings = [{"id": "percentage_1", "kind": "percentage", "start": 3, "end": 6}]
    records = canonical_audience_claims.missing_field_cause_records(query, issues, bindings)
    assert [record["cause"] for record in records] == [semantic_outcome.CAUSE_MODEL_OMISSION]

    without = canonical_audience_claims.missing_field_cause_records(query, issues, [])
    assert [record["cause"] for record in without] == [semantic_outcome.CAUSE_USER_OMISSION]


# ── 시간 절: 떨어진 구간 둘이 기간 하나다 ─────────────────────────────────────────
# 포함 관계는 구간 **하나**만 본다. 시간 표현은 그렇게 생기지 않았다 — '최근 30일' 은 표지와
# 수량이 서로 다른 구간이고, 모델은 표지만 지목하는 일이 잦다(실측 2026-08-06: 22회 중 4회).
# 그때 '30일' 은 신고 구간 밖이라 결핍으로 남았고, 첫 라운드가 재시도 없이 되묻기로 닫혔다.


def test_a_stated_period_is_recovered_in_the_first_round() -> None:
    """모델이 '최근'만 지목해도 원문이 말한 '30일'은 **첫 라운드에서** 살아난다."""

    query = "최근 30일 구매한 회원 수를 알려줘"
    plan = _plan_with_missing(query, "period", "최근")

    causes = _causes(plan)
    assert [record["cause"] for record in causes] == [semantic_outcome.CAUSE_MODEL_OMISSION], (
        f"원문이 말한 기간을 되묻고 있다: {causes}"
    )
    # 근거 구간은 리터럴 구간과 같은 좌표계·같은 모양이어야 한다(하류가 한 형태만 읽는다).
    assert causes[0]["literal_spans"] == [(0, 2), (3, 6)]
    assert query[3:6] == "30일"
    assert plan["semantic_ir"]["failure_kind"] == "structurer_failure"


def test_each_recency_marker_is_judged_by_its_own_clause() -> None:
    """한 문장의 표지 둘이 서로 다른 답을 받는다 — 판정은 **절 단위**다.

    앞의 '최근'은 ``최근 3개월`` 절의 표지라 원문이 기간을 말했고(재방출), 뒤의 '최근'은
    수량을 얻지 못한 별개의 절이라 사용자만 답할 수 있다(되묻기). 절 경계를 보지 않으면
    둘 중 하나는 반드시 틀린다.
    """
    query = "최근 3개월 내 주문한 적은 있지만 최근 구매가 없는 회원"

    stated = _plan_with_missing(query, "period", "최근", occurrence=1)
    assert [record["cause"] for record in _causes(stated)] == [
        semantic_outcome.CAUSE_MODEL_OMISSION
    ]
    assert _causes(stated)[0]["literal_spans"] == [(0, 2), (3, 6)]

    bare = _plan_with_missing(query, "period", "최근", occurrence=2)
    assert [record["cause"] for record in _causes(bare)] == [
        semantic_outcome.CAUSE_USER_OMISSION
    ]
    assert bare["semantic_ir"]["failure_kind"] == "user_clarification"


@pytest.mark.parametrize(
    ("occurrence", "expected"),
    [
        (1, semantic_outcome.CAUSE_MODEL_OMISSION),
        (2, semantic_outcome.CAUSE_USER_OMISSION),
    ],
)
def test_clause_judgement_does_not_depend_on_the_surrounding_verb(
    occurrence: int, expected: str
) -> None:
    """같은 규칙이 다른 동사·다른 소스(장바구니)에서도 그대로 성립한다.

    이 문장은 하류에서 결정론 빌더가 표현을 세워 plan 에 결핍이 남지 않는다 — 그래서 판정자
    자체를 직접 부른다. 규칙이 문장별 예외가 아니라는 증거는 이 층에서 서야 한다.
    """
    query = "최근 3개월 장바구니에 담고 최근 구매가 없는 회원"
    start = _nth_index(query, "최근", occurrence)
    issues = [{
        "code": "missing_argument", "argument": "period", "message": "?",
        "evidence": {"text": "최근", "start": start, "end": start + 2},
    }]
    bindings = extract_literal_bindings(query, current_date="2026-08-07")

    records = canonical_audience_claims.missing_field_cause_records(query, issues, bindings)
    assert [record["cause"] for record in records] == [expected]


@pytest.mark.parametrize(
    "query",
    [
        "최근 30일 구매한 회원",      # 일
        "최근 2주 동안 구매한 회원",   # 주
        "최근 3개월 동안 구매한 회원",  # 개월
        "최근 1년 동안 구매한 회원",   # 년
        "최근 1주일 동안 구매한 회원",  # 단어형 단위
        "최근 7일간 구매한 회원",      # 조사가 붙은 표면형
    ],
)
def test_the_rule_holds_for_every_stated_unit(query: str) -> None:
    """규칙은 문장이 아니라 문법이다 — 단위가 바뀌어도 분기가 늘지 않는다."""

    plan = _plan_with_missing(query, "period", "최근")
    assert [record["cause"] for record in _causes(plan)] == [
        semantic_outcome.CAUSE_MODEL_OMISSION
    ], query


@pytest.mark.parametrize(
    "query",
    [
        "최근 일주일 동안 구매한 회원",
        "최근 한 달 동안 구매한 회원",
        "최근 반년 동안 구매한 회원",
        "최근 보름 동안 구매한 회원",
        "최근 석달 동안 구매한 회원",
        "최근 한해 동안 구매한 회원",
    ],
)
def test_word_form_periods_are_owned_by_the_extractor(query: str) -> None:
    """숫자 없는 단어형 기간도 원문이 **말한** 기간이다 — 되묻지 않고 재방출한다.

    이 표면형이 추출기 원장에 없던 동안 여기서는 fail-close(되묻기)가 옳았다. 판정자가 둘이
    되지 않게 한 선택이었고, 남은 위험은 '사용자는 기간을 말했는데 되묻는다'였다. 그 위험은
    판정자를 늘려서가 아니라 추출기 어휘(:mod:`query_structurer.semantic_ir` ← 문법 소유자
    :mod:`calendar_window`)를 채워 없앴다. 판정자는 여전히 하나다.
    """
    plan = _plan_with_missing(query, "period", "최근")
    assert [record["cause"] for record in _causes(plan)] == [
        semantic_outcome.CAUSE_MODEL_OMISSION
    ], query


@pytest.mark.parametrize("query", ["최근 오랫동안 구매한 회원", "최근 한 분기 동안 구매한 회원"])
def test_a_period_the_extractor_does_not_own_stays_a_user_question(query: str) -> None:
    """추출기 원장에 없는 표면형은 **추측하지 않고** 되묻는다(fail-close 는 그대로다).

    '오랫동안'은 어휘가 모호 정도어로 선언한 표현이고, '한 분기'는 단위 환산 표뿐 값이 없다.
    둘 다 기간 원자가 서지 않으므로 사용자만 답할 수 있다 — 비슷해 보인다고 지어내지 않는다.
    """
    assert not [
        binding
        for binding in extract_literal_bindings(query, current_date="2026-08-07")
        if binding["kind"] == "duration"
    ], query
    plan = _plan_with_missing(query, "period", "최근")
    assert [record["cause"] for record in _causes(plan)] == [
        semantic_outcome.CAUSE_USER_OMISSION
    ], query


def test_bindings_are_consumed_only_once() -> None:
    """``bindings`` 는 Iterable 계약이다 — 제너레이터로 줘도 같은 답이 나와야 한다.

    두 번 순회하면 두 번째 소비가 조용히 빈 목록이 되고, 그 순간 판정이 되묻기로 뒤집힌다.
    """
    query = "최근 30일 구매한 회원 수를 알려줘"
    issues = [{
        "code": "missing_argument", "argument": "period", "message": "?",
        "evidence": {"text": "최근", "start": 0, "end": 2},
    }]
    bindings = [{
        "id": "duration_1",
        "kind": "duration",
        "text": "30일",
        "start": 3,
        "end": 6,
        "normalized": {
            "value": 30,
            "surface_unit": "일",
            "semantic_unit": "days",
            "temporal_kind": "rolling_duration",
        },
    }]

    records = canonical_audience_claims.missing_field_cause_records(
        query, issues, iter(bindings)
    )
    assert [record["cause"] for record in records] == [semantic_outcome.CAUSE_MODEL_OMISSION]


def test_stated_period_reemission_actually_retries_the_structurer() -> None:
    """계산된 원인이 **구조화기 재시도**로 이어지는지 가짜 complete 로 확인한다.

    첫 응답이 거짓 결핍(``expression=null`` + ``missing_argument(period)`` on '최근')이면
    되묻기로 닫지 말고 다시 물어야 한다. 이 경로가 끊기면 C 의 판정이 계산만 되고 아무것도
    바꾸지 않는다.
    """
    query = "최근 30일 구매한 회원 수를 알려줘"
    envelope = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
    }
    false_deficit = json.dumps({
        **envelope,
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": "missing_argument",
                "argument": "period",
                "message": "기간이 명시되지 않았습니다.",
                "evidence": {"text": "최근", "start": 0, "end": 2},
            }],
        },
    }, ensure_ascii=False)
    corrected = json.dumps({
        **envelope,
        "audience_requirement": {
            "expression": {
                "type": "exists",
                "relation": {
                    "type": "filter",
                    "relation": {"type": "source", "name": "purchase"},
                    "where": {
                        "type": "time_filter",
                        "field": {"type": "field", "name": "purchase.occurred_at"},
                        "window": {
                            "type": "rolling",
                            "value": 30,
                            "unit": "day",
                            "direction": "past",
                        },
                    },
                },
                "evidence": {"text": "최근 30일 구매한", "start": 0, "end": 10},
            },
            "issues": [],
        },
    }, ensure_ascii=False)

    responses = iter((false_deficit, corrected))
    events: list[tuple[str, dict]] = []
    result = LLMCampaignQueryPlanV4Structurer(
        lambda _messages: next(responses),
        max_retries=1,
        on_event=lambda name, payload: events.append((name, payload)),
    ).structure(QueryStructuringInput(
        query=query,
        context=StructuringContext(current_date="2026-08-07", timezone="Asia/Seoul"),
    ))

    failure = next(
        payload for name, payload in events
        if name == "campaign_query_plan_v4_attempt_failed"
    )
    assert "audience.period" in failure["error"], failure["error"]
    assert result["semantic_ir"]["status"] == "resolved", result["semantic_ir"]


def test_a_truly_bare_recency_is_not_retried_by_the_structurer() -> None:
    """반대 방향의 가드 — 진짜 맨 '최근'은 재시도를 태우지 않고 되묻기로 닫힌다."""

    query = "최근 구매한 회원"
    calls = 0

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": None,
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [{
                    "code": "missing_argument",
                    "argument": "period",
                    "message": "기간이 명시되지 않았습니다.",
                    "evidence": {"text": "최근", "start": 0, "end": 2},
                }],
            },
        }, ensure_ascii=False)

    result = LLMCampaignQueryPlanV4Structurer(complete, max_retries=2).structure(
        QueryStructuringInput(
            query=query,
            context=StructuringContext(current_date="2026-08-07", timezone="Asia/Seoul"),
        )
    )

    assert calls == 1
    assert result["semantic_ir"]["status"] == "needs_clarification"
    assert result["semantic_ir"]["failure_kind"] == "user_clarification"


# ── 표지 **앞**의 duration 은 그 표지의 창이 아니다 ───────────────────────────────
# 시간 절 판정자에게 되묻는 경로가 생기면서, 표지와 무관한 duration(구매주기 임계값·유효기간·
# 배송 소요일)이 근접성만으로 맨 표지의 창으로 읽힐 수 있게 됐다. 그 오판정은 되묻기를
# 재방출로 바꾸므로 재시도 예산만 태우고 사용자는 답할 기회를 잃는다 — 이 파일이 처음부터
# 막으려던 바로 그 손해다(:func:`test_bare_recency_is_still_a_user_question`).
#
# 가르는 것은 한국어 어순이다: 수량은 자기 표지 **뒤**에 온다('최근 30일'). 표지 앞에 있는
# duration 은 다른 절의 값이다. 규칙은 문장별 예외가 아니므로 여러 어휘로 함께 고정한다.


@pytest.mark.parametrize(
    "query",
    [
        "구매주기가 30일 이하이고 최근 구매가 없는 회원",
        "구매 간격이 30일 이상이고 최근 로그인이 없는 회원",
        "유효기간 30일 쿠폰을 최근 받은 회원",
        "환불까지 7일 걸린 최근 주문 회원",
        "체험판 14일 종료된 최근 회원",
    ],
)
def test_a_duration_before_a_bare_marker_is_not_that_markers_period(query: str) -> None:
    """앞 절의 임계값 duration 이 뒤의 맨 표지를 '수량화된 절'로 만들지 않는다."""

    start = query.rindex("최근")
    issues = [{
        "code": "missing_argument", "argument": "period", "message": "?",
        "evidence": {"text": "최근", "start": start, "end": start + 2},
    }]
    bindings = extract_literal_bindings(query, current_date="2026-08-07")

    records = canonical_audience_claims.missing_field_cause_records(query, issues, bindings)
    assert [record["cause"] for record in records] == [
        semantic_outcome.CAUSE_USER_OMISSION
    ], f"임계값 duration 이 맨 '최근'의 창으로 읽혔다: {records}"


@pytest.mark.parametrize(
    "query",
    [
        "구매주기가 30일 이하이고 최근 구매한 회원",
        "배송이 3일 지연된 최근 주문 회원",
    ],
)
def test_a_threshold_duration_does_not_burn_the_retry_budget(query: str) -> None:
    """오판정의 실제 손해를 종단으로 고정한다 — 답할 수 있는 질문이 시스템 실패로 바뀐다.

    재방출로 새면 구조화기가 같은 응답에 재시도를 모두 쓰고(3회) 결말이
    ``system_failure`` 가 되며 ``missing_field_causes`` 가 비어 사용자에게 물을 것이
    남지 않는다. 되묻기를 줄이는 것이 목표가 아니라 **틀린** 되묻기를 없애는 것이 목표다.
    """

    start = query.rindex("최근")
    calls = 0

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": None,
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [{
                    "code": "missing_argument",
                    "argument": "period",
                    "message": "기간이 명시되지 않았습니다.",
                    "evidence": {"text": "최근", "start": start, "end": start + 2},
                }],
            },
        }, ensure_ascii=False)

    result = LLMCampaignQueryPlanV4Structurer(complete, max_retries=2).structure(
        QueryStructuringInput(
            query=query,
            context=StructuringContext(current_date="2026-08-07", timezone="Asia/Seoul"),
        )
    )

    assert calls == 1, "임계값 duration 때문에 재시도가 걸렸다."
    assert result["semantic_ir"]["failure_kind"] == "user_clarification"
    assert [record["cause"] for record in _causes(result)] == [
        semantic_outcome.CAUSE_USER_OMISSION
    ]
