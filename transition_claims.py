"""원문 문장 → 전이 요청(무엇을 골랐는가).

책임 경계
---------
이 모듈은 **원문에서 무엇을 골랐는가**만 책임진다. 계약 검증과 IR 조립은
:mod:`transition_metrics` 한 곳이 소유하고, 여기서는 그 함수를 부른다. 데이터베이스에는
접근하지 않으며 생성된 조건을 실행하지도 않는다.

축 이름을 모른다
----------------
``가치등급``·``회원등급`` 같은 문자열이 이 파일에 없다. 값 표면어의 정체는 카탈로그 값 사전이
정하고(:func:`canonical_audience_claims.catalog_value_claims`), 그 값 도메인을 다루는 전이 지표는
선언으로 찾는다(:func:`transition_metrics.transition_metric_for_domain`). 그래서 새 전이 축은
카탈로그 한 항목으로 늘고 이 파일은 그대로다.

무엇을 전이로 인정하는가
------------------------
1. 원문에 ``CHANGE_BETWEEN`` 마커가 하나 있다(어휘 소유자는 :mod:`targeting_domain`).
2. 그 마커와 **같은 절**에 같은 값 도메인의 값이 정확히 둘 있다.
3. 원문 어순이 ``from → to`` 를 정한다.
4. 방향어(승급/강등)가 있으면 값 순서와 모순되지 않는지 :mod:`transition_metrics` 가 검증한다.

마커 구간이 아니라 **절**로 값을 찾는 이유는 어휘의 모양이다. ``골드에서 VIP로 변경`` 은 마커
하나가 값을 품지만 ``골드에서 VIP로 승급`` 의 마커는 방향어(``승급``) 두 글자뿐이라 값이 마커
밖에 있다. 절 단위로 보면 두 모양이 같은 규칙으로 읽힌다.

기간이 붙은 전이
----------------
``최근 3개월 동안 골드에서 VIP로 변경`` 처럼 마커와 같은 절에 기간 표현이 있으면 합성하지
않는다(``transition_period_unsupported``). 기간 전이는 이력 행이나 이벤트 레코드가 필요한
모양이고, 한 행의 현재값/직전값 비교로 접으면 **다른 뜻**이 된다. 다른 조건이 이미 그 기간을
소유했다면 호출자가 ``consumed_spans`` 로 알려 준다 — 그 구간은 전이의 한정어가 아니다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import audience_frame
import calendar_window
import canonical_audience_claims
import event_ir
import resolved_semantic_catalog
import sql_dialect
import targeting_domain
import temporal_semantics
import transition_metrics

OWNER = "transition_metrics.catalog_value_pair"

# 이 계층(원문 판정)이 내는 사유. 계약 사유는 :mod:`transition_metrics` 가 소유한다.
VALUES_NOT_PAIRED = "transition_values_not_paired"
DOMAIN_MIXED = "transition_domain_mixed"
MARKER_AMBIGUOUS = "transition_marker_ambiguous"
PERIOD_UNSUPPORTED = "transition_period_unsupported"

# 기간 표현을 **찾기만** 할 때 쓰는 기준일. 상대 달력어('지난달'·'어제')는 기준일이 있어야
# 문법이 인식하지만, 이 모듈이 쓰는 것은 그 표현의 **구간**뿐이고 구간은 기준일과 무관하다
# (실측: 같은 문장에서 today 를 7년 옮겨도 스팬이 같다). 그래서 값이 결과를 바꾸지 않으며,
# 실제 창을 계산해야 하는 호출자는 `today` 로 자기 기준일을 주입한다.
SPAN_PROBE_DATE = date(2000, 1, 1)

Span = tuple[int, int]


@dataclass(frozen=True)
class TransitionRequest:
    """원문이 고른 전이 하나 — 아직 IR 이 아니다."""

    metric_id: str
    value_domain: str
    from_value: str
    to_value: str
    direction: str | None
    evidence: event_ir.Evidence
    marker_text: str


@dataclass(frozen=True)
class TransitionSynthesis:
    """전이 조건 + 어떤 선언과 어떤 원문 근거로 만들었는지의 영수증."""

    expression: event_ir.Condition
    receipt: dict[str, Any]


@dataclass(frozen=True)
class TransitionRejection:
    """전이를 말했지만 합성하지 않는다 — 사유와 근거를 남긴다(조용히 버리지 않는다)."""

    code: str
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _ValueHit:
    domain: str
    canonical: str
    start: int
    end: int


def _evidence(query: str, start: int, end: int) -> event_ir.Evidence:
    return event_ir.Evidence(text=query[start:end], start=start, end=end)


def _evidence_dict(query: str, start: int, end: int) -> dict[str, Any]:
    return {"text": query[start:end], "start": start, "end": end}


def _transition_markers(query: str) -> list[temporal_semantics.TemporalMarker]:
    """원문의 값 전이 마커. 어휘는 도메인이 소유하고 여기서는 연산자로만 거른다."""

    lexicon = targeting_domain.temporal_lexicon()
    return [
        marker
        for marker in lexicon.detect(query)
        if marker.operator == temporal_semantics.CHANGE_BETWEEN
    ]


def _value_hits(query: str, snapshot: Mapping[str, Any]) -> list[_ValueHit]:
    """카탈로그 값 청구 → 평평한 히트 목록(더 긴 표면어가 짧은 것을 삼킨다)."""

    hits = [
        _ValueHit(
            domain=str(claim.get("domain")),
            canonical=str(claim.get("canonical")),
            start=int(start),
            end=int(end),
        )
        for claim in canonical_audience_claims.catalog_value_claims(query, snapshot)
        for start, end in claim.get("hits") or ()
    ]
    # 더 긴 표면어 안에 든 짧은 표면어는 독립된 값 언급이 아니다. 구간이 판정하므로 새 값이
    # 늘어도 이 규칙은 그대로다(:mod:`member_scalar_metric_claims` 의 별칭 규칙과 같은 뜻).
    return [
        hit
        for hit in hits
        if not any(
            other is not hit
            and other.start <= hit.start
            and hit.end <= other.end
            and (other.end - other.start) > (hit.end - hit.start)
            for other in hits
        )
    ]


def _direction_spans(query: str) -> list[tuple[str, Span]]:
    """방향 표면어의 (방향, 구간). 어휘는 :mod:`targeting_domain` 이 소유한다."""

    found: list[tuple[str, Span]] = []
    for cue, direction in targeting_domain.transition_direction_cues().items():
        cursor = 0
        folded = query.casefold()
        while (start := folded.find(cue, cursor)) >= 0:
            found.append((direction, (start, start + len(cue))))
            cursor = start + max(1, len(cue))
    return found


def _period_spans(query: str, today: date) -> list[Span]:
    """기간·시점 표현의 구간. 문법의 소유자는 :mod:`calendar_window` 하나다."""

    spans: list[Span] = [
        (start, end)
        for _window, start, end in calendar_window.parse_calendar_window_spans(
            query, today=today
        )
    ]
    compact = query.replace(" ", "").casefold()
    for candidate in calendar_window.duration_window_candidates(compact):
        # 억제된 후보('3년 전' 안의 '3년')도 기간 한정어다 — 롤링 창이 아닐 뿐 시점을 말한다.
        span = audience_frame.compact_to_source_span(query, candidate.start, candidate.end)
        if span is not None:
            spans.append(span)
    return sorted(set(spans))


def _covered(span: Span, owned: Iterable[Span]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in owned)


def detect_transition_request(
    query: str,
    *,
    snapshot: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    consumed_spans: Sequence[Span] = (),
    today: date | None = None,
) -> TransitionRequest | TransitionRejection | None:
    """원문에서 전이 요청 하나를 고르거나, 사유와 함께 닫는다.

    ``None`` 은 "이 문장은 전이를 말하지 않았다"이고, :class:`TransitionRejection` 은
    "전이를 말했지만 만들 수 없다"이다. 둘을 구분하는 이유는 호출자의 결말이 다르기 때문이다.
    """

    if not isinstance(query, str) or not query.strip():
        return None
    markers = _transition_markers(query)
    if not markers:
        return None
    if len(markers) > 1:
        first, last = markers[0], markers[-1]
        return TransitionRejection(
            MARKER_AMBIGUOUS,
            "한 문장에 값 전이 표현이 둘 이상입니다. 어느 축의 전이인지 확정할 수 없습니다.",
            _evidence_dict(query, first.start, last.end),
        )
    marker = markers[0]
    marker_span: Span = (marker.start, marker.end)

    hits = [
        hit
        for hit in _value_hits(query, snapshot)
        if audience_frame.in_same_clause(query, (hit.start, hit.end), marker_span)
    ]
    hits.sort(key=lambda item: (item.start, item.end))
    if len(hits) != 2:
        start = min([marker.start, *(hit.start for hit in hits)])
        end = max([marker.end, *(hit.end for hit in hits)])
        return TransitionRejection(
            VALUES_NOT_PAIRED,
            "값 전이는 바뀌기 전 값과 바뀐 뒤 값이 모두 필요합니다. "
            f"이 문장에서 확인된 선언 값은 {len(hits)}개입니다.",
            _evidence_dict(query, start, end),
        )
    source_hit, target_hit = hits
    span_start = min(marker.start, source_hit.start)
    span_end = max(marker.end, target_hit.end)
    if source_hit.domain != target_hit.domain:
        return TransitionRejection(
            DOMAIN_MIXED,
            f"전이의 두 값이 서로 다른 값 도메인({source_hit.domain}, {target_hit.domain})에 "
            "속합니다. 한 축의 값 변화만 표현할 수 있습니다.",
            _evidence_dict(query, span_start, span_end),
        )

    period = [
        span
        for span in _period_spans(query, today or SPAN_PROBE_DATE)
        if not _covered(span, consumed_spans)
        and audience_frame.in_same_clause(query, span, marker_span)
    ]
    if period:
        return TransitionRejection(
            PERIOD_UNSUPPORTED,
            "기간이 명시된 값 전이는 아직 지원하지 않습니다. 현재 지원하는 전이는 한 행에 "
            "함께 있는 현재값과 직전값의 비교뿐이며, 기간 전이를 그 비교로 접으면 뜻이 달라집니다.",
            _evidence_dict(query, min(period[0][0], span_start), max(period[-1][1], span_end)),
        )

    directions = {
        direction
        for direction, span in _direction_spans(query)
        if audience_frame.in_same_clause(query, span, marker_span)
    }
    if len(directions) > 1:
        return TransitionRejection(
            transition_metrics.DIRECTION_UNVERIFIABLE,
            f"한 절에 서로 다른 방향 표현({sorted(directions)})이 있어 값의 이동 방향을 "
            "확정할 수 없습니다.",
            _evidence_dict(query, span_start, span_end),
        )
    direction = next(iter(directions)) if directions else None

    try:
        metric = transition_metrics.transition_metric_for_domain(catalog, source_hit.domain)
    except transition_metrics.TransitionContractError as exc:
        return TransitionRejection(
            exc.code, str(exc), _evidence_dict(query, span_start, span_end)
        )

    return TransitionRequest(
        metric_id=metric.id,
        value_domain=source_hit.domain,
        from_value=source_hit.canonical,
        to_value=target_hit.canonical,
        direction=direction,
        evidence=_evidence(query, span_start, span_end),
        marker_text=marker.text,
    )


def synthesize_transition_predicate(
    query: str,
    *,
    snapshot: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    consumed_spans: Sequence[Span] = (),
    today: date | None = None,
    dialect: sql_dialect.SqlDialect | None = None,
) -> TransitionSynthesis | TransitionRejection | None:
    """전이 조건 하나를 합성하거나, 사유와 함께 닫는다."""

    request = detect_transition_request(
        query,
        snapshot=snapshot,
        catalog=catalog,
        consumed_spans=consumed_spans,
        today=today,
    )
    if request is None or isinstance(request, TransitionRejection):
        return request
    try:
        lowered = transition_metrics.lower_transition_metric(
            catalog,
            request.metric_id,
            from_value=request.from_value,
            to_value=request.to_value,
            direction=request.direction,
            evidence=request.evidence,
            dialect=dialect,
        )
    except transition_metrics.TransitionContractError as exc:
        # 계약이 어긋나면 합성하지 않는다. 반박할 근거가 없는데 반박하는 것이 이 경로가
        # 피하려는 실패다 — 사유는 선언을 짚어 그대로 전달한다.
        return TransitionRejection(
            exc.code,
            str(exc),
            _evidence_dict(query, request.evidence.start, request.evidence.end),
        )

    receipt = dict(lowered.receipt)
    receipt.update({
        "owner": OWNER,
        "marker_text": request.marker_text,
        "evidence": {
            "text": request.evidence.text,
            "start": request.evidence.start,
            "end": request.evidence.end,
        },
    })
    return TransitionSynthesis(expression=lowered.expression, receipt=receipt)


__all__ = [
    "DOMAIN_MIXED",
    "MARKER_AMBIGUOUS",
    "OWNER",
    "PERIOD_UNSUPPORTED",
    "SPAN_PROBE_DATE",
    "VALUES_NOT_PAIRED",
    "TransitionRejection",
    "TransitionRequest",
    "TransitionSynthesis",
    "detect_transition_request",
    "synthesize_transition_predicate",
]
