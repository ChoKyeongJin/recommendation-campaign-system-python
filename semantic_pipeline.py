"""의미 해석과 query plan 생성의 분리 — 파이프라인 본체.

    사용자 원문
    → SemanticPlan 추출(LLM)
    → 값 정규화
    → 원문 coverage 검증 (+ 제한적 재추출)
    → capability 판정
    → semantic validation (schema 파생 missing/status)
    → deterministic query plan compile
    → 최종 검증

각 단계의 소유자가 다르고, 뒤 단계가 앞 단계의 산출물을 **고치지 않는다**. 특히:

  - missing_fields 는 4단계에서 계산되고 그 뒤 누구도 삭제하지 않는다
    (그래서 `_drop_*_missing_fields` 계열이 존재할 자리가 없다).
  - query_plan 슬롯은 6단계 컴파일러만 쓴다
    (그래서 `_apply_*_backfill` 계열이 존재할 자리가 없다).
  - 원문을 다시 읽는 곳은 3단계 coverage 검증뿐이고, 그 결과는 슬롯이 아니라
    **재추출 요청**이다.

순수 모듈 규약: graph_rag 를 import 하지 않는다(실행 지식은 CompileContext 로 주입).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import semantic_capability
import semantic_coverage
import semantic_plan
from legacy_plan_compiler import CompileContext, CompileResult, LegacyQueryPlanCompiler
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
    contract_violations: list[str] = field(default_factory=list)
    reextracted_spans: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_plan": self.plan.to_dict(),
            "coverage": self.coverage.to_dict(),
            "compiled": self.compiled.to_dict(),
            "status": self.status,
            "contract_violations": list(self.contract_violations),
            "reextracted_spans": list(self.reextracted_spans),
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
    return plan


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


# ── 3단계: coverage 검증 + 제한적 재추출 ─────────────────────────────────────────
def close_coverage(
    query: str,
    plan: SemanticPlanV2,
    *,
    claimed_spans: Sequence[tuple[int, int]] = (),
    reextract: Callable[[str, Sequence[str]], tuple[SemanticPlanV2, list[str]]] | None = None,
) -> tuple[semantic_coverage.CoverageReport, list[str], list[str]]:
    """coverage 를 검증하고, 누락 구간이 있으면 **그 구간만** 한 번 재추출해 병합한다.

    반환: (최종 coverage 보고, 재추출한 구간, 계약 위반)
    """
    report = semantic_coverage.verify_coverage(query, plan, claimed_spans=claimed_spans)
    if not report.uncovered_requirements or reextract is None:
        return report, [], []
    spans = [str(item.get("source_span") or "") for item in report.uncovered_requirements]
    spans = [span for span in spans if span.strip()]
    if not spans:
        return report, [], []
    try:
        addition, violations = reextract(query, spans)
    except Exception:  # noqa: BLE001 — 재추출 실패는 원래의 정직한 결핍 보고로 귀결된다.
        return report, [], []
    import semantic_plan_llm  # 순환 없음(llm 모듈은 pipeline 을 모른다)

    semantic_plan_llm.merge_reextracted(plan, addition, spans=spans)
    final_report = semantic_coverage.verify_coverage(query, plan, claimed_spans=claimed_spans)
    return final_report, spans, list(violations)


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
            "source_span": item.get("source_span"),
            "candidates": [{"type": node_type} for node_type in item.get("types", [])],
        })
    return plan


# ── 6단계: 컴파일 ────────────────────────────────────────────────────────────────
def compile_plan(
    plan: SemanticPlanV2,
    *,
    context: CompileContext,
    registry: Any = None,
) -> CompileResult:
    capability_registry = registry
    if capability_registry is None:
        try:
            capability_registry = semantic_capability.registry()
        except semantic_capability.CapabilityRegistryError:
            capability_registry = None
    return LegacyQueryPlanCompiler().compile(plan, capability_registry, context)


def apply_to_query_plan(query_plan: dict[str, Any], compiled: CompileResult) -> list[str]:
    """컴파일 산출물을 실행 플랜에 쓴다. **슬롯을 쓰는 유일한 경로다.**

    이미 값이 있는 슬롯은 덮어쓰지 않는다 — 이것은 fill-if-empty 백필이 아니라 같은
    컴파일러 산출물끼리의 멱등 보호다(재실행 시 중복 누적 방지). 다른 생산자가 같은 슬롯을
    쓰는 일은 드리프트 가드 테스트가 막는다.
    """
    written: list[str] = []
    target_user = query_plan.get("target_user")
    if not isinstance(target_user, dict):
        target_user = {}
        query_plan["target_user"] = target_user
    for slot, value in compiled.target_user.items():
        if target_user.get(slot):
            continue
        target_user[slot] = copy.deepcopy(value)
        written.append(f"target_user.{slot}")
    for slot, value in compiled.plan.items():
        if query_plan.get(slot):
            continue
        query_plan[slot] = copy.deepcopy(value)
        written.append(slot)
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


# ── 전체 실행 ────────────────────────────────────────────────────────────────────
def run(
    query: str,
    *,
    extract: Callable[[str], tuple[SemanticPlanV2, list[str]]],
    context: CompileContext,
    reextract: Callable[[str, Sequence[str]], tuple[SemanticPlanV2, list[str]]] | None = None,
    claimed_spans: Sequence[tuple[int, int]] = (),
    registry: Any = None,
    available_months: Mapping[str, int] | None = None,
    entity_resolver: EntityResolver | None = None,
) -> PipelineResult:
    """원문 하나를 파이프라인 전체에 통과시킨다."""
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

    normalize_plan(plan, today=context.today, entity_resolver=entity_resolver)
    report, reextracted, reextract_violations = close_coverage(
        query, plan, claimed_spans=claimed_spans, reextract=reextract
    )
    violations.extend(reextract_violations)
    if reextracted:
        normalize_plan(plan, today=context.today, entity_resolver=entity_resolver)
    attach_coverage(plan, report)
    judge_capabilities(plan, registry=registry, available_months=available_months)
    compiled = compile_plan(plan, context=context, registry=registry)
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
    return PipelineResult(
        plan=plan,
        coverage=report,
        compiled=compiled,
        status=plan.status(),
        contract_violations=violations,
        reextracted_spans=reextracted,
    )


__all__ = [
    "CompileContext",
    "PipelineResult",
    "apply_to_query_plan",
    "attach_coverage",
    "close_coverage",
    "compile_plan",
    "judge_capabilities",
    "normalize_plan",
    "project_semantic_ir",
    "run",
]
