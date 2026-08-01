"""normalization_result 계약 — 닫힌 상태/오류 코드 집합과 결과 타입 불변식.

이 집합들은 소비자(오케스트레이터/게이트/프론트)가 문자열로 분기하는 어휘다 —
여기서 고정하지 않으면 오타 하나가 게이트를 조용히 무력화한다(unsupported_reasons 와 같은 관례)."""

from __future__ import annotations

import pytest

from normalization_result import (
    ALL_STATUSES,
    ERROR_CODES,
    RETRYABLE_CODES,
    SQL_ALLOWED_STATUSES,
    STATUS_NORMALIZED,
    NormalizationError,
    NormalizationResult,
)

# 요청 12번이 요구한 최소 오류 코드 목록 — 지우면 소비자 분기가 침묵으로 죽는다.
REQUIRED_ERROR_CODES = {
    "missing_value", "invalid_type", "invalid_number", "out_of_range",
    "unsupported_unit", "unsupported_operator", "unknown_slot", "unknown_metric",
    "unknown_entity", "unknown_concept", "unsupported_concept", "compiler_not_registered",
    "ambiguous_expression", "candidate_not_found", "candidate_not_allowed",
    "invalid_date_range", "source_span_missing", "source_span_mismatch",
    "low_confidence", "missing_threshold", "execution_not_supported",
}


def test_status_set_is_closed_and_only_normalized_reaches_sql() -> None:
    assert ALL_STATUSES == {"candidate", "normalized", "needs_review", "unresolved", "invalid", "unsupported"}
    assert SQL_ALLOWED_STATUSES == {"normalized"}


def test_error_codes_cover_required_minimum() -> None:
    missing = REQUIRED_ERROR_CODES - ERROR_CODES
    assert not missing, f"요청 12번 최소 오류 코드가 빠졌다: {sorted(missing)}"


def test_retryable_codes_are_the_limited_subset() -> None:
    """재시도는 허용값 안에서 고칠 수 있는 것만 — 개념/컴파일러 부재는 재해석으로 해결되지 않는다."""
    assert RETRYABLE_CODES == {"unsupported_unit", "unsupported_operator", "candidate_not_allowed", "invalid_type"}
    assert "unsupported_concept" not in RETRYABLE_CODES
    assert "compiler_not_registered" not in RETRYABLE_CODES
    assert "execution_not_supported" not in RETRYABLE_CODES


def test_unregistered_error_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        NormalizationError("totally_new_code")


def test_unregistered_status_is_rejected() -> None:
    with pytest.raises(ValueError):
        NormalizationResult(ok=False, status="mystery")


def test_ok_true_requires_normalized_status_and_no_errors() -> None:
    with pytest.raises(ValueError):
        NormalizationResult(ok=True, status="candidate", value={})
    with pytest.raises(ValueError):
        NormalizationResult(ok=True, status=STATUS_NORMALIZED, value={},
                            errors=(NormalizationError("missing_value"),))


def test_failure_cannot_smuggle_a_value_downstream() -> None:
    with pytest.raises(ValueError):
        NormalizationResult(ok=False, status="invalid", value={"leaked": True})


def test_retryable_defaults_derive_from_code_and_override_wins() -> None:
    assert NormalizationError("unsupported_unit").retryable is True
    assert NormalizationError("unsupported_concept").retryable is False
    assert NormalizationError("unsupported_unit", retryable=False).retryable is False


def test_sql_generation_allowed_requires_every_axis() -> None:
    ok = NormalizationResult.normalized({"v": 1}, catalog_match=True, execution_support=True)
    assert ok.sql_generation_allowed

    no_catalog = NormalizationResult.normalized({"v": 1}, catalog_match=False, execution_support=True)
    assert not no_catalog.sql_generation_allowed

    no_compiler = NormalizationResult.normalized({"v": 1}, catalog_match=True, execution_support=False)
    assert not no_compiler.sql_generation_allowed

    failed = NormalizationResult.failure("unresolved", NormalizationError("unsupported_concept"))
    assert not failed.sql_generation_allowed


def test_failure_factory_refuses_sql_allowed_status() -> None:
    with pytest.raises(ValueError):
        NormalizationResult.failure(STATUS_NORMALIZED)
