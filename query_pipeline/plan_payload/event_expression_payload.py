"""``plan["event_expression"]`` 페이로드 → 실행 사양 어댑터.

이 모듈의 전신 이름은 ``compatibility/legacy_event_expression.py`` 였고, docstring 은 자신을
"이행 3단계에서 사라질 deprecated 다리"로 소개했다. 그 서술은 **틀렸다** — legacy 오디언스 레인이
폐쇄되면서 ``plan["event_expression"]`` 은 폐기 대상 슬롯이 아니라 오디언스 의미의 **유일한
직렬화 표기**가 됐다. 이름이 남기던 오해(“legacy 페이로드를 읽는 임시 코드”)를 없애려고
패키지·모듈·타입 이름을 함께 바꿨다. 동작은 바뀌지 않았다.

방향은 여전히 **한 방향뿐이다**: 저장 페이로드 → 사양. 사양 → 페이로드 역투영은 만들지 않는다
(그 역방향이 곧 두 번째 실행 모델이다).

계층 위치: 이 패키지는 ``requirement`` 와 ``event_query`` 를 **둘 다** 안다. 그래서 네 계층
(requirement → event_query → planning → compiler) 중 하나가 아니라 그 위의 조립 계층이며,
:mod:`tests.test_query_pipeline_layering` 이 그 예외를 사유와 함께 선언한다.
"""

from __future__ import annotations

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

# 저장된 payload 가 canonical 생산자에서 나왔음을 나타내는 표식.
# `audience_authority.CANONICAL_EVENT_EXPRESSION_SOURCES` 와 값이 같아야 한다
# (`tests/test_no_semantic_plan_residue.py` 가 두 사본의 일치를 강제한다).
CANONICAL_SOURCES: frozenset[str] = frozenset({"audience_requirement", "semantic_plan"})


class PayloadAdapterError(ValueError):
    """저장된 payload 를 사양으로 옮길 수 없다. 실패 단계를 이름으로 드러낸다."""

    stage = "event_expression_payload_adapter"


class EventExpressionPayload(StrictModel):
    """저장된 슬롯의 타입 있는 표기.

    ``extra="ignore"`` 인 이유: 이 payload 는 **우리가 닫을 대상이 아니다**. 생산자가 자기 키를
    얹을 수 있고, 그 키를 여기서 거부하면 저장된 플랜을 읽지 못한다. 새로 만드는 모델은 전부
    ``extra="forbid"`` 다.
    """

    model_config = ConfigDict(extra="ignore")

    expression: JsonValue | None = None
    source: str | None = None
    receipts: tuple[JsonValue, ...] = ()

    @property
    def is_canonical(self) -> bool:
        return self.source in CANONICAL_SOURCES


class PayloadSpecConversion(StrictModel):
    status: Literal["converted"] = "converted"
    spec: EventQuerySpec


class PayloadConversionFailed(StrictModel):
    status: Literal["failed"] = "failed"
    stage: str
    message: str


PayloadConversionResult: TypeAlias = Annotated[
    PayloadSpecConversion | PayloadConversionFailed,
    Field(discriminator="status"),
]


class EventExpressionPayloadAdapter:
    """저장된 ``event_expression`` → :class:`EventQuerySpec`."""

    @staticmethod
    def to_event_query_spec(
        payload_value: object,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        subject_entity: str = "subject",
        requirement_id: str = "plan-event-expression",
    ) -> EventQuerySpec:
        """변환하거나 :class:`PayloadAdapterError` 로 멈춘다(축소 폴백 없음)."""
        active_clock = clock or SystemClock()
        factory = id_factory or UuidIdFactory()

        try:
            payload = EventExpressionPayload.model_validate(payload_value)
        except ValueError as exc:
            raise PayloadAdapterError(f"저장된 payload 형태가 아닙니다: {exc}") from exc
        if payload.expression is None:
            raise PayloadAdapterError("저장된 payload 에 표현(expression)이 없습니다")

        try:
            expression = canonicalize_expression(from_wire(payload.expression))
        except (ExpressionBridgeError, ValueError, TypeError) as exc:
            raise PayloadAdapterError(
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
                f" payload_receipts={len(payload.receipts)})"
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
            raise PayloadAdapterError(str(exc)) from exc

    @staticmethod
    def convert(
        payload_value: object,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        subject_entity: str = "subject",
    ) -> PayloadConversionResult:
        """예외 대신 구조화된 결과를 원하는 호출자용(§10 계약)."""
        try:
            spec = EventExpressionPayloadAdapter.to_event_query_spec(
                payload_value,
                clock=clock,
                id_factory=id_factory,
                subject_entity=subject_entity,
            )
        except PayloadAdapterError as exc:
            return PayloadConversionFailed(stage=exc.stage, message=str(exc))
        return PayloadSpecConversion(spec=spec)


def to_unsupported_payload(reason: str, message: str) -> dict[str, Any]:
    """실패를 ``query_plan['unsupported']`` 모양으로 옮긴다(응답 계약 유지)."""
    return {"reason": reason, "message": message, "clarification": message}


__all__ = [
    "CANONICAL_SOURCES",
    "EventExpressionPayload",
    "EventExpressionPayloadAdapter",
    "PayloadAdapterError",
    "PayloadConversionFailed",
    "PayloadConversionResult",
    "PayloadSpecConversion",
    "to_unsupported_payload",
]
