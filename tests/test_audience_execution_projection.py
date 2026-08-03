"""`_derive_audience_execution` 의 **행동 스냅샷** — 이관 전의 동치 판정 기준.

이 파일은 정답을 주장하지 않는다. 217줄 함수가 요구 검증(a)과 legacy plan 키 투영(b)을
함께 하고 있고, 그 함수를 `DefaultRequirementResolver` 로 옮기는 작업의 성공 조건이
"동치"인데 **지금은 동치를 증명할 수단이 없다**. 그래서 이관 **전에** 현재 산출을 전량
고정한다(`docs/plans_query_pipeline_debt.md` Phase 0-1).

고정하는 것은 그 함수가 쓰는 plan 키 6개 + 결정 로그다.

    event_expression                  성공 시 기록 / 미해결 시 pop
    semantic_ir                       status · failure_kind · missing_fields · 사용자 문구
    audience_unsupported_hypotheses   미지원 선언이 실행 자산에 반박당했고 SemanticPlan 이 이어받음
    audience_execution_assets         자산은 선언됐는데 그 축을 낼 생산자가 없음(registry gap)
    audience_requirement.expression   정규화(창 종류 보정 등)를 거친 뒤의 표현
    audience_requirement.issues       모델 신고 + 애플리케이션 계산의 합
    decisions                         plan_decisions.record 가 남긴 결정 로그

`semantic_ir.failure_kind` 3분기(structurer_failure / user_clarification / system_failure)가
사용자에게 나가는 결말을 결정하므로, 한 칸이 어긋나면 사용자가 보는 답이 바뀐다 — 그래서
분기 이름이 아니라 **값 전체**를 고정한다.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)

CURRENT_DATE = "2026-08-03"

# 투영이 건드리는 plan 키. 이 목록이 스냅샷의 범위다.
PROJECTED_KEYS = (
    "event_expression",
    "semantic_ir",
    "audience_unsupported_hypotheses",
    "audience_execution_assets",
    "audience_requirement.expression",
    "audience_requirement.issues",
    "decisions",
)


def _evidence(query: str, text: str) -> dict[str, Any]:
    start = query.index(text)
    return {"text": text, "start": start, "end": start + len(text)}


@dataclass(frozen=True)
class Case:
    """입력 payload 하나와 그것이 재현하는 갈래의 이름."""

    name: str
    query: str
    requirement: dict[str, Any]
    nodes: list[dict[str, Any]] = field(default_factory=list)


def _run(case: Case) -> dict[str, Any]:
    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None,
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": copy.deepcopy(case.requirement),
            "semantic_plan": {"nodes": copy.deepcopy(case.nodes)},
        },
        case.query,
        current_date=CURRENT_DATE,
    )


def projection(case: Case) -> dict[str, Any]:
    """plan 에서 투영 6키(+결정 로그)만 뽑는다. 없는 키는 ``None`` 으로 자리를 남긴다."""
    plan = _run(case)
    requirement = plan.get("audience_requirement") or {}
    return {
        "event_expression": plan.get("event_expression"),
        "semantic_ir": plan.get("semantic_ir"),
        "audience_unsupported_hypotheses": plan.get("audience_unsupported_hypotheses"),
        "audience_execution_assets": plan.get("audience_execution_assets"),
        "audience_requirement.expression": requirement.get("expression"),
        "audience_requirement.issues": requirement.get("issues"),
        "decisions": plan.get("decisions"),
    }


# ── 대표 6종 ──────────────────────────────────────────────────────────────────────

READY_QUERY = "최근 30일 동안 구매한 회원을 찾아줘"
MISSING_QUERY = "최근 구매한 회원을 찾아줘"
AMBIGUOUS_QUERY = "구매를 많이 한 회원을 찾아줘"
CONTRADICTED_QUERY = "골드에서 VIP로 바뀌고 이메일 수신에 동의한 회원을 찾아줘"
GAP_QUERY = "이메일 수신에 동의한 회원을 찾아줘"
MODEL_OMISSION_QUERY = "누적 구매금액 상위 10% 회원을 추출해줘"

# 창 종류를 **일부러 틀리게** 낸다('최근 30일'은 rolling 인데 relative 로). 애플리케이션이
# 소유한 종류로 맞춰 넣는 정규화(`apply_window_kinds`)와 그 결정 기록까지 한 갈래에서 고정된다.
_READY_EXPRESSION: dict[str, Any] = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {
                "type": "relative",
                "value": 30,
                "unit": "day",
                "direction": "past",
            },
        },
    },
    "evidence": _evidence(READY_QUERY, "최근 30일 동안 구매한"),
}

_GRADE_TRANSITION_NODE: dict[str, Any] = {
    "id": "r1",
    "type": "relation_predicate",
    "subject": "member",
    "attribute": "member_grade",
    "relation": "transition",
    "from_value": "골드",
    "to_value": "VIP",
    **{
        "source_span": "골드에서 VIP로 바뀌고",
        "source_start": CONTRADICTED_QUERY.index("골드에서 VIP로 바뀌고"),
        "source_end": CONTRADICTED_QUERY.index("골드에서 VIP로 바뀌고")
        + len("골드에서 VIP로 바뀌고"),
    },
}

_CONSENT_ISSUE_MESSAGE = "이메일 수신 동의 조건은 Canonical Event IR로 표현할 수 없습니다."

CASES: tuple[Case, ...] = (
    Case(
        name="ready",
        query=READY_QUERY,
        requirement={"expression": _READY_EXPRESSION, "issues": []},
    ),
    Case(
        name="missing-argument",
        query=MISSING_QUERY,
        requirement={
            "expression": None,
            "issues": [
                {
                    "code": "missing_argument",
                    "argument": "period",
                    "message": "'최근'의 범위를 확정할 기간 값이 필요합니다.",
                    "evidence": _evidence(MISSING_QUERY, "최근"),
                }
            ],
        },
    ),
    Case(
        name="ambiguous-requirement",
        query=AMBIGUOUS_QUERY,
        requirement={
            "expression": None,
            "issues": [
                {
                    "code": "ambiguous_requirement",
                    "argument": "threshold",
                    "message": "'많이'의 기준이 확정되지 않았습니다.",
                    "evidence": _evidence(AMBIGUOUS_QUERY, "많이"),
                }
            ],
        },
    ),
    # 미지원 선언이 실행 자산에 반박당했고, 그 축을 낼 SemanticPlan 노드가 **있다**
    # → 가설로 내리고 False 를 돌려줘 SemanticPlan 경로가 이어받는다.
    Case(
        name="unsupported-contradicted",
        query=CONTRADICTED_QUERY,
        requirement={
            "expression": None,
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "email_consent_flag",
                    "message": _CONSENT_ISSUE_MESSAGE,
                    "evidence": _evidence(CONTRADICTED_QUERY, "이메일 수신에 동의한"),
                }
            ],
        },
        nodes=[_GRADE_TRANSITION_NODE],
    ),
    # 같은 반박인데 그 축을 낼 **생산자가 없다** → 미지원이 아니라 레지스트리 구멍
    # (`failure_kind="system_failure"`).
    Case(
        name="unsupported-registry-gap",
        query=GAP_QUERY,
        requirement={
            "expression": None,
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "email_consent_flag",
                    "message": _CONSENT_ISSUE_MESSAGE,
                    "evidence": _evidence(GAP_QUERY, "이메일 수신에 동의한"),
                }
            ],
        },
    ),
    # 결핍의 원인이 사용자가 아니라 모델이다(원문에 '10%' 가 있다) → 되묻지 않고 재방출.
    Case(
        name="model-omission",
        query=MODEL_OMISSION_QUERY,
        requirement={
            "expression": None,
            "issues": [
                {
                    "code": "missing_argument",
                    "argument": "percentage",
                    "message": "percentage 값을 확정하지 못했습니다.",
                    "evidence": _evidence(MODEL_OMISSION_QUERY, "상위 10%"),
                }
            ],
        },
    ),
)

# ── 고정값 ────────────────────────────────────────────────────────────────────────
#
# `_READY_EXPRESSION` 이 정규화를 거친 뒤의 모습. 창 종류가 relative → rolling 으로 바뀌었고
# `direction` 이 사라졌다(rolling 창에는 없는 필드다).
_NORMALIZED_READY_EXPRESSION: dict[str, Any] = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {"type": "rolling", "value": 30, "unit": "day"},
        },
    },
    "evidence": {"text": "최근 30일 동안 구매한", "start": 0, "end": 13},
}

_RESOLVED_SEMANTIC_IR: dict[str, Any] = {
    "status": "resolved",
    "operations": [],
    "missing_fields": [],
    "missing_field_causes": [],
    "failure_kind": None,
    "policy_applications": [],
    "unsupported_operations": [],
    "message": None,
}

_CONSENT_ISSUE: dict[str, Any] = {
    "code": "unsupported_semantics",
    "argument": "email_consent_flag",
    "message": _CONSENT_ISSUE_MESSAGE,
}

EXPECTED: dict[str, dict[str, Any]] = {
    "ready": {
        "event_expression": {
            "expression": _NORMALIZED_READY_EXPRESSION,
            "source": "audience_requirement",
            "receipts": [
                {
                    "node_id": "audience-atom-0",
                    "fingerprint": (
                        "19ed321fd9a6d359520f415178245447c4af130530986fe9fe4722cc71c70498"
                    ),
                    "status": "compiled",
                    "polarity": "positive",
                    "sources": ["purchase"],
                }
            ],
        },
        "semantic_ir": _RESOLVED_SEMANTIC_IR,
        "audience_unsupported_hypotheses": None,
        "audience_execution_assets": None,
        "audience_requirement.expression": _NORMALIZED_READY_EXPRESSION,
        "audience_requirement.issues": [],
        "decisions": [
            {
                "seq": 0,
                "filter": "canonical_audience_claims.window_kind",
                "action": "update",
                "slot": "audience_requirement.window.type",
                "reason": (
                    "기간 표현 종류는 애플리케이션 소유 — 30day 창을 'relative'에서 'rolling'로 맞췄다"
                ),
                "value": {"value": 30, "unit": "day", "from": "relative", "to": "rolling"},
            }
        ],
    },
    "missing-argument": {
        "event_expression": None,
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["audience.period"],
            "missing_field_causes": [
                {
                    "field": "audience.period",
                    "path": "audience_requirement.period",
                    "source_span": "최근",
                    "node_type": None,
                    "cause": "user_omission",
                }
            ],
            "failure_kind": "user_clarification",
            "policy_applications": [],
            "unsupported_operations": [],
            "message": "'최근'의 범위를 확정할 기간 값이 필요합니다.",
        },
        "audience_unsupported_hypotheses": None,
        "audience_execution_assets": None,
        "audience_requirement.expression": None,
        "audience_requirement.issues": [
            {
                "code": "missing_argument",
                "argument": "period",
                "message": "'최근'의 범위를 확정할 기간 값이 필요합니다.",
                "evidence": {"text": "최근", "start": 0, "end": 2},
            }
        ],
        "decisions": None,
    },
    "ambiguous-requirement": {
        "event_expression": None,
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["audience.threshold"],
            "missing_field_causes": [
                {
                    "field": "audience.threshold",
                    "path": "audience_requirement.threshold",
                    "source_span": "많이",
                    "node_type": None,
                    "cause": "user_omission",
                }
            ],
            "failure_kind": "user_clarification",
            "policy_applications": [],
            "unsupported_operations": [],
            "message": "'많이'의 기준이 확정되지 않았습니다.",
        },
        "audience_unsupported_hypotheses": None,
        "audience_execution_assets": None,
        "audience_requirement.expression": None,
        "audience_requirement.issues": [
            {
                "code": "ambiguous_requirement",
                "argument": "threshold",
                "message": "'많이'의 기준이 확정되지 않았습니다.",
                "evidence": {"text": "많이", "start": 4, "end": 6},
            }
        ],
        "decisions": None,
    },
    # `semantic_ir` 이 resolved 인 이유: 이 갈래는 `False` 를 돌려주고 SemanticPlan 경로가
    # 이어받는다. 즉 미지원 선언은 **가설로만** 남고 결말은 등급 전이 노드가 낸다.
    "unsupported-contradicted": {
        "event_expression": None,
        "semantic_ir": _RESOLVED_SEMANTIC_IR,
        "audience_unsupported_hypotheses": [
            {
                "kind": "email_consent_flag",
                "reason": _CONSENT_ISSUE_MESSAGE,
                "evidence": "이메일 수신에 동의한",
            }
        ],
        "audience_execution_assets": None,
        "audience_requirement.expression": None,
        "audience_requirement.issues": [
            {**_CONSENT_ISSUE, "evidence": {"text": "이메일 수신에 동의한", "start": 14, "end": 25}}
        ],
        "decisions": None,
    },
    "unsupported-registry-gap": {
        "event_expression": None,
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["audience.requirement"],
            "missing_field_causes": [],
            "failure_kind": "system_failure",
            "policy_applications": [],
            "unsupported_operations": [],
            "message": (
                "요청한 조건을 처리할 실행 자산은 선언돼 있으나 이 경로로 낼 수 없습니다"
                "(선언된 자산: email_optin)."
            ),
        },
        "audience_unsupported_hypotheses": None,
        "audience_execution_assets": [
            {
                "argument": "email_consent_flag",
                "evidence": "이메일 수신에 동의한",
                "assets": [
                    {
                        "layer": "member_filter",
                        "symbol": "email_optin",
                        "surface": "이메일 수신 동의",
                    }
                ],
            }
        ],
        "audience_requirement.expression": None,
        "audience_requirement.issues": [
            {**_CONSENT_ISSUE, "evidence": {"text": "이메일 수신에 동의한", "start": 0, "end": 11}}
        ],
        "decisions": None,
    },
    "model-omission": {
        "event_expression": None,
        "semantic_ir": {
            "status": "needs_clarification",
            "operations": [],
            "missing_fields": ["audience.percentage"],
            "missing_field_causes": [
                {
                    "field": "audience.percentage",
                    "path": "audience_requirement.percentage",
                    "source_span": "상위 10%",
                    "node_type": None,
                    "cause": "model_omission",
                    "literal_spans": [(11, 14)],
                }
            ],
            "failure_kind": "structurer_failure",
            "policy_applications": [],
            "unsupported_operations": [],
            "message": "percentage 값을 확정하지 못했습니다.",
        },
        "audience_unsupported_hypotheses": None,
        "audience_execution_assets": None,
        "audience_requirement.expression": None,
        "audience_requirement.issues": [
            {
                "code": "missing_argument",
                "argument": "percentage",
                "message": "percentage 값을 확정하지 못했습니다.",
                "evidence": {"text": "상위 10%", "start": 8, "end": 14},
            }
        ],
        "decisions": None,
    },
}


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_projection_is_unchanged(case: Case) -> None:
    assert projection(case) == EXPECTED[case.name]


def test_every_projected_key_is_covered() -> None:
    """스냅샷이 6키를 실제로 **한 번씩은 비어 있지 않게** 관측하는지 확인한다.

    전부 ``None`` 인 키가 있으면 그 키의 회귀는 이 파일이 잡지 못한다 — 안전망이 있다고
    믿는 상태로 이관하는 것이 이 Phase 가 막으려는 것이다.
    """
    observed = {key: False for key in PROJECTED_KEYS}
    for expected in EXPECTED.values():
        for key, value in expected.items():
            if value:
                observed[key] = True
    assert all(observed.values()), f"관측되지 않은 투영 키: {sorted(k for k, v in observed.items() if not v)}"
