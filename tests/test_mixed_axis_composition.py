"""혼합축(일반 조건 × 등급/상태 이력) 합성 계약.

이 파일이 있는 이유는 하나다. **72종 라이브 코퍼스에 혼합축 프롬프트가 0건이었고, 그래서
아래 결함이 전수 감사를 통과했다.**

2026-08-02 실측(재현 완료): `audience_requirement`(canonical Event IR)와
`semantic_plan`(등급/상태 이력)이 **함께** 있는 플랜은 `graph_rag._apply_semantic_plan_pipeline`
의 조기 반환 때문에 이력 컴파일 경로 전체를 건너뛰고도 ``is_success=True`` 로 SQL 을 냈다.
경고 0, 되묻기 0. 게다가 상태 축에서는 기본 정상회원 필터 때문에 "휴면이 된 회원"을 요청했는데
"현재 정상인 회원"이 나오는 **의미 반전**이었다.

하네스 주의: `parser="rules"` 로는 이 회귀를 재현할 수 없다 — rules 플랜에는
``event_expression`` 이 애초에 존재하지 않아 조기 반환이 발동하지 않는다. 반드시 V4 페이로드 +
``parser="llm"`` 이어야 한다(`tests/test_canonical_audience_path.py` 와 같은 방식).

대표 프롬프트 선정도 함정이다. "지난달 말 기준 VIP였던"처럼 **시간 마커**가 있는 문형은
구조화 단계에서 semantic obligation 이 생겨 ``event_expression`` 이 통째로 버려지므로 조기 반환에
닿지도 않는다. 마커를 남기지 않는 **'A에서 B로 바뀐' 전이 문형만** 샌다 — 마커형으로 테스트를
쓰면 통과하는 테스트를 만들고 버그는 남는다.
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


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _female(query: str) -> event_ir.Condition:
    return event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef(name="subject.gender"),
        right=event_ir.Literal(value="female"),
        evidence=_evidence(query, "여성"),
    )


def _history_node(query: str, span: str, **payload: Any) -> dict[str, Any]:
    start = query.index(span)
    return {
        "id": "r1",
        "type": "relation_predicate",
        "subject": "member",
        "source_span": span,
        "source_start": start,
        "source_end": start + len(span),
        **payload,
    }


def _mixed_result(query: str, node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """일반 조건(성별)과 이력 조건을 동시에 실은 V4 페이로드를 실제 경로로 태운다."""
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            AUDIENCE_REQUIREMENT_KEY: {"expression": _female(query).to_dict(), "issues": []},
            "semantic_plan": {"nodes": [node]},
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


STATE_TRANSITION_QUERY = "여성이면서 정상에서 휴면으로 바뀐 회원"
GRADE_TRANSITION_QUERY = "여성이면서 골드에서 VIP로 바뀐 회원"


def test_state_history_mix_never_emits_partial_sql() -> None:
    """상태 이력은 소스 자체가 없다(적재 부재). 그러면 SQL 이 나가면 안 된다.

    실측된 실패 모양: ``WHERE B.MEMBER_STATE_CD = '...NORMAL' AND B.GENDER_CD = '...FEMALE'``
    — '휴면이 된 회원'을 요청했는데 '현재 정상인 회원'이 나오는 의미 반전.
    """
    _, result = _mixed_result(
        STATE_TRANSITION_QUERY,
        _history_node(
            STATE_TRANSITION_QUERY,
            "정상에서 휴면으로 바뀐",
            attribute="member_state",
            relation="transition",
            from_value="정상",
            to_value="휴면",
        ),
    )
    _blocked_with_a_named_reason(result)


def test_grade_transition_mix_puts_both_clauses_in_one_sql() -> None:
    """등급 전이는 표현 가능한 의미다(직전값 컬럼이 있다). 두 절이 한 SQL 에 있어야 한다."""
    _, result = _mixed_result(
        GRADE_TRANSITION_QUERY,
        _history_node(
            GRADE_TRANSITION_QUERY,
            "골드에서 VIP로 바뀐",
            attribute="member_grade",
            relation="transition",
            from_value="골드",
            to_value="VIP",
        ),
    )
    assert result["is_success"], (
        "등급 전이 + 성별 혼합이 SQL 을 내지 못했다: "
        f"{result.get('failure_reason')} / {result.get('clarification_questions')}"
    )
    sql = result["sql"]
    assert "GENDER_CD" in sql, f"성별 절이 사라졌다:\n{sql}"
    assert "PREV_" in sql, f"등급 전이 절이 사라졌다:\n{sql}"


def test_no_success_while_a_clause_is_silently_dropped() -> None:
    """경고 0 · 되묻기 0 인 채로 절이 사라진 성공은 어떤 축에서도 허용되지 않는다.

    canonical 권위가 있다는 이유로 결정론 드롭 감지기를 통째로 면제하면 정확히 이 상태가 된다.
    """
    for query, node in (
        (
            STATE_TRANSITION_QUERY,
            _history_node(
                STATE_TRANSITION_QUERY, "정상에서 휴면으로 바뀐",
                attribute="member_state", relation="transition", from_value="정상", to_value="휴면",
            ),
        ),
        (
            GRADE_TRANSITION_QUERY,
            _history_node(
                GRADE_TRANSITION_QUERY, "골드에서 VIP로 바뀐",
                attribute="member_grade", relation="transition", from_value="골드", to_value="VIP",
            ),
        ),
    ):
        _, result = _mixed_result(query, node)
        if not result["is_success"]:
            continue
        sql = result["sql"]
        assert "PREV_" in sql or "YYYYMM" in sql, (
            f"[{query}] 이력 절의 흔적이 SQL 에 없는데 성공으로 나갔다. "
            f"경고={result.get('dropped_signal_warnings')} 되묻기={result.get('clarification_questions')}\n{sql}"
        )


@pytest.mark.parametrize(
    ("query", "node"),
    [
        pytest.param(
            "정상에서 휴면으로 바뀐 회원",
            {"attribute": "member_state", "relation": "transition", "from_value": "정상", "to_value": "휴면"},
            id="state-only",
        ),
        pytest.param(
            "골드에서 VIP로 바뀐 회원",
            {"attribute": "member_grade", "relation": "transition", "from_value": "골드", "to_value": "VIP"},
            id="grade-only",
        ),
    ],
)
def test_history_only_path_is_unaffected(query: str, node: dict[str, Any]) -> None:
    """단독 이력 경로(일반 조건 없음)는 이번 변경으로 흔들리면 안 된다 — 회귀 기준선."""
    span = query.replace(" 회원", "")
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {},
            "result_limit": None,
            "semantic_plan": {"nodes": [_history_node(query, span, **node)]},
        },
        query,
        current_date=CURRENT_DATE,
    )
    plan = graph_rag.build_query_plan(query, parser="llm", query_plan_v4=payload)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=query
    )
    if result["is_success"]:
        assert "PREV_" in result["sql"] or "YYYYMM" in result["sql"]
    else:
        _blocked_with_a_named_reason(result)
