"""의미 토큰 — extractor 의 출력이자 composer 의 입력.

extractor 가 곧바로 Event IR 을 만들지 않는 이유는 **조합 결정을 한 곳에 모으기 위해서**다.
extractor 가 각자 IR 을 만들면 '이 시간 표현이 어느 사건에 붙는가' 같은 결정이 여러 파일에
흩어지고, 그러면 문장 유형이 하나 늘 때마다 여러 파일을 함께 고쳐야 한다.

토큰은 **원문의 어느 구간을 무엇으로 읽었는지**를 증거로 들고 다닌다. 이 스팬 정보가
composer 의 근접성 판단(어느 조건에 붙일지)과 잔여 구간 계산(무엇이 아직 안 읽혔는지)의
근거가 된다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "SemanticToken",
    "TokenKind",
    "covered_offsets",
    "sort_tokens",
    "spans_overlap",
    "uncovered_spans",
]


class TokenKind(StrEnum):
    """토큰의 종류.

    ``StrEnum`` 이므로 ``token.kind == "EVENT"`` 같은 문자열 비교도 그대로 동작한다 —
    기존 호출부를 깨지 않으면서 오타를 잡기 위한 선택이다.
    """

    EVENT = "EVENT"
    TARGET = "TARGET"
    TIME_WINDOW = "TIME_WINDOW"
    DATE_RANGE = "DATE_RANGE"
    ENTITY_TYPE = "ENTITY_TYPE"
    ENTITY_VALUE = "ENTITY_VALUE"
    COUNT_CONDITION = "COUNT_CONDITION"
    AGGREGATE = "AGGREGATE"
    POLARITY = "POLARITY"
    LOGIC = "LOGIC"
    SORT = "SORT"
    COMPARISON = "COMPARISON"
    # 형태는 알아봤지만 값으로 만들 수 없는 구간(예: 달력에 없는 날짜 ``2026-02-30``).
    # 이런 구간을 토큰 없이 버리면 그 조건이 통째로 사라진 채 IR 이 정상처럼 보인다 —
    # 기간 조건이 사라지면 조회 범위가 조용히 전체로 넓어진다. 원문을 보존해 상위 단계가
    # 명시적으로 실패하게 한다(CLAUDE.md 11).
    UNPARSED = "UNPARSED"


@dataclass(frozen=True, slots=True)
class SemanticToken:
    """정규화 텍스트의 ``[start, end)`` 구간을 하나의 의미로 읽은 결과.

    ``value`` 가 ``object`` 인 것은 종류마다 담는 타입이 다르기 때문이다(EVENT 는
    :class:`~nl_event_ir.enums.EventType`, TIME_WINDOW 는 시간 창 객체 …). composer 는
    ``kind`` 로 분기한 뒤에만 ``value`` 를 좁히므로, 이 ``object`` 가 타입 검사를 우회하는
    구멍이 되지는 않는다.
    """

    kind: TokenKind
    value: object
    start: int
    end: int
    raw_text: str
    confidence: float = 1.0
    source: str = "rule"

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(
                f"invalid token span [{self.start}, {self.end}) for kind={self.kind}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


def spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """두 반열린 구간이 한 글자라도 겹치는가."""

    return left[0] < right[1] and right[0] < left[1]


def sort_tokens(tokens: Iterable[SemanticToken]) -> tuple[SemanticToken, ...]:
    """문서 순서로 안정 정렬한다.

    보조 키까지 정하는 이유는 재현성이다(CLAUDE.md 63). 같은 자리에서 시작하는 토큰이
    여럿일 때 순서가 실행마다 달라지면 composer 의 결과도 달라진다.
    """

    return tuple(
        sorted(tokens, key=lambda token: (token.start, token.end, token.kind.value))
    )


def covered_offsets(tokens: Iterable[SemanticToken]) -> frozenset[int]:
    """토큰들이 이미 읽어 간 글자 위치 집합."""

    covered: set[int] = set()
    for token in tokens:
        covered.update(range(token.start, token.end))
    return frozenset(covered)


def uncovered_spans(
    text: str,
    tokens: Sequence[SemanticToken],
    *,
    minimum_length: int = 1,
) -> tuple[tuple[int, int], ...]:
    """아무 토큰도 읽지 않은 **잔여 구간**을 반환한다.

    이 함수가 사전 없는 엔티티 추출의 토대다. 브랜드명·상품명은 사전에 없으므로 '아는
    낱말'로는 찾을 수 없다. 대신 규칙이 읽어 낸 것을 전부 걷어내고 **남은 자리**를 후보로
    보고, 그 후보의 정체는 repository 가 판정한다.
    """

    covered = covered_offsets(tokens)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(len(text)):
        if index in covered:
            if start is not None:
                spans.append((start, index))
                start = None
            continue
        if start is None:
            start = index
    if start is not None:
        spans.append((start, len(text)))
    return tuple(span for span in spans if span[1] - span[0] >= minimum_length)
