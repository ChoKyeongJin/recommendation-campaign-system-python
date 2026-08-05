"""비교·금액 조건 추출 — ``얼마``와 ``어떤 부등호``를 읽는다.

금액에는 ``float`` 를 쓰지 않는다(CLAUDE.md 14). ``100000원`` 은 :class:`~decimal.Decimal`
로 만들고, 변환은 :func:`parse_amount` 한 곳에서만 일어난다 — 암묵적 타입 변환을 여러 곳에
흩으면 ``"10"``·``10``·``Decimal("10")`` 이 슬그머니 같은 값처럼 취급된다(CLAUDE.md 20).

집계 종류(합계/평균/최대/최소)는 **문장에 표지가 있을 때만** 확정한다. ``주문 금액이
100000원 이상`` 처럼 표지가 없으면 '건별 금액'인지 '기간 합계'인지 문장만으로는 결정되지
않는다. 이때 조용히 하나를 고르지 않고, 기간 합계로 읽되 **신뢰도를 낮춰** 표시한다 —
그 표시를 검증기가 경고로 올린다(CLAUDE.md 12: 의미가 불명확하면 추측을 감추지 않는다).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from nl_event_ir.aliases import AliasRegistry, AliasSection
from nl_event_ir.enums import ComparisonOperator, MetricType
from nl_event_ir.models import MetricCondition
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["ComparisonExtractor", "parse_amount"]

# 통화 단위와 배수. 문법(단위 체계)이지 동의어가 아니므로 코드가 소유한다.
_AMOUNT_UNITS: dict[str, int] = {
    "원": 1,
    "만원": 10_000,
    "억원": 100_000_000,
}

# 집계 표지가 없을 때 쓰는 신뢰도. 1.0 미만이라는 사실 자체가 '가정이 들어갔다'는 신호다.
_ASSUMED_AGGREGATION_CONFIDENCE = 0.7


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(word) for word in sorted(words, key=lambda w: (-len(w), w)))


def parse_amount(digits: str, unit: str) -> Decimal | None:
    """``100,000`` + ``원`` → ``Decimal('100000')``. 변환 실패는 ``None`` 이다."""

    try:
        base = Decimal(digits.replace(",", ""))
    except InvalidOperation:
        return None
    multiplier = _AMOUNT_UNITS.get(unit)
    if multiplier is None:
        return None
    return base * multiplier


class ComparisonExtractor:
    """금액 조건(AGGREGATE)과 단독 비교어(COMPARISON)를 추출한다."""

    def __init__(self, registry: AliasRegistry) -> None:
        self._registry = registry
        comparison_alternation = registry.alternation(AliasSection.COMPARISON)
        self._comparison_re = re.compile(f"({comparison_alternation})")
        self._metric_re = re.compile(f"({registry.alternation(AliasSection.METRIC)})")

        # 금액 조건. 스팬은 **숫자에서 시작한다** — 앞의 금액 명사까지 삼키면 그 명사에 붙어
        # 있는 사건 낱말이 함께 먹힌다. ``구매금액이 100000원 이상`` 에서 명사까지 매치하면
        # 스팬 중재(긴 쪽 우선)가 ``구매`` EVENT 토큰을 지워 문장 전체가 해석 불능이 됐다.
        # 금액 축이라는 사실은 통화 단위(``원``)가 이미 말해 주고, 집계 종류는 아래
        # :meth:`_resolve_metric_type` 의 앞쪽 탐색이 판정한다.
        self._amount_re = re.compile(
            rf"(?P<value>\d[\d,]*)\s*(?P<unit>{_alternation(tuple(_AMOUNT_UNITS))})"
            rf"\s*(?P<operator>{comparison_alternation})"
        )

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        claimed: list[tuple[int, int]] = []

        for match in self._amount_re.finditer(text):
            token = self._build_amount_token(text, match)
            if token is None:
                continue
            tokens.append(token)
            claimed.append(match.span())

        # 남은 단독 비교어. '2회 이상' 안의 '이상' 처럼 다른 토큰에 포함되는 것은
        # parser 의 스팬 중재가 걸러 낸다 — 여기서 다른 축을 넘겨다보지 않는다.
        for match in self._comparison_re.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in claimed):
                continue
            canonical = self._registry.lookup(AliasSection.COMPARISON, match.group(1))
            if canonical is None:  # pragma: no cover
                continue
            tokens.append(
                SemanticToken(
                    kind=TokenKind.COMPARISON,
                    value=ComparisonOperator(canonical),
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                )
            )
        return tokens

    def _build_amount_token(self, text: str, match: re.Match[str]) -> SemanticToken | None:
        amount = parse_amount(match.group("value"), match.group("unit"))
        if amount is None:  # pragma: no cover - 교대가 단위표에서 나왔으므로 도달 불가
            return None
        canonical_operator = self._registry.lookup(
            AliasSection.COMPARISON, match.group("operator")
        )
        if canonical_operator is None:  # pragma: no cover
            return None

        metric_type, confidence = self._resolve_metric_type(text, match.start())
        return SemanticToken(
            kind=TokenKind.AGGREGATE,
            value=MetricCondition(
                metric_type=metric_type,
                operator=ComparisonOperator(canonical_operator),
                value=amount,
            ),
            start=match.start(),
            end=match.end(),
            raw_text=match.group(0),
            confidence=confidence,
        )

    def _resolve_metric_type(self, text: str, amount_start: int) -> tuple[MetricType, float]:
        """금액 앞쪽에 집계 표지가 있으면 그것을 쓰고, 없으면 합계로 읽되 신뢰도를 낮춘다."""

        window_start = max(0, amount_start - _METRIC_MARKER_LOOKBEHIND)
        preceding = text[window_start:amount_start]
        found = list(self._metric_re.finditer(preceding))
        if not found:
            return MetricType.SUM, _ASSUMED_AGGREGATION_CONFIDENCE
        canonical = self._registry.lookup(AliasSection.METRIC, found[-1].group(1))
        if canonical is None:  # pragma: no cover
            return MetricType.SUM, _ASSUMED_AGGREGATION_CONFIDENCE
        return MetricType(canonical), 1.0


# 집계 표지를 찾아볼 앞쪽 범위(글자 수). 절 하나를 넘지 않도록 좁게 잡는다.
_METRIC_MARKER_LOOKBEHIND = 20
