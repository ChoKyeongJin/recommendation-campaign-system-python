"""회원별 스칼라 지표 **카탈로그 계약**의 고정 계약.

같은 물리 컬럼(``CRM_MB_MONTHCRMINFO.*``)에 두 계약이 걸려 있다.

    member_metric_<id>   kind=aggregate  + ranking_*   전역 순위(모집단·정렬·동점자 정책)
    member_scalar_<id>   kind=member_scalar, cardinality=scalar   회원 한 명의 값

둘을 섞으면 '구매주기가 30일 이하인 회원'이 순위 선언을 타면서 모집단 정책이 조용히 사라진다.
그래서 여기서 재는 것은 SQL 이 아니라 **계약이 오용을 이름과 함께 막는가**다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import member_scalar_metrics  # noqa: E402
import resolved_semantic_catalog  # noqa: E402
from resolved_semantic_catalog import CatalogError, MetricSpec  # noqa: E402

# 회원 지표 레지스트리가 선언한 9종. 목록을 여기 리터럴로 두는 이유는 이 파일이 **회귀 대장**
# 이기 때문이다 — 파생으로 세면 선언이 줄어들어도 초록이 된다.
PROFILE_SCALAR_METRIC_IDS: tuple[str, ...] = (
    "member_scalar_activity_month_cnt",
    "member_scalar_buy_cycle",
    "member_scalar_buy_product_cnt",
    "member_scalar_max_buy_amt",
    "member_scalar_mean_buy_amt",
    "member_scalar_min_buy_amt",
    "member_scalar_total_buy_amt",
    "member_scalar_total_buy_cnt",
    "member_scalar_total_buy_qty",
)


@pytest.fixture(scope="module")
def catalog() -> resolved_semantic_catalog.ResolvedSemanticCatalog:
    return audience_runtime.resolve_audience_catalog()


def test_catalog_declares_exactly_the_nine_profile_scalar_metrics(catalog) -> None:
    assert member_scalar_metrics.member_scalar_metric_ids(catalog) == PROFILE_SCALAR_METRIC_IDS


@pytest.mark.parametrize("metric_id", PROFILE_SCALAR_METRIC_IDS)
def test_member_scalar_contract_is_complete(metric_id: str, catalog) -> None:
    """회원별 스칼라는 grain=subject · cardinality=scalar · 순위 없음 · 집계 함수 없음."""

    metric = catalog.metric(metric_id)
    assert metric.kind == member_scalar_metrics.MEMBER_SCALAR_KIND
    assert metric.grain == resolved_semantic_catalog.SUBJECT_GRAIN
    assert metric.cardinality == "scalar"
    assert metric.aggregate_function is None
    assert metric.ranking_entity is None
    assert metric.ranking_limit_units == ()
    assert metric.null_behavior == "exclude"
    assert metric.zero_row_behavior == "exclude"
    assert "metric.member_scalar" in metric.required_capabilities
    # 값 필드는 같은 소스에 있어야 '회원 행 하나를 읽는다'가 성립한다.
    assert catalog.field(str(metric.expression_field)).source == metric.source
    # 소스는 팩트 테이블이어야 상관 EXISTS 서브쿼리로 낮출 수 있다.
    assert catalog.source(metric.source).event.binding == "fact_table"


@pytest.mark.parametrize("metric_id", PROFILE_SCALAR_METRIC_IDS)
def test_member_scalar_snapshot_is_pinned_to_the_latest_month(metric_id: str, catalog) -> None:
    """스냅샷 고정이 소스 선언에 있다 — 빠지면 과거 월 행이 조건을 만족시켜 대상이 부푼다."""

    spec = catalog.source(metric_id).event
    assert any("YYYYMM" in predicate for predicate in spec.extra_predicates)
    # 순위 모집단 정책(active_members 조인)은 이 계약에 들어오지 않는다.
    assert not any("MEMBER_STATE" in predicate for predicate in spec.extra_predicates)
    assert "IS NOT NULL" not in " ".join(spec.extra_predicates)


def test_global_ranking_metric_is_rejected_in_the_member_scalar_position(catalog) -> None:
    """전역 순위 지표를 회원별 스칼라 자리에 쓰면 **계약 오류**로 이름과 함께 막힌다."""

    assert catalog.metric("buy_cycle").kind == "aggregate"
    with pytest.raises(member_scalar_metrics.MemberScalarContractError) as error:
        member_scalar_metrics.validate_member_scalar_contract(
            catalog, "buy_cycle", operator="<=", value=30
        )
    assert error.value.code == member_scalar_metrics.CONTRACT_MISMATCH
    assert error.value.symbol == "buy_cycle"
    assert "aggregate" in str(error.value)


def test_unregistered_metric_is_rejected(catalog) -> None:
    with pytest.raises(member_scalar_metrics.MemberScalarContractError) as error:
        member_scalar_metrics.validate_member_scalar_contract(
            catalog, "member_scalar_not_declared", operator="<=", value=30
        )
    assert error.value.code == "catalog_metric_unregistered"


def test_valid_member_scalar_request_passes(catalog) -> None:
    metric = member_scalar_metrics.validate_member_scalar_contract(
        catalog, "member_scalar_buy_cycle", operator="<=", value=30
    )
    assert metric.id == "member_scalar_buy_cycle"


def test_unsupported_operator_is_rejected(catalog) -> None:
    with pytest.raises(member_scalar_metrics.MemberScalarContractError) as error:
        member_scalar_metrics.validate_member_scalar_contract(
            catalog, "member_scalar_buy_cycle", operator="~", value=30
        )
    assert error.value.code == member_scalar_metrics.CONTRACT_MISMATCH


@pytest.mark.parametrize("value", ["30", True, None, [30]])
def test_value_type_mismatch_is_rejected(value, catalog) -> None:
    """숫자 지표에 문자열·불리언 임계값을 넣으면 계약 오류다(암묵 변환 금지)."""

    with pytest.raises(member_scalar_metrics.MemberScalarContractError) as error:
        member_scalar_metrics.validate_member_scalar_contract(
            catalog, "member_scalar_buy_cycle", operator="<=", value=value
        )
    assert error.value.code == member_scalar_metrics.VALUE_TYPE_MISMATCH


# ── 선언 자체의 검증(합성 카탈로그) ──────────────────────────────────────────────────
# 실제 카탈로그는 이미 올바르므로, '틀린 선언이 로딩되지 않는다'는 여기서 잰다.


def _declaration(**overrides):
    declaration = {
        "source": "snapshot",
        "kind": "member_scalar",
        "cardinality": "scalar",
        "grain": "subject",
        "expression_field": "snapshot.value",
        "value_type": "number",
        "required_capabilities": ["metric.member_scalar"],
    }
    declaration.update(overrides)
    return declaration


def _catalog(metric_declaration):
    return resolved_semantic_catalog.resolve_semantic_catalog(
        registry={},
        runtime_config={
            "sources": {
                "snapshot": {
                    "table": "SNAPSHOT",
                    "alias": "S",
                    "subject_key": "MEMBER_NO",
                    "event_subject_key": "MEMBER_NO",
                    "time_column": "YYYYMM",
                    "time_format": "char6",
                    "binding": "fact_table",
                }
            },
            "fields": {
                "snapshot.value": {
                    "source": "snapshot",
                    "column": "VALUE",
                    "data_type": "number",
                }
            },
            "metrics": {"probe": metric_declaration},
        },
    )


def test_member_scalar_declaration_loads(catalog) -> None:
    resolved = _catalog(_declaration())
    assert resolved.metric("probe").kind == "member_scalar"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"cardinality": "set"}, "cardinality 'scalar'"),
        ({"grain": "order"}, "grain"),
        ({"function": "sum"}, "aggregate_function"),
        ({"required_capabilities": []}, "metric.member_scalar"),
        ({"required_capabilities": ["metric.not_declared"]}, "undeclared capabilities"),
        ({"expression_field": None}, "expression_field"),
        ({"null_behavior": "include_as_zero"}, "null_behavior"),
        ({"zero_row_behavior": "include"}, "zero_row_behavior"),
        ({"ranking_entity": "member"}, "ranking"),
    ],
)
def test_incomplete_member_scalar_declaration_fails_closed(overrides, fragment) -> None:
    with pytest.raises(CatalogError) as error:
        _catalog(_declaration(**overrides))
    assert fragment in str(error.value)


def test_scalar_cardinality_outside_member_scalar_fails_closed() -> None:
    """순위·집계 지표가 scalar cardinality 를 주장하면 로딩되지 않는다."""

    with pytest.raises(CatalogError) as error:
        _catalog(
            {
                "source": "snapshot",
                "kind": "aggregate",
                "function": "sum",
                "expression_field": "snapshot.value",
                "cardinality": "scalar",
            }
        )
    assert "scalar cardinality" in str(error.value)


def test_member_scalar_on_a_subject_column_source_fails_closed() -> None:
    """주체 컬럼 바인딩은 상관 EXISTS 서브쿼리를 만들 수 없다 — 교차검증이 막는다."""

    with pytest.raises(CatalogError) as error:
        resolved_semantic_catalog.resolve_semantic_catalog(
            registry={},
            runtime_config={
                "sources": {
                    "profile": {
                        "table": "MEMBER",
                        "alias": "B",
                        "subject_key": "MEMBER_NO",
                        "event_subject_key": "MEMBER_NO",
                        "time_column": "REG_DT",
                        "time_format": "char8",
                        "binding": "subject_column",
                    }
                },
                "fields": {
                    "profile.value": {
                        "source": "profile",
                        "column": "VALUE",
                        "data_type": "number",
                    }
                },
                "metrics": {
                    "probe": {
                        "source": "profile",
                        "kind": "member_scalar",
                        "cardinality": "scalar",
                        "expression_field": "profile.value",
                        "required_capabilities": ["metric.member_scalar"],
                    }
                },
            },
        )
    assert "fact_table" in str(error.value)


def test_capability_shortfall_is_a_contract_error_not_a_compile_exception() -> None:
    """capability 부족은 SQL 생성 중 예외가 아니라 lowering 전 계약 오류로 답한다."""

    resolved = _catalog(
        _declaration(
            required_capabilities=["metric.member_scalar", "relation.ranked_limit"]
        )
    )
    # 선언 자체는 유효하다(어휘 안의 이름이다).
    assert "relation.ranked_limit" in resolved.metric("probe").required_capabilities

    class _NoRankingDialect:
        name = "probe"

    original = event_compiler.compiler_capabilities
    try:
        event_compiler.compiler_capabilities = lambda dialect=None: frozenset()  # type: ignore[assignment]
        with pytest.raises(member_scalar_metrics.MemberScalarContractError) as error:
            member_scalar_metrics.validate_member_scalar_contract(
                resolved, "probe", operator="<=", value=1, dialect=_NoRankingDialect()
            )
    finally:
        event_compiler.compiler_capabilities = original  # type: ignore[assignment]
    assert error.value.code == member_scalar_metrics.CAPABILITY_UNSUPPORTED
    assert "relation.ranked_limit" in str(error.value)


def test_capability_vocabulary_is_closed() -> None:
    """카탈로그가 요구할 수 있는 capability 이름은 event_ir 이 단일 소유한다."""

    assert "metric.member_scalar" in event_ir.CAPABILITIES
    assert member_scalar_metrics.SUPPORTED_CAPABILITIES <= event_ir.CAPABILITIES
    assert event_compiler.compiler_capabilities() <= event_ir.CAPABILITIES
    assert isinstance(MetricSpec(id="x", source="y", kind="field",
                                 expression_field="y.z").required_capabilities, tuple)
