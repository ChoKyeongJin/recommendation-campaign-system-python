"""Metric·Binding 카탈로그의 고정 계약 — 교차검증, 관측 능력 선언, 적재 판정, binding 해석.

이 파일은 데이터베이스에 연결하지 않는다. 재는 것은 **선언이 서로 어긋날 때 조용히 통과하지
않는가**이다 — 이 저장소는 읽히지 않는 선언(오타 난 coverage 키, 배선되지 않은 별칭)이 사실과
갈라지는 사고를 이미 겪었고, 그 결함은 SQL 이 아니라 결과 집합으로만 드러났다.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import temporal_ir  # noqa: E402
from temporal_ir import calendar as tcal  # noqa: E402
from temporal_ir import catalog as tcat  # noqa: E402
from temporal_ir import operators as tops  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
SNAPSHOT = "member.grade.monthly_snapshot"


@pytest.fixture(scope="module")
def semantic_catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def payload() -> dict:
    import json

    return json.loads(tcat.DEFAULT_TEMPORAL_CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog(semantic_catalog) -> tcat.TemporalCatalog:
    return temporal_ir.create_temporal_runtime(semantic_catalog).temporal_catalog


def _load(semantic_catalog, payload: dict) -> tcat.TemporalCatalog:
    return tcat.load_temporal_catalog(
        semantic_catalog,
        payload=payload,
        known_operators=tops.create_default_temporal_operator_registry().names(),
    )


def _mutated(payload: dict, binding: str, **changes) -> dict:
    clone = copy.deepcopy(payload)
    clone["bindings"][binding].update(changes)
    return clone


# ── 실카탈로그 ────────────────────────────────────────────────────────────────────


def test_shipped_catalog_loads_and_cross_checks_against_the_physical_catalog(catalog) -> None:
    binding = catalog.binding(SNAPSHOT)
    assert binding.representation is tcat.Representation.PERIODIC_SNAPSHOT
    assert binding.semantic_grain is sir.TimeUnit.MONTH
    assert binding.bucket_semantics is tcat.BucketSemantics.PERIOD_END_STATE
    assert binding.correlation_keys["subject"] == binding.correlation_keys["source"]
    assert binding.unique_per_time_point


def test_every_binding_declares_all_observation_capabilities(catalog) -> None:
    for binding in catalog.bindings.values():
        assert set(binding.observation_capabilities.to_dict()) == set(tcat.FLAG_NAMES), binding.id


def test_every_declared_operator_is_registered(catalog) -> None:
    names = set(tops.create_default_temporal_operator_registry().names())
    for binding in catalog.bindings.values():
        assert binding.supported_operators <= names, binding.id


# ── 선언 교차검증 ─────────────────────────────────────────────────────────────────


def test_capability_declaration_must_be_complete(semantic_catalog, payload) -> None:
    """빠진 항목을 기본값으로 채우면 그 기본값이 곧 추측이다."""
    broken = copy.deepcopy(payload)
    broken["bindings"][SNAPSHOT]["observation_capabilities"].pop("supports_event_count")
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert "supports_event_count" in str(error.value)


def test_contradictory_capabilities_are_rejected(semantic_catalog, payload) -> None:
    broken = copy.deepcopy(payload)
    broken["bindings"][SNAPSHOT]["observation_capabilities"]["supports_intra_bucket_changes"] = True
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert "supports_exact_transition_time" in str(error.value)


def test_correlation_key_mismatch_is_named(semantic_catalog, payload) -> None:
    """상관 조건의 주인은 카탈로그다 — 물리 선언과 어긋나면 로딩이 실패한다."""
    broken = _mutated(payload, SNAPSHOT, correlation_keys={"subject": "MEMBER_ID", "source": "MEMBER_NO"})
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "temporal_catalog_reference_mismatch"


def test_grain_mismatch_with_the_storage_column_is_named(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, semantic_grain="day")
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert "month" in str(error.value)


def test_storage_codec_mismatch_is_named(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, storage_codec="char8")
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "temporal_catalog_reference_mismatch"


def test_value_domain_mismatch_between_metric_and_binding_is_named(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, value_field="member_month_snapshot.newproduct_favor")
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "temporal_catalog_reference_mismatch"


def test_unknown_field_reference_is_named(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, value_field="member_month_snapshot.not_a_field")
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "temporal_catalog_reference_unresolved"


def test_uniqueness_claim_is_cross_checked_against_row_identity(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, row_identity=["MEMBER_NO", "YYYYMM", "ZTS_GRADE"])
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "temporal_catalog_reference_mismatch"


def test_point_state_binding_with_many_rows_needs_a_tie_breaker(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, unique_per_time_point=False)
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert "tie_breaker" in str(error.value)


def test_current_only_binding_cannot_declare_a_time_field(semantic_catalog, payload) -> None:
    broken = _mutated(payload, "member.grade.current", time_field="member_month_snapshot.occurred_at")
    with pytest.raises(tcat.TemporalCatalogError):
        _load(semantic_catalog, broken)


def test_unknown_operator_name_in_a_binding_is_rejected(semantic_catalog, payload) -> None:
    broken = _mutated(payload, SNAPSHOT, supported_operators=["temporal.as_of", "temporal.someday"])
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert "temporal.someday" in str(error.value)


def test_metric_and_binding_must_point_at_each_other(semantic_catalog, payload) -> None:
    broken = copy.deepcopy(payload)
    broken["metrics"]["member.grade"]["temporal_bindings"] = ["member.grade.current"]
    broken["metrics"]["member.grade"]["binding_priority"] = ["member.grade.current"]
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "temporal_catalog_reference_mismatch"


def test_binding_priority_cannot_name_an_unowned_binding(semantic_catalog, payload) -> None:
    broken = copy.deepcopy(payload)
    broken["metrics"]["member.grade"]["binding_priority"] = ["member.login.latest"]
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert error.value.code == "invalid_temporal_declaration"


def test_unknown_coverage_key_is_rejected(semantic_catalog, payload) -> None:
    """선언 키 오타는 경계를 조용히 None 으로 만든다 — 이 저장소가 실제로 겪은 결함이다."""
    broken = copy.deepcopy(payload)
    broken["bindings"][SNAPSHOT]["coverage"]["avaliable_from"] = "20170101"
    with pytest.raises(tcat.TemporalCatalogError) as error:
        _load(semantic_catalog, broken)
    assert "avaliable_from" in str(error.value)


# ── 적재 판정 ─────────────────────────────────────────────────────────────────────


def _interval(start: tuple[int, int, int], end: tuple[int, int, int]) -> tcal.TimeInterval:
    return tcal.TimeInterval(datetime(*start, tzinfo=SEOUL), datetime(*end, tzinfo=SEOUL))


def test_coverage_inside_the_declared_range_is_evaluable(catalog) -> None:
    coverage = catalog.binding(SNAPSHOT).coverage
    verdict = coverage.assess(_interval((2017, 1, 1), (2017, 2, 1)), sir.TimeUnit.MONTH, "Asia/Seoul")
    assert verdict.status is tcat.CoverageStatus.EVALUABLE
    assert verdict.missing_buckets == ()


def test_coverage_outside_the_declared_range_is_reported(catalog) -> None:
    coverage = catalog.binding(SNAPSHOT).coverage
    verdict = coverage.assess(_interval((2026, 7, 1), (2026, 8, 1)), sir.TimeUnit.MONTH, "Asia/Seoul")
    assert verdict.status is tcat.CoverageStatus.OUT_OF_COVERAGE


def test_partially_covered_span_is_incomplete_with_named_missing_buckets(catalog) -> None:
    coverage = catalog.binding(SNAPSHOT).coverage
    verdict = coverage.assess(_interval((2017, 1, 1), (2017, 4, 1)), sir.TimeUnit.MONTH, "Asia/Seoul")
    assert verdict.status is tcat.CoverageStatus.INCOMPLETE
    assert verdict.missing_buckets == ("201702", "201703")


def test_declared_missing_partition_inside_the_range_is_detected() -> None:
    """범위 안이어도 중간 파티션이 비면 전칭 판정은 답할 수 없다(§14)."""
    coverage = tcat.CoverageSpec.from_dict({
        "available_from": "20260101",
        "available_through": "20261231",
        "expected_cadence": "month",
        "completeness": "complete",
        "missing_partitions": ["202605"],
    })
    verdict = coverage.assess(_interval((2026, 4, 1), (2026, 7, 1)), sir.TimeUnit.MONTH, "Asia/Seoul")
    assert verdict.status is tcat.CoverageStatus.INCOMPLETE
    assert verdict.missing_buckets == ("202605",)


def test_freshness_lag_is_reported_as_a_warning() -> None:
    coverage = tcat.CoverageSpec.from_dict({
        "available_from": "20260101",
        "available_through": "20261231",
        "completeness": "complete",
        "freshness_through": "20260430",
    })
    verdict = coverage.assess(_interval((2026, 4, 1), (2026, 7, 1)), sir.TimeUnit.MONTH, "Asia/Seoul")
    assert "freshness_lag" in verdict.warnings


def test_partitions_only_coverage_is_still_a_declaration() -> None:
    """적재 선언은 두 날짜만이 아니다 — 결측 파티션만 선언해도 그 사실이 판정에 남아야 한다.

    예전에는 `declared` 가 available_from/through 만 봐서, 결측 파티션 선언이 통째로 무시되고
    없는 칸이 '적재 정상'으로 보고됐다(리뷰 실측).
    """
    coverage = tcat.CoverageSpec.from_dict(
        {"missing_partitions": ["202605"], "completeness": "incomplete"}
    )
    assert coverage.declared
    verdict = coverage.assess(_interval((2026, 4, 1), (2026, 7, 1)), sir.TimeUnit.MONTH, "Asia/Seoul")
    assert verdict.status is tcat.CoverageStatus.INCOMPLETE
    assert verdict.missing_buckets == ("202605",)


def test_undeclared_coverage_says_so_instead_of_pretending(catalog) -> None:
    coverage = catalog.binding("member.purchase.events").coverage
    verdict = coverage.assess(_interval((2026, 7, 1), (2026, 8, 1)), sir.TimeUnit.DAY, "Asia/Seoul")
    assert verdict.status is tcat.CoverageStatus.EVALUABLE
    assert verdict.warnings == ("coverage_undeclared",)


# ── binding 해석 ─────────────────────────────────────────────────────────────────


def test_explicit_binding_must_belong_to_the_metric(catalog) -> None:
    with pytest.raises(tcat.BindingResolutionError) as error:
        catalog.resolve_binding(
            "member.worth_grade", operator="temporal.as_of", requested=SNAPSHOT
        )
    assert error.value.code == "temporal_binding_metric_mismatch"


def test_explicit_binding_must_declare_the_operator(catalog) -> None:
    with pytest.raises(tcat.BindingResolutionError) as error:
        catalog.resolve_binding(
            "member.grade", operator="temporal.every_bucket", requested="member.grade.current"
        )
    assert error.value.code == "temporal_operator_unsupported_by_binding"


def test_metric_without_a_binding_for_the_operator_is_named(catalog) -> None:
    with pytest.raises(tcat.BindingResolutionError) as error:
        catalog.resolve_binding("member.login", operator="temporal.every_bucket")
    assert error.value.code == "temporal_operator_unsupported_by_metric"


def test_candidates_follow_the_declared_priority(catalog) -> None:
    candidates = catalog.candidate_bindings("member.grade", operator="temporal.as_of")
    assert [item.id for item in candidates] == ["member.grade.current", SNAPSHOT]


def test_ambiguous_bindings_are_not_resolved_arbitrarily(semantic_catalog, payload) -> None:
    """우선순위 선언이 없으면 고르지 않는다 — 임의 선택은 절반의 확률로 다른 집합이다.

    같은 계약이 **실행 경로**에서도 집행되는지는
    tests/test_temporal_lowering.py::test_two_viable_bindings_without_priority_are_refused 가 잰다
    (이 메서드만 검사하면 프로덕션이 쓰지 않는 함수를 지키는 셈이 된다).
    """
    ambiguous = copy.deepcopy(payload)
    ambiguous["metrics"]["member.grade"].pop("binding_priority")
    catalog = _load(semantic_catalog, ambiguous)
    with pytest.raises(tcat.BindingResolutionError) as error:
        catalog.resolve_binding("member.grade", operator="temporal.as_of")
    assert error.value.code == "temporal_binding_ambiguous"


# ── 직렬화 ────────────────────────────────────────────────────────────────────────


def test_binding_spec_round_trips_through_its_wire_form(catalog, semantic_catalog, payload) -> None:
    original = catalog.binding(SNAPSHOT)
    restored = tcat.TemporalBindingSpec.from_dict(original.id, original.to_dict())
    assert restored == original
