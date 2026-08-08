"""오디언스 요구 → 실행 표현. **검증**과 **legacy plan 키 투영**을 나눈 두 함수.

이 모듈이 생긴 이유는 하나다. ``campaign_plan_v4._derive_audience_execution`` 이 217줄
안에서 두 가지 일을 섞고 있었다:

    (a) 요구 검증 → issue 판정        …… 이제 :func:`run_audience_resolver`
    (b) legacy plan 키 6개로 투영     …… 이제 :func:`project_resolution_to_plan`

(a)는 다시 둘로 갈린다 — 표면 payload 를 **읽을 수 있는 표현으로 만드는 일**(정규화·파싱·
근거 검증)은 이 배포의 계약을 아는 도메인이 하고, 그 표현이 유효한지 **묻는 일**은 요구
계층(:class:`~query_pipeline.requirement.resolver.DefaultRequirementResolver`)이 주입된
검증기(:mod:`audience_validators`)로 한다.

**여기서 동작을 개선하지 않는다.** 이관과 개선을 섞으면 회귀의 원인을 가릴 수 없다 —
산출은 ``tests/test_audience_execution_projection.py`` 가 갈래별로 고정한 그대로여야 한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import audience_authority
import audience_issue_contract
import campaign_metric_claims
import canonical_audience_claims
import consent_cardinality
import event_ir
import execution_assets
import lowering_planner
import plan_decisions
import rolling_absence_claims
import semantic_outcome
import semantic_requirements
from audience_validators import audience_validators
from query_pipeline.compiler.capability import event_ir_capability_profile
from query_pipeline.requirement.models import (
    AudienceRequirement,
    IntentKind,
    ProposedExpression,
    RequirementIntent,
    RequirementSource,
)
from query_pipeline.requirement.parser import REQUIREMENT_SCHEMA_VERSION
from query_pipeline.requirement.resolver import (
    DefaultRequirementResolver,
    RequirementResolutionContext,
    StaticSchemaRegistry,
)
from query_pipeline.requirement.validation import ISSUE_CODE_KINDS, report_from_issue
from query_structurer.semantic_ir import write_semantic_ir
from query_structurer.semantic_outcome import (
    FAILURE_REASON_EMISSION,
    FAILURE_REASON_REGISTRY_GAP,
)

AUDIENCE_REQUIREMENT_KEY = "audience_requirement"
EVENT_EXPRESSION_KEY = "event_expression"
# 시간·이력 절의 **선언된** 반려(코드·문장·근거·귀결 종류). 사용자 문구는 semantic_ir.message 가
# 나르지만, 운영이 읽을 구조화 진단은 문장 하나로 뭉개지 않고 이 키에 그대로 남긴다.
TEMPORAL_REJECTION_KEY = "audience_temporal_rejection"
# 적재 범위 경고(의미 지원 여부와 **다른 축**). 조건은 그대로 나가되 그 사실이 응답에 남는다.
COVERAGE_WARNINGS_KEY = "audience_coverage_warnings"

# LLM 계약이 쓸 수 있는 issue 코드. 손 목록이 아니라 code ↔ kind 표에서 **파생**한다 —
# 예전에는 이 집합과 그 표가 각자 적혀 있었고, 그러면 한쪽만 늘어난 상태가 조용히 생긴다.
AUDIENCE_REQUIREMENT_ISSUE_CODES = frozenset(ISSUE_CODE_KINDS)

# 오디언스 경로에는 사용자 문장의 로케일 개념이 없다(표현은 이미 canonical 이다). 요구
# 계층이 요구하는 값이라 선언만 해 두고, 기간 표면어 해석은 이 경로를 타지 않는다.
_LOCALE = "ko-KR"
_TIMEZONE = "Asia/Seoul"


class AudienceValidationError(Exception):
    """페이로드가 오디언스 계약 자체를 어겼다(issue 로 답할 수 없는 형태 위반).

    :class:`campaign_plan_v4.CampaignQueryPlanValidationError` 로 감싸여 나간다 — 이 모듈이
    그 예외 타입을 import 하면 순환이 된다.
    """


# ── 계약 형태 검증(원문 근거 대조) ────────────────────────────────────────────────


def validate_audience_issue(item: Any, query: str) -> dict[str, Any]:
    """모델이 신고한 issue 하나의 형태·근거를 확인한다(구간이 원문과 맞아야 한다)."""
    if not isinstance(item, dict):
        raise AudienceValidationError("audience_requirement.issues items must be objects")
    code = str(item.get("code") or "")
    argument = str(item.get("argument") or "")
    message = str(item.get("message") or "")
    evidence = item.get("evidence")
    if code not in AUDIENCE_REQUIREMENT_ISSUE_CODES:
        raise AudienceValidationError(f"unknown audience issue code: {code!r}")
    if not argument or not message or not isinstance(evidence, dict):
        raise AudienceValidationError("audience issue needs argument/message/evidence")
    text = evidence.get("text")
    start, end = evidence.get("start"), evidence.get("end")
    if not (
        isinstance(text, str) and text
        and isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
        and query[start:end] == text
    ):
        raise AudienceValidationError("audience issue evidence does not match original_query")
    if code == "missing_argument" and argument == "period":
        import audience_runtime

        # 그 낱말이 기간을 요구하지 않는 **관측 선택자**로 해석됐다면 이 신고는 계약 위반이다.
        # 판정은 애플리케이션이 소유하고(:mod:`targeting_domain`), 모델의 신고는 그 판정을
        # 뒤집지 못한다 — 프롬프트가 아니라 여기가 source of truth 다.
        if audience_issue_contract.period_issue_is_observation_selector(query, item):
            raise AudienceValidationError(
                "missing_argument(period) targets an observation selector, not a period: "
                "'최근/현재/직전 + 속성 축'은 관측 시점 선택이며 기간을 요구하지 않는다"
            )
        if audience_issue_contract.fabricated_period_issue_for_current_catalog_value(
            query, item, audience_runtime.catalog_snapshot()
        ):
            raise AudienceValidationError(
                "missing_argument(period) contradicts a catalog-grounded current subject value"
            )
    return {
        "code": code,
        "argument": argument,
        "message": message,
        "evidence": {"text": text, "start": start, "end": end},
    }


def _audience_issue_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    """issue 하나의 신원(코드·인자·근거 구간). 생산자를 가르는 데만 쓴다."""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    return (str(item.get("code")), str(item.get("argument")), str(evidence.get("text")))


def _supported_obligation_conflicts(
    query: str, unsupported: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """'표현 불가' 신고 중 **애플리케이션이 낼 수 있다고 계산해 둔** 자리의 것들.

    반박의 축과 판정의 축은 같아야 한다 — 구조화기의 재방출 요구
    (:func:`query_structurer.structurer._audience_repair_error`)와 이 종결 판정이 서로 다른
    기준을 쓰면, 재시도를 거는 조건과 미지원으로 닫는 조건이 갈라진다.
    """
    obligations = canonical_audience_claims.supported_obligations_for_query(query)
    if not obligations:
        return []
    conflicts: list[dict[str, Any]] = []
    for item in unsupported:
        obligation = canonical_audience_claims.obligation_conflicting_with_claim(
            item, obligations
        )
        if obligation is None:
            continue
        conflicts.append({
            "argument": str(item.get("argument") or ""),
            "evidence": str((item.get("evidence") or {}).get("text") or ""),
            "obligation_kind": semantic_requirements.obligation_kind(obligation),
            "source_span": dict(obligation.source_span),
        })
    return conflicts


def _lowering_plan_conflicts(
    query: str, unsupported: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """'표현 불가' 신고 중 **실제로 낮출 수 있는** 자리의 것들.

    지원 여부의 authoritative source 다. 의무 종류 allowlist(:data:`canonical_audience_claims.
    CANONICAL_COMPILED_OBLIGATION_KINDS`)와 달리 이 판정은 목록을 조회하지 않는다 —
    :mod:`lowering_planner` 가 canonical 표현을 실제로 만들어 컴파일해 보고 답한다. 그러므로 새
    지표·새 기간 문법이 카탈로그에 들어오면 여기 한 줄도 고치지 않고 함께 판정된다.

    **겹침은 후보를 찾는 데만 쓴다.** 계획의 구간이 신고 구간을 덮는다는 사실은 그 계획이 그
    의미를 낼 수 있다는 뜻이 아니다 — 계획의 span 은 표지들의 hull 이라 구조가 읽지도 않은
    텍스트까지 삼킨다. 그래서 반박은 :func:`lowering_planner.plan_satisfying_span` 이
    **요구 정산**으로 답한다(hull 안 요구를 전부 소비한 계획만 반박한다).
    """
    if not isinstance(query, str) or not query.strip():
        return []
    conflicts: list[dict[str, Any]] = []
    for item in unsupported:
        evidence = item.get("evidence")
        try:
            plan = lowering_planner.plan_satisfying_span(query, evidence)
        # 계획을 못 세우면 반박하지 않는다(추측 금지) — 카탈로그·달력 로딩 실패는 지원 없음의
        # 근거가 아니라 판정 불가다.
        except Exception:
            return []
        if plan is None:
            continue
        conflicts.append({
            "argument": str(item.get("argument") or ""),
            "evidence": str((evidence or {}).get("text") or ""),
            "obligation_kind": plan.obligation.kind,
            "source_span": {
                "start": plan.obligation.source_span[0],
                "end": plan.obligation.source_span[1],
            },
            "capabilities": sorted(plan.capabilities),
        })
    return conflicts


# 판정자의 typed 답이 남는 자리. legacy 게이트와 판정자가 갈릴 때 **무엇이 갈렸는지**가
# 응답에 남아야 다음 이관의 근거가 된다(Phase 3A shadow).
PLANNER_RESOLUTION_KEY = "audience_planner_resolution"


def _planner_resolution(payload: dict[str, Any], query: str) -> Any:
    """판정자에게 "이 요청을 실행할 수 있는가"를 묻고 그 답을 payload 에 남긴다.

    부르는 것 자체가 shadow evaluation 이다 — 기존 경로의 귀결은 바꾸지 않고 두 답을 나란히
    기록한다. 판정자가 죽으면 shadow 도 없다(요청을 막지 않는다): 관측이 판정을 이기면 안 된다.
    """

    import lowering_planner  # 지연 import(순환 방지)

    try:
        resolution = lowering_planner.resolve_executable(query)
    # 판정 자체를 못 하면 관측을 남기지 않는다. 넓게 잡는 이유는 이 호출이 **관측 전용**이라
    # 여기서 던진 예외가 사용자 요청을 막아서는 안 되기 때문이다(귀결에 영향이 없다).
    except Exception as exc:
        payload[PLANNER_RESOLUTION_KEY] = {
            "kind": "unavailable", "detail": f"{type(exc).__name__}: {exc}"
        }
        return None
    record: dict[str, Any] = {"kind": type(resolution).__name__}
    if isinstance(resolution, lowering_planner.Executable):
        record["plans"] = [
            {"obligation": type(plan.obligation).__name__, "sql": plan.sql}
            for plan in resolution.plans
        ]
    elif isinstance(resolution, lowering_planner.Undetermined):
        record["reason"] = resolution.reason
    else:
        diagnostic = getattr(resolution, "diagnostic", None)
        if diagnostic is not None:
            record["diagnostic"] = diagnostic.to_dict()
    payload[PLANNER_RESOLUTION_KEY] = record
    return resolution


def _planner_capability_diagnostic(resolution: Any) -> Any:
    """판정자가 **자산·주체의 한계**로 닫은 경우의 typed 진단. 아니면 ``None``.

    이관된 첫 게이트다. 이 자리를 먼저 옮기는 이유: 능력 부재와 주체 불일치는 판정자만 아는
    사실이고(선언과 대조해야 안다), 그 사실이 없으면 사용자는 "표현할 수 없습니다"라는 원인
    없는 문장 — 또는 사유조차 없는 ``failure`` — 를 받는다. 귀결은 그대로 ``unsupported`` 이고
    **이름만** 정확해진다.
    """

    import lowering_planner  # 지연 import(순환 방지)
    import semantic_diagnostics

    if not isinstance(
        resolution, (lowering_planner.MissingCapability, lowering_planner.InvalidSemantics)
    ):
        return None
    diagnostic = resolution.diagnostic
    if diagnostic.outcome is not semantic_diagnostics.Outcome.UNSUPPORTED:
        # 귀결 파생이 미지원이 아니면 이 자리에서 쓰지 않는다 — 귀결을 정하는 것은 진단이지
        # 이 분기가 아니다.
        return None
    return diagnostic


# 진단이 실린 자리. 사용자 문구는 진단이 만들고, 귀결은 Outcome Mapper 가 정한다.
DIAGNOSTIC_KEY = "audience_diagnostic"

# typed 귀결 → 이 저장소의 wire status. 파생의 소유자는 :mod:`semantic_diagnostics` 이고,
# 여기서는 그 값을 wire 어휘로 옮기기만 한다(두 번째 판단을 만들지 않는다).
_WIRE_STATUS: dict[str, str] = {
    "clarification": "needs_clarification",
    "unsupported": "unsupported",
    "internal_failure": "needs_clarification",
}


def write_diagnostic(payload: dict[str, Any], diagnostic: Any) -> None:
    """typed 진단 하나를 사용자 귀결로 옮긴다. **귀결을 여기서 고르지 않는다.**

    상태·문구·사유가 전부 진단에서 나온다. 이 함수가 하는 일은 wire 어휘로의 번역뿐이고,
    그래서 같은 원인이 경로마다 다른 귀결로 끝날 수 없다(Outcome Mapper 단일화).
    """

    import semantic_diagnostics
    from query_structurer.campaign_plan_v4 import empty_semantic_ir

    outcome = semantic_diagnostics.outcome_for(diagnostic)
    status = _WIRE_STATUS[str(outcome)]
    write_semantic_ir(
        payload,
        empty_semantic_ir(
            status=status,
            missing_fields=(
                ["audience.requirement"] if status == "needs_clarification" else []
            ),
            message=diagnostic.user_action,
            failure_kind=(
                "unsupported"
                if outcome is semantic_diagnostics.Outcome.UNSUPPORTED
                else "system_failure"
                if outcome is semantic_diagnostics.Outcome.INTERNAL_FAILURE
                else "user_clarification"
            ),
            # 미지원 귀결에는 **무엇이** 미지원인지가 함께 실려야 한다(wire 계약). 진단의
            # 코드가 곧 그 종류이므로 여기서 새 이름을 만들지 않는다.
            unsupported_operations=(
                [{
                    "kind": diagnostic.code,
                    "reason": diagnostic.developer_detail,
                    "evidence": diagnostic.evidence or "",
                }]
                if outcome is semantic_diagnostics.Outcome.UNSUPPORTED
                else []
            ),
        ),
    )
    payload[DIAGNOSTIC_KEY] = diagnostic.to_dict()


def _write_capability_gap(payload: dict[str, Any], diagnostic: Any) -> None:
    """능력 부재로 닫는다 — 귀결은 미지원이고 **이름은 그 능력**이다."""

    write_diagnostic(payload, diagnostic)


def _requested_subject_diagnostic(query: str) -> Any:
    """결과 주체가 선언된 주체가 아니면 그 진단. 판정의 소유자는 :mod:`lowering_planner` 다."""

    import lowering_planner  # 지연 import(순환 방지)

    try:
        return lowering_planner.unsupported_subject_diagnostic(query)
    # 판정을 못 하면 주체를 문제 삼지 않는다(추측 금지) — 기존 경로가 그대로 답한다.
    except Exception:
        return None


def _temporal_rejection_diagnostic(declared: Any) -> Any:
    """판정 계층이 선언한 반려 → typed 진단. 표에 없는 코드면 ``None``(기존 경로 유지).

    **순서가 계약이다(Phase 4).** 이 번역은 진단 생산자를 먼저 고친 뒤에만 안전하다. 잘못된
    진단을 그대로 중앙화하면 귀결만 뒤집혀서, 고칠 수 없는 것을 고치라고 안내하게 된다 —
    실측 #73 이 정확히 그 자리였다(값 결핍처럼 보이지만 실제로는 이력 소스 부재).

    표에 없는 코드에 기본값을 주지 않는 이유도 같다. 모르는 사유를 되묻기로 접으면 없는
    복구 경로를 광고하고, 미지원으로 접으면 없는 한계를 광고한다.
    """

    import semantic_diagnostics
    import temporal_claims

    code = _TEMPORAL_DIAGNOSTIC_CODES.get(str(getattr(declared, "code", "")))
    if code is None:
        return None
    evidence = declared.evidence if isinstance(declared.evidence, Mapping) else {}
    diagnostic = semantic_diagnostics.Diagnostic(
        code=code,
        user_action=str(declared.message),
        developer_detail=(
            f"temporal_claims.{declared.code} disposition={declared.disposition}"
        ),
        recoverability=(
            semantic_diagnostics.Recoverability.USER
            if declared.disposition == temporal_claims.CLARIFICATION
            else semantic_diagnostics.Recoverability.DEPLOYMENT
        ),
        symbol=str(declared.code),
        evidence=str(evidence.get("text") or "") or None,
    )
    # 선언된 귀결과 파생된 귀결이 어긋나면 **번역하지 않는다**. 둘 중 하나가 틀린 것이고,
    # 어느 쪽인지는 이 자리에서 알 수 없다 — 추측해서 뒤집으면 조용한 귀결 변경이 된다.
    expected = (
        semantic_diagnostics.Outcome.CLARIFICATION
        if declared.disposition == temporal_claims.CLARIFICATION
        else semantic_diagnostics.Outcome.UNSUPPORTED
    )
    return diagnostic if diagnostic.outcome is expected else None


# 판정 계층의 사유 코드 → typed 진단 코드. 왼쪽의 소유자는 :mod:`temporal_claims` 이고
# 오른쪽은 :mod:`semantic_diagnostics` 다. 드리프트는 테스트가 고정한다.
_TEMPORAL_DIAGNOSTIC_CODES: dict[str, str] = {
    "temporal_value_count_mismatch": "missing_value",
    "temporal_interval_missing": "missing_value",
    "temporal_bucket_unit_missing": "missing_value",
    "temporal_bucket_count_missing": "missing_value",
    "temporal_change_count_value_missing": "missing_value",
    "temporal_value_domain_unresolved": "ambiguous_meaning",
    "temporal_domain_mixed": "ambiguous_meaning",
    "temporal_metric_ambiguous": "ambiguous_meaning",
    # 값 축에 시간 관측 지표가 없다 = 그 축의 **이력 소스가 없다**. 필드 부재가 아니다 —
    # 필드는 있고(현재값), 없는 것은 시점을 가진 관측이다(감사 #73).
    "temporal_metric_not_declared": "missing_history_source",
    "temporal_interval_required_not_expressible": "unlowerable_temporal_constraint",
    "temporal_interval_forbidden": "unlowerable_temporal_constraint",
    "temporal_anchor_shape_unsupported": "unlowerable_temporal_constraint",
    "temporal_operator_plan_missing": "unlowerable_temporal_constraint",
}


def _write_emission_failure(
    payload: dict[str, Any], failures: list[dict[str, Any]]
) -> None:
    """지원되는 의미인데 표현이 서지 않은 자리의 종결. 문구는 **내부 상태에서** 나온다.

    사용자 문구를 여기 한 곳에서만 만드는 이유는, 예전에 같은 요청이 모델이 지어낸 argument
    문자열에 따라 서로 다른 문구로 끝났기 때문이다(실측 2026-08-07).
    """
    # 지연 import — 순환 import 를 피하는 기존 관례를 그대로 따른다(project_resolution_to_plan 과 동일).
    from query_structurer.campaign_plan_v4 import empty_semantic_ir

    write_semantic_ir(
        payload,
        empty_semantic_ir(
            status="needs_clarification",
            missing_fields=["audience.requirement"],
            message="요청한 조건은 지원되는 의미이지만 실행 표현으로 확정되지 않았습니다.",
            failure_kind="system_failure",
            failure_reason=FAILURE_REASON_EMISSION,
        ),
    )
    payload["audience_emission_failures"] = failures


def _dedupe_audience_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        key = (
            item.get("code"), item.get("argument"),
            evidence.get("start"), evidence.get("end"),
        )
        unique.setdefault(key, item)
    return list(unique.values())


def as_of_date(current_date: str | None) -> date | None:
    """계획 시점 기준일. 파싱 불가면 None(컴파일러가 실행 시점으로 폴백)."""
    try:
        return date.fromisoformat(current_date) if current_date else None
    except ValueError:
        return None


def _parse_audience_expression(raw: dict[str, Any], query: str) -> event_ir.Condition:
    """wire dict → 검증된 사건 IR. 형태·근거 위반은 issue 가 아니라 **계약 위반**이다."""
    try:
        expression = event_ir.condition_from_dict(raw)
        event_ir.validate_evidence(expression)
    except (event_ir.IrSchemaError, event_ir.SemanticLossError) as exc:
        raise AudienceValidationError(f"invalid audience expression: {exc}") from exc
    lowering_only = event_ir.lowering_only_nodes(expression)
    if lowering_only:
        # 실행 계획(파생 테이블·윈도 함수·관계 출력 참조)은 낮춤이 만드는 것이다. 모델 표현에서
        # 그것을 받으면 '검증된 의미'와 '검증되지 않은 SQL 모양'이 같은 자리에 섞인다.
        raise AudienceValidationError(
            "audience expression must not contain lowering-only nodes: "
            + ", ".join(lowering_only)
        )
    for atom, _negated in event_ir.iter_signed_atoms(expression):
        evidence = atom.evidence
        if evidence is None or not (
            0 <= evidence.start < evidence.end <= len(query)
            and query[evidence.start:evidence.end] == evidence.text
        ):
            raise AudienceValidationError(
                "audience expression evidence does not match original_query"
            )
    return expression


def _pin_explicit_as_of_rolling_windows(
    expression: event_ir.Condition,
    literals: list[Any],
    query: str,
) -> tuple[event_ir.Condition, list[dict[str, Any]], dict[str, Any] | None]:
    """Resolve an explicitly anchored rolling window to an absolute interval.

    Unanchored ``RollingWindow`` remains execution-clock based.  Only the
    application-extracted ``as_of_date`` role can pin it; neither model output
    nor ``StructuringContext.current_date`` is execution authority for this
    conversion.
    """

    raw = expression.to_dict()
    rolling_nodes: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "rolling":
                rolling_nodes.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw)
    if not rolling_nodes:
        return expression, [], None
    anchors = [
        item
        for item in literals
        if isinstance(item, Mapping) and item.get("kind") == "as_of_date"
    ]
    if not anchors:
        return expression, [], None
    if len(anchors) != 1:
        return expression, [], {
            "code": "ambiguous_requirement",
            "argument": "as_of_date",
            "message": "여러 기준일 중 rolling 기간에 적용할 하나의 기준일을 확정할 수 없습니다.",
            "evidence": {"text": query, "start": 0, "end": len(query)},
        }
    normalized = anchors[0].get("normalized")
    anchor_text = normalized.get("date") if isinstance(normalized, Mapping) else None
    try:
        anchor = date.fromisoformat(str(anchor_text))
    except ValueError:
        return expression, [], {
            "code": "validation_mismatch",
            "argument": "as_of_date",
            "message": "애플리케이션이 추출한 기준일을 유효한 날짜로 해석할 수 없습니다.",
            "evidence": _binding_evidence_for_issue(anchors[0], query),
        }

    records: list[dict[str, Any]] = []
    for node in rolling_nodes:
        try:
            window = event_ir.RollingWindow.from_dict(node)
        except event_ir.IrSchemaError:
            continue
        end_exclusive = anchor + timedelta(days=1)
        interval = event_ir.AbsoluteInterval(
            start=end_exclusive - timedelta(days=window.days),
            end_exclusive=end_exclusive,
        )
        duration_bindings = [
            item
            for item in literals
            if isinstance(item, Mapping)
            and item.get("kind") == "duration"
            and isinstance(item.get("normalized"), Mapping)
            and item["normalized"].get("temporal_kind") == "rolling_duration"
            and item["normalized"].get("value") == window.value
            and event_ir.canonical_unit(item["normalized"].get("semantic_unit"))
            == window.unit
        ]
        duration_literal_id = (
            str(duration_bindings[0].get("id") or "")
            if len(duration_bindings) == 1
            else ""
        )
        node.clear()
        node.update(interval.to_dict())
        records.append({
            "literal_id": str(anchors[0].get("id") or ""),
            "anchor_literal_id": str(anchors[0].get("id") or ""),
            "duration_literal_id": duration_literal_id,
            "anchor_date": anchor.isoformat(),
            "value": window.value,
            "unit": window.unit,
            "start": interval.start.isoformat(),
            "end_exclusive": interval.end_exclusive.isoformat(),
        })
    if not records:
        return expression, [], None
    return _parse_audience_expression(raw, query), records, None


def _binding_evidence_for_issue(binding: Mapping[str, Any], query: str) -> dict[str, Any]:
    start, end = binding.get("start"), binding.get("end")
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
    ):
        return {"text": query[start:end], "start": start, "end": end}
    return {"text": query, "start": 0, "end": len(query)}


def _audience_receipts(expression: event_ir.Condition) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for index, (atom, negated) in enumerate(event_ir.iter_signed_atoms(expression)):
        semantic = atom.to_dict()
        fingerprint = hashlib.sha256(
            json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        receipts.append({
            "node_id": f"audience-atom-{index}",
            "fingerprint": fingerprint,
            "status": "compiled",
            "polarity": "negative" if negated else "positive",
            "sources": sorted(event_ir.sources(atom)),
        })
    return receipts


# ── (a) 검증 ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AudienceResolution:
    """검증이 끝난 오디언스 요구 하나. 투영이 필요로 하는 것만 들고 있다."""

    expression: event_ir.Condition | None
    issues: list[dict[str, Any]]
    # 모델이 **직접 신고한** issue 의 신원. 미지원 선언의 강등 판정은 여기 있는 것에만
    # 적용된다 — 애플리케이션이 계산한 판정까지 뒤집으면 조용한 오답을 막으려던 장치가
    # 그것을 만드는 장치가 된다.
    model_reported: set[tuple[str, str, str]]
    # 결핍 원인 판정이 대조할 원문. 투영이 payload 에서 다시 읽지 않게 여기 싣는다 —
    # 검증이 본 문장과 투영이 보는 문장이 갈리면 근거 구간의 좌표계가 어긋난다.
    query: str = ""
    # 애플리케이션이 소유한 값으로 맞춰 넣은 정규화 기록(창 종류 등).
    normalizations: list[dict[str, Any]] = field(default_factory=list)
    # 구조가 완전히 증명된 rolling absence에서 기간/연산자까지 삼킨 모델 evidence를
    # 사건 부정 구절로 좁힌 기록. 값이나 조건은 바꾸지 않고 근거 소유권만 바로잡는다.
    evidence_normalizations: list[dict[str, Any]] = field(default_factory=list)
    # 사용자 명시 기준일 + rolling duration을 고정 반개구간으로 바꾼 기록.
    as_of_normalizations: list[dict[str, Any]] = field(default_factory=list)
    # 모델이 null 로 돌려준 표현을 애플리케이션 소유 계약이 완전히 증명해 채운 경우의
    # 소유자. 투영 시 결정 로그에 남기며, 검증 issue 가 하나라도 남으면 설정하지 않는다.
    synthesis_owner: str | None = None
    # '캠페인당 평균 구매금액' 판정 근거. 합성이 성립하면 모델의 행당 평균 집계를 캠페인 분모
    # 복합식으로 **바꾼** 기록이고, 성립하지 않으면 표현을 버린 기록이다(둘은 receipt 에
    # numerator/denominator 가 있는지로 구분된다).
    campaign_average_receipt: dict[str, Any] | None = None
    campaign_average_rewritten: bool = False
    # 적재 범위 판정 경고(예: 요청한 관측 칸이 적재 구간 밖). **의미 지원 여부와 다른 축**이라
    # SQL 을 막지 않지만, 남기지 않으면 "SQL 은 나왔는데 0건"의 이유가 응답에 존재하지 않는다.
    coverage_warnings: tuple[str, ...] = ()


def _requirement_from_payload(
    expression: event_ir.Condition, query: str
) -> AudienceRequirement:
    """검증된 표현 하나를 요구 모델로 싣는다.

    모델이 신고한 issue 를 여기 싣지 않는 이유: 그것은 이미 legacy 표기로 확인·보존돼
    있고, 요구 모델로 왕복시키면 같은 사실이 두 표기로 존재하게 된다. 요구 계층에 넘기는
    것은 **검증받을 표현**뿐이다.
    """
    return AudienceRequirement(
        id="audience-requirement",
        version=REQUIREMENT_SCHEMA_VERSION,
        intent=RequirementIntent(kind=IntentKind.FIND),
        expression=ProposedExpression(payload=expression.to_dict()),
        source=RequirementSource(text=query),
        created_at=datetime.now(UTC),
    )


def _run_sync(coroutine: Coroutine[Any, Any, Any]) -> Any:
    """요구 계층의 async 계약을 동기 경로에서 돌린다.

    구조화기는 동기 함수이고 FastAPI 는 그것을 워커 스레드에서 부른다 — 보통은 루프가 없다.
    그렇지 않은 호출자(이미 루프 안)에서도 같은 코드가 돌아야 하므로, 루프가 있으면 별도
    스레드에서 돌린다. 여기서 조용히 실패하면 원인이 'SQL 이 안 나온다'로만 보인다.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _validation_issues(
    expression: event_ir.Condition,
    query: str,
    literals: list[Any],
    *,
    current_date: str | None,
    scalar_literal_spans: Sequence[tuple[int, int]] = (),
) -> list[dict[str, Any]]:
    """요구 계층에 검증기를 주입해 돌리고, 결과를 기존 issue 표기로 되돌린다."""
    resolver = DefaultRequirementResolver(
        validators=audience_validators(
            as_of=as_of_date(current_date), scalar_literal_spans=scalar_literal_spans
        )
    )
    context = RequirementResolutionContext(
        timezone=_TIMEZONE,
        locale=_LOCALE,
        schema_registry=StaticSchemaRegistry(),
        capability_profile=event_ir_capability_profile(),
        literals=tuple(literals),
    )
    result = _run_sync(
        resolver.resolve(_requirement_from_payload(expression, query), context)
    )
    if result.status == "ready":
        return []
    issues: list[dict[str, Any]] = []
    for issue in result.issues:
        code, argument = report_from_issue(issue)
        evidence = issue.evidence
        issues.append({
            "code": code,
            "argument": argument,
            "message": issue.message,
            "evidence": (
                {"text": evidence.text, "start": evidence.start, "end": evidence.end}
                if evidence is not None
                else {"text": query, "start": 0, "end": len(query)}
            ),
        })
    return issues


@dataclass(frozen=True)
class _ApplicationOwnedSynthesis:
    expression: event_ir.Condition
    owner: str
    # 이 합성이 방면하는 신고들. 첫 항목이 앵커(합성기가 자기 규칙으로 검증한 신고)이고,
    # 나머지는 합성 구간으로 설명된 신고다.
    issue_keys: tuple[tuple[str, str, str], ...]
    # 합성이 **스칼라 임계값으로** 소비한 원문 구간. 기간처럼 보이지만 창이 아닌 리터럴
    # ('구매주기가 30일 이하'의 '30일')을 시간 검증기가 소실된 창으로 세지 않게 한다.
    scalar_literal_spans: tuple[tuple[int, int], ...] = ()
    # 이 합성이 **원문에서 소유했다고 선언하는** 구간. 앵커 밖의 신고를 설명할 수 있는 근거는
    # 이것뿐이다. 선언하지 않는 합성기는 앵커 하나만 방면한다(종전 동작).
    accounted_spans: tuple[tuple[int, int], ...] = ()
    # 낮춤이 낸 적재 범위 경고. 조건을 막지는 않지만 응답까지 나가야 한다.
    coverage_warnings: tuple[str, ...] = ()


def _issue_evidence_contains(
    issue: Mapping[str, Any], start: int, end: int
) -> bool:
    evidence = issue.get("evidence")
    return bool(
        isinstance(evidence, Mapping)
        and isinstance(evidence.get("start"), int)
        and isinstance(evidence.get("end"), int)
        and int(evidence["start"]) <= start < end <= int(evidence["end"])
    )


def _accounted_issue_keys(
    issues: Sequence[Mapping[str, Any]],
    anchor: Mapping[str, Any],
    synthesis: _ApplicationOwnedSynthesis,
) -> tuple[tuple[str, str, str], ...] | None:
    """앵커 밖의 신고가 **전부** 합성 구간으로 설명되면 그 신고들의 신원. 아니면 ``None``.

    모델은 같은 의미 실패를 자주 여러 신고로 쪼갠다(실측 2026-08-08: 시간 절 하나에 대해
    ``unsupported_semantics(member_state_history)`` + ``ambiguous_requirement(subject.grade)``).
    구제 여부를 신고 **개수**로 정하면 그 쪼갬이 곧 실패가 되어, 같은 요청이 회차마다
    다른 귀결로 끝난다. 기준은 개수가 아니라 "합성이 신고된 자리를 설명하는가"다.

    설명의 정의는 앵커에 쓰는 것과 같다(:func:`_issue_evidence_contains`) — 신고가 가리킨
    구간 안에 합성이 소유한 구간이 있어야 한다. 하나라도 설명되지 않으면 부분 방면 대신
    전부 포기한다: 설명되지 않은 절이 남은 채 표현이 서면 그 절이 사라진 SQL 이 나간다.
    """

    anchor_key = _audience_issue_key(anchor)
    accounted: list[tuple[str, str, str]] = [anchor_key]
    for issue in issues:
        key = _audience_issue_key(issue)
        if key == anchor_key:
            continue
        if not any(
            _issue_evidence_contains(issue, start, end)
            for start, end in synthesis.accounted_spans
        ):
            return None
        accounted.append(key)
    return tuple(accounted)


def _application_owned_synthesis(
    query: str,
    issues: list[dict[str, Any]],
    literal_bindings: list[Any],
    *,
    current_date: str | None = None,
) -> _ApplicationOwnedSynthesis | None:
    """Resolve the model's false issues through a complete structural contract.

    This path never combines a synthesized atom with an unknown remainder: one
    synthesis must account for every reported issue, its evidence must contain
    the owned literal, and the generated expression still goes through all
    ordinary audience validators before those issues can be discharged.

    합성기 우선순위는 :func:`_synthesis_for_issue` 가 소유하고, 여기서는 어느 신고를
    앵커로 볼지만 원문 순서대로 시도한다 — 둘 다 결정론이라 같은 입력은 같은 합성을 고른다.
    """

    for anchor in issues:
        synthesis = _synthesis_for_issue(
            query, anchor, literal_bindings, current_date=current_date
        )
        if synthesis is None:
            continue
        issue_keys = _accounted_issue_keys(issues, anchor, synthesis)
        if issue_keys is None:
            continue
        return replace(synthesis, issue_keys=issue_keys)
    return None


def _synthesis_for_issue(
    query: str,
    issue: Mapping[str, Any],
    literal_bindings: list[Any],
    *,
    current_date: str | None = None,
) -> _ApplicationOwnedSynthesis | None:
    """이 신고 하나를 앵커로 삼는 합성. 합성기 순서가 계약이다(바꾸지 않는다)."""

    code, argument = issue.get("code"), issue.get("argument")

    import audience_runtime

    catalog = audience_runtime.catalog_snapshot()
    if code in {"ambiguous_requirement", "unsupported_semantics"}:
        # **진입 조건은 의미의 종류이지 모델이 쓴 문자열이 아니다.** 예전에는
        # ``argument == "consent_count"`` 를 요구했고, 그 문자열은 모델 산문이라 같은 요청이
        # 어휘 운에 따라 열리거나 닫혔다(감사 #47). 지금 묻는 것은 둘이다: 원문에 typed
        # 카디널리티 주장이 있는가, 그 주장의 도메인이 동의 채널인가.
        claim = consent_cardinality.detect_cardinality_claim(query, catalog)
        if (
            claim is not None
            and claim.domain == consent_cardinality.CONSENT_CHANNEL_DOMAIN
            # 공유 근거로 판정한다 — 세 멤버가 각자 스팬을 가질 필요가 없다. 그 어구 전체가
            # 하나의 집합 술어를 증명한다.
            and _issue_evidence_contains(issue, *claim.quantifier_span)
        ):
            expression = consent_cardinality.synthesize_exact_consent_cardinality(
                query, literal_bindings, catalog
            )
            if expression is not None:
                return _ApplicationOwnedSynthesis(
                    expression,
                    "consent_cardinality.exact_truth_table",
                    (_audience_issue_key(issue),),
                    accounted_spans=(claim.footprint,),
                )

    if code in {"ambiguous_requirement", "unsupported_semantics"}:
        # 캠페인 분모 평균. 모델은 '캠페인별'을 모호로 신고하지만(실측 2026-08-06) 그 집계
        # 수준은 카탈로그가 하나로 선언하고 있으므로, 선언이 완전하고 문장이 이 조건
        # 하나뿐이면 애플리케이션이 그 신고를 반박한다. 인자 문자열로 라우팅하지 않는
        # 이유는 다른 합성기와 같다 — 그 문자열은 모델 산문이다.
        synthesis = _campaign_average_synthesis(query, issue, literal_bindings)
        if synthesis is not None:
            return synthesis

    if code == "unsupported_semantics" and argument == "comparison_operator":
        expression = rolling_absence_claims.synthesize_rolling_absence(
            query, literal_bindings, catalog
        )
        if expression is not None:
            consumed = rolling_absence_claims.consumed_literal_binding_indices(
                query, expression, literal_bindings, catalog
            )
            operator_bindings = [
                binding
                for index, binding in enumerate(literal_bindings)
                if index in consumed
                and isinstance(binding, Mapping)
                and binding.get("kind") == "comparison_operator"
                and isinstance(binding.get("start"), int)
                and isinstance(binding.get("end"), int)
            ]
            if len(operator_bindings) == 1 and _issue_evidence_contains(
                issue,
                int(operator_bindings[0]["start"]),
                int(operator_bindings[0]["end"]),
            ):
                return _ApplicationOwnedSynthesis(
                    expression,
                    "rolling_absence.not_exists_window",
                    (_audience_issue_key(issue),),
                )

    if code == "unsupported_semantics":
        # 회원별 스칼라 지표 임계(구매주기·누적 구매금액 …). issue 인자 문자열로 라우팅하지
        # 않는 이유: 그 문자열은 모델 산문이라 지표마다 달라지고, 실제 안전장치는 합성기가
        # 요구하는 **닫힌 문형 + 카탈로그 계약 + 근거 구간 포함**이다. 라우팅을 인자에 걸면
        # 표현할 수 있는 요청이 모델 어휘 때문에 닫힌다.
        synthesis = _member_scalar_synthesis(query, issue, literal_bindings)
        if synthesis is not None:
            return synthesis
        # 시간·이력 조건(시점 값·전이·기간 전칭 …). 같은 이유로 인자 문자열을 보지 않고,
        # 합성이 성립하는 조건은 **원문 근거가 issue 근거 안에 있을 것**뿐이다.
        synthesis = _temporal_synthesis(query, issue, current_date=current_date)
        if synthesis is not None:
            return synthesis
        # 기간 대 기간 변화(크기 포함). 계획이 요구를 다 소비했을 때만 서므로, 여기 도달한
        # 합성은 '10% 이상'까지 실린 표현이다 — 크기가 빠진 형상은 애초에 계획이 없다.
        synthesis = _change_comparison_synthesis(query, issue, current_date=current_date)
        if synthesis is not None:
            return synthesis
        # 절 의무(부재·칸별 발생·창을 가진 존재). 마지막에 두는 이유는 위 합성기들이 자기
        # 축에 대해 더 좁은 증명을 갖고 있기 때문이다 — 넓은 판정이 좁은 증명을 가로채면
        # 그 축이 가진 계약(진리표·리터럴 정산)이 우회된다.
        synthesis = _clause_plan_synthesis(
            query, issue, literal_bindings, current_date=current_date
        )
        if synthesis is not None:
            return synthesis

    if code == "missing_argument" and argument == "period":
        # 맨 '최근'에 대한 기간 결핍 신고. 구조화기는 규칙대로 신고했지만, 그 절이 스스로 창을
        # 확정하는 조건('등급이 승급한')이면 결핍이 아니다 — 판정은 합성이 한다.
        #
        # 이 갈래가 없던 동안, 같은 문장이 모델의 신고 코드에 따라 갈렸다(실측 #17:
        # unsupported_semantics 로 신고되면 되살아나고 missing_argument(period) 로 신고되면
        # 되묻기). 귀결이 방출 편차를 따라가면 안 되므로 두 코드가 같은 합성에 도달한다.
        synthesis = _temporal_synthesis(query, issue, current_date=current_date)
        if synthesis is not None:
            return synthesis
    return None


def _temporal_outcome(query: str, *, current_date: str | None = None) -> Any:
    """시간·이력 절의 판정 결과(합성 / 반려 / 해당 없음). 판정의 소유자는 :mod:`temporal_claims` 다.

    두 소비자가 같은 판정을 봐야 한다 — 하나는 그 절을 되살리고(합성), 하나는 그 절이 왜 막혔는지
    사용자에게 말한다(반려). 각자 부르면 같은 문장이 서로 다른 사유로 설명될 수 있다.
    """

    import audience_runtime  # noqa: PLC0415 - 지연 import(순환 방지)
    import temporal_claims  # noqa: PLC0415
    import temporal_ir  # noqa: PLC0415

    catalog = audience_runtime.resolve_audience_catalog()
    snapshot = audience_runtime.catalog_snapshot()
    try:
        runtime = temporal_ir.create_temporal_runtime(catalog)
    except temporal_ir.TemporalCatalogError:
        # 선언을 읽지 못하는 것은 '해당 없음'이 아니다. 그러나 이 경로의 결말은 어차피
        # 모델 신고 유지(SQL 없음)이므로, 판정 불가를 통과로 바꾸지 않고 그대로 둔다.
        return None
    return temporal_claims.synthesize_temporal_claim(
        query,
        snapshot=snapshot,
        catalog=catalog,
        runtime=runtime,
        context=temporal_claims.request_context_for(current_date, timezone=_TIMEZONE),
    )


def temporal_claims_owner() -> str:
    """반려 사유를 만든 판정 계층의 이름(진단 payload 에 그대로 실린다)."""

    import temporal_claims  # 지연 import(순환 방지)

    return temporal_claims.OWNER


def _declared_temporal_rejection(
    query: str, unsupported: Sequence[Mapping[str, Any]]
) -> Any:
    """이 신고들이 가리키는 절에 대해 판정 계층이 **선언한** 반려(없으면 ``None``).

    근거 구간이 신고 안에 있을 것을 요구하는 이유는 다른 절의 사유로 이 신고를 설명하지 않기
    위해서다 — 한 문장에 시간 절과 다른 절이 함께 있을 때 엉뚱한 문구가 나가면 사용자는 고칠
    곳을 찾지 못한다.
    """

    import temporal_claims  # 지연 import(순환 방지)

    try:
        outcome = _temporal_outcome(query)
    # 판정 자체를 할 수 없으면 사유를 지어내지 않는다(범용 문구가 그대로 남는다).
    except Exception:
        return None
    if not isinstance(outcome, temporal_claims.TemporalClaimRejection):
        return None
    evidence = outcome.evidence
    start, end = evidence.get("start"), evidence.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if not any(_issue_evidence_contains(item, start, end) for item in unsupported):
        return None
    return outcome


def _temporal_synthesis(
    query: str, issue: Mapping[str, Any], *, current_date: str | None = None
) -> _ApplicationOwnedSynthesis | None:
    """모델이 표현하지 못한 시간·이력 절을 canonical Temporal IR 로 되살린다.

    ``None`` 을 돌려주는 세 경우를 구분하지 않는 것은 의도다 — 시간 조건이 없거나, 근거가
    어긋나거나, 낮출 수 없으면 **모델의 미지원 신고가 그대로 남는다**. 그래야 절이 조용히
    사라진 성공이 생기지 않는다(부분 SQL 금지).
    """

    import temporal_claims  # 지연 import(순환 방지)

    outcome = _temporal_outcome(query, current_date=current_date)
    if not isinstance(outcome, temporal_claims.TemporalClaimSynthesis):
        return None
    # 합성의 근거가 모델이 신고한 구간 안에 있어야 그 신고를 반박할 수 있다.
    #
    # 기간 결핍 신고만 예외다. 그 신고는 조건 절이 아니라 **그 옆의 시간 낱말**('최근')에
    # 붙으므로 포함 관계로는 영원히 만나지 않는다 — 같은 절인지로 묻고, 그 절이 실제로
    # 낮춰졌다는 사실(이 합성)이 곧 "기간은 결핍이 아니다"의 근거다.
    if not any(
        _issue_evidence_contains(issue, start, end) for start, end in outcome.spans
    ) and not audience_issue_contract.bare_period_issue_owned_by_spans(
        query, issue, outcome.spans
    ):
        return None
    return _ApplicationOwnedSynthesis(
        outcome.expression,
        temporal_claims.OWNER,
        (_audience_issue_key(issue),),
        scalar_literal_spans=outcome.spans,
        # 시간 합성은 자기가 읽은 원문 구간(표지·값·기간)을 전부 안다 — 그래서 같은 절을
        # 두고 쪼개진 다른 신고도 이 구간으로 설명할 수 있다.
        accounted_spans=outcome.spans,
        coverage_warnings=tuple(outcome.warnings or ()),
    )


def _settle_threshold_grain(
    query: str, expression: event_ir.Condition, payload: dict[str, Any]
) -> event_ir.Condition:
    """임계값의 **적용 grain** 을 원문이 말한 대로 맞춘다.

    같은 금액·기간·연산자인데 뜻이 다른 두 문장이 있다(실측 2026-08-08, 라이브 id 42/43)::

        …총 구매금액이 30만원 이상인 회원          회원별 합계   9,585명
        …구매금액이 30만원 이상인 주문을 한 회원    그런 주문 존재   688명

    두 문장은 **바이트 동일한 SQL** 을 냈다. 모델이 고른 트리 모양이 곧 grain 이었고, 그
    선택이 원문과 맞는지 보는 자리가 없었기 때문이다.

    여기서는 원문이 grain 을 **명시했을 때만** 개입한다. 명시가 없으면(``2019년에 이십만원
    이상을 구매한 고객`` 처럼 원문이 실제로 모호한 자리) 아무것도 바꾸지 않는다 — 모호한
    자리에 기본값을 넣는 것이 곧 추측이다(§12). 낮추지 못하는 모양도 그대로 둔다: 여기서
    fail-close 하면 지금 나가던 SQL 이 사라진다.

    바꾼 사실은 :mod:`plan_decisions` 에 남는다. 애플리케이션이 소유한 결정론 수정은
    응답의 ``decisions`` 로 드러나야 한다 — 조용히 바꾸면 검증기가 자기 SQL 을 근거 없는
    조건으로 되잡는다.
    """
    import grain_claims  # 지연 import(순환 방지)

    try:
        conflict = grain_claims.conflicting_grain(query, expression)
    except Exception:  # 주장을 못 세우면 개입하지 않는다(추측 금지 · 요청을 막지 않는다)
        return expression
    if conflict is None:
        return expression
    claim, realized = conflict
    if claim.grain != grain_claims.ROW:
        # subject 주장인데 트리가 row 인 경우는 아직 낮추지 않는다 — 행 임계를 합계로
        # 올리는 변환은 모집단 정의가 필요해서 이 자리에서 추측할 수 없다.
        return expression
    regrained = grain_claims.regrain_to_row(expression)
    if regrained is None:
        return expression
    plan_decisions.record(
        payload,
        filter_name="grain_claims",
        action=plan_decisions.SET,
        slot=f"{EVENT_EXPRESSION_KEY}.grain",
        reason="threshold_grain_declared_by_source_text",
        value=claim.grain,
        evidence=claim.evidence_text,
        realized_before=sorted(realized),
    )
    return regrained


def _change_comparison_synthesis(
    query: str, issue: Mapping[str, Any], *, current_date: str | None = None
) -> _ApplicationOwnedSynthesis | None:
    """모델이 표현하지 못한 기간 대 기간 변화를 canonical Event IR 로 되살린다.

    :func:`_temporal_synthesis` 와 **같은 계약**이다. 다른 점은 하나뿐인데 그게 이 축의 핵심이다:
    계획은 **자기 구간의 요구를 전부 소비했을 때만** 존재한다(:func:`lowering_planner.
    unsettled_requirements`). 그래서 '10% 이상'이 빠진 형상을 애플리케이션이 스스로 출고하는
    일이 구조적으로 생기지 않는다 — 크기를 못 낮추면 계획이 없고, 계획이 없으면 합성도 없고,
    모델의 미지원 신고가 그대로 남아 fail-close 한다.

    같은 구간에 계획이 둘이면(카탈로그가 같은 표면어에 지표를 둘 선언) 아무것도 내지 않는다.
    어느 쪽을 고르든 절반은 틀린 오디언스이므로 고르지 않는 것이 옳다.
    """

    import lowering_planner  # 지연 import(순환 방지)

    try:
        plans = [
            plan
            for plan in lowering_planner.plans_for_query(query, today=as_of_date(current_date))
            if plan.obligation.kind == lowering_planner.AGGREGATE_COMPARISON
            and getattr(plan.obligation, "threshold", None) is not None
            and not lowering_planner.unsettled_requirements(query, plan)
        ]
    except Exception:  # 계획을 못 세우면 합성하지 않는다(추측 금지)
        return None
    matched = [
        plan
        for plan in plans
        if _issue_evidence_contains(issue, *plan.obligation.source_span)
        or _issue_evidence_contains(issue, *plan.obligation.threshold.source_span)
    ]
    if len(matched) != 1:
        return None
    plan = matched[0]
    same_span = [
        candidate
        for candidate in plans
        if tuple(candidate.obligation.source_span) == tuple(plan.obligation.source_span)
    ]
    if len({item.sql for item in same_span}) != 1:
        return None  # 같은 자리에 서로 다른 지표 — 고르지 않는다
    spans = (tuple(plan.obligation.source_span),)
    return _ApplicationOwnedSynthesis(
        plan.expression,
        "lowering_planner.aggregate_comparison",
        (_audience_issue_key(issue),),
        scalar_literal_spans=spans,
        accounted_spans=spans,
    )


def _temporal_clause_already_compiled(
    query: str,
    expression: event_ir.Condition,
    synthesis: _ApplicationOwnedSynthesis,
    *,
    current_date: str | None,
) -> bool:
    """모델 표현이 이 합성의 절을 **이미** 낮춰 두었는가.

    모델이 canonical 형상을 스스로 내면서 같은 절을 미지원으로도 신고하는 모양이 있다.
    그 신고만 보고 결합하면 같은 조건이 두 번 들어가고(``A ∧ A``), 조건 커버리지가 그 중복을
    미귀결로 읽어 **옳은 SQL 이 막힌다**(실측 2026-08-08 라이브 #20: 3회 중 2회 차단).
    뜻은 같으므로 결합하지 않고 신고만 방면하는 것이 정확하다.

    판정은 여기서도 낮춤이 한다 — 표현이 실제로 그 구간을 컴파일했는지 물어보고, 합성이
    읽은 구간이 전부 그 안에 들어 있을 때만 '이미 있다'로 본다(부분 겹침은 결합한다).
    """

    if not synthesis.accounted_spans:
        return False
    compiled = canonical_audience_claims.temporal_obligation_compiled_spans(
        query, expression, today=as_of_date(current_date)
    )
    if not compiled:
        return False
    return all(
        any(start <= span_start and span_end <= end for start, end in compiled)
        for span_start, span_end in synthesis.accounted_spans
    )


def _conjoinable_synthesis(
    query: str,
    issues: list[dict[str, Any]],
    literal_bindings: Sequence[Mapping[str, Any]] = (),
    *,
    current_date: str | None = None,
) -> _ApplicationOwnedSynthesis | None:
    """모델 표현과 **결합할 수 있는** 합성 하나. 없으면 ``None``(신고가 그대로 남는다).

    결합을 시간 축으로 제한하는 것은 의도다. 시간 조건은 자기 근거 구간을 정확히 소유하고
    (낮춘 원자가 그 구간을 그대로 들고 있다) 낮춤이 전부-또는-아무것도이므로, 결합해도
    '어느 절이 어디서 왔는가'가 흐려지지 않는다. 다른 축까지 한꺼번에 열면 그 성질이
    보장되지 않는 합성이 모델 표현과 섞인다.
    """

    for anchor in issues:
        if anchor.get("code") != "unsupported_semantics":
            continue
        synthesis = _temporal_synthesis(query, anchor, current_date=current_date)
        if synthesis is None:
            # 절 의무도 같은 성질을 갖는다 — 낮춘 원자가 자기 근거 구간을 그대로 들고 있고,
            # 계획이 서지 않으면 아무 조각도 만들지 않는다(전부-또는-아무것도).
            synthesis = _clause_plan_synthesis(
                query, anchor, literal_bindings, current_date=current_date
            )
        if synthesis is None:
            continue
        # 결합 갈래에서도 규칙은 같다 — 남은 신고가 전부 이 합성으로 설명돼야 한다.
        # 하나라도 남으면 결합해서는 안 된다(설명되지 않은 절이 사라진 SQL 이 나간다).
        issue_keys = _accounted_issue_keys(issues, anchor, synthesis)
        if issue_keys is None:
            continue
        return replace(synthesis, issue_keys=issue_keys)
    return None


def _clause_plan_synthesis(
    query: str,
    issue: Mapping[str, Any],
    literal_bindings: Sequence[Mapping[str, Any]] = (),
    *,
    current_date: str | None = None,
) -> _ApplicationOwnedSynthesis | None:
    """판정자가 **실제로 낮춘 절**로 모델의 미지원 신고를 되살린다.

    라우팅에 모델의 argument 문자열을 쓰지 않는 이유는 이 저장소가 이미 그 대가를 치렀기
    때문이다 — 같은 요청이 모델 어휘에 따라 열리기도 닫히기도 했다. 여기서 보는 것은 둘뿐이다:
    판정자가 그 자리를 낮췄는가, 그 계획의 근거가 신고 구간 안에 있는가.

    이 갈래가 감사 B(거짓 미지원)를 구조적으로 닫는다. ``최근 90일 동안 주문하지 않은`` ·
    ``최근 3개월 동안 매월 한 번 이상 구매한`` 은 결정론 경로에서 정확한 SQL 이 나오는데도
    "표현할 수 없다"로 끝나고 있었다 — 낮출 수 있다는 사실이 종결 판정에 도달하지 못했다.
    """

    import lowering_planner  # 지연 import(순환 방지)

    evidence = issue.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    start, end = evidence.get("start"), evidence.get("end")
    if not (isinstance(start, int) and isinstance(end, int)):
        return None
    resolution = lowering_planner.resolve_executable(query, today=as_of_date(current_date))
    if not isinstance(resolution, lowering_planner.Executable):
        return None
    for plan in resolution.plans:
        if not isinstance(plan.obligation, lowering_planner.ClauseObligation):
            continue
        owned = [plan.obligation.source_span]
        temporal = plan.obligation.clause.temporal
        if temporal is not None and temporal.span is not None:
            owned.append(temporal.span)
        # 계획이 **소비한 구간 전부**가 신고 안에 있어야 그 신고를 반박할 수 있다. 조건 절만
        # 보고 방면하면 그 옆의 창을 신고하지 않은 모델 응답에서도 창이 조용히 실려 나간다 —
        # 이 저장소가 이미 세워 둔 계약(합성은 자기가 읽은 리터럴을 전부 소유해야 한다)이다.
        if not (start <= min(item[0] for item in owned)
                and max(item[1] for item in owned) <= end):
            continue
        if canonical_audience_claims.literal_claim_issues(
            query, plan.expression, literal_bindings
        ):
            # 이 계획이 원문 리터럴을 전부 읽지 못했다 — 그 자리는 **더 좁은 증명을 가진**
            # 합성기(구조 영수증·진리표)의 것이다. 여기서 통과시키면 읽지 않은 리터럴이 조용히
            # 사라진 SQL 이 나간다(부분 SQL 금지).
            continue
        return _ApplicationOwnedSynthesis(
            plan.expression,
            f"lowering_planner.{plan.obligation.kind}",
            (_audience_issue_key(issue),),
            accounted_spans=tuple(owned),
        )
    return None


def _member_scalar_synthesis(
    query: str, issue: Mapping[str, Any], literal_bindings: list[Any]
) -> _ApplicationOwnedSynthesis | None:
    import audience_runtime
    import member_scalar_metric_claims

    registry_path = audience_runtime.member_metric_registry_snapshot()
    if registry_path is None:
        return None
    result = member_scalar_metric_claims.synthesize_member_scalar_predicate(
        query,
        issue,
        literal_bindings,
        registry_path,
        audience_runtime.resolve_audience_catalog(),
    )
    if result is None:
        return None
    consumed = {
        str(binding_id)
        for binding_id in result.receipt.get("consumed_literal_binding_ids") or ()
    }
    spans = tuple(
        (int(binding["start"]), int(binding["end"]))
        for binding in literal_bindings
        if isinstance(binding, Mapping)
        and str(binding.get("id")) in consumed
        and isinstance(binding.get("start"), int)
        and isinstance(binding.get("end"), int)
    )
    return _ApplicationOwnedSynthesis(
        result.expression,
        member_scalar_metric_claims.OWNER,
        (_audience_issue_key(issue),),
        scalar_literal_spans=spans,
    )


def _claimed_scalar_threshold_spans(
    query: str, expression: event_ir.Condition, literal_bindings: list[Any]
) -> tuple[tuple[int, int], ...]:
    """최종 표현에서 역산한 스칼라 임계 구간 — **표현의 생산자와 무관하다**.

    합성기가 넘겨주는 :attr:`_ApplicationOwnedSynthesis.scalar_literal_spans` 는 합성이 실제로
    일어난 갈래에만 있다. 모델이 같은 조건을 스스로 표현하면(``expression≠None, issues=[]``)
    그 지식이 통째로 사라지고, '구매주기가 30일 이하'의 ``30일`` 이 소실된 기간 창으로 세어져
    옳은 요청이 반려된다. 그래서 여기서 다시 계산한다.

    카탈로그·레지스트리 적재 실패(:class:`audience_runtime.AudienceCatalogLoadError`)는 잡지
    않는다 — 같은 경로의 다른 소비자(:func:`_campaign_average_claim`, 근거 정규화, 카탈로그
    검증기)가 모두 전파하므로 실패 표면을 하나로 유지한다. 레지스트리 선언 자체가 없는
    배포는 예외가 아니라 ``None`` 이고, 그때는 청구하지 않는다(fail-close).
    """

    import audience_runtime
    import member_scalar_metric_claims

    registry = audience_runtime.member_metric_registry_snapshot()
    if registry is None:
        return ()
    return member_scalar_metric_claims.consumed_scalar_threshold_spans(
        query,
        expression,
        literal_bindings,
        registry,
        audience_runtime.resolve_audience_catalog(),
    )


def _campaign_average_synthesis(
    query: str, issue: Mapping[str, Any], literal_bindings: list[Any]
) -> _ApplicationOwnedSynthesis | None:
    """모델이 표현을 비운 캠페인 분모 평균 문장을 카탈로그 선언만으로 세운다.

    :func:`_campaign_average_claim` 과 짝이다 — 저쪽은 모델이 낸 행당 평균을 **고치고**,
    이쪽은 모델이 아무것도 내지 않았을 때 같은 선언으로 **세운다**. 성립하지 않으면 ``None``
    이고 모델의 신고가 그대로 남아 fail-close 한다(비슷한 지표로 갈아타지 않는다).
    """

    import audience_runtime

    result = campaign_metric_claims.synthesize_campaign_average_predicate(
        query, issue, literal_bindings, audience_runtime.catalog_snapshot()
    )
    if result is None:
        return None
    return _ApplicationOwnedSynthesis(
        result.expression,
        campaign_metric_claims.DECLARED_SYNTHESIS_OWNER,
        (_audience_issue_key(issue),),
        scalar_literal_spans=result.consumed_spans,
    )


def _campaign_average_claim(
    query: str, raw_expression: Any, literal_bindings: Any
) -> dict[str, Any] | None:
    """'캠페인당 평균 구매금액'을 행당 평균으로 위장한 표현을 잡아 정확한 식으로 바꾼다.

    바꾸지 않고 그대로 두면 같은 문장이 **반응 행당 평균**(``AVG(BUY_AMT)``)으로 조용히
    실행된다 — 캠페인 수로 나눈 평균과 값이 다른데 둘 다 성공으로 보인다. 판정과 합성은
    카탈로그 선언(:mod:`campaign_metric_claims`)이 하고, 여기서는 그 결과를 표현 교체 또는
    fail-close 로 옮긴다.

    카탈로그를 읽지 못하면 판정을 할 수 없고, **판정 불가는 '해당 없음'이 아니다**.
    예전에는 :class:`audience_runtime.AudienceCatalogLoadError` 를 삼키고 ``None`` 을 돌려줬는데
    (실측 2026-08-05: 주입 시 행당 평균 SQL 출고), 그러면 근거 부재가 곧 통과가 된다.
    그래서 예외를 전파한다 — 이 모듈의 다른 카탈로그 소비자(합성 판정·근거 정규화)도 같은
    예외를 전파하므로 실패 표면이 하나로 유지된다.
    """

    import audience_runtime

    return campaign_metric_claims.detect_campaign_average_claim(
        query, raw_expression, literal_bindings, audience_runtime.catalog_snapshot()
    )


def run_audience_resolver(
    payload: dict[str, Any], query: str, *, current_date: str | None
) -> AudienceResolution | None:
    """오디언스 계약을 검증한다. 계약 자체가 없으면 ``None``(이 계층은 의미를 청구하지 않는다)."""
    requirement = payload.get(AUDIENCE_REQUIREMENT_KEY)
    if not isinstance(requirement, dict):
        return None
    raw_issues = requirement.get("issues")
    if not isinstance(raw_issues, list):
        raise AudienceValidationError("audience_requirement.issues must be an array")
    issues = [validate_audience_issue(item, query) for item in raw_issues]
    model_reported = {_audience_issue_key(item) for item in issues}

    raw_expression = requirement.get("expression")
    literal_bindings = payload.get("literal_bindings")
    # 캠페인 분모 평균 판정을 **먼저** 하는 이유는 하나다 — 그대로 두면 뜻이 다른 SQL(행당
    # 평균)이 성공으로 나간다. 합성이 성립하면 그 자리를 정확한 복합식으로 바꾸고, 성립하지
    # 않으면 표현을 버린다(조용한 대체 금지).
    campaign_average = _campaign_average_claim(query, raw_expression, literal_bindings)
    campaign_average_receipt: dict[str, Any] | None = None
    campaign_average_rewritten = False
    if campaign_average is not None:
        campaign_average_receipt = dict(campaign_average["receipt"])
        synthesized = campaign_average.get("expression")
        if isinstance(synthesized, dict):
            raw_expression = synthesized
            campaign_average_rewritten = True
        else:
            raw_expression = None
            issues = [
                *issues,
                {
                    "code": "unsupported_semantics",
                    "argument": str(campaign_average["argument"]),
                    "message": str(campaign_average["message"]),
                    "evidence": dict(campaign_average["evidence"]),
                },
            ]
    synthesis: _ApplicationOwnedSynthesis | None = None
    if raw_expression is None and issues and isinstance(literal_bindings, list):
        synthesis = _application_owned_synthesis(
            query, issues, literal_bindings, current_date=current_date
        )
        if synthesis is not None:
            raw_expression = synthesis.expression.to_dict()
    elif isinstance(raw_expression, dict) and issues:
        # **혼합 문장**: 모델이 일부 절만 표현하고 나머지를 미지원으로 신고한 모양.
        # 표현이 있다는 이유로 합성을 건너뛰면 그 문장은 영원히 막히고(전 절이 컴파일
        # 가능한데도), 반대로 신고를 무시하면 절이 조용히 사라진 SQL 이 나간다.
        # 그래서 신고된 절을 합성해 **결합**하고, 결합 결과는 이후의 모든 검증을 그대로
        # 통과해야 한다 — 합성이 실패하면 신고가 남아 문장 전체가 막힌다.
        conjunct = _conjoinable_synthesis(
            query, issues, literal_bindings or (), current_date=current_date
        )
        if conjunct is not None:
            synthesis = conjunct
            model_expression = _parse_audience_expression(raw_expression, query)
            # 결합은 **없는 절을 더할 때만** 한다. 모델이 그 절을 이미 낮춰 놨으면 같은
            # 조건이 두 번 들어가고, 그 중복이 조건 커버리지에서 미귀결로 읽힌다.
            if not _temporal_clause_already_compiled(
                query, model_expression, conjunct, current_date=current_date
            ):
                raw_expression = event_ir.And(
                    operands=(model_expression, conjunct.expression)
                ).to_dict()
    expression: event_ir.Condition | None = None
    normalizations: list[dict[str, Any]] = []
    evidence_normalizations: list[dict[str, Any]] = []
    as_of_normalizations: list[dict[str, Any]] = []
    synthesis_owner: str | None = None
    if isinstance(raw_expression, dict):
        if not isinstance(literal_bindings, list):
            raise AudienceValidationError(
                "application-owned literal_bindings must be attached before audience validation"
            )
        # 창의 **종류**('최근 N일' 길이 / 'N단위 전' 시점)는 표면 문법을 읽은 애플리케이션이
        # 소유한다. 반려로만 처리하면 옳은 조건을 만들 수 있는 요청이 재시도 예산을 태우고
        # 실패하므로, 소유한 값으로 맞춰 넣고 무엇을 고쳤는지 남긴다.
        normalizations = canonical_audience_claims.apply_window_kinds(
            raw_expression, literal_bindings
        )
        import audience_runtime

        evidence_correction = rolling_absence_claims.normalize_rolling_absence_evidence(
            query,
            raw_expression,
            literal_bindings,
            audience_runtime.catalog_snapshot(),
        )
        if evidence_correction is not None:
            evidence_normalizations.append(evidence_correction)
        expression = _parse_audience_expression(raw_expression, query)
        # 합성 부산물과 식 역산을 **합집합**으로 쓴다. 치환하면 합성 갈래가 이미 증명해 둔
        # 구간이 사라질 수 있고(역산이 닫는 문형이 더 좁다), 역산만 빼면 모델이 표현을 낸
        # 갈래에서 스칼라 임계값이 다시 소실된 창으로 세어진다.
        scalar_literal_spans = tuple(
            sorted(
                {
                    *(synthesis.scalar_literal_spans if synthesis is not None else ()),
                    *_claimed_scalar_threshold_spans(
                        query, expression, literal_bindings
                    ),
                }
            )
        )
        calculated = _validation_issues(
            expression,
            query,
            literal_bindings,
            current_date=current_date,
            scalar_literal_spans=scalar_literal_spans,
        )
        if not calculated:
            expression, as_of_normalizations, as_of_issue = (
                _pin_explicit_as_of_rolling_windows(
                    expression, literal_bindings, query
                )
            )
            if as_of_issue is not None:
                calculated.append(as_of_issue)
        if synthesis is not None and not calculated:
            # 합성이 설명한 신고를 **전부** 방면한다. 앵커 하나만 지우면 같은 절을 두고
            # 쪼개진 나머지 신고가 남아, 표현이 섰는데도 문장이 결핍으로 닫힌다.
            discharged = set(synthesis.issue_keys)
            issues = [
                issue
                for issue in issues
                if _audience_issue_key(issue) not in discharged
            ]
            synthesis_owner = synthesis.owner
        issues.extend(calculated)
    elif raw_expression is not None:
        raise AudienceValidationError(
            "audience_requirement.expression must be an object or null"
        )

    # §3-D 백스톱. 표현도 없고 issue 도 없는 resolution 은 하류에서 assert 로 떨어진다 —
    # 예전에는 SemanticPlan 으로 유예하는 갈래가 있었지만 그 소비자가 사라졌으므로 무조건
    # 결핍을 선언한다(조용히 빈 오디언스로 SQL 이 나가는 것을 막는 마지막 문이다).
    if expression is None and not issues:
        issues.append({
            "code": "missing_argument",
            "argument": "audience_expression",
            "message": "타겟 오디언스 조건을 canonical expression으로 확정하지 못했습니다.",
            "evidence": {"text": query, "start": 0, "end": len(query)},
        })
    return AudienceResolution(
        expression=expression,
        issues=_dedupe_audience_issues(issues),
        model_reported=model_reported,
        query=query,
        normalizations=normalizations,
        evidence_normalizations=evidence_normalizations,
        as_of_normalizations=as_of_normalizations,
        synthesis_owner=synthesis_owner,
        campaign_average_receipt=campaign_average_receipt,
        campaign_average_rewritten=campaign_average_rewritten,
        # 경고는 표현이 실제로 채택됐을 때만 뜻이 있다(버려진 합성의 경고는 이 응답의 사실이
        # 아니다). 그 판정은 위에서 이미 났다 — ``synthesis_owner`` 가 그 영수증이다.
        coverage_warnings=(
            synthesis.coverage_warnings
            if synthesis is not None and synthesis_owner is not None
            else ()
        ),
    )


# ── (b) 투영 ──────────────────────────────────────────────────────────────────────


def project_resolution_to_plan(
    payload: dict[str, Any], resolution: AudienceResolution
) -> bool:
    """검증 결과를 legacy plan 키로 투영한다. 반환값은 '이 경로가 결말을 냈는가'다."""
    from query_structurer.campaign_plan_v4 import empty_semantic_ir

    if resolution.synthesis_owner and resolution.expression is not None:
        plan_decisions.record(
            payload,
            filter_name=resolution.synthesis_owner,
            action=plan_decisions.SET,
            slot="audience_requirement.expression",
            reason=(
                "모델이 비워 둔 표현을 카탈로그·리터럴·구조 검증이 모두 증명한 "
                "canonical Event IR로 채웠다"
            ),
            value=resolution.expression.to_dict(),
        )
    if resolution.coverage_warnings:
        # 적재 범위 경고는 조건을 막지 않는다(정책 축이 다르다). 다만 응답에 남지 않으면
        # "SQL 은 나왔는데 0건"의 이유가 어디에도 없게 된다 — 그래서 결말과 함께 싣는다.
        payload[COVERAGE_WARNINGS_KEY] = list(resolution.coverage_warnings)
    if resolution.campaign_average_receipt is not None:
        rewritten = resolution.campaign_average_rewritten
        plan_decisions.record(
            payload,
            filter_name=campaign_metric_claims.SYNTHESIS_OWNER,
            action=plan_decisions.UPDATE if rewritten else plan_decisions.DROP,
            slot="audience_requirement.expression",
            reason=(
                "카탈로그가 캠페인 분모 평균으로 선언한 절인데 모델 표현은 반응 행당 "
                "평균이라 뜻이 다르다 — "
                + (
                    "선언된 분자(SUM)와 분모(서로 다른 캠페인 실행 수)로 복합 집계식을 "
                    "만들어 그 자리를 바꿨다"
                    if rewritten
                    else "합성에 필요한 선언이 불완전해 표현을 버리고 미지원으로 닫았다"
                )
            ),
            value=resolution.campaign_average_receipt,
        )
    for correction in resolution.evidence_normalizations:
        plan_decisions.record(
            payload,
            filter_name="rolling_absence.evidence_span",
            action=plan_decisions.UPDATE,
            slot="audience_requirement.expression.evidence",
            reason=(
                "기간·비교 리터럴은 rolling window 구조가 소유하므로 Exists 근거를 "
                "카탈로그 사건의 부정 구절로 좁혔다"
            ),
            value=correction,
        )
    for correction in resolution.normalizations:
        plan_decisions.record(
            payload,
            filter_name="canonical_audience_claims.window_kind",
            action=plan_decisions.UPDATE,
            slot="audience_requirement.window.type",
            reason=(
                f"기간 표현 종류는 애플리케이션 소유 — {correction['value']}"
                f"{correction['unit']} 창을 '{correction['from']}'에서 "
                f"'{correction['to']}'로 맞췄다"
            ),
            value=correction,
        )
    for correction in resolution.as_of_normalizations:
        plan_decisions.record(
            payload,
            filter_name="canonical_audience_claims.explicit_as_of",
            action=plan_decisions.UPDATE,
            slot="audience_requirement.window",
            reason=(
                "사용자가 명시한 기준일을 rolling 창의 고정 반개구간으로 확정했다"
            ),
            value=correction,
        )

    expression, issues = resolution.expression, resolution.issues
    requirement = payload[AUDIENCE_REQUIREMENT_KEY]
    requirement["expression"] = expression.to_dict() if expression is not None else None
    requirement["issues"] = issues

    # **주체가 다른 요청은 신고 내용과 무관하게 여기서 닫힌다.** 아래 갈래들은 전부 "회원을
    # 고르는 술어를 만들 수 있는가"를 묻는데, 결과 주체가 브랜드면 그 질문에 옳은 답이 없다.
    # 조건 신고 유무로 갈리게 두면 같은 요청이 회차마다 `failure`(사유 없음) 와
    # `unsupported`(사유 있음) 를 오간다 — 기준선에서 실제로 그렇게 뒤집혔다(#44).
    subject_diagnostic = _requested_subject_diagnostic(resolution.query)
    if subject_diagnostic is not None:
        write_diagnostic(payload, subject_diagnostic)
        return True

    if issues:
        payload.pop(EVENT_EXPRESSION_KEY, None)
        missing = sorted({
            f"audience.{item['argument']}"
            for item in issues if item.get("code") in {"missing_argument", "ambiguous_requirement"}
        })
        unsupported = [item for item in issues if item.get("code") == "unsupported_semantics"]
        if unsupported and not missing:
            # **미지원 선언은 가설이지 판정이 아니다.** 원문 결핍(missing_argument/
            # ambiguous_requirement)은 원문을 읽은 LLM 만 볼 수 있으므로 그대로 종결하지만,
            # "표현할 수 없다"는 실행 자산(컴파일러·카탈로그)을 아는 애플리케이션의 몫이다.
            #
            # 판정 순서가 곧 권위의 순서다. **실제로 낮출 수 있는가**가 가장 강한 근거이므로
            # 먼저 묻는다 — 계획이 서면 그 신고는 종류를 따질 것도 없이 거짓이고, 자산 표면어나
            # 의무 allowlist 를 보기 전에 방출 실패로 확정된다.
            lowering_conflicts = _lowering_plan_conflicts(resolution.query, unsupported)
            if lowering_conflicts:
                _write_emission_failure(payload, lowering_conflicts)
                return True
            # **판정자의 typed 답을 기록한다(Phase 3A shadow).** 부르는 것 자체가 관측이고,
            # 아래에서 귀결을 뒤집는 것은 능력 부재 하나뿐이다.
            planner = _planner_resolution(payload, resolution.query)
            gap = _planner_capability_diagnostic(planner)
            if gap is not None:
                # **여기가 자산 대조보다 앞이어야 한다(Phase 4A).** 아래 갈래는 "자산은
                # 선언돼 있는데 이 경로로 낼 수 없다"(레지스트리 구멍)고 말하는데, 능력이
                # 없는 자리에서 그 문구가 나가면 원인이 뒤바뀐다 — 운영자는 없는 생산자를
                # 찾게 되고, 실제로 없는 것은 그 질문에 답할 수 있는 **관측**이다.
                # 실측(#68 `앱으로 로그인하지 않은 회원`): 이 순서가 아니면 사용자가 받는
                # 문장이 `선언된 자산: app_user, inactivity_period` 였다.
                _write_capability_gap(payload, gap)
                return True
            # 계획이 서지 않는다면 그다음 질문은 "**이 관계를 구현하는 자산이 선언돼 있는가**"다.
            contradicted = [
                (
                    item,
                    execution_assets.assets_compatible_with_issue(
                        item, query=resolution.query
                    ),
                )
                for item in unsupported
                if _audience_issue_key(item) in resolution.model_reported
            ]
            contradicted = [(item, assets) for item, assets in contradicted if assets]
            if contradicted:
                # 자산은 선언돼 있는데 그 축을 낼 **생산자가 없다**. 이것은 '표현할 수 없다'가
                # 아니라 레지스트리 구멍이고, 저장소에는 이미 그 이름(semantic_registry_gap)과
                # 사용자 문구가 있다. 미지원으로 부르면 없는 한계를 있다고 말하는 것이 된다.
                #
                # 사유를 **여기서 명시**하는 것이 이 분기의 계약이다. 레지스트리 불일치는 자산
                # 목록을 대조해 본 이 경로만 아는 사실이라, 하류의 파생에 맡기면 성격이 다른
                # system_failure 까지 같은 이름으로 보고된다.
                named = sorted({asset.symbol for _item, assets in contradicted for asset in assets})
                write_semantic_ir(
                    payload,
                    empty_semantic_ir(
                        status="needs_clarification",
                        missing_fields=["audience.requirement"],
                        message=(
                            "요청한 조건을 처리할 실행 자산은 선언돼 있으나 이 경로로 낼 수 없습니다"
                            f"(선언된 자산: {', '.join(named)})."
                        ),
                        failure_kind="system_failure",
                        failure_reason=FAILURE_REASON_REGISTRY_GAP,
                    ),
                )
                payload["audience_execution_assets"] = [
                    {"argument": item["argument"], "evidence": item["evidence"]["text"],
                     "assets": [asset.to_dict() for asset in assets]}
                    for item, assets in contradicted
                ]
                return True
            # 실행 자산도 컴파일러도 이 의미를 낼 수 있는데(= 애플리케이션이 그 구간을 이미
            # 의무로 계산해 두었는데) 표현이 서지 않았다면, 그것은 '표현할 수 없다'가 아니라
            # **방출 실패**다. 재시도까지 소진한 뒤에도 미지원으로 종결하면 없는 한계를 있다고
            # 말하게 되고, 운영에서는 레지스트리 구멍을 찾게 된다(고칠 곳이 다르다).
            emission_failures = _supported_obligation_conflicts(resolution.query, unsupported)
            if emission_failures:
                _write_emission_failure(payload, emission_failures)
                return True
            # 판정 계층이 이 절을 **왜** 낮출 수 없는지 이미 알고 있다면 그 문장을 그대로 쓴다.
            # 사유를 여기서 새로 추론하지 않는다 — 아래 문구는 아무것도 선언되지 않았을 때의
            # 마지막 기본값이다. 이 갈래가 없던 동안, 이미 계산된 사유(기간이 없다 / 이 관측
            # 선언으로는 답할 수 없다)가 응답에 도달하지 못하고 범용 문장으로 덮였다(실측 2026-08-08).
            declared = _declared_temporal_rejection(resolution.query, unsupported)
            if declared is not None:
                payload[TEMPORAL_REJECTION_KEY] = {
                    "code": declared.code,
                    "message": declared.message,
                    "disposition": declared.disposition,
                    "evidence": dict(declared.evidence),
                    "judge": temporal_claims_owner(),
                }
                # **선언된 사유를 귀결로 존중한다(Phase 4B).** 생산자를 먼저 고쳤으므로
                # 이제 안전하다 — 값 결핍은 되묻기로, 이력 소스 부재는 미지원으로 간다.
                # 그 전까지 이 자리는 status 를 하드코딩해, 11곳에서 선언된 disposition 이
                # 소비자 없이 죽어 있었다.
                diagnostic = _temporal_rejection_diagnostic(declared)
                if diagnostic is not None:
                    write_diagnostic(payload, diagnostic)
                    return True
            write_semantic_ir(
                payload,
                empty_semantic_ir(
                    status="unsupported",
                    # 사용자에게 나가는 문장은 **모델이 쓴 산문이 아니다**. 실측(2026-08-03) 30/30 이
                    # 모델 산문이었고 그 판정은 틀렸다 — 지어낸 kind 만 23종이었다.
                    # 판정 계층이 이 절을 왜 낮출 수 없는지 선언했다면 그 문장을 그대로 쓴다.
                    message=(
                        declared.message
                        if declared is not None
                        else "요청한 조건을 현재 실행 자산으로 표현할 수 없습니다."
                    ),
                    failure_kind="unsupported",
                    unsupported_operations=[
                        {
                            # kind 는 닫힌 코드다. 모델의 자유 텍스트(item["argument"])는 근거로 내린다.
                            "kind": "unsupported_semantics",
                            "reason": item["message"],
                            "evidence": item["evidence"]["text"],
                        }
                        for item in unsupported
                    ],
                ),
            )
        else:
            # 결핍의 원인을 리터럴 색인과 대조해 계산한다. 이것이 없으면 **시스템이 이미
            # 결정론으로 추출해 정규화까지 마친 값을 사용자에게 되묻는다**(실측 #3: '10%').
            causes = canonical_audience_claims.missing_field_cause_records(
                resolution.query, issues, payload.get("literal_bindings") or []
            )
            model_omitted = any(
                record.get("cause") == semantic_outcome.CAUSE_MODEL_OMISSION
                for record in causes
            )
            write_semantic_ir(
                payload,
                empty_semantic_ir(
                    status="needs_clarification",
                    missing_fields=missing or ["audience.requirement"],
                    message=issues[0]["message"],
                    # 모델이 놓친 값을 사용자에게 물으면 안 된다 — 그 결핍은 재방출로 고친다.
                    failure_kind=(
                        "structurer_failure"
                        if model_omitted
                        else "user_clarification"
                        if missing
                        else "system_failure"
                    ),
                    missing_field_causes=causes,
                ),
            )
        return True

    assert expression is not None
    expression = _settle_threshold_grain(resolution.query, expression, payload)
    payload[EVENT_EXPRESSION_KEY] = {
        "expression": expression.to_dict(),
        "source": AUDIENCE_REQUIREMENT_KEY,
        "receipts": _audience_receipts(expression),
    }
    audience_authority.stamp_authority(
        payload, audience_authority.AudienceAuthority.EVENT_IR
    )
    write_semantic_ir(payload, empty_semantic_ir(status="resolved"))
    return True


__all__ = [
    "AUDIENCE_REQUIREMENT_ISSUE_CODES",
    "AudienceResolution",
    "AudienceValidationError",
    "as_of_date",
    "project_resolution_to_plan",
    "run_audience_resolver",
    "validate_audience_issue",
]
