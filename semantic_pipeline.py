"""의미 해석과 실행 계획 생성의 분리 — 파이프라인 본체(범용 코어).

    사용자 원문
    → requirement 추출(SemanticPlan 노드)
    → 값·시간 한정어 정규화
    → capability 판정(5축)
    → 실행 계획 컴파일
    → requirement 원장 조립 + requirement 단위 의미검증
    → 미해결 requirement만 패치 재방출(정책 횟수)
    → 구체적인 실패 또는 clarification

각 단계의 소유자가 다르고, 뒤 단계가 앞 단계의 산출물을 **고치지 않는다**. 특히:

  - missing 은 스키마에서 계산되고 그 뒤 누구도 삭제하지 않는다
    (그래서 결핍 사후 삭제 함수가 존재할 자리가 없다).
  - 실행 슬롯은 컴파일러만 쓴다(그래서 정규식 백필이 존재할 자리가 없다).
  - 원문을 다시 읽는 곳은 coverage 검증뿐이고, 그 결과는 슬롯이 아니라 **패치 요청**이다.
  - 재방출은 미해결 requirement 만 건드리고, 회귀가 생기면 라운드를 되돌린다.

범용 코어 규약: graph_rag 를 import 하지 않는다. 실행 플랜 **컨테이너 이름조차 모른다** —
컴파일러가 컨테이너별로 산출물을 돌려주고, 여기서는 그것을 그대로 옮긴다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import requirement_ledger
import semantic_capability
import semantic_coverage
import semantic_domain_binding
import semantic_plan
import semantic_reemission
import temporal_semantics
from compile_contract import CompileContext, CompileResult
from requirement_ledger import RequirementLedger
from semantic_normalizers import (
    AmountNormalizer,
    EntityResolver,
    NormalizationError,
    OperatorNormalizer,
    PeriodNormalizer,
    RankLimitNormalizer,
    UnitNormalizer,
)
from semantic_plan import SemanticPlanV2

# 정규화 대상 값 종류 → 정규화기. 노드가 아니라 **값 종류**로 dispatch 한다.
_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "operator": OperatorNormalizer.normalize,
    "quantity": AmountNormalizer.normalize,
    "rank_limit": RankLimitNormalizer.normalize,
    "unit": UnitNormalizer.normalize,
}


@dataclass
class PipelineResult:
    plan: SemanticPlanV2
    coverage: semantic_coverage.CoverageReport
    compiled: CompileResult
    status: str
    ledger: RequirementLedger = field(default_factory=RequirementLedger)
    contract_violations: list[str] = field(default_factory=list)
    reextracted_spans: list[str] = field(default_factory=list)
    reemission: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_plan": self.plan.to_dict(),
            "coverage": self.coverage.to_dict(),
            "compiled": self.compiled.to_dict(),
            "status": self.status,
            "requirements": self.ledger.to_dict(),
            "contract_violations": list(self.contract_violations),
            "reextracted_spans": list(self.reextracted_spans),
            "reemission": copy.deepcopy(self.reemission),
        }


# ── 2단계: 값 정규화 ──────────────────────────────────────────────────────────────
def normalize_plan(
    plan: SemanticPlanV2, *, today: Any = None, entity_resolver: EntityResolver | None = None
) -> SemanticPlanV2:
    """노드 값을 canonical 형태로 확정한다. 문장을 다시 읽지 않는다 — 값만 본다.

    정규화 실패는 validation_errors 로 기록한다(내부 불량이지 '미지원'이 아니다).
    """
    resolver = entity_resolver or EntityResolver()
    for node in plan.walk():
        for spec in type(node).FIELDS:
            if spec.name not in node.values:
                continue
            raw = node.values[spec.name]
            try:
                if spec.kind == "period":
                    window = PeriodNormalizer.normalize(raw, today=today)
                    node.values[spec.name] = (
                        window.to_window() if hasattr(window, "to_window")
                        else {"value": window.value, "unit": window.unit}
                    )
                elif spec.kind == "entity":
                    node.values[spec.name] = resolver.resolve(raw) or raw
                elif spec.kind in _NORMALIZERS:
                    normalized = _NORMALIZERS[spec.kind](raw)
                    node.values[spec.name] = _plain(normalized)
            except NormalizationError as exc:
                plan.validation_errors.append({
                    "node_id": node.id,
                    "field": spec.name,
                    "failure_code": semantic_plan.VALIDATION_MISMATCH,
                    "reason": str(exc),
                    "received": raw,
                })
        _normalize_temporal(node, plan)
    return plan


def _normalize_temporal(node: Any, plan: SemanticPlanV2) -> None:
    """시간 한정어를 **범용 연산자**로 확정해 노드에 싣는다.

    표면 표현별 예외가 아니라 사상이다: 도메인 관계명은 별칭 표로, 이미 범용 연산자면
    그대로. 시간 축이 아닌 관계(동시구매 등)는 아무 일도 일어나지 않는다.
    """
    if not any(spec.kind == "temporal" for spec in type(node).FIELDS):
        return
    raw = node.values.get("temporal") or node.values.get("relation")
    if raw in (None, "", {}, []):
        return
    try:
        qualifier = temporal_semantics.normalize(
            raw, aliases=semantic_domain_binding.temporal_aliases()
        )
    except temporal_semantics.TemporalSemanticsError as exc:
        plan.validation_errors.append({
            "node_id": node.id,
            "field": "temporal",
            "failure_code": semantic_plan.VALIDATION_MISMATCH,
            "reason": f"시간 한정어를 해석하지 못했습니다: {exc}",
            "received": raw,
        })
        return
    if qualifier is None:
        return
    # 시간 인자는 노드가 이미 소유한 필드에서 가져온다(값의 이중 소유를 만들지 않는다).
    anchor = node.values.get("period")
    interval = node.values.get("period") if node.values.get("months") else None
    payload = temporal_semantics.TemporalQualifier(
        operator=qualifier.operator,
        anchor=qualifier.anchor if qualifier.anchor is not None else anchor,
        interval=(
            qualifier.interval
            if qualifier.interval is not None
            else (interval or _interval_from_months(node))
        ),
        count=qualifier.count if qualifier.count is not None else node.values.get("count"),
        subinterval_unit=(
            qualifier.subinterval_unit
            or (
                semantic_domain_binding.temporal_subinterval_unit()
                if temporal_semantics.ARG_SUBINTERVAL_UNIT in qualifier.spec.requires
                else None
            )
        ),
        source_span=qualifier.source_span or node.source_span,
    )
    node.values["temporal"] = payload.to_dict()


def _interval_from_months(node: Any) -> Any:
    months = node.values.get("months")
    if isinstance(months, (int, float)) and not isinstance(months, bool) and months > 0:
        return {"value": int(months), "unit": "months"}
    return None


def _plain(value: Any) -> Any:
    """정규화 산출물을 노드에 다시 실을 수 있는 평범한 값으로.

    단위 없는 수량(개월 수·변경 횟수 등)은 **스칼라로 남긴다** — dict 로 감싸면 그 필드를
    숫자로 읽는 컴파일러 분기가 조용히 빠진다(실측: 'N개월' 이력 조건이 기간 미확정으로 차단).
    """
    from semantic_normalizers import Quantity  # 순환 없음

    if isinstance(value, Quantity) and value.unit is None:
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


# 재방출은 `semantic_reemission` 하나가 소유한다. 예전에 여기 있던 `close_coverage`
# (uncovered 구간 재추출 + 무조건 병합)는 2026-08-02 계층 분리에서 삭제됐다 — 재방출 경로가
# 둘이면 어느 쪽이 이겼는지가 다시 코드 순서에 숨는다. 지금은 원장의 미해결 항목만이
# 패치 요청의 입력이고, 보호 집합과 회귀 되돌림이 그 경로에만 있다.


# ── 4·5단계: capability 판정 + 검증 ──────────────────────────────────────────────
def judge_capabilities(
    plan: SemanticPlanV2,
    *,
    registry: Any = None,
    available_months: Mapping[str, int] | None = None,
) -> SemanticPlanV2:
    capability_registry = registry
    if capability_registry is None:
        try:
            capability_registry = semantic_capability.registry()
        except semantic_capability.CapabilityRegistryError as exc:
            plan.validation_errors.append({
                "failure_code": semantic_plan.INTERNAL_FAULT,
                "reason": f"capability 선언을 읽지 못했습니다: {exc}",
            })
            return plan
    plan.capability_verdicts = [
        verdict.to_dict()
        for verdict in capability_registry.judge_plan(plan, available_months=available_months)
        if not verdict.executable
    ]
    return plan


# coverage 검증이 만든 충돌의 계보 표시(재평가 때 파생분만 비우기 위한 것).
_COVERAGE_CONFLICT_SOURCE = "coverage"


def attach_coverage(plan: SemanticPlanV2, report: semantic_coverage.CoverageReport) -> SemanticPlanV2:
    """coverage 결과를 플랜에 싣는다 — status 파생의 입력이 된다."""
    plan.uncovered_requirements = list(report.uncovered_requirements)
    for item in report.ungrounded_nodes:
        plan.validation_errors.append({
            "node_id": item.get("node_id"),
            "failure_code": semantic_plan.VALIDATION_MISMATCH,
            "reason": f"노드에 원문 근거가 없습니다: {item.get('reason')}",
        })
    for item in report.contested_spans:
        plan.conflicts.append({
            "status": semantic_plan.STATUS_AMBIGUOUS,
            "failure_code": semantic_plan.AMBIGUOUS_REQUIREMENT,
            "source": _COVERAGE_CONFLICT_SOURCE,
            "source_span": item.get("source_span"),
            "candidates": [{"type": node_type} for node_type in item.get("types", [])],
        })
    return plan


# ── 6단계: 컴파일 ────────────────────────────────────────────────────────────────
def default_compiler() -> Any:
    """조립 지점이 컴파일러를 주입하지 않았을 때의 기본 도메인 컴파일러.

    코어가 도메인 컴파일러를 **아는** 유일한 지점이며, 그것도 지연 import 로 이름만 안다.
    다른 도메인을 얹을 때는 `run(compiler=...)` 로 주입하면 이 경로를 타지 않는다.
    """
    from legacy_plan_compiler import LegacyQueryPlanCompiler  # 조립 편의(코어 계약은 compile_contract)

    return LegacyQueryPlanCompiler()


def compile_plan(
    plan: SemanticPlanV2,
    *,
    context: CompileContext,
    registry: Any = None,
    compiler: Any = None,
) -> CompileResult:
    capability_registry = registry
    if capability_registry is None:
        try:
            capability_registry = semantic_capability.registry()
        except semantic_capability.CapabilityRegistryError:
            capability_registry = None
    return (compiler or default_compiler()).compile(plan, capability_registry, context)


def apply_to_query_plan(query_plan: dict[str, Any], compiled: CompileResult) -> list[str]:
    """컴파일 산출물을 실행 플랜에 쓴다. **슬롯을 쓰는 유일한 경로다.**

    컨테이너 이름은 컴파일러가 붙여 준다 — 이 함수는 도메인 컨테이너 이름을 모른다.
    이미 값이 있는 슬롯은 덮어쓰지 않는다: fill-if-empty 백필이 아니라 같은 컴파일러
    산출물끼리의 멱등 보호다(재실행 시 중복 누적 방지). 다른 생산자가 같은 슬롯을 쓰는
    일은 드리프트 가드 테스트가 막는다.
    """
    written: list[str] = []
    for container, slots in (compiled.containers or {}).items():
        if not slots:
            continue
        if not container:
            target = query_plan
            prefix = ""
        else:
            existing = query_plan.get(container)
            if not isinstance(existing, dict):
                existing = {}
                query_plan[container] = existing
            target = existing
            prefix = f"{container}."
        for slot, value in slots.items():
            if target.get(slot):
                continue
            target[slot] = copy.deepcopy(value)
            written.append(f"{prefix}{slot}")
    return written


# ── semantic_ir 투영(파생물 — LLM 이 만들지 않는다) ──────────────────────────────
def project_semantic_ir(plan: SemanticPlanV2) -> dict[str, Any]:
    """SemanticPlanV2 → 기존 실행기가 읽는 semantic_ir 형태.

    예전에는 LLM 이 이 객체를 직접 냈고(그래서 status·missing 을 LLM 이 정했고), 그 뒤
    여러 sweep 이 그것을 고쳤다. 이제는 **전부 파생값**이다: 여기서 계산되고, 그 뒤 아무도
    고치지 않는다.
    """
    status = plan.status()
    missing = list(plan.missing_fields())
    for item in plan.uncovered_requirements:
        span = str(item.get("source_span") or "").strip()
        if span:
            missing.append(f"uncovered:{span}")
    unsupported = [
        {
            "kind": str(verdict.get("node_type") or "unknown"),
            "reason": str(verdict.get("message") or verdict.get("failure_code") or ""),
            "evidence": str(verdict.get("metric") or ""),
        }
        for verdict in plan.unsupported_operations()
    ]
    ir_status = {
        semantic_plan.STATUS_RESOLVED: "resolved",
        semantic_plan.STATUS_NEEDS_CLARIFICATION: "needs_clarification",
        semantic_plan.STATUS_AMBIGUOUS: "needs_clarification",
        semantic_plan.STATUS_UNSUPPORTED: "unsupported",
        semantic_plan.STATUS_INVALID: "needs_clarification",
    }[status]
    if ir_status == "needs_clarification" and not missing:
        missing = [
            f"conflict:{conflict.get('source_span')}" for conflict in plan.conflicts
        ] or [
            f"invalid:{error.get('node_id') or error.get('reason')}"
            for error in plan.validation_errors
        ] or ["semantic_interpretation"]
    return {
        "status": ir_status,
        "operations": [],
        "missing_fields": missing if ir_status == "needs_clarification" else [],
        "policy_applications": [],
        "unsupported_operations": unsupported if ir_status == "unsupported" else [],
        "message": _status_message(plan, status),
    }


def _status_message(plan: SemanticPlanV2, status: str) -> str | None:
    if status == semantic_plan.STATUS_UNSUPPORTED:
        messages = [str(item.get("message") or "") for item in plan.unsupported_operations()]
        joined = " ".join(message for message in messages if message)
        return joined or None
    if status == semantic_plan.STATUS_AMBIGUOUS:
        spans = [str(item.get("source_span") or "") for item in plan.conflicts]
        listed = ", ".join(span for span in spans if span)
        return f"'{listed}' 의 의미가 둘 이상으로 해석됩니다. 어느 쪽인지 알려주세요." if listed else None
    if status == semantic_plan.STATUS_INVALID:
        # 내부 불량은 '미지원'이 아니다 — 확인 요청으로 안내하고 코드는 따로 남긴다.
        return "요청 해석 중 내부 검증에 실패했습니다. 조건을 조금 더 구체적으로 알려주세요."
    return None


# ── 평가 1회분(정규화 → coverage → capability → 컴파일 → 원장) ──────────────────
@dataclass
class Evaluation:
    """플랜 한 상태의 전 단계 판정. 재방출 라운드마다 이 묶음을 통째로 다시 만든다."""

    report: semantic_coverage.CoverageReport
    compiled: CompileResult
    ledger: RequirementLedger


def evaluate(
    query: str,
    plan: SemanticPlanV2,
    *,
    context: CompileContext,
    claimed_spans: Sequence[tuple[int, int]] = (),
    registry: Any = None,
    available_months: Mapping[str, int] | None = None,
    entity_resolver: EntityResolver | None = None,
    compiler: Any = None,
    slot_catalog: Mapping[str, Sequence[str]] | None = None,
    labeller: Any = None,
) -> Evaluation:
    """플랜을 정규화·판정·컴파일하고 요구사항 원장까지 만든다(재실행 가능).

    파생 산출물(결핍 보고·capability 판정·충돌·미커버)은 **매번 비우고 다시 계산한다** —
    파생값을 누적하면 이전 라운드의 실패가 유령으로 남는다.
    """
    plan.validation_errors.clear()
    plan.capability_verdicts.clear()
    plan.uncovered_requirements.clear()
    # 충돌은 두 출처가 있다: 후보 병합 단계(생산자 소유)와 coverage 검증(파생).
    # 파생분만 비운다 — 생산자가 남긴 모호성까지 지우면 그 조건이 조용히 사라진다.
    plan.conflicts[:] = [
        conflict for conflict in plan.conflicts
        if not (isinstance(conflict, Mapping) and conflict.get("source") == _COVERAGE_CONFLICT_SOURCE)
    ]

    normalize_plan(plan, today=context.today, entity_resolver=entity_resolver)
    report = semantic_coverage.verify_coverage(query, plan, claimed_spans=claimed_spans)
    attach_coverage(plan, report)
    judge_capabilities(plan, registry=registry, available_months=available_months)
    compiled = compile_plan(plan, context=context, registry=registry, compiler=compiler)
    for failure in compiled.failures:
        code = failure.get("failure_code")
        if code in semantic_plan.INTERNAL_FAILURE_CODES:
            plan.validation_errors.append(failure)
        else:
            plan.capability_verdicts.append({
                "node_id": failure.get("node_id"),
                "node_type": "compiler",
                "failure_code": code,
                "message": failure.get("reason"),
            })
    ledger = requirement_ledger.build_ledger(
        plan,
        compiled=compiled,
        coverage=report,
        slot_catalog=slot_catalog or _default_slot_catalog(),
        labeller=labeller,
    )
    return Evaluation(report=report, compiled=compiled, ledger=ledger)


def _default_slot_catalog() -> Mapping[str, Sequence[str]]:
    from legacy_plan_compiler import NODE_SLOT_MAP  # 조립 편의(코어 계약은 compile_contract)

    return NODE_SLOT_MAP


# ── 전체 실행 ────────────────────────────────────────────────────────────────────
def run(
    query: str,
    *,
    extract: Callable[[str], tuple[SemanticPlanV2, list[str]]],
    context: CompileContext,
    reextract: Callable[[str, Sequence[str]], tuple[SemanticPlanV2, list[str]]] | None = None,
    request_patch: Callable[[Any], SemanticPlanV2 | None] | None = None,
    claimed_spans: Sequence[tuple[int, int]] = (),
    registry: Any = None,
    available_months: Mapping[str, int] | None = None,
    entity_resolver: EntityResolver | None = None,
    compiler: Any = None,
    slot_catalog: Mapping[str, Sequence[str]] | None = None,
    labeller: Any = None,
    reemission_policy: semantic_reemission.ReemissionPolicy | None = None,
) -> PipelineResult:
    """원문 하나를 파이프라인 전체에 통과시킨다.

    재방출은 **미해결 requirement 만** 대상으로 하는 패치이고, 라운드 수는 정책값이다.
    `reextract` 는 이전 계약(구간 목록 재추출)을 패치 요청으로 감싸 그대로 받아들인다.
    """
    violations: list[str] = []
    try:
        plan, extraction_violations = extract(query)
        violations.extend(extraction_violations)
    except Exception as exc:  # noqa: BLE001 — 추출 실패는 내부 사고다(미지원이 아니다).
        plan = SemanticPlanV2(source_query=query)
        plan.validation_errors.append({
            "failure_code": semantic_plan.INTERNAL_FAULT,
            "reason": f"의미 추출 실패: {exc.__class__.__name__}: {exc}",
        })
        return PipelineResult(
            plan=plan,
            coverage=semantic_coverage.CoverageReport(),
            compiled=CompileResult(),
            status=plan.status(),
            contract_violations=violations,
        )

    evaluate_kwargs = {
        "context": context,
        "claimed_spans": claimed_spans,
        "registry": registry,
        "available_months": available_months,
        "entity_resolver": entity_resolver,
        "compiler": compiler,
        "slot_catalog": slot_catalog,
        "labeller": labeller,
    }
    state = evaluate(query, plan, **evaluate_kwargs)
    latest: dict[str, Evaluation] = {"value": state}

    def _rebuild(updated: SemanticPlanV2) -> RequirementLedger:
        latest["value"] = evaluate(query, updated, **evaluate_kwargs)
        return latest["value"].ledger

    patcher = request_patch or _patch_adapter(query, reextract, violations)
    outcome = semantic_reemission.run(
        plan,
        state.ledger,
        request_patch=patcher,
        rebuild=_rebuild,
        policy=reemission_policy,
    )
    final = latest["value"] if outcome.patched else state
    if outcome.plan is not plan:
        # 라운드가 되돌려졌다 — 스냅샷 플랜으로 판정을 다시 맞춘다.
        final = evaluate(query, outcome.plan, **evaluate_kwargs)
    return PipelineResult(
        plan=outcome.plan,
        coverage=final.report,
        compiled=final.compiled,
        status=outcome.plan.status(),
        ledger=final.ledger,
        contract_violations=violations,
        reextracted_spans=[
            span for record in outcome.rounds for span in record.requested_spans
        ],
        reemission=outcome.to_dict(),
    )


def _patch_adapter(
    query: str,
    reextract: Callable[[str, Sequence[str]], tuple[SemanticPlanV2, list[str]]] | None,
    violations: list[str],
) -> Callable[[Any], SemanticPlanV2 | None] | None:
    """구간 재추출 계약 → 패치 요청 계약(호출자 코드를 바꾸지 않고 패치 경로로 태운다)."""
    if reextract is None:
        return None

    def request_patch(request: Any) -> SemanticPlanV2 | None:
        patch, patch_violations = reextract(query, list(request.spans))
        violations.extend(patch_violations or [])
        return patch

    return request_patch


__all__ = [
    "CompileContext",
    "Evaluation",
    "PipelineResult",
    "apply_to_query_plan",
    "attach_coverage",
    "compile_plan",
    "default_compiler",
    "evaluate",
    "judge_capabilities",
    "normalize_plan",
    "project_semantic_ir",
    "run",
]
