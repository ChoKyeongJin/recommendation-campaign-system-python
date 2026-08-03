"""RequirementResolver — 요구를 **실행 가능한 사양**으로 확정하는 단 하나의 계층.

여기서 하는 일과 하지 않는 일을 계약으로 못박는다.

    한다:  자연어 기간 정규화 · 논리 참조 해결 · 기본값 적용 · 후보 선택 ·
           capability 검증 · policy 검증 · assumption/receipt 생성 · 사양 생성
    안 한다: SQL 생성(문자열도 조각도), 물리 테이블/컬럼 결정, 표현의 축소

미해결이 하나라도 남으면 사양을 만들지 않는다 — 부분 사양을 만들어 SQL 까지 보내는 것이
이 저장소가 반복해서 겪은 '조용한 오답'의 형태다. 그래서 결과 타입 자체가
:class:`ReadyResolution` 과 :class:`UnresolvedResolution` 둘로 갈린다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Annotated, Any, Literal, Protocol, TypeAlias, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, JsonValue

from query_pipeline.base import (
    Clock,
    IdFactory,
    StrictModel,
    SystemClock,
    UuidIdFactory,
)
from query_pipeline.event_query.expressions import (
    AbsoluteWindow,
    AggregateConditionExpression,
    AggregateDefinition,
    AggregateFunction,
    AggregateOperand,
    AndExpression,
    AttributeOperand,
    ComparisonExpression,
    ComparisonOperator,
    EntityRelation,
    EventExpression,
    LiteralOperand,
    LiteralValue,
    RelativeWindow,
    RollingWindow,
    TimeWindow,
    TimeWindowExpression,
    WindowUnit,
    attribute_operands,
    canonicalize_expression,
    walk,
)
from query_pipeline.event_query.models import (
    CapabilityRequirements,
    EventQuerySpec,
    QueryOutput,
    QuerySource,
    ResolvedBinding,
    ResolvedMeasure,
    ResolvedSort,
)
from query_pipeline.event_query.receipts import (
    Assumption,
    ReceiptAction,
    ReceiptActor,
    ResolutionReceipt,
)
from query_pipeline.requirement.issues import (
    IssueKind,
    IssueSeverity,
    RequirementIssue,
    ResolutionKind,
)
from query_pipeline.requirement.models import (
    AmbiguousRequirementValue,
    AudienceRequirement,
    InferredRequirementValue,
    MetricKind,
    MissingRequirementValue,
    RequirementConstraint,
    RequirementOperator,
    RequirementReference,
    RequirementValue,
    ResolvedRequirementValue,
    SortDirection,
)
from query_pipeline.requirement.validation import ExpressionValidator

SPEC_SCHEMA_VERSION = "1"


# ── 주입 계약 ─────────────────────────────────────────────────────────────────────


class FieldResolution(StrictModel):
    """논리 이름 하나의 확정 결과."""

    entity: str = Field(min_length=1)
    attribute: str = Field(min_length=1)


class MetricResolution(StrictModel):
    """지표 하나의 확정 결과 — 어떤 관계를 어떤 함수로, 어떤 단위(grain)로 집계하는가."""

    logical_name: str = Field(min_length=1)
    function: AggregateFunction
    entity: str = Field(min_length=1)
    attribute: str | None = None
    grain: str | None = None


@runtime_checkable
class SchemaRegistry(Protocol):
    """논리 이름 → entity/attribute 확정자. 물리 스키마는 모른다."""

    def resolve_entity(self, logical_name: str) -> str | None: ...

    def resolve_attribute(self, entity: str, logical_name: str) -> str | None: ...

    def resolve_field(self, logical_name: str) -> FieldResolution | None: ...

    def resolve_metric(self, logical_name: str) -> MetricResolution | None: ...


@runtime_checkable
class CapabilityProfile(Protocol):
    """실행 계층이 무엇을 할 줄 아는가. 판정 주체는 실행 자산이지 모델이 아니다."""

    def supports(self, capability: str) -> bool: ...


@runtime_checkable
class ResolutionPolicy(Protocol):
    """결핍 경로에 적용할 조직 기본값. 없으면 사용자에게 되묻는다."""

    def default_for(
        self, path: str, requirement: AudienceRequirement
    ) -> LiteralValue | None: ...


class StaticSchemaRegistry:
    """선언으로 만드는 기본 구현(테스트·소규모 도메인용)."""

    def __init__(
        self,
        *,
        entities: Mapping[str, str] | None = None,
        fields: Mapping[str, FieldResolution] | None = None,
        metrics: Mapping[str, MetricResolution] | None = None,
    ) -> None:
        self._entities = dict(entities or {})
        self._fields = dict(fields or {})
        self._metrics = dict(metrics or {})

    def resolve_entity(self, logical_name: str) -> str | None:
        if logical_name in self._entities:
            return self._entities[logical_name]
        return logical_name if logical_name in set(self._entities.values()) else None

    def resolve_attribute(self, entity: str, logical_name: str) -> str | None:
        resolution = self._fields.get(f"{entity}.{logical_name}") or self._fields.get(
            logical_name
        )
        if resolution is None or resolution.entity != entity:
            return None
        return resolution.attribute

    def resolve_field(self, logical_name: str) -> FieldResolution | None:
        return self._fields.get(logical_name)

    def resolve_metric(self, logical_name: str) -> MetricResolution | None:
        return self._metrics.get(logical_name)


class StaticCapabilityProfile:
    """선언된 capability 집합만 지원한다고 답한다(모르는 것은 지원하지 않는다)."""

    def __init__(self, supported: frozenset[str] | set[str] | tuple[str, ...]) -> None:
        self._supported = frozenset(supported)

    def supports(self, capability: str) -> bool:
        return capability in self._supported


class StaticResolutionPolicy:
    def __init__(self, defaults: Mapping[str, LiteralValue] | None = None) -> None:
        self._defaults = dict(defaults or {})

    def default_for(
        self, path: str, requirement: AudienceRequirement
    ) -> LiteralValue | None:
        return self._defaults.get(path)


class RequirementResolutionContext(StrictModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    timezone: str
    locale: str
    schema_registry: SchemaRegistry
    capability_profile: CapabilityProfile
    policy: ResolutionPolicy | None = None
    # 애플리케이션이 원문에서 **결정론으로** 뽑아 둔 값 색인. 검증기가 "이 결핍은 사용자가
    # 답할 것인가, 모델이 놓친 것인가"를 가르는 데 쓴다 — 이 계층은 그 형식을 해석하지 않고
    # 그대로 넘기기만 한다(해석의 소유자는 검증기를 준 도메인이다).
    literals: tuple[JsonValue, ...] = ()


# ── 결과 ──────────────────────────────────────────────────────────────────────────


class ReadyResolution(StrictModel):
    status: Literal["ready"] = "ready"
    spec: EventQuerySpec


class UnresolvedResolution(StrictModel):
    status: Literal["unresolved"] = "unresolved"
    requirement: AudienceRequirement
    issues: tuple[RequirementIssue, ...] = Field(min_length=1)


RequirementResolutionResult: TypeAlias = Annotated[
    ReadyResolution | UnresolvedResolution,
    Field(discriminator="status"),
]


class RequirementResolver(Protocol):
    async def resolve(
        self,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
    ) -> RequirementResolutionResult: ...


# ── 자연어 기간 ───────────────────────────────────────────────────────────────────
#
# 표면어 표를 **여기 두지 않는다**. 저장소의 기간 표면 문법 단일 소유자는
# :mod:`calendar_window`(달력 표현·기간 길이·시점 판정)이고, "표면어가 판정한 종류가 어느 창
# 타입인가"의 소유자는 :data:`event_ir.CALENDAR_KIND_WINDOW_TYPES` 다. 이 계층은 그 둘을
# 호출해 실행 표현으로 옮기기만 한다 — 표를 한 벌 더 적는 순간 '지난달'이 두 뜻을 갖는다.


class PeriodResolution(StrictModel):
    """기간 표현 하나의 확정 결과.

    ``window`` 를 :class:`AbsoluteWindow` 로 좁히지 않은 것이 핵심이다. '최근 30일'을 계획
    시점 날짜로 접으면 그 창이 **고정**되고, 같은 사양을 내일 실행해도 어제의 30일을 본다
    (:mod:`event_compiler` 가 명문화한 규칙: 롤링 경계는 실행 시점 함수로 렌더한다).
    """

    window: TimeWindow
    phrase: str


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - 배포 환경 결함
        raise ValueError(f"알 수 없는 timezone 입니다: {timezone!r}") from exc


def _absolute_window(calendar: Mapping[str, Any]) -> AbsoluteWindow | None:
    """달력 창 표기 ``{from, to}``(YYYYMMDD 포함 구간) → 반개구간. 경계 규칙은 IR 이 소유한다."""
    import event_ir

    interval = event_ir.AbsoluteInterval.from_calendar_window(dict(calendar))
    if interval is None:  # pragma: no cover - 소유자가 YYYYMMDD 표기를 보장한다
        return None
    return AbsoluteWindow(start=interval.start, end_exclusive=interval.end_exclusive)


def _duration_window(duration: Mapping[str, Any], *, today: date) -> TimeWindow | None:
    """기간 표현(길이/시점) → 실행 창.

    ``rolling``('최근 30일')은 **접지 않는다** — 실행 시점에 렌더되어야 하는 길이다.
    ``past_point``('3개월 전')는 과거의 한 달력 칸이라 계획 시점에 확정한다(기존 관례이자
    :func:`event_ir.resolve_relative_window` 의 계약).
    """
    import calendar_window
    import event_ir

    kind = duration.get(calendar_window.SOURCE_TEMPORAL_KIND_KEY)
    window_type = event_ir.CALENDAR_KIND_WINDOW_TYPES.get(str(kind))
    unit = event_ir.canonical_unit(duration.get("unit"))
    value = duration.get("value")
    if (
        window_type is None
        or unit is None
        or unit not in event_ir.WINDOW_UNITS
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        return None
    if window_type == "rolling":
        return RollingWindow(value=value, unit=WindowUnit(unit))
    interval = event_ir.resolve_relative_window(
        event_ir.RelativeWindow(value=value, unit=unit), today
    )
    return AbsoluteWindow(start=interval.start, end_exclusive=interval.end_exclusive)


def _window_description(window: TimeWindow) -> str:
    """영수증 문장에 실을 창 서술. 롤링은 경계 대신 **길이**로 말한다(경계가 없기 때문이다)."""
    if isinstance(window, AbsoluteWindow):
        return f"[{window.start}, {window.end_exclusive})"
    return f"실행 시점 기준 최근 {window.value}{window.unit.value} 창"


def _window_label(window: TimeWindow) -> str:
    """영수증의 ``after`` 값(기계 판독용 짧은 표기)."""
    if isinstance(window, AbsoluteWindow):
        return f"{window.start}/{window.end_exclusive}"
    return f"{window.kind}:{window.value}{window.unit.value}"


def resolve_period_phrase(
    phrase: str, *, now: datetime, timezone: str
) -> PeriodResolution | None:
    """기간 표현 하나를 실행 창으로 확정한다 — 표면 문법은 읽지 않고 **위임한다**.

    기준 시각의 타임존 달력을 쓰는 의미는 그대로다: 여기서 계산한 ``today`` 를 소유자에게
    넘긴다(소유자가 ``date.today()`` 를 부르면 배포 타임존이 답을 바꾼다).

    위임의 부수 이득으로 소유자가 아는 표현이 전부 열린다('YYYY년 M월'·'YYYY-MM-DD'·
    상반기·N분기·'금월/당월/전월'·단어형 기간 '일주일/반년'). 대신 이 계층이 각자 적던
    영어 표면어('last month')는 사라진다 — 소유자의 어휘에 없기 때문이고, 필요하면
    그 어휘를 늘리는 것이 답이지 여기 두 번째 표를 두는 것이 아니다.
    """
    import calendar_window

    text = phrase.strip()
    if not text:
        return None
    today = now.astimezone(_zone(timezone)).date()

    calendar = calendar_window.parse_calendar_window(
        text, today=today
    ) or calendar_window.parse_relative_year_window(text, today=today)
    if calendar is not None:
        absolute = _absolute_window(calendar)
        if absolute is not None:
            return PeriodResolution(window=absolute, phrase=phrase)

    duration = calendar_window.parse_duration_window(text)
    if duration is not None:
        window = _duration_window(duration, today=today)
        if window is not None:
            return PeriodResolution(window=window, phrase=phrase)
    return None


# ── capability 요구 도출 ──────────────────────────────────────────────────────────


def required_capabilities(expression: EventExpression) -> tuple[str, ...]:
    """이 표현을 실행하려면 필요한 능력 목록(노드 종류·연산자·집계 함수에서 파생).

    손으로 나열하지 않는다 — 새 노드가 생기면 이름이 자동으로 늘고, 프로필이 모르는
    이름은 '지원하지 않음'으로 답하므로 fail-close 가 기본이 된다.
    """
    capabilities: set[str] = set()
    for node in walk(expression):
        kind = getattr(node, "kind", None)
        if isinstance(kind, str):
            capabilities.add(f"node.{kind}")
        if isinstance(node, ComparisonExpression):
            capabilities.add(f"operator.{node.operator.value}")
        if isinstance(node, AggregateOperand):
            capabilities.add(f"aggregate.{node.function.value}")
        if isinstance(node, AggregateConditionExpression):
            capabilities.add(f"aggregate.{node.aggregate.function.value}")
            capabilities.add(f"operator.{node.operator.value}")
        if isinstance(node, TimeWindowExpression):
            capabilities.add(f"window.{node.window.kind}")
    return tuple(sorted(capabilities))


# ── 기본 구현 ─────────────────────────────────────────────────────────────────────


class _ResolutionLog:
    """해결 중 쌓이는 부산물(영수증·가정·미해결)의 단일 수집기."""

    def __init__(self, *, clock: Clock, id_factory: IdFactory) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self.receipts: list[ResolutionReceipt] = []
        self.assumptions: list[Assumption] = []
        self.issues: list[RequirementIssue] = []

    def receipt(
        self,
        *,
        action: ReceiptAction,
        input_path: str,
        reason: str,
        actor: ReceiptActor,
        output_path: str | None = None,
        before: LiteralValue | None = None,
        after: LiteralValue | None = None,
        confidence: float | None = None,
        policy_id: str | None = None,
    ) -> None:
        self.receipts.append(
            ResolutionReceipt(
                id=self._id_factory.new_id("receipt"),
                action=action,
                input_path=input_path,
                output_path=output_path,
                before=before,
                after=after,
                reason=reason,
                actor=actor,
                confidence=confidence,
                policy_id=policy_id,
                schema_version=SPEC_SCHEMA_VERSION,
                timestamp=self._clock.now(),
            )
        )

    def assume(
        self, *, path: str, description: str, value: LiteralValue, confidence: float | None = None
    ) -> None:
        self.assumptions.append(
            Assumption(
                id=self._id_factory.new_id("assumption"),
                path=path,
                description=description,
                value=value,
                confidence=confidence,
            )
        )

    def issue(
        self,
        *,
        kind: IssueKind,
        path: str,
        message: str,
        candidates: tuple[LiteralValue, ...] = (),
        identifier: str | None = None,
    ) -> None:
        self.issues.append(
            RequirementIssue(
                id=identifier or self._id_factory.new_id("issue"),
                kind=kind,
                severity=IssueSeverity.ERROR,
                path=path,
                message=message,
                candidates=candidates,
            )
        )


class DefaultRequirementResolver:
    """요구 → 사양. 두 입력 갈래(제안 표현 / 제약 목록)를 같은 규칙으로 확정한다."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        validators: Sequence[ExpressionValidator] = (),
    ) -> None:
        self._clock = clock or SystemClock()
        self._id_factory = id_factory or UuidIdFactory()
        # 검증기는 **순서대로** 돈다. issue 순서가 사용자에게 나가는 첫 문장을 정하므로
        # 순서는 이 리스트의 관측 가능한 계약이다.
        self._validators = tuple(validators)

    async def resolve(
        self,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
    ) -> RequirementResolutionResult:
        log = _ResolutionLog(clock=self._clock, id_factory=self._id_factory)

        for issue in requirement.blocking_issues:
            log.issues.append(issue)

        expression: EventExpression | None = None
        output: QueryOutput | None = None
        if requirement.expression is not None:
            resolved = self._resolve_proposed(requirement, context, log)
            if resolved is not None:
                expression, output = resolved
        elif requirement.constraints:
            resolved = self._resolve_constraints(requirement, context, log)
            if resolved is not None:
                expression, output = resolved
        elif not log.issues:
            log.issue(
                kind=IssueKind.MISSING,
                path="$.expression",
                message="타겟 오디언스 조건을 확정하지 못했습니다.",
                identifier="missing-audience-expression",
            )

        if expression is None or output is None or log.issues:
            return UnresolvedResolution(
                requirement=requirement,
                issues=tuple(log.issues)
                or (
                    RequirementIssue(
                        id="unresolved-requirement",
                        kind=IssueKind.MISSING,
                        severity=IssueSeverity.ERROR,
                        path="$",
                        message="요구를 실행 가능한 사양으로 확정하지 못했습니다.",
                    ),
                ),
            )

        expression = canonicalize_expression(expression)
        capabilities = required_capabilities(expression)
        missing = [
            capability
            for capability in capabilities
            if not context.capability_profile.supports(capability)
        ]
        if missing:
            for capability in missing:
                log.issue(
                    kind=IssueKind.UNSUPPORTED,
                    path="$.expression",
                    message=f"현재 실행 계층이 지원하지 않는 기능입니다: {capability}",
                    identifier=f"unsupported:{capability}",
                )
            return UnresolvedResolution(
                requirement=requirement, issues=tuple(log.issues)
            )
        log.receipt(
            action=ReceiptAction.CAPABILITY_CHECKED,
            input_path="$.expression",
            reason="실행 계층 capability 확인 완료: " + ", ".join(capabilities),
            actor=ReceiptActor.APPLICATION,
        )

        bindings = self._bindings(expression, context, log)
        if log.issues:
            return UnresolvedResolution(
                requirement=requirement, issues=tuple(log.issues)
            )

        spec = EventQuerySpec(
            id=self._id_factory.new_id("spec"),
            version=SPEC_SCHEMA_VERSION,
            expression=expression,
            output=output,
            bindings=bindings,
            assumptions=tuple(log.assumptions),
            receipts=tuple(log.receipts),
            source=QuerySource(
                requirement_id=requirement.id,
                requirement_version=requirement.version,
            ),
            capabilities=CapabilityRequirements(required=capabilities),
            created_at=self._clock.now(),
        )
        return ReadyResolution(spec=spec)

    # ── 갈래 1: LLM 이 제안한 canonical 표현 ────────────────────────────────────

    def _resolve_proposed(
        self,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
    ) -> tuple[EventExpression, QueryOutput] | None:
        assert requirement.expression is not None
        from query_pipeline.event_query.event_ir_bridge import (
            ExpressionBridgeError,
            from_wire,
        )

        try:
            expression = from_wire(requirement.expression.payload)
        except (ExpressionBridgeError, ValueError, TypeError) as exc:
            log.issue(
                kind=IssueKind.INVALID,
                path="$.expression",
                message=f"제안된 오디언스 표현을 해석하지 못했습니다: {exc}",
                identifier="invalid-proposed-expression",
            )
            return None
        log.receipt(
            action=ReceiptAction.REWROTE_EXPRESSION,
            input_path="$.expression.payload",
            output_path="$.expression",
            reason="제안된 사건 대수를 검증된 실행 표현으로 변환했다",
            actor=ReceiptActor.APPLICATION,
        )
        for validator in self._validators:
            log.issues.extend(
                validator.validate(
                    expression,
                    query=requirement.source.text,
                    literals=context.literals,
                )
            )
        subject = self._subject_entity(requirement, context)
        output = self._build_output(requirement, context, [], subject, log)
        if output is None:
            return None
        return expression, output

    def _subject_entity(
        self, requirement: AudienceRequirement, context: RequirementResolutionContext
    ) -> str:
        target = requirement.intent.target
        if target is not None:
            resolved = context.schema_registry.resolve_entity(target.name)
            if resolved:
                return resolved
            return target.name
        return "subject"

    # ── 갈래 2: 제약 목록 ──────────────────────────────────────────────────────

    def _resolve_constraints(
        self,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
    ) -> tuple[EventExpression, QueryOutput] | None:
        issue_index = {issue.path: issue for issue in requirement.issues}
        predicates: list[EventExpression] = []
        aggregate_metrics: list[MetricResolution] = []
        primary_entity: str | None = None

        for index, constraint in enumerate(requirement.constraints):
            path = f"$.constraints[{index}].value"
            metric = context.schema_registry.resolve_metric(constraint.field.name)
            if metric is not None:
                node = self._aggregate_condition(
                    constraint, metric, path, requirement, context, log, issue_index
                )
                if node is not None:
                    predicates.append(node)
                    aggregate_metrics.append(metric)
                    primary_entity = primary_entity or metric.entity
                continue
            field = self._resolve_field(constraint.field, context)
            if field is None:
                log.issue(
                    kind=IssueKind.SCHEMA_MISMATCH,
                    path=f"$.constraints[{index}].field",
                    message=(
                        f"카탈로그에 없는 필드입니다: {constraint.field.logical_name}"
                    ),
                    identifier=f"unknown-field:{constraint.field.logical_name}",
                )
                continue
            primary_entity = primary_entity or field.entity
            log.receipt(
                action=ReceiptAction.RESOLVED_REFERENCE,
                input_path=f"$.constraints[{index}].field",
                output_path=f"{field.entity}.{field.attribute}",
                reason="논리 필드 참조를 카탈로그 entity/attribute 로 확정했다",
                actor=ReceiptActor.APPLICATION,
            )
            node = self._row_condition(
                constraint, field, path, requirement, context, log, issue_index
            )
            if node is not None:
                predicates.append(node)

        if log.issues or not predicates:
            if not predicates and not log.issues:
                log.issue(
                    kind=IssueKind.MISSING,
                    path="$.constraints",
                    message="실행 가능한 제약이 하나도 없습니다.",
                    identifier="empty-constraints",
                )
            return None

        expression: EventExpression = (
            predicates[0] if len(predicates) == 1 else AndExpression(expressions=tuple(predicates))
        )
        output = self._build_output(requirement, context, aggregate_metrics, primary_entity, log)
        if output is None:
            return None
        return expression, output

    def _resolve_field(
        self, reference: RequirementReference, context: RequirementResolutionContext
    ) -> FieldResolution | None:
        registry = context.schema_registry
        if reference.namespace:
            entity = registry.resolve_entity(reference.namespace)
            if entity is None:
                return None
            attribute = registry.resolve_attribute(entity, reference.name)
            if attribute is None:
                return None
            return FieldResolution(entity=entity, attribute=attribute)
        return registry.resolve_field(reference.name)

    def _row_condition(
        self,
        constraint: RequirementConstraint,
        field: FieldResolution,
        path: str,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
        issue_index: Mapping[str, RequirementIssue],
    ) -> EventExpression | None:
        attribute = AttributeOperand(entity=field.entity, attribute=field.attribute)
        value = self._confirm_value(constraint, path, requirement, context, log, issue_index)
        if value is None:
            return None
        if isinstance(value, (AbsoluteWindow, RollingWindow, RelativeWindow)):
            return TimeWindowExpression(attribute=attribute, window=value)
        return ComparisonExpression(
            operator=_COMPARISON_BY_REQUIREMENT[constraint.operator],
            left=attribute,
            right=LiteralOperand(value=value),
        )

    def _aggregate_condition(
        self,
        constraint: RequirementConstraint,
        metric: MetricResolution,
        path: str,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
        issue_index: Mapping[str, RequirementIssue],
    ) -> EventExpression | None:
        value = self._confirm_value(constraint, path, requirement, context, log, issue_index)
        if value is None:
            return None
        if isinstance(value, (AbsoluteWindow, RollingWindow, RelativeWindow)):
            log.issue(
                kind=IssueKind.INVALID,
                path=path,
                message="집계 임계값에 기간 표현을 쓸 수 없습니다.",
                identifier=f"aggregate-window:{metric.logical_name}",
            )
            return None
        target = (
            AttributeOperand(entity=metric.entity, attribute=metric.attribute)
            if metric.attribute
            else None
        )
        log.receipt(
            action=ReceiptAction.RESOLVED_REFERENCE,
            input_path=f"$.constraints.{constraint.id}.field",
            output_path=metric.logical_name,
            reason=(
                f"지표 {metric.logical_name} 를 "
                f"{metric.function.value} 집계로 확정했다"
            ),
            actor=ReceiptActor.APPLICATION,
        )
        return AggregateConditionExpression(
            aggregate=AggregateDefinition(
                function=metric.function,
                target=target,
                relation=EntityRelation(entity=metric.entity),
            ),
            operator=_COMPARISON_BY_REQUIREMENT[constraint.operator],
            value=LiteralOperand(value=value),
        )

    # ── 값 확정(상태별 규칙) ───────────────────────────────────────────────────

    def _confirm_value(
        self,
        constraint: RequirementConstraint,
        path: str,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
        issue_index: Mapping[str, RequirementIssue],
    ) -> LiteralValue | TimeWindow | None:
        value: RequirementValue = constraint.value
        if isinstance(value, ResolvedRequirementValue):
            return self._normalize(value.value, constraint, path, context, log)
        if isinstance(value, InferredRequirementValue):
            normalized = self._normalize(value.value, constraint, path, context, log)
            if normalized is None:
                return None
            log.assume(
                path=path,
                description="사용자가 명시하지 않아 원문 표현에서 추론한 값",
                value=value.value,
                confidence=value.confidence,
            )
            return normalized
        if isinstance(value, AmbiguousRequirementValue):
            return self._resolve_ambiguous(
                value, constraint, path, log, issue_index
            )
        if isinstance(value, MissingRequirementValue):
            return self._resolve_missing(
                value, constraint, path, requirement, context, log
            )
        return None  # pragma: no cover - discriminated union 은 위 네 갈래가 전부다

    def _normalize(
        self,
        raw: LiteralValue,
        constraint: RequirementConstraint,
        path: str,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
    ) -> LiteralValue | TimeWindow | None:
        # 기간 해석은 **기간 자리에서만** 한다. 표면 문법 소유자는 문장을 훑는 스캐너라, 아무
        # 문자열에나 걸면 '2025년 신년 프로모션' 같은 값이 조용히 시간 창이 된다. 그리고 범위가
        # 아닌 연산자에는 창을 실을 자리가 없다 — 어느 경계를 쓸지 추측해야 하기 때문이다.
        if not isinstance(raw, str) or constraint.operator is not RequirementOperator.BETWEEN:
            return raw
        period = resolve_period_phrase(
            raw, now=self._clock.now(), timezone=context.timezone
        )
        if period is None:
            log.issue(
                kind=IssueKind.AMBIGUOUS,
                path=path,
                message=f"기간 표현을 달력 구간으로 확정하지 못했습니다: {raw!r}",
                identifier=f"unresolved-period:{constraint.id}",
            )
            return None
        log.receipt(
            action=ReceiptAction.NORMALIZED_VALUE,
            input_path=path,
            reason=(
                f"'{period.phrase}' 를 {context.timezone} 달력으로 "
                f"{_window_description(period.window)} 로 확정했다"
            ),
            actor=ReceiptActor.APPLICATION,
            before=raw,
            after=_window_label(period.window),
        )
        return period.window

    def _resolve_ambiguous(
        self,
        value: AmbiguousRequirementValue,
        constraint: RequirementConstraint,
        path: str,
        log: _ResolutionLog,
        issue_index: Mapping[str, RequirementIssue],
    ) -> LiteralValue | None:
        issue = issue_index.get(path)
        resolution = issue.resolution if issue is not None else None
        if resolution is not None and resolution.default_value is not None:
            action: ReceiptAction | None = None
            reason = ""
            if resolution.kind is ResolutionKind.SELECT_CANDIDATE:
                if resolution.default_value not in value.candidates:
                    log.issue(
                        kind=IssueKind.AMBIGUOUS,
                        path=path,
                        message="선택된 후보가 후보 목록에 없습니다.",
                        candidates=value.candidates,
                        identifier=f"invalid-candidate:{constraint.id}",
                    )
                    return None
                action = ReceiptAction.SELECTED_CANDIDATE
                reason = "모호한 값을 선언된 후보 중 하나로 확정했다"
            elif resolution.kind is ResolutionKind.USE_DEFAULT:
                action = ReceiptAction.APPLIED_DEFAULT
                reason = "모호한 값에 선언된 기본값을 적용했다"
            if action is not None:
                log.receipt(
                    action=action,
                    input_path=path,
                    reason=reason,
                    actor=ReceiptActor.POLICY,
                    after=resolution.default_value,
                    policy_id=issue.id if issue is not None else None,
                )
                log.assume(
                    path=path,
                    description=reason,
                    value=resolution.default_value,
                )
                return resolution.default_value
        log.issue(
            kind=IssueKind.AMBIGUOUS,
            path=path,
            message="값이 하나로 확정되지 않았습니다.",
            candidates=value.candidates,
            identifier=f"ambiguous:{constraint.id}",
        )
        return None

    def _resolve_missing(
        self,
        value: MissingRequirementValue,
        constraint: RequirementConstraint,
        path: str,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
    ) -> LiteralValue | None:
        default = (
            context.policy.default_for(path, requirement)
            if context.policy is not None
            else None
        )
        if default is None:
            log.issue(
                kind=IssueKind.MISSING,
                path=path,
                message=f"값이 필요합니다({value.expected_type}).",
                identifier=f"missing:{constraint.id}",
            )
            return None
        log.receipt(
            action=ReceiptAction.APPLIED_DEFAULT,
            input_path=path,
            reason="조직 policy 기본값을 적용했다",
            actor=ReceiptActor.POLICY,
            after=default,
        )
        log.assume(path=path, description="policy 기본값", value=default)
        return default

    # ── 출력/바인딩 ────────────────────────────────────────────────────────────

    def _build_output(
        self,
        requirement: AudienceRequirement,
        context: RequirementResolutionContext,
        aggregate_metrics: list[MetricResolution],
        primary_entity: str | None,
        log: _ResolutionLog,
    ) -> QueryOutput | None:
        registry = context.schema_registry
        entity = primary_entity or self._subject_entity(requirement, context)
        dimensions: list[AttributeOperand] = []
        measures: list[ResolvedMeasure] = []
        order_by: list[ResolvedSort] = []
        limit: int | None = None

        declared = requirement.output
        if declared is not None:
            for reference in declared.dimensions:
                field = self._resolve_field(reference, context)
                if field is None:
                    log.issue(
                        kind=IssueKind.SCHEMA_MISMATCH,
                        path="$.output.dimensions",
                        message=f"카탈로그에 없는 필드입니다: {reference.logical_name}",
                        identifier=f"unknown-dimension:{reference.logical_name}",
                    )
                    continue
                dimensions.append(
                    AttributeOperand(entity=field.entity, attribute=field.attribute)
                )
            for metric in declared.metrics:
                function = _AGGREGATE_BY_METRIC_KIND.get(metric.kind)
                if function is None:
                    log.issue(
                        kind=IssueKind.UNSUPPORTED,
                        path="$.output.metrics",
                        message=(
                            f"현재 실행 계층이 지원하지 않는 집계입니다: {metric.kind.value}"
                        ),
                        identifier=f"unsupported-metric:{metric.kind.value}",
                    )
                    continue
                target: AttributeOperand | None = None
                if metric.target is not None:
                    field = self._resolve_field(metric.target, context)
                    if field is None:
                        log.issue(
                            kind=IssueKind.SCHEMA_MISMATCH,
                            path="$.output.metrics",
                            message=(
                                f"카탈로그에 없는 필드입니다: {metric.target.logical_name}"
                            ),
                            identifier=f"unknown-measure:{metric.target.logical_name}",
                        )
                        continue
                    target = AttributeOperand(
                        entity=field.entity, attribute=field.attribute
                    )
                measures.append(
                    ResolvedMeasure(
                        alias=metric.alias
                        or (
                            f"{metric.kind.value}_{target.attribute}"
                            if target is not None
                            else f"{metric.kind.value}_all"
                        ),
                        function=function,
                        target=target,
                    )
                )
            for order in declared.order_by:
                order_by.append(
                    ResolvedSort(
                        name=order.target.name,
                        descending=order.direction is SortDirection.DESC,
                    )
                )
            limit = declared.limit

        # 집계 조건(HAVING)이 있는데 출력 선언이 없으면 지표 자신이 grain 과 measure 를
        # 알고 있다 — 여기서 되묻는 것은 시스템이 이미 아는 것을 묻는 일이다.
        for resolved_metric in aggregate_metrics:
            if resolved_metric.grain and not dimensions:
                attribute = (
                    registry.resolve_attribute(
                        resolved_metric.entity, resolved_metric.grain
                    )
                    or resolved_metric.grain
                )
                dimensions.append(
                    AttributeOperand(entity=resolved_metric.entity, attribute=attribute)
                )
            if not any(
                measure.alias == resolved_metric.logical_name for measure in measures
            ):
                measures.append(
                    ResolvedMeasure(
                        alias=resolved_metric.logical_name,
                        function=resolved_metric.function,
                        target=(
                            AttributeOperand(
                                entity=resolved_metric.entity,
                                attribute=resolved_metric.attribute,
                            )
                            if resolved_metric.attribute
                            else None
                        ),
                    )
                )

        if log.issues:
            return None
        return QueryOutput(
            entity=entity,
            dimensions=tuple(dimensions),
            measures=tuple(measures),
            order_by=tuple(order_by),
            limit=limit,
        )

    def _bindings(
        self,
        expression: EventExpression,
        context: RequirementResolutionContext,
        log: _ResolutionLog,
    ) -> dict[str, ResolvedBinding]:
        bindings: dict[str, ResolvedBinding] = {}
        for operand in attribute_operands(expression):
            bindings[operand.logical_name] = ResolvedBinding(
                logical_name=operand.logical_name,
                entity=operand.entity,
                attribute=operand.attribute,
            )
        return bindings


_COMPARISON_BY_REQUIREMENT: dict[RequirementOperator, ComparisonOperator] = {
    RequirementOperator.EQ: ComparisonOperator.EQ,
    RequirementOperator.NEQ: ComparisonOperator.NEQ,
    RequirementOperator.GT: ComparisonOperator.GT,
    RequirementOperator.GTE: ComparisonOperator.GTE,
    RequirementOperator.LT: ComparisonOperator.LT,
    RequirementOperator.LTE: ComparisonOperator.LTE,
    RequirementOperator.IN: ComparisonOperator.IN,
    RequirementOperator.BETWEEN: ComparisonOperator.BETWEEN,
    RequirementOperator.CONTAINS: ComparisonOperator.CONTAINS,
}

# 요구 어휘 → 실행 어휘. ``median`` 이 여기 없다는 것이 곧 "말할 수는 있지만 실행할 수
# 없다"의 구조적 표현이다.
_AGGREGATE_BY_METRIC_KIND: dict[MetricKind, AggregateFunction] = {
    MetricKind.COUNT: AggregateFunction.COUNT,
    MetricKind.SUM: AggregateFunction.SUM,
    MetricKind.AVG: AggregateFunction.AVG,
    MetricKind.MIN: AggregateFunction.MIN,
    MetricKind.MAX: AggregateFunction.MAX,
}


__all__ = [
    "SPEC_SCHEMA_VERSION",
    "CapabilityProfile",
    "DefaultRequirementResolver",
    "FieldResolution",
    "MetricResolution",
    "PeriodResolution",
    "ReadyResolution",
    "RequirementResolutionContext",
    "RequirementResolutionResult",
    "RequirementResolver",
    "ResolutionPolicy",
    "SchemaRegistry",
    "StaticCapabilityProfile",
    "StaticResolutionPolicy",
    "StaticSchemaRegistry",
    "UnresolvedResolution",
    "required_capabilities",
    "resolve_period_phrase",
]
