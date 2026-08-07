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
from datetime import date
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


def _issue(query: str, code: str, argument: str, span: str) -> dict:
    start = query.index(span)
    return {
        "code": code,
        "argument": argument,
        "message": "The requested condition cannot be represented.",
        "evidence": {"text": span, "start": start, "end": start + len(span)},
    }


def _payload(query: str, *, expression: dict | None, issues: list[dict]) -> dict:
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {"expression": expression, "issues": issues},
    }


def _raw(query: str, span: str) -> dict:
    """모델이 실제로 내는 모양: 표현은 못 만들었고 그 구절을 미지원으로 신고했다."""

    return _payload(
        query,
        expression=None,
        issues=[_issue(query, "unsupported_semantics", "temporal_condition", span)],
    )


def _run_payload(query: str, payload: dict) -> tuple[dict, dict, dict]:
    structured = attach_campaign_query_plan_v4_identity(
        copy.deepcopy(payload), query, current_date=CURRENT_DATE
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


def _run(query: str, span: str) -> tuple[dict, dict, dict]:
    return _run_payload(query, _raw(query, span))


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


# ── provenance 는 의미가 아니다 ────────────────────────────────────────────────────
#
# 방면 판정은 **의미 구조**로 한다. 근거 구간(evidence)은 그 조건이 원문 어디에서 왔는지를
# 적은 출처 표기이지 조건의 뜻이 아니다. 두 축을 섞으면 뜻이 같은 표현이 인용 구간 차이만으로
# 반려되고(A), 모델은 앱의 낮춤이 고른 구간을 글자 단위로 맞혀야만 통과할 수 있다 — 통과할 수
# 없으므로 재시도 예산만 태우다 회차마다 다른 귀결로 끝난다(실측 2026-08-08, 같은 원문 5회에
# sql 1 / clarification 3 / system_failure 1).
#
# 그렇다고 근거 구간으로 방면하면 위조가 열린다(B·C). 그래서 계약은 둘로 나뉜다:
#   판정   = provenance 를 제외한 semantic atom 일치
#   방면 구간 = 애플리케이션 소유 낮춤이 계산한 근거(모델이 주장한 구간이 아니다)

# 값 축이 절대 달력인 원문을 쓴다 — 재판정 기준일이 어떻게 정해지든 창이 같아서, 이 테스트가
# 재는 것이 오직 provenance 축이 되게 한다.
_ABSOLUTE_AS_OF_QUERY = "2026년 3월 기준 골드 등급 회원"
_ABSOLUTE_AS_OF_SPAN = "2026년 3월 기준 골드 등급"


def _reprovenance(node: object, evidence: dict) -> object:
    """표현의 모든 근거 표기를 하나로 갈아 끼운다(의미 구조는 손대지 않는다)."""

    if isinstance(node, dict):
        return {
            key: dict(evidence) if key == "evidence" else _reprovenance(value, evidence)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_reprovenance(item, evidence) for item in node]
    return node


def _canonical_expression(query: str, span: str) -> dict:
    """애플리케이션 소유 낮춤이 만든 canonical 표현(라이브 경로에서 그대로 꺼낸다)."""

    structured, _plan, _result = _run(query, span)
    expression = structured["audience_requirement"]["expression"]
    assert isinstance(expression, dict), "전제: 이 원문은 애플리케이션이 낮출 수 있다"
    return copy.deepcopy(expression)


def _evidence_of(query: str, span: str) -> dict:
    start = query.index(span)
    return {"text": span, "start": start, "end": start + len(span)}


def test_a_same_semantics_with_different_evidence_is_still_discharged() -> None:
    """A. 의미 구조가 같고 근거 표기만 다른 표현은 의무를 방면한다.

    모델이 절 전체가 아니라 값 어구만 인용해도(둘 다 원문의 옳은 근거다) 같은 조건이다.
    """

    import canonical_audience_claims  # noqa: PLC0415
    import event_ir  # noqa: PLC0415

    canonical = _canonical_expression(_ABSOLUTE_AS_OF_QUERY, _ABSOLUTE_AS_OF_SPAN)
    restated = _reprovenance(
        canonical, _evidence_of(_ABSOLUTE_AS_OF_QUERY, "골드 등급")
    )
    assert restated != canonical, "전제: 근거 표기가 실제로 달라졌다"

    spans = canonical_audience_claims.temporal_obligation_compiled_spans(
        _ABSOLUTE_AS_OF_QUERY, event_ir.condition_from_dict(copy.deepcopy(restated))
    )
    assert spans, "의미가 같은데 근거 표기 때문에 방면되지 않았다"


def test_a_restated_evidence_reaches_sql_through_the_live_path() -> None:
    """A(종단). 같은 표현을 모델이 낸 것으로 놓아도 SQL 까지 간다."""

    canonical = _canonical_expression(_ABSOLUTE_AS_OF_QUERY, _ABSOLUTE_AS_OF_SPAN)
    restated = _reprovenance(
        canonical, _evidence_of(_ABSOLUTE_AS_OF_QUERY, "골드 등급")
    )
    assert restated != canonical, "전제: 근거 표기가 실제로 달라졌다"
    _structured, _plan, result = _run_payload(
        _ABSOLUTE_AS_OF_QUERY, _payload(_ABSOLUTE_AS_OF_QUERY, expression=restated, issues=[])
    )
    assert result["is_success"] is True, result.get("failure_reason")
    assert "MS.YYYYMM >= '202603'" in (result["sql"] or "")


FORGERY_CASES: tuple[tuple[str, str, dict], ...] = (
    # B. 시간축이 아닌 구조에 전이 구절의 근거를 붙였다.
    (
        "gender",
        "골드에서 VIP로 승급한 회원",
        {
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": "subject.gender"},
            "right": {"type": "literal", "value": "female"},
            "evidence": {"text": "골드에서 VIP로 승급", "start": 0, "end": 12},
        },
    ),
    # C. **현재값** 필터에 as-of 구절의 근거를 붙였다. 이것이 통과하면 '그때 VIP였던'이
    #    '지금 VIP인'으로 조용히 바뀐다.
    (
        "current_grade",
        _ABSOLUTE_AS_OF_QUERY,
        {
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": "subject.grade"},
            "right": {"type": "literal", "value": "gold"},
            "evidence": _evidence_of(_ABSOLUTE_AS_OF_QUERY, _ABSOLUTE_AS_OF_SPAN),
        },
    ),
)


@pytest.mark.parametrize(
    ("name", "query", "raw"), FORGERY_CASES, ids=[case[0] for case in FORGERY_CASES]
)
def test_bc_forged_structure_never_discharges_a_temporal_obligation(
    name: str, query: str, raw: dict
) -> None:
    """B·C. 근거 표기를 베껴도 의미 구조가 다르면 방면되지 않는다."""

    import canonical_audience_claims  # noqa: PLC0415
    import event_ir  # noqa: PLC0415

    spans = canonical_audience_claims.temporal_obligation_compiled_spans(
        query, event_ir.condition_from_dict(copy.deepcopy(raw))
    )
    assert spans == (), f"{name}: 위조된 근거로 시간 의무가 방면됐다"


# ── 하나의 합성이 여러 신고를 설명할 때 ──────────────────────────────────────────
#
# 모델은 같은 의미 실패를 자주 여러 issue 로 쪼개 낸다(실측: 시간 절 1 + 등급 극성 1).
# 구제 여부를 issue **개수**로 정하면 그 쪼갬이 곧 실패가 된다. 기준은 개수가 아니라
# "합성이 신고된 자리를 전부 설명하는가"다.

_MULTI_ISSUE_QUERY = "지난달 말 기준 VIP였던 회원을 찾아줘"


def test_d_multi_issue_is_rescued_when_the_synthesis_accounts_for_every_issue() -> None:
    """D. 모든 신고가 합성 구간으로 설명되면 구제된다."""

    payload = _payload(
        _MULTI_ISSUE_QUERY,
        expression=None,
        issues=[
            _issue(_MULTI_ISSUE_QUERY, "ambiguous_requirement", "subject.grade", "VIP였던"),
            _issue(
                _MULTI_ISSUE_QUERY,
                "unsupported_semantics",
                "member_state_history",
                "지난달 말 기준",
            ),
        ],
    )
    structured, _plan, result = _run_payload(_MULTI_ISSUE_QUERY, payload)

    assert structured["audience_requirement"]["issues"] == []
    assert result["is_success"] is True, result.get("failure_reason")
    sql = result["sql"] or ""
    for fragment in ("MS.YYYYMM >= '202607'", "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'"):
        assert fragment in sql, f"{fragment!r} 가 없다:\n{sql}"


def test_e_an_unaccounted_issue_blocks_the_whole_synthesis() -> None:
    """E. 설명되지 않는 신고가 하나라도 남으면 전부 막힌다(부분 방면 금지)."""

    payload = _payload(
        _MULTI_ISSUE_QUERY,
        expression=None,
        issues=[
            _issue(
                _MULTI_ISSUE_QUERY,
                "unsupported_semantics",
                "member_state_history",
                "지난달 말 기준",
            ),
            # 합성 구간(지난달 / 지난달 말 기준 / VIP) 어디에도 걸치지 않는 자리.
            _issue(
                _MULTI_ISSUE_QUERY, "ambiguous_requirement", "result_scope", "회원을 찾아줘"
            ),
        ],
    )
    structured, _plan, result = _run_payload(_MULTI_ISSUE_QUERY, payload)

    assert result["is_success"] is False
    assert not result["sql"]
    # 설명된 신고만 지우고 나머지를 흘려 보내면 절이 사라진 SQL 이 나간다 — 표현이 아예 서지
    # 않아야 하고, 두 신고가 모두 남아야 한다.
    assert structured["audience_requirement"]["expression"] is None
    assert len(structured["audience_requirement"]["issues"]) == 2


def test_g_a_clause_the_model_already_compiled_is_not_conjoined_twice() -> None:
    """G. 모델이 이미 낮춘 절에 합성을 또 붙이지 않는다(``A ∧ A`` 금지).

    모델이 canonical 형상을 스스로 내면서 같은 절을 미지원으로도 신고하는 모양이 있다.
    결합해 버리면 같은 조건이 두 번 들어가고, 그 중복을 조건 커버리지가 미귀결로 읽어
    **옳은 SQL 이 막힌다**(실측 2026-08-08 라이브 #20: 3회 중 2회).
    """

    query, span = "3개월 내내 VIP를 유지한 회원", "3개월 내내 VIP를 유지"
    canonical = _canonical_expression(query, span)

    structured, _plan, result = _run_payload(
        query,
        _payload(
            query,
            expression=copy.deepcopy(canonical),
            issues=[_issue(query, "unsupported_semantics", "member_state_history", span)],
        ),
    )

    assert structured["audience_requirement"]["issues"] == []
    expression = structured["audience_requirement"]["expression"]
    if expression.get("type") == "and":
        operands = expression.get("operands") or []
        assert operands[0] != operands[1], "같은 절이 두 번 결합됐다"
    assert result["is_success"] is True, result.get("failure_reason")
    # 전칭 낮춤은 존재 ∧ 위반부재 두 절이다 — 네 절이면 중복이 SQL 까지 갔다는 뜻이다.
    label = (result["sql"] or "").split("AS segment_label")[0]
    assert label.count("스냅샷") == 2, f"조건이 중복됐다:\n{label}"


# ── 지원 판정의 소유자 ────────────────────────────────────────────────────────────
#
# "이 의무를 낼 수 있는가"의 답은 목록 조회가 아니라 **낮춰 보기**다. 종류 이름을 registry 에
# 적어 두고 실제 probe 를 연결하지 않으면 없는 지원을 광고하게 되고, 그때 모델의 **옳은**
# 미지원 신고까지 반박해 재시도만 반복한다.


def test_the_planner_kind_name_matches_the_obligation_ledger() -> None:
    """계획자와 원장이 같은 종류 이름을 써야 한다(둘이 갈라지면 좌표가 안 맞는다)."""

    import lowering_planner  # noqa: PLC0415

    assert (
        lowering_planner.MEMBER_STATE_HISTORY
        == semantic_requirements.TEMPORAL_QUALIFIER_KIND
    )


def test_the_planner_plans_a_compilable_temporal_clause() -> None:
    """낮출 수 있는 시간 절에는 계획이 있고, 그 계획은 SQL 까지 내려간다."""

    import lowering_planner  # noqa: PLC0415

    plans = [
        plan
        for plan in lowering_planner.plans_for_query(
            _MULTI_ISSUE_QUERY, today=date.fromisoformat(CURRENT_DATE)
        )
        if plan.obligation.kind == lowering_planner.MEMBER_STATE_HISTORY
    ]
    assert plans, "낮출 수 있는 절인데 계획이 없다"
    # 계획의 증명은 SQL 이다. capabilities 는 비어 있을 수 있다 — 이 형상이 기본 대수만
    # 쓴다는 뜻이지 낮추지 못했다는 뜻이 아니다.
    assert plans[0].sql.strip()
    assert plans[0].obligation.source_span == (0, 8)


@pytest.mark.parametrize(
    ("query", "span"), BLOCKED_CASES, ids=[case[0] for case in BLOCKED_CASES]
)
def test_the_planner_never_plans_an_unanswerable_clause(query: str, span: str) -> None:
    """낮출 수 없는 절에는 계획이 없어야 한다 — 있으면 없는 지원을 광고하는 것이다."""

    import lowering_planner  # noqa: PLC0415

    assert [
        plan
        for plan in lowering_planner.plans_for_query(
            query, today=date.fromisoformat(CURRENT_DATE)
        )
        if plan.obligation.kind == lowering_planner.MEMBER_STATE_HISTORY
    ] == []


def test_the_prompt_recipe_only_teaches_what_the_planner_can_lower() -> None:
    """레시피는 계획이 있을 때만 나온다(지원하지 않는 형상을 가르치지 않는다)."""

    from query_structurer.prompt import render_temporal_state_recipe  # noqa: PLC0415

    supported = render_temporal_state_recipe(_MULTI_ISSUE_QUERY, current_date=CURRENT_DATE)
    assert supported and "[Member State History Recipe]" in supported

    for query, _span in BLOCKED_CASES:
        assert render_temporal_state_recipe(query, current_date=CURRENT_DATE) is None, (
            f"낮출 수 없는 {query!r} 에 형상 안내가 나갔다"
        )


def test_f_single_issue_synthesis_behaviour_is_unchanged() -> None:
    """F. 신고가 하나뿐인 기존 갈래는 그대로다(구제 + 그 신고 방면)."""

    query, span = "지난달 말 기준 VIP였던 회원을 찾아줘", "지난달 말 기준 VIP였던"
    structured, plan, result = _run(query, span)

    assert structured["audience_requirement"]["issues"] == []
    assert result["is_success"] is True, result.get("failure_reason")
    receipts = plan.get(semantic_requirements.SOURCE_REQUIREMENT_RECEIPTS_KEY) or []
    assert any(
        receipt.get("compiler") == "temporal_ir" and receipt.get("status") == "compiled"
        for receipt in receipts
    )
