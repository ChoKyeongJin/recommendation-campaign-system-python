"""시간 표현의 **typed clause 결합** — 떨어져 있는 표면 구간 여러 개가 기간 하나를 이룬다.

배경(실측 2026-08-06, `/target-sql` 전수 실행)
------------------------------------------------
``최근 30일 구매한 회원 수를 알려줘`` 가 ``missing_argument(period)`` 로 닫혔다. 원문에는
``30일`` 이 분명히 있다. 무슨 일이 있었냐면 — 구조화 모델이 ``최근`` 을 근거 구간으로 삼아
"기간이 없다"를 신고했고, 애플리케이션에는 **그 신고를 반박할 결정론 재료가 없었다**.
``최근`` 과 ``30일`` 은 서로 다른 문자 구간이고, 그때까지 이 저장소에는 "떨어진 구간 둘이
하나의 시간 절"이라는 개념이 없었기 때문이다. 창(window)은 이미
:mod:`calendar_window` 가 소유하고 duration 리터럴은 :mod:`query_structurer.semantic_ir` 가
소유하지만, **둘을 잇는 자리**가 비어 있었다.

이 모듈이 그 자리다. 하는 일은 하나뿐이다:

    원문 + 리터럴 바인딩 → :class:`TemporalClause` 들

그리고 그 결과로 하나의 질문에 답한다 — "모델이 '기간이 없다'고 지목한 구간은,
원문이 이미 기간을 말한 절의 일부인가?" 답이 예이면 그 신고는 **원문과 모순**이므로
결핍이 아니다. 이 판정은 프롬프트별 예외가 아니라 문법 규칙이다: 근접성 + 절 경계 +
타입이 맞는 리터럴. 새 문장이 늘어도 여기 분기가 늘지 않는다.

**추측하지 않는다.** 숫자가 없는 ``최근`` 은 이 모듈에서 ``amount=None`` 인 절로 남고,
기본값을 지어내지 않는다 — 그 결정은 정책 계층(:mod:`targeting_policy` ·
:mod:`default_period_policy`)이 소유한다. 이 모듈은 "원문이 무엇을 말했나"만 답한다.

실행: python -m pytest tests/test_temporal_clause.py -q
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import lexicon_patterns

# 시간 절의 관계(닫힌 집합). 표면어가 아니라 **연산자**다.
RELATION_LOOKBACK = "lookback"              # 최근 N일 — 기준시각에서 과거로 N
RELATION_CALENDAR_PERIOD = "calendar_period"  # 작년/지난달 — 달력 구간
RELATION_ABSOLUTE = "absolute"              # 2026-01-01 ~ 2026-03-31

# 기준 시각의 출처(닫힌 집합). 값 자체는 여기서 만들지 않는다.
ANCHOR_REQUEST_REFERENCE_TIME = "request_reference_time"

# 소유자. 사용자가 말한 값과 애플리케이션이 고른 값을 절 수준에서 구분한다.
OWNERSHIP_QUERY = "query"        # 원문이 말했다
OWNERSHIP_POLICY = "policy"      # 배포 설정이 채웠다

# 근접 결합 예산. ``최근`` 과 duration 사이에 낄 수 있는 조사·수식어의 글자 수다.
# 넉넉하면 다른 절의 숫자를 끌어오고, 좁으면 '최근 약 30일' 같은 정상 표현을 놓친다.
_ADJACENCY_BUDGET = 6
# 절 경계. 이 문자가 사이에 있으면 같은 시간 절이 아니다.
_CLAUSE_BOUNDARY_RE = re.compile(r"[,.;!?\n]")
# duration 리터럴의 시간 종류(리터럴 추출기 소유 어휘).
_ROLLING_DURATION = "rolling_duration"


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """원문 좌표계의 근거 구간 하나."""

    start: int
    end: int
    text: str
    role: str = "marker"  # marker | amount | unit | boundary

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text, "role": self.role}


@dataclass(frozen=True, slots=True)
class TemporalClause:
    """의미 하나로 결합된 시간 표현. 구간은 **여럿**일 수 있다.

    ``최근 30일`` 은 표면 구간 둘(``최근`` · ``30일``)이지만 시간 절은 하나다. 이 타입이
    생기기 전에는 그 사실을 담을 자리가 없어서, 한쪽 구간만 본 판정이 다른 쪽 구간을
    없는 것으로 취급했다.
    """

    relation: str
    amount: int | None = None
    unit: str | None = None
    calendar_period: str | None = None
    anchor: str = ANCHOR_REQUEST_REFERENCE_TIME
    start: str | None = None
    end: str | None = None
    ownership: str = OWNERSHIP_QUERY
    source_spans: tuple[EvidenceSpan, ...] = ()

    @property
    def marker_bound(self) -> bool:
        """이 절이 **시간 표지**에 묶여 있는가.

        표지 없는 duration 리터럴은 창이 아닐 수 있다 — ``구매주기가 30일 이하`` 의 ``30일`` 은
        임계값이고, ``활동 개월 수가 6개월 이상`` 의 ``6개월`` 도 마찬가지다. 표지 없이 창으로
        단정하면 그 값들이 '사라진 기간'으로 오탐돼 정상 SQL 이 부분 SQL 로 강등된다(실측).
        """
        return any(span.role == "marker" for span in self.source_spans)

    @property
    def is_quantified(self) -> bool:
        """이 절이 기간을 **말했는가**. 달력 구간도 기간이다(작년 = 확정된 구간)."""
        if self.relation == RELATION_LOOKBACK:
            return self.amount is not None and self.unit is not None
        if self.relation == RELATION_CALENDAR_PERIOD:
            return bool(self.calendar_period)
        if self.relation == RELATION_ABSOLUTE:
            return bool(self.start and self.end)
        return False

    @property
    def span(self) -> tuple[int, int] | None:
        """절 전체를 덮는 구간(구간이 없으면 None)."""
        if not self.source_spans:
            return None
        return (
            min(item.start for item in self.source_spans),
            max(item.end for item in self.source_spans),
        )

    def covers(self, start: int, end: int) -> bool:
        """주어진 구간이 이 절의 구간 중 하나와 겹치는가."""
        return any(
            not (end <= item.start or start >= item.end) for item in self.source_spans
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "amount": self.amount,
            "unit": self.unit,
            "calendar_period": self.calendar_period,
            "anchor": self.anchor,
            "start": self.start,
            "end": self.end,
            "ownership": self.ownership,
            "quantified": self.is_quantified,
            "marker_bound": self.marker_bound,
            "source_spans": [item.to_dict() for item in self.source_spans],
        }


def _recency_marker_spans(query: str) -> list[tuple[int, int, str]]:
    """recency 표지(``최근``·``지난``…)의 원문 구간. 어휘는 렉시콘이 소유한다."""
    spans: list[tuple[int, int, str]] = []
    for term in lexicon_patterns.vocabulary("temporal_recency_marker"):
        if not term:
            continue
        cursor = 0
        while (found := query.find(term, cursor)) >= 0:
            spans.append((found, found + len(term), term))
            cursor = found + 1
    # 긴 표지가 짧은 표지를 삼키면 같은 자리를 두 번 세지 않는다.
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    kept: list[tuple[int, int, str]] = []
    for start, end, text in spans:
        if any(start >= other_start and end <= other_end for other_start, other_end, _ in kept):
            continue
        kept.append((start, end, text))
    return kept


def _rolling_duration_literals(
    literal_bindings: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[int, int, str, int, str]]:
    """리터럴 바인딩에서 ``rolling_duration`` 만 뽑는다 → (start, end, text, value, unit)."""
    found: list[tuple[int, int, str, int, str]] = []
    for binding in literal_bindings or ():
        if not isinstance(binding, Mapping) or binding.get("kind") != "duration":
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        if normalized.get("temporal_kind") != _ROLLING_DURATION:
            continue
        value = normalized.get("value")
        unit = normalized.get("semantic_unit")
        start, end = binding.get("start"), binding.get("end")
        if not (isinstance(value, int) and not isinstance(value, bool)):
            continue
        if not (isinstance(unit, str) and unit):
            continue
        if not (isinstance(start, int) and isinstance(end, int) and start < end):
            continue
        found.append((start, end, str(binding.get("text") or ""), value, unit))
    return sorted(found)


def _separated_by_clause_boundary(query: str, left: int, right: int) -> bool:
    if left >= right:
        return False
    return _CLAUSE_BOUNDARY_RE.search(query[left:right]) is not None


def combine_temporal_clauses(
    query: str,
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[TemporalClause, ...]:
    """원문의 시간 표현을 typed clause 로 결합한다.

    결합 규칙(문장별 예외가 아니라 문법):

    1. recency 표지 하나에 duration 리터럴 **하나**가 근접(예산 내)하고,
    2. 그 사이에 절 경계 문자가 없고,
    3. 그 리터럴이 다른 표지에 더 가깝지 않으면

    → 표지와 리터럴은 같은 시간 절이다. 그 밖의 duration 리터럴은 표지 없는 lookback 절로,
    숫자를 못 얻은 표지는 ``amount=None`` 인 미정 절로 남는다(추측하지 않는다).
    """

    if not isinstance(query, str) or not query:
        return ()
    markers = _recency_marker_spans(query)
    durations = _rolling_duration_literals(literal_bindings)

    # 리터럴 → 가장 가까운 표지. 한 리터럴이 두 표지에 붙는 것을 막는다
    # ('최근 3개월 … 최근 30일' 처럼 표지가 여럿인 문장이 실제로 있다).
    assignment: dict[int, int] = {}
    for literal_index, (literal_start, literal_end, _text, _value, _unit) in enumerate(durations):
        best: tuple[int, int] | None = None
        for marker_index, (marker_start, marker_end, _marker) in enumerate(markers):
            if marker_end <= literal_start:
                gap = literal_start - marker_end
                boundary = _separated_by_clause_boundary(query, marker_end, literal_start)
            elif literal_end <= marker_start:
                gap = marker_start - literal_end
                boundary = _separated_by_clause_boundary(query, literal_end, marker_start)
            else:
                gap, boundary = 0, False
            if boundary or gap > _ADJACENCY_BUDGET:
                continue
            if best is None or gap < best[1]:
                best = (marker_index, gap)
        if best is not None:
            assignment[literal_index] = best[0]

    clauses: list[TemporalClause] = []
    used_markers: set[int] = set()
    for literal_index, (literal_start, literal_end, text, value, unit) in enumerate(durations):
        marker_index = assignment.get(literal_index)
        spans = [EvidenceSpan(literal_start, literal_end, text, role="amount")]
        if marker_index is not None:
            marker_start, marker_end, marker_text = markers[marker_index]
            spans.insert(0, EvidenceSpan(marker_start, marker_end, marker_text, role="marker"))
            used_markers.add(marker_index)
        clauses.append(
            TemporalClause(
                relation=RELATION_LOOKBACK,
                amount=value,
                unit=unit,
                source_spans=tuple(sorted(spans, key=lambda item: item.start)),
            )
        )

    for marker_index, (marker_start, marker_end, marker_text) in enumerate(markers):
        if marker_index in used_markers:
            continue
        clauses.append(
            TemporalClause(
                relation=RELATION_LOOKBACK,
                amount=None,
                unit=None,
                source_spans=(EvidenceSpan(marker_start, marker_end, marker_text, role="marker"),),
            )
        )
    return tuple(sorted(clauses, key=lambda clause: (clause.span or (0, 0))))


def quantified_clause_for_span(
    query: str,
    span: tuple[int, int],
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> TemporalClause | None:
    """이 원문 구간이 속한 **기간을 말한** 시간 절. 없으면 ``None``.

    ``missing_argument(period)`` 반박의 단일 판정자다. 근거 구간이 이미 수량화된 절의
    일부라면 그 신고는 원문과 모순이다 — 사용자에게 되물을 것이 없다.
    """

    start, end = span
    for clause in combine_temporal_clauses(query, literal_bindings):
        if clause.is_quantified and clause.covers(start, end):
            return clause
    return None


def stated_period_for_issue(
    query: str,
    issue: Mapping[str, Any],
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> TemporalClause | None:
    """``missing_argument(period)`` 신고가 원문과 모순인지 판정한다.

    반환값이 있으면 그것이 **원문이 말한 기간**이다(신고는 거짓). ``None`` 이면 신고는
    유효하다 — 그 경우의 처리(되묻기 / 기본값)는 이 모듈이 정하지 않는다.
    """

    if not isinstance(issue, Mapping):
        return None
    if issue.get("code") != "missing_argument" or issue.get("argument") != "period":
        return None
    evidence = issue.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    start, end = evidence.get("start"), evidence.get("end")
    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(query)):
        return None
    return quantified_clause_for_span(query, (start, end), literal_bindings)


__all__ = [
    "ANCHOR_REQUEST_REFERENCE_TIME",
    "OWNERSHIP_POLICY",
    "OWNERSHIP_QUERY",
    "RELATION_ABSOLUTE",
    "RELATION_CALENDAR_PERIOD",
    "RELATION_LOOKBACK",
    "EvidenceSpan",
    "TemporalClause",
    "combine_temporal_clauses",
    "quantified_clause_for_span",
    "stated_period_for_issue",
]
