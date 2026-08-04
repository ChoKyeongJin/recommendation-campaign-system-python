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
산출은 ``tests/test_audience_execution_projection.py`` 가 6갈래로 고정한 그대로여야 한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import audience_authority
import audience_issue_contract
import campaign_metric_claims
import canonical_audience_claims
import consent_cardinality
import event_ir
import execution_assets
import plan_decisions
import profile_metric_claims
import rolling_absence_claims
import semantic_plan as semantic_plan_module
import semantic_relation_ownership
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

AUDIENCE_REQUIREMENT_KEY = "audience_requirement"
EVENT_EXPRESSION_KEY = "event_expression"
SEMANTIC_PLAN_KEY = "semantic_plan"

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
    # A model-authored audience issue can be disproved by one structurally
    # owned SemanticPlan node.  In that case this path yields to the node
    # compiler instead of inventing a missing audience expression.
    defer_to_semantic_plan: bool = False
    # 모델이 null 로 돌려준 표현을 애플리케이션 소유 계약이 완전히 증명해 채운 경우의
    # 소유자. 투영 시 결정 로그에 남기며, 검증 issue 가 하나라도 남으면 설정하지 않는다.
    synthesis_owner: str | None = None
    # Event IR 밖의 최신 스냅샷 축을 선언 자산+리터럴로 완전히 증명해 SemanticPlan 에 넘긴 경우.
    # 모델의 unsupported 산문을 지운 근거를 attach 단계 로그에 남긴다.
    semantic_plan_synthesis: dict[str, Any] | None = None


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
) -> list[dict[str, Any]]:
    """요구 계층에 검증기를 주입해 돌리고, 결과를 기존 issue 표기로 되돌린다."""
    resolver = DefaultRequirementResolver(
        validators=audience_validators(as_of=as_of_date(current_date))
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


def _semantic_plan_owns_catalog_issue(
    issue: Mapping[str, Any], payload: Mapping[str, Any], query: str
) -> bool:
    """Defer a catalog value claim explicitly owned by a relation node.

    This is only an early-stage deferral.  The graph-level receipt gate still
    requires that node to compile; an unsupported or malformed node therefore
    continues to block the whole query.
    """
    import audience_runtime

    catalog = audience_runtime.catalog_snapshot()
    return semantic_relation_ownership.semantic_plan_owns_issue(
        issue, payload, query, catalog
    )


@dataclass(frozen=True)
class _ApplicationOwnedSynthesis:
    expression: event_ir.Condition
    owner: str
    issue_key: tuple[str, str, str]


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


def _application_owned_synthesis(
    query: str,
    issues: list[dict[str, Any]],
    literal_bindings: list[Any],
) -> _ApplicationOwnedSynthesis | None:
    """Resolve one false model issue through a complete structural contract.

    This path never combines a synthesized atom with an unknown remainder: one
    model issue must account for the null expression, its evidence must contain
    the owned literal, and the generated expression still goes through all
    ordinary audience validators before that issue can be discharged.
    """
    if len(issues) != 1:
        return None
    issue = issues[0]
    code, argument = issue.get("code"), issue.get("argument")

    import audience_runtime

    catalog = audience_runtime.catalog_snapshot()
    if (
        code in {"ambiguous_requirement", "unsupported_semantics"}
        and argument == "consent_count"
    ):
        expression = consent_cardinality.synthesize_exact_consent_cardinality(
            query, literal_bindings, catalog
        )
        if expression is not None:
            validation = consent_cardinality.validate_consent_cardinality(
                query, expression, literal_bindings, catalog
            )
            if validation is not None and _issue_evidence_contains(
                issue, validation.quantifier_start, validation.quantifier_end
            ):
                return _ApplicationOwnedSynthesis(
                    expression,
                    "consent_cardinality.exact_truth_table",
                    _audience_issue_key(issue),
                )

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
                    _audience_issue_key(issue),
                )
    return None


def _closed_audience_synthesis_envelope(payload: Mapping[str, Any]) -> bool:
    """Require a closed audience-only envelope before replacing model issues.

    The claim synthesizers prove the complete *query text* belongs to one
    audience predicate.  A hallucinated intent, campaign metadata, or result
    limit lives outside that predicate and would otherwise survive issue
    discharge as an invented execution constraint.
    """

    constraints = payload.get("campaign_constraints")
    return bool(
        payload.get("intent") in {"find_user_segment", "recommend_campaign"}
        and payload.get("result_limit") is None
        and isinstance(constraints, Mapping)
        and all(value in (None, [], {}) for value in constraints.values())
    )


def _closed_model_expression_envelope(payload: Mapping[str, Any]) -> bool:
    """Exclude every legacy execution side channel from model-expression retyping."""

    exclude = payload.get("exclude")
    return bool(
        _closed_audience_synthesis_envelope(payload)
        and payload.get("target_user") == {}
        and isinstance(exclude, Mapping)
        and set(exclude) <= {"gender", "interests", "lifecycle"}
        and all(value in (None, [], {}) for value in exclude.values())
        and payload.get("aggregation_request") is None
        and all(
            payload.get(key) == []
            for key in (
                "set_expressions",
                "computed_metrics",
                "external_conditions",
                "compound_dimension_filters",
                "semantic_evidence",
                "unresolved",
            )
        )
    )


def run_audience_resolver(
    payload: dict[str, Any], query: str, *, current_date: str | None
) -> AudienceResolution | None:
    """오디언스 계약을 검증한다. 계약 자체가 없으면 ``None``(SemanticPlan 경로가 이어받는다)."""
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
    deferred_period_issue = False
    if raw_expression is None and issues:
        retained: list[dict[str, Any]] = []
        for issue in issues:
            if audience_issue_contract.latest_transition_owns_period_issue(
                query, issue, payload.get(SEMANTIC_PLAN_KEY)
            ):
                deferred_period_issue = True
            else:
                retained.append(issue)
        issues = retained
    semantic_plan_synthesis: (
        profile_metric_claims.ProfileMetricSynthesis
        | campaign_metric_claims.CampaignMetricSynthesis
        | None
    ) = None
    closed_synthesis_envelope = _closed_audience_synthesis_envelope(payload)
    if (
        _closed_model_expression_envelope(payload)
        and isinstance(raw_expression, dict)
        and not issues
        and isinstance(literal_bindings, list)
    ):
        semantic_plan_synthesis = (
            campaign_metric_claims.synthesize_campaign_average_amount_expression(
                query,
                raw_expression,
                literal_bindings,
                payload.get(SEMANTIC_PLAN_KEY),
            )
        )
        if semantic_plan_synthesis is not None:
            # The aggregate has moved to the one execution path that owns its
            # campaign denominator and fixed response predicates.  Do not also
            # project the generic Event IR comparison.
            raw_expression = None
    if (
        semantic_plan_synthesis is None
        and closed_synthesis_envelope
        and raw_expression is None
        and issues
        and isinstance(literal_bindings, list)
    ):
        semantic_plan_synthesis = (
            campaign_metric_claims.synthesize_campaign_average_amount_predicate(
                query,
                issues,
                literal_bindings,
                payload.get(SEMANTIC_PLAN_KEY),
            )
        )
    if (
        semantic_plan_synthesis is None
        and closed_synthesis_envelope
        and raw_expression is None
        and len(issues) == 1
        and isinstance(literal_bindings, list)
    ):
        semantic_plan_synthesis = profile_metric_claims.synthesize_profile_metric_predicate(
            query,
            issues[0],
            literal_bindings,
            payload.get(SEMANTIC_PLAN_KEY),
        )
    if semantic_plan_synthesis is not None:
        raw_plan = payload.get(SEMANTIC_PLAN_KEY)
        assert isinstance(raw_plan, dict)  # synthesis requires the exact empty-node contract
        raw_plan["nodes"] = [semantic_plan_synthesis.node]
        issues = []
    synthesis: _ApplicationOwnedSynthesis | None = None
    if raw_expression is None and issues and isinstance(literal_bindings, list):
        synthesis = _application_owned_synthesis(query, issues, literal_bindings)
        if synthesis is not None:
            raw_expression = synthesis.expression.to_dict()
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
        calculated = _validation_issues(
            expression, query, literal_bindings, current_date=current_date
        )
        calculated = [
            issue
            for issue in calculated
            if not _semantic_plan_owns_catalog_issue(issue, payload, query)
        ]
        if not calculated:
            expression, as_of_normalizations, as_of_issue = (
                _pin_explicit_as_of_rolling_windows(
                    expression, literal_bindings, query
                )
            )
            if as_of_issue is not None:
                calculated.append(as_of_issue)
        if synthesis is not None and not calculated:
            issues = [
                issue
                for issue in issues
                if _audience_issue_key(issue) != synthesis.issue_key
            ]
            synthesis_owner = synthesis.owner
        issues.extend(calculated)
    elif raw_expression is not None:
        raise AudienceValidationError(
            "audience_requirement.expression must be an object or null"
        )

    relation_owns_entire_audience = False
    if expression is None and not issues:
        import audience_runtime

        relation_owns_entire_audience = (
            semantic_relation_ownership.semantic_plan_owns_entire_audience(
                payload, query, audience_runtime.catalog_snapshot()
            )
        )
    defer_to_semantic_plan = bool(
        expression is None
        and not issues
        and (
            deferred_period_issue
            or semantic_plan_synthesis is not None
            or relation_owns_entire_audience
        )
    )
    if expression is None and not issues and not defer_to_semantic_plan:
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
        defer_to_semantic_plan=defer_to_semantic_plan,
        synthesis_owner=synthesis_owner,
        semantic_plan_synthesis=(
            semantic_plan_synthesis.receipt
            if semantic_plan_synthesis is not None
            else None
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
    if resolution.semantic_plan_synthesis is not None:
        plan_decisions.record(
            payload,
            filter_name=str(
                resolution.semantic_plan_synthesis.get("owner")
                or "application_metric_claims"
            ),
            action=plan_decisions.SET,
            slot="semantic_plan.nodes",
            reason=(
                "모델의 미지원 신고를 선언된 지표·물리 실행 자산·"
                "문형/숫자/단위/비교 리터럴 영수증으로 반박해 SemanticPlan 노드를 채웠다"
            ),
            value=resolution.semantic_plan_synthesis,
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

    if resolution.defer_to_semantic_plan:
        payload.pop(EVENT_EXPRESSION_KEY, None)
        return False

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
            # 강등의 조건은 "**선언된 실행 자산 중 이 의미를 처리하는 것이 있는가**"다.
            plan_nodes = payload.get(SEMANTIC_PLAN_KEY)
            plan_nodes = plan_nodes.get("nodes") if isinstance(plan_nodes, dict) else None
            contradicted = [
                (item, execution_assets.non_canonical_assets_for_issue(item))
                for item in unsupported
                if _audience_issue_key(item) in resolution.model_reported
            ]
            contradicted = [(item, assets) for item, assets in contradicted if assets]
            if contradicted and plan_nodes:
                payload["audience_unsupported_hypotheses"] = [
                    {"kind": item["argument"], "reason": item["message"],
                     "evidence": item["evidence"]["text"]}
                    for item in unsupported
                ]
                return False
            if contradicted:
                # 자산은 선언돼 있는데 그 축을 낼 **생산자가 없다**. 이것은 '표현할 수 없다'가
                # 아니라 레지스트리 구멍이고, 저장소에는 이미 그 이름(semantic_registry_gap)과
                # 사용자 문구가 있다. 미지원으로 부르면 없는 한계를 있다고 말하는 것이 된다.
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
                    ),
                )
                payload["audience_execution_assets"] = [
                    {"argument": item["argument"], "evidence": item["evidence"]["text"],
                     "assets": [asset.to_dict() for asset in assets]}
                    for item, assets in contradicted
                ]
                return True
            write_semantic_ir(
                payload,
                empty_semantic_ir(
                    status="unsupported",
                    # 사용자에게 나가는 문장은 **모델이 쓴 산문이 아니다**. 실측(2026-08-03) 30/30 이
                    # 모델 산문이었고 그 판정은 틀렸다 — 지어낸 kind 만 23종이었다.
                    message="요청한 조건을 현재 실행 자산으로 표현할 수 없습니다.",
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
                record.get("cause") == semantic_plan_module.CAUSE_MODEL_OMISSION
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
