"""capability 판정의 실행 자산 쪽 구현.

"이 조건을 표현할 수 있는가"의 판정 주체는 **실행 자산**이지 모델이 아니다(이 저장소의
반복된 실측: 모델이 '미지원'이라 선언한 조건을 처리하는 컴파일러가 살아 있었고 호출조차
되지 않았다). 그래서 :class:`~query_pipeline.requirement.resolver.CapabilityProfile` 은
요구 계층의 **프로토콜**로만 존재하고, 그 구현은 컴파일러 계층에 있다.
"""

from __future__ import annotations

from collections.abc import Iterable

from query_pipeline.compiler.sql_compiler import (
    SCALAR_COMPARISON_OPERATORS,
    SET_COMPARISON_OPERATORS,
    SUPPORTED_AGGREGATE_FUNCTIONS,
    SUPPORTED_WINDOW_KINDS,
)

# 사건 IR 이 말할 수 있는 창의 종류. 아래 두 선언이 이 집합의 부분집합으로만 갈린다.
_WINDOW_KINDS: frozenset[str] = frozenset({"interval", "rolling", "relative"})

# 사건 IR 엔진(event_compiler)이 표현하는 노드/연산자/집계/창.
# 목록이 아니라 **파생**이 되도록 가능한 한 실제 표에서 뽑는다.
_EVENT_IR_NODES: frozenset[str] = frozenset(
    {
        "node.comparison",
        "node.exists",
        "node.time_filter",
        "node.temporal_relation",
        "node.not",
        "node.and",
        "node.or",
        "node.attribute",
        "node.literal",
        "node.arithmetic",
        "node.aggregate",
        "node.entity",
        "node.filter",
        "node.join",
        "node.group",
        "node.project",
        "node.summarize",
        "node.order",
        "node.limit",
        "node.limit.count",
        "node.limit.percent",
        "node.occurrence",
        "node.duration",
        # 창 노드는 두 이름으로 보고된다: 노드 종류(node.*)와 창의 종류(window.*).
        # 둘 다 있어야 "이 창을 표현할 수 있는가"가 노드 순회만으로 답해진다.
        "node.interval",
        "node.rolling",
        "node.relative",
        "window.interval",
        "window.rolling",
        "window.relative",
    }
)

_EVENT_IR_OPERATORS: frozenset[str] = frozenset(
    f"operator.{operator.value}" for operator in SCALAR_COMPARISON_OPERATORS
)
_EVENT_IR_AGGREGATES: frozenset[str] = frozenset(
    f"aggregate.{function.value}" for function in SUPPORTED_AGGREGATE_FUNCTIONS
)

EVENT_IR_CAPABILITIES: frozenset[str] = (
    _EVENT_IR_NODES | _EVENT_IR_OPERATORS | _EVENT_IR_AGGREGATES
)

# 일반 컴파일러는 사건 IR 엔진이 못 하는 집합 연산자를 파라미터 바인딩으로 표현하고,
# 반대로 시간 관계와 **렌더가 없는 창**은 표현하지 못한다. 창 목록은 손으로 적지 않고
# 렌더 표(:data:`SUPPORTED_WINDOW_KINDS`)의 차집합으로 파생한다.
_UNRENDERED_WINDOWS: frozenset[str] = _WINDOW_KINDS - SUPPORTED_WINDOW_KINDS

# 괄호가 의미를 바꾼다: ``-`` 가 ``|`` 보다 먼저 묶이므로 괄호 없이 쓰면 뺄셈이 오른쪽
# 집합에만 걸리고 EVENT_IR_CAPABILITIES 의 항목은 **하나도 빠지지 않는다**. 그러면 이 선언은
# "빼겠다"고 적어 놓고 아무것도 빼지 않는 장식이 되고, 컴파일러가 표현하지 못하는 노드가
# capability 검사를 통과해 컴파일 단계에서야 실패한다(사유를 잃은 실패다).
GENERIC_SQL_CAPABILITIES: frozenset[str] = (
    EVENT_IR_CAPABILITIES
    | frozenset(f"operator.{operator.value}" for operator in SET_COMPARISON_OPERATORS)
) - (
    frozenset({"node.temporal_relation"})
    | frozenset({"node.limit.percent"})
    | frozenset(f"node.{kind}" for kind in _UNRENDERED_WINDOWS)
    | frozenset(f"window.{kind}" for kind in _UNRENDERED_WINDOWS)
)


class DeclaredCapabilityProfile:
    """선언된 집합만 지원한다. 모르는 이름은 **지원하지 않음**(fail-close)."""

    def __init__(self, capabilities: Iterable[str]) -> None:
        self._capabilities = frozenset(capabilities)

    def supports(self, capability: str) -> bool:
        return capability in self._capabilities

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities


def event_ir_capability_profile() -> DeclaredCapabilityProfile:
    return DeclaredCapabilityProfile(EVENT_IR_CAPABILITIES)


def generic_sql_capability_profile() -> DeclaredCapabilityProfile:
    return DeclaredCapabilityProfile(GENERIC_SQL_CAPABILITIES)


__all__ = [
    "EVENT_IR_CAPABILITIES",
    "GENERIC_SQL_CAPABILITIES",
    "DeclaredCapabilityProfile",
    "event_ir_capability_profile",
    "generic_sql_capability_profile",
]
