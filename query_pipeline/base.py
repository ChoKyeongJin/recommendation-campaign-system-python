"""계층 공통 기반 — 모델 베이스·시계·식별자 발급기.

이 모듈은 **어느 계층에도 속하지 않는다**. requirement / event_query / planning /
compiler 넷이 모두 여기를 import 하고, 여기는 넷 중 어느 것도 import 하지 않는다
(:mod:`tests.test_query_pipeline_layering` 가 전수 강제).

시계와 식별자를 주입 가능한 프로토콜로 두는 이유: 요구 → 실행 사양 변환은 '지난달'
같은 상대 표현을 절대 구간으로 확정하고 사양에 id/타임스탬프를 남긴다. 그 두 값이
``date.today()`` / ``uuid4()`` 로 모듈 안에서 결정되면 같은 입력이 실행할 때마다 다른
사양을 만들고, 테스트는 달 경계에서만 깨지는 종류의 실패를 갖게 된다.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """이 패키지의 모든 모델이 따르는 계약.

    ``extra="forbid"`` — 모르는 키를 조용히 흘리지 않는다. 이 저장소에서 반복된 사고가
    "생산자가 새 키를 붙였는데 소비자가 못 읽어 조건이 사라진 SQL 이 성공으로 나갔다"이고,
    그 사고는 열린 payload 에서만 생긴다.
    ``frozen=True``  — 계층 경계를 넘은 뒤 in-place 로 변형되지 않는다(현행 query_plan dict
    의 인플레이스 변형이 IR 결합의 진앙이라는 감사 결론을 그대로 반영한다).
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
    )


@runtime_checkable
class Clock(Protocol):
    """'지금'의 단일 공급자. 상대 기간 확정과 사양 타임스탬프가 같은 값을 본다."""

    def now(self) -> datetime: ...


class SystemClock:
    """운영 기본값 — UTC aware datetime."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """테스트용 고정 시계(주입해 달 경계 의존을 제거한다)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@runtime_checkable
class IdFactory(Protocol):
    """식별자 발급기. 요구/사양의 id 는 감사 단위이므로 재현 가능해야 한다."""

    def new_id(self, prefix: str) -> str: ...


class UuidIdFactory:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4()}"


class SequentialIdFactory:
    """테스트용 결정론 발급기(``req-1``, ``req-2`` …)."""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count[int]] = {}

    def new_id(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}-{next(counter)}"


__all__ = [
    "Clock",
    "FixedClock",
    "IdFactory",
    "SequentialIdFactory",
    "StrictModel",
    "SystemClock",
    "UuidIdFactory",
]
