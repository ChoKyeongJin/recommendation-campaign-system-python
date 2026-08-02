"""원문 coverage 검증 — LLM 이 조건 노드를 **통째로 빠뜨린 사건**을 슬롯 백필 없이 잡는다.

이전 구조에서 '방출 실패'(지원되는 조건인데 LLM 이 안 채움)의 대응은 결정론 백필이었다.
그러면 같은 문장을 두 번 해석하게 되고, 어느 해석이 이겼는지가 코드 순서에 숨는다.

여기서는 **누락 자체를 검출**하고 그 구간만 다시 추출한다:

    1차 SemanticPlan 추출 → coverage 검증 → uncovered span 식별
    → 그 span 만 재추출 → 병합 → 전체 재검증

검증 방법(중요 — 이건 의미 파서가 아니다):
  - 앵커는 애플리케이션이 이미 소유한 **값 원자**다(날짜창·수량·퍼센트·비교어 —
    `query_structurer.semantic_ir.extract_literal_bindings`). 값의 의미는 보지 않고
    "이 값을 어떤 노드가 근거로 삼았는가"만 본다.
  - 값이 없는 의미 연산자(반복·집합·스냅샷)는 기존 요구 원장
    (`semantic_requirements.capture_source_semantic_obligations`)이 앵커를 준다.
  - 다른 소유자(V4 슬롯 계층 등)가 이미 근거로 청구한 구간은 `claimed_spans` 로 주입받아
    커버된 것으로 본다 — 소유자가 여럿인 이행기에 거짓 누락 보고를 만들지 않기 위해서다.

Coverage verifier 는 query plan 슬롯을 채우지 않는다. 판단만 하고 결과를 돌려준다.

순수 모듈 규약: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import semantic_plan
from semantic_plan import SemanticPlanV2

_CLAUSE_SPLIT_RE = re.compile(r"[,.;\n]|\s(?:그리고|또는|이고|이며)\s")
# 값 원자 중 조건 근거가 아닌 것(순수 표기)은 앵커에서 뺀다 — 비교어는 단독으로 조건을
# 만들지 않고 반드시 수량과 함께 오므로 중복 보고만 만든다.
_NON_ANCHOR_LITERAL_KINDS = frozenset({"comparison_operator"})


@dataclass(frozen=True)
class Anchor:
    """원문에서 조건의 존재를 알리는 구간."""

    start: int
    end: int
    text: str
    kind: str


@dataclass
class CoverageReport:
    uncovered_requirements: list[dict[str, Any]] = field(default_factory=list)
    ungrounded_nodes: list[dict[str, Any]] = field(default_factory=list)
    contested_spans: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not (self.uncovered_requirements or self.ungrounded_nodes or self.contested_spans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uncovered_requirements": list(self.uncovered_requirements),
            "ungrounded_nodes": list(self.ungrounded_nodes),
            "contested_spans": list(self.contested_spans),
            "is_complete": self.is_complete,
        }


def _literal_anchors(query: str) -> list[Anchor]:
    from query_structurer.semantic_ir import extract_literal_bindings  # 순환 없음

    anchors: list[Anchor] = []
    for literal in extract_literal_bindings(query):
        kind = str(literal.get("kind") or "")
        if kind in _NON_ANCHOR_LITERAL_KINDS:
            continue
        start, end = literal.get("start"), literal.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        anchors.append(Anchor(start=start, end=end, text=str(literal.get("text") or ""), kind=f"literal:{kind}"))
    return anchors


def _obligation_anchors(query: str) -> list[Anchor]:
    import semantic_requirements  # 순수 모듈

    anchors: list[Anchor] = []
    for requirement in semantic_requirements.capture_source_semantic_obligations(query):
        span = getattr(requirement, "source_span", None)
        start, end = _span_bounds(span)
        if start is None or end is None:
            continue
        anchors.append(
            Anchor(
                start=start,
                end=end,
                text=query[start:end],
                kind=f"obligation:{(requirement.base or {}).get('name', 'unknown')}",
            )
        )
    return anchors


def _span_bounds(span: Any) -> tuple[int | None, int | None]:
    if isinstance(span, dict):
        start, end = span.get("start"), span.get("end")
    elif isinstance(span, (list, tuple)) and len(span) == 2:
        start, end = span
    else:
        return None, None
    if isinstance(start, int) and isinstance(end, int):
        return start, end
    return None, None


def source_anchors(query: str) -> list[Anchor]:
    """원문의 조건 앵커 전체(값 원자 + 값 없는 의미 연산자 의무)."""
    anchors = [*_literal_anchors(query), *_obligation_anchors(query)]
    return sorted(anchors, key=lambda item: (item.start, item.end))


def _node_spans(plan: SemanticPlanV2, query: str) -> list[tuple[int, int, str]]:
    """노드가 청구한 원문 구간. 좌표가 없으면 source_span 문자열로 되찾는다."""
    spans: list[tuple[int, int, str]] = []
    for node in plan.walk():
        start, end = node.source_start, node.source_end
        text = node.source_span or ""
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(query)
                and query[start:end] == text):
            if not text:
                continue
            found = query.find(text)
            if found < 0:
                continue
            start, end = found, found + len(text)
        spans.append((start, end, node.id))
    return spans


def clause_bounds(query: str, position: int) -> tuple[int, int]:
    """앵커가 속한 절의 경계 — 재추출 요청 단위다."""
    boundaries = [0]
    for match in _CLAUSE_SPLIT_RE.finditer(query):
        boundaries.append(match.end())
    start = max((bound for bound in boundaries if bound <= position), default=0)
    ends = [match.start() for match in _CLAUSE_SPLIT_RE.finditer(query) if match.start() > position]
    end = min(ends) if ends else len(query)
    return start, end


def verify_coverage(
    query: str,
    plan: SemanticPlanV2,
    *,
    claimed_spans: Iterable[tuple[int, int]] = (),
) -> CoverageReport:
    """원문 조건 앵커가 SemanticPlan 노드에 대응하는지 판정한다(플랜을 바꾸지 않는다)."""
    report = CoverageReport()
    if not isinstance(query, str) or not query.strip():
        return report

    node_spans = _node_spans(plan, query)
    external = [tuple(span) for span in claimed_spans if isinstance(span, (tuple, list)) and len(span) == 2]

    # ① AST 노드가 실제 원문 근거를 갖는가.
    for node in plan.walk():
        if node.type == semantic_plan.LogicalExpression.TYPE:
            continue
        text = node.source_span or ""
        if not text.strip():
            report.ungrounded_nodes.append(
                {"node_id": node.id, "type": node.type, "reason": "source_span 이 비어 있음"}
            )
        elif text not in query:
            report.ungrounded_nodes.append(
                {"node_id": node.id, "type": node.type, "reason": "source_span 이 원문에 없음",
                 "source_span": text}
            )

    # ② 동일 span 이 서로 다른 의미의 노드 여러 개에 잘못 연결되었는가.
    by_span: dict[tuple[int, int], list[str]] = {}
    for start, end, node_id in node_spans:
        by_span.setdefault((start, end), []).append(node_id)
    for (start, end), node_ids in by_span.items():
        if len(node_ids) < 2:
            continue
        types = {node.type for node in plan.walk() if node.id in node_ids}
        if len(types) > 1:
            report.contested_spans.append(
                {"source_span": query[start:end], "node_ids": node_ids, "types": sorted(types)}
            )

    # ③ 원문의 조건 앵커가 어떤 노드에도 대응하지 않는가.
    # 기간을 소유한 노드는 자기 절의 날짜 리터럴을 함께 청구한다 — 근거 구절이 조건부만
    # 가리키고 기간이 절 앞머리에 있는 흔한 형태('2019년 3월 같은 상품을 …')에서 정상
    # 요청이 누락으로 오보고되는 것을 막는다.
    period_clauses = _period_owner_clauses(query, plan, node_spans)
    uncovered_by_clause: dict[tuple[int, int], list[Anchor]] = {}
    for anchor in source_anchors(query):
        if _covered(anchor, node_spans) or _covered_external(anchor, external):
            continue
        if anchor.kind == "literal:date_window" and any(
            start <= anchor.start and anchor.end <= end for start, end in period_clauses
        ):
            continue
        bounds = clause_bounds(query, anchor.start)
        uncovered_by_clause.setdefault(bounds, []).append(anchor)
    for (start, end), anchors in sorted(uncovered_by_clause.items()):
        report.uncovered_requirements.append(
            {
                "source_span": query[start:end].strip(),
                "source_start": start,
                "source_end": end,
                "anchors": [{"text": anchor.text, "kind": anchor.kind} for anchor in anchors],
                "reason": "대응하는 의미 노드가 없음",
            }
        )
    return report


_PERIOD_FIELD_KINDS = frozenset({"period"})


def _period_owner_clauses(
    query: str, plan: SemanticPlanV2, node_spans: Sequence[tuple[int, int, str]]
) -> list[tuple[int, int]]:
    """기간 필드를 실제로 채운 노드의 절 경계 목록."""
    spans_by_id = {node_id: (start, end) for start, end, node_id in node_spans}
    clauses: list[tuple[int, int]] = []
    for node in plan.walk():
        owns_period = any(
            spec.kind in _PERIOD_FIELD_KINDS and node.values.get(spec.name) not in (None, "", {}, [])
            for spec in type(node).FIELDS
        )
        if not owns_period or node.id not in spans_by_id:
            continue
        clauses.append(clause_bounds(query, spans_by_id[node.id][0]))
    return clauses


def _covered(anchor: Anchor, node_spans: Sequence[tuple[int, int, str]]) -> bool:
    return any(start <= anchor.start and anchor.end <= end for start, end, _ in node_spans)


def _covered_external(anchor: Anchor, claimed: Sequence[tuple[int, int]]) -> bool:
    return any(
        isinstance(start, int) and isinstance(end, int) and start <= anchor.start and anchor.end <= end
        for start, end in claimed
    )


__all__ = [
    "Anchor",
    "CoverageReport",
    "clause_bounds",
    "source_anchors",
    "verify_coverage",
]
