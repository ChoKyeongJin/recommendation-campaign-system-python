"""원문 시간 한정어 → canonical Temporal Semantic IR(:mod:`temporal_ir`).

책임 경계
---------
이 모듈은 **어떤 시간 연산인가**를 새로 판정하지 않는다. 그 판정은 이미 두 곳이 소유한다.

* 표면형 → 범용 연산자: :func:`targeting_domain.temporal_lexicon`
  (연산자 어휘의 닫힌 집합은 :mod:`temporal_semantics`).
* 값·축 표면형 → canonical 값/값 도메인: :func:`canonical_audience_claims.catalog_value_claims`.

여기서 하는 일은 그 연산자를 :mod:`temporal_ir` 의 ``selector × quantifier × predicate``
조합으로 **선언표에 따라** 옮기는 것뿐이다. 그래서 이 파일에는 한국어 낱말도, 문장 유형별
분기도, 물리 컬럼 이름도 없다 — 새 시간 표현은 :data:`_OPERATOR_PLANS` 한 항목이거나
카탈로그 한 항목이며, 새 값 축은 카탈로그만 늘면 된다.

왜 새 IR 타입을 만들지 않는가
-----------------------------
'시점 값 조회 / 직접 전이 / 기간 내 전이 / 변경 횟수 / 기간 유지 / 매 칸 존재'는 서로 다른
**노드**가 아니라 같은 세 축(어느 관측을 · 몇 개가 · 무엇이 성립)의 서로 다른 **값**이다.
문형마다 dataclass 를 만들면 같은 뜻을 두 언어가 말하게 되고, 그 순간 lowering·영수증·
capability 검증을 두 벌 유지해야 한다(CLAUDE.md §4). 아래 선언표가 그 사상을 대신한다.

    ValueAt              AsOfSelector | PreviousSelector + Exists + StatePredicate
    ChangeBetween        AsOfSelector                    + Exists + TransitionPredicate
    TransitionDuring     WindowSelector                  + Exists + TransitionPredicate
    HeldValueDuring      WindowSelector + AllObservations + StatePredicate
    PresentEveryPeriod   WindowSelector + EveryBucket     + StatePredicate
    ChangeCount          WindowSelector + Exists          + ChangeCountPredicate
    ConsecutivePeriods   WindowSelector + ConsecutiveBuckets + StatePredicate

전이 값 쌍의 소유자
-------------------
'A에서 B로'의 값 쌍 선택·어순·방향 검증은 :mod:`transition_claims` 가 이미 소유하므로 다시
구현하지 않고 그 판정기를 부른다. 기간이 같은 절에 있으면 그 구간을 ``consumed_spans`` 로
넘겨 준다 — 기간의 소유자가 이 모듈(WindowSelector)이라는 뜻이고, 그래야 같은 문장이
'기간 미지원'으로 닫히지 않는다.

지원하지 않는 것은 조용히 버리지 않는다
-------------------------------------
연산이 낮춰지지 않으면(데이터 표현이 그 질문에 답할 수 없으면) :class:`TemporalClaimRejection`
으로 사유와 근거 구간을 남긴다. 근사하거나 비슷한 연산으로 갈아타지 않는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import audience_frame
import calendar_window
import canonical_audience_claims
import event_ir
import resolved_semantic_catalog
import targeting_domain
import temporal_ir
import temporal_semantics
import transition_claims
import transition_metrics
from temporal_ir import semantic_ir as sir

OWNER = "temporal_ir.catalog_temporal_condition"

# ── 이 계층(원문 판정)이 내는 사유 ────────────────────────────────────────────────
# 낮춤 계약의 사유는 temporal_ir 이 소유한다. 여기 있는 것은 "원문에서 조건을 만들 수 없다"뿐.
VALUE_DOMAIN_UNRESOLVED = "temporal_value_domain_unresolved"
METRIC_NOT_DECLARED = "temporal_metric_not_declared"
METRIC_AMBIGUOUS = "temporal_metric_ambiguous"
DOMAIN_MIXED = "temporal_domain_mixed"
VALUE_COUNT_MISMATCH = "temporal_value_count_mismatch"
INTERVAL_MISSING = "temporal_interval_missing"
INTERVAL_FORBIDDEN = "temporal_interval_forbidden"
ANCHOR_SHAPE_UNSUPPORTED = "temporal_anchor_shape_unsupported"
BUCKET_UNIT_MISSING = "temporal_bucket_unit_missing"
BUCKET_COUNT_MISSING = "temporal_bucket_count_missing"
CHANGE_COUNT_VALUE_MISSING = "temporal_change_count_value_missing"
OPERATOR_PLAN_MISSING = "temporal_operator_plan_missing"

# 기간 표현을 **찾기만** 할 때 쓰는 기준일. 구간의 위치는 기준일과 무관하므로 값이 결과를
# 바꾸지 않는다(:mod:`transition_claims` 와 같은 규약).
SPAN_PROBE_DATE = date(2000, 1, 1)

Span = tuple[int, int]

# calendar_window 의 복수형 단위 표기 → temporal_ir 의 닫힌 TimeUnit 어휘.
_WINDOW_UNITS: dict[str, str] = {
    "days": "day",
    "weeks": "week",
    "months": "month",
    "years": "year",
}
# 달력 단위는 칸 경계에 정렬되는 것이 자연스럽고(‘지난 6개월’ = 여섯 달), 일·주는 굴러가는
# 창이 자연스럽다. 정책을 여기 한 줄로 두는 이유는 문장마다 달라지면 안 되기 때문이다.
_CALENDAR_UNITS = frozenset({"month", "year"})


@dataclass(frozen=True)
class _OperatorPlan:
    """범용 시간 연산자 하나를 IR 조합으로 옮기는 선언.

    ``values`` 는 술어가 요구하는 **원문 값의 개수**다(전이는 2, 상태는 1, 변경 횟수는 0).
    ``interval`` 은 구간 요구('required'/'optional'/'forbidden')이며, 요구가 어긋나면
    조용히 기본값을 넣지 않고 사유와 함께 닫는다.
    """

    selector: str
    quantifier: str
    predicate: str
    values: int
    interval: str = "forbidden"
    bucket: bool = False
    count: bool = False
    null_policy: str = "exclude"


# 범용 연산자 → IR 조합. 왼쪽은 :mod:`temporal_semantics` 의 닫힌 집합이고 오른쪽은
# :mod:`temporal_ir` 의 타입 조합이다. **낱말은 어느 쪽에도 없다.**
_OPERATOR_PLANS: dict[str, _OperatorPlan] = {
    temporal_semantics.AS_OF: _OperatorPlan(
        selector="as_of", quantifier="exists", predicate="state",
        values=1, interval="optional",
    ),
    temporal_semantics.IMMEDIATELY_PRECEDING: _OperatorPlan(
        selector="previous", quantifier="exists", predicate="state",
        values=1, interval="optional",
    ),
    temporal_semantics.WITHIN_INTERVAL: _OperatorPlan(
        selector="window", quantifier="exists", predicate="state",
        values=1, interval="required",
    ),
    temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL: _OperatorPlan(
        selector="window", quantifier="exists", predicate="state",
        values=1, interval="required",
    ),
    temporal_semantics.NEVER_IN_INTERVAL: _OperatorPlan(
        selector="window", quantifier="none", predicate="state",
        values=1, interval="required",
    ),
    temporal_semantics.THROUGHOUT_INTERVAL: _OperatorPlan(
        # '내내'는 관측 전칭이다. null 정책이 exclude 면 값이 빈 관측이 조용히 통과하므로
        # temporal.all 은 treat_as_mismatch 만 받는다(계약이 그렇게 좁혀져 있다).
        selector="window", quantifier="all_observations", predicate="state",
        values=1, interval="required", null_policy="treat_as_mismatch",
    ),
    temporal_semantics.EVERY_SUBINTERVAL: _OperatorPlan(
        selector="window", quantifier="every_bucket", predicate="state",
        values=1, interval="required", bucket=True,
    ),
    temporal_semantics.UNCHANGED_THROUGHOUT: _OperatorPlan(
        # '한 번도 바뀌지 않았다'는 관측 전체에 대한 진술이다. exists 로 두면 "안 바뀐 관측이
        # 하나라도 있다"가 되어 정반대에 가까운 집합이 나온다.
        selector="window", quantifier="all_observations", predicate="unchanged",
        values=0, interval="required",
    ),
    temporal_semantics.CHANGE_BETWEEN: _OperatorPlan(
        # 기간이 없으면 '가장 최근 관측에서의 전이'(as_of), 있으면 '그 구간 안의 전이'(window).
        selector="as_of", quantifier="exists", predicate="transition",
        values=2, interval="optional",
    ),
    temporal_semantics.CHANGE_COUNT: _OperatorPlan(
        selector="window", quantifier="exists", predicate="change_count",
        values=0, interval="required", count=True,
    ),
    temporal_semantics.CONSECUTIVE_SUBINTERVALS: _OperatorPlan(
        selector="window", quantifier="consecutive_buckets", predicate="state",
        values=1, interval="required", bucket=True,
    ),
}

_QUANTIFIERS: dict[str, Any] = {
    "exists": sir.ExistsQuantifier,
    "none": sir.NoneQuantifier,
    "all_observations": sir.AllObservationsQuantifier,
    "every_bucket": sir.EveryBucketQuantifier,
    "consecutive_buckets": sir.ConsecutiveBucketsQuantifier,
}


@dataclass(frozen=True)
class TemporalClaimRequest:
    """원문이 고른 시간 조건 하나 — 아직 낮추지 않은 상태."""

    operator: str
    metric_id: str
    value_domain: str | None
    condition: sir.TemporalCondition
    spans: tuple[Span, ...]


@dataclass(frozen=True)
class TemporalClaimSynthesis:
    """낮춘 조건 + 그것을 증명한 선언·원문 근거의 영수증."""

    expression: event_ir.Condition
    receipts: tuple[dict[str, Any], ...]
    requests: tuple[TemporalClaimRequest, ...]
    spans: tuple[Span, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemporalClaimRejection:
    """시간 조건을 말했지만 만들 수 없다 — 사유와 근거를 남긴다."""

    code: str
    message: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _ValueHit:
    domain: str
    canonical: str
    start: int
    end: int


# ── 원문 조각 ────────────────────────────────────────────────────────────────────


def _evidence(query: str, start: int, end: int) -> sir.Evidence:
    return sir.Evidence(text=query[start:end], start=start, end=end)


def _evidence_dict(query: str, start: int, end: int) -> dict[str, Any]:
    return {"text": query[start:end], "start": start, "end": end}


def _markers(query: str) -> list[temporal_semantics.TemporalMarker]:
    """원문의 시간 한정어 마커. 어휘의 소유자는 :mod:`targeting_domain` 하나다."""

    return targeting_domain.temporal_lexicon().detect(query)


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


def _period_candidates(
    query: str, today: date
) -> list[tuple[Span, dict[str, Any], str]]:
    """기간·시점 표현의 (구간, 창, 종류). 문법의 소유자는 :mod:`calendar_window` 하나다."""

    found: list[tuple[Span, dict[str, Any], str]] = []
    for window, start, end in calendar_window.parse_calendar_window_spans(
        query, today=today
    ):
        found.append(((start, end), dict(window), "calendar"))
    compact = query.replace(" ", "").casefold()
    for candidate in calendar_window.duration_window_candidates(compact):
        span = audience_frame.compact_to_source_span(query, candidate.start, candidate.end)
        if span is None:
            continue
        try:
            window = calendar_window.duration_window_from_candidate(candidate)
        except (ValueError, TypeError):
            continue
        found.append((span, dict(window), "duration"))
    return sorted(found, key=lambda item: item[0])


def _covered(span: Span, owned: Iterable[Span]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in owned)


# ── 원문 조각 → IR 인자 ──────────────────────────────────────────────────────────


def _relative_window(window: Mapping[str, Any]) -> sir.RelativeWindow | None:
    unit = _WINDOW_UNITS.get(str(window.get("unit")))
    amount = window.get("value")
    if unit is None or not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None
    mode = "calendar" if unit in _CALENDAR_UNITS else "rolling"
    return sir.RelativeWindow(
        amount=amount, unit=unit, mode=mode, include_current=True
    )


def _absolute_window(
    window: Mapping[str, Any], timezone: str
) -> sir.AbsoluteWindow | None:
    start_text, end_text = window.get("from"), window.get("to")
    if not (isinstance(start_text, str) and isinstance(end_text, str)):
        return None
    try:
        start_day = datetime.strptime(start_text, "%Y%m%d").date()
        end_day = datetime.strptime(end_text, "%Y%m%d").date()
    except ValueError:
        return None
    zone = ZoneInfo(timezone)
    start = datetime.combine(start_day, time.min, tzinfo=zone)
    # 달력 창은 마지막 날을 **포함**하고 IR 의 구간은 반개구간이므로 하루를 더한다.
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=zone)
    if end_exclusive <= start:
        return None
    return sir.AbsoluteWindow(start=start, end_exclusive=end_exclusive)


def _window_from(
    window: Mapping[str, Any], kind: str, timezone: str
) -> sir.TemporalWindow | None:
    if kind == "duration":
        return _relative_window(window)
    return _absolute_window(window, timezone)


def _calendar_anchor(
    window: Mapping[str, Any], kind: str, today: date
) -> sir.RelativeAnchor | None:
    """달력 표현 하나를 **달력 시점**으로 읽는다('지난달 말 기준' → 한 달 전의 끝).

    절대 순간(:class:`sir.AbsoluteAnchor`)으로 읽지 않는 것이 중요하다. 칸 단위로 적재된
    관측은 칸 안의 임의 순간을 대표하지 못하므로, 절대 시각을 주면 낮춤이
    ``temporal_anchor_grain_too_fine`` 으로 정당하게 닫는다. 달력 표현이 말하는 것은
    애초에 순간이 아니라 **칸**이므로 그 칸을 그대로 시점으로 넘긴다.
    """

    if kind != "calendar":
        return None
    start_text, end_text = window.get("from"), window.get("to")
    if not (isinstance(start_text, str) and isinstance(end_text, str)):
        return None
    try:
        start = datetime.strptime(start_text, "%Y%m%d").date()
        end = datetime.strptime(end_text, "%Y%m%d").date()
    except ValueError:
        return None
    if start > end:
        return None
    if start == end:
        return sir.RelativeAnchor(
            offset=(start - today).days, unit="day", boundary="end"
        )
    month_end = date(
        start.year, start.month, calendar_window.month_last_day(start.year, start.month)
    )
    if start.day == 1 and end == month_end:
        offset = (start.year - today.year) * 12 + (start.month - today.month)
        return sir.RelativeAnchor(offset=offset, unit="month", boundary="end")
    if (start.month, start.day, end.month, end.day) == (1, 1, 12, 31) and start.year == end.year:
        return sir.RelativeAnchor(
            offset=start.year - today.year, unit="year", boundary="end"
        )
    return None


def _coarsest_grain(
    runtime: temporal_ir.TemporalRuntime, metric_id: str, operator: str
) -> str | None:
    """그 연산자를 받겠다고 선언한 관측들의 가장 굵은 칸 단위."""

    order = {"day": 0, "week": 1, "month": 2, "year": 3}
    grains = [
        str(binding.semantic_grain)
        for binding in runtime.temporal_catalog.bindings_for(metric_id)
        if operator in binding.supported_operators
    ]
    if not grains:
        return None
    return max(grains, key=lambda grain: order.get(grain, 0))


# ── 지표 해석 ────────────────────────────────────────────────────────────────────


def temporal_metric_for_domain(
    runtime: temporal_ir.TemporalRuntime, domain: str
) -> str | None:
    """값 도메인을 다루는 시간 지표. 선언으로 찾으므로 축 이름을 몰라도 된다."""

    matches = sorted(
        metric_id
        for metric_id, spec in runtime.temporal_catalog.metrics.items()
        if spec.value_domain == domain
    )
    if len(matches) != 1:
        return None
    return matches[0]


def _domain_metric_count(runtime: temporal_ir.TemporalRuntime, domain: str) -> int:
    return sum(
        1
        for spec in runtime.temporal_catalog.metrics.values()
        if spec.value_domain == domain
    )


# ── 조건 조립 ────────────────────────────────────────────────────────────────────


def _selector(
    plan: _OperatorPlan,
    *,
    anchor: sir.Anchor,
    window: sir.TemporalWindow | None,
    bucket: str | None,
) -> sir.Selector:
    if plan.selector == "as_of":
        return sir.AsOfSelector(anchor=anchor, strategy=sir.ExactBucket())
    if plan.selector == "previous":
        return sir.PreviousSelector(anchor=anchor, previous_kind="bucket")
    if window is None:  # pragma: no cover - 호출자가 먼저 막는다
        raise ValueError("window selector requires a window")
    return sir.WindowSelector(window=window, bucket=bucket, anchor=anchor)


def _quantifier(plan: _OperatorPlan, *, bucket_count: int | None) -> sir.Quantifier:
    if plan.quantifier == "consecutive_buckets":
        if bucket_count is None:  # pragma: no cover - 호출자가 먼저 막는다
            raise ValueError("consecutive quantifier requires a bucket count")
        return sir.ConsecutiveBucketsQuantifier(bucket_count=bucket_count)
    return _QUANTIFIERS[plan.quantifier]()


def _window_unit(window: sir.TemporalWindow | None) -> str | None:
    """구간 자체가 말하는 칸 단위('3개월 연속'의 '개월')."""

    return str(window.unit) if isinstance(window, sir.RelativeWindow) else None


def _predicate(
    plan: _OperatorPlan,
    *,
    values: Sequence[str],
    count: Decimal | None,
) -> sir.Predicate:
    if plan.predicate == "state":
        return sir.StatePredicate(
            comparison=sir.Comparison(operator="=", value=values[0]),
            null_policy=plan.null_policy,
        )
    if plan.predicate == "transition":
        return sir.TransitionPredicate(
            from_value=values[0],
            to_value=values[1],
            transition_mode="direct_observed_transition",
        )
    if plan.predicate == "unchanged":
        return sir.UnchangedPredicate(observation_semantics="observed_values_equal")
    if count is None:  # pragma: no cover - 호출자가 먼저 막는다
        raise ValueError("change count predicate requires a count")
    return sir.ChangeCountPredicate(
        transition=sir.AnyValueChange(),
        comparison=sir.NumericComparison(operator=">=", value=count),
    )


def _leading_number(query: str, span: Span) -> Decimal | None:
    """마커가 품은 수. 한글 수사는 읽지 않는다(못 읽으면 사유와 함께 닫는다)."""

    match = re.search(r"\d+", query[span[0] : span[1]])
    if match is None:
        return None
    return Decimal(match.group(0))


def _bucket_unit(query: str, span: Span) -> str | None:
    """하위 구간 단위. 표면어의 소유자는 :mod:`targeting_domain` 이다."""

    text = re.sub(r"\s+", "", query[span[0] : span[1]]).casefold()
    for cue, unit in targeting_domain.subinterval_unit_cues().items():
        if cue in text:
            return unit
    return None


# ── 판정 ─────────────────────────────────────────────────────────────────────────


def detect_temporal_claims(
    query: str,
    *,
    snapshot: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    runtime: temporal_ir.TemporalRuntime,
    timezone: str = "Asia/Seoul",
    consumed_spans: Sequence[Span] = (),
    today: date | None = None,
) -> tuple[TemporalClaimRequest, ...] | TemporalClaimRejection | None:
    """원문의 시간 조건들을 고르거나, 사유와 함께 닫는다.

    ``None`` 은 "이 문장은 시간 조건을 말하지 않았다"이고, :class:`TemporalClaimRejection`
    은 "말했지만 만들 수 없다"이다. 호출자의 결말이 다르므로 둘을 구분한다.
    """

    if not isinstance(query, str) or not query.strip():
        return None
    markers = _markers(query)
    if not markers:
        return None

    probe_day = today or SPAN_PROBE_DATE
    periods = _period_candidates(query, probe_day)
    hits = _value_hits(query, snapshot)
    requests: list[TemporalClaimRequest] = []

    for marker in markers:
        marker_span: Span = (marker.start, marker.end)
        plan = _OPERATOR_PLANS.get(marker.operator)
        if plan is None:
            return TemporalClaimRejection(
                OPERATOR_PLAN_MISSING,
                f"시간 연산자 '{marker.operator}' 를 IR 조합으로 옮기는 선언이 없습니다.",
                _evidence_dict(query, *marker_span),
            )

        clause_periods = [
            (span, window, kind)
            for span, window, kind in periods
            if not _covered(span, consumed_spans)
            and audience_frame.in_same_clause(query, span, marker_span)
        ]
        clause_hits = [
            hit
            for hit in hits
            if audience_frame.in_same_clause(query, (hit.start, hit.end), marker_span)
        ]
        clause_hits.sort(key=lambda hit: (hit.start, hit.end))

        outcome = _plan_request(
            query,
            marker=marker,
            plan=plan,
            clause_hits=clause_hits,
            clause_periods=clause_periods,
            snapshot=snapshot,
            catalog=catalog,
            runtime=runtime,
            timezone=timezone,
            consumed_spans=consumed_spans,
            today=probe_day,
        )
        if isinstance(outcome, TemporalClaimRejection):
            return outcome
        requests.append(outcome)

    return tuple(requests)


def _plan_request(
    query: str,
    *,
    marker: temporal_semantics.TemporalMarker,
    plan: _OperatorPlan,
    clause_hits: Sequence[_ValueHit],
    clause_periods: Sequence[tuple[Span, dict[str, Any], str]],
    snapshot: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    runtime: temporal_ir.TemporalRuntime,
    timezone: str,
    consumed_spans: Sequence[Span],
    today: date,
) -> TemporalClaimRequest | TemporalClaimRejection:
    marker_span: Span = (marker.start, marker.end)
    spans: list[Span] = [marker_span]
    values: list[str] = []
    domain: str | None = None

    if plan.values:
        if len(clause_hits) != plan.values:
            start = min([marker.start, *(hit.start for hit in clause_hits)])
            end = max([marker.end, *(hit.end for hit in clause_hits)])
            return TemporalClaimRejection(
                VALUE_COUNT_MISMATCH,
                f"이 시간 조건은 선언된 값 {plan.values}개를 요구하지만 "
                f"문장에서 확인된 값은 {len(clause_hits)}개입니다.",
                _evidence_dict(query, start, end),
            )
        domains = {hit.domain for hit in clause_hits}
        if len(domains) != 1:
            return TemporalClaimRejection(
                DOMAIN_MIXED,
                f"한 시간 조건의 값이 서로 다른 값 도메인({sorted(domains)})에 속합니다.",
                _evidence_dict(
                    query,
                    min(marker.start, clause_hits[0].start),
                    max(marker.end, clause_hits[-1].end),
                ),
            )
        domain = next(iter(domains))
        values = [hit.canonical for hit in clause_hits]
        spans.extend((hit.start, hit.end) for hit in clause_hits)
    else:
        # 값이 없는 연산('한 번도 바뀌지 않은'·'3회 이상 변경')은 축에서 도메인을 찾는다.
        domain = _axis_domain(query, marker_span, snapshot)
        if domain is None:
            return TemporalClaimRejection(
                VALUE_DOMAIN_UNRESOLVED,
                "이 시간 조건이 어떤 값 축을 말하는지 문장에서 확정할 수 없습니다.",
                _evidence_dict(query, *marker_span),
            )

    # 전이는 값 쌍의 어순·방향까지 봐야 한다 — 그 판정의 소유자는 transition_claims 다.
    if plan.predicate == "transition":
        owned = list(consumed_spans) + [span for span, _w, _k in clause_periods]
        request = transition_claims.detect_transition_request(
            query,
            snapshot=snapshot,
            catalog=catalog,
            consumed_spans=owned,
            today=today,
        )
        if isinstance(request, transition_claims.TransitionRejection):
            return TemporalClaimRejection(
                request.code, request.message, request.evidence
            )
        if request is None:
            return TemporalClaimRejection(
                VALUE_COUNT_MISMATCH,
                "값 전이를 말했지만 바뀌기 전 값과 바뀐 뒤 값을 확정할 수 없습니다.",
                _evidence_dict(query, *marker_span),
            )
        # 방향어('승급'/'강등')가 값 순서와 모순되는지까지 검증한다. 이 검사가 빠지면
        # 'VIP에서 골드로 승급'이 그대로 SQL 로 나가 문장의 모순이 조용히 사라진다(실측).
        try:
            transition_metrics.validate_transition_request(
                catalog,
                request.metric_id,
                from_value=request.from_value,
                to_value=request.to_value,
                direction=request.direction,
            )
        except transition_metrics.TransitionContractError as exc:
            return TemporalClaimRejection(
                exc.code,
                str(exc),
                _evidence_dict(query, request.evidence.start, request.evidence.end),
            )
        values = [request.from_value, request.to_value]
        domain = request.value_domain
        spans.append((request.evidence.start, request.evidence.end))

    if domain is None:  # pragma: no cover - 위에서 전부 막힌다
        return TemporalClaimRejection(
            VALUE_DOMAIN_UNRESOLVED, "값 축을 확정할 수 없습니다.",
            _evidence_dict(query, *marker_span),
        )

    metric_id = temporal_metric_for_domain(runtime, domain)
    if metric_id is None:
        count = _domain_metric_count(runtime, domain)
        code = METRIC_AMBIGUOUS if count > 1 else METRIC_NOT_DECLARED
        message = (
            f"값 도메인 '{domain}' 을 다루는 시간 지표 선언이 {count}개라 확정할 수 없습니다."
            if count > 1
            else f"값 도메인 '{domain}' 에는 시간 관측 지표가 선언되어 있지 않습니다."
        )
        return TemporalClaimRejection(code, message, _evidence_dict(query, *marker_span))

    # ── 구간·시점 ─────────────────────────────────────────────────────────────
    period = clause_periods[0] if clause_periods else None
    if period is not None and plan.interval == "forbidden":
        span = period[0]
        return TemporalClaimRejection(
            INTERVAL_FORBIDDEN,
            "이 시간 연산은 구간 한정을 받지 않습니다.",
            _evidence_dict(query, span[0], span[1]),
        )

    # 전이는 기간이 붙는 순간 '그 구간 안의 전이'가 된다 — 같은 술어, 다른 관측 범위.
    # 승격을 **구간을 읽기 전에** 해야 기간 표현을 시점으로 잘못 읽지 않는다.
    effective = plan
    if plan.predicate == "transition" and period is not None:
        effective = replace(plan, selector="window")

    window: sir.TemporalWindow | None = None
    anchor: sir.Anchor | None = None
    if period is not None:
        span, raw, kind = period
        spans.append(span)
        if effective.selector == "window":
            window = _window_from(raw, kind, timezone)
            if window is None:
                return TemporalClaimRejection(
                    INTERVAL_MISSING,
                    "구간 조건의 기간을 이 표현에서 확정할 수 없습니다.",
                    _evidence_dict(query, span[0], span[1]),
                )
        else:
            anchor = _calendar_anchor(raw, kind, today)
            if anchor is None:
                return TemporalClaimRejection(
                    ANCHOR_SHAPE_UNSUPPORTED,
                    "시점 조건의 기준 시점을 이 표현에서 확정할 수 없습니다"
                    "(기간은 시점이 아닙니다).",
                    _evidence_dict(query, span[0], span[1]),
                )
    if effective.selector == "window" and window is None:
        return TemporalClaimRejection(
            INTERVAL_MISSING,
            "이 시간 연산은 구간이 있어야 성립합니다. 기간을 명시해 주세요.",
            _evidence_dict(query, *marker_span),
        )

    if anchor is None:
        grain = _coarsest_grain(runtime, metric_id, _expected_operator(effective))
        anchor = (
            sir.RelativeAnchor(offset=0, unit=grain, boundary="end")
            if grain in {"week", "month", "year"}
            else sir.ReferenceAnchor()
        )

    bucket: str | None = None
    if effective.bucket:
        bucket = _bucket_unit(query, marker_span) or _window_unit(window)
        if bucket is None:
            return TemporalClaimRejection(
                BUCKET_UNIT_MISSING,
                "하위 구간 조건의 단위(달·주 …)를 확정할 수 없습니다.",
                _evidence_dict(query, *marker_span),
            )

    count: Decimal | None = None
    if effective.count:
        count = _leading_number(query, marker_span)
        if count is None:
            return TemporalClaimRejection(
                CHANGE_COUNT_VALUE_MISSING,
                "변경 횟수 조건의 횟수를 문장에서 읽을 수 없습니다"
                "(한글 수사는 아직 읽지 않습니다).",
                _evidence_dict(query, *marker_span),
            )

    bucket_count: int | None = None
    if effective.quantifier == "consecutive_buckets":
        raw_count = _leading_number(query, marker_span)
        if raw_count is None or int(raw_count) < 2:
            return TemporalClaimRejection(
                BUCKET_COUNT_MISSING,
                "연속 조건의 칸 수를 문장에서 읽을 수 없습니다"
                "(2 이상의 수가 있어야 '연속'이 성립합니다).",
                _evidence_dict(query, *marker_span),
            )
        bucket_count = int(raw_count)

    span_start = min(start for start, _end in spans)
    span_end = max(end for _start, end in spans)
    condition = sir.TemporalCondition(
        metric=metric_id,
        binding=None,
        selector=_selector(effective, anchor=anchor, window=window, bucket=bucket),
        quantifier=_quantifier(effective, bucket_count=bucket_count),
        predicate=_predicate(effective, values=values, count=count),
        evidence=_evidence(query, span_start, span_end),
    )
    return TemporalClaimRequest(
        operator=marker.operator,
        metric_id=metric_id,
        value_domain=domain,
        condition=condition,
        spans=tuple(sorted(set(spans))),
    )


def _expected_operator(plan: _OperatorPlan) -> str:
    """이 조합이 파생할 연산자 이름(앵커 굵기를 고르기 위한 사전 조회).

    이름을 여기서 짓지 않는다 — 빈 조합을 만들어 registry 에 물어본다.
    """

    probe = sir.TemporalCondition(
        metric="probe",
        binding=None,
        selector=_selector(
            plan,
            anchor=sir.ReferenceAnchor(),
            window=sir.RelativeWindow(
                amount=1, unit="month", mode="calendar", include_current=True
            ),
            bucket="month" if plan.bucket else None,
        ),
        quantifier=_quantifier(plan, bucket_count=2),
        predicate=_predicate(plan, values=["probe", "probe2"], count=Decimal(1)),
        evidence=None,
    )
    return temporal_ir.resolve_operator_name(probe)


def _axis_domain(
    query: str, marker_span: Span, snapshot: Mapping[str, Any]
) -> str | None:
    """값 없는 조건의 값 축. 축 표면어와 그 축의 canonical 값 선언으로 도메인을 찾는다."""

    clause_axis = [
        (term, span)
        for term in targeting_domain.attribute_axis_terms()
        for span in audience_frame.surface_spans(query, (term,))
        if audience_frame.in_same_clause(query, span, marker_span)
    ]
    if not clause_axis:
        return None
    attributes = (targeting_domain.attribute_catalog().get("attributes") or {})
    domains = {
        domain
        for term, _span in clause_axis
        for name, spec in attributes.items()
        if term in (spec.get("surface_terms") or ())
        for domain in (_domain_of_values(spec, snapshot),)
        if domain is not None
    }
    if len(domains) != 1:
        return None
    return next(iter(domains))


def _domain_of_values(
    spec: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> str | None:
    """속성 선언의 canonical 값들이 속한 값 도메인(선언에서 파생, 축 이름 무관)."""

    declared = set((spec.get("values") or {}).keys())
    if not declared:
        return None
    domains = {
        str(domain)
        for domain, payload in (snapshot.get("value_domains") or {}).items()
        if declared & set((payload.get("values") or {}).keys())
    }
    if len(domains) != 1:
        return None
    return next(iter(domains))


# ── 합성 ─────────────────────────────────────────────────────────────────────────


def synthesize_temporal_claim(
    query: str,
    *,
    snapshot: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    runtime: temporal_ir.TemporalRuntime,
    context: sir.TemporalRequestContext,
    consumed_spans: Sequence[Span] = (),
    additional: Sequence[event_ir.Condition] = (),
    today: date | None = None,
) -> TemporalClaimSynthesis | TemporalClaimRejection | None:
    """시간 조건을 낮추거나, 사유와 함께 닫는다.

    합성은 :func:`temporal_ir.lowering.compose_audience` 가 **전부 또는 아무것도**로 한다 —
    조건 하나가 실패했는데 나머지로 SQL 을 내면 요청하지 않은 더 넓은 집합이 나간다.
    """

    detected = detect_temporal_claims(
        query,
        snapshot=snapshot,
        catalog=catalog,
        runtime=runtime,
        timezone=context.timezone,
        consumed_spans=consumed_spans,
        today=today or context.now.date(),
    )
    if detected is None or isinstance(detected, TemporalClaimRejection):
        return detected

    composition = runtime.compose(
        [request.condition for request in detected],
        context,
        additional=list(additional),
    )
    if composition.status != "compiled":
        return _rejection_from(query, composition, detected)

    receipts = [
        dict(receipt.to_dict(), owner=OWNER) for receipt in composition.receipts
    ]
    spans = sorted({span for request in detected for span in request.spans})
    return TemporalClaimSynthesis(
        expression=composition.expression,
        receipts=tuple(receipts),
        requests=tuple(detected),
        spans=tuple(spans),
        warnings=tuple(composition.coverage_warnings or ()),
    )


def _rejection_from(
    query: str,
    composition: temporal_ir.AudienceComposition,
    detected: Sequence[TemporalClaimRequest],
) -> TemporalClaimRejection:
    """합성 실패 → 첫 실패의 사유를 그대로 전달한다(근사·재해석 금지)."""

    failure = next(iter(composition.failures), None)
    code = getattr(failure, "code", None) or f"temporal_{composition.status}"
    message = (
        getattr(failure, "message", None)
        or "요청한 시간 조건을 현재 관측 선언으로 낮출 수 없습니다."
    )
    condition = getattr(failure, "condition", None)
    evidence = getattr(condition, "evidence", None)
    if evidence is None and detected:
        evidence = detected[0].condition.evidence
    if evidence is None:
        return TemporalClaimRejection(
            code, message, {"text": query, "start": 0, "end": len(query)}
        )
    return TemporalClaimRejection(
        code,
        message,
        {"text": evidence.text, "start": evidence.start, "end": evidence.end},
    )


def _atom_keys(expression: event_ir.Condition) -> set[str]:
    return {
        json.dumps(atom.to_dict(), sort_keys=True, ensure_ascii=False)
        for atom, _negated in event_ir.iter_signed_atoms(expression)
    }


def compiled_obligation_spans(
    query: str,
    expression: event_ir.Condition | None,
    *,
    snapshot: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    runtime: temporal_ir.TemporalRuntime,
    context: sir.TemporalRequestContext | None = None,
    timezone: str = "Asia/Seoul",
    today: date | None = None,
) -> tuple[Span, ...]:
    """이 표현 안에서 시간 조건이 **실제로 컴파일된** 원문 구간.

    판정 규칙은 근거 구간이 아니라 **낮춘 원자의 일치**다. 원문에서 시간 조건을 다시 골라
    같은 선언으로 낮춘 뒤, 그 원자들이 대상 표현 안에 그대로 있을 때만 컴파일로 인정한다.

    근거 구간만 보면 방면이 위조된다(실측: `Comparison(subject.gender = female)` 에 전이
    구절의 구간을 붙이면 통과했다). 그때 사라지는 것은 이력 조건이고, 남는 것은 현재값
    조건이다 — '휴면이 된 회원'이 '지금 휴면인 회원'으로 바뀌는 그 사고다.
    """

    if expression is None:
        return ()
    request_context = context or sir.TemporalRequestContext(
        now=datetime.now(ZoneInfo(timezone))
    )
    # 재판정의 기준일은 낮춤의 기준 시각과 **같아야** 한다. 다르면 절대 달력 표현
    # ('2026년 3월 기준')이 다른 상대 시점으로 다시 읽혀 원자가 어긋나고, 컴파일된 조건이
    # 컴파일되지 않은 것으로 보인다(실측: 탐지 기준일이 SPAN_PROBE_DATE 로 새던 결함).
    detected = detect_temporal_claims(
        query,
        snapshot=snapshot,
        catalog=catalog,
        runtime=runtime,
        timezone=timezone,
        today=today or request_context.now.date(),
    )
    if not isinstance(detected, tuple) or not detected:
        return ()
    target_atoms = _atom_keys(expression)
    compiled: list[Span] = []
    for request in detected:
        evidence = request.condition.evidence
        if evidence is None:
            continue
        lowered = runtime.lower(request.condition, request_context)
        if lowered.status != "compiled":
            continue
        own_atoms = _atom_keys(lowered.expression)
        if own_atoms and own_atoms <= target_atoms:
            compiled.append((evidence.start, evidence.end))
    return tuple(compiled)


__all__ = [
    "ANCHOR_SHAPE_UNSUPPORTED",
    "BUCKET_COUNT_MISSING",
    "BUCKET_UNIT_MISSING",
    "CHANGE_COUNT_VALUE_MISSING",
    "DOMAIN_MIXED",
    "INTERVAL_FORBIDDEN",
    "INTERVAL_MISSING",
    "METRIC_AMBIGUOUS",
    "METRIC_NOT_DECLARED",
    "OPERATOR_PLAN_MISSING",
    "OWNER",
    "TemporalClaimRejection",
    "TemporalClaimRequest",
    "TemporalClaimSynthesis",
    "VALUE_COUNT_MISMATCH",
    "VALUE_DOMAIN_UNRESOLVED",
    "compiled_obligation_spans",
    "detect_temporal_claims",
    "synthesize_temporal_claim",
    "temporal_metric_for_domain",
]
