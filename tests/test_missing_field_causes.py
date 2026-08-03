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

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import canonical_audience_claims  # noqa: E402
import semantic_plan  # noqa: E402
from query_structurer import structurer  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)


def _plan_with_missing(query: str, argument: str, span: str) -> dict:
    start = query.index(span)
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
        "semantic_plan": {"nodes": []},
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
    assert [record["cause"] for record in causes] == [semantic_plan.CAUSE_MODEL_OMISSION], (
        f"추출된 값이 있는 결핍이 사용자 누락으로 분류됐다: {causes}"
    )
    assert plan["semantic_ir"]["failure_kind"] != "user_clarification", (
        "모델이 놓친 값을 사용자에게 묻고 있다 — 재방출로 고쳐야 할 결핍이다."
    )


def test_bare_recency_is_still_a_user_question() -> None:
    """되묻기를 줄이는 것이 목표가 아니다 — **틀린** 되묻기를 없애는 것이 목표다.

    맨 '최근'은 원문에 값이 정말 없다. 다른 절에 '3개월'이 있다고 재방출로 보내면
    재시도만 소모하고 사용자는 답할 기회를 잃는다(구간을 보지 않던 특례의 실제 오탐).
    """
    query = "최근 3개월 내 주문한 적은 있지만 최근 구매가 없는 회원"
    plan = _plan_with_missing(query, "period", "최근")

    causes = _causes(plan)
    assert [record["cause"] for record in causes] == [semantic_plan.CAUSE_USER_OMISSION], (
        f"다른 절의 기간 때문에 진짜 결핍이 재방출로 샜다: {causes}"
    )
    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"


def test_placeholder_is_always_a_user_question() -> None:
    """'특정 브랜드'는 어떤 추출값으로도 못 채운다 — 오직 사용자만 값을 줄 수 있다."""
    plan = _plan_with_missing("특정 브랜드를 2회 이상 구매한 회원", "brand", "특정 브랜드")
    causes = _causes(plan)
    assert causes[0]["cause"] == semantic_plan.CAUSE_USER_OMISSION
    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"


@pytest.mark.parametrize(
    ("query", "argument", "span", "expected"),
    [
        # 구간 안에 값이 있다 → 모델이 지목해 놓고 못 본 것이다.
        ("최근 30일 동안 구매한 회원", "period", "최근 30일", semantic_plan.CAUSE_MODEL_OMISSION),
        # 구간 안에 값이 없다 → 사용자만 답할 수 있다.
        ("최근 구매한 회원", "period", "최근", semantic_plan.CAUSE_USER_OMISSION),
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
    assert [record["cause"] for record in records] == [semantic_plan.CAUSE_MODEL_OMISSION]

    without = canonical_audience_claims.missing_field_cause_records(query, issues, [])
    assert [record["cause"] for record in without] == [semantic_plan.CAUSE_USER_OMISSION]
