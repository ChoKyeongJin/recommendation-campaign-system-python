"""엔티티 해석 계약 — 값의 정체는 사전이 아니라 **데이터**가 정한다.

이 파일에는 구현이 없고 계약만 있다. 그래야 테스트는 인메모리 구현을, 운영은 실제 카탈로그
repository 를 같은 자리에 꽂을 수 있다(CLAUDE.md 28·29: 조회를 연산자 안에 숨기지 않고
주입한다).

:class:`EntityResolution` 이 후보 **목록**을 들고 다니는 것이 요점이다. 후보가 여럿일 때
하나를 골라 버리면 그 선택의 근거가 사라지고, 틀렸을 때 되짚을 수 없다. 고르지 못한 상태도
정상적인 도메인 결과이므로 예외가 아니라 값으로 표현한다(CLAUDE.md 11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nl_event_ir.enums import EntityType

__all__ = ["EntityCandidate", "EntityRepository", "EntityResolution"]


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    """repository 가 돌려준 후보 하나."""

    entity_type: EntityType
    entity_id: str
    canonical_name: str
    score: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"entity score must be in [0, 1], got {self.score}")


class EntityRepository(Protocol):
    """브랜드·상품·카테고리 카탈로그 조회 인터페이스.

    ``entity_types`` 가 주어지면 그 종류로 좁혀 검색한다. ``None`` 이면 전체를 본다.
    반환 순서는 **결정론적**이어야 한다 — 같은 입력에 순서가 흔들리면 동점 후보 중
    무엇이 선택되는지가 실행마다 달라진다(CLAUDE.md 63).
    """

    def search(
        self,
        text: str,
        entity_types: tuple[EntityType, ...] | None = None,
    ) -> list[EntityCandidate]: ...


@dataclass(frozen=True, slots=True)
class EntityResolution:
    """한 값 후보에 대한 해석 결과.

    ``selected`` 가 ``None`` 인 경우는 두 가지이며 둘은 다르게 다뤄져야 한다.

    * ``candidates`` 가 비어 있음 — 카탈로그에 없는 말. 조건이 아니라 잉여어일 수 있다.
    * ``candidates`` 가 여럿 — 진짜 모호. 사용자에게 되물어야 하는 상태다.
    """

    raw_value: str
    candidates: tuple[EntityCandidate, ...]
    selected: EntityCandidate | None = None

    @property
    def is_ambiguous(self) -> bool:
        return self.selected is None and len(self.candidates) > 1

    @property
    def is_unknown(self) -> bool:
        return not self.candidates
