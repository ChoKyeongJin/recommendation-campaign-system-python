"""Semantic Issue Detector — 무엇이 비었거나 모호한지 **발견만** 한다.

판정하지 않는다. 여기서 나온 결핍이 자동으로 채워질지, 되묻기가 될지, 미지원으로 닫힐지는
:mod:`resolution.policy` 하나만 결정한다(§9). 감지기가 그 판단을 조금이라도 하면 판정자가
둘이 되고, 둘이 갈리는 순간 같은 문장이 회차마다 다른 귀결을 낸다.

감지기는 둘뿐이고 근거가 서로 다르다.

``reported_issue_detector``
    표현을 만든 계층이 이미 신고한 결핍(``missing_argument`` 등)을 **타입 있는 결핍**으로 옮긴다.
    새 사실을 만들지 않는다 — 신고에 없는 결핍을 여기서 발명하지 않는다.

``comparison_grain_detector``
    원문이 grain 을 말하지 않았는데 트리가 한쪽 grain 으로 굳어 있고, **다른 쪽도 실제로 컴파일
    가능한** 자리를 찾는다. 이 자리가 저장소에서 실측으로 가장 크게 다친 곳이다(같은 금액·기간·
    연산자인데 9,585명 대 688명). 지금까지는 모델이 고른 트리 모양이 곧 답이었고, 사용자는
    자기가 어느 쪽을 받았는지 알 수 없었다.

    후보 판정은 :mod:`grain_claims` 가 소유한 순수 IR 검사만 쓴다 — 여기서 한국어를 다시 읽지
    않는다. 낮출 수 없는 모양(count·avg 집계 등)에서는 후보가 하나뿐이므로 **모호성이 아니다**.

새 감지기를 여는 방법은 :data:`DETECTORS` 에 함수 하나를 더하는 것이다. 함수는 결핍을 내놓기만
하고 어떤 귀결도 주장하지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

import event_ir
import grain_claims
from resolution.config import ResolutionPolicyConfig
from resolution.issues import (
    AMBIGUOUS_COMPARISON_GRAIN,
    AMBIGUOUS_REQUIREMENT,
    GRAIN_ROW,
    GRAIN_SUBJECT,
    MISSING_ENTITY_VALUE,
    MISSING_RECENT_PERIOD,
    MISSING_SEMANTIC_VALUE,
    UNSUPPORTED_SEMANTICS,
    IssueCandidate,
    SemanticIssue,
    SourceSpan,
)
from resolution.request import CanonicalRequest

logger = logging.getLogger("resolution.detection")

#: 기간 결핍을 가리키는 argument 이름. 소유자는 구조화기 계약이고 여기서는 이름만 참조한다.
PERIOD_ARGUMENT = "period"

#: legacy 보고 코드 → 결핍 종류. 표에 없는 코드는 **타입 있는 결핍을 만들지 않는다** —
#: 그 신고는 legacy 표기로 남아 기존 진단 경로가 그대로 처리한다(조용히 삼키지 않는다).
_REPORT_CODE_KINDS: dict[str, str] = {
    "ambiguous_requirement": AMBIGUOUS_REQUIREMENT,
    "unsupported_semantics": UNSUPPORTED_SEMANTICS,
}

Detector = Callable[
    [CanonicalRequest, ResolutionPolicyConfig], Iterable[SemanticIssue]
]


def _report_evidence(report: Mapping[str, Any]) -> SourceSpan | None:
    return SourceSpan.from_mapping(report.get("evidence"))


def _report_message(report: Mapping[str, Any], fallback: str) -> str:
    message = report.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else fallback


def reported_issue_detector(
    request: CanonicalRequest, config: ResolutionPolicyConfig
) -> Iterator[SemanticIssue]:
    """이미 신고된 결핍을 타입 있는 결핍으로. 신고에 없는 것은 만들지 않는다."""

    for report in request.reported_issues:
        if not isinstance(report, Mapping):
            continue
        code = str(report.get("code") or "")
        argument = str(report.get("argument") or "")
        evidence = _report_evidence(report)
        legacy = (code, argument)

        if code == "missing_argument":
            if argument == PERIOD_ARGUMENT:
                yield SemanticIssue(
                    kind=MISSING_RECENT_PERIOD,
                    slot="audience.period",
                    message=_report_message(report, "기간을 확정하지 못했습니다."),
                    clause_id=argument,
                    evidence=evidence,
                    origin="reported_issue_detector",
                    legacy_report=legacy,
                )
                continue
            entity = config.entity_argument(argument)
            if entity is not None:
                yield SemanticIssue(
                    kind=MISSING_ENTITY_VALUE,
                    slot=f"predicate.{argument}",
                    message=_report_message(
                        report, f"{entity.label} 값을 확정하지 못했습니다."
                    ),
                    clause_id=argument,
                    entity_type=entity.entity_type,
                    evidence=evidence,
                    origin="reported_issue_detector",
                    legacy_report=legacy,
                )
                continue
            yield SemanticIssue(
                kind=MISSING_SEMANTIC_VALUE,
                slot=f"audience.{argument}" if argument else "audience.requirement",
                message=_report_message(report, "필요한 값을 확정하지 못했습니다."),
                clause_id=argument or None,
                evidence=evidence,
                origin="reported_issue_detector",
                legacy_report=legacy,
            )
            continue

        kind = _REPORT_CODE_KINDS.get(code)
        if kind is None:
            # validation_mismatch 처럼 사용자가 답할 수 없는 신고는 여기서 타입을 얻지 않는다.
            # 기존 진단 경로(구조화기 실패/시스템 실패)가 소유한다.
            continue
        yield SemanticIssue(
            kind=kind,
            slot=f"audience.{argument}" if argument else "audience.requirement",
            message=_report_message(report, "요청한 조건을 확정하지 못했습니다."),
            clause_id=argument or None,
            evidence=evidence,
            origin="reported_issue_detector",
            legacy_report=legacy,
        )


def _parsed_expression(request: CanonicalRequest) -> Any | None:
    if not request.has_expression:
        return None
    try:
        return event_ir.condition_from_dict(dict(request.expression or {}))
    except event_ir.IrSchemaError:
        # 표현이 스키마를 어기면 그 실패의 소유자는 컴파일 경로다. 여기서 결핍을 지어내지 않는다.
        return None


def comparison_grain_detector(
    request: CanonicalRequest, config: ResolutionPolicyConfig
) -> Iterator[SemanticIssue]:
    """원문이 grain 을 말하지 않았고 두 해석이 모두 컴파일되는 임계값 자리."""

    expression = _parsed_expression(request)
    if expression is None:
        return
    if grain_claims.detect_grain_claims(request.query):
        # 원문이 grain 을 말했다 — 확정된 의미이지 모호성이 아니다.
        return
    thresholds = grain_claims.regrainable_thresholds(expression)
    if not thresholds:
        return
    realized = grain_claims.expression_grains(expression)
    if grain_claims.SUBJECT not in realized:
        return
    for index, node in enumerate(thresholds):
        evidence = getattr(node, "evidence", None)
        span = (
            SourceSpan(text=evidence.text, start=evidence.start, end=evidence.end)
            if evidence is not None
            and isinstance(getattr(evidence, "text", None), str)
            and isinstance(getattr(evidence, "start", None), int)
            and isinstance(getattr(evidence, "end", None), int)
            else None
        )
        quoted = span.text if span is not None else "임계값"
        yield SemanticIssue(
            kind=AMBIGUOUS_COMPARISON_GRAIN,
            slot="comparison.grain",
            message=(
                f"'{quoted}' 기준이 한 번의 주문에 걸리는지 기간 합계에 걸리는지 "
                "원문에서 확정되지 않았습니다."
            ),
            clause_id=f"threshold-{index}",
            candidates=(
                IssueCandidate(value=GRAIN_ROW, label="한 번의 주문 금액"),
                IssueCandidate(value=GRAIN_SUBJECT, label="기간 내 총 구매금액"),
            ),
            evidence=span,
            origin="comparison_grain_detector",
        )


#: 등록된 감지기. 순서는 질문 순서가 아니다(그건 :mod:`resolution.clarification` 이 정한다).
DETECTORS: tuple[Detector, ...] = (
    reported_issue_detector,
    comparison_grain_detector,
)


def detect_semantic_issues(
    request: CanonicalRequest,
    config: ResolutionPolicyConfig,
    *,
    detectors: Sequence[Detector] | None = None,
) -> tuple[SemanticIssue, ...]:
    """등록된 감지기 전부를 돌려 결핍 목록을 만든다(같은 신원은 한 번만)."""

    seen: dict[str, SemanticIssue] = {}
    for detector in detectors if detectors is not None else DETECTORS:
        for issue in detector(request, config):
            seen.setdefault(issue.issue_id, issue)
    return tuple(seen.values())


__all__ = [
    "DETECTORS",
    "PERIOD_ARGUMENT",
    "Detector",
    "comparison_grain_detector",
    "detect_semantic_issues",
    "reported_issue_detector",
]
