"""혼합축(일반 조건 × 등급/상태 이력) 합성 계약.

이 파일이 있는 이유는 하나다. **72종 라이브 코퍼스에 혼합축 프롬프트가 0건이었고, 그래서
아래 결함이 전수 감사를 통과했다.**

2026-08-02 실측(재현 완료): `audience_requirement`(canonical Event IR)와 등급/상태 이력 절이
**함께** 있는 플랜이 이력 컴파일 경로 전체를 건너뛰고도 ``is_success=True`` 로 SQL 을 냈다.
경고 0, 되묻기 0. 게다가 상태 축에서는 기본 정상회원 필터 때문에 "휴면이 된 회원"을 요청했는데
"현재 정상인 회원"이 나오는 **의미 반전**이었다.

2026-08-05 이행에서 등급/상태 이력 축 자체가 폐기됐다(fail-close). 그래도 이 파일은 남긴다 —
고정하는 것은 이력 컴파일의 존재가 아니라 "절이 조용히 사라진 성공은 없다"이기 때문이다.

**재현 형태가 바뀌었다.** 예전에는 이력 절을 `semantic_plan` relation 노드로 실어 재현했지만
그 노출면이 폐기돼 오늘의 유일한 경로는 canonical ingress 다. 그래서 두 모양을 모두 태운다.

1. 구조화기가 이력 절을 **미지원으로 신고**한 경우(신고가 있으면 표현은 통째로 버려진다).
2. 구조화기가 신고조차 없이 **일반 조건만 담은 표현**을 낸 경우 — 조용한 절 소실. 이때는
   원문의 카탈로그 값('정상'·'휴면'·'골드'·'VIP')이 표현에서 소비되지 않았다는 사실을
   canonical 청구 커버리지가 잡아 SQL 출고를 막아야 한다.

마커형 함정 주의: "지난달 말 기준 VIP였던"처럼 **시간 마커**가 있는 문형은 구조화 단계에서
semantic obligation 이 먼저 생겨 다른 경로로 끝난다. 마커를 남기지 않는 **'A에서 B로 바뀐'
전이 문형만** 이 결함을 재현한다 — 마커형으로 테스트를 쓰면 통과하는 테스트를 만들고 버그는
남는다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import event_ir  # noqa: E402
import graph_rag  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    AUDIENCE_REQUIREMENT_KEY,
    attach_campaign_query_plan_v4_identity,
)

CURRENT_DATE = "2026-08-02"

STATE_TRANSITION_QUERY = "여성이면서 정상에서 휴면으로 바뀐 회원"
GRADE_TRANSITION_QUERY = "여성이면서 골드에서 VIP로 바뀐 회원"
TRANSITION_SPANS = {
    STATE_TRANSITION_QUERY: "정상에서 휴면으로 바뀐",
    GRADE_TRANSITION_QUERY: "골드에서 VIP로 바뀐",
}


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _female(query: str) -> event_ir.Condition:
    """이력 절을 **빼고** 성별만 담은 표현 — 조용한 절 소실의 실제 모양이다."""
    return event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef(name="subject.gender"),
        right=event_ir.Literal(value="female"),
        evidence=_evidence(query, "여성"),
    )


def _unsupported_issue(query: str, span: str) -> dict[str, Any]:
    start = query.index(span)
    return {
        "code": "unsupported_semantics",
        "argument": "grade_transition_values",
        "message": "model-authored issue",
        "evidence": {"text": span, "start": start, "end": start + len(span)},
    }


def _mixed_result(
    query: str, *, report_issue: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """일반 조건(성별)만 담은 표현을 실제 canonical 경로로 태운다."""
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            AUDIENCE_REQUIREMENT_KEY: {
                "expression": _female(query).to_dict(),
                "issues": (
                    [_unsupported_issue(query, TRANSITION_SPANS[query])]
                    if report_issue
                    else []
                ),
            },
        },
        query,
        current_date=CURRENT_DATE,
    )
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=payload)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=query
    )
    return plan, result


def _blocked_with_a_named_reason(result: dict[str, Any]) -> None:
    """부분 SQL 대신 '이름을 대는' 차단이어야 한다 — 무언 실패도 합격이 아니다."""
    assert not result["is_success"], (
        "이력 절이 컴파일되지 않았는데 SQL 이 출고됐다(조용한 부분 SQL):\n"
        f"{result.get('sql')}"
    )
    assert not result.get("sql")
    spoken = str(result.get("failure_reason") or "") + " ".join(
        result.get("clarification_questions") or []
    )
    assert spoken.strip(), "차단은 됐지만 사용자에게 사유를 말하지 않는다(무언 실패)."


@pytest.mark.parametrize("report_issue", [True, False])
def test_state_history_mix_never_emits_partial_sql(report_issue: bool) -> None:
    """상태 이력 절이 빠진 채로 SQL 이 나가면 안 된다.

    실측된 실패 모양: ``WHERE B.MEMBER_STATE_CD = '...NORMAL' AND B.GENDER_CD = '...FEMALE'``
    — '휴면이 된 회원'을 요청했는데 '현재 정상인 회원'이 나오는 의미 반전.

    상태 축은 전이 지표도 이력 소스도 선언이 없어 여전히 컴파일되지 않는다. 그래서 이 문장의
    계약은 그대로 '차단'이다 — 등급 축이 열렸다는 이유로 함께 열리면 안 된다.
    """
    _plan, result = _mixed_result(STATE_TRANSITION_QUERY, report_issue=report_issue)
    _blocked_with_a_named_reason(result)


@pytest.mark.parametrize("report_issue", [True, False])
def test_grade_history_mix_emits_both_clauses_or_nothing(report_issue: bool) -> None:
    """등급 이력 절은 컴파일된다 — 그러면 **두 절이 모두** SQL 에 있어야 한다.

    고정하는 것은 예나 지금이나 같다: 절이 조용히 사라진 성공은 없다. 바뀐 것은 결말이
    아니라 전제다 — 예전에는 이 절을 아무도 컴파일하지 못해 문장 전체가 막혔고, 이제는
    생산자(:mod:`temporal_claims`)가 컴파일하므로 성별 절과 함께 나간다.

    ``report_issue=False`` 는 구조화기가 신고조차 없이 성별만 담은 표현을 낸 경우다. 그때는
    합성할 근거(issue)가 없으므로 전이 절을 되살릴 수 없고, 원문의 카탈로그 값이 미소비로
    잡혀 차단돼야 한다 — 조용한 절 소실 금지가 그 자리에서 유효하다.
    """
    _plan, result = _mixed_result(GRADE_TRANSITION_QUERY, report_issue=report_issue)
    if not result["is_success"]:
        assert not result.get("sql")
        return
    sql = result["sql"]
    assert "GENDER_CD" in sql, f"성별 절이 사라졌다:\n{sql}"
    assert "PREV_ZTS_GRADE" in sql, f"전이 절이 사라졌다:\n{sql}"


def test_no_success_while_a_clause_is_silently_dropped() -> None:
    """경고 0 · 되묻기 0 인 채로 절이 사라진 성공은 어떤 축에서도 허용되지 않는다.

    canonical 권위가 있다는 이유로 결정론 드롭 감지기를 통째로 면제하면 정확히 이 상태가 된다.
    """
    for query in (STATE_TRANSITION_QUERY, GRADE_TRANSITION_QUERY):
        _plan, result = _mixed_result(query, report_issue=False)
        if not result["is_success"]:
            continue
        sql = result["sql"]
        assert "PREV_" in sql or "YYYYMM" in sql, (
            f"[{query}] 이력 절의 흔적이 SQL 에 없는데 성공으로 나갔다. "
            f"경고={result.get('dropped_signal_warnings')} 되묻기={result.get('clarification_questions')}\n{sql}"
        )


def test_silently_dropped_history_clause_is_named_in_unresolved_coverage() -> None:
    """무엇이 소실됐는지까지 남는다 — 원문의 카탈로그 값이 미소비로 잡혀야 한다.

    차단 사유만 있고 '어느 구절이 문제인가'가 없으면 운영자는 원인을 찾지 못한다.
    """
    plan, _result = _mixed_result(STATE_TRANSITION_QUERY, report_issue=False)
    unresolved = plan.get("unresolved_source_conditions") or []
    assert unresolved, "절이 소실됐는데 미해결 목록이 비었다."
    labels = " ".join(str(item.get("label") or "") for item in unresolved)
    assert "휴면" in labels or "정상" in labels, (
        f"소실된 이력 절의 근거 낱말이 미해결 목록에 없다: {unresolved}"
    )


# `test_history_only_path_is_unaffected` 는 2026-08-05 삭제됐다 — 의미 노드 하나만 실은
# 페이로드(단독 이력 경로)가 흔들리지 않는지를 본 회귀 기준선이다. 그 페이로드 모양은 폐기된
# 표현이라 실행 플랜에 실리지 않는다. 폐기 축1 의 fail-close 는
# `tests/test_retired_axes_fail_close.py` 가 canonical ingress 에서 고정한다.
