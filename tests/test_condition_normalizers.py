"""공통 타입 정규화기 계약 — Duration/Comparison/DateWindow/Threshold/EntityReference/GenericCondition.

3층 구조: (1) 타입별 단위 계약, (2) 어휘 JSON↔코드 폴백 parity(이중 소유 드리프트 차단),
(3) targeting_ir 어댑터 회귀 — 창 계열 슬롯이 실제로 이 정규화기를 공유하는지."""

from __future__ import annotations

import json

import pytest

import condition_normalizers as cn
from concept_catalog import ConceptCatalog, ConceptSpec
from targeting_ir import SLOT_SHAPES


# ── (2) 어휘 parity 가드 ───────────────────────────────────────────────────────────────────
def test_lexicon_json_and_code_fallback_do_not_drift() -> None:
    """JSON 이 소스, 코드 폴백은 미러 — 둘이 갈라지면 '파일 유무'가 동작을 바꾼다."""
    payload = json.loads(cn.DEFAULT_NORMALIZATION_LEXICON_PATH.read_text(encoding="utf-8"))
    for section in cn._CODE_FALLBACK:
        assert payload.get(section) == cn._CODE_FALLBACK[section], (
            f"normalization_lexicon.json 의 {section!r} 이 코드 폴백과 갈라졌다 — 한쪽만 고치지 마라.")


def test_comparison_word_operators_preserve_surface_regex_order() -> None:
    """graph_rag._OP_ALT_BASIC / event_parser 정규식이 이 열거 순서에 의존한다."""
    assert list(cn.comparison_word_operators()) == ["이상", "초과", "이하", "미만"]


def test_extended_operator_aliases_stay_out_of_core_words() -> None:
    """확장 별칭(최소/적어도)은 새 계층 전용 — 핵심 표에 스며들면 표면 정규식이 바뀐다."""
    core = set(cn.comparison_word_operators())
    assert core == {"이상", "초과", "이하", "미만"}
    aliases = cn.operator_aliases()
    assert aliases["최소"] == ">=" and aliases["적어도"] == ">=" and aliases["최대"] == "<="


# ── (1) Duration ──────────────────────────────────────────────────────────────────────────
def test_duration_normal_value() -> None:
    result = cn.normalize_duration({"value": 3, "unit": "months"})
    assert result.ok and result.status == "normalized"
    assert result.value == {"value": 3, "unit": "months"}


def test_duration_korean_alias_and_string_number() -> None:
    result = cn.normalize_duration({"value": "3", "unit": "개월"})
    assert result.ok and result.value == {"value": 3, "unit": "months"}


def test_duration_fixed_unit_keeps_exact_min_days() -> None:
    result = cn.normalize_duration({"value": 3, "unit": "weeks"})
    assert result.ok and result.value == {"value": 3, "unit": "weeks", "min_days": 21}


def test_unit_days_excludes_calendar_unit_approximations() -> None:
    assert cn.unit_days() == {"days": 1, "weeks": 7}


def test_duration_unsupported_unit_is_retryable_with_allowed_values() -> None:
    result = cn.normalize_duration({"value": 1, "unit": "quarters"})
    assert not result.ok and result.status == "needs_review"
    (error,) = result.errors
    assert error.code == "unsupported_unit" and error.retryable
    assert set(error.allowed_values) == {"days", "weeks", "months", "years"}


def test_duration_negative_and_zero_rejected_by_default() -> None:
    assert cn.normalize_duration({"value": -3, "unit": "days"}).errors[0].code == "out_of_range"
    assert cn.normalize_duration({"value": 0, "unit": "days"}).errors[0].code == "out_of_range"


def test_duration_zero_allowed_when_policy_says_so() -> None:
    policy = cn.DurationPolicy(min_value=0, allow_zero=True)
    assert cn.normalize_duration({"value": 0, "unit": "days"}, policy=policy).ok


def test_duration_fractional_value_fails_closed_not_truncated() -> None:
    """3.5개월을 3개월로 잘라 쓰면 조건이 조용히 바뀐다 — 버리는 게 맞다."""
    result = cn.normalize_duration({"value": 3.5, "unit": "months"})
    assert not result.ok and result.errors[0].code == "invalid_number"


def test_duration_missing_unit_and_bool_value() -> None:
    assert cn.normalize_duration({"value": 3}).errors[0].code == "missing_value"
    assert cn.normalize_duration({"value": True, "unit": "days"}).errors[0].code == "invalid_type"


# ── (1) Comparison ────────────────────────────────────────────────────────────────────────
def test_comparison_word_symbol_and_extended_alias() -> None:
    assert cn.normalize_comparison({"operator": "이상", "value": 100000}).value == {"operator": ">=", "value": 100000}
    assert cn.normalize_comparison({"operator": ">=", "value": 3}).value["operator"] == ">="
    assert cn.normalize_comparison({"operator": "최소", "value": 3}).value["operator"] == ">="


def test_comparison_ambiguous_degree_term_is_never_forced() -> None:
    result = cn.normalize_comparison({"operator": "높은", "value": None})
    assert result.status == "needs_review"
    assert result.errors[0].code == "ambiguous_expression" and not result.errors[0].retryable


def test_comparison_unknown_operator_is_retryable() -> None:
    result = cn.normalize_comparison({"operator": "무렵", "value": 3})
    assert result.errors[0].code == "unsupported_operator" and result.errors[0].retryable


def test_comparison_operator_outside_policy_allowlist() -> None:
    policy = cn.ComparisonPolicy(allowed_operators=frozenset({">="}))
    result = cn.normalize_comparison({"operator": "<", "value": 3}, policy=policy)
    assert result.errors[0].code == "unsupported_operator"
    assert result.errors[0].allowed_values == (">=",)


def test_comparison_blocks_sql_string_in_numeric_field() -> None:
    result = cn.normalize_comparison({"operator": ">=", "value": "100000; DROP TABLE users"})
    assert not result.ok and result.errors[0].code == "invalid_number"


# ── (1) DateWindow ────────────────────────────────────────────────────────────────────────
def test_date_window_normal_range_canonicalizes() -> None:
    result = cn.normalize_date_window({"from": "2026-01-01", "to": "20260331"})
    assert result.ok and result.value == {"from": "20260101", "to": "20260331"}


def test_date_window_reversed_range_rejected() -> None:
    result = cn.normalize_date_window({"from": "20260401", "to": "20260101"})
    assert result.errors[0].code == "invalid_date_range"


def test_date_window_nonexistent_calendar_date_rejected() -> None:
    result = cn.normalize_date_window({"from": "20261301", "to": "20261401"})
    assert not result.ok and all(error.code == "invalid_date_range" for error in result.errors)


# ── (1) Threshold / GenericCondition — 스텁 카탈로그 ──────────────────────────────────────
def _catalog(**overrides) -> ConceptCatalog:
    spec = ConceptSpec(
        concept_id="purchase_amount", label="구매 금액", aliases=("구매금액",),
        kind="generic_condition", normalizer="generic_condition",
        compiler="purchase_aggregate", allowed_operators=(">", ">=", "<", "<="),
        supported_windows=True, supported_aggregations=("sum",), **overrides.pop("spec", {}))
    specs = {"purchase_amount": spec}
    no_window = ConceptSpec(
        concept_id="customer_score", label="고객 점수", kind="generic_condition",
        normalizer="generic_condition", compiler="member_profile_metric", supported_windows=False)
    specs["customer_score"] = no_window
    available = overrides.pop("compiler_available", lambda name: True)
    return ConceptCatalog(specs, compiler_available=available)


def test_threshold_unknown_concept_is_unresolved_not_guessed() -> None:
    result = cn.normalize_threshold({"concept": "predicted_ltv_growth", "operator": ">", "value": 0.7},
                                    catalog=_catalog())
    assert result.status == "unresolved"
    assert result.errors[0].code == "unsupported_concept" and not result.errors[0].retryable
    assert result.catalog_match is False and result.execution_support is False


def test_threshold_compiler_not_registered_blocks_execution() -> None:
    catalog = _catalog(compiler_available=lambda name: False)
    result = cn.normalize_threshold({"concept": "purchase_amount", "operator": ">=", "value": 1},
                                    catalog=catalog)
    assert result.status == "unresolved" and result.errors[0].code == "compiler_not_registered"
    assert result.catalog_match is True and result.execution_support is False


def test_threshold_missing_value_is_missing_threshold_with_concept_resolved() -> None:
    result = cn.normalize_threshold({"concept": "purchase_amount", "operator": ">=", "value": None},
                                    catalog=_catalog())
    assert result.status == "needs_review" and result.errors[0].code == "missing_threshold"
    assert result.catalog_match is True


def test_generic_condition_normal_with_window() -> None:
    result = cn.normalize_generic_condition(
        {"concept": "purchase_amount", "operator": "이상", "value": 100000,
         "window": {"value": 30, "unit": "days"}},
        catalog=_catalog())
    assert result.ok and result.value["operator"] == ">=" and result.value["window_days"] == 30
    assert result.catalog_match is True and result.execution_support is True


def test_generic_condition_calendar_window_preserves_unit_without_window_days() -> None:
    result = cn.normalize_generic_condition(
        {"concept": "purchase_amount", "operator": "이상", "value": 100000,
         "window": {"value": 1, "unit": "months"}},
        catalog=_catalog())
    assert result.ok
    assert result.value["window"] == {"value": 1, "unit": "months"}
    assert "window_days" not in result.value


def test_generic_condition_window_unsupported_concept() -> None:
    result = cn.normalize_generic_condition(
        {"concept": "customer_score", "operator": ">", "value": 80, "window": {"value": 7, "unit": "days"}},
        catalog=_catalog())
    assert result.status == "unsupported"
    assert result.errors[0].code == "execution_not_supported" and result.errors[0].field == "window"


def test_generic_condition_disallowed_aggregation() -> None:
    result = cn.normalize_generic_condition(
        {"concept": "purchase_amount", "operator": ">", "value": 1, "aggregation": "median"},
        catalog=_catalog())
    assert result.status == "unsupported" and result.errors[0].field == "aggregation"


# ── (1) EntityReference — 스텁 provider ───────────────────────────────────────────────────
class _Provider:
    def __init__(self, entities):
        self._entities = entities

    def search(self, query, entity_type=None, limit=5):
        return list(self._entities.values())[:limit]

    def get_by_id(self, canonical_id):
        return self._entities.get(canonical_id)


def test_entity_reference_known_id_passes() -> None:
    provider = _Provider({"campaign_2026_summer": {"entity_type": "campaign", "active": True}})
    result = cn.normalize_entity_reference(
        {"entity_type": "campaign", "canonical_id": "campaign_2026_summer"}, provider=provider)
    assert result.ok and result.catalog_match is True


def test_entity_reference_unknown_id_blocked() -> None:
    """후보 검색에 없던 임의 ID(LLM 생성)는 실행되지 않는다."""
    result = cn.normalize_entity_reference(
        {"entity_type": "campaign", "canonical_id": "campaign_that_never_existed"},
        provider=_Provider({}))
    assert result.status == "unresolved" and result.errors[0].code == "unknown_entity"


def test_entity_reference_inactive_entity_blocked() -> None:
    provider = _Provider({"old_campaign": {"entity_type": "campaign", "active": False}})
    result = cn.normalize_entity_reference(
        {"entity_type": "campaign", "canonical_id": "old_campaign"}, provider=provider)
    assert result.status == "unresolved" and result.errors[0].code == "candidate_not_allowed"


# ── 레지스트리 ────────────────────────────────────────────────────────────────────────────
def test_normalizer_registry_is_closed_and_nonempty() -> None:
    assert set(cn.NORMALIZERS) == {
        "duration", "comparison", "date_window", "threshold", "entity_reference", "generic_condition"}
    with pytest.raises(KeyError):
        cn.get_normalizer("llm_invented_normalizer")


# ── (3) targeting_ir 어댑터 회귀 — 창 계열 슬롯이 공유 정규화기를 실제로 쓴다 ────────────────
@pytest.mark.parametrize("slot", ["purchase_inactivity", "inactivity_period", "recent_login"])
def test_window_slots_share_duration_normalizer(slot: str) -> None:
    assert SLOT_SHAPES[slot].coerce({"value": 2, "unit": "개월"}) is None


def test_window_slot_fractional_value_drops_instead_of_truncating() -> None:
    """어댑터 전 int() 절단으로 3.7개월→3개월이 되던 것 — fail-close 로 교정된 동작을 고정한다."""
    assert SLOT_SHAPES["purchase_inactivity"].coerce({"value": 3.7, "unit": "months"}) is None


def test_window_slot_min_days_fallback_preserved() -> None:
    coerced = SLOT_SHAPES["purchase_inactivity"].coerce({"min_days": 45})
    assert coerced == {"value": 45, "unit": "days", "min_days": 45}


def test_signup_slot_uses_shared_window_normalization() -> None:
    assert SLOT_SHAPES["signup_target"].coerce({"value": 1, "unit": "년"}) is None
