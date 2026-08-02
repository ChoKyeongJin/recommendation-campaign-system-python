"""후보 병합과 충돌 기록 — 어떤 생산자도 query_plan 을 직접 고치지 못하게 하는 지점.

이전 구조에서는 LLM 과 결정론 파서가 각자 최종 플랜을 썼고, 나중에 쓴 쪽이 이기거나
(fill-if-empty) 먼저 쓴 쪽이 이겼다. 어느 쪽이든 **선택의 근거가 기록되지 않았다**.

여기서는 모든 생산자가 `RequirementCandidate` 만 반환하고, resolver 가 병합한다.
같은 원문 근거(span)에 서로 다른 의미가 오면 조용히 하나를 고르지 않고 conflict 로 남긴다 —
그 결과는 status='ambiguous' 이고, 사용자에게 무엇이 모호한지 그대로 보인다.

순수 모듈 규약: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import semantic_plan
from semantic_plan import SemanticNode, SemanticPlanV2


@dataclass
class RequirementCandidate:
    """한 생산자가 제출한 '원문 구절 → 의미 노드' 후보."""

    source_span: str
    node: SemanticNode
    producer: str
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_span": self.source_span,
            "producer": self.producer,
            "confidence": self.confidence,
            "evidence": dict(self.evidence),
            "node": self.node.to_dict(),
        }


def _span_key(candidate: RequirementCandidate) -> str:
    """근거 동일성 판정 키. 좌표가 있으면 좌표, 없으면 공백 제거 문자열."""
    node = candidate.node
    if node.source_start is not None and node.source_end is not None:
        return f"{node.source_start}:{node.source_end}"
    return "".join((candidate.source_span or node.source_span or "").split())


def _semantic_key(node: SemanticNode) -> tuple[Any, ...]:
    """'같은 의미인가' 판정 키 — 타입 + 의미를 정하는 필드 값."""
    identity_fields = ("scope", "metric", "attribute", "entity", "relation", "operator", "direction")
    return (node.type, *(_hashable(node.values.get(name)) for name in identity_fields))


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    return value


def _conflict_payload(span: str, candidates: list[RequirementCandidate]) -> dict[str, Any]:
    return {
        "status": semantic_plan.STATUS_AMBIGUOUS,
        "failure_code": semantic_plan.AMBIGUOUS_REQUIREMENT,
        "source_span": span,
        "candidates": [
            {
                "producer": candidate.producer,
                "type": candidate.node.type,
                **{
                    key: candidate.node.values.get(key)
                    for key in ("scope", "metric", "attribute", "entity", "relation")
                    if candidate.node.values.get(key) is not None
                },
            }
            for candidate in candidates
        ],
    }


def resolve_candidates(
    candidates: Iterable[RequirementCandidate],
    *,
    source_query: str = "",
) -> SemanticPlanV2:
    """후보를 하나의 SemanticPlanV2 로 병합한다.

    규칙:
      - 같은 span + 같은 의미  → 하나로 합친다(신뢰도가 높은 쪽의 값을 채택, 빈 필드는 보완).
      - 같은 span + 다른 의미  → **양쪽 다 버리고** conflict 로 기록한다. 조용한 승자 선택 금지.
      - 다른 span            → 그대로 둘 다 남긴다.
    """
    grouped: dict[str, list[RequirementCandidate]] = {}
    order: list[str] = []
    for candidate in candidates:
        key = _span_key(candidate)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(candidate)

    plan = SemanticPlanV2(source_query=source_query)
    for key in order:
        group = grouped[key]
        by_meaning: dict[tuple[Any, ...], list[RequirementCandidate]] = {}
        for candidate in group:
            by_meaning.setdefault(_semantic_key(candidate.node), []).append(candidate)
        if len(by_meaning) > 1:
            span = group[0].source_span or group[0].node.source_span
            plan.conflicts.append(_conflict_payload(span, group))
            continue
        merged = _merge_same_meaning(next(iter(by_meaning.values())))
        plan.nodes.append(merged)
    return plan


def _merge_same_meaning(group: list[RequirementCandidate]) -> SemanticNode:
    """같은 의미의 후보들을 하나로 — 신뢰도 최상위를 기준으로 빈 필드만 보완한다.

    이것은 fill-if-empty 백필이 아니다: 대상이 query_plan 슬롯이 아니라 **같은 의미로
    판정된 동일 노드**이고, 값이 충돌하면(다른 의미) 애초에 여기 오지 않는다.
    """
    ranked = sorted(group, key=lambda item: (item.confidence, len(item.node.values)), reverse=True)
    primary = ranked[0]
    node = primary.node
    for other in ranked[1:]:
        for name, value in other.node.values.items():
            if name not in node.values or node.values[name] in (None, "", [], {}):
                node.values[name] = value
        if not node.source_span and other.node.source_span:
            node.source_span = other.node.source_span
        if node.source_start is None and other.node.source_start is not None:
            node.source_start = other.node.source_start
            node.source_end = other.node.source_end
    if node.confidence is None:
        node.confidence = primary.confidence or None
    return node


def candidates_from_plan(
    plan: SemanticPlanV2, *, producer: str = "llm", confidence: float = 1.0
) -> list[RequirementCandidate]:
    """SemanticPlanV2 를 후보 목록으로 되돌린다(병합 입력을 균질하게 만드는 어댑터)."""
    return [
        RequirementCandidate(
            source_span=node.source_span,
            node=node,
            producer=node.producer or producer,
            confidence=node.confidence if node.confidence is not None else confidence,
            evidence={"source_start": node.source_start, "source_end": node.source_end},
        )
        for node in plan.nodes
    ]


__all__ = [
    "RequirementCandidate",
    "candidates_from_plan",
    "resolve_candidates",
]
