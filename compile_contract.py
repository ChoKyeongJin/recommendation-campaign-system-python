"""컴파일 계약 — 코어와 도메인 컴파일러 사이의 **타입만** 담은 경계.

코어 파이프라인은 "무엇이 컴파일됐는가"를 알아야 하지만 "어느 도메인 슬롯인가"는 몰라야
한다. 그 둘을 가르는 것이 이 파일이다:

    CompileContext  실행 지식 주입(슬롯 shape·닫힌 어휘·지표 해소기) — 내용은 도메인이 채운다
    CompileResult   컨테이너별 산출물 + 노드→슬롯 대장 + 실패 목록

`containers` 는 `{컨테이너 이름: {슬롯: 값}}` 이고, 빈 문자열 키는 플랜 루트를 뜻한다.
코어는 이 dict 를 그대로 실행 플랜에 옮길 뿐, 어떤 이름이 들어 있는지 해석하지 않는다.

범용 코어 규약: graph_rag 도, 도메인 모듈도 import 하지 않는다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Mapping

PLAN_ROOT = ""  # 컨테이너 없이 플랜 루트에 쓰는 슬롯의 키.


@dataclass
class CompileContext:
    """컴파일에 필요한 실행 지식(주입 — 컴파일러는 레지스트리를 스스로 열지 않는다)."""

    slot_shapes: Mapping[str, Any] = field(default_factory=dict)
    allowed: Mapping[str, Any] = field(default_factory=dict)
    # 의미 노드 필드의 닫힌 어휘(`node_field_vocabularies` 선언이 가리키는 키 → 값 목록).
    # `allowed` 와 별개인 이유: 이쪽은 **LLM 에 노출되는 어휘 이름**으로 키가 잡혀 있고,
    # 타입 판정의 discriminator 역인덱스가 그 이름을 그대로 쓴다.
    node_vocabularies: Mapping[str, Any] = field(default_factory=dict)
    today: date | None = None
    # 지표 표면 → id 해소기(도메인별). 없으면 노드 값을 그대로 쓴다.
    metric_resolvers: Mapping[str, Callable[[Any], str | None]] = field(default_factory=dict)
    # 집계 도메인 → 슬롯 라우팅 override(설정으로 확장 가능한 지점).
    scope_slots: Mapping[str, str] = field(default_factory=dict)
    # 도메인 → **정수 카운트** 지표 id 집합. 카운트 위에서만 임계 비교가 존재/부재로 환원된다
    # (COUNT>0 ≡ 있음, COUNT=0 ≡ 없음). 합계·비율 지표에는 그 환원이 성립하지 않으므로
    # 이 집합에 없는 지표는 임계 경로 그대로 간다. 무엇이 카운트인지는 도메인 설정이 선언하고,
    # 코어는 "카운트인가"만 묻는다(지표 이름을 알지 못한다).
    count_metrics: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def resolve_metric(self, domain: str, surface: Any) -> str | None:
        resolver = self.metric_resolvers.get(domain)
        if resolver is None:
            return str(surface) if isinstance(surface, str) and surface.strip() else None
        return resolver(surface)


@dataclass
class CompileResult:
    """컴파일 산출물. 플랜을 직접 변형하지 않고 '무엇을 어느 컨테이너에 쓸지'를 돌려준다."""

    containers: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 노드 id → 채운 슬롯 경로(감사·소유권 대장).
    node_slots: dict[str, str] = field(default_factory=dict)
    # 컴파일하지 못한 노드(사유는 실패 분류 코드).
    failures: list[dict[str, Any]] = field(default_factory=list)

    def container(self, name: str = PLAN_ROOT) -> dict[str, Any]:
        """이름 붙은 컨테이너(없으면 만든다). 컴파일러가 슬롯을 쓰는 유일한 통로."""
        return self.containers.setdefault(name, {})

    @property
    def is_empty(self) -> bool:
        return not any(self.containers.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "containers": copy.deepcopy(self.containers),
            "node_slots": dict(self.node_slots),
            "failures": copy.deepcopy(self.failures),
        }


__all__ = ["PLAN_ROOT", "CompileContext", "CompileResult"]
