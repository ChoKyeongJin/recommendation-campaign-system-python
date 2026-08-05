"""호환 어댑터 — 저장된 ``plan["event_expression"]`` 을 새 계층으로 잇는다.

**제거 조건(deprecated):** 이 모듈은 3단계(``event_expression`` 슬롯 제거)에서 사라진다.
지금 필요한 이유는 저장된 플랜과 이행 도구(``audience_cutover`` / ``audience_shadow`` /
``tools/migrate_legacy_audience.py``)가 그 슬롯을 계속 읽고 쓰기 때문이다. 다음 두 조건이
동시에 만족되면 삭제한다:

1. ``plan["event_expression"]`` 의 생산자가 남지 않는다(구조화기·lowering·cutover 전부가
   :class:`~query_pipeline.event_query.models.EventQuerySpec` 를 직접 저장한다).
2. 저장된 플랜 재생 경로(shadow/rollback)가 사양 스냅샷으로 재생된다.

그때까지 이 어댑터는 **한 방향으로만** 쓴다: legacy payload → 사양. 사양 → legacy 슬롯
역투영은 만들지 않는다(그 역방향이 곧 두 번째 실행 모델이다).
"""

from __future__ import annotations

import warnings
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import ConfigDict, Field, JsonValue

from query_pipeline.base import (
    Clock,
    IdFactory,
    StrictModel,
    SystemClock,
    UuidIdFactory,
)
from query_pipeline.event_query.event_ir_bridge import (
    ExpressionBridgeError,
    from_wire,
)
from query_pipeline.event_query.expressions import (
    attribute_operands,
    canonicalize_expression,
)
from query_pipeline.event_query.models import (
    CapabilityRequirements,
    EventQuerySpec,
    EventQuerySpecError,
    QueryOutput,
    QuerySource,
    ResolvedBinding,
)
from query_pipeline.event_query.receipts import (
    ReceiptAction,
    ReceiptActor,
    ResolutionReceipt,
)
from query_pipeline.requirement.resolver import (
    SPEC_SCHEMA_VERSION,
    required_capabilities,
)

# 저장된 payload 가 canonical 생산자에서 나왔음을 나타내는 표식(기존 계약 그대로).
# ``semantic_plan`` 은 생산자가 폐기된 뒤에도 **저장 페이로드 호환**으로 남는다.
# `audience_authority.CANONICAL_EVENT_EXPRESSION_SOURCES` 와 값이 같아야 한다.
CANONICAL_SOURCES: frozenset[str] = frozenset({"audience_requirement", "semantic_plan"})

DEPRECATION_MESSAGE = (
    "plan['event_expression'] 는 deprecated 입니다. EventQuerySpec 를 직접 쓰십시오"
    " (제거 조건은 query_pipeline.compatibility.legacy_event_expression 문서 참조)."
)


class LegacyAdapterError(ValueError):
    """저장된 payload 를 사양으로 옮길 수 없다. 실패 단계를 이름으로 드러낸다."""

    stage = "legacy_event_expression_adapter"


class LegacyEventExpressionPayload(StrictModel):
    """저장된 슬롯의 타입 있는 표기.

    ``extra="ignore"`` 인 이유: 이 payload 는 **우리가 닫을 대상이 아니다**. 이행 도구가
    binding fingerprint 같은 자기 키를 얹고, 그 키를 여기서 거부하면 저장된 플랜을 읽지
    못한다. 새로 만드는 모델은 전부 ``extra="forbid"`` 다.
    """

    model_config = ConfigDict(extra="ignore")

    expression: JsonValue | None = None
    source: str | None = None
    receipts: tuple[JsonValue, ...] = ()

    @property
    def is_canonical(self) -> bool:
        return self.source in CANONICAL_SOURCES


class LegacySpecConversion(StrictModel):
    status: Literal["converted"] = "converted"
    spec: EventQuerySpec


class LegacyConversionFailed(StrictModel):
    status: Literal["failed"] = "failed"
    stage: str
    message: str


LegacyConversionResult: TypeAlias = Annotated[
    LegacySpecConversion | LegacyConversionFailed,
    Field(discriminator="status"),
]


class LegacyEventExpressionAdapter:
    """저장된 ``event_expression`` → :class:`EventQuerySpec`."""

    @staticmethod
    def to_event_query_spec(
        legacy_expression: object,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        subject_entity: str = "subject",
        requirement_id: str = "legacy-event-expression",
        warn: bool = True,
    ) -> EventQuerySpec:
        """변환하거나 :class:`LegacyAdapterError` 로 멈춘다(축소 폴백 없음)."""
        if warn:
            warnings.warn(DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)
        active_clock = clock or SystemClock()
        factory = id_factory or UuidIdFactory()

        try:
            payload = LegacyEventExpressionPayload.model_validate(legacy_expression)
        except ValueError as exc:
            raise LegacyAdapterError(f"저장된 payload 형태가 아닙니다: {exc}") from exc
        if payload.expression is None:
            raise LegacyAdapterError("저장된 payload 에 표현(expression)이 없습니다")

        try:
            expression = canonicalize_expression(from_wire(payload.expression))
        except (ExpressionBridgeError, ValueError, TypeError) as exc:
            raise LegacyAdapterError(
                f"저장된 사건 IR 을 실행 표현으로 옮기지 못했습니다: {exc}"
            ) from exc

        bindings = {
            operand.logical_name: ResolvedBinding(
                logical_name=operand.logical_name,
                entity=operand.entity,
                attribute=operand.attribute,
            )
            for operand in attribute_operands(expression)
        }
        receipt = ResolutionReceipt(
            id=factory.new_id("receipt"),
            action=ReceiptAction.REWROTE_EXPRESSION,
            input_path="plan.event_expression.expression",
            output_path="spec.expression",
            reason=(
                "저장된 canonical 표현을 사양으로 옮겼다"
                f" (source={payload.source or 'unknown'},"
                f" legacy_receipts={len(payload.receipts)})"
            ),
            actor=ReceiptActor.APPLICATION,
            schema_version=SPEC_SCHEMA_VERSION,
            timestamp=active_clock.now(),
        )
        try:
            return EventQuerySpec(
                id=factory.new_id("spec"),
                version=SPEC_SCHEMA_VERSION,
                expression=expression,
                output=QueryOutput(entity=subject_entity),
                bindings=bindings,
                receipts=(receipt,),
                source=QuerySource(
                    requirement_id=requirement_id,
                    requirement_version=SPEC_SCHEMA_VERSION,
                ),
                capabilities=CapabilityRequirements(
                    required=required_capabilities(expression)
                ),
                created_at=active_clock.now(),
            )
        except EventQuerySpecError as exc:
            raise LegacyAdapterError(str(exc)) from exc

    @staticmethod
    def convert(
        legacy_expression: object,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        subject_entity: str = "subject",
    ) -> LegacyConversionResult:
        """예외 대신 구조화된 결과를 원하는 호출자용(§10 계약)."""
        try:
            spec = LegacyEventExpressionAdapter.to_event_query_spec(
                legacy_expression,
                clock=clock,
                id_factory=id_factory,
                subject_entity=subject_entity,
                warn=False,
            )
        except LegacyAdapterError as exc:
            return LegacyConversionFailed(stage=exc.stage, message=str(exc))
        return LegacySpecConversion(spec=spec)


def to_legacy_unsupported(reason: str, message: str) -> dict[str, Any]:
    """새 실패를 기존 ``query_plan['unsupported']`` 모양으로 옮긴다(응답 계약 유지)."""
    return {"reason": reason, "message": message, "clarification": message}


__all__ = [
    "CANONICAL_SOURCES",
    "DEPRECATION_MESSAGE",
    "LegacyAdapterError",
    "LegacyConversionFailed",
    "LegacyConversionResult",
    "LegacyEventExpressionAdapter",
    "LegacyEventExpressionPayload",
    "LegacySpecConversion",
    "to_legacy_unsupported",
]
