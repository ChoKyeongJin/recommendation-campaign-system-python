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

요청 하나의 다섯 축은 어디에 있는가
-----------------------------------
'도메인 · 연산 · 비교 · 임계값 · 구간'은 이 계층이 따로 담지 않는다. 그 다섯은 이미
:class:`sir.TemporalCondition` 의 필드이고, 여기서 새 dataclass 로 한 번 더 담으면 같은 뜻을
두 모델이 말하게 된다(위 §4 와 같은 이유). 대응은 이렇게 읽는다::

    domain      condition.metric / TemporalClaimRequest.value_domain
    operation   marker.operator → resolve_operator_name(condition)
    comparator  condition.predicate.comparison.operator
    threshold   condition.predicate.comparison.value
    window      condition.selector.window (+ TemporalClaimRequest.window_source)

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

import aggregate_parser_config
import audience_frame
import calendar_window
import canonical_audience_claims
import condition_normalizers
import event_ir
import ordered_catalog_claims
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
INTERVAL_NOT_EXPRESSIBLE = "temporal_interval_required_not_expressible"
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


# 원문이 기간을 말하지 않았을 때 그 연산자가 하는 일. **연산자 선언이 소유한다** —
# 코드에서 연산자 이름을 나열해 특별 처리하면 같은 판단이 두 곳에 생기고, 새 연산자는
# 어느 쪽 목록에 들어가야 하는지 알 수 없게 된다.
ALL_AVAILABLE_DATA = "all_available_data"  # 전체 가용 데이터 범위로 읽는다(시간 필터 없음)
CLARIFICATION = "clarification"  # 사용자에게 기간을 묻는다(고칠 수 있는 결핍)
UNSUPPORTED_WITHOUT_INTERVAL = "unsupported"  # 구간 없이는 이 의미를 표현할 수 없다

MISSING_WINDOW_POLICIES: frozenset[str] = frozenset(
    {ALL_AVAILABLE_DATA, CLARIFICATION, UNSUPPORTED_WITHOUT_INTERVAL}
)


@dataclass(frozen=True)
class _OperatorPlan:
    """범용 시간 연산자 하나를 IR 조합으로 옮기는 선언.

    ``values`` 는 술어가 요구하는 **원문 값의 개수**다(전이는 2, 상태는 1, 변경 횟수는 0).
    ``interval`` 은 구간을 받을 수 있는지다('optional'/'required'/'forbidden'). ``forbidden``
    인 연산에 구간이 붙으면 조용히 무시하지 않고 사유와 함께 닫는다.

    ``missing_window`` 는 원문이 구간을 **말하지 않았을 때** 무엇을 할지의 선언이다
    (:data:`MISSING_WINDOW_POLICIES`). 구간이 의미의 핵심인 연산(하위 구간 전칭·연속 칸)은
    칸 수가 정해지지 않으면 판정 자체가 성립하지 않으므로 되묻고, 나머지는 전체 가용 범위로
    읽는다 — '등급이 2회 이상 변경된 회원'에 기간이 없다는 것은 결핍이 아니라 전체를 뜻한다.

    ``direction`` 은 방향 전이('승급'·'강등')에서만 채워진다. 값 쌍 대신 서열 방향이 술어를
    확정하므로, 그 방향은 원문에서 읽은 뒤 **계획에 실려** 술어 조립까지 간다.
    """

    selector: str
    quantifier: str
    predicate: str
    values: int
    interval: str = "forbidden"
    missing_window: str = CLARIFICATION
    bucket: bool = False
    count: bool = False
    null_policy: str = "exclude"
    direction: str | None = None
    # '직전'의 세 뜻 중 어느 것인가(:class:`sir.PreviousKind`). 선언의 기본값은 달력 칸이고,
    # 머리가 속성 축이면 :func:`_observation_plan` 이 관측으로 바꾼다 — 낱말이 아니라 머리가
    # 뜻을 정한다는 규칙이 코드로 있는 자리다.
    previous_kind: str = "bucket"

    def __post_init__(self) -> None:
        if self.missing_window not in MISSING_WINDOW_POLICIES:
            raise ValueError(
                f"알 수 없는 missing_window 정책: {self.missing_window!r}"
                f" (허용: {sorted(MISSING_WINDOW_POLICIES)})"
            )


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
        values=1, interval="optional", missing_window=ALL_AVAILABLE_DATA,
    ),
    temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL: _OperatorPlan(
        selector="window", quantifier="exists", predicate="state",
        values=1, interval="optional", missing_window=ALL_AVAILABLE_DATA,
    ),
    temporal_semantics.NEVER_IN_INTERVAL: _OperatorPlan(
        selector="window", quantifier="none", predicate="state",
        values=1, interval="optional", missing_window=ALL_AVAILABLE_DATA,
    ),
    temporal_semantics.THROUGHOUT_INTERVAL: _OperatorPlan(
        # '내내'는 관측 전칭이다. null 정책이 exclude 면 값이 빈 관측이 조용히 통과하므로
        # temporal.all 은 treat_as_mismatch 만 받는다(계약이 그렇게 좁혀져 있다).
        selector="window", quantifier="all_observations", predicate="state",
        values=1, interval="optional", missing_window=ALL_AVAILABLE_DATA,
        null_policy="treat_as_mismatch",
    ),
    temporal_semantics.EVERY_SUBINTERVAL: _OperatorPlan(
        # 칸 전칭은 '몇 칸이어야 하는가'가 판정의 재료다. 경계 없는 구간에는 기대 칸 수가
        # 없으므로 전체 범위로 읽을 수 없다 — 되묻는 것이 유일하게 정직한 결말이다.
        selector="window", quantifier="every_bucket", predicate="state",
        values=1, interval="required", missing_window=CLARIFICATION, bucket=True,
    ),
    temporal_semantics.UNCHANGED_THROUGHOUT: _OperatorPlan(
        # '한 번도 바뀌지 않았다'는 관측 전체에 대한 진술이다. exists 로 두면 "안 바뀐 관측이
        # 하나라도 있다"가 되어 정반대에 가까운 집합이 나온다.
        selector="window", quantifier="all_observations", predicate="unchanged",
        values=0, interval="optional", missing_window=ALL_AVAILABLE_DATA,
    ),
    temporal_semantics.CHANGE_BETWEEN: _OperatorPlan(
        # 기간이 없으면 '가장 최근 관측에서의 전이'(as_of), 있으면 '그 구간 안의 전이'(window).
        selector="as_of", quantifier="exists", predicate="transition",
        values=2, interval="optional",
    ),
    temporal_semantics.CHANGE_COUNT: _OperatorPlan(
        # '등급이 2회 이상 변경된 회원'에 기간이 없으면 전체 가용 범위에서 센다. 적재가 한 달인지
        # 백 달인지는 그 SQL 의 **답**을 바꾸지만 SQL 을 만들 수 있는지는 바꾸지 않는다.
        selector="window", quantifier="exists", predicate="change_count",
        values=0, interval="optional", missing_window=ALL_AVAILABLE_DATA, count=True,
    ),
    temporal_semantics.CONSECUTIVE_SUBINTERVALS: _OperatorPlan(
        # '연속 N칸'도 칸 수가 재료다(EVERY_SUBINTERVAL 과 같은 이유).
        selector="window", quantifier="consecutive_buckets", predicate="state",
        values=1, interval="required", missing_window=CLARIFICATION, bucket=True,
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
    """원문이 고른 시간 조건 하나 — 아직 낮추지 않은 상태.

    ``window_source`` 는 이 조건의 구간을 **누가 골랐는가**다. 정책이 채운 구간을 사용자가 말한
    구간과 같게 보고하면 두 가지가 깨진다: 응답을 읽는 쪽이 "원문에 없는 조건"을 설명할 수 없고,
    의미 검증기가 자기 SQL 을 spurious 로 막는다.

    ``current_value`` 는 이 청구가 **지금의 값**만 말한다는 표시다('현재 등급이 VIP'). 그런
    조건의 소유자는 이력 관측이 아니라 현재값 자산이므로, 직전 값과 짝을 이루지 못하면 이
    계층은 손을 뗀다(:func:`_merge_state_transitions`). 표시를 들고 다니는 이유는 그 판단이
    청구를 만든 자리에서만 가능하기 때문이다 — 낮춘 뒤에는 두 AS_OF 가 구분되지 않는다.
    """

    operator: str
    metric_id: str
    value_domain: str | None
    condition: sir.TemporalCondition
    spans: tuple[Span, ...]
    current_value: bool = False

    @property
    def window_source(self) -> sir.WindowSource:
        window = getattr(self.condition.selector, "window", None)
        if window is None:
            return sir.WindowSource.USER
        return sir.window_source(window)


@dataclass(frozen=True)
class TemporalClaimSynthesis:
    """낮춘 조건 + 그것을 증명한 선언·원문 근거의 영수증."""

    expression: event_ir.Condition
    receipts: tuple[dict[str, Any], ...]
    requests: tuple[TemporalClaimRequest, ...]
    spans: tuple[Span, ...]
    warnings: tuple[str, ...] = ()


# 반려의 **귀결 종류**. 사용자가 문장을 고쳐 해결할 수 있는 결핍/모호는 되묻기이고, 선언과
# 표현의 한계는 미지원이다. 코드에서 사유를 추론하지 않고 반려를 만든 자리가 선언한다.
CLARIFICATION = "clarification"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class TemporalClaimRejection:
    """시간 조건을 말했지만 만들 수 없다 — 사유와 근거, 그리고 **귀결 종류**를 남긴다.

    ``disposition`` 을 코드에서 되짚지 않고 반려 지점이 선언하는 이유: 같은 문장이 어느
    계층에서 막혔는지에 따라 사용자에게 줄 다음 행동이 다르고(기간을 말해 달라 / 이 데이터
    표현으로는 답할 수 없다), 그 판단은 반려를 만든 쪽만 안다.
    """

    code: str
    message: str
    evidence: dict[str, Any]
    disposition: str = UNSUPPORTED


@dataclass(frozen=True)
class _ValueHit:
    domain: str
    canonical: str
    start: int
    end: int


# ── 원문 조각 ────────────────────────────────────────────────────────────────────


def _undeclared_metric_rejection(
    query: str,
    domain: str,
    runtime: temporal_ir.TemporalRuntime,
    marker_span: Span,
) -> TemporalClaimRejection:
    """값 축에 시간 관측 지표가 없다 — **이력 소스 부재**의 사유 하나.

    두 소비자가 이 함수를 부른다. 하나는 값 개수를 세기 **전**이고(감사 #73: 두 값을 완벽히
    적어도 열리지 않는 축에 "값이 하나 모자랍니다"라고 답하지 않기 위해), 하나는 축이 확정된
    뒤다. 사유를 한 자리에서 만들지 않으면 같은 부재가 두 이름으로 설명된다.

    귀결은 미지원이다(기본 disposition). 사용자가 문장을 고쳐서 열 수 있는 결핍이 아니다.
    """
    count = _domain_metric_count(runtime, domain)
    code = METRIC_AMBIGUOUS if count > 1 else METRIC_NOT_DECLARED
    message = (
        f"값 도메인 '{domain}' 을 다루는 시간 지표 선언이 {count}개라 확정할 수 없습니다."
        if count > 1
        else f"값 도메인 '{domain}' 에는 시간 관측 지표가 선언되어 있지 않습니다."
    )
    return TemporalClaimRejection(code, message, _evidence_dict(query, *marker_span))


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
    # 낱말 경계는 원문에서만 보인다 — 압축 좌표 대응표를 함께 넘겨야 단어형 기간 가드가 돈다
    # (:func:`calendar_window.is_standalone_word_duration`). 이 두 인자가 없던 동안 가드는
    # **선언만 되고 한 번도 돌지 않았고**, '적어도 한 달은 골드'의 '한 달'이 1개월 창이 됐다
    # (2026-08-08 실측). 판정 규칙은 :func:`calendar_window.parse_duration_window` 와 같다.
    offsets = tuple(index for index, char in enumerate(query) if char != " ")
    for candidate in calendar_window.duration_window_candidates(
        compact,
        source=query,
        source_offsets=offsets if len(offsets) == len(compact) else None,
    ):
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


# ── 서열 비교('골드 **이상**') ──────────────────────────────────────────────────
# 비교어 → 부등호 표의 단일 소유자는 :func:`condition_normalizers.comparison_literal_operators`
# 다. 같은 표에서 같은 방식으로 정규식을 만들기 때문에 여기서 잡는 구간은 리터럴 정산이 보는
# 구간(`query_structurer.semantic_ir.extract_literal_bindings`)과 **글자 단위로 같다** — 그
# 동일성이 깨지면 소유 신고가 미소비 리터럴을 덮지 못하므로 계약 테스트로 고정한다.
_COMPARISON_OPERATOR_RE = re.compile(
    "|".join(
        re.escape(surface)
        for surface in condition_normalizers.comparison_literal_operators()
    )
)


def _ordered_comparison(
    query: str, hit: _ValueHit, domain: str, snapshot: Mapping[str, Any]
) -> tuple[str, Span] | None:
    """값 바로 뒤에 붙은 **서열 비교어**와 그 구간. 없으면 None.

    조건은 셋이고 하나라도 어긋나면 등호를 유지한다(추측하지 않는다).

    1. 값 도메인이 **순서를 선언**했다(``ordered: true``). 순서 없는 축('정상/휴면')에
       부등호를 만들면 사전식 비교라는 다른 뜻이 조용히 들어온다.
    2. 비교어가 값에 **직접 붙어** 있다(사이에 공백만). 이 인접 조건이 이 함수의 존재
       이유다 — '골드였고 10만원 이상 구매한' 의 '이상'은 금액의 것이지 등급의 것이 아니다.
    3. 정규화된 연산자가 서열 비교다(:data:`ordered_catalog_claims.ORDERED_OPERATORS`).

    돌려주는 구간은 **소유 신고**에 쓴다. 연산자만 바꾸고 구간을 신고하지 않으면 '이상'이
    미소비 리터럴로 남아 정산 게이트가 문장 전체를 막는다(fail-close 는 옳지만 답은 아니다).
    """

    declared = (snapshot.get("value_domains") or {}).get(domain)
    if not isinstance(declared, Mapping) or declared.get("ordered") is not True:
        return None
    match = _COMPARISON_OPERATOR_RE.search(query, hit.end)
    if match is None or query[hit.end : match.start()].strip():
        return None
    operator = condition_normalizers.comparison_literal_operators().get(match.group(0))
    if operator not in ordered_catalog_claims.ORDERED_OPERATORS:
        return None
    return operator, (match.start(), match.end())


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
        return sir.PreviousSelector(anchor=anchor, previous_kind=plan.previous_kind)
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
    comparison_operator: str = "=",
) -> sir.Predicate:
    if plan.predicate == "state":
        # 연산자는 인자로 받는다. 여기서 '=' 를 박아 두던 동안 '골드 **이상**'이 경고 없이
        # '골드'로 좁아졌다(VIP 누락, 2026-08-08 실측) — 부등호를 물리값 IN 목록으로 펴는
        # 일반 로직은 event_compiler 에 이미 있었고, 그 자리까지 뜻이 오지 못했을 뿐이다.
        return sir.StatePredicate(
            comparison=sir.Comparison(operator=comparison_operator, value=values[0]),
            null_policy=plan.null_policy,
        )
    if plan.predicate == "transition":
        return sir.TransitionPredicate(
            from_value=values[0],
            to_value=values[1],
            transition_mode="direct_observed_transition",
        )
    if plan.predicate == "directional_transition":
        if plan.direction is None:  # pragma: no cover - 계획 생성이 먼저 막는다
            raise ValueError("directional transition predicate requires a direction")
        return sir.DirectionalTransitionPredicate(
            # 방향 이름은 도메인 표(:mod:`targeting_domain`)에서 온 문자열이다. 여기서 닫힌
            # 어휘로 세워 두면 표가 어긋난 날 SQL 이 아니라 이 자리에서 이름을 대며 터진다.
            direction=sir.TransitionDirection(plan.direction),
            transition_mode=sir.TransitionMode.DIRECT_OBSERVED_TRANSITION,
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
    """마커가 품은 수. 아라비아 숫자와 한글 수관형사를 **같은 문법**으로 읽는다.

    수사 어휘를 이 모듈에 두지 않는 이유는 소유권이다 — '두 번'을 2로 읽는 표는 이미
    :mod:`aggregate_parser_config` 가 소유하고(결속 규칙과 어림수 가드까지 포함), 여기서 두 번째
    표를 만들면 '두 번 이상 구매'와 '두 번 이상 변경'이 서로 다른 문법으로 읽힌다.
    """

    return aggregate_parser_config.read_count_value(query[span[0] : span[1]])


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
        plan = _observation_plan(plan, marker)

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
        # 마커가 자기 머리를 품고 있으면(‘직전에는 골드’) 그 값이 이 조건의 값이다. 절 전체를
        # 보면 접속사 없이 이어진 문장('직전에는 골드였는데 지금은 VIP')에서 두 조건의 값이
        # 서로의 절에 섞여 둘 다 '값이 2개'로 닫힌다 — 근거 구간이 있는 쪽이 소유자다.
        inside = [
            hit for hit in clause_hits if marker.start <= hit.start and hit.end <= marker.end
        ]
        clause_hits = inside or clause_hits
        clause_hits.sort(key=lambda hit: (hit.start, hit.end))

        if _states_current_value(plan, marker) and not clause_hits:
            # '최근에 등급이 승급한' 의 '최근에 등급'. 값이 없으므로 상태 조건이 아니다 —
            # 이 낱말은 같은 절의 다른 조건이 고르는 관측을 다시 말한 것뿐이다. 조건을
            # 만들지 않되 선택자로는 소비된 상태로 남는다(기간 결핍이 생기지 않는다).
            continue

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
        requests.append(
            replace(outcome, current_value=_states_current_value(plan, marker))
        )

    # 같은 축의 '지금 값'과 '직전 값'은 두 조건이 아니라 하나의 전이다. 문형으로 찾지 않고
    # **이미 만들어진 청구**를 정규화하는 것이 이 순서의 요점이다(I5).
    settled = _merge_state_transitions(query, tuple(requests))
    # 짝을 이루지 못한 '지금 값' 청구는 이 계층이 소유하지 않는다 — 그 뜻은 현재값 자산이
    # 그대로 답한다('현재 등급이 VIP' = '등급이 VIP'). 여기서 관측 조건을 내면 같은 조건에
    # 주인이 둘 생기고, 스냅샷 적재 월이 앵커와 다른 배포에서는 답까지 달라진다.
    materialized = tuple(item for item in settled if not item.current_value)
    return materialized or None


def _observation_plan(
    plan: _OperatorPlan, marker: temporal_semantics.TemporalMarker
) -> _OperatorPlan:
    """선택자 낱말의 뜻을 **머리**로 확정한다(I1).

    '직전'은 낱말 하나에 세 뜻이 있다(:class:`sir.PreviousKind`) — 직전 달력 칸, 직전 관측,
    현재값과 다른 마지막 값. 어느 것인지는 그 낱말이 무엇을 수식하는지가 정한다. 속성 축을
    수식하면 관측이고('직전 상태'), 달력 단위를 수식하면 칸이다('직전 달').

    판정은 :func:`targeting_domain.temporal_head_kind` 하나가 소유한다 — 여기서 낱말을 다시
    읽으면 마커를 만든 표와 뜻을 정하는 표가 갈라진다.
    """

    if plan.selector != "previous":
        return plan
    if targeting_domain.temporal_head_kind(marker.text) != targeting_domain.HEAD_ATTRIBUTE:
        return plan
    return replace(plan, previous_kind="observation")


def _states_current_value(
    plan: _OperatorPlan, marker: temporal_semantics.TemporalMarker
) -> bool:
    """이 청구가 속성 축의 **지금 값**만 말하는가(판정의 소유자는 도메인 계층 하나다).

    ``plan`` 도 함께 보는 이유는 술어 모양 때문이다 — 같은 마커라도 값 없는 조합(전이·변경
    횟수)으로 승격됐다면 그것은 '지금 값'이 아니다.
    """

    return (
        plan.selector == "as_of"
        and plan.predicate == "state"
        and targeting_domain.selects_current_value(marker.operator, marker.text)
    )


def _merge_state_transitions(
    query: str, requests: tuple[TemporalClaimRequest, ...]
) -> tuple[TemporalClaimRequest, ...]:
    """같은 축의 (지금 값 · 직전 값) 청구 쌍을 전이 하나로 정규화한다(I5).

    전이를 문형으로 찾지 않는 것이 이 함수의 존재 이유다. '골드에서 VIP로 승급'과
    '현재 등급이 VIP이고 직전 등급이 골드'는 서로 다른 문형이지만 같은 뜻이고, 문형마다
    감지기를 만들면 문형이 늘 때마다 같은 뜻이 새로 미지원이 된다.

    결합 조건은 절 위치도 텍스트 거리도 아니다 — **같은 지표(=같은 엔터티의 같은 축)**,
    같은 시점(anchor), 같은 모양의 상태 술어. 축이 다르면 합치지 않는다('현재 등급이 VIP이고
    직전 구매 상품은 골드 패키지'는 전이가 아니다).

    두 조건을 한 관측 행에서 함께 읽는 것이 전이의 존재 이유다(:mod:`transition_metrics` 의
    같은 계약). 따로 두면 서로 다른 행에서 만족돼도 통과한다.
    """

    if len(requests) < 2:
        return requests
    merged: list[TemporalClaimRequest] = []
    consumed: set[int] = set()
    for index, current in enumerate(requests):
        if index in consumed:
            continue
        partner = next(
            (
                other
                for other, candidate in enumerate(requests)
                if other not in consumed
                and other != index
                and _pairs_into_transition(current, candidate)
            ),
            None,
        )
        if partner is None:
            merged.append(current)
            continue
        consumed.update({index, partner})
        latest, earlier = (
            (current, requests[partner])
            if current.current_value
            else (requests[partner], current)
        )
        merged.append(_merged_transition(query, latest, earlier))
    return tuple(merged)


def _pairs_into_transition(
    one: TemporalClaimRequest, other: TemporalClaimRequest
) -> bool:
    """이 둘이 같은 축의 (지금 값 · 직전 값) 쌍인가."""

    if one.current_value == other.current_value:
        return False
    latest, earlier = (one, other) if one.current_value else (other, one)
    if earlier.operator != temporal_semantics.IMMEDIATELY_PRECEDING:
        return False
    # 같은 지표 = 같은 엔터티의 같은 의미 축. 이름이 아니라 선언에서 온 id 로 묻는다.
    if latest.metric_id != earlier.metric_id:
        return False
    selector, earlier_selector = latest.condition.selector, earlier.condition.selector
    if not isinstance(selector, sir.AsOfSelector) or not isinstance(
        earlier_selector, sir.PreviousSelector
    ):
        return False
    # 같은 관측을 고른 두 조건만 한 행으로 접을 수 있다. 시점이 다르면 서로 다른 관측이고,
    # 그때 한 행으로 접으면 사용자가 말하지 않은 집합이 된다.
    if selector.anchor != earlier_selector.anchor:
        return False
    if earlier_selector.previous_kind is not sir.PreviousKind.OBSERVATION:
        return False
    to_value = _state_value(latest.condition.predicate)
    from_value = _state_value(earlier.condition.predicate)
    return to_value is not None and from_value is not None and to_value != from_value


def _merged_transition(
    query: str, latest: TemporalClaimRequest, earlier: TemporalClaimRequest
) -> TemporalClaimRequest:
    """(지금 값 · 직전 값) 쌍 → 전이 청구 하나(한 관측 행의 두 비교)."""

    selector = latest.condition.selector
    to_value = _state_value(latest.condition.predicate)
    from_value = _state_value(earlier.condition.predicate)
    spans = tuple(sorted(set(latest.spans) | set(earlier.spans)))
    start = min(span[0] for span in spans)
    end = max(span[1] for span in spans)
    condition = sir.TemporalCondition(
        metric=latest.metric_id,
        binding=None,
        selector=selector,
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.TransitionPredicate(
            from_value=str(from_value),
            to_value=str(to_value),
            transition_mode="direct_observed_transition",
        ),
        evidence=_evidence(query, start, end),
    )
    return TemporalClaimRequest(
        operator=temporal_semantics.CHANGE_BETWEEN,
        metric_id=latest.metric_id,
        value_domain=latest.value_domain,
        condition=condition,
        spans=spans,
    )


def _state_value(predicate: sir.Predicate) -> str | None:
    """동등 비교 상태 술어의 값(그 모양이 아니면 ``None``)."""

    if not isinstance(predicate, sir.StatePredicate):
        return None
    if predicate.comparison.operator != "=":
        return None
    return str(predicate.comparison.value)


def _directional_plan(
    query: str,
    marker_span: Span,
    plan: _OperatorPlan,
    clause_hits: Sequence[_ValueHit],
) -> _OperatorPlan | None:
    """마커가 **방향어 하나뿐**인 전이 요청이면 방향 전이 계획으로 바꾼다.

    '골드에서 VIP로 승급'은 여기 오지 않는다 — 그 문장은 절에 값이 둘 있고, 값 쌍이 있으면
    방향은 검증의 대상이지 술어의 근거가 아니다(:mod:`transition_metrics` 가 어순 모순을 잡는다).
    값이 하나뿐인 'VIP로 승급'도 바꾸지 않는다: 도착값만 있고 출발 쪽이 비어 있는 요청을
    방향으로 덮으면 사용자가 말한 값이 조용히 사라진다. 그러므로 조건은 **값 0개**다.

    방향어 판정에 원문을 다시 훑지 않고 마커 구간만 보는 이유는 소유권이다 — 이 마커를 만든
    표가 방향어 표와 같으므로(:func:`targeting_domain.transition_direction`), 마커로 잡혔는데
    방향은 못 읽는 조합이 생기지 않는다.
    """

    if plan.predicate != "transition" or clause_hits:
        return None
    direction = targeting_domain.transition_direction(query[marker_span[0] : marker_span[1]])
    if direction is None:
        return None
    return replace(plan, predicate="directional_transition", values=0, direction=direction)


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
    # 상태 술어의 비교 연산자. 기본은 등호이고, 값 바로 뒤에 서열 비교어가 붙어 있을 때만
    # 바뀐다(:func:`_ordered_comparison`).
    comparison_operator = "="

    plan = _directional_plan(query, marker_span, plan, clause_hits) or plan

    # **관측이 없는 축은 값을 더 말해도 열리지 않는다.** 그래서 값 개수보다 이 질문이 먼저다.
    #
    # 실측(2026-08-08 감사 #73) — `여성이면서 정상에서 휴면으로 바뀐 회원` 이 "선언된 값 2개를
    # 요구하지만 확인된 값은 1개"로 반려됐다. 귀결(미지원)은 옳았지만 **이름이 틀렸다**: 회원
    # 상태에는 시점·이력 지표 선언이 아예 없어서, 사용자가 두 값을 완벽히 적어도 열리지 않는다.
    # 그 문구를 읽은 운영자는 고칠 수 없는 것을 고치라고 안내하게 된다.
    axis_domains = {hit.domain for hit in clause_hits}
    if not axis_domains:
        axis = _axis_domain(query, marker_span, snapshot)
        axis_domains = {axis} if axis is not None else set()
    for axis in sorted(item for item in axis_domains if item):
        if temporal_metric_for_domain(runtime, axis) is not None:
            continue
        # 사유를 여기서 새로 만들지 않는다 — 아래 `metric_id` 판정이 쓰는 것과 **같은** 코드와
        # 문장이다. 달라진 것은 순서뿐이고, 순서가 곧 진단의 정확도다.
        return _undeclared_metric_rejection(query, axis, runtime, marker_span)

    if plan.values:
        if len(clause_hits) != plan.values:
            start = min([marker.start, *(hit.start for hit in clause_hits)])
            end = max([marker.end, *(hit.end for hit in clause_hits)])
            return TemporalClaimRejection(
                VALUE_COUNT_MISMATCH,
                f"이 시간 조건은 선언된 값 {plan.values}개를 요구하지만 "
                f"문장에서 확인된 값은 {len(clause_hits)}개입니다.",
                _evidence_dict(query, start, end),
                disposition=CLARIFICATION,
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
                disposition=CLARIFICATION,
            )
        domain = next(iter(domains))
        values = [hit.canonical for hit in clause_hits]
        spans.extend((hit.start, hit.end) for hit in clause_hits)
        if plan.predicate == "state" and len(clause_hits) == 1:
            ordered = _ordered_comparison(query, clause_hits[0], domain, snapshot)
            if ordered is not None:
                comparison_operator, comparison_span = ordered
                spans.append(comparison_span)
    else:
        # 값이 없는 연산('한 번도 바뀌지 않은'·'3회 이상 변경')은 축에서 도메인을 찾는다.
        domain = _axis_domain(query, marker_span, snapshot)
        if domain is None:
            return TemporalClaimRejection(
                VALUE_DOMAIN_UNRESOLVED,
                "이 시간 조건이 어떤 값 축을 말하는지 문장에서 확정할 수 없습니다.",
                _evidence_dict(query, *marker_span),
                disposition=CLARIFICATION,
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
                disposition=CLARIFICATION,
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
            disposition=CLARIFICATION,
        )

    metric_id = temporal_metric_for_domain(runtime, domain)
    if metric_id is None:
        return _undeclared_metric_rejection(query, domain, runtime, marker_span)

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
    if plan.predicate in {"transition", "directional_transition"} and period is not None:
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
                    disposition=CLARIFICATION,
                )
        else:
            anchor = _calendar_anchor(raw, kind, today)
            if anchor is None:
                return TemporalClaimRejection(
                    ANCHOR_SHAPE_UNSUPPORTED,
                    "시점 조건의 기준 시점을 이 표현에서 확정할 수 없습니다"
                    "(기간은 시점이 아닙니다).",
                    _evidence_dict(query, span[0], span[1]),
                    disposition=CLARIFICATION,
                )
    if effective.selector == "window" and window is None:
        # 원문이 구간을 말하지 않았다. 무엇을 할지는 **연산자 선언**이 정한다(이름 분기 금지).
        if effective.missing_window == ALL_AVAILABLE_DATA:
            window = sir.AllAvailableDataWindow(source=sir.WindowSource.POLICY_DEFAULT)
        elif effective.missing_window == UNSUPPORTED_WITHOUT_INTERVAL:
            return TemporalClaimRejection(
                INTERVAL_NOT_EXPRESSIBLE,
                "이 시간 연산은 구간 없이는 표현할 수 없습니다.",
                _evidence_dict(query, *marker_span),
            )
        else:
            return TemporalClaimRejection(
                INTERVAL_MISSING,
                "이 시간 연산은 구간이 있어야 성립합니다. 기간을 명시해 주세요.",
                _evidence_dict(query, *marker_span),
                disposition=CLARIFICATION,
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
                disposition=CLARIFICATION,
            )

    count: Decimal | None = None
    if effective.count:
        count = _leading_number(query, marker_span)
        if count is None:
            return TemporalClaimRejection(
                CHANGE_COUNT_VALUE_MISSING,
                "변경 횟수 조건의 횟수를 문장에서 읽을 수 없습니다"
                "(아라비아 숫자 또는 수관형사 하나가 필요합니다).",
                _evidence_dict(query, *marker_span),
                disposition=CLARIFICATION,
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
                disposition=CLARIFICATION,
            )
        bucket_count = int(raw_count)

    span_start = min(start for start, _end in spans)
    span_end = max(end for _start, end in spans)
    condition = sir.TemporalCondition(
        metric=metric_id,
        binding=None,
        selector=_selector(effective, anchor=anchor, window=window, bucket=bucket),
        quantifier=_quantifier(effective, bucket_count=bucket_count),
        predicate=_predicate(
            effective,
            values=values,
            count=count,
            comparison_operator=comparison_operator,
        ),
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
        for term in targeting_domain.attribute_axis_identifying_terms()
        for span in audience_frame.surface_spans(query, (term,))
        if audience_frame.in_same_clause(query, span, marker_span)
    ]
    # 축 표면어는 서로를 포함한다('가치등급' ⊃ '등급'). 포함된 쪽을 그대로 두면 한 절이 두
    # 도메인을 가리키게 되어 옳은 요청이 '확정 불가'로 닫히고, 한쪽만 선언돼 있으면 **조용히
    # 다른 축**으로 해석된다. 그러므로 더 긴 표면어가 덮은 짧은 표면어는 그 자리의 주인이 아니다.
    clause_axis = [
        (term, span)
        for term, span in clause_axis
        if not any(
            other_span != span and other_span[0] <= span[0] and span[1] <= other_span[1]
            for _other_term, other_span in clause_axis
        )
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


# 원자에서 **뜻이 아닌 것**. 근거 표기는 이 조건이 원문 어디에서 왔는지를 적은 출처이지
# 조건의 의미가 아니다 — 같은 조건을 두 사람이 서로 다른 구간을 인용해 적을 수 있다.
_PROVENANCE_FIELDS = frozenset({"evidence"})


def _semantic_shape(value: Any) -> Any:
    """출처 표기를 벗긴 의미 구조. 중첩 dict/list/tuple 어디에 있어도 재귀로 벗긴다."""

    if isinstance(value, Mapping):
        return {
            key: _semantic_shape(item)
            for key, item in value.items()
            if key not in _PROVENANCE_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_shape(item) for item in value]
    return value


def _atom_keys(expression: event_ir.Condition) -> set[str]:
    """원자별 **의미** 지문. provenance 는 지문의 구성요소가 아니다.

    예전에는 ``to_dict()`` 를 통째로 직렬화해서, 필드·연산자·창·값이 모두 같아도 인용 구간이
    다르면 다른 조건으로 세어졌다. 그 비교를 통과하려면 표현을 만든 쪽이 애플리케이션 낮춤이
    고른 구간을 글자 단위로 맞혀야 하는데, 그것은 뜻과 무관한 요구다(실측 2026-08-08:
    구조가 완전히 같은 표현이 근거 표기 하나 때문에 반려되어 재시도 예산을 태웠다).
    """

    return {
        json.dumps(
            _semantic_shape(atom.to_dict()), sort_keys=True, ensure_ascii=False
        )
        for atom, _negated in event_ir.iter_signed_atoms(expression)
    }


def request_context_for(
    current_date: str | date | None, *, timezone: str = sir.DEFAULT_TIMEZONE
) -> sir.TemporalRequestContext:
    """계획이 확정한 기준일 → 요청 기준 시각. **낮춤과 재판정이 같은 것을 써야 한다.**

    기준 시각을 만드는 자리가 둘이면 같은 표현이 낮출 때와 다시 볼 때 서로 다른 창으로
    읽힌다(상대 시점 표현에서 원자가 어긋난다). 그래서 변환은 여기 하나뿐이고, 기준일을
    읽을 수 없을 때만 실행 시점을 쓴다.
    """

    zone = ZoneInfo(timezone)
    anchor: date | None
    if isinstance(current_date, date):
        anchor = current_date
    elif isinstance(current_date, str) and current_date.strip():
        try:
            anchor = date.fromisoformat(current_date)
        except ValueError:
            anchor = None
    else:
        anchor = None
    if anchor is None:
        return sir.TemporalRequestContext(now=datetime.now(zone), timezone=timezone)
    return sir.TemporalRequestContext(
        now=datetime.combine(anchor, time(9, 0), tzinfo=zone), timezone=timezone
    )


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

    계약은 두 축으로 나뉜다.

    * **판정**은 provenance 를 제외한 :func:`_atom_keys` 의 의미 일치다. 원문에서 시간
      조건을 다시 골라 같은 선언으로 낮춘 뒤, 그 원자들이 대상 표현 안에 그대로 있을
      때만 컴파일로 인정한다.
    * **방면에 쓰는 구간**은 애플리케이션 소유 낮춤이 계산한 근거
      (``request.condition.evidence``)다. 대상 표현이 주장한 근거는 판정에도 방면에도
      쓰지 않는다.

    근거 구간으로 판정하면 방면이 위조된다(실측: `Comparison(subject.gender = female)` 에
    전이 구절의 구간을 붙이면 통과했다). 그때 사라지는 것은 이력 조건이고, 남는 것은
    현재값 조건이다 — '휴면이 된 회원'이 '지금 휴면인 회원'으로 바뀌는 그 사고다.
    위조를 막는 것은 근거 비교가 아니라 **구조 비교**이므로, provenance 를 빼도 그 문은
    그대로 닫혀 있다.
    """

    if expression is None:
        return ()
    request_context = context or request_context_for(today, timezone=timezone)
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
    "request_context_for",
    "synthesize_temporal_claim",
    "temporal_metric_for_domain",
]
