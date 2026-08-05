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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import audience_authority
import audience_issue_contract
import campaign_metric_claims
import canonical_audience_claims
import consent_cardinality
import event_ir
import execution_assets
import plan_decisions
import rolling_absence_claims
import semantic_outcome
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
    # 모델이 null 로 돌려준 표현을 애플리케이션 소유 계약이 완전히 증명해 채운 경우의
    # 소유자. 투영 시 결정 로그에 남기며, 검증 issue 가 하나라도 남으면 설정하지 않는다.
    synthesis_owner: str | None = None
    # '캠페인당 평균 구매금액' 판정 근거. 합성이 성립하면 모델의 행당 평균 집계를 캠페인 분모
    # 복합식으로 **바꾼** 기록이고, 성립하지 않으면 표현을 버린 기록이다(둘은 receipt 에
    # numerator/denominator 가 있는지로 구분된다).
    campaign_average_receipt: dict[str, Any] | None = None
    campaign_average_rewritten: bool = False


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
    issue_key: tuple[str, str, str]
    # 합성이 **스칼라 임계값으로** 소비한 원문 구간. 기간처럼 보이지만 창이 아닌 리터럴
    # ('구매주기가 30일 이하'의 '30일')을 시간 검증기가 소실된 창으로 세지 않게 한다.
    scalar_literal_spans: tuple[tuple[int, int], ...] = ()


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
    *,
    current_date: str | None = None,
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
    return None


def _temporal_synthesis(
    query: str, issue: Mapping[str, Any], *, current_date: str | None = None
) -> _ApplicationOwnedSynthesis | None:
    """모델이 표현하지 못한 시간·이력 절을 canonical Temporal IR 로 되살린다.

    ``None`` 을 돌려주는 세 경우를 구분하지 않는 것은 의도다 — 시간 조건이 없거나, 근거가
    어긋나거나, 낮출 수 없으면 **모델의 미지원 신고가 그대로 남는다**. 그래야 절이 조용히
    사라진 성공이 생기지 않는다(부분 SQL 금지).
    """

    import audience_runtime  # noqa: PLC0415 - 지연 import(순환 방지)
    import temporal_claims  # noqa: PLC0415
    import temporal_ir  # noqa: PLC0415
    from temporal_ir import semantic_ir as sir  # noqa: PLC0415

    catalog = audience_runtime.resolve_audience_catalog()
    snapshot = audience_runtime.catalog_snapshot()
    try:
        runtime = temporal_ir.create_temporal_runtime(catalog)
    except temporal_ir.TemporalCatalogError:
        # 선언을 읽지 못하는 것은 '해당 없음'이 아니다. 그러나 이 경로의 결말은 어차피
        # 모델 신고 유지(SQL 없음)이므로, 판정 불가를 통과로 바꾸지 않고 그대로 둔다.
        return None

    context = sir.TemporalRequestContext(now=_request_now(current_date))
    outcome = temporal_claims.synthesize_temporal_claim(
        query,
        snapshot=snapshot,
        catalog=catalog,
        runtime=runtime,
        context=context,
    )
    if not isinstance(outcome, temporal_claims.TemporalClaimSynthesis):
        return None
    # 합성의 근거가 모델이 신고한 구간 안에 있어야 그 신고를 반박할 수 있다.
    if not any(
        _issue_evidence_contains(issue, start, end) for start, end in outcome.spans
    ):
        return None
    return _ApplicationOwnedSynthesis(
        outcome.expression,
        temporal_claims.OWNER,
        _audience_issue_key(issue),
        scalar_literal_spans=outcome.spans,
    )


def _conjoinable_synthesis(
    query: str,
    issues: list[dict[str, Any]],
    *,
    current_date: str | None = None,
) -> _ApplicationOwnedSynthesis | None:
    """모델 표현과 **결합할 수 있는** 합성 하나. 없으면 ``None``(신고가 그대로 남는다).

    결합을 시간 축으로 제한하는 것은 의도다. 시간 조건은 자기 근거 구간을 정확히 소유하고
    (낮춘 원자가 그 구간을 그대로 들고 있다) 낮춤이 전부-또는-아무것도이므로, 결합해도
    '어느 절이 어디서 왔는가'가 흐려지지 않는다. 다른 축까지 한꺼번에 열면 그 성질이
    보장되지 않는 합성이 모델 표현과 섞인다.
    """

    unsupported = [item for item in issues if item.get("code") == "unsupported_semantics"]
    if len(unsupported) != 1:
        return None
    return _temporal_synthesis(query, unsupported[0], current_date=current_date)


def _request_now(current_date: str | None = None) -> datetime:
    """합성 기준 시각 — 플랜이 확정한 기준일을 쓰고, 없을 때만 요청 시점을 쓴다.

    시계를 직접 읽으면 같은 입력이 실행 시각에 따라 다른 창으로 낮아진다. 이 저장소의
    시간 계층은 기준 시각을 주입받는 것이 규약이고(:class:`sir.TemporalRequestContext`),
    구조화기가 이미 ``current_date`` 를 확정해 두므로 그것이 유일한 권위다.
    """

    zone = ZoneInfo("Asia/Seoul")
    anchor = as_of_date(current_date)
    if anchor is None:
        return datetime.now(zone)
    return datetime.combine(anchor, dtime(9, 0), tzinfo=zone)


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
        _audience_issue_key(issue),
        scalar_literal_spans=spans,
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
        conjunct = _conjoinable_synthesis(query, issues, current_date=current_date)
        if conjunct is not None:
            synthesis = conjunct
            raw_expression = event_ir.And(
                operands=(
                    _parse_audience_expression(raw_expression, query),
                    conjunct.expression,
                )
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
        calculated = _validation_issues(
            expression,
            query,
            literal_bindings,
            current_date=current_date,
            scalar_literal_spans=(
                synthesis.scalar_literal_spans if synthesis is not None else ()
            ),
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
            contradicted = [
                (item, execution_assets.non_canonical_assets_for_issue(item))
                for item in unsupported
                if _audience_issue_key(item) in resolution.model_reported
            ]
            contradicted = [(item, assets) for item, assets in contradicted if assets]
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
