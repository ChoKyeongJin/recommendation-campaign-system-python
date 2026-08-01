"""조건 컴파일러 레지스트리 — "이 개념을 SQL 로 바꿀 수 있는가"의 명시적 단일 소유자.

두 계층 관계: :mod:`compiler_strategies` 는 qualifier(브랜드/상품/카테고리) 필터 하나를 WHERE 조각
(CompiledFilter)으로 만드는 **전략 dispatch** 다. 이 모듈은 그 위 계층 — 정규화된 **조건 전체**를
검증된 query_plan 슬롯 값으로 방출(emit)하는 조건 컴파일러의 레지스트리다. SQL 문자열은 여기서
만들지 않는다: 방출된 슬롯 값은 targeting_ir.SLOT_SHAPES 의 coerce(LLM 경로와 동일한 닫힌 어휘
검증)를 반드시 통과하고, 실제 SQL 은 graph_rag 의 기존 빌더 레지스트리(_sql_target_builder_registry)
가 그 슬롯을 읽어 만든다. 즉 새 계층이 SQL 표면을 새로 열지 않는다 — 이미 검증된 경로만 재사용한다.

카탈로그(concept_catalog)의 ``compiler`` 필드는 여기 등록된 이름만 가리킬 수 있다. 이름이 선언됐는데
레지스트리에 없으면 카탈로그가 is_executable=False 를 돌려주고 정규화 결과는
compiler_not_registered(unresolved) 로 끝난다 — LLM 이 컴파일러 이름을 지어내도 실행되지 않는다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다(targeting_ir/compiler_strategies 만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import compiler_strategies
from targeting_ir import SLOT_SHAPES


class CompilerError(ValueError):
    """컴파일 계약 위반(미등록 컴파일러·coerce 거부·비정규화 입력). 조용한 드롭 대신 즉시 실패."""


@dataclass(frozen=True)
class SlotEmission:
    """컴파일 결과 — query_plan 의 어느 컨테이너·슬롯에 어떤 검증된 값을 넣는가.

    mode="set" 은 슬롯 대입, "extend" 는 리스트 슬롯에 항목 추가(aggregate_conditions 처럼
    여러 조건이 한 슬롯을 공유할 때)."""

    container: str  # "target_user" | "plan"
    slot: str
    value: Any
    mode: str = "set"

    def __post_init__(self) -> None:
        if self.mode not in ("set", "extend"):
            raise CompilerError(f"미지원 emission mode: {self.mode!r}")


@dataclass(frozen=True)
class CompileContext:
    """컴파일 시점 주입 어휘. graph_rag._llm_slot_allowed() 와 같은 shape 의 매핑을 받는다
    (키: aggregate_metrics/profile_metrics/… — SlotShape.allowed_key 와 동일한 이름 공간)."""

    allowed: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConditionCompiler:
    """조건 컴파일러 하나. ``emit`` 은 정규화된 조건을 슬롯 값으로 바꾼다(SQL 아님)."""

    name: str
    supports_ids: frozenset[str]  # 이 컴파일러가 소유하는 concept/slot id 집합
    emit: Callable[[Mapping[str, Any], CompileContext], SlotEmission]

    def supports(self, concept_or_slot: str) -> bool:
        return concept_or_slot in self.supports_ids

    def compile(self, normalized_condition: Mapping[str, Any], context: CompileContext) -> SlotEmission:
        """정규화 확정(status=normalized) 조건만 받는다 — 실행 게이트를 우회한 호출도 여기서 죽는다."""
        if not isinstance(normalized_condition, Mapping):
            raise CompilerError(f"{self.name}: 조건은 매핑이어야 한다(받은 타입 {type(normalized_condition).__name__})")
        status = normalized_condition.get("status")
        if status != "normalized":
            raise CompilerError(f"{self.name}: status={status!r} 조건은 컴파일할 수 없다(normalized 만 허용)")
        emission = self.emit(normalized_condition, context)
        if not isinstance(emission, SlotEmission):
            raise CompilerError(f"{self.name}: emit 이 SlotEmission 을 반환하지 않았다")
        return emission


class CompilerRegistry:
    """register/get/has (요청 16번). 같은 이름의 중복 등록은 즉시 실패한다(조용한 덮어쓰기 금지)."""

    def __init__(self) -> None:
        self._compilers: dict[str, ConditionCompiler] = {}

    def register(self, name: str, compiler: ConditionCompiler) -> None:
        if not name or not isinstance(name, str):
            raise CompilerError(f"컴파일러 이름이 유효하지 않다: {name!r}")
        if name in self._compilers:
            raise CompilerError(f"이미 등록된 컴파일러: {name!r}")
        self._compilers[name] = compiler

    def get(self, name: str) -> ConditionCompiler:
        compiler = self._compilers.get(name)
        if compiler is None:
            raise CompilerError(f"미등록 컴파일러: {name!r} — compiler_registry.default_registry() 선언을 확인하라.")
        return compiler

    def has(self, name: str | None) -> bool:
        return bool(name) and name in self._compilers

    def names(self) -> frozenset[str]:
        return frozenset(self._compilers)


# ── 슬롯 방출 헬퍼 ─────────────────────────────────────────────────────────────────────────
def _coerced_slot_value(slot_name: str, raw_value: Any, context: CompileContext) -> Any:
    """SLOT_SHAPES coerce 를 통과시킨 값만 방출한다 — LLM 경로와 동일한 닫힌 어휘 검증."""
    shape = SLOT_SHAPES.get(slot_name)
    if shape is None:
        raise CompilerError(f"미등록 슬롯: {slot_name!r} — targeting_ir.SLOT_SHAPES 에 선언해야 한다.")
    allowed = context.allowed.get(shape.allowed_key) if shape.allowed_key else None
    coerced = shape.coerce(raw_value, allowed=allowed)
    if coerced is None:
        raise CompilerError(
            f"슬롯 {slot_name!r} coerce 가 값을 거부했다: {raw_value!r}"
            + (f" (allowed_key={shape.allowed_key} 어휘 주입 필요)" if shape.allowed_key and allowed is None else ""))
    return coerced


def _duration_slot_compiler(name: str, slot_name: str) -> ConditionCompiler:
    """기간 창 계열 슬롯(가입/로그인/미접속/미구매/카트보관) 공통 방출기 — 슬롯당 함수를 만들지 않는다."""

    def emit(condition: Mapping[str, Any], context: CompileContext) -> SlotEmission:
        normalized = condition.get("normalized")
        if not isinstance(normalized, Mapping):
            raise CompilerError(f"{name}: normalized 기간 값이 없다")
        container = SLOT_SHAPES[slot_name].container
        return SlotEmission(container, slot_name, _coerced_slot_value(slot_name, dict(normalized), context))

    return ConditionCompiler(name=name, supports_ids=frozenset({name, slot_name}), emit=emit)


def _aggregate_threshold_compiler() -> ConditionCompiler:
    """공통 조건(GenericCondition) → target_user.aggregate_conditions 항목 방출.

    concept 가 곧 설정(member_target_filters.json aggregate_targets.metrics)의 metric_id 다 —
    coerce 의 닫힌 어휘(allowed: aggregate_metrics)가 미등록 지표를 거부한다."""

    def emit(condition: Mapping[str, Any], context: CompileContext) -> SlotEmission:
        normalized = condition.get("normalized")
        if not isinstance(normalized, Mapping) or not normalized.get("concept"):
            raise CompilerError("purchase_aggregate: normalized {concept, operator, value} 가 없다")
        item: dict[str, Any] = {
            "metric_id": normalized["concept"],
            "operator": normalized.get("operator"),
            "threshold": normalized.get("value"),
        }
        if normalized.get("window_days"):
            item["window_days"] = normalized["window_days"]
        coerced = _coerced_slot_value("aggregate_conditions", [item], context)
        return SlotEmission("target_user", "aggregate_conditions", coerced, mode="extend")

    return ConditionCompiler(
        name="purchase_aggregate",
        supports_ids=frozenset({"purchase_aggregate"}),
        emit=emit,
    )


def _campaign_buy_amount_compiler() -> ConditionCompiler:
    """캠페인 귀속 구매금액 임계 → target_user.campaign_buy_amount 방출."""

    def emit(condition: Mapping[str, Any], context: CompileContext) -> SlotEmission:
        normalized = condition.get("normalized")
        if not isinstance(normalized, Mapping):
            raise CompilerError("campaign_purchase_aggregate: normalized 임계가 없다")
        raw = {"operator": normalized.get("operator"), "amount": normalized.get("value")}
        if normalized.get("window_days"):
            raw["window_days"] = normalized["window_days"]
        return SlotEmission("target_user", "campaign_buy_amount",
                            _coerced_slot_value("campaign_buy_amount", raw, context))

    return ConditionCompiler(
        name="campaign_purchase_aggregate",
        supports_ids=frozenset({"campaign_purchase_aggregate", "campaign_buy_amount"}),
        emit=emit,
    )


def _profile_metric_compiler() -> ConditionCompiler:
    """회원 프로필 수치 지표 임계 → target_user.balance_conditions 방출(물리 바인딩은 coerce 의
    allowed(profile_metrics) 매핑이 결정론으로 채운다 — 임의 컬럼 생성 불가)."""

    def emit(condition: Mapping[str, Any], context: CompileContext) -> SlotEmission:
        normalized = condition.get("normalized")
        if not isinstance(normalized, Mapping) or not normalized.get("concept"):
            raise CompilerError("member_profile_metric: normalized {concept, operator, value} 가 없다")
        item = {
            "metric_id": normalized["concept"],
            "operator": normalized.get("operator"),
            "threshold": normalized.get("value"),
        }
        coerced = _coerced_slot_value("balance_conditions", [item], context)
        return SlotEmission("target_user", "balance_conditions", coerced, mode="extend")

    return ConditionCompiler(
        name="member_profile_metric",
        supports_ids=frozenset({"member_profile_metric"}),
        emit=emit,
    )


def default_registry() -> CompilerRegistry:
    """기본 조건 컴파일러 집합. 이름은 concept_catalog 의 compiler 필드가 참조하는 어휘다."""
    registry = CompilerRegistry()
    for name, slot in (
        ("purchase_inactivity", "purchase_inactivity"),
        ("inactivity_period", "inactivity_period"),
        ("recent_login", "recent_login"),
        ("signup_window", "signup_target"),
        ("cart_retention", "cart_retention"),
    ):
        registry.register(name, _duration_slot_compiler(name, slot))
    for compiler in (_aggregate_threshold_compiler(), _campaign_buy_amount_compiler(), _profile_metric_compiler()):
        registry.register(compiler.name, compiler)
    return registry


def strategy_names() -> frozenset[str]:
    """qualifier 필터 전략(compiler_strategies) 이름 집합 — 카탈로그 검증이 참조용으로 쓴다."""
    return frozenset(compiler_strategies.COMPILER_STRATEGIES)


__all__ = [
    "CompileContext",
    "CompilerError",
    "CompilerRegistry",
    "ConditionCompiler",
    "SlotEmission",
    "default_registry",
    "strategy_names",
]
