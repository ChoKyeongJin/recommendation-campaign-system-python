"""Extractor 계층 — 각 파일이 **한 가지 의미 축**만 읽는다.

축을 나눈 기준은 '문장 유형'이 아니라 '의미의 종류'다. 문장 유형으로 나누면 문장이 하나
늘 때마다 파일이 하나 는다. 의미 축으로 나누면 새 문장은 **기존 축의 새 조합**이 된다 —
``최근 3개월`` + ``2번 이상`` + ``구매`` 는 세 축의 조합이지 새 규칙이 아니다.

extractor 는 서로를 모른다. 전역 상태를 공유하지 않고, 실행 순서에 의존하지 않으며,
겹치는 구간을 어떻게 처리할지도 결정하지 않는다(그 결정은 :mod:`nl_event_ir.parser` 의
스팬 중재와 :mod:`nl_event_ir.composer` 의 조합 규칙이 소유한다). 그래서 extractor 하나는
단독으로 테스트할 수 있다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from nl_event_ir.extractors.comparison import ComparisonExtractor
from nl_event_ir.extractors.count import CountExtractor
from nl_event_ir.extractors.event import EventExtractor
from nl_event_ir.extractors.logic import LogicExtractor, PolarityExtractor
from nl_event_ir.extractors.scope import ScopeExtractor, TargetExtractor
from nl_event_ir.extractors.sort import SortExtractor
from nl_event_ir.extractors.temporal import TemporalExtractor
from nl_event_ir.tokenizer import SemanticToken

__all__ = [
    "ComparisonExtractor",
    "CountExtractor",
    "EventExtractor",
    "LogicExtractor",
    "PolarityExtractor",
    "ResidualExtractor",
    "ScopeExtractor",
    "SortExtractor",
    "TargetExtractor",
    "TemporalExtractor",
    "TokenExtractor",
]


class TokenExtractor(Protocol):
    """정규화된 문장에서 자기 축의 토큰을 뽑는다."""

    def extract(self, text: str) -> list[SemanticToken]: ...


class ResidualExtractor(Protocol):
    """다른 extractor 가 읽고 **남은 자리**에서만 토큰을 뽑는 2단계 extractor.

    사전 없이 브랜드·상품명을 다루기 위한 장치다. 그 값들은 사전에 없으므로 '아는 낱말'로는
    찾을 수 없고, 대신 '아무도 안 읽어 간 자리'가 후보가 된다. 정체 판정은 extractor 가
    아니라 repository 의 몫이다.
    """

    def extract_residual(
        self,
        text: str,
        tokens: Sequence[SemanticToken],
    ) -> list[SemanticToken]: ...
