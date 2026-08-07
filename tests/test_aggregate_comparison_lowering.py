"""기간 대 기간 집계 비교의 **의미 불변식** — 지원 여부는 이름이 아니라 lowering 가능성이다.

배경(실측 2026-08-07). ``2026년 2월과 3월의 구매금액이 증가한 회원`` 을 라이브로 12회 돌렸을 때
같은 코드·같은 원문인데 귀결이 넷이었다::

    성공 SQL 생성                     3회
    semantic_ir_unsupported           6회
    semantic_registry_gap             3회
    ambiguous_requirement             1회

원인은 요구가 어려워서가 아니었다 — 그 요구는 ``aggregate.scalar`` 둘과 비교 하나로 이미
컴파일된다. 갈린 것은 **모델이 그 자리를 무엇이라 불렀는가**였다. 같은 원문 12회에 이름이
여섯 가지였고(``metric_transition`` · ``month_to_month_change`` ·
``temporal_comparison_between_monthly_metrics`` · ``monthly_purchase_amount_transition`` ·
``month_over_month_increase`` · ``comparison``), 그 이름에 ``transition`` 이라는 글자가 있는지와
근거 구간에 ``구매금액`` 이라는 표면어가 있는지가 사용자 문구를 결정했다.

그래서 여기서 재는 것은 SQL 문자열이 아니라 계약이다.

    1. 어순이 달라도 같은 canonical 구조로 떨어진다(paraphrase corpus).
    2. 모델이 어떤 이름을 지어내도 귀결이 같다(**이름 독립성**).
    3. 낮출 수 있는 요구는 false unsupported / false registry gap 으로 끝나지 않는다.
    4. 증감 방향이 비교 연산자의 극성으로 정확히 보존된다(반전은 최악의 오답이다).
    5. 순위·단일 임계 요청을 비교로 오인하지 않는다(오탐 0).
"""

from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

import execution_assets  # noqa: E402
import lowering_planner  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)

CURRENT_DATE = "2026-08-03"
TODAY = date(2026, 8, 3)

QUERY = "2026년 2월과 3월의 구매금액이 증가한 회원"

# 실측된 모델 산 argument 이름 전부. 이 목록은 **지원 여부와 무관해야 한다**는 사실을 재는
# 재료일 뿐, 판정에 쓰이지 않는다. 새 이름이 관측되면 여기 한 줄 늘려 같은 계약을 재확인한다.
OBSERVED_MODEL_ARGUMENTS = (
    "metric_transition",
    "month_to_month_change",
    "temporal_comparison_between_monthly_metrics",
    "monthly_purchase_amount_transition",
    "month_over_month_increase",
    "comparison",
)

# 어순·표지가 다른 같은 뜻. 전부 '3월 합계 > 2월 합계'로 떨어져야 한다.
PARAPHRASES = (
    "2026년 2월과 3월의 구매금액이 증가한 회원",
    "2026년 2월보다 2026년 3월 구매금액이 증가한 회원",
    "2026년 2월과 비교해 2026년 3월 구매금액이 많은 회원",
    "2026년 3월 구매금액이 2026년 2월 구매금액보다 큰 회원",
    "2026년 2월 대비 2026년 3월 구매금액이 증가한 회원",
)

FEBRUARY = (date(2026, 2, 1), date(2026, 3, 1))
MARCH = (date(2026, 3, 1), date(2026, 4, 1))


def _purchase_amount_obligations(query: str) -> tuple[Any, ...]:
    return tuple(
        obligation
        for obligation in lowering_planner.detect_comparison_obligations(query, today=TODAY)
        if obligation.left.metric_id == "purchase_amount"
    )


def _sole_obligation(query: str) -> Any:
    obligations = _purchase_amount_obligations(query)
    assert len(obligations) == 1, f"의무가 {len(obligations)}개다: {obligations}"
    return obligations[0]


# ── 1. paraphrase corpus → 같은 canonical 구조 ────────────────────────────────────


@pytest.mark.parametrize("query", PARAPHRASES)
def test_paraphrases_lower_to_the_same_aggregate_comparison(query: str) -> None:
    """어순이 달라도 canonical 구조는 하나다 — 'month_to_month_change' 같은 **이름**이 아니라."""
    obligation = _sole_obligation(query)

    assert obligation.operator == ">"
    assert obligation.left.metric_id == obligation.right.metric_id == "purchase_amount"
    # 나중 기간이 왼쪽(증가의 주체), 이전 기간이 오른쪽(기준)이다.
    assert (obligation.left.window_start, obligation.left.window_end_exclusive) == MARCH
    assert (obligation.right.window_start, obligation.right.window_end_exclusive) == FEBRUARY
    assert obligation.left.window_start != obligation.right.window_start
    assert lowering_planner.can_plan(obligation)


@pytest.mark.parametrize("query", PARAPHRASES)
def test_the_plan_is_built_from_declared_execution_primitives(query: str) -> None:
    """계획의 존재가 곧 지원의 증명이다 — capability 목록 조회가 아니라 실제 컴파일 결과다."""
    plan = lowering_planner.try_plan(_sole_obligation(query))

    assert plan is not None
    assert plan.capabilities == frozenset({"aggregate.scalar"})
    # 두 창이 각각 반개구간으로 실려야 한다. 하루라도 어긋나면 조용히 다른 집합이 된다.
    assert "'20260301'" in plan.sql and "'20260401'" in plan.sql
    assert "'20260201'" in plan.sql


def test_increase_and_decrease_keep_opposite_polarity() -> None:
    """극성 반전은 최악의 오답이다 — 방향 낱말 하나가 연산자를 정확히 뒤집어야 한다."""
    increased = _sole_obligation("2026년 2월과 3월의 구매금액이 증가한 회원")
    decreased = _sole_obligation("2026년 2월과 3월의 구매금액이 감소한 회원")

    assert increased.operator == ">"
    assert decreased.operator == "<"
    # 창 배치는 같다. 달라지는 것은 연산자뿐이다.
    assert increased.left.window_start == decreased.left.window_start
    assert increased.right.window_start == decreased.right.window_start


def test_the_mechanism_is_not_specific_to_purchase_amount() -> None:
    """카탈로그가 집계로 선언한 지표면 무엇이든 같은 경로를 탄다."""
    obligations = lowering_planner.detect_comparison_obligations(
        "2026년 1분기와 2분기 구매 횟수가 증가한 회원", today=TODAY
    )
    counts = [item for item in obligations if item.left.metric_id == "purchase_count"]

    assert counts, "구매 횟수도 같은 구조로 잡혀야 한다"
    assert lowering_planner.can_plan(counts[0])


# ── 2. 오탐 0 — 비교가 아닌 요청을 비교로 읽지 않는다 ──────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        # 순위 요청. '큰'이 있지만 비교 표지가 없으므로 방향으로 읽지 않는다.
        "2026년 2월과 3월에 구매한 회원 중 구매금액이 큰 순으로",
        # 단일 임계. 창이 하나뿐이다.
        "2026년 3월 구매금액이 10만원 이상인 회원",
        # 창도 방향도 없다.
        "최근 30일 동안 구매한 회원을 찾아줘",
        # 창은 둘이지만 지표가 없다.
        "2026년 2월과 3월에 로그인한 회원",
    ],
)
def test_non_comparison_requests_capture_no_obligation(query: str) -> None:
    assert lowering_planner.detect_comparison_obligations(query, today=TODAY) == ()


# ── 3. 이름 독립성 — 이 파일의 핵심 계약 ──────────────────────────────────────────


def _payload(argument: str) -> dict[str, Any]:
    """모델이 ``argument`` 라는 이름으로 미지원을 신고한 V4 출력."""
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None, "offer_type": None, "channels": [], "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": "unsupported_semantics",
                "argument": argument,
                "message": "월 대비 구매금액 증가를 표현할 수 없습니다.",
                "evidence": {"text": QUERY, "start": 0, "end": len(QUERY)},
            }],
        },
    }


def _project(argument: str) -> dict[str, Any]:
    return attach_campaign_query_plan_v4_identity(
        copy.deepcopy(_payload(argument)), QUERY, current_date=CURRENT_DATE
    )


@pytest.mark.parametrize("argument", OBSERVED_MODEL_ARGUMENTS)
def test_no_false_unsupported_whatever_the_model_named_it(argument: str) -> None:
    """낮출 수 있는 요구는 '표현할 수 없다'로 끝나지 않는다."""
    semantic_ir = _project(argument).get("semantic_ir") or {}

    assert semantic_ir.get("status") != "unsupported"
    assert semantic_ir.get("failure_kind") != "unsupported"
    assert semantic_ir.get("message") != "요청한 조건을 현재 실행 자산으로 표현할 수 없습니다."


@pytest.mark.parametrize("argument", OBSERVED_MODEL_ARGUMENTS)
def test_no_false_registry_gap_whatever_the_model_named_it(argument: str) -> None:
    """현재값 지표 개념이 선언돼 있다는 사실은 기간 비교가 선언돼 있다는 증거가 아니다."""
    plan = _project(argument)

    assert plan.get("audience_execution_assets") is None
    message = (plan.get("semantic_ir") or {}).get("message") or ""
    assert "선언된 자산" not in message


@pytest.mark.parametrize("argument", OBSERVED_MODEL_ARGUMENTS)
def test_every_model_name_ends_in_the_same_emission_failure(argument: str) -> None:
    """귀결은 이름과 무관하게 하나다 — 실행 표현이 서지 않은 **방출 실패**."""
    plan = _project(argument)
    semantic_ir = plan.get("semantic_ir") or {}

    assert semantic_ir.get("failure_kind") == "system_failure"
    # 사유는 **명시 선언**이어야 한다. 파생(system_failure → semantic_registry_gap)에 맡기면
    # 방출 실패가 '레지스트리 구멍'으로 보고돼 운영에서 없는 결함을 찾게 된다.
    assert semantic_ir.get("failure_reason") == "semantic_emission_failure"
    assert semantic_ir.get("message") == (
        "요청한 조건은 지원되는 의미이지만 실행 표현으로 확정되지 않았습니다."
    )
    failures = plan.get("audience_emission_failures") or []
    assert [item["obligation_kind"] for item in failures] == [
        lowering_planner.AGGREGATE_COMPARISON
    ]


def test_all_observed_model_names_produce_one_identical_outcome() -> None:
    """이름 여섯 가지가 **한 가지** 귀결로 모인다 — 흔들림 자체가 결함이었다."""
    outcomes = {
        (
            (_project(argument).get("semantic_ir") or {}).get("status"),
            (_project(argument).get("semantic_ir") or {}).get("failure_kind"),
            (_project(argument).get("semantic_ir") or {}).get("message"),
        )
        for argument in OBSERVED_MODEL_ARGUMENTS
    }

    assert len(outcomes) == 1, f"이름에 따라 귀결이 갈렸다: {outcomes}"


# ── 4. 자산 호환성은 관계로 판정한다 ──────────────────────────────────────────────


@pytest.mark.parametrize("argument", OBSERVED_MODEL_ARGUMENTS)
def test_current_value_assets_never_answer_a_period_comparison(argument: str) -> None:
    """``구매금액`` 표면어를 아는 자산이 있다는 것과 기간 비교를 구현한다는 것은 다른 말이다."""
    issue = {
        "code": "unsupported_semantics",
        "argument": argument,
        "evidence": {"text": QUERY, "start": 0, "end": len(QUERY)},
    }

    # 표면어로는 여전히 걸린다 — 그 낱말이 원문에 있으므로.
    assert any(
        asset.symbol == "purchase_amount"
        for asset in execution_assets.non_canonical_assets_for_text(QUERY)
    )
    # 그러나 **이 관계**에는 답이 되지 않는다.
    assert execution_assets.assets_compatible_with_issue(issue, query=QUERY) == ()


def test_a_current_value_relation_still_finds_its_declared_asset() -> None:
    """관계 판정이 정상적인 registry gap 까지 막아 버리면 안 된다(email_optin 회귀 방지)."""
    query = "이메일 수신에 동의한 회원을 찾아줘"
    issue = {
        "code": "unsupported_semantics",
        "argument": "email_consent_flag",
        "evidence": {"text": "이메일 수신에 동의한", "start": 0, "end": 11},
    }

    assets = execution_assets.assets_compatible_with_issue(issue, query=query)

    assert [asset.symbol for asset in assets] == ["email_optin"]
