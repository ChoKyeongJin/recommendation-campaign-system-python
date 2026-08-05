"""시간·이력 조건이 **라이브 경로로** SQL 까지 가는지의 계약.

:mod:`tests.test_temporal_claims` 는 생산자와 낮춤을 직접 부른다. 그것만으로는 부족하다 —
이 저장소에서 실제로 반복된 사고는 "모듈은 완성돼 있는데 아무도 부르지 않는다"였다
(전이 스택이 그 상태로 감사를 통과했다). 그래서 여기서는 **모델 payload → 플랜 → SQL** 의
전 구간을 태우고, 도중의 게이트 넷을 전부 지나는지 잰다.

    ① 합성 라우터        모델의 미지원 신고를 애플리케이션이 반박하는가
    ② 청구 커버리지      기간 리터럴과 카탈로그 값이 소비된 것으로 인정되는가
    ③ 의무 영수증        member_state_history 의무가 **컴파일**로 귀결되는가
    ④ 의미 불변식        전칭 낱춤(존재 ∧ 위반부재)이 존재/부재 모순으로 오탐되지 않는가

한 게이트만 남아도 SQL 은 나가지 않으므로, 이 파일이 초록이면 네 게이트가 모두 열린 것이다.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import semantic_requirements  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)

CURRENT_DATE = "2026-08-05"


def _raw(query: str, span: str) -> dict:
    """모델이 실제로 내는 모양: 표현은 못 만들었고 그 구절을 미지원으로 신고했다."""

    start = query.index(span)
    return {
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
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "temporal_condition",
                    "message": "The requested condition cannot be represented.",
                    "evidence": {
                        "text": span,
                        "start": start,
                        "end": start + len(span),
                    },
                }
            ],
        },
    }


def _run(query: str, span: str) -> tuple[dict, dict, dict]:
    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(_raw(query, span)), query, current_date=CURRENT_DATE
    )
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=structured)
    result = graph_rag.build_sql_result(
        nx.Graph(),
        query,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        100,
        original_query=query,
    )
    return structured, plan, result


# (원문, 모델이 신고한 구절, SQL 에 반드시 있어야 하는 조각)
COMPILED_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "골드에서 VIP로 승급한 회원",
        "골드에서 VIP로 승급",
        ("MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'", "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "가치등급이 골드에서 VIP로 바뀐 회원",
        "가치등급이 골드에서 VIP로 바뀐",
        ("MS.WORTH_GRADE = 'VIP'", "MS.PREV_WORTH_GRADE = 'GOLD'"),
    ),
    (
        "지난달 말 기준 VIP였던 회원을 찾아줘",
        "지난달 말 기준 VIP였던",
        ("MS.YYYYMM >= '202607'", "MS.YYYYMM < '202608'", "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'"),
    ),
    (
        "직전 등급이 골드였던 회원",
        "직전 등급이 골드였던",
        ("MS.YYYYMM >= '202607'", "MS.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "2026년 3월 기준 골드 등급 회원",
        "2026년 3월 기준 골드 등급",
        ("MS.YYYYMM >= '202603'", "MS.YYYYMM < '202604'"),
    ),
    (
        "최근 6개월 동안 골드에서 VIP로 승급한 회원",
        "최근 6개월 동안 골드에서 VIP로 승급",
        ("MS.YYYYMM >= '202603'", "MS.YYYYMM < '202609'", "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
    (
        "지난 6개월 매월 골드 등급이었던 회원",
        "지난 6개월 매월 골드 등급",
        ("COUNT(DISTINCT MS.YYYYMM) = 6",),
    ),
    (
        "최근 6개월 내내 골드 등급을 유지한 회원",
        "최근 6개월 내내 골드 등급을 유지",
        ("NOT EXISTS", "MS.ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"),
    ),
)


@pytest.mark.parametrize(
    ("query", "span", "fragments"),
    COMPILED_CASES,
    ids=[case[0] for case in COMPILED_CASES],
)
def test_a_temporal_clause_reaches_sql_through_the_live_path(
    query: str, span: str, fragments: tuple[str, ...]
) -> None:
    structured, _plan, result = _run(query, span)

    assert structured["semantic_ir"]["status"] == "resolved"
    assert structured["audience_requirement"]["issues"] == []
    assert result["is_success"] is True, result.get("failure_reason")
    sql = result["sql"] or ""
    for fragment in fragments:
        assert fragment in sql, f"{fragment!r} 가 없다:\n{sql}"


def test_the_temporal_obligation_is_discharged_by_a_compile_receipt() -> None:
    """의무는 '종류'가 아니라 **컴파일 사실**로 방면된다.

    종류만 보고 면제하면 as_of·직전값·유지·변경횟수 마커가 만든 의무까지 함께 풀려,
    낮춰지지 않은 표현이 검증을 통과한다. 그래서 영수증에 컴파일러 이름이 남아야 한다.
    """

    query = "골드에서 VIP로 승급한 회원"
    _structured, plan, _result = _run(query, "골드에서 VIP로 승급")

    receipts = plan.get(semantic_requirements.SOURCE_REQUIREMENT_RECEIPTS_KEY) or []
    temporal = [
        receipt
        for receipt in receipts
        if receipt.get("compiler") == "temporal_ir"
        and receipt.get("status") == "compiled"
    ]
    assert temporal, f"시간 의무의 컴파일 영수증이 없다: {receipts}"


def test_an_unrelated_condition_never_discharges_a_temporal_obligation() -> None:
    """다른 컴파일러가 만든 표현은 시간 의무를 방면하지 못한다.

    방면의 조인 키는 근거 구간의 **정확한 일치**다. 포함 관계로 느슨하게 보면 현재값 필터가
    이력 조건을 반박하게 되고, 그때 '휴면이 된 회원'이 '지금 휴면인 회원'으로 바뀐다.
    """

    import canonical_audience_claims  # noqa: PLC0415
    import event_ir  # noqa: PLC0415

    query = "골드에서 VIP로 승급한 회원"
    unrelated = event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef("subject.gender"),
        right=event_ir.Literal("female"),
        evidence=event_ir.Evidence(text="골드에서 VIP로 승급", start=0, end=12),
    )
    assert (
        canonical_audience_claims.temporal_obligation_compiled_spans(query, unrelated)
        == ()
    )


# (원문, 신고 구절) — 여전히 SQL 이 나가면 안 되는 요청.
BLOCKED_CASES: tuple[tuple[str, str], ...] = (
    # 상태 축: 전이 지표도 이력 소스도 선언이 없다.
    ("정상에서 휴면으로 바뀐 회원", "정상에서 휴면으로 바뀐"),
    # 방향어가 값 순서와 모순된다.
    ("VIP에서 골드로 승급한 회원", "VIP에서 골드로 승급"),
    # 월 스냅샷은 칸 안의 변경을 관측하지 못한다.
    ("최근 1년 동안 등급이 3회 이상 변경된 회원", "등급이 3회 이상 변경"),
    # 끊기지 않은 N칸은 주체별 정렬·간격 판정이 필요하다.
    ("3개월 연속 골드 등급이었던 회원", "3개월 연속 골드 등급"),
)


@pytest.mark.parametrize(
    ("query", "span"), BLOCKED_CASES, ids=[case[0] for case in BLOCKED_CASES]
)
def test_an_unanswerable_temporal_clause_still_reaches_no_sql(
    query: str, span: str
) -> None:
    """열린 축이 닫힌 축까지 함께 열지 않는다 — 그리고 침묵하지 않는다."""

    _structured, _plan, result = _run(query, span)

    assert result["is_success"] is False
    assert not result["sql"]
    spoken = str(result.get("failure_reason") or "") + " ".join(
        result.get("clarification_questions") or []
    )
    assert spoken.strip(), "차단은 됐지만 사용자에게 사유를 말하지 않는다(무언 실패)."


def test_a_mixed_sentence_keeps_both_clauses_or_none() -> None:
    """일반 조건과 시간 조건이 섞여도 절이 조용히 사라진 성공은 없다.

    모델이 일반 조건만 표현하고 시간 절을 신고한 모양 — 합성이 성립하면 **두 절이 모두**
    SQL 에 있어야 하고, 성립하지 않으면 아무것도 나가지 않아야 한다.
    """

    query = "여성이면서 골드에서 VIP로 바뀐 회원"
    span = "골드에서 VIP로 바뀐"
    start = query.index(span)
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {
            "expression": {
                "type": "comparison",
                "operator": "=",
                "left": {"type": "field", "name": "subject.gender"},
                "right": {"type": "literal", "value": "female"},
                "evidence": {"text": "여성", "start": 0, "end": 2},
            },
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "temporal_condition",
                    "message": "cannot represent",
                    "evidence": {
                        "text": span,
                        "start": start,
                        "end": start + len(span),
                    },
                }
            ],
        },
    }
    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(payload), query, current_date=CURRENT_DATE
    )
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=structured)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    if not result["is_success"]:
        assert not result["sql"]
        return
    sql = result["sql"]
    assert "GENDER_CD" in sql, f"성별 절이 사라졌다:\n{sql}"
    assert "PREV_ZTS_GRADE" in sql, f"전이 절이 사라졌다:\n{sql}"
