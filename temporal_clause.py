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

같은 결함이 **달력 창**에도 있었다(실측 2026-08-07). ``지난달 구매한 회원`` 은 기간을 확정해
말한 문장인데, 이 모듈이 duration 리터럴만 읽어 절을 만들었으므로 ``지난달`` 을 지목한
"기간이 없다" 신고를 반박할 재료가 없었다 — 사용자의 달력 창이 되묻기 또는 배포 기본 창에
덮였다. 그래서 달력 리터럴(``date_window``)도 같은 결합 경로를 통해 절이 된다. 표면 문법의
소유자는 여전히 :mod:`calendar_window` 이고, 이 모듈은 그 판정 결과(``event_ir_window``)만
읽는다.

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

import event_ir
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

# 근접 결합 예산. ``최근`` 과 **그 뒤에 오는** duration 사이에 낄 수 있는 조사·수식어의 글자
# 수다. 넉넉하면 다른 절의 숫자를 끌어오고, 좁으면 '최근 약 30일' 같은 정상 표현을 놓친다.
_ADJACENCY_BUDGET = 6
# 절 경계. 이 문자가 사이에 있으면 같은 시간 절이 아니다.
_CLAUSE_BOUNDARY_RE = re.compile(r"[,.;!?\n]")
# duration 리터럴의 시간 종류(리터럴 추출기 소유 어휘).
_ROLLING_DURATION = "rolling_duration"
# 달력 리터럴의 kind(리터럴 추출기 소유 어휘). '지난달'·'2026년 3월'·'작년 1월'·'올해 상반기'가
# 모두 여기로 온다 — 표면 문법의 소유자는 :mod:`calendar_window` 이고, 이 모듈은 그 판정 결과만
# 읽는다(표면어를 다시 읽지 않는다).
_DATE_WINDOW = "date_window"
# 달력 리터럴이 실어 오는 wire 창 타입 → 이 절의 관계. 종류의 판정은 이미 문법 계층이 했고
# 결과가 ``normalized.event_ir_window.type`` 으로 실려 온다. 표에 없는 타입은 **절을 만들지
# 않는다** — 모르는 창을 아는 척 분류하면 되묻어야 할 자리가 조용히 확정된다.
#
# ``RELATION_CALENDAR_PERIOD`` 에 생산자가 없는 이유도 같다. 지금 바인딩이 싣는 모양은 이미
# 기준일로 확정된 구간 하나뿐이라("지난달" → 2026-07-01~2026-08-01), '이름 붙은 달력 칸'과
# '원문이 직접 적은 절대 구간'을 **바인딩 모양만으로는 가를 수 없다**. 표면어를 다시 읽어
# 가르는 것이 곧 두 번째 문법 사전이므로 하지 않는다.
_WINDOW_TYPE_RELATIONS: dict[str, str] = {"interval": RELATION_ABSOLUTE}


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
    # 확정된 구간의 경계(ISO date). ``end`` 는 **배타**(exclusive)다 — IR 창의 경계 규칙이
    # 반개구간이므로 여기서 다른 규칙을 쓰면 하루가 조용히 늘거나 준다.
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
    def wire_window(self) -> dict[str, Any] | None:
        """이 절이 요구하는 **wire 창**. 표현할 수 없으면 ``None``(지어내지 않는다).

        구조화 툴 스키마가 받는 모양 그대로다. 단위는 IR 어휘로 접는다 — 리터럴 추출기는
        복수형(``days``)을 쓰고 스키마 enum 은 단수형(``day``)만 받으므로, 접지 않은 값을
        지시문에 실으면 **스키마를 지키는 모델이 반드시 틀린다**(실측: 모델이 지시문의
        ``"days"`` 를 그대로 복사한 응답이 교정 라운드를 실패시켰다).

        절대 구간은 리터럴 바인딩의 ``normalized.event_ir_window`` 와 같은 dict 다 —
        '그대로 복사'가 이미 구조화 안내의 계약이므로 여기서 다른 모양을 만들지 않는다.
        """

        if self.relation == RELATION_LOOKBACK:
            unit = event_ir.canonical_unit(self.unit)
            if self.amount is None or unit not in event_ir.WINDOW_UNITS:
                return None
            return {"type": "rolling", "value": self.amount, "unit": unit}
        if self.relation == RELATION_ABSOLUTE:
            if not (self.start and self.end):
                return None
            return {"type": "interval", "start": self.start, "end_exclusive": self.end}
        return None

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
            "wire_window": self.wire_window,
            "source_spans": [item.to_dict() for item in self.source_spans],
        }


# ── 시간 표현의 결속 범위(scope binding) ──────────────────────────────────────────
# 한 문장에 절이 둘 있으면 기간도 둘일 수 있다. ``작년에 가장 많이 팔린 상품 5개를 올해 구매한 고객``
# 의 ``작년``은 **랭킹 모집단**의 기간이고 ``올해``는 **회원 행동**의 기간이다 — 같은 문법으로 읽으면
# 서로 다른 술어가 된다. 결속 대상이 표현되지 않으면 둘 중 하나는 조용히 사라지거나(누락), 다른 절의
# 창을 빌려 쓴다(확대). 그래서 "이 창은 어느 절의 것인가"를 여기서 **명시적으로** 판정한다.
#
# 규칙은 한국어 어순 하나다: 시간 수식어는 자기 머리말 **앞**에 온다. 그래서 창은 자기 뒤에 있는
# 앵커 중 가장 가까운 것에 붙고, 사이에 절 경계가 있거나 예산을 넘으면 붙지 않는다. 앵커의 어휘
# (랭킹 구절·행동 동사)는 호출자가 준다 — 이 모듈에 도메인 낱말을 적지 않는다.
SCOPE_RANKING = "ranking"        # 순위를 계산하는 모집단의 기간
SCOPE_MEMBERSHIP = "membership"  # 회원이 그 행동을 한 기간

# 결속 결정의 감사 로그 이름과 기본 정책. 기록하는 쪽(플랜 조립)과 읽는 쪽(의미 검증 게이트)이
# 같은 이름을 써야 하므로 선언은 여기 하나뿐이다.
SCOPE_BINDING_FILTER = "temporal_scope_binding.ranked_entity_set"
# 문장이 회원 행동의 기간을 말하지 않았을 때의 기본값. 한국어 어순에서 ``작년에 가장 많이 팔린``
# 의 ``작년``은 관형절(팔린) 안에 갇히므로 회원의 구매 시점까지 제한하지 않는다. 이 기본은
# **명시된 회원 기간을 이기지 못한다** — 원문이 말한 기간이 언제나 우선한다.
DEFAULT_SCOPE_BINDING = "ranking_only"


@dataclass(frozen=True, slots=True)
class ScopeWindowBinding:
    """창 하나가 어느 절에 결속됐는가. 판정의 근거(간격·앵커 위치)를 함께 남긴다."""

    scope: str
    window: Mapping[str, Any]
    window_span: tuple[int, int]
    anchor_span: tuple[int, int]
    gap: int
    # 결속된 창의 **원문 표면어**('작년'). 해석된 라벨('2025년')과 다른 글자라, 원문 어휘로
    # 말하는 소비자(검증기 판정문 대조 등)는 이것이 없으면 같은 창을 알아보지 못한다.
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "text": self.text,
            "window": dict(self.window),
            "window_span": list(self.window_span),
            "anchor_span": list(self.anchor_span),
            "gap": self.gap,
        }


def bind_window_scopes(
    query: str,
    *,
    windows: Sequence[tuple[Mapping[str, Any], int, int]],
    anchors: Sequence[tuple[str, int, int]],
    budget: int = _ADJACENCY_BUDGET,
) -> tuple[ScopeWindowBinding, ...]:
    """달력 창들을 앵커(절)에 결속한다. 붙일 앵커가 없는 창은 **결속하지 않는다**.

    ``windows`` 는 :func:`calendar_window.parse_calendar_window_spans` 의 산출물이고
    ``anchors`` 는 ``(scope, start, end)`` 다. 한 창은 한 앵커에, 한 앵커는 한 창에만 붙는다 —
    같은 창을 두 절이 나눠 갖는 해석은 만들지 않는다(그것이 필요하면 호출자가 결속 결과를 보고
    명시적으로 공유를 선언해야 한다).

    추측하지 않는다: 결속되지 않은 창은 결과에 없다. 그 창의 처리(다른 파서가 가져가거나,
    미해결로 남거나)는 호출자의 몫이다.
    """
    if not isinstance(query, str) or not query or not windows or not anchors:
        return ()

    candidates: list[tuple[int, int, int, int, int]] = []
    for window_index, (_window, window_start, window_end) in enumerate(windows):
        for anchor_index, (_scope, anchor_start, anchor_end) in enumerate(anchors):
            if anchor_end <= anchor_start or window_end > anchor_start:
                # 수식어가 머리말 뒤에 오는 결속은 만들지 않는다(어순 규칙).
                continue
            gap = anchor_start - window_end
            if gap > budget or _separated_by_clause_boundary(query, window_end, anchor_start):
                continue
            candidates.append((gap, window_start, anchor_start, window_index, anchor_index))

    bound: list[ScopeWindowBinding] = []
    used_windows: set[int] = set()
    used_anchors: set[int] = set()
    for gap, _window_start, _anchor_start, window_index, anchor_index in sorted(candidates):
        if window_index in used_windows or anchor_index in used_anchors:
            continue
        used_windows.add(window_index)
        used_anchors.add(anchor_index)
        window, window_start, window_end = windows[window_index]
        scope, anchor_start, anchor_end = anchors[anchor_index]
        bound.append(ScopeWindowBinding(
            scope=str(scope),
            window=window,
            window_span=(window_start, window_end),
            anchor_span=(anchor_start, anchor_end),
            gap=gap,
            text=query[window_start:window_end],
        ))
    return tuple(sorted(bound, key=lambda item: item.window_span))


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


@dataclass(frozen=True, slots=True)
class _TemporalAtom:
    """원문이 **기간을 말한** 구간 하나. 표지와 결합할 후보다.

    두 종류가 같은 타입으로 들어온다: 길이를 말한 duration 리터럴(``최근 30일`` 의 ``30일``)과
    구간을 말한 달력 리터럴(``지난달``·``2026년 3월``). 종류를 나눠 두 벌의 결합 루프를 만들면
    근접·절 경계·어순 규칙이 곧 갈라지므로 결합 경로는 하나로 유지한다.

    ``self_quantifying`` 은 "이 구간이 표지 없이도 창인가"다. 달력 토큰은 스스로 구간을
    확정하므로 참이고, 그때 결합된 표지는 절이 **삼키기만** 한다(근거 구간으로 적지 않는다).
    표지 구간을 적으면 :attr:`TemporalClause.marker_bound` 가 참이 되는데, 그 속성이 답하는
    질문은 "이 duration 리터럴이 임계값이 아니라 창임이 증명됐는가"이고 달력 토큰에는 증명이
    필요 없다. 두 질문을 한 플래그로 합치면 그 값을 읽는 하류가 다른 사실을 보게 된다.
    """

    start: int
    end: int
    text: str
    relation: str
    amount: int | None = None
    unit: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    self_quantifying: bool = False


def _rolling_duration_atoms(
    literal_bindings: Sequence[Mapping[str, Any]],
) -> list[_TemporalAtom]:
    """리터럴 바인딩에서 ``rolling_duration`` 만 뽑는다(길이를 말한 구간)."""
    found: list[_TemporalAtom] = []
    for binding in literal_bindings:
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
        found.append(
            _TemporalAtom(
                start=start,
                end=end,
                text=str(binding.get("text") or ""),
                relation=RELATION_LOOKBACK,
                amount=value,
                unit=unit,
            )
        )
    return found


def _calendar_window_atoms(
    literal_bindings: Sequence[Mapping[str, Any]],
) -> list[_TemporalAtom]:
    """리터럴 바인딩에서 달력 창을 뽑는다(구간을 말한 토큰).

    ``지난달`` 처럼 원문이 기간을 **확정해** 말했는데도 모델이 '기간이 없다'를 신고하면, 이
    재료가 없던 동안에는 그 신고를 반박할 방법이 없어 사용자의 달력 창이 되묻기나 배포 기본
    창에 덮였다. 종류·경계의 판정은 문법 계층이 이미 했으므로 여기서는 그 결과만 옮긴다.
    """
    found: list[_TemporalAtom] = []
    for binding in literal_bindings:
        if not isinstance(binding, Mapping) or binding.get("kind") != _DATE_WINDOW:
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        window = normalized.get("event_ir_window")
        if not isinstance(window, Mapping):
            continue
        relation = _WINDOW_TYPE_RELATIONS.get(str(window.get("type")))
        window_start, window_end = window.get("start"), window.get("end_exclusive")
        start, end = binding.get("start"), binding.get("end")
        if relation is None:
            continue
        if not (isinstance(window_start, str) and isinstance(window_end, str)):
            continue
        if not (window_start and window_end):
            continue
        if not (isinstance(start, int) and isinstance(end, int) and start < end):
            continue
        found.append(
            _TemporalAtom(
                start=start,
                end=end,
                text=str(binding.get("text") or ""),
                relation=relation,
                window_start=window_start,
                window_end=window_end,
                self_quantifying=True,
            )
        )
    return found


def _temporal_atoms(
    literal_bindings: Sequence[Mapping[str, Any]] | None,
) -> list[_TemporalAtom]:
    """원문이 기간을 말한 구간 전부(원문 좌표 순)."""
    rows = list(literal_bindings or ())
    atoms = _rolling_duration_atoms(rows) + _calendar_window_atoms(rows)
    return sorted(atoms, key=lambda atom: (atom.start, atom.end))


def _separated_by_clause_boundary(query: str, left: int, right: int) -> bool:
    if left >= right:
        return False
    return _CLAUSE_BOUNDARY_RE.search(query[left:right]) is not None


def _assign_markers(
    query: str,
    atoms: Sequence[_TemporalAtom],
    markers: Sequence[tuple[int, int, str]],
) -> dict[int, int]:
    """각 구간에 **그 구간을 소유하는 표지**를 붙인다 → {atom index: marker index}.

    결합 규칙은 종류와 무관하게 하나다(어순 → 근접 → 절 경계 → 가장 가까운 표지). 롤링 길이와
    달력 구간이 각자 규칙을 갖게 되면 같은 문장에서 서로 다른 소유권이 나온다.
    """

    assignment: dict[int, int] = {}
    for atom_index, atom in enumerate(atoms):
        best: tuple[int, int] | None = None
        for marker_index, (_marker_start, marker_end, _marker) in enumerate(markers):
            if marker_end > atom.start:
                # 어순 규칙. 표지보다 앞선 구간은 이 표지의 것이 아니다.
                continue
            gap = atom.start - marker_end
            boundary = _separated_by_clause_boundary(query, marker_end, atom.start)
            if boundary or gap > _ADJACENCY_BUDGET:
                continue
            if best is None or gap < best[1]:
                best = (marker_index, gap)
        if best is not None:
            assignment[atom_index] = best[0]
    return assignment


def combine_temporal_clauses(
    query: str,
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[TemporalClause, ...]:
    """원문의 시간 표현을 typed clause 로 결합한다.

    결합 규칙(문장별 예외가 아니라 문법):

    1. duration 리터럴이 표지 **뒤**에 오고(한국어 어순: ``최근 30일``),
    2. 그 리터럴이 표지에 근접(예산 내)하고,
    3. 그 사이에 절 경계 문자가 없고,
    4. 그 리터럴이 다른 표지에 더 가깝지 않으면

    → 표지와 리터럴은 같은 시간 절이다. 그 밖의 duration 리터럴은 표지 없는 lookback 절로,
    숫자를 못 얻은 표지는 ``amount=None`` 인 미정 절로 남는다(추측하지 않는다).

    1번이 없으면 근접성만 남고, 그러면 **앞 절의 임계값**이 뒤의 맨 표지를 수량화한다 —
    ``구매주기가 30일 이하이고 최근 구매가 없는 회원`` 의 ``30일`` 은 창이 아니라 임계값이고
    (``유효기간 30일``·``환불까지 7일`` 도 같다), 그것을 ``최근`` 의 창으로 읽으면 진짜 결핍이
    '원문이 이미 말했다'로 뒤집혀 되묻기가 재방출로 샌다(실측 형태). 어순 규칙은 이 모듈이
    :func:`bind_window_scopes` 에서 이미 선언한 것과 같다 — 시간 수식어는 자기 머리말 앞에
    오고, 그 수식어 안에서 수량은 표지 뒤에 온다.

    달력 구간(``지난달``·``2026년 3월``)도 **같은 규칙**을 받는다. 다른 점은 하나뿐이다:
    달력 토큰은 스스로 구간을 확정하므로 결합된 표지를 근거 구간으로 적지 않고 삼키기만 한다
    (:class:`_TemporalAtom` 의 ``self_quantifying`` 주석 참조).
    """

    if not isinstance(query, str) or not query:
        return ()
    atoms = _temporal_atoms(literal_bindings)
    # 달력 토큰 **안**에 있는 표지는 독립된 표지가 아니라 그 토큰의 글자다(``지난달`` 의
    # ``지난``). 홀로 남기면 원문이 이미 확정한 기간을 '맨 표지'로 다시 신고하는 유령 절이 되고,
    # 그 절을 읽는 쪽에는 그것이 유령임을 알 재료가 없다.
    markers = [
        span
        for span in _recency_marker_spans(query)
        if not any(
            atom.self_quantifying and span[0] < atom.end and atom.start < span[1]
            for atom in atoms
        )
    ]

    # 구간 → 가장 가까운 표지. 한 구간이 두 표지에 붙는 것을 막는다
    # ('최근 3개월 … 최근 30일' 처럼 표지가 여럿인 문장이 실제로 있다).
    assignment = _assign_markers(query, atoms, markers)

    clauses: list[TemporalClause] = []
    used_markers: set[int] = set()
    for atom_index, atom in enumerate(atoms):
        spans = [EvidenceSpan(atom.start, atom.end, atom.text, role="amount")]
        marker_index = assignment.get(atom_index)
        if marker_index is not None:
            used_markers.add(marker_index)
            if not atom.self_quantifying:
                marker_start, marker_end, marker_text = markers[marker_index]
                spans.insert(
                    0, EvidenceSpan(marker_start, marker_end, marker_text, role="marker")
                )
        clauses.append(
            TemporalClause(
                relation=atom.relation,
                amount=atom.amount,
                unit=atom.unit,
                start=atom.window_start,
                end=atom.window_end,
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
