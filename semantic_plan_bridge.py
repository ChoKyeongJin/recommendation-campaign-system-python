"""**도메인 조립 지점** — 범용 코어 파이프라인과 회원 타기팅 실행 플랜의 배선.

graph_rag 가 부르는 단 하나의 진입점이며, 하는 일은 **주입**뿐이다: 실행 레지스트리
(슬롯 shape·닫힌 어휘·지표 카탈로그)를 CompileContext 로 묶고, 도메인 컴파일러와 슬롯
카탈로그·라벨러를 코어에 넘기고, 산출물을 플랜에 쓴다.

계층:
    graph_rag → semantic_plan_bridge(도메인 조립) → semantic_pipeline(범용 코어)
                        │
                        ├─ targeting_domain(도메인 선언·어휘·시간 한정어 결속)
                        └─ legacy_plan_compiler(실행 슬롯 지식)

이 파일이 대체한 것(전부 삭제됨): 카트/주문·캠페인·프로필·파생 집합·속성 이력·동시구매의
정규식 백필과, 미귀결 라벨 힌트 재방출, 결핍 사후 삭제 계열. 원문을 다시 읽는 곳은
coverage 검증 하나뿐이고 그 결과는 슬롯이 아니라 패치 요청이다.

순수 모듈 규약: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping, Sequence

import legacy_plan_compiler
import plan_decisions
import requirement_ledger
import semantic_pipeline
import semantic_plan
import semantic_reemission
import targeting_domain
from compile_contract import CompileContext
from semantic_normalizers import MetricResolver
from semantic_pipeline import PipelineResult
from semantic_plan import SemanticPlanV2

PLAN_KEY = "semantic_plan"
PIPELINE_KEY = "semantic_pipeline"
REQUIREMENTS_KEY = "requirements"


def build_context(
    *,
    slot_shapes: Mapping[str, Any],
    allowed: Mapping[str, Any],
    aggregate_metric_specs: Mapping[str, Any] | None = None,
    member_metric_specs: Sequence[Mapping[str, Any]] | None = None,
    cart_metric_ids: Sequence[str] = (),
    campaign_event_specs: Mapping[str, Any] | None = None,
    profile_metric_specs: Mapping[str, Any] | None = None,
    history_attribute_specs: Mapping[str, Any] | None = None,
    entity_set_measures: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> CompileContext:
    """실행 어휘를 도메인별 MetricResolver 로 묶는다(컴파일러는 레지스트리를 스스로 열지 않는다)."""
    resolvers: dict[str, Callable[[Any], str | None]] = {
        "aggregate": _resolver(MetricResolver.from_specs(aggregate_metric_specs or {})),
        "member_metric": _resolver(MetricResolver.from_specs(list(member_metric_specs or []))),
        "cart": _resolver(MetricResolver.from_specs({metric: {} for metric in cart_metric_ids})),
        "campaign_event": _resolver(MetricResolver.from_specs(campaign_event_specs or {})),
        "profile": _resolver(MetricResolver.from_specs(profile_metric_specs or {})),
        "history_attribute": _resolver(MetricResolver.from_specs(history_attribute_specs or {})),
        "entity_set_measure": _resolver(MetricResolver.from_specs(entity_set_measures or {})),
    }
    return CompileContext(
        slot_shapes=slot_shapes,
        allowed=allowed,
        today=today or date.today(),
        metric_resolvers=resolvers,
    )


def _resolver(metric_resolver: MetricResolver) -> Callable[[Any], str | None]:
    def resolve(surface: Any) -> str | None:
        # 카탈로그가 해소하지 못하면 원문 표현을 그대로 넘긴다 — 닫힌 어휘 coerce 가 최종
        # 판정자이므로, 여기서 조용히 버리면 '왜 떨어졌는지'가 사라진다.
        resolved = metric_resolver.resolve(surface)
        if resolved:
            return resolved
        return str(surface) if isinstance(surface, str) and surface.strip() else None

    return resolve


def _plan_from_payload(query_plan: Mapping[str, Any], query: str) -> tuple[SemanticPlanV2, list[str]]:
    raw = query_plan.get(PLAN_KEY)
    if not isinstance(raw, Mapping):
        return SemanticPlanV2(source_query=query), []
    try:
        return semantic_plan.plan_from_dict(dict(raw), source_query=query), []
    except semantic_plan.SemanticPlanError as exc:
        plan = SemanticPlanV2(source_query=query)
        plan.validation_errors.append({
            "failure_code": semantic_plan.VALIDATION_MISMATCH,
            "reason": f"semantic_plan 을 해석하지 못했습니다: {exc}",
        })
        return plan, [f"semantic_plan_invalid:{exc}"]


def claimed_evidence_spans(query_plan: Mapping[str, Any]) -> list[tuple[int, int]]:
    """다른 소유자(V4 슬롯 계층)가 이미 근거로 청구한 구간.

    coverage 검증이 '내가 안 만든 조건'까지 누락으로 보고하지 않게 하는 이행기 장치다.
    슬롯 계층이 전부 SemanticPlan 으로 옮겨오면 이 목록은 자연히 빈다.
    """
    spans: list[tuple[int, int]] = []
    for item in query_plan.get("semantic_evidence") or []:
        if not isinstance(item, Mapping):
            continue
        start, end = item.get("start"), item.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end:
            spans.append((start, end))
    return spans


def condition_label(node: Any) -> str:
    """요구사항 원장이 보여 줄 조건 라벨 — 조건 종류 + 집계 도메인(있으면)."""
    base = targeting_domain.condition_label(getattr(node, "type", ""))
    scope = (getattr(node, "values", {}) or {}).get("scope")
    if isinstance(scope, str) and scope:
        return f"{targeting_domain.scope_label(scope)} {base}"
    return base


def apply(
    query_plan: dict[str, Any],
    query: str,
    *,
    context: CompileContext,
    reextract: Callable[[str, Sequence[str]], tuple[SemanticPlanV2, list[str]]] | None = None,
    available_months: Mapping[str, int] | None = None,
    reemission_policy: semantic_reemission.ReemissionPolicy | None = None,
) -> PipelineResult:
    """플랜에 실린 semantic_plan 을 파이프라인에 통과시키고 산출물을 쓴다.

    쓰는 것: 컴파일된 실행 슬롯, 파생 semantic_ir, 요구사항 원장, 파이프라인 감사 기록.
    쓰지 않는 것: 그 외 무엇도. 특히 기존 missing_fields 를 고치지 않는다(파생값이라 고칠 게 없다).
    """
    base_plan, violations = _plan_from_payload(query_plan, query)

    def _extract(_query: str) -> tuple[SemanticPlanV2, list[str]]:
        return base_plan, violations

    result = semantic_pipeline.run(
        query,
        extract=_extract,
        context=context,
        reextract=reextract,
        claimed_spans=claimed_evidence_spans(query_plan),
        available_months=available_months,
        compiler=legacy_plan_compiler.LegacyQueryPlanCompiler(),
        slot_catalog=legacy_plan_compiler.NODE_SLOT_MAP,
        labeller=condition_label,
        reemission_policy=reemission_policy or semantic_reemission.ReemissionPolicy.from_env(),
    )
    written = semantic_pipeline.apply_to_query_plan(query_plan, result.compiled)
    query_plan[PLAN_KEY] = result.plan.to_dict()
    query_plan["semantic_ir"] = semantic_pipeline.project_semantic_ir(result.plan)
    query_plan[REQUIREMENTS_KEY] = result.ledger.to_dict()
    query_plan[PIPELINE_KEY] = {
        "status": result.status,
        "written_slots": written,
        "reextracted_spans": result.reextracted_spans,
        "contract_violations": result.contract_violations,
        "coverage": result.coverage.to_dict(),
        "compile_failures": result.compiled.failures,
        "node_slots": result.compiled.node_slots,
        "reemission": result.reemission,
        "clarification_targets": requirement_ledger.clarification_targets(result.ledger),
    }
    for slot in written:
        node_ids = [node for node, path in result.compiled.node_slots.items() if path == slot]
        plan_decisions.record(
            query_plan,
            filter_name="semantic_plan_compiler",
            action=plan_decisions.SET,
            slot=slot,
            reason="SemanticPlanV2 노드의 결정론 컴파일(원문 재해석 아님)",
            value=",".join(node_ids)[:80],
        )
    return result


__all__ = [
    "PIPELINE_KEY",
    "PLAN_KEY",
    "REQUIREMENTS_KEY",
    "apply",
    "build_context",
    "claimed_evidence_spans",
    "condition_label",
]
