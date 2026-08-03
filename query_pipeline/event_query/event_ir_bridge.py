"""타입 있는 실행 표현 ↔ 기존 :mod:`event_ir` 대수의 **무손실 왕복**.

새 계층을 세우면서 기존 IR 을 지우지 않는 이유는 단순하다: 사건 IR → SQL 렌더링은
:mod:`event_compiler` 가 이미 소유하고 있고, 그 렌더링에는 실CRM 물리 스키마의 세부
(반개구간 경계·char8 날짜·subject_column 바인딩·semi/anti 조인 전개)가 축적돼 있다.
그 지식을 새로 쓰는 것은 리팩터링이 아니라 재작성이고, 이 저장소에서 그 재작성의 값은
'같은 문장이 다른 오디언스를 뽑는다'이다.

그래서 경계를 이렇게 둔다:

    EventExpression(pydantic, 검증 완료)  ──to_event_ir──▶  event_ir.Condition
    event_ir.Condition(저장/legacy)       ──from_event_ir──▶ EventExpression

왕복이 무손실이라는 것은 계약 테스트가 노드 타입 전수로 강제한다. 손실이 생기면 그것은
'조용한 드롭'이므로 여기서 예외로 드러낸다 — 표현하지 못하는 노드는 절대 무시하지 않는다.
"""

from __future__ import annotations

from typing import Any

import event_ir
from query_pipeline.event_query import expressions as expr


class ExpressionBridgeError(ValueError):
    """두 대수 사이에서 노드를 무손실로 옮길 수 없다(축소 금지 — 여기서 멈춘다)."""


_COMPARISON_TO_IR: dict[expr.ComparisonOperator, str] = {
    expr.ComparisonOperator.EQ: "=",
    expr.ComparisonOperator.NEQ: "!=",
    expr.ComparisonOperator.GT: ">",
    expr.ComparisonOperator.GTE: ">=",
    expr.ComparisonOperator.LT: "<",
    expr.ComparisonOperator.LTE: "<=",
}
_COMPARISON_FROM_IR: dict[str, expr.ComparisonOperator] = {
    symbol: operator for operator, symbol in _COMPARISON_TO_IR.items()
}

_ARITHMETIC_TO_IR: dict[expr.ArithmeticOperator, str] = {
    expr.ArithmeticOperator.ADD: "+",
    expr.ArithmeticOperator.SUBTRACT: "-",
    expr.ArithmeticOperator.MULTIPLY: "*",
    expr.ArithmeticOperator.DIVIDE: "/",
}
_ARITHMETIC_FROM_IR: dict[str, expr.ArithmeticOperator] = {
    symbol: operator for operator, symbol in _ARITHMETIC_TO_IR.items()
}


def _comparison_symbol(operator: expr.ComparisonOperator) -> str:
    symbol = _COMPARISON_TO_IR.get(operator)
    if symbol is None:
        raise ExpressionBridgeError(
            f"사건 IR 은 비교 연산자 '{operator.value}' 를 표현하지 못합니다"
        )
    return symbol


def _evidence_to_ir(evidence: expr.SourceEvidence | None) -> event_ir.Evidence | None:
    if evidence is None:
        return None
    return event_ir.Evidence(text=evidence.text, start=evidence.start, end=evidence.end)


def _evidence_from_ir(evidence: event_ir.Evidence | None) -> expr.SourceEvidence | None:
    if evidence is None:
        return None
    return expr.SourceEvidence(text=evidence.text, start=evidence.start, end=evidence.end)


# ── 스칼라 ────────────────────────────────────────────────────────────────────────


def _operand_to_ir(operand: expr.EventOperand) -> event_ir.Scalar:
    if isinstance(operand, expr.LiteralOperand):
        if isinstance(operand.value, tuple):
            raise ExpressionBridgeError(
                "사건 IR 리터럴은 단일 스칼라만 담습니다(집합 리터럴은 IN/BETWEEN 전용)"
            )
        return event_ir.Literal(value=operand.value)
    if isinstance(operand, expr.AttributeOperand):
        return event_ir.FieldRef(name=operand.logical_name)
    if isinstance(operand, expr.ArithmeticOperand):
        return event_ir.Arithmetic(
            operator=_ARITHMETIC_TO_IR[operand.operator],
            left=_operand_to_ir(operand.left),
            right=_operand_to_ir(operand.right),
        )
    if isinstance(operand, expr.AggregateOperand):
        return event_ir.Aggregate(
            function=operand.function.value,
            relation=_relation_to_ir(operand.relation),
            expression=(
                _operand_to_ir(operand.expression) if operand.expression is not None else None
            ),
            distinct=operand.distinct,
        )
    raise ExpressionBridgeError(f"알 수 없는 피연산자입니다: {operand!r}")


def _operand_from_ir(scalar: event_ir.Scalar) -> expr.EventOperand:
    if isinstance(scalar, event_ir.Literal):
        return expr.LiteralOperand(value=scalar.value)
    if isinstance(scalar, event_ir.FieldRef):
        entity, _, attribute = scalar.name.partition(".")
        return expr.AttributeOperand(entity=entity, attribute=attribute)
    if isinstance(scalar, event_ir.Arithmetic):
        operator = _ARITHMETIC_FROM_IR.get(scalar.operator)
        if operator is None:
            raise ExpressionBridgeError(f"알 수 없는 산술 연산자입니다: {scalar.operator!r}")
        return expr.ArithmeticOperand(
            operator=operator,
            left=_operand_from_ir(scalar.left),
            right=_operand_from_ir(scalar.right),
        )
    if isinstance(scalar, event_ir.Aggregate):
        return expr.AggregateOperand(
            function=expr.AggregateFunction(scalar.function),
            relation=_relation_from_ir(scalar.relation),
            expression=(
                _operand_from_ir(scalar.expression) if scalar.expression is not None else None
            ),
            distinct=scalar.distinct,
        )
    raise ExpressionBridgeError(f"알 수 없는 스칼라 노드입니다: {scalar!r}")


# ── 시간 ──────────────────────────────────────────────────────────────────────────


def _window_to_ir(window: expr.TimeWindow) -> event_ir.TimeWindow:
    if isinstance(window, expr.AbsoluteWindow):
        return event_ir.AbsoluteInterval(
            start=window.start, end_exclusive=window.end_exclusive
        )
    if isinstance(window, expr.RollingWindow):
        return event_ir.RollingWindow(value=window.value, unit=window.unit.value)
    return event_ir.RelativeWindow(
        value=window.value, unit=window.unit.value, direction=window.direction
    )


def _window_from_ir(window: event_ir.TimeWindow) -> expr.TimeWindow:
    if isinstance(window, event_ir.AbsoluteInterval):
        return expr.AbsoluteWindow(start=window.start, end_exclusive=window.end_exclusive)
    if isinstance(window, event_ir.RollingWindow):
        return expr.RollingWindow(value=window.value, unit=expr.WindowUnit(window.unit))
    if isinstance(window, event_ir.RelativeWindow):
        return expr.RelativeWindow(
            value=window.value, unit=expr.WindowUnit(window.unit), direction="past"
        )
    raise ExpressionBridgeError(f"알 수 없는 시간 창입니다: {window!r}")


# ── 관계 ──────────────────────────────────────────────────────────────────────────


def _relation_to_ir(relation: expr.EventRelation) -> event_ir.Relation:
    if isinstance(relation, expr.EntityRelation):
        return event_ir.Source(name=relation.entity, correlation=relation.correlation.value)
    if isinstance(relation, expr.FilteredRelation):
        return event_ir.Filter(
            relation=_relation_to_ir(relation.relation),
            where=to_event_ir(relation.where),
        )
    if isinstance(relation, expr.JoinedRelation):
        joined_on = to_event_ir(relation.on)
        if not isinstance(joined_on, event_ir.Comparison):
            raise ExpressionBridgeError("join.on 은 비교 노드여야 합니다")
        return event_ir.Join(
            left=_relation_to_ir(relation.left),
            right=_relation_to_ir(relation.right),
            on=joined_on,
            kind=relation.join.value,
        )
    if isinstance(relation, expr.GroupedRelation):
        return event_ir.Group(
            relation=_relation_to_ir(relation.relation),
            keys=tuple(event_ir.FieldRef(name=key.logical_name) for key in relation.keys),
        )
    if isinstance(relation, expr.ProjectedRelation):
        return event_ir.Project(
            relation=_relation_to_ir(relation.relation),
            items=tuple(
                event_ir.NamedExpression(
                    name=item.name, expression=_operand_to_ir(item.expression)
                )
                for item in relation.items
            ),
        )
    if isinstance(relation, expr.SummarizedRelation):
        return event_ir.Summarize(
            relation=_relation_to_ir(relation.relation),
            keys=tuple(
                event_ir.NamedExpression(
                    name=key.name, expression=_operand_to_ir(key.expression)
                )
                for key in relation.keys
            ),
            measures=tuple(
                event_ir.NamedMeasure(
                    name=measure.name,
                    function=measure.function.value,
                    expression=(
                        _operand_to_ir(measure.expression)
                        if measure.expression is not None
                        else None
                    ),
                    distinct=measure.distinct,
                )
                for measure in relation.measures
            ),
        )
    if isinstance(relation, expr.OrderedRelation):
        return event_ir.Order(
            relation=_relation_to_ir(relation.relation),
            keys=tuple(
                event_ir.SortKey(name=key.name, direction=key.direction.value)
                for key in relation.keys
            ),
        )
    if isinstance(relation, expr.LimitedRelation):
        return event_ir.Limit(
            relation=_relation_to_ir(relation.relation), count=relation.count
        )
    raise ExpressionBridgeError(f"알 수 없는 관계 노드입니다: {relation!r}")


def _relation_from_ir(relation: event_ir.Relation) -> expr.EventRelation:
    if isinstance(relation, event_ir.Source):
        return expr.EntityRelation(
            entity=relation.name,
            correlation=expr.RelationCorrelation(relation.correlation),
        )
    if isinstance(relation, event_ir.Filter):
        return expr.FilteredRelation(
            relation=_relation_from_ir(relation.relation),
            where=from_event_ir(relation.where),
        )
    if isinstance(relation, event_ir.Join):
        joined_on = from_event_ir(relation.on)
        if not isinstance(joined_on, expr.ComparisonExpression):
            raise ExpressionBridgeError("join.on 은 비교 노드여야 합니다")
        return expr.JoinedRelation(
            left=_relation_from_ir(relation.left),
            right=_relation_from_ir(relation.right),
            on=joined_on,
            join=expr.JoinKind(relation.kind),
        )
    if isinstance(relation, event_ir.Group):
        keys: list[expr.AttributeOperand] = []
        for key in relation.keys:
            operand = _operand_from_ir(key)
            if not isinstance(operand, expr.AttributeOperand):
                raise ExpressionBridgeError("group key 는 속성 참조여야 합니다")
            keys.append(operand)
        return expr.GroupedRelation(
            relation=_relation_from_ir(relation.relation), keys=tuple(keys)
        )
    if isinstance(relation, event_ir.Project):
        return expr.ProjectedRelation(
            relation=_relation_from_ir(relation.relation),
            items=tuple(
                expr.NamedOperand(
                    name=item.name, expression=_operand_from_ir(item.expression)
                )
                for item in relation.items
            ),
        )
    if isinstance(relation, event_ir.Summarize):
        return expr.SummarizedRelation(
            relation=_relation_from_ir(relation.relation),
            keys=tuple(
                expr.NamedOperand(
                    name=key.name, expression=_operand_from_ir(key.expression)
                )
                for key in relation.keys
            ),
            measures=tuple(
                expr.NamedMeasure(
                    name=measure.name,
                    function=expr.AggregateFunction(measure.function),
                    expression=(
                        _operand_from_ir(measure.expression)
                        if measure.expression is not None
                        else None
                    ),
                    distinct=measure.distinct,
                )
                for measure in relation.measures
            ),
        )
    if isinstance(relation, event_ir.Order):
        return expr.OrderedRelation(
            relation=_relation_from_ir(relation.relation),
            keys=tuple(
                expr.RelationSortKey(
                    name=key.name, direction=expr.SortDirection(key.direction)
                )
                for key in relation.keys
            ),
        )
    if isinstance(relation, event_ir.Limit):
        return expr.LimitedRelation(
            relation=_relation_from_ir(relation.relation), count=relation.count
        )
    raise ExpressionBridgeError(f"알 수 없는 관계 노드입니다: {relation!r}")


# ── 조건 ──────────────────────────────────────────────────────────────────────────


def to_event_ir(expression: expr.EventExpression) -> event_ir.Condition:
    """타입 있는 실행 표현 → 기존 사건 IR. 표현 못 하는 노드는 예외다."""
    if isinstance(expression, expr.AggregateConditionExpression):
        return to_event_ir(expr.canonicalize_expression(expression))
    if isinstance(expression, expr.ComparisonExpression):
        return event_ir.Comparison(
            operator=_comparison_symbol(expression.operator),
            left=_operand_to_ir(expression.left),
            right=_operand_to_ir(expression.right),
            evidence=_evidence_to_ir(expression.evidence),
        )
    if isinstance(expression, expr.ExistsExpression):
        return event_ir.Exists(
            relation=_relation_to_ir(expression.relation),
            evidence=_evidence_to_ir(expression.evidence),
        )
    if isinstance(expression, expr.TimeWindowExpression):
        return event_ir.TimeFilter(
            field=event_ir.FieldRef(name=expression.attribute.logical_name),
            window=_window_to_ir(expression.window),
        )
    if isinstance(expression, expr.TemporalRelationExpression):
        return event_ir.TemporalRelation(
            operator=expression.operator.value,
            left=event_ir.EventReference(
                source=expression.left.entity, selector=expression.left.selector.value
            ),
            right=event_ir.EventReference(
                source=expression.right.entity, selector=expression.right.selector.value
            ),
            duration=event_ir.Duration(
                value=expression.duration.value, unit=expression.duration.unit.value
            ),
            evidence=_evidence_to_ir(expression.evidence),
        )
    if isinstance(expression, expr.NotExpression):
        return event_ir.Not(operand=to_event_ir(expression.expression))
    if isinstance(expression, expr.AndExpression):
        return event_ir.And(
            operands=tuple(to_event_ir(item) for item in expression.expressions)
        )
    if isinstance(expression, expr.OrExpression):
        return event_ir.Or(
            operands=tuple(to_event_ir(item) for item in expression.expressions)
        )
    raise ExpressionBridgeError(f"알 수 없는 조건 노드입니다: {expression!r}")


def from_event_ir(condition: event_ir.Condition) -> expr.EventExpression:
    """기존 사건 IR → 타입 있는 실행 표현."""
    if isinstance(condition, event_ir.Comparison):
        operator = _COMPARISON_FROM_IR.get(condition.operator)
        if operator is None:
            raise ExpressionBridgeError(f"알 수 없는 비교 연산자입니다: {condition.operator!r}")
        return expr.ComparisonExpression(
            operator=operator,
            left=_operand_from_ir(condition.left),
            right=_operand_from_ir(condition.right),
            evidence=_evidence_from_ir(condition.evidence),
        )
    if isinstance(condition, event_ir.Exists):
        return expr.ExistsExpression(
            relation=_relation_from_ir(condition.relation),
            evidence=_evidence_from_ir(condition.evidence),
        )
    if isinstance(condition, event_ir.TimeFilter):
        entity, _, attribute = condition.field.name.partition(".")
        return expr.TimeWindowExpression(
            attribute=expr.AttributeOperand(entity=entity, attribute=attribute),
            window=_window_from_ir(condition.window),
        )
    if isinstance(condition, event_ir.TemporalRelation):
        return expr.TemporalRelationExpression(
            operator=expr.TemporalOperator(condition.operator),
            left=expr.EventOccurrence(
                entity=condition.left.source,
                selector=expr.EventSelector(condition.left.selector),
            ),
            right=expr.EventOccurrence(
                entity=condition.right.source,
                selector=expr.EventSelector(condition.right.selector),
            ),
            duration=expr.DurationSpec(
                value=condition.duration.value,
                unit=expr.DurationUnit(condition.duration.unit),
            ),
            evidence=_evidence_from_ir(condition.evidence),
        )
    if isinstance(condition, event_ir.Not):
        return expr.NotExpression(expression=from_event_ir(condition.operand))
    if isinstance(condition, event_ir.And):
        return expr.AndExpression(
            expressions=tuple(from_event_ir(item) for item in condition.operands)
        )
    if isinstance(condition, event_ir.Or):
        return expr.OrExpression(
            expressions=tuple(from_event_ir(item) for item in condition.operands)
        )
    raise ExpressionBridgeError(f"알 수 없는 조건 노드입니다: {condition!r}")


def from_wire(payload: Any) -> expr.EventExpression:
    """저장된 wire dict → 타입 있는 실행 표현.

    닫힌 판별자 파싱은 :func:`event_ir.condition_from_dict` 하나만 소유한다 — 여기서 dict 를
    다시 읽으면 같은 문법의 파서가 둘이 되고, 둘은 반드시 갈라진다.
    """
    return from_event_ir(event_ir.condition_from_dict(payload))


def to_wire(expression: expr.EventExpression) -> dict[str, Any]:
    """타입 있는 실행 표현 → 저장/전송용 wire dict(기존 표기 그대로)."""
    payload: dict[str, Any] = to_event_ir(expression).to_dict()
    return payload


__all__ = [
    "ExpressionBridgeError",
    "from_event_ir",
    "from_wire",
    "to_event_ir",
    "to_wire",
]
