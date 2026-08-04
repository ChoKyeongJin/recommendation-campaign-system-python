"""SQL 생성 안전 게이트 — 검증된 실행 가능 조건만 query_plan 에 도달하게 하는 마지막 관문.

요청 17번의 타입 강제 구현이다:

  resolved dict(오케스트레이터 출력)
      → ensure_executable() : status=normalized + catalog_match + execution_support +
                              compiler 등록 + source span 실재를 전부 재검증
      → ExecutableCondition(불변 타입 — 이 타입의 존재 자체가 검증 통과의 증명)
      → build_validated_plan_fragment() : 등록된 컴파일러의 SlotEmission 만으로 plan 조각 조립
      → ValidatedQueryModel

만들어진 plan 조각은 targeting_ir.SLOT_SHAPES coerce 를 통과한 값만 담으므로, graph_rag 의
기존 SQL 파이프라인(compile_executable_plan → 빌더 레지스트리 → sql_guard/AST 검증)이 그대로
소비할 수 있다 — SQL 생성기가 새 표면을 얻는 게 아니라 이미 검증된 입구만 쓴다.

candidate / needs_review / unresolved / invalid / unsupported 상태는 예외로 차단된다.
LLM 이 만든 임의 컬럼·테이블명이 이 경로로 들어올 자리는 없다: ExecutableCondition 은 슬롯
값만 나르고, 물리 스키마는 컴파일러·설정 레지스트리가 소유한다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import compiler_registry as _compiler_registry
from compiler_registry import CompileContext, CompilerRegistry, SlotEmission
from normalization_result import SQL_ALLOWED_STATUSES, STATUS_NORMALIZED


class ExecutionGateError(ValueError):
    """실행 게이트 위반 — 정규화되지 않은(또는 실행 불가) 조건으로 SQL 을 만들려는 시도."""


@dataclass(frozen=True)
class ExecutableCondition:
    """검증 통과가 타입으로 증명된 조건(요청 17번 ExecutableCondition).

    생성은 :func:`ensure_executable` 로만 한다 — 직접 생성해도 __post_init__ 이 같은 검증을
    다시 돌리므로 우회할 수 없다."""

    concept: str
    kind: str
    compiler: str
    normalized: Mapping[str, Any]
    status: str
    catalog_match: bool
    execution_support: bool
    source: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.status not in SQL_ALLOWED_STATUSES:
            raise ExecutionGateError(f"status={self.status!r} 조건은 실행할 수 없다({sorted(SQL_ALLOWED_STATUSES)} 만 허용).")
        if not self.catalog_match:
            raise ExecutionGateError(f"{self.concept}: catalog_match=False — 카탈로그 미접지 조건은 실행 불가.")
        if not self.execution_support:
            raise ExecutionGateError(f"{self.concept}: execution_support=False — 컴파일 지원 없는 조건은 실행 불가.")
        if not (isinstance(self.concept, str) and self.concept):
            raise ExecutionGateError("concept 가 비어 있다.")
        if not (isinstance(self.compiler, str) and self.compiler):
            raise ExecutionGateError(f"{self.concept}: compiler 미지정 — 등록된 컴파일러 없이 실행 불가.")
        if not (isinstance(self.normalized, Mapping) and self.normalized):
            raise ExecutionGateError(f"{self.concept}: normalized 값이 비어 있다.")
        span = self.source.get("span") if isinstance(self.source, Mapping) else None
        if not (isinstance(span, Mapping) and isinstance(span.get("start"), int) and isinstance(span.get("end"), int)):
            raise ExecutionGateError(f"{self.concept}: source span 없는 조건은 실행 불가(요청 15·17번).")

    def as_condition_payload(self) -> dict[str, Any]:
        """컴파일러 입력 계약(status 포함 매핑)."""
        return {"concept": self.concept, "slot": self.concept, "kind": self.kind,
                "normalized": dict(self.normalized), "status": self.status}


def ensure_executable(resolved: Mapping[str, Any]) -> ExecutableCondition:
    """오케스트레이터 출력 하나를 실행 가능 조건으로 승격한다. 위반은 예외(조용한 통과 없음)."""
    if not isinstance(resolved, Mapping):
        raise ExecutionGateError(f"조건은 매핑이어야 한다(받은 타입 {type(resolved).__name__}).")
    status = resolved.get("status")
    if status != STATUS_NORMALIZED:
        raise ExecutionGateError(
            f"status={status!r} 조건은 SQL 생성 단계로 진행할 수 없다"
            f"(reason={resolved.get('reason')!r}).")
    if resolved.get("sql_generation_allowed") is False:
        raise ExecutionGateError(f"{resolved.get('concept')!r}: sql_generation_allowed=False.")
    return ExecutableCondition(
        concept=str(resolved.get("concept") or ""),
        kind=str(resolved.get("kind") or ""),
        compiler=str(resolved.get("compiler") or ""),
        normalized=resolved.get("normalized") or {},
        status=str(status),
        catalog_match=bool(resolved.get("catalog_match")),
        execution_support=bool(resolved.get("execution_support")),
        source=resolved.get("source") or {},
    )


@dataclass(frozen=True)
class ValidatedQueryModel:
    """실행 가능 조건들 + 그 컴파일 결과 plan 조각(요청 17번 ValidatedQueryModel).

    SQL 생성기는 이 모델(정확히는 plan 조각을 병합한 query_plan)만 받는다."""

    conditions: tuple[ExecutableCondition, ...]
    plan_fragment: Mapping[str, Any]


def build_validated_plan_fragment(
    resolved_conditions: Sequence[Mapping[str, Any]],
    *,
    registry: CompilerRegistry | None = None,
    context: CompileContext | None = None,
) -> ValidatedQueryModel:
    """실행 가능 조건들만으로 query_plan 조각을 만든다(요청 18번 ValidatedQueryBuilder).

    한 조건이라도 게이트를 통과하지 못하면 전체가 실패한다 — 일부만 조용히 빠진 plan 은
    '조건이 사라진 SQL' 이 되므로 fail-close 가 맞다. 부분 수용이 필요하면 호출자가
    ensure_executable 로 미리 걸러 넘긴다."""
    registry = registry if registry is not None else _compiler_registry.default_registry()
    context = context if context is not None else CompileContext()
    conditions = tuple(ensure_executable(resolved) for resolved in resolved_conditions)

    fragment: dict[str, dict[str, Any]] = {}
    for condition in conditions:
        if not registry.has(condition.compiler):
            raise ExecutionGateError(f"{condition.concept}: 컴파일러 {condition.compiler!r} 미등록.")
        try:
            emission: SlotEmission = registry.get(condition.compiler).compile(
                condition.as_condition_payload(), context
            )
        except _compiler_registry.CompilerError as exc:
            raise ExecutionGateError(
                f"{condition.concept}: 실행 슬롯 컴파일에 실패했습니다: {exc}"
            ) from exc
        holder = fragment.setdefault(emission.container, {})
        if emission.mode == "extend":
            existing = holder.get(emission.slot)
            items = list(existing) if isinstance(existing, list) else []
            items.extend(emission.value if isinstance(emission.value, list) else [emission.value])
            holder[emission.slot] = items
        else:
            if emission.slot in holder:
                raise ExecutionGateError(
                    f"슬롯 충돌: {emission.container}.{emission.slot} 에 두 조건이 대입을 시도했다.")
            holder[emission.slot] = emission.value
    return ValidatedQueryModel(conditions=conditions, plan_fragment=fragment)


__all__ = [
    "ExecutableCondition",
    "ExecutionGateError",
    "ValidatedQueryModel",
    "build_validated_plan_fragment",
    "ensure_executable",
]
