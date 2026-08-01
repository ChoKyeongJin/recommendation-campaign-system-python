"""정규화 오케스트레이터 — 의미 해석과 SQL 사이의 관문(요청 18번 NormalizationOrchestrator).

처리 순서(요청 1번 구조의 가운데 네 단계):

  RawInterpretation(의미 해석 후보)
      → source span 검증(원문 근거 실재·좌표 일치)
      → confidence 정책(요청 14번: 높아도 실행 가능성과 무관)
      → 카탈로그 후보 검색 + 선택 검증(후보 밖 canonical_id 차단, 요청 7번)
      → 결정론 정규화(condition_normalizers) + 제한 재시도(요청 13번)
      → 실행 가능성 판정(catalog_match / execution_support 분리, 요청 3번)
      → 표준 중간 표현(요청 10번) — status ∈ normalized|needs_review|unresolved|invalid|unsupported

LLM 은 두 자리에서만 개입할 수 있고 둘 다 주입식이다: (i) 이 모듈 앞에서 RawInterpretation 을
만드는 SemanticInterpreter, (ii) 재시도 수리 repair 콜러블. 기본 repair 는 결정론이다 —
등록된 단위 환산(unit_conversions: '분기'→3개월)만 적용하고, 원문 근거 없는 값 추측·카탈로그에
없는 concept 생성·후보 밖 canonical_id 생성은 구조적으로 불가능하다(수리는 후보 dict 만 바꾸고
개념 선택은 후보 목록 검증을 다시 통과해야 한다).

순수 모듈 불변식: graph_rag 를 import 하지 않는다. LLM 호출도 하지 않는다(주입만 받는다).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

import condition_normalizers
from concept_catalog import ConceptCatalog, ConceptSpec, default_catalog
from condition_normalizers import ComparisonPolicy, DurationPolicy
from normalization_result import (
    STATUS_INVALID,
    STATUS_NEEDS_REVIEW,
    STATUS_NORMALIZED,
    STATUS_UNRESOLVED,
    NormalizationError,
    NormalizationResult,
)

KIND_SPECIAL_SLOT = "special_slot"
KIND_GENERIC_CONDITION = "generic_condition"


@dataclass(frozen=True)
class ValidationPolicy:
    """확신도 문턱·재시도 상한(요청 13·14번). 슬롯별이 아니라 파이프라인 정책이다."""

    accept_confidence: float = 0.85
    review_confidence: float = 0.60
    ambiguity_margin: float = 0.05  # 중간 확신 대역에서 상위 두 후보 점수 차가 이보다 작으면 needs_review
    max_retries: int = 1
    require_source_span: bool = True


DEFAULT_POLICY = ValidationPolicy()


@dataclass(frozen=True)
class SourceRef:
    """원문 근거(요청 15번). 정규화 결과까지 그대로 보존된다."""

    text: str
    matched_text: str
    start: int
    end: int

    def validation_error(self) -> NormalizationError | None:
        if not self.text or not self.matched_text:
            return NormalizationError("source_span_missing", field="source")
        if not (0 <= self.start < self.end <= len(self.text)):
            return NormalizationError("source_span_mismatch", field="source.span",
                                      received=[self.start, self.end])
        if self.text[self.start:self.end] != self.matched_text:
            return NormalizationError(
                "source_span_mismatch", field="source.matched_text",
                received=self.matched_text,
                detail=f"span 이 가리키는 원문은 {self.text[self.start:self.end]!r} 다")
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "matched_text": self.matched_text,
                "span": {"start": self.start, "end": self.end}}


@dataclass(frozen=True)
class RawInterpretation:
    """의미 해석 계층의 출력이자 이 모듈의 입력(요청 10번 표준 중간 표현의 candidate 상태).

    candidate 는 **표준 후보**여야 한다 — '석 달'을 {value:3, unit:'months'} 로 바꾸는 것까지가
    의미 해석의 일이고, 여기는 그 후보를 검증만 한다. concept_query 는 후보 검색 질의(개념을
    가리키는 표현)이고, selected_candidate_id 는 해석기가 이미 후보를 골랐을 때만 채운다."""

    kind: str
    concept_query: str
    candidate: Mapping[str, Any]
    source: SourceRef
    confidence: float
    selected_candidate_id: str | None = None
    semantic_description: str = ""
    context: str = ""


class SemanticInterpreter(Protocol):
    """의미 해석 계층 인터페이스(요청 18번). 구현은 LLM 이든 규칙이든 이 shape 만 지키면 된다."""

    def interpret(self, text: str, context: str = "") -> Sequence[RawInterpretation]:
        ...


RepairFn = Callable[[Mapping[str, Any], NormalizationResult], Mapping[str, Any] | None]


def deterministic_repair(candidate: Mapping[str, Any], result: NormalizationResult) -> Mapping[str, Any] | None:
    """기본 재시도 수리 — 등록된 매핑만 적용한다(요청 13번 규칙: 허용된 값 안에서만, 추측 금지).

    unsupported_unit + 등록된 단위 환산('분기'→months×3)일 때만 후보를 고쳐 돌려주고,
    그 외에는 None(수리 포기 → unresolved/needs_review 확정)."""
    conversions = condition_normalizers.unit_conversions()
    for error in result.retryable_errors():
        if error.code != "unsupported_unit":
            continue
        surface = str(error.received or "").strip().casefold()
        conversion = conversions.get(surface)
        raw_value = candidate.get("value") if isinstance(candidate, Mapping) else None
        window = candidate.get("window") if isinstance(candidate, Mapping) else None
        if conversion and isinstance(raw_value, int) and error.field == "unit":
            repaired = dict(candidate)
            repaired["value"] = raw_value * conversion["factor"]
            repaired["unit"] = conversion["unit"]
            return repaired
        if conversion and isinstance(window, Mapping) and isinstance(window.get("value"), int):
            repaired = dict(candidate)
            repaired["window"] = {"value": window["value"] * conversion["factor"], "unit": conversion["unit"]}
            return repaired
    return None


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────────────────
def _failure_payload(
    interp: RawInterpretation,
    status: str,
    errors: Sequence[NormalizationError],
    *,
    semantic_match: bool,
    catalog_match: bool | None,
    execution_support: bool | None,
    concept: str | None = None,
    compiler: str | None = None,
    candidates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    reason = errors[0].code if errors else None
    return _payload(
        interp, status=status, errors=errors, semantic_match=semantic_match,
        catalog_match=catalog_match, execution_support=execution_support,
        concept=concept, compiler=compiler, candidates=candidates, normalized=None, reason=reason)


def _payload(
    interp: RawInterpretation,
    *,
    status: str,
    errors: Sequence[NormalizationError],
    semantic_match: bool,
    catalog_match: bool | None,
    execution_support: bool | None,
    concept: str | None,
    compiler: str | None,
    candidates: Sequence[Mapping[str, Any]],
    normalized: Mapping[str, Any] | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """표준 중간 표현(요청 10번). source/confidence/후보 목록이 최종 결과까지 보존된다."""
    sql_allowed = (
        status == STATUS_NORMALIZED and catalog_match is not False and execution_support is not False)
    out: dict[str, Any] = {
        "kind": interp.kind,
        "slot": concept if interp.kind == KIND_SPECIAL_SLOT else None,
        "type": None,  # 아래에서 spec.normalizer 로 채워진다(개념 미확정이면 None 유지)
        "candidate": dict(interp.candidate),
        "normalized": dict(normalized) if isinstance(normalized, Mapping) else None,
        "concept": concept,
        "compiler": compiler,
        "status": status,
        "reason": reason,
        "errors": [error.to_dict() for error in errors],
        "confidence": interp.confidence,
        "semantic_match": semantic_match,
        "catalog_match": catalog_match,
        "execution_support": execution_support,
        "sql_generation_allowed": sql_allowed,
        "candidates": [dict(candidate) for candidate in candidates],
        "selected_candidate_id": concept,
        "source": interp.source.to_dict(),
        "concept_resolution": {
            "status": "resolved" if catalog_match else "unresolved",
            "concept": concept,
        },
        "value_resolution": {
            "status": "resolved" if status == STATUS_NORMALIZED else "unresolved",
        },
    }
    if interp.semantic_description:
        out["semantic_description"] = interp.semantic_description
    if status != STATUS_NORMALIZED and errors:
        out["value_resolution"]["reason"] = errors[0].code
    return out


def _select_candidate(
    interp: RawInterpretation,
    candidates: Sequence[Mapping[str, Any]],
    policy: ValidationPolicy,
) -> tuple[str | None, NormalizationError | None, str | None]:
    """후보 중 하나를 확정한다. 반환: (concept_id, 오류, needs_review 상태 사유).

    해석기가 고른 id 는 후보 목록 멤버십을 검증하고(후보 밖 id = candidate_not_allowed),
    안 골랐으면 결정론으로 최상위 후보를 쓰되 중간 확신 대역에서 상위 후보가 근소하면
    자동 확정하지 않는다(요청 14번)."""
    candidate_ids = [str(candidate.get("canonical_id")) for candidate in candidates]
    if interp.selected_candidate_id is not None:
        if interp.selected_candidate_id in candidate_ids:
            return interp.selected_candidate_id, None, None
        return None, NormalizationError(
            "candidate_not_allowed", field="selected_candidate_id",
            received=interp.selected_candidate_id, allowed_values=tuple(candidate_ids)), None
    if not candidates:
        return None, NormalizationError("candidate_not_found", field="concept",
                                        received=interp.concept_query, retryable=False), None
    if (
        interp.confidence < policy.accept_confidence
        and len(candidates) > 1
        and float(candidates[0].get("score", 0)) - float(candidates[1].get("score", 0)) < policy.ambiguity_margin
    ):
        return None, NormalizationError(
            "ambiguous_expression", field="concept", received=interp.concept_query, retryable=False,
            allowed_values=tuple(candidate_ids),
            detail="상위 후보 점수 차가 작아 자동 확정하지 않는다"), STATUS_NEEDS_REVIEW
    return candidate_ids[0], None, None


def _normalize_once(
    spec: ConceptSpec,
    candidate: Mapping[str, Any],
    catalog: ConceptCatalog,
) -> NormalizationResult:
    """개념 선언(normalizer 이름 + 정책)에 따라 공통 정규화기를 호출한다 — 슬롯별 함수가 없다."""
    if spec.normalizer == "duration":
        policy = DurationPolicy(
            allowed_units=frozenset(spec.allowed_units) if spec.allowed_units else DurationPolicy().allowed_units,
            min_value=spec.min_value if spec.min_value is not None else 1,
        )
        return condition_normalizers.normalize_duration(candidate, policy=policy)
    if spec.normalizer == "comparison":
        return condition_normalizers.normalize_comparison(
            candidate, policy=ComparisonPolicy(allowed_operators=frozenset(spec.allowed_operators)))
    if spec.normalizer == "date_window":
        return condition_normalizers.normalize_date_window(candidate)
    if spec.normalizer in ("threshold", "generic_condition"):
        merged = {"concept": spec.concept_id, **dict(candidate)}
        normalizer = condition_normalizers.get_normalizer(spec.normalizer)
        return normalizer(merged, catalog=catalog)
    if spec.normalizer == "entity_reference":
        raise ValueError("entity_reference 개념은 resolve_entity_reference 경로를 사용해야 한다")
    raise ValueError(f"미등록 normalizer: {spec.normalizer!r}")


# ── 공개 진입점 ───────────────────────────────────────────────────────────────────────────
def resolve_condition(
    interp: RawInterpretation,
    *,
    catalog: ConceptCatalog | None = None,
    policy: ValidationPolicy = DEFAULT_POLICY,
    repair: RepairFn = deterministic_repair,
) -> dict[str, Any]:
    """RawInterpretation 하나 → 표준 중간 표현(요청 10번). SQL 로 가는 유일한 관문은
    status=normalized + catalog_match + execution_support 가 전부 참인 결과뿐이다."""
    catalog = catalog if catalog is not None else default_catalog()

    # 1) 원문 근거 검증 — 근거 없는 해석은 확신도와 무관하게 실행하지 않는다(요청 14·15번).
    if policy.require_source_span:
        span_error = interp.source.validation_error()
        if span_error is not None:
            return _failure_payload(interp, STATUS_UNRESOLVED, [span_error],
                                    semantic_match=False, catalog_match=None, execution_support=None)

    # 2) 확신도 하한 — 낮으면 후보 검색 전에 멈춘다.
    if interp.confidence < policy.review_confidence:
        return _failure_payload(
            interp, STATUS_UNRESOLVED,
            [NormalizationError("low_confidence", field="confidence", received=interp.confidence,
                                retryable=False)],
            semantic_match=False, catalog_match=None, execution_support=None)

    # 3) 카탈로그 후보 검색 + 선택 검증 — 후보 밖 개념은 여기서 끝난다.
    candidates = catalog.search_concepts(interp.concept_query, context=interp.context, limit=5)
    concept_id, selection_error, review_status = _select_candidate(interp, candidates, policy)
    if selection_error is not None or review_status is not None:
        errors = [selection_error] if selection_error is not None else []
        status = review_status or (
            STATUS_NEEDS_REVIEW if selection_error is not None and selection_error.retryable else STATUS_UNRESOLVED)
        return _failure_payload(
            interp, status, errors,
            semantic_match=True, catalog_match=False, execution_support=False, candidates=candidates)

    spec = catalog.get_concept(concept_id)

    # 4) 결정론 정규화 + 제한 재시도(retryable 오류만, 상한은 정책 소유).
    candidate: Mapping[str, Any] = interp.candidate
    result = _normalize_once(spec, candidate, catalog)
    attempts = 0
    while not result.ok and result.retryable_errors() and attempts < policy.max_retries:
        repaired = repair(candidate, result)
        if repaired is None:
            break
        candidate = repaired
        result = _normalize_once(spec, candidate, catalog)
        attempts += 1

    # 5) 실행 가능성 — 정규화기가 판정하지 않은 축(특수 슬롯 duration 등)을 카탈로그로 보강.
    catalog_match = result.catalog_match if result.catalog_match is not None else True
    if result.ok:
        if not catalog.is_executable(concept_id):
            return _failure_payload(
                interp, STATUS_UNRESOLVED,
                [NormalizationError("compiler_not_registered", field="concept", received=concept_id,
                                    retryable=False)],
                semantic_match=True, catalog_match=True, execution_support=False,
                concept=concept_id, compiler=spec.compiler, candidates=candidates)
        payload = _payload(
            interp, status=STATUS_NORMALIZED, errors=(), semantic_match=True,
            catalog_match=True, execution_support=True, concept=concept_id,
            compiler=spec.compiler, candidates=candidates, normalized=result.value)
        payload["type"] = spec.normalizer
        return payload

    payload = _failure_payload(
        interp, result.status, list(result.errors),
        semantic_match=True, catalog_match=catalog_match,
        execution_support=result.execution_support,
        concept=concept_id if catalog_match else None,
        compiler=spec.compiler if catalog_match else None, candidates=candidates)
    payload["type"] = spec.normalizer
    return payload


def resolve_conditions(
    interpretations: Sequence[RawInterpretation],
    *,
    catalog: ConceptCatalog | None = None,
    policy: ValidationPolicy = DEFAULT_POLICY,
    repair: RepairFn = deterministic_repair,
) -> list[dict[str, Any]]:
    return [resolve_condition(interp, catalog=catalog, policy=policy, repair=repair)
            for interp in interpretations]


__all__ = [
    "DEFAULT_POLICY",
    "KIND_GENERIC_CONDITION",
    "KIND_SPECIAL_SLOT",
    "RawInterpretation",
    "SemanticInterpreter",
    "SourceRef",
    "ValidationPolicy",
    "deterministic_repair",
    "resolve_condition",
    "resolve_conditions",
]
