"""절 단위 typed semantics — **극성·수량자·기간 소유권**을 한 자리에 확정한다.

이 모듈이 생긴 이유(실측 2026-08-08, 라이브 감사 40~86)
-------------------------------------------------------
``최근 30일에는 구매하지 않았지만 과거 구매 이력은 있는 회원`` 이 ``기간 값이 없습니다`` 로
닫혔다. 기간은 문장에 **있다**. 무슨 일이 있었냐면 — 기간 결핍 판정이 *문장 전체* 를 단위로
돌았고, 창이 **부정 절**에 붙어 있어 소유자를 찾지 못했다. 같은 뿌리에서 두 번째 결함이 나온다:
``앱으로 로그인하지 않은 회원`` 이 ``LAST_LOGIN_CHANNEL != 'APP'`` 로 컴파일됐다 — 부재(never)를
**마지막 값 부등호**로 근사한 것이다. 어제 앱으로 로그인한 회원이 대상에 들어간다.

두 결함의 공통 원인은 하나다. 문장에는 절이 여럿인데, 그 절 각각이 **무엇을 주장하는가**
(극성·수량자)와 **어느 기간이 자기 것인가**를 담을 타입이 없었다. 그래서 판정마다
``has_period`` · ``missing_period`` 같은 문장 수준 불리언이 따로 계산됐고, 서로 어긋났다.

이 모듈은 그 자리를 하나로 만든다::

    원문 + 리터럴 바인딩  →  ClauseSemantics 들

각 절은 자기 ``polarity`` · ``quantifier`` · ``temporal`` 을 들고 다닌다. 기간의 Single Source
of Truth 는 :attr:`ClauseSemantics.temporal` 이고, 문장 어디에도 두 번째 사본을 두지 않는다.

**낱말 목록을 새로 만들지 않는다.** 재료는 전부 이미 선언돼 있다.

===========================  =======================================================
필요한 것                     소유자
===========================  =======================================================
사건이 무엇인가               ``audience_catalog.sources[*].aliases`` (시간축을 가진 소스)
그 사건이 부정됐는가           :func:`audience_frame.local_negation_spans`
기간 표현이 무엇인가           :func:`temporal_clause.combine_temporal_clauses`
칸별 전칭 표지                 ``source_recurrence_*`` 어휘(:mod:`lexicon_patterns`)
===========================  =======================================================

기간 소유권 규칙도 문장별 예외가 아니라 **어순 문법** 하나다: 한국어에서 시간 수식어는 자기
머리말 앞에 오므로, 창은 자기 **뒤에 오는 가장 가까운 사건 앵커**의 것이다. 한 앵커에 창이
둘 다 붙으려 하면 가까운 쪽이 이긴다(:func:`temporal_clause.bind_window_scopes` 와 같은 규칙).
이 규칙이 위 두 문장을 자동으로 가른다 — ``최근 30일`` 은 부정 절(``구매하지 않았``)에 붙고,
``과거 구매 이력`` 절은 창이 **없는** 채로 남는다(무한 구간이며, 결핍이 아니다).

**추측하지 않는다.** 창이 없는 절은 ``temporal=None`` 으로 남고 기본값을 지어내지 않는다.
정책이 채우는 경우에도 :class:`ClauseTemporal` 의 ``provenance`` 로 사용자 값과 구분한다 —
그 구분이 없으면 의미 검증기가 자기 SQL 을 근거 없는 조건으로 되잡는다.

실행: python -m pytest tests/test_clause_semantics.py -q
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import audience_frame
import lexicon_patterns
import temporal_clause

Span = tuple[int, int]


class Polarity(StrEnum):
    """절이 사건의 **존재**를 주장하는가, **부재**를 주장하는가."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class Quantifier(StrEnum):
    """절의 수량 주장. 극성과 **다른 축**이다 — 극성은 방향, 수량자는 몇 번/어디서다.

    ``EVERY_BUCKET_STATE`` 와 ``EVERY_BUCKET_OCCURRENCE`` 를 나눈 이유가 이 열거의 요점이다.
    '모든 칸에서 상태가 X였는가'는 칸을 **전부 열거**할 수 있어야 답하고(빈 칸이 곧 거짓),
    '각 칸마다 사건이 최소 한 번 있었는가'는 그 구간의 **발생을 전부 볼 수** 있으면 답한다.
    사건 로그는 뒤의 질문에 답할 수 있고 앞의 질문에는 답할 수 없다. 두 뜻을 한 이름에 두면
    답할 수 있는 요청까지 함께 막힌다(실측: ``최근 3개월 동안 매월 한 번 이상 구매한 회원``).
    """

    EXISTS = "exists"
    NEVER = "never"
    EVERY_BUCKET_STATE = "every_bucket_state"
    EVERY_BUCKET_OCCURRENCE = "every_bucket_occurrence"
    CARDINALITY = "cardinality"


class Provenance(StrEnum):
    """이 절의 기간을 **누가 골랐는가**. 사용자 값과 정책 값을 절 수준에서 구분한다."""

    USER_EXPLICIT = "user_explicit"
    POLICY_DEFAULT = "policy_default"


# 수량자 → 그 뜻을 낮추려면 관측이 선언해야 하는 능력. 이름은
# ``temporal_bindings.json`` 의 ``observation_capabilities`` 키와 같아야 한다 — 여기서 다른
# 이름을 쓰면 선언과 판정이 갈라진다(드리프트는 테스트가 고정한다).
#
# 값이 **집합**인 이유가 이 표의 요점이다. 하나라도 있으면 답할 수 있다는 뜻이고, 그래야
# 부재(never)의 두 갈래가 갈린다.
#
# * **맨 부재** — '최근 30일간 로그인하지 않은'. 마지막 발생 시각 하나만 알아도 답한다
#   (마지막 로그인이 창보다 앞이면 그 창에 로그인은 없다 · 단조성). 그래서
#   ``supports_point_state`` 로도 충분하고, 이 저장소의 살아 있는 미접속 경로가 그 근거로
#   정확한 SQL 을 내고 있다.
# * **한정된 부재** — '**앱으로** 로그인하지 않은'. 마지막 로그인의 채널만 알면 그 이전
#   로그인의 채널은 모른다. 어제 앱, 오늘 PC 인 회원이 대상에 들어간다(감사 #68 의 실제 결함).
#   그래서 여기서는 ``supports_all_occurrences`` 를 **엄격히** 요구한다.
#
# 두 갈래를 가르는 것은 부정어가 아니라 **한정어의 유무**다 — 그것이 실제로 답할 수 있는지를
# 정하는 사실이기 때문이다.
BARE_ABSENCE_CAPABILITIES = frozenset({"supports_all_occurrences", "supports_point_state"})
QUALIFIED_ABSENCE_CAPABILITIES = frozenset({"supports_all_occurrences"})

REQUIRED_CAPABILITY: dict[str, frozenset[str]] = {
    Quantifier.EVERY_BUCKET_OCCURRENCE: frozenset({"supports_all_occurrences"}),
    Quantifier.EVERY_BUCKET_STATE: frozenset({"supports_complete_bucket_enumeration"}),
}

# 값 한정어와 사건 표면어 사이에 허용하는 글자 수('앱으로 로그인' 의 '으로 '). 겹치는 청구는
# 거리와 무관하게 이 절의 것이다.
_QUALIFIER_BUDGET = 6


@dataclass(frozen=True, slots=True)
class ClauseTemporal:
    """절이 소유한 기간 하나 + 그 값을 누가 골랐는가.

    ``clause`` 는 :mod:`temporal_clause` 가 결합한 표면 절이다. 여기서 창 문법을 다시 읽지
    않는 이유는 그 문법의 소유자가 하나여야 하기 때문이다.
    """

    clause: temporal_clause.TemporalClause
    provenance: Provenance = Provenance.USER_EXPLICIT

    @property
    def is_quantified(self) -> bool:
        return self.clause.is_quantified

    @property
    def span(self) -> Span | None:
        return self.clause.span

    def to_dict(self) -> dict[str, Any]:
        return {"provenance": str(self.provenance), "clause": self.clause.to_dict()}


@dataclass(frozen=True, slots=True)
class ClauseSemantics:
    """문장 안의 조건 절 하나 — 무엇에 대해, 어느 방향으로, 몇 번, 언제.

    ``clause_id`` 는 이 문장 안에서만 유효한 안정 식별자다(원문 좌표 기반). 진단이 "어느 절이
    막혔는지" 말할 때 쓰이며, 같은 원문은 같은 id 를 낸다(§63 재현성).
    """

    clause_id: str
    event: str
    polarity: Polarity
    quantifier: Quantifier
    span: Span
    evidence: str
    temporal: ClauseTemporal | None = None
    bucket_unit: str | None = None
    # 이 절이 사건을 **한정**하는가('앱으로' 로그인 · 특정 브랜드 구매). 한정어의 값 도메인
    # 청구가 사건 표면어에 붙어 있으면 참이다. 부재의 능력 계약을 가르는 유일한 재료다.
    qualified: bool = False
    qualifier_evidence: str | None = None

    @property
    def required_capabilities(self) -> frozenset[str]:
        """이 절을 낮추려면 관측이 선언해야 하는 능력들 — **하나라도** 있으면 답할 수 있다.

        빈 집합은 '능력 계약 없음'이다(맨 존재 조건). :data:`REQUIRED_CAPABILITY` 를 그대로
        조회하지 않고 여기를 거치는 이유는 부재가 한정 여부에 따라 갈리기 때문이다.
        """
        if self.quantifier is Quantifier.NEVER:
            return (
                QUALIFIED_ABSENCE_CAPABILITIES
                if self.qualified
                else BARE_ABSENCE_CAPABILITIES
            )
        return REQUIRED_CAPABILITY.get(str(self.quantifier), frozenset())

    @property
    def has_period(self) -> bool:
        """이 절이 **자기** 기간을 말했는가. 다른 절의 기간은 세지 않는다."""
        return self.temporal is not None and self.temporal.is_quantified

    def covers(self, start: int, end: int) -> bool:
        """원문 구간이 이 절(조건 구간 + 소유한 기간 구간)에 걸치는가."""
        if not (end <= self.span[0] or start >= self.span[1]):
            return True
        owned = self.temporal.span if self.temporal is not None else None
        return owned is not None and not (end <= owned[0] or start >= owned[1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "event": self.event,
            "polarity": str(self.polarity),
            "quantifier": str(self.quantifier),
            "span": list(self.span),
            "evidence": self.evidence,
            "bucket_unit": self.bucket_unit,
            "qualified": self.qualified,
            "qualifier_evidence": self.qualifier_evidence,
            "has_period": self.has_period,
            "required_capabilities": sorted(self.required_capabilities),
            "temporal": self.temporal.to_dict() if self.temporal is not None else None,
        }


# ── 사건 앵커(카탈로그가 소유한 표면어) ─────────────────────────────────────────────


def _event_aliases(catalog: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """시간축을 가진 카탈로그 소스 → 그 소스의 표면어들.

    시간축이 없는 소스를 제외하는 이유는 이 모듈이 답하는 질문이 **시점을 가진 사건**에 대한
    것이기 때문이다. 순수 속성 테이블에는 '언제 일어났는가'가 없다.
    """
    sources = catalog.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    found: dict[str, tuple[str, ...]] = {}
    for name, declaration in sources.items():
        if not isinstance(declaration, Mapping) or not declaration.get("time_column"):
            continue
        aliases = declaration.get("aliases")
        terms = tuple(
            term
            for term in (
                declaration.get("label"),
                *(aliases if isinstance(aliases, list) else []),
            )
            if isinstance(term, str) and term
        )
        if terms:
            found[str(name)] = terms
    return found


def _anchor_spans(query: str, terms: Sequence[str]) -> list[Span]:
    """표면어가 나타난 구간(겹치는 짧은 쪽은 긴 쪽에 삼켜진다)."""
    spans = sorted(
        {
            match.span()
            for term in terms
            for match in re.finditer(re.escape(term), query)
        }
    )
    kept: list[Span] = []
    for start, end in sorted(spans, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start >= other[0] and end <= other[1] for other in kept):
            continue
        kept.append((start, end))
    return sorted(kept)


# ── 칸별 전칭 표지('매월'·'각 달마다') ──────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _recurrence_pattern() -> re.Pattern[str] | None:
    """칸별 반복 표지. 어휘가 없으면 ``None``(캡처하지 않는다).

    빈 교대를 정규식에 넣으면 아무 위치에서나 매치되므로, 어휘가 비면 캡처 자체를 끈다
    (:mod:`lowering_planner` 의 같은 규칙).
    """
    try:
        prefix = lexicon_patterns.alternation("source_recurrence_prefix")
        universal = lexicon_patterns.alternation("source_recurrence_universal")
        bucket = lexicon_patterns.alternation("source_recurrence_bucket")
        suffix = lexicon_patterns.alternation("source_recurrence_suffix")
    except Exception:
        return None
    if not bucket or not (prefix or universal):
        return None
    heads = [item for item in (prefix, universal) if item]
    head = "|".join(f"(?:{item})" for item in heads)
    tail = f"(?:\\s*(?:{suffix}))?" if suffix else ""
    return re.compile(f"(?:{head})\\s*(?P<bucket>{bucket}){tail}")


# calendar 단위 표면어 → IR 단위. 표면어의 소유자는 ``source_recurrence_bucket`` 어휘이고,
# 여기서는 그 표면어가 어느 칸 단위인지만 옮긴다(IR 어휘는 :mod:`event_ir` 소유).
_BUCKET_UNITS: dict[str, str] = {
    "일": "day",
    "주": "week",
    "주차": "week",
    "월": "month",
    "개월": "month",
    "분기": "quarter",
    "년": "year",
}


def _recurrence_hits(query: str) -> list[tuple[Span, str | None]]:
    """칸별 전칭 표지의 (구간, IR 칸 단위). 단위를 모르면 ``None`` 으로 남긴다."""
    pattern = _recurrence_pattern()
    if pattern is None:
        return []
    return [
        (match.span(), _BUCKET_UNITS.get(match.group("bucket") or ""))
        for match in pattern.finditer(query)
    ]


# ── 기간 소유권(어순 문법) ──────────────────────────────────────────────────────────


def _assign_temporal(
    anchors: Sequence[Span],
    clauses: Sequence[temporal_clause.TemporalClause],
) -> dict[int, int]:
    """창 → 그 창을 소유하는 앵커. 규칙은 어순 하나다(수식어는 머리말 앞).

    창은 자기 **뒤**에 오는 앵커 중 가장 가까운 것에 붙는다. 한 앵커를 두 창이 노리면 가까운
    창이 이기고, 진 창은 그 다음 앵커를 찾는다 — 한 앵커에 창을 겹쳐 붙이면 두 기간이 한
    술어에 들어가 조용히 교집합이 된다.

    뒤에 앵커가 하나도 없는 창은 **붙이지 않는다**. 문장 끝의 기간 표현이 앞 절의 창인지
    독립된 요청인지는 어순만으로 알 수 없고, 추측하면 없는 조건이 생긴다.
    """
    candidates: list[tuple[int, int, int]] = []
    for clause_index, clause in enumerate(clauses):
        span = clause.span
        if span is None:
            continue
        for anchor_index, (anchor_start, _anchor_end) in enumerate(anchors):
            if anchor_start < span[1]:
                continue
            candidates.append((anchor_start - span[1], clause_index, anchor_index))

    owner: dict[int, int] = {}
    taken_anchors: set[int] = set()
    taken_clauses: set[int] = set()
    for _gap, clause_index, anchor_index in sorted(candidates):
        if clause_index in taken_clauses or anchor_index in taken_anchors:
            continue
        taken_clauses.add(clause_index)
        taken_anchors.add(anchor_index)
        owner[clause_index] = anchor_index
    return owner


def analyze_clauses(
    query: str,
    catalog: Mapping[str, Any],
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[ClauseSemantics, ...]:
    """원문의 조건 절들을 typed semantics 로 확정한다(원문 좌표 순).

    절 하나 = 시간축을 가진 카탈로그 사건 표면어 하나. 그 자리에서 극성(부정됐는가)·수량자
    (부재/칸별 전칭/존재)·기간(어느 창이 내 것인가)이 함께 정해진다.

    사건을 하나도 못 찾으면 빈 결과다 — 이 계층은 "무엇이 없다"를 말하지 않는다.
    """
    if not isinstance(query, str) or not query.strip():
        return ()

    aliases = _event_aliases(catalog)
    if not aliases:
        return ()

    negated: dict[str, tuple[Span, ...]] = {
        event: audience_frame.local_negation_spans(query, terms)
        for event, terms in aliases.items()
    }
    recurrences = _recurrence_hits(query)
    qualifiers = _qualifier_spans(query, catalog)

    anchors: list[tuple[Span, str]] = []
    for event, terms in aliases.items():
        anchors.extend((span, event) for span in _anchor_spans(query, terms))
    # 같은 자리를 두 사건이 주장하면 긴 표면어가 이긴다(짧은 별칭의 부분 매치 방지).
    anchors = [
        item
        for item in anchors
        if not any(
            other is not item
            and other[0][0] <= item[0][0]
            and item[0][1] <= other[0][1]
            and (other[0][1] - other[0][0]) > (item[0][1] - item[0][0])
            for other in anchors
        )
    ]
    # **선언된 더 긴 표면어 안의 사건 별칭은 사건이 아니다.** 소유 대장의 규칙이다(같은 근거
    # 표현은 한 번만 해석된다). 구현 중 두 번 실측했다.
    #
    # * 지표 — ``2026년 2월과 3월의 **구매**금액이 증가한`` 이 ``Exists(2월 구매)`` 로 좁혀졌다.
    # * 필드 — ``향후 7일 안에 **구매**예정일이 도래하는`` 이 ``EO.ORDER_DATE >= 7일 전`` 이 됐다.
    #   미래 조건이 **과거 창으로 뒤집힌** 것이고, 이 부류가 가장 나쁘다(조용한 의미 반전).
    metric_spans = _declared_surface_spans(query, catalog)
    anchors = [
        item
        for item in anchors
        if not any(
            span[0] <= item[0][0] and item[0][1] <= span[1] and span != item[0]
            for span in metric_spans
        )
    ]
    anchors.sort(key=lambda item: item[0])
    if not anchors:
        return ()

    temporal_clauses = [
        clause
        for clause in temporal_clause.combine_temporal_clauses(query, literal_bindings)
        if clause.is_quantified and clause.span is not None
    ]
    owner = _assign_temporal([span for span, _event in anchors], temporal_clauses)
    by_anchor = {anchor_index: clause_index for clause_index, anchor_index in owner.items()}

    found: list[ClauseSemantics] = []
    for anchor_index, ((start, end), event) in enumerate(anchors):
        is_negated = any(
            span[0] <= start and end <= span[1] for span in negated.get(event, ())
        )
        clause_index = by_anchor.get(anchor_index)
        owned = (
            ClauseTemporal(temporal_clauses[clause_index])
            if clause_index is not None
            else None
        )
        bucket_unit: str | None = None
        quantifier = Quantifier.NEVER if is_negated else Quantifier.EXISTS
        if not is_negated:
            # 칸별 전칭은 이 앵커 **앞**에 있는 표지 중 가장 가까운 것이 정한다. 사건 앵커에
            # 붙었을 때는 상태가 아니라 발생에 대한 전칭이다('매월 한 번 이상 구매').
            preceding = [
                (span, unit) for span, unit in recurrences if span[1] <= start
            ]
            if preceding:
                span, unit = max(preceding, key=lambda item: item[0][1])
                if start - span[1] <= _RECURRENCE_BUDGET:
                    quantifier = Quantifier.EVERY_BUCKET_OCCURRENCE
                    bucket_unit = unit
                    start = min(start, span[0])
        qualifier = _qualifier_for_anchor(qualifiers, (start, end))
        evidence_span = (start, end)
        found.append(
            ClauseSemantics(
                clause_id=f"clause@{evidence_span[0]}-{evidence_span[1]}",
                event=event,
                polarity=Polarity.NEGATIVE if is_negated else Polarity.POSITIVE,
                quantifier=quantifier,
                span=evidence_span,
                evidence=query[evidence_span[0]:evidence_span[1]],
                temporal=owned,
                bucket_unit=bucket_unit,
                qualified=qualifier is not None,
                qualifier_evidence=(
                    query[qualifier[0]:qualifier[1]] if qualifier is not None else None
                ),
            )
        )
    return tuple(found)


def _declared_surface_spans(query: str, catalog: Mapping[str, Any]) -> tuple[Span, ...]:
    """카탈로그 **지표·필드** 표면어가 나타난 구간들('구매금액'·'구매예정일').

    사건 별칭이 이 안에 들어 있으면 그 글자는 사건이 아니라 그 심볼의 일부다. 지표와 필드를
    함께 보는 이유는 둘 다 사건 별칭을 접두어로 가질 수 있기 때문이고, 선언의 소유자는
    카탈로그이므로 여기서 낱말을 적지 않는다.
    """
    surfaces: set[str] = set()
    for section in ("metrics", "fields"):
        declarations = catalog.get(section)
        if not isinstance(declarations, Mapping):
            continue
        for symbol, declaration in declarations.items():
            if not isinstance(declaration, Mapping):
                continue
            aliases = declaration.get("aliases")
            for term in (symbol, declaration.get("label"), *(aliases or ())):
                if isinstance(term, str) and len(term) >= 2:
                    surfaces.add(term)
    return tuple(_anchor_spans(query, sorted(surfaces)))


def _qualifier_spans(query: str, catalog: Mapping[str, Any]) -> tuple[Span, ...]:
    """카탈로그 **값 도메인** 청구가 나타난 구간들('앱으로'·'골드').

    청구의 소유자는 :mod:`canonical_audience_claims` 하나다 — 값 표면어를 여기서 다시 읽으면
    같은 사전이 두 벌이 된다. 청구를 못 읽으면 한정어가 **없는 것으로** 둔다: 한정을 지어내면
    답할 수 있는 맨 부재까지 미지원으로 닫힌다(추측 금지의 방향은 언제나 덜 막는 쪽).
    """
    import canonical_audience_claims  # 지연 import(순환 방지)

    try:
        claims = canonical_audience_claims.catalog_value_claims(query, catalog)
    except Exception:
        return ()
    return tuple(
        sorted(
            (int(start), int(end))
            for claim in claims or ()
            if isinstance(claim, Mapping)
            for start, end in claim.get("hits") or ()
        )
    )


def _qualifier_for_anchor(qualifiers: Sequence[Span], anchor: Span) -> Span | None:
    """이 사건을 한정하는 값 구간. 겹치거나 **바로 앞**에 있는 것만 이 절의 것이다.

    어순 규칙은 이 모듈이 기간에 쓰는 것과 같다 — 한국어에서 한정어는 머리말 앞에 온다.
    """
    start, end = anchor
    for qualifier in qualifiers:
        overlaps = not (qualifier[1] <= start or qualifier[0] >= end)
        precedes = qualifier[1] <= start and start - qualifier[1] <= _QUALIFIER_BUDGET
        if overlaps or precedes:
            return qualifier
    return None


# 칸 표지와 사건 사이에 허용하는 글자 수(수량 표현 + 조사 정도: '매월 한 번 이상 구매').
# 넉넉하면 다른 절의 표지를 끌어오고 좁으면 정상 표현을 놓친다.
_RECURRENCE_BUDGET = 12


def clause_owning_span(
    clauses: Sequence[ClauseSemantics], span: Span
) -> ClauseSemantics | None:
    """이 원문 구간을 소유한 절. 없으면 ``None``.

    기간 결핍 신고를 절 단위로 정산하는 단일 통로다 — 문장 전체가 아니라 **그 신고가 가리킨
    절**이 기간을 말했는지 묻는다.
    """
    start, end = span
    for clause in clauses:
        if clause.covers(start, end):
            return clause
    return None


def clear_cache() -> None:
    _recurrence_pattern.cache_clear()


__all__ = [
    "BARE_ABSENCE_CAPABILITIES",
    "QUALIFIED_ABSENCE_CAPABILITIES",
    "REQUIRED_CAPABILITY",
    "ClauseSemantics",
    "ClauseTemporal",
    "Polarity",
    "Provenance",
    "Quantifier",
    "analyze_clauses",
    "clause_owning_span",
    "clear_cache",
]
