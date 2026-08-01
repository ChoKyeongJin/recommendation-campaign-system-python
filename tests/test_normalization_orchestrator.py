"""오케스트레이터 통합 — 의미 해석 후보 → 후보 검색 → 정규화 → 실행 가능성 3축 판정.

요청 20·21번의 통합 시나리오를 결정론으로 고정한다(LLM 없음 — RawInterpretation 이 의미
해석 계층의 출력을 대신한다). 특히 "높은 confidence ≠ 실행 가능성" 과 "후보 밖 canonical_id
선택 불가"를 여기서 못박는다."""

from __future__ import annotations

from concept_catalog import ConceptCatalog, ConceptSpec
from normalization_orchestrator import (
    RawInterpretation,
    SourceRef,
    ValidationPolicy,
    resolve_condition,
)


def _ref(text: str, matched: str) -> SourceRef:
    start = text.index(matched)
    return SourceRef(text=text, matched_text=matched, start=start, end=start + len(matched))


def _interp(**overrides) -> RawInterpretation:
    base = dict(
        kind="special_slot",
        concept_query="미구매",
        candidate={"value": 3, "unit": "months"},
        source=_ref("석 달 동안 구매 안 한 고객", "석 달"),
        confidence=0.96,
    )
    base.update(overrides)
    return RawInterpretation(**base)


# ── 요청 20번: 세 표현이 같은 정규화 결과 ───────────────────────────────────────────────────
def test_three_phrasings_normalize_to_the_same_result() -> None:
    cases = [
        _interp(concept_query="구매 안 한",
                source=_ref("석 달 동안 구매 안 한 고객", "석 달"),
                candidate={"value": 3, "unit": "months"}),
        _interp(concept_query="구매가 없는",
                source=_ref("3개월째 구매가 없는 고객", "3개월"),
                candidate={"value": 3, "unit": "개월"}),
        # '한 분기' — 의미 계층이 단위를 못 바꿨어도 등록된 환산으로 제한 재시도가 확정한다.
        _interp(concept_query="미구매",
                source=_ref("한 분기 동안 미구매 고객", "한 분기"),
                candidate={"value": 1, "unit": "분기"}),
    ]
    results = [resolve_condition(interp) for interp in cases]
    for result in results:
        assert result["status"] == "normalized", result["errors"]
        assert result["concept"] == "purchase_inactivity"
        assert result["normalized"] == {"value": 3, "unit": "months", "min_days": 90}
        assert result["compiler"] == "purchase_inactivity"
        assert result["execution_support"] is True and result["sql_generation_allowed"]


def test_generic_condition_variants_link_to_same_concept() -> None:
    variants = [
        _interp(kind="generic_condition", concept_query="결제한 돈",
                source=_ref("최근 30일 동안 결제한 돈이 10만원 이상인 고객", "결제한 돈이 10만원 이상"),
                candidate={"operator": "이상", "value": 100000, "window": {"value": 30, "unit": "days"}}),
        _interp(kind="generic_condition", concept_query="주문 총액",
                source=_ref("지난달 주문 총액이 최소 10만원인 고객", "주문 총액이 최소 10만원"),
                candidate={"operator": "최소", "value": "100000", "window": {"value": 30, "unit": "days"}}),
    ]
    for interp in variants:
        result = resolve_condition(interp)
        assert result["status"] == "normalized", result["errors"]
        assert result["concept"] == "purchase_amount"
        assert result["normalized"]["operator"] == ">=" and result["normalized"]["value"] == 100000
        assert result["normalized"]["window_days"] == 30


# ── 모호/미확정 — 강제 확정 금지 ──────────────────────────────────────────────────────────
def test_degree_word_operator_needs_review() -> None:
    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="구매 금액",
        source=_ref("매출이 높은 고객", "매출이 높은"),
        candidate={"operator": "높은", "value": None}, confidence=0.8))
    assert result["status"] == "needs_review" and result["reason"] == "ambiguous_expression"
    assert not result["sql_generation_allowed"]


def test_resolved_concept_with_missing_threshold() -> None:
    """개념 해석과 값 해석의 분리(요청 8번): concept 는 resolved, value 는 unresolved."""
    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="쓴 돈",
        source=_ref("돈을 많이 쓴 고객", "돈을 많이 쓴"),
        candidate={"operator": None, "value": None}, confidence=0.8))
    assert result["status"] == "needs_review" and result["reason"] == "missing_threshold"
    assert result["concept_resolution"]["status"] == "resolved"
    assert result["value_resolution"] == {"status": "unresolved", "reason": "missing_threshold"}


# ── 미지원 개념 — 의미 이해 ≠ 실행 ─────────────────────────────────────────────────────────
def test_unsupported_concept_stays_unresolved_with_semantic_description() -> None:
    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="고객 생애가치 상승 가능성",
        source=_ref("고객 생애가치가 앞으로 상승할 가능성이 높은 고객", "생애가치가 앞으로 상승할 가능성"),
        candidate={"operator": ">", "value": 0.7}, confidence=0.92,
        semantic_description="미래 고객 생애가치 상승 가능성"))
    assert result["status"] == "unresolved" and result["reason"] == "candidate_not_found"
    assert result["catalog_match"] is False and result["execution_support"] is False
    assert result["sql_generation_allowed"] is False
    assert result["semantic_description"] == "미래 고객 생애가치 상승 가능성"


def test_high_confidence_never_implies_executability() -> None:
    """요청 14번: confidence=0.98 이어도 카탈로그·컴파일러가 없으면 실행 불가."""
    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="예측 이탈 확률",
        source=_ref("이탈 확률이 높은 고객", "이탈 확률"),
        candidate={"operator": ">", "value": 0.5}, confidence=0.98))
    assert result["semantic_match"] is True
    assert result["status"] == "unresolved" and not result["sql_generation_allowed"]


# ── 후보 선택 규율 ────────────────────────────────────────────────────────────────────────
def test_selected_candidate_outside_search_results_is_rejected() -> None:
    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="구매 금액",
        source=_ref("구매 금액 10만원 이상", "구매 금액 10만원 이상"),
        candidate={"operator": ">=", "value": 100000},
        selected_candidate_id="llm_generated_metric"))
    assert result["status"] == "needs_review" and result["reason"] == "candidate_not_allowed"
    assert not result["sql_generation_allowed"]
    allowed = result["errors"][0]["allowed_values"]
    assert "llm_generated_metric" not in allowed


def test_near_tie_candidates_at_mid_confidence_need_review() -> None:
    """중간 확신 대역에서 상위 후보 점수가 근소하면 자동 확정하지 않는다(요청 14번)."""
    twin_a = ConceptSpec(concept_id="metric_a", label="쌍둥이 지표", aliases=("공유 별칭",),
                         normalizer="generic_condition", compiler="purchase_aggregate")
    twin_b = ConceptSpec(concept_id="metric_b", label="쌍둥이 지표", aliases=("공유 별칭",),
                         normalizer="generic_condition", compiler="purchase_aggregate")
    catalog = ConceptCatalog({"metric_a": twin_a, "metric_b": twin_b},
                             compiler_available=lambda name: True)
    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="공유 별칭",
        source=_ref("공유 별칭 10 이상", "공유 별칭"),
        candidate={"operator": ">=", "value": 10}, confidence=0.7), catalog=catalog)
    assert result["status"] == "needs_review" and result["reason"] == "ambiguous_expression"


# ── 확신도·근거 게이트 ────────────────────────────────────────────────────────────────────
def test_low_confidence_is_unresolved_before_candidate_search() -> None:
    result = resolve_condition(_interp(confidence=0.4))
    assert result["status"] == "unresolved" and result["reason"] == "low_confidence"


def test_missing_source_span_is_unresolved() -> None:
    result = resolve_condition(_interp(
        source=SourceRef(text="석 달 동안 구매 안 한 고객", matched_text="", start=0, end=0)))
    assert result["status"] == "unresolved" and result["reason"] == "source_span_missing"


def test_span_text_mismatch_is_detected() -> None:
    """matched_text 가 span 이 가리키는 원문과 다르면 근거 위조다 — 실행하지 않는다(요청 15번)."""
    result = resolve_condition(_interp(
        source=SourceRef(text="석 달 동안 구매 안 한 고객", matched_text="여섯 달", start=0, end=3)))
    assert result["status"] == "unresolved" and result["reason"] == "source_span_mismatch"


def test_source_is_preserved_through_to_final_result() -> None:
    result = resolve_condition(_interp())
    assert result["source"] == {
        "text": "석 달 동안 구매 안 한 고객", "matched_text": "석 달",
        "span": {"start": 0, "end": 3}}
    assert result["candidates"], "후보 목록이 결과에 보존되지 않았다"


# ── 재시도 상한 ───────────────────────────────────────────────────────────────────────────
def test_retry_is_bounded_by_policy() -> None:
    """max_retries=0 이면 등록된 환산('분기')도 적용되지 않는다 — 무한 재시도 방지의 극단 케이스."""
    policy = ValidationPolicy(max_retries=0)
    result = resolve_condition(_interp(candidate={"value": 1, "unit": "분기"}), policy=policy)
    assert result["status"] == "needs_review"
    assert result["errors"][0]["code"] == "unsupported_unit"


def test_non_retryable_error_is_not_repaired() -> None:
    calls: list[int] = []

    def counting_repair(candidate, result):
        calls.append(1)
        return None

    result = resolve_condition(_interp(
        kind="generic_condition", concept_query="구매 금액",
        source=_ref("구매 금액 조건", "구매 금액"),
        candidate={"operator": None, "value": None}), repair=counting_repair)
    assert result["reason"] == "missing_threshold"
    assert not calls, "재시도 불가 오류에 수리가 호출됐다"
