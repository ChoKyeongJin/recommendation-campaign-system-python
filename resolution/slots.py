"""의미 슬롯의 **적용기** — 확정된 값 하나를 canonical IR 의 그 자리에만 넣는다.

이 모듈이 지키는 계약은 하나다.

    한 결핍의 답은 **그 결핍이 가리킨 슬롯 밖의 의미를 바꾸지 않는다**(§34-E).

그래서 적용은 트리 전체 재생성이 아니라 **자리 교체**다. grain 답변은 임계 비교 하나만
낮추고(:func:`grain_claims.regrain_to_row` 가 절 소유권을 지킨다), 기간 답변은 창이 없는 절
하나에만 :class:`event_ir.TimeFilter` 를 씌운다. 다른 절의 창·값·극성은 그대로 남는다.

두 번째 계약은 **정직한 적용 가능성**이다. 적용기가 "나는 이 자리를 못 고친다"고 말할 수
있어야 정책이 자동 확정을 약속하지 않는다(:meth:`SlotApplier.can_apply`). 이 질문이 없으면
정책은 채울 수 없는 값을 AUTO_RESOLVE 로 약속하고, 고정점 루프는 같은 결핍을 영원히 다시
본다 — :mod:`resolution.loop` 가 invariant 위반으로 닫는 바로 그 상태다.

원문을 다시 읽지 않는다. 이 모듈은 :mod:`query_structurer` 도 정규화기도 import 하지 않으며,
그 사실은 아키텍처 테스트가 계약으로 잰다(§34-F).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import event_ir
import grain_claims
from resolution.issues import GRAIN_ROW, GRAIN_SUBJECT, SemanticIssue, SourceSpan
from resolution.request import CanonicalRequest, EntityBinding


@dataclass(frozen=True, slots=True)
class SlotPatch:
    """적용 결과. 적용하지 못했으면 ``reason`` 이 그 이유를 코드로 들고 있다."""

    request: CanonicalRequest
    applied: bool
    reason: str | None = None


class SlotApplier(Protocol):
    """슬롯 하나를 확정하는 방법. 값은 받고, 원문은 보지 않는다."""

    def can_apply(self, request: CanonicalRequest, issue: SemanticIssue) -> bool: ...

    def apply(
        self, request: CanonicalRequest, issue: SemanticIssue, value: Any
    ) -> SlotPatch: ...


# ── 조건 트리 국소 교체 ───────────────────────────────────────────────────────────


def _replace_nodes(node: Any, transform: Callable[[Any], Any | None]) -> Any:
    """``transform`` 이 새 노드를 주면 그 자리만 바꾸고, 아니면 자식으로 내려간다.

    결합자(And/Or/Not)와 비교의 양변까지만 내려간다 — 교체 대상(사건 존재·집계)은 통째로
    바뀌므로 그 안쪽을 다시 조립할 이유가 없고, 조립하지 않는 만큼 다른 절이 변형될 여지도 없다.
    """

    swapped = transform(node)
    if swapped is not None:
        return swapped
    if isinstance(node, event_ir.And | event_ir.Or):
        return type(node)(
            operands=tuple(_replace_nodes(item, transform) for item in node.operands)
        )
    if isinstance(node, event_ir.Not):
        return event_ir.Not(operand=_replace_nodes(node.operand, transform))
    if isinstance(node, event_ir.Comparison):
        return event_ir.Comparison(
            operator=node.operator,
            left=_replace_nodes(node.left, transform),
            right=_replace_nodes(node.right, transform),
            evidence=node.evidence,
        )
    return node


def _parse(request: CanonicalRequest) -> Any | None:
    if not request.has_expression:
        return None
    try:
        return event_ir.condition_from_dict(dict(request.expression or {}))
    except event_ir.IrSchemaError:
        return None


def _spans_overlap(left: SourceSpan | None, right: Any) -> bool:
    """두 근거 구간이 겹치는가. 한쪽이라도 없으면 겹침을 **주장하지 않는다**."""

    if left is None or right is None:
        return False
    start, end = getattr(right, "start", None), getattr(right, "end", None)
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return max(left.start, start) < min(left.end, end)


# ── grain ────────────────────────────────────────────────────────────────────────


class GrainSlotApplier:
    """임계값의 적용 grain 을 확정한다. row 는 낮추고, subject 는 이미 그 모양이다."""

    def can_apply(self, request: CanonicalRequest, issue: SemanticIssue) -> bool:
        expression = _parse(request)
        if expression is None:
            return False
        return bool(grain_claims.regrainable_thresholds(expression))

    def apply(
        self, request: CanonicalRequest, issue: SemanticIssue, value: Any
    ) -> SlotPatch:
        expression = _parse(request)
        if expression is None:
            return SlotPatch(request, False, "expression_unavailable")
        if value == GRAIN_SUBJECT:
            # 트리가 이미 실현한 모양이다. 값을 확정했다는 사실만 남고 IR 은 바뀌지 않는다.
            return SlotPatch(request, True, None)
        if value != GRAIN_ROW:
            return SlotPatch(request, False, f"unknown_grain:{value!r}")
        regrained = grain_claims.regrain_to_row(expression)
        if regrained is None:
            return SlotPatch(request, False, "row_grain_not_lowerable")
        return SlotPatch(request.with_expression(regrained.to_dict()), True, None)


# ── 기간 ─────────────────────────────────────────────────────────────────────────


def _window_from_value(value: Any) -> event_ir.TimeWindow | None:
    """확정된 기간 값 → 창. 해석되지 않으면 ``None``(비슷한 값으로 대신하지 않는다)."""

    if isinstance(value, event_ir.RollingWindow | event_ir.AbsoluteInterval | event_ir.RelativeWindow):
        return value
    if isinstance(value, Mapping):
        try:
            return event_ir.window_from_dict(dict(value))
        except event_ir.IrSchemaError:
            return None
    return None


def _source_name(relation: Any) -> str | None:
    for node in event_ir.walk(relation):
        if isinstance(node, event_ir.Source):
            return node.name
    return None


def _has_time_filter(relation: Any) -> bool:
    return any(isinstance(node, event_ir.TimeFilter) for node in event_ir.walk(relation))


class PeriodSlotApplier:
    """창이 없는 관측 자리 **하나**에 관측 창을 씌운다.

    자리는 두 가지다 — 사건의 존재(:class:`event_ir.Exists`)와 사건의 집계
    (:class:`event_ir.Aggregate`). 둘을 함께 보는 이유는 '최근 20만원 이상 구매'처럼 기간이
    집계의 모집단에 걸리는 문장이 흔하기 때문이다. 존재만 보면 그 문장에서 정책이 값을
    만들어 놓고 넣지 못한다.

    창이 없는 자리가 여럿이면 근거 구간이 가리키는 하나로 좁히고, 그래도 하나가 아니면
    적용하지 않는다 — 어느 절의 기간인지 모르는 채 고르는 것이 곧 추측이다(§12).
    """

    def _targets(self, expression: Any, issue: SemanticIssue) -> list[Any]:
        found = [
            node
            for node in event_ir.walk(expression)
            if isinstance(node, event_ir.Exists | event_ir.Aggregate)
            and not _has_time_filter(node.relation)
        ]
        if len(found) <= 1:
            return found
        return [
            node
            for node in found
            if _spans_overlap(issue.evidence, getattr(node, "evidence", None))
        ]

    def can_apply(self, request: CanonicalRequest, issue: SemanticIssue) -> bool:
        expression = _parse(request)
        if expression is None:
            return False
        return len(self._targets(expression, issue)) == 1

    def apply(
        self, request: CanonicalRequest, issue: SemanticIssue, value: Any
    ) -> SlotPatch:
        expression = _parse(request)
        if expression is None:
            return SlotPatch(request, False, "expression_unavailable")
        window = _window_from_value(value)
        if window is None:
            return SlotPatch(request, False, "period_value_unreadable")
        targets = self._targets(expression, issue)
        if len(targets) != 1:
            return SlotPatch(request, False, f"period_target_count:{len(targets)}")
        target = targets[0]
        source = _source_name(target.relation)
        if source is None:
            return SlotPatch(request, False, "period_target_has_no_source")

        def transform(node: Any) -> Any | None:
            if node is not target:
                return None
            windowed = event_ir.Filter(
                relation=node.relation,
                where=event_ir.TimeFilter(
                    field=event_ir.time_field(source), window=window
                ),
            )
            if isinstance(node, event_ir.Exists):
                return event_ir.Exists(relation=windowed, evidence=node.evidence)
            return event_ir.Aggregate(
                function=node.function,
                relation=windowed,
                expression=node.expression,
                distinct=node.distinct,
            )

        patched = _replace_nodes(expression, transform)
        return SlotPatch(request.with_expression(patched.to_dict()), True, None)


# ── 엔터티 값 ────────────────────────────────────────────────────────────────────


class EntityBindingApplier:
    """엔터티 이름 답변을 **타입 있는 결속**으로 기록한다.

    표현이 서지 못해 되묻는 경우(가장 흔하다)에는 값을 넣을 canonical 자리가 아직 없다. 그때
    답변 문자열을 원문에 이어 붙여 다시 해석하면 원래 확정돼 있던 의미까지 흔들린다(§15).
    대신 결속으로 들고 있다가, 표현을 만드는 계층에 **애플리케이션 소유 값**으로 전달한다
    (:func:`resolution.applier.render_entity_binding_instruction`).

    자동 확정에는 절대 쓰이지 않는다 — 엔터티 식별자는 HIGH 위험이라 어느 모드에서도 정책이
    고르지 않는다(§34-H).
    """

    def can_apply(self, request: CanonicalRequest, issue: SemanticIssue) -> bool:
        return bool(issue.entity_type)

    def apply(
        self, request: CanonicalRequest, issue: SemanticIssue, value: Any
    ) -> SlotPatch:
        if not isinstance(value, str) or not value.strip():
            return SlotPatch(request, False, "entity_value_empty")
        if not issue.entity_type:
            return SlotPatch(request, False, "entity_type_missing")
        binding = EntityBinding(
            issue_id=issue.issue_id,
            entity_type=issue.entity_type,
            value=value.strip(),
            evidence=issue.evidence,
        )
        return SlotPatch(request.with_entity_binding(binding), True, None)


#: 슬롯 이름 → 적용기. 정확 일치가 먼저이고, 없으면 접두어로 찾는다.
SLOT_APPLIERS: dict[str, SlotApplier] = {
    "comparison.grain": GrainSlotApplier(),
    "audience.period": PeriodSlotApplier(),
}

#: 접두어 적용기(엔터티 슬롯은 argument 이름이 붙어 정확 일치가 되지 않는다).
SLOT_PREFIX_APPLIERS: tuple[tuple[str, SlotApplier], ...] = (
    ("predicate.", EntityBindingApplier()),
)


def applier_for(slot: str) -> SlotApplier | None:
    applier = SLOT_APPLIERS.get(slot)
    if applier is not None:
        return applier
    for prefix, prefix_applier in SLOT_PREFIX_APPLIERS:
        if slot.startswith(prefix):
            return prefix_applier
    return None


def can_apply(request: CanonicalRequest, issue: SemanticIssue) -> bool:
    applier = applier_for(issue.slot)
    return applier is not None and applier.can_apply(request, issue)


def apply_slot(
    request: CanonicalRequest, issue: SemanticIssue, value: Any
) -> SlotPatch:
    applier = applier_for(issue.slot)
    if applier is None:
        return SlotPatch(request, False, f"no_applier_for_slot:{issue.slot}")
    return applier.apply(request, issue, value)


__all__ = [
    "SLOT_APPLIERS",
    "SLOT_PREFIX_APPLIERS",
    "EntityBindingApplier",
    "GrainSlotApplier",
    "PeriodSlotApplier",
    "SlotApplier",
    "SlotPatch",
    "applier_for",
    "apply_slot",
    "can_apply",
]
