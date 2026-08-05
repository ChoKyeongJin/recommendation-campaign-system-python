"""시간 표현 추출 — 상대 기간과 달력 구간을 구분해서 읽는다.

두 종류를 한 타입으로 뭉개지 않는 이유: ``최근 3개월`` 은 기준 시각이 정해져야 경계가
생기는 **상대** 표현이고, ``2026-07-01부터 2026-07-31까지`` 는 이미 확정된 **절대** 구간이다.
전자를 미리 날짜로 바꿔 버리면 IR 이 만들어진 시각에 결과가 묶여 재현성이 깨진다.

기준 시각은 이 모듈이 만들지 않고 **주입받는다**(CLAUDE.md 15·44). ``오늘``/``이번 달`` 같은
직시(deictic) 표현은 기준 날짜 없이는 뜻이 없으므로, 주입되지 않았으면 **추측해서 만들지
않고 아무 토큰도 내지 않는다** — 그 문장은 잔여물로 남아 상위 단계에서 드러난다.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Callable
from datetime import date, timedelta

from nl_event_ir.aliases import AliasRegistry, AliasSection
from nl_event_ir.enums import TimeUnit
from nl_event_ir.models import AbsoluteDateRange, RelativeTimeWindow
from nl_event_ir.tokenizer import SemanticToken, TokenKind

__all__ = ["TemporalExtractor"]

logger = logging.getLogger(__name__)

# 기간을 여는 말과 닫는 말. 조건을 담지 않고 기간 표현에 붙기만 하는 문법 표지라 alias 가
# 아니라 여기 문법으로 둔다(canonical 값에 대응되지 않는다).
_RECENCY_MARKERS = ("최근", "지난", "근래", "요즘")
_DURATION_TAILS = ("동안", "간", "이내", "내에", "안에", "새")

_ISO_DATE = r"\d{4}[-./]\d{1,2}[-./]\d{1,2}"
_RANGE_OPENERS = ("부터", "에서부터", "에서", "~")
_RANGE_CLOSERS = ("까지",)


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(re.escape(word) for word in sorted(words, key=lambda w: (-len(w), w)))


class TemporalExtractor:
    """상대 기간(TIME_WINDOW)과 절대 구간(DATE_RANGE)을 추출한다."""

    def __init__(
        self,
        registry: AliasRegistry,
        *,
        reference_date: date | None = None,
    ) -> None:
        self._registry = registry
        self._reference_date = reference_date

        unit_alternation = registry.alternation(AliasSection.TIME_UNIT)
        recency = _alternation(_RECENCY_MARKERS)
        tails = _alternation(_DURATION_TAILS)

        # '최근 3개월' / '지난 7일 동안'. 꼬리말(동안·이내)까지 매치 범위에 넣는 이유는
        # 잔여물 계산 때문이다 — 안 읽고 남기면 엔티티 후보로 오인된다.
        self._relative_leading_re = re.compile(
            rf"(?:{recency})\s*(?P<value>\d+)\s*(?P<unit>{unit_alternation})"
            rf"(?:\s*(?:{tails}))?"
        )
        # '3개월 동안' 처럼 최근/지난 없이 꼬리말만 있는 형태. 꼬리말을 **필수**로 두어
        # '3개월 할부' 같은 비기간 표현을 끌어오지 않는다.
        self._relative_trailing_re = re.compile(
            rf"(?<![가-힣\d])(?P<value>\d+)\s*(?P<unit>{unit_alternation})\s*(?:{tails})"
        )
        self._iso_range_re = re.compile(
            rf"(?P<start>{_ISO_DATE})\s*(?:{_alternation(_RANGE_OPENERS)})\s*"
            rf"(?P<end>{_ISO_DATE})(?:\s*(?:{_alternation(_RANGE_CLOSERS)}))?"
        )

    # ── 추출 ────────────────────────────────────────────────────────────────────

    def extract(self, text: str) -> list[SemanticToken]:
        tokens: list[SemanticToken] = []
        claimed: list[tuple[int, int]] = []

        # 절대 구간을 먼저 읽는다. 날짜 안의 숫자가 상대 기간 규칙에 잡히면 안 되기 때문이다.
        for match in self._iso_range_re.finditer(text):
            window = self._build_absolute_range(match.group("start"), match.group("end"))
            if window is None:
                # 날짜 모양은 맞지만 달력에 없는 날이다. 토큰을 만들지 않고 넘어가면 기간
                # 조건이 사라져 조회가 조용히 전체 기간으로 넓어진다 — 그 자리를 표시해 둔다.
                tokens.append(
                    SemanticToken(
                        kind=TokenKind.UNPARSED,
                        value=match.group(0),
                        start=match.start(),
                        end=match.end(),
                        raw_text=match.group(0),
                    )
                )
                claimed.append((match.start(), match.end()))
                continue
            tokens.append(self._date_range_token(match, window))
            claimed.append((match.start(), match.end()))

        for match in self._relative_leading_re.finditer(text):
            if _overlaps_any(match.span(), claimed):
                continue
            token = self._relative_token(match)
            if token is None:
                continue
            tokens.append(token)
            claimed.append(match.span())

        for match in self._relative_trailing_re.finditer(text):
            if _overlaps_any(match.span(), claimed):
                continue
            token = self._relative_token(match)
            if token is None:
                continue
            tokens.append(token)
            claimed.append(match.span())

        tokens.extend(self._extract_deictic(text, claimed))
        return tokens

    # ── 상대 기간 ────────────────────────────────────────────────────────────────

    def _relative_token(self, match: re.Match[str]) -> SemanticToken | None:
        raw_value = int(match.group("value"))
        canonical_unit = self._registry.lookup(AliasSection.TIME_UNIT, match.group("unit"))
        if canonical_unit is None:  # pragma: no cover - 교대가 사전에서 나왔으므로 도달 불가
            return None
        if raw_value <= 0:
            # 0 이하의 기간은 뜻이 없다. 여기서 버리지 않고 토큰으로 만들어 검증기가
            # 구체적인 이유를 붙이게 한다(조용한 삭제 금지).
            logger.debug("non-positive time window value: %s", raw_value)
        return SemanticToken(
            kind=TokenKind.TIME_WINDOW,
            value=RelativeTimeWindow(value=raw_value, unit=TimeUnit(canonical_unit)),
            start=match.start(),
            end=match.end(),
            raw_text=match.group(0),
        )

    # ── 절대 구간 ────────────────────────────────────────────────────────────────

    def _date_range_token(
        self, match: re.Match[str], window: AbsoluteDateRange
    ) -> SemanticToken:
        return SemanticToken(
            kind=TokenKind.DATE_RANGE,
            value=window,
            start=match.start(),
            end=match.end(),
            raw_text=match.group(0),
        )

    def _build_absolute_range(self, start_text: str, end_text: str) -> AbsoluteDateRange | None:
        start = _parse_date(start_text)
        end = _parse_date(end_text)
        if start is None or end is None:
            # 달력에 없는 날짜(2026-13-45)는 만들지 않는다. 토큰이 없으면 그 구간은
            # 잔여물로 남아 상위 단계에서 미해석으로 보고된다.
            logger.debug("unparseable date range: %s ~ %s", start_text, end_text)
            return None
        return AbsoluteDateRange(start=start, end=end)

    # ── 직시 표현(기준 날짜 필요) ─────────────────────────────────────────────────

    def _extract_deictic(
        self, text: str, claimed: list[tuple[int, int]]
    ) -> list[SemanticToken]:
        if self._reference_date is None:
            # 기준 날짜가 없으면 '오늘'의 뜻이 정해지지 않는다. 지어내지 않는다.
            return []

        tokens: list[SemanticToken] = []
        for surface, builder in _DEICTIC_BUILDERS.items():
            for match in re.finditer(
                rf"(?<![가-힣]){re.escape(surface)}(?![가-힣])", text
            ):
                if _overlaps_any(match.span(), claimed):
                    continue
                window = builder(self._reference_date)
                tokens.append(
                    SemanticToken(
                        kind=TokenKind.DATE_RANGE,
                        value=window,
                        start=match.start(),
                        end=match.end(),
                        raw_text=match.group(0),
                    )
                )
                claimed.append(match.span())
        return tokens


# ── 달력 계산(순수 함수) ──────────────────────────────────────────────────────────


def _parse_date(text: str) -> date | None:
    parts = re.split(r"[-./]", text)
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _day_range(reference: date, *, offset_days: int) -> AbsoluteDateRange:
    day = reference + timedelta(days=offset_days)
    return AbsoluteDateRange(start=day, end=day)


def _month_range(reference: date, *, offset_months: int) -> AbsoluteDateRange:
    year = reference.year
    month = reference.month + offset_months
    # 12월 경계에서 연도가 넘어간다. divmod 로 명시적으로 처리한다.
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return AbsoluteDateRange(start=date(year, month, 1), end=date(year, month, last_day))


# 표면어마다 계산식을 선언으로 둔다. 새 직시 표현은 여기 한 줄이지 새 분기가 아니다.
# 타입을 명시해야 호출부가 타입 검사를 통과한다 — 주석 없는 lambda 는 strict 모드에서
# '알 수 없는 함수 호출'이 되어 반환값이 Any 로 새어 나간다.
_DEICTIC_BUILDERS: dict[str, Callable[[date], AbsoluteDateRange]] = {
    "오늘": lambda reference: _day_range(reference, offset_days=0),
    "금일": lambda reference: _day_range(reference, offset_days=0),
    "어제": lambda reference: _day_range(reference, offset_days=-1),
    "어저께": lambda reference: _day_range(reference, offset_days=-1),
    "그제": lambda reference: _day_range(reference, offset_days=-2),
    "그저께": lambda reference: _day_range(reference, offset_days=-2),
    "이번 달": lambda reference: _month_range(reference, offset_months=0),
    "이달": lambda reference: _month_range(reference, offset_months=0),
    "금월": lambda reference: _month_range(reference, offset_months=0),
    "지난 달": lambda reference: _month_range(reference, offset_months=-1),
    "저번 달": lambda reference: _month_range(reference, offset_months=-1),
    "전월": lambda reference: _month_range(reference, offset_months=-1),
}


def _overlaps_any(span: tuple[int, int], claimed: list[tuple[int, int]]) -> bool:
    return any(span[0] < other[1] and other[0] < span[1] for other in claimed)
