"""Canonical IR 과 Lowering Planner 사이의 **확정 계층**.

    Canonical Request IR
        ↓ Semantic Issue Detector      detection.py   무엇이 비었/모호한가
        ↓ Resolution Policy            policy.py      자동 확정 / 되묻기 / 미지원
        ├→ Clarification Planner       clarification.py  ASK_USER 를 질문으로
        └→ Auto Resolution / Applier   applier.py · slots.py  값을 그 슬롯에만
        ↓ (고정점 반복)                loop.py
    Lowering Planner → SQL Compiler

목적은 한 문장이다.

    자연어가 조금 부족해도 **운영 정책으로 안전하게 확정할 수 있는 것은 자동으로 채워 SQL 까지
    진행하고**, 정책으로 정하면 결과가 크게 달라지는 것만 사용자에게 묻는다.

이 계층이 지키는 불변식(테스트가 계약으로 잰다: ``tests/test_resolution_invariants.py``)

    A 운영 기본값은 :mod:`resolution.policy` 밖에서 만들어지지 않는다.
    B 질문 생성기 입력은 ``ASK_USER`` 결정뿐이다.
    C 자동 확정된 결핍은 질문이 되지 않는다.
    D 미지원 결핍은 질문이 되지 않는다.
    E 답변은 그 결핍의 슬롯 밖 의미를 바꾸지 않는다.
    F 답변 처리는 원문을 다시 해석하지 않는다(정규화기 의존 없음).
    G 사용자 명시가 아닌 의미가 SQL 에 있으면 provenance 영수증이 반드시 있다.
    H HIGH 위험 모호성은 명시적 답변 없이 사라지지 않는다.
"""

from __future__ import annotations

from resolution.applier import (
    ApplicationResult,
    apply_auto_resolutions,
    apply_clarification,
    render_entity_binding_instruction,
)
from resolution.clarification import (
    ClarificationAnswer,
    ClarificationInvariantViolation,
    ClarificationOption,
    ClarificationPlan,
    ClarificationQuestion,
    legacy_questions,
    parse_answers,
    plan_clarifications,
)
from resolution.config import (
    ResolutionConfigError,
    ResolutionMode,
    ResolutionPolicyConfig,
    resolved_config,
)
from resolution.detection import detect_semantic_issues
from resolution.issues import (
    IssueCandidate,
    IssueFamily,
    ResolutionRisk,
    SemanticIssue,
    SourceSpan,
)
from resolution.loop import (
    NeedsClarification,
    ResolutionInvariantViolation,
    ResolutionOutcome,
    ResolvedRequest,
    UnsupportedResolution,
    resolve_request,
)
from resolution.observability import log_resolution, resolution_metrics
from resolution.policy import (
    ResolutionAction,
    ResolutionDecision,
    ResolutionDecisions,
    ResolutionPolicy,
)
from resolution.projection import (
    RESOLUTION_KEY,
    legacy_clarification_questions,
    resolution_payload,
)
from resolution.request import (
    CanonicalRequest,
    EntityBinding,
    ResolutionProvenance,
    ResolvedAssumption,
)
from resolution.runtime import clarification_scope, current_answers

__all__ = [
    "RESOLUTION_KEY",
    "ApplicationResult",
    "CanonicalRequest",
    "ClarificationAnswer",
    "ClarificationInvariantViolation",
    "ClarificationOption",
    "ClarificationPlan",
    "ClarificationQuestion",
    "EntityBinding",
    "IssueCandidate",
    "IssueFamily",
    "NeedsClarification",
    "ResolutionAction",
    "ResolutionConfigError",
    "ResolutionDecision",
    "ResolutionDecisions",
    "ResolutionInvariantViolation",
    "ResolutionMode",
    "ResolutionOutcome",
    "ResolutionPolicy",
    "ResolutionPolicyConfig",
    "ResolutionProvenance",
    "ResolutionRisk",
    "ResolvedAssumption",
    "ResolvedRequest",
    "SemanticIssue",
    "SourceSpan",
    "UnsupportedResolution",
    "apply_auto_resolutions",
    "apply_clarification",
    "clarification_scope",
    "current_answers",
    "detect_semantic_issues",
    "legacy_clarification_questions",
    "legacy_questions",
    "log_resolution",
    "parse_answers",
    "plan_clarifications",
    "render_entity_binding_instruction",
    "resolution_metrics",
    "resolution_payload",
    "resolve_request",
    "resolved_config",
]
