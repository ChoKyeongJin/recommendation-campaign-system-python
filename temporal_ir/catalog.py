"""Metric · Temporal Binding 카탈로그 — '무엇을'과 '어떻게 관측하는가'를 분리해 선언한다.

왜 나누는가
-----------
``member.grade`` 는 **속성의 의미**이고, 그 값을 어디서 어떻게 읽는가는 **관측 방식**이다.
같은 속성이라도 관측 방식이 다르면 답할 수 있는 질문이 다르다::

    member.grade.current            지금 등급만 안다(과거 시점 질의 불가)
    member.grade.monthly_snapshot   월별 관측 상태를 안다(월중 변경 시각은 모른다)

이 둘을 한 필드로 취급하면 '지난달 말 기준 VIP'가 현재 등급 비교로 조용히 축소된다. 축소된
SQL 은 성공한 것처럼 보이고, 잘못된 대상이 발송된 뒤에야 드러난다.

무엇을 선언하는가
-----------------
:class:`TemporalBindingSpec` 은 lowering 이 필요로 하는 **모든** 사실을 담는다: 어느 소스인지,
주체와 어떻게 상관되는지, 값과 시각이 어느 필드인지, 한 시점에 몇 행이 가능한지, 정렬과 동점자
기준이 무엇인지, 어떤 연산자를 받을 수 있는지, 무엇을 관측할 수 있는지
(:class:`ObservationCapabilities`), 적재는 어디까지인지(:class:`CoverageSpec`).

lowering 코드에는 그래서 ``MEMBER_NO`` 도 ``ZTS_GRADE`` 도 ``VIP`` 도 없다. 새 월별 속성은
이 카탈로그 한 항목이며 Python 은 한 줄도 바뀌지 않는다.

이중 선언과 교차검증
--------------------
물리 바인딩의 **권위**는 여전히 :mod:`resolved_semantic_catalog`(audience_catalog.json)에 있다.
여기서 다시 적는 상관키·저장 포맷·칸 단위는 사본이 아니라 **교차검증 선언**이다: 두 선언이
어긋나면 로딩이 이름을 대며 실패한다. 시간 의미가 물리 grain 을 잘못 알면 그 오답은 SQL 이
아니라 결과 집합으로만 드러나므로, 그 어긋남은 조용할 수 없어야 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

import event_compiler
import resolved_semantic_catalog
from temporal_ir import calendar as tcal
from temporal_ir import semantic_ir as sir

DEFAULT_TEMPORAL_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs" / "data" / "runtime" / "semantics" / "temporal_bindings.json"
)


class TemporalCatalogError(ValueError):
    """카탈로그 선언 오류. 어떤 심볼의 무엇이 왜 어긋났는지 이름을 댄다."""

    def __init__(self, code: str, message: str, *, symbol: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.symbol = symbol


class BindingResolutionError(TemporalCatalogError):
    """요청한 연산을 지원하는 binding 을 확정할 수 없다(없거나, 모호하다)."""


# ── 닫힌 어휘 ─────────────────────────────────────────────────────────────────────


class Representation(StrEnum):
    """데이터가 이력을 **보유한 형태**(§7). 이름만으로 능력을 추론하지 않는다 — 능력은 따로 선언한다."""

    EVENT_LOG = "event_log"
    SINGLETON = "singleton"
    LATEST_ONLY = "latest_only"
    PERIODIC_SNAPSHOT = "periodic_snapshot"
    VALIDITY_INTERVAL = "validity_interval"
    CURRENT_ONLY = "current_only"


class TimeRole(StrEnum):
    """시각 필드가 무엇의 시각인가."""

    EVENT_TIME = "event_time"
    SNAPSHOT_TIME = "snapshot_time"
    STATE_CHANGE_TIME = "state_change_time"
    VALIDITY_START = "validity_start"


class BucketSemantics(StrEnum):
    """스냅샷 한 행이 그 칸의 **무엇을** 대표하는가.

    '지난달 말 기준'을 월 스냅샷으로 답하려면 그 행이 월말 상태를 대표한다는 **계약**이 있어야
    한다. 계약 없이 답하면 그것은 '그 달 어느 시점의 상태'이며 다른 집합이다.
    """

    PERIOD_END_STATE = "period_end_state"
    PERIOD_START_STATE = "period_start_state"
    PERIOD_ANY_STATE = "period_any_state"
    POINT_IN_TIME = "point_in_time"


class Completeness(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class BackfillStatus(StrEnum):
    COMPLETE = "complete"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"


class CoverageStatus(StrEnum):
    """적재 판정(의미 지원 여부와 **다른 축**)."""

    EVALUABLE = "supported_and_evaluable"
    OUT_OF_COVERAGE = "supported_but_out_of_coverage"
    INCOMPLETE = "supported_but_incomplete"


class TieBreakerPolicy(StrEnum):
    """한 시점에 여러 행이 가능할 때 어느 행을 볼 것인가."""

    FIRST_BY_ORDERING = "first_by_ordering"
    LAST_BY_ORDERING = "last_by_ordering"
    REJECT = "reject"


SELECTION_STRATEGIES: frozenset[str] = frozenset({"exact_bucket", "latest_at_or_before"})


def _require(value: Any, label: str, *, symbol: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemporalCatalogError(
            "invalid_temporal_declaration", f"{label} 은 비어 있지 않은 문자열이어야 합니다",
            symbol=symbol,
        )
    return value.strip()


def _string_tuple(value: Any, label: str, *, symbol: str | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise TemporalCatalogError(
            "invalid_temporal_declaration", f"{label} 은 문자열 목록이어야 합니다", symbol=symbol
        )
    return tuple(_require(item, label, symbol=symbol) for item in value)


def _date_or_none(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise TemporalCatalogError(
            "invalid_temporal_declaration", f"{label} 은 ISO 날짜여야 합니다: {value!r}"
        ) from exc


def _enum(value: Any, enum_type: type[StrEnum], label: str, *, symbol: str | None = None) -> Any:
    try:
        return enum_type(str(value))
    except ValueError as exc:
        raise TemporalCatalogError(
            "invalid_temporal_declaration",
            f"{label}: 알 수 없는 값 {value!r} (허용: {[item.value for item in enum_type]})",
            symbol=symbol,
        ) from exc


# ── 관측 능력 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ObservationCapabilities:
    """이 binding 이 **실제로 관측할 수 있는 것**(§8).

    representation 이름에서 파생하지 않고 항목마다 선언하게 하는 이유: 같은 이름의 표현이라도
    적재 방식에 따라 답할 수 있는 것이 다르다(중간 이력이 있는 스냅샷도 있고 없는 것도 있다).
    선언은 여덟 항목 전부를 요구한다 — 빠진 항목을 기본값으로 채우면 그 기본값이 곧 추측이다.
    """

    supports_point_state: bool
    supports_ordered_observations: bool
    supports_exact_transition_time: bool
    supports_intra_bucket_changes: bool
    supports_continuous_validity: bool
    supports_complete_bucket_enumeration: bool
    supports_event_count: bool
    # 구간 안 **임의 발생**의 존재를 판정할 수 있는가. latest_only 와 event_log 를 가르는 능력이며
    # (마지막 것만 아는 표현은 '그 구간에 있었는가'에 답할 수 없다), 이름이 아니라 이 선언이 판정한다.
    supports_all_occurrences: bool

    def __post_init__(self) -> None:
        for name in FLAG_NAMES:
            if not isinstance(getattr(self, name), bool):
                raise TemporalCatalogError(
                    "invalid_temporal_declaration", f"observation capability {name} 은 boolean 이어야 합니다"
                )
        # 서로 모순되는 선언은 로딩에서 막는다. 이것은 representation 추론이 아니라
        # **능력 사이의 함의**다(정확한 변경 시각을 알면 관측 순서도 알 수 있다).
        implications = (
            ("supports_intra_bucket_changes", "supports_exact_transition_time"),
            ("supports_exact_transition_time", "supports_ordered_observations"),
            ("supports_complete_bucket_enumeration", "supports_ordered_observations"),
            ("supports_event_count", "supports_all_occurrences"),
        )
        for claimed, required in implications:
            if getattr(self, claimed) and not getattr(self, required):
                raise TemporalCatalogError(
                    "invalid_temporal_declaration",
                    f"{claimed}=True 는 {required}=True 를 함의합니다",
                )

    def has(self, name: str) -> bool:
        if name not in FLAG_NAMES:
            raise TemporalCatalogError(
                "invalid_temporal_declaration", f"알 수 없는 관측 능력: {name!r}"
            )
        return bool(getattr(self, name))

    def to_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in FLAG_NAMES}

    @classmethod
    def from_dict(cls, raw: Any, *, symbol: str) -> "ObservationCapabilities":
        if not isinstance(raw, Mapping):
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {symbol!r} 에 observation_capabilities 선언이 없습니다",
                symbol=symbol,
            )
        unknown = sorted(set(raw) - set(FLAG_NAMES))
        missing = sorted(set(FLAG_NAMES) - set(raw))
        if unknown or missing:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {symbol!r} 의 observation_capabilities 선언이 어긋납니다"
                f"(알 수 없음: {unknown}, 누락: {missing})",
                symbol=symbol,
            )
        return cls(**{name: bool(raw[name]) for name in FLAG_NAMES})


FLAG_NAMES: tuple[str, ...] = (
    "supports_point_state",
    "supports_ordered_observations",
    "supports_exact_transition_time",
    "supports_intra_bucket_changes",
    "supports_continuous_validity",
    "supports_complete_bucket_enumeration",
    "supports_event_count",
    "supports_all_occurrences",
)


# ── 적재 범위 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CoverageAssessment:
    """한 요청 구간에 대한 적재 판정 + 근거."""

    status: CoverageStatus
    missing_buckets: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "missing_buckets": list(self.missing_buckets),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class CoverageSpec:
    """적재 범위를 **시작·끝 두 날짜로만** 판정하지 않는다(§14).

    범위 안이어도 중간 파티션이 비어 있으면 전칭 판정(모든 달)은 답할 수 없다. 그 사실이
    선언에 없으면 SQL 은 조용히 '있는 달만' 세고 사용자는 그것을 전부라고 읽는다.
    """

    available_from: date | None = None
    available_through: date | None = None  # 포함 끝
    expected_cadence: sir.TimeUnit | None = None
    completeness: Completeness = Completeness.UNKNOWN
    missing_partitions: tuple[str, ...] = ()
    freshness_through: date | None = None
    ingestion_lag_days: int | None = None
    backfill_status: BackfillStatus = BackfillStatus.UNKNOWN

    def __post_init__(self) -> None:
        if (
            self.available_from is not None
            and self.available_through is not None
            and self.available_from > self.available_through
        ):
            raise TemporalCatalogError(
                "invalid_temporal_declaration", "coverage available_from 이 available_through 보다 늦습니다"
            )
        if self.ingestion_lag_days is not None and (
            isinstance(self.ingestion_lag_days, bool) or self.ingestion_lag_days < 0
        ):
            raise TemporalCatalogError(
                "invalid_temporal_declaration", "coverage ingestion_lag_days 는 0 이상 정수여야 합니다"
            )

    @property
    def declared(self) -> bool:
        """적재에 대해 **무엇이라도** 선언되었는가.

        예전에는 두 날짜만 봤다. 그래서 결측 파티션만 선언한 카탈로그가 '선언 없음'으로 읽혀
        그 결측이 판정에서 통째로 사라졌다(리뷰 실측).
        """
        return any((
            self.available_from is not None,
            self.available_through is not None,
            bool(self.missing_partitions),
            self.completeness is not Completeness.UNKNOWN,
            self.freshness_through is not None,
            self.backfill_status is not BackfillStatus.UNKNOWN,
        ))

    def assess(
        self, interval: tcal.TimeInterval, unit: sir.TimeUnit, timezone: str
    ) -> CoverageAssessment:
        """요청 구간이 선언된 적재와 어떻게 만나는가."""
        warnings: list[str] = []
        if not self.declared:
            return CoverageAssessment(
                CoverageStatus.EVALUABLE, warnings=("coverage_undeclared",)
            )
        tz = tcal.zone(timezone)
        start = interval.start.astimezone(tz).date()
        end_inclusive = (interval.end_exclusive.astimezone(tz) - timedelta(microseconds=1)).date()
        low = self.available_from or date.min
        high = self.available_through or date.max
        if end_inclusive < low or start > high:
            return CoverageAssessment(CoverageStatus.OUT_OF_COVERAGE, warnings=("out_of_coverage",))

        missing = self.missing_buckets_in(interval, unit, timezone)
        partly_outside = start < low or end_inclusive > high
        if partly_outside:
            warnings.append("partially_out_of_coverage")
        if self.completeness is Completeness.INCOMPLETE:
            warnings.append("declared_incomplete")
        if self.backfill_status is BackfillStatus.IN_PROGRESS:
            warnings.append("backfill_in_progress")
        if self.freshness_through is not None and self.freshness_through < end_inclusive:
            warnings.append("freshness_lag")
        if missing or partly_outside or self.completeness is Completeness.INCOMPLETE:
            return CoverageAssessment(
                CoverageStatus.INCOMPLETE, missing_buckets=missing, warnings=tuple(warnings)
            )
        return CoverageAssessment(CoverageStatus.EVALUABLE, warnings=tuple(warnings))

    def missing_buckets_in(
        self, interval: tcal.TimeInterval, unit: sir.TimeUnit, timezone: str
    ) -> tuple[str, ...]:
        """요청 구간이 덮는 칸 중 **없다고 선언된** 칸(범위 밖 + 명시된 결측 파티션)."""
        declared_missing = set(self.missing_partitions)
        low = self.available_from
        high = self.available_through
        missing: list[str] = []
        for bucket in tcal.enumerate_buckets(interval, unit, timezone):
            key = partition_key(bucket, unit, timezone)
            bucket_start = bucket.start.astimezone(tcal.zone(timezone)).date()
            bucket_end = (
                bucket.end_exclusive.astimezone(tcal.zone(timezone)) - timedelta(microseconds=1)
            ).date()
            if key in declared_missing:
                missing.append(key)
            elif (low is not None and bucket_end < low) or (high is not None and bucket_start > high):
                missing.append(key)
        return tuple(missing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_from": self.available_from.isoformat() if self.available_from else None,
            "available_through": (
                self.available_through.isoformat() if self.available_through else None
            ),
            "expected_cadence": str(self.expected_cadence) if self.expected_cadence else None,
            "completeness": str(self.completeness),
            "missing_partitions": list(self.missing_partitions),
            "freshness_through": (
                self.freshness_through.isoformat() if self.freshness_through else None
            ),
            "ingestion_lag_days": self.ingestion_lag_days,
            "backfill_status": str(self.backfill_status),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "CoverageSpec":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise TemporalCatalogError("invalid_temporal_declaration", "coverage 는 객체여야 합니다")
        known = {
            "label", "note", "available_from", "available_through", "from", "to",
            "expected_cadence", "completeness", "missing_partitions", "freshness_through",
            "ingestion_lag_days", "backfill_status",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            # 선언 키 오타는 경계를 조용히 None 으로 만든다 — 이 저장소가 실제로 겪은 결함이다.
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"coverage 가 알 수 없는 키를 선언했습니다: {unknown}",
            )
        cadence = raw.get("expected_cadence")
        return cls(
            available_from=_date_or_none(
                raw.get("available_from", raw.get("from")), "coverage.available_from"
            ),
            available_through=_date_or_none(
                raw.get("available_through", raw.get("to")), "coverage.available_through"
            ),
            expected_cadence=(
                _enum(cadence, sir.TimeUnit, "coverage.expected_cadence") if cadence else None
            ),
            completeness=_enum(
                raw.get("completeness", Completeness.UNKNOWN), Completeness, "coverage.completeness"
            ),
            missing_partitions=_string_tuple(
                raw.get("missing_partitions"), "coverage.missing_partitions"
            ),
            freshness_through=_date_or_none(
                raw.get("freshness_through"), "coverage.freshness_through"
            ),
            ingestion_lag_days=raw.get("ingestion_lag_days"),
            backfill_status=_enum(
                raw.get("backfill_status", BackfillStatus.UNKNOWN),
                BackfillStatus,
                "coverage.backfill_status",
            ),
        )


_PARTITION_FORMATS: dict[sir.TimeUnit, str] = {
    sir.TimeUnit.DAY: "%Y%m%d",
    sir.TimeUnit.MONTH: "%Y%m",
    sir.TimeUnit.YEAR: "%Y",
}


def partition_key(bucket: tcal.TimeInterval, unit: sir.TimeUnit, timezone: str) -> str:
    """칸 하나의 선언 표기. 결측 파티션 선언과 **같은 표기**여야 한다."""
    local = bucket.start.astimezone(tcal.zone(timezone))
    if unit is sir.TimeUnit.WEEK:
        year, week, _ = local.isocalendar()
        return f"{year:04d}-W{week:02d}"
    return local.strftime(_PARTITION_FORMATS[unit])


# ── binding / metric ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TemporalBindingSpec:
    """한 metric 을 **관측하는 한 가지 방법**의 완전한 선언."""

    id: str
    metric_id: str
    source: str
    representation: Representation
    subject_entity: str = "member"
    role: TimeRole = TimeRole.EVENT_TIME
    value_field: str | None = None
    prev_value_field: str | None = None
    time_field: str | None = None
    transition_metric: str | None = None
    semantic_grain: sir.TimeUnit = sir.TimeUnit.DAY
    storage_codec: str = ""
    timezone: str = sir.DEFAULT_TIMEZONE
    calendar: str = "gregorian"
    bucket_semantics: BucketSemantics = BucketSemantics.POINT_IN_TIME
    correlation_keys: Mapping[str, str] = field(default_factory=dict)
    selection_strategies: frozenset[str] = frozenset({"exact_bucket"})
    supported_operators: frozenset[str] = frozenset()
    row_identity: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()
    # 한 시점에 주체당 최대 1행인가. **선언**으로 받고 row_identity 로 교차검증한다(:func:`validate_catalog`).
    # 파생만으로 두면 이벤트 로그처럼 원래 여러 행인 표현과, 스냅샷처럼 한 행이어야 하는 표현을
    # 같은 규칙으로 다루게 되고 그 차이가 as_of 의 답을 정한다.
    unique_per_time_point: bool = False
    tie_breaker: TieBreakerPolicy | None = None
    missing_policy: sir.MissingPolicy = sir.MissingPolicy.UNKNOWN
    null_policy: sir.NullPolicy = sir.NullPolicy.EXCLUDE
    observation_capabilities: ObservationCapabilities = field(
        default_factory=lambda: ObservationCapabilities(
            supports_point_state=True,
            supports_ordered_observations=False,
            supports_exact_transition_time=False,
            supports_intra_bucket_changes=False,
            supports_continuous_validity=False,
            supports_complete_bucket_enumeration=False,
            supports_event_count=False,
            supports_all_occurrences=False,
        )
    )
    coverage: CoverageSpec = field(default_factory=CoverageSpec)
    label: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        _require(self.id, "binding.id")
        _require(self.metric_id, f"binding {self.id}.metric_id", symbol=self.id)
        _require(self.source, f"binding {self.id}.source", symbol=self.id)
        object.__setattr__(self, "correlation_keys", MappingProxyType(dict(self.correlation_keys)))
        unknown = sorted(set(self.selection_strategies) - SELECTION_STRATEGIES)
        if unknown:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {self.id!r} 이 알 수 없는 선택 전략을 선언했습니다: {unknown}",
                symbol=self.id,
            )
        if self.representation is not Representation.CURRENT_ONLY and not self.time_field:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {self.id!r}({self.representation}) 에는 time_field 가 필요합니다",
                symbol=self.id,
            )
        if self.representation is Representation.CURRENT_ONLY and self.time_field:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {self.id!r} 은 current_only 인데 time_field 를 선언했습니다"
                " — 시각을 아는 표현이면 current_only 가 아닙니다",
                symbol=self.id,
            )
        if (
            self.observation_capabilities.supports_point_state
            and not self.unique_per_time_point
            and self.tie_breaker is None
        ):
            # 시점 상태를 읽는 관측인데 한 시점에 여러 행이면 '그 시점의 값'이 정해지지 않는다.
            # 이벤트 로그처럼 원래 여러 행인 표현은 시점 상태를 주장하지 않으므로 여기 걸리지 않는다.
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {self.id!r} 은 시점 상태를 관측한다고 하면서 한 시점에 여러 행이 "
                "가능하다고 선언했습니다. tie_breaker 없이는 같은 질의가 적재에 따라 다른 답을 냅니다",
                symbol=self.id,
            )

    def supports(self, operator: str) -> bool:
        return operator in self.supported_operators

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "metric_id": self.metric_id,
            "source": self.source,
            "subject_entity": self.subject_entity,
            "representation": str(self.representation),
            "role": str(self.role),
            "value_field": self.value_field,
            "prev_value_field": self.prev_value_field,
            "time_field": self.time_field,
            "transition_metric": self.transition_metric,
            "semantic_grain": str(self.semantic_grain),
            "storage_codec": self.storage_codec,
            "timezone": self.timezone,
            "calendar": self.calendar,
            "bucket_semantics": str(self.bucket_semantics),
            "correlation_keys": dict(self.correlation_keys),
            "selection_strategies": sorted(self.selection_strategies),
            "supported_operators": sorted(self.supported_operators),
            "row_identity": list(self.row_identity),
            "ordering": list(self.ordering),
            "unique_per_time_point": self.unique_per_time_point,
            "tie_breaker": str(self.tie_breaker) if self.tie_breaker else None,
            "missing_policy": str(self.missing_policy),
            "null_policy": str(self.null_policy),
            "observation_capabilities": self.observation_capabilities.to_dict(),
            "coverage": self.coverage.to_dict(),
            "label": self.label,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, binding_id: str, raw: Any) -> "TemporalBindingSpec":
        if not isinstance(raw, Mapping):
            raise TemporalCatalogError(
                "invalid_temporal_declaration", f"binding {binding_id!r} 은 객체여야 합니다"
            )
        tie = raw.get("tie_breaker")
        return cls(
            id=binding_id,
            metric_id=_require(raw.get("metric_id"), f"binding {binding_id}.metric_id"),
            source=_require(raw.get("source"), f"binding {binding_id}.source"),
            representation=_enum(
                raw.get("representation"), Representation, f"binding {binding_id}.representation"
            ),
            subject_entity=str(raw.get("subject_entity") or "member"),
            role=_enum(raw.get("role", TimeRole.EVENT_TIME), TimeRole, f"binding {binding_id}.role"),
            value_field=(str(raw["value_field"]) if raw.get("value_field") else None),
            prev_value_field=(
                str(raw["prev_value_field"]) if raw.get("prev_value_field") else None
            ),
            time_field=(str(raw["time_field"]) if raw.get("time_field") else None),
            transition_metric=(
                str(raw["transition_metric"]) if raw.get("transition_metric") else None
            ),
            semantic_grain=_enum(
                raw.get("semantic_grain", sir.TimeUnit.DAY),
                sir.TimeUnit,
                f"binding {binding_id}.semantic_grain",
            ),
            storage_codec=str(raw.get("storage_codec") or ""),
            timezone=str(raw.get("timezone") or sir.DEFAULT_TIMEZONE),
            calendar=str(raw.get("calendar") or "gregorian"),
            bucket_semantics=_enum(
                raw.get("bucket_semantics", BucketSemantics.POINT_IN_TIME),
                BucketSemantics,
                f"binding {binding_id}.bucket_semantics",
            ),
            correlation_keys={
                str(key): str(value) for key, value in (raw.get("correlation_keys") or {}).items()
            },
            selection_strategies=frozenset(
                _string_tuple(
                    raw.get("selection_strategies"), f"binding {binding_id}.selection_strategies"
                )
                or ("exact_bucket",)
            ),
            supported_operators=frozenset(
                _string_tuple(
                    raw.get("supported_operators"), f"binding {binding_id}.supported_operators"
                )
            ),
            row_identity=_string_tuple(raw.get("row_identity"), f"binding {binding_id}.row_identity"),
            ordering=_string_tuple(raw.get("ordering"), f"binding {binding_id}.ordering"),
            unique_per_time_point=bool(raw.get("unique_per_time_point", False)),
            tie_breaker=(
                _enum(tie, TieBreakerPolicy, f"binding {binding_id}.tie_breaker") if tie else None
            ),
            missing_policy=_enum(
                raw.get("missing_policy", sir.MissingPolicy.UNKNOWN),
                sir.MissingPolicy,
                f"binding {binding_id}.missing_policy",
            ),
            null_policy=_enum(
                raw.get("null_policy", sir.NullPolicy.EXCLUDE),
                sir.NullPolicy,
                f"binding {binding_id}.null_policy",
            ),
            observation_capabilities=ObservationCapabilities.from_dict(
                raw.get("observation_capabilities"), symbol=binding_id
            ),
            coverage=CoverageSpec.from_dict(raw.get("coverage")),
            label=str(raw.get("label") or ""),
            version=int(raw.get("version") or 1),
        )


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """속성의 **의미**. 물리도 시간도 모른다 — 그것은 binding 의 일이다."""

    id: str
    value_type: str = "string"
    value_domain: str | None = None
    supported_comparisons: frozenset[str] = frozenset({"=", "!="})
    temporal_bindings: tuple[str, ...] = ()
    binding_priority: tuple[str, ...] = ()
    label: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        _require(self.id, "metric.id")
        unknown = sorted(set(self.supported_comparisons) - sir.COMPARISON_OPERATORS)
        if unknown:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"metric {self.id!r} 이 알 수 없는 비교 연산자를 선언했습니다: {unknown}",
                symbol=self.id,
            )
        unknown_priority = sorted(set(self.binding_priority) - set(self.temporal_bindings))
        if unknown_priority:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"metric {self.id!r} 의 binding_priority 가 선언되지 않은 binding 을 가리킵니다:"
                f" {unknown_priority}",
                symbol=self.id,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "value_type": self.value_type,
            "value_domain": self.value_domain,
            "supported_comparisons": sorted(self.supported_comparisons),
            "temporal_bindings": list(self.temporal_bindings),
            "binding_priority": list(self.binding_priority),
            "label": self.label,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, metric_id: str, raw: Any) -> "MetricSpec":
        if not isinstance(raw, Mapping):
            raise TemporalCatalogError(
                "invalid_temporal_declaration", f"metric {metric_id!r} 은 객체여야 합니다"
            )
        comparisons = _string_tuple(
            raw.get("supported_comparisons"), f"metric {metric_id}.supported_comparisons"
        )
        return cls(
            id=metric_id,
            value_type=str(raw.get("value_type") or "string"),
            value_domain=(str(raw["value_domain"]) if raw.get("value_domain") else None),
            supported_comparisons=frozenset(comparisons or ("=", "!=")),
            temporal_bindings=_string_tuple(
                raw.get("temporal_bindings"), f"metric {metric_id}.temporal_bindings"
            ),
            binding_priority=_string_tuple(
                raw.get("binding_priority"), f"metric {metric_id}.binding_priority"
            ),
            label=str(raw.get("label") or ""),
            version=int(raw.get("version") or 1),
        )


# ── 카탈로그 ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TemporalCatalog:
    """검증된 metric/binding 선언 + binding 해석기."""

    metrics: Mapping[str, MetricSpec]
    bindings: Mapping[str, TemporalBindingSpec]

    def metric(self, metric_id: str) -> MetricSpec:
        metric = self.metrics.get(metric_id)
        if metric is None:
            raise TemporalCatalogError(
                "temporal_metric_unregistered",
                f"선언되지 않은 temporal metric 입니다: {metric_id!r}",
                symbol=metric_id,
            )
        return metric

    def binding(self, binding_id: str) -> TemporalBindingSpec:
        binding = self.bindings.get(binding_id)
        if binding is None:
            raise TemporalCatalogError(
                "temporal_binding_unregistered",
                f"선언되지 않은 temporal binding 입니다: {binding_id!r}",
                symbol=binding_id,
            )
        return binding

    def bindings_for(self, metric_id: str) -> tuple[TemporalBindingSpec, ...]:
        return tuple(self.binding(name) for name in self.metric(metric_id).temporal_bindings)

    def resolve_binding(
        self, metric_id: str, *, operator: str, requested: str | None = None
    ) -> TemporalBindingSpec:
        """요청한 시간 연산을 **지원하는** binding 을 고른다.

        복수 후보에서 임의로 고르지 않는다. 우선순위 선언이 있으면 그 순서를 따르고, 없으면
        모호성 실패로 답한다 — 어느 쪽을 골라도 절반은 다른 집합이고 그 오답은 실행 결과로만
        드러난다.
        """
        metric = self.metric(metric_id)
        if requested is not None:
            binding = self.binding(requested)
            if binding.metric_id != metric_id:
                raise BindingResolutionError(
                    "temporal_binding_metric_mismatch",
                    f"binding {requested!r} 은 metric {binding.metric_id!r} 의 것입니다"
                    f"(요청: {metric_id!r})",
                    symbol=requested,
                )
            if not binding.supports(operator):
                raise BindingResolutionError(
                    "temporal_operator_unsupported_by_binding",
                    f"binding {requested!r} 은 {operator} 를 지원한다고 선언하지 않았습니다"
                    f"(선언된 연산: {sorted(binding.supported_operators)})",
                    symbol=requested,
                )
            return binding

        candidates = list(self.candidate_bindings(metric_id, operator=operator))
        if not candidates:
            raise BindingResolutionError(
                "temporal_operator_unsupported_by_metric",
                f"metric {metric_id!r} 의 어떤 관측 방식도 {operator} 를 지원하지 않습니다"
                f"(선언된 관측: {list(metric.temporal_bindings)})",
                symbol=metric_id,
            )
        if len(candidates) == 1:
            return candidates[0]
        by_id = {item.id: item for item in candidates}
        for preferred in metric.binding_priority:
            if preferred in by_id:
                return by_id[preferred]
        raise BindingResolutionError(
            "temporal_binding_ambiguous",
            f"metric {metric_id!r} 에 {operator} 를 지원하는 관측 방식이 둘 이상입니다"
            f"({sorted(by_id)}). 우선순위 선언이나 명시적 binding 이 필요합니다",
            symbol=metric_id,
        )

    def candidate_bindings(
        self, metric_id: str, *, operator: str
    ) -> tuple[TemporalBindingSpec, ...]:
        """그 연산을 **선언한** 관측 방식들을 우선순위 순서로.

        고르는 것은 호출자(lowering)다. 여기서 바로 하나를 고르지 않는 이유는 '지원한다고
        선언했다'와 '이 요청에 답할 수 있다'가 다르기 때문이다 — 현재값만 아는 관측도
        ``temporal.as_of`` 를 지원하지만 지난달을 물으면 답할 수 없다. 그 판정은 연산자 계약이
        하고, 여기서는 후보와 **결정론적 순서**만 준다.
        """
        metric = self.metric(metric_id)
        candidates = [item for item in self.bindings_for(metric_id) if item.supports(operator)]
        order = {name: index for index, name in enumerate(metric.binding_priority)}
        return tuple(
            sorted(candidates, key=lambda item: (order.get(item.id, len(order)), item.id))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": {key: value.to_dict() for key, value in sorted(self.metrics.items())},
            "bindings": {key: value.to_dict() for key, value in sorted(self.bindings.items())},
        }


def load_temporal_catalog(
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    *,
    path: str | Path = DEFAULT_TEMPORAL_CATALOG_PATH,
    payload: Mapping[str, Any] | None = None,
    known_operators: Iterable[str] | None = None,
) -> TemporalCatalog:
    """선언을 읽고 **물리 카탈로그와 교차검증**해 카탈로그를 만든다.

    검증하지 않고 실어 주는 로더는 선언을 장식으로 만든다 — 이 저장소는 그것을 이미 겪었다
    (읽히지 않는 coverage 키, 살아 있지 않은 별칭). 그래서 여기서는 소스·필드·상관키·칸
    단위·값 도메인·연산자 이름을 전부 대조하고, 어긋나면 이름을 대며 실패한다.
    """
    if payload is None:
        resolved_path = Path(path)
        try:
            payload = json.loads(resolved_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TemporalCatalogError(
                "temporal_catalog_load_failed", f"temporal 카탈로그를 읽을 수 없습니다: {resolved_path}: {exc}"
            ) from exc
    if not isinstance(payload, Mapping):
        raise TemporalCatalogError("invalid_temporal_declaration", "temporal 카탈로그 루트는 객체여야 합니다")

    metrics = {
        metric_id: MetricSpec.from_dict(metric_id, raw)
        for metric_id, raw in _section(payload, "metrics").items()
    }
    bindings = {
        binding_id: TemporalBindingSpec.from_dict(binding_id, raw)
        for binding_id, raw in _section(payload, "bindings").items()
    }
    catalog = TemporalCatalog(
        metrics=MappingProxyType(dict(sorted(metrics.items()))),
        bindings=MappingProxyType(dict(sorted(bindings.items()))),
    )
    validate_catalog(catalog, semantic_catalog, known_operators=known_operators)
    return catalog


def _section(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name) or {}
    if not isinstance(value, Mapping):
        raise TemporalCatalogError("invalid_temporal_declaration", f"{name} 은 객체여야 합니다")
    return {str(key): item for key, item in value.items() if not str(key).startswith("_")}


def validate_catalog(
    catalog: TemporalCatalog,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    *,
    known_operators: Iterable[str] | None = None,
) -> None:
    """§17-3 의 검증 목록을 한 곳에서 수행한다."""
    operator_names = frozenset(known_operators or ())
    for metric in catalog.metrics.values():
        for binding_id in metric.temporal_bindings:
            binding = catalog.binding(binding_id)
            if binding.metric_id != metric.id:
                raise TemporalCatalogError(
                    "temporal_catalog_reference_mismatch",
                    f"metric {metric.id!r} 이 binding {binding_id!r} 을 소유한다고 선언했지만 "
                    f"그 binding 은 {binding.metric_id!r} 의 것입니다",
                    symbol=binding_id,
                )
    for binding in catalog.bindings.values():
        metric = catalog.metric(binding.metric_id)
        if binding.id not in metric.temporal_bindings:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r} 이 metric {metric.id!r} 의 temporal_bindings 에 없습니다",
                symbol=binding.id,
            )
        _validate_binding(binding, metric, semantic_catalog, operator_names)


def _validate_binding(
    binding: TemporalBindingSpec,
    metric: MetricSpec,
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    operator_names: frozenset[str],
) -> None:
    try:
        source = semantic_catalog.source(binding.source)
    except resolved_semantic_catalog.CatalogError as exc:
        raise TemporalCatalogError(
            "temporal_catalog_reference_unresolved",
            f"binding {binding.id!r} 이 알 수 없는 소스 {binding.source!r} 를 가리킵니다",
            symbol=binding.source,
        ) from exc

    if operator_names and not binding.supported_operators <= operator_names:
        raise TemporalCatalogError(
            "temporal_catalog_reference_unresolved",
            f"binding {binding.id!r} 이 등록되지 않은 시간 연산자를 선언했습니다: "
            f"{sorted(binding.supported_operators - operator_names)}",
            symbol=binding.id,
        )

    for label, field_id in (("value_field", binding.value_field), ("prev_value_field", binding.prev_value_field)):
        if field_id is None:
            continue
        spec = _field(semantic_catalog, field_id, binding.id, label)
        if spec.source != binding.source:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r}.{label} {field_id!r} 이 다른 소스({spec.source!r})의 필드입니다",
                symbol=field_id,
            )
        if metric.value_domain and spec.value_domain != metric.value_domain:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r}.{label} {field_id!r} 의 값 도메인({spec.value_domain!r})이 "
                f"metric {metric.id!r} 의 값 도메인({metric.value_domain!r})과 다릅니다",
                symbol=field_id,
            )

    if binding.time_field is not None:
        time_spec = _field(semantic_catalog, binding.time_field, binding.id, "time_field")
        if time_spec.source != binding.source:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r}.time_field 가 다른 소스({time_spec.source!r})의 필드입니다",
                symbol=binding.time_field,
            )
        grain = event_compiler.DATA_TYPE_GRAINS.get(time_spec.data_type)
        if grain is None:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r}.time_field {binding.time_field!r} 는 시각 타입이 아닙니다"
                f"({time_spec.data_type})",
                symbol=binding.time_field,
            )
        if grain.unit != str(binding.semantic_grain):
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r} 은 semantic_grain={binding.semantic_grain} 을 선언했지만 "
                f"시각 컬럼의 저장 칸은 {grain.unit} 입니다",
                symbol=binding.id,
            )
        if binding.storage_codec and binding.storage_codec != grain.time_format:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r} 의 storage_codec({binding.storage_codec!r})이 소스 선언"
                f"({grain.time_format!r})과 다릅니다",
                symbol=binding.id,
            )
        if binding.ordering and binding.ordering[0] != source.event.time_column:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {binding.id!r} 의 ordering 첫 키는 시각 컬럼"
                f"({source.event.time_column!r})이어야 합니다",
                symbol=binding.id,
            )

    declared_subject = binding.correlation_keys.get("subject")
    declared_source = binding.correlation_keys.get("source")
    if binding.representation is not Representation.CURRENT_ONLY:
        if declared_subject is None or declared_source is None:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {binding.id!r} 에는 correlation_keys.subject/source 선언이 필요합니다"
                " — 상관 조건의 주인은 카탈로그이고 lowerer 가 아닙니다",
                symbol=binding.id,
            )
        if declared_subject != source.event.subject_key or declared_source != source.event.event_subject_key:
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r} 의 상관키 선언({declared_subject}={declared_source})이 소스 "
                f"선언({source.event.subject_key}={source.event.event_subject_key})과 다릅니다",
                symbol=binding.id,
            )
        unknown_identity = [
            column
            for column in binding.row_identity
            if column not in {source.event.event_subject_key, source.event.time_column}
            and column not in _source_columns(semantic_catalog, binding.source)
        ]
        if unknown_identity:
            raise TemporalCatalogError(
                "temporal_catalog_reference_unresolved",
                f"binding {binding.id!r} 의 row_identity 가 소스에 없는 컬럼을 가리킵니다: "
                f"{unknown_identity}",
                symbol=binding.id,
            )
        if not binding.row_identity:
            raise TemporalCatalogError(
                "invalid_temporal_declaration",
                f"binding {binding.id!r} 에는 row_identity 선언이 필요합니다"
                " — 한 시점에 몇 행이 가능한지가 as_of 의 답을 정합니다",
                symbol=binding.id,
            )
        identity_axes = {source.event.event_subject_key, source.event.time_column}
        if binding.unique_per_time_point and not set(binding.row_identity) <= identity_axes:
            # '한 시점에 한 행'이라고 선언했는데 행 식별자에 다른 축이 있으면 둘 중 하나는 거짓이다.
            # 그 거짓은 SQL 로 드러나지 않고 결과 집합으로만 드러난다.
            raise TemporalCatalogError(
                "temporal_catalog_reference_mismatch",
                f"binding {binding.id!r} 은 한 시점에 한 행이라고 선언했지만 row_identity 에 "
                f"{sorted(set(binding.row_identity) - identity_axes)} 축이 더 있습니다",
                symbol=binding.id,
            )


def _field(
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    field_id: str,
    binding_id: str,
    label: str,
) -> resolved_semantic_catalog.FieldSpec:
    try:
        return semantic_catalog.field(field_id)
    except resolved_semantic_catalog.CatalogError as exc:
        raise TemporalCatalogError(
            "temporal_catalog_reference_unresolved",
            f"binding {binding_id!r}.{label} 이 알 수 없는 필드 {field_id!r} 를 가리킵니다",
            symbol=field_id,
        ) from exc


def _source_columns(
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog, source_id: str
) -> frozenset[str]:
    return frozenset(
        spec.compiler_field.column
        for spec in semantic_catalog.fields.values()
        if spec.source == source_id
    )


__all__ = [
    "BackfillStatus",
    "BindingResolutionError",
    "BucketSemantics",
    "Completeness",
    "CoverageAssessment",
    "CoverageSpec",
    "CoverageStatus",
    "DEFAULT_TEMPORAL_CATALOG_PATH",
    "FLAG_NAMES",
    "MetricSpec",
    "ObservationCapabilities",
    "Representation",
    "SELECTION_STRATEGIES",
    "TemporalBindingSpec",
    "TemporalCatalog",
    "TemporalCatalogError",
    "TieBreakerPolicy",
    "TimeRole",
    "load_temporal_catalog",
    "partition_key",
    "validate_catalog",
]
