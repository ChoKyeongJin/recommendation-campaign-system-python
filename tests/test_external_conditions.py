from __future__ import annotations

from datetime import datetime, timedelta, timezone

import networkx as nx

import graph_rag
from external_conditions.classifier import classify_external_conditions
from external_conditions.mappers.administrative_region_mapper import (
    AdministrativeRegionMapper,
    RegionMappingError,
)
from external_conditions.models import (
    AdministrativeRegion,
    ExternalCondition,
    ResolutionContext,
    ResolverResult,
)
from external_conditions.registry import ExternalConditionResolverRegistry
from external_conditions.resolvers.kma_weather_alert import (
    KmaWeatherAlertConfig,
    KmaWeatherAlertResolver,
)
from external_conditions.service import ExternalConditionService, ResolverResultCache


NOW = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)


def _mapper() -> AdministrativeRegionMapper:
    return AdministrativeRegionMapper(
        "docs/data/runtime/external/external_region_mapping.json",
        "docs/data/generated/member_value_index.json",
    )


def _condition() -> ExternalCondition:
    return ExternalCondition(
        id="external-condition-1",
        domain="weather",
        condition_type="alert",
        condition_code="heatwave",
        source_text="폭염특보 지역",
    )


def _plan() -> dict:
    return {"external_conditions": [_condition().to_dict()]}


def _config() -> KmaWeatherAlertConfig:
    return KmaWeatherAlertConfig(
        api_url="https://example.invalid/kma",
        api_key="test-key",
        timeout_seconds=0.1,
        cache_ttl_seconds=600,
        max_response_age_seconds=21_600,
    )




def test_classifier_does_not_turn_product_or_history_context_into_live_condition() -> None:
    assert classify_external_conditions("폭염 대비 양산을 전국 회원에게 판매해줘") == []
    assert classify_external_conditions("상품명이 폭염 양산인 상품 구매 고객") == []
    assert classify_external_conditions("지난해 폭염 캠페인 구매 고객을 찾아줘") == []


def test_mapper_preserves_sido_sigungu_hierarchy() -> None:
    mapper = _mapper()
    result = ResolverResult(
        condition_id=_condition().id,
        status="resolved",
        provider="test",
        resolver="test",
        resolver_version="1",
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        targets=(
            AdministrativeRegion(sido_code="11", sido_name="서울특별시"),
            AdministrativeRegion(sido_code="41", sido_name="경기도", sigungu_name="수원시"),
        ),
    )

    compound, mapped = mapper.to_compound_dimension_filter(_condition(), result)
    sql = graph_rag._compile_compound_dimension_filter(compound)

    assert len(mapped) > 2  # 수원시 부모는 CRM의 실제 하위 구 값으로 정확히 확장된다.
    assert "B.SIDO = '서울'" in sql
    assert "B.SIDO = '경기' AND B.SIGUNGU = '수원시" in sql
    assert "B.SIDO IN ('서울', '경기')" not in sql


def test_mapper_fails_when_any_region_is_unmapped() -> None:
    result = ResolverResult(
        condition_id=_condition().id,
        status="resolved",
        provider="test",
        resolver="test",
        resolver_version="1",
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        targets=(
            AdministrativeRegion(sido_code="11", sido_name="서울특별시"),
            AdministrativeRegion(sido_name="존재하지 않는 지역"),
        ),
    )
    try:
        _mapper().to_compound_dimension_filter(_condition(), result)
    except RegionMappingError as exc:
        assert exc.code == "region_mapping_incomplete"
        assert exc.unmapped
    else:
        raise AssertionError("unmapped region must fail closed")


def test_kma_resolver_normal_empty_timeout_and_cache() -> None:
    calls = 0

    def fetcher(_url: str, _params: dict[str, str], _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        return {
            "response": {
                "header": {"resultCode": "00"},
                "body": {"items": {"item": [
                    {"conditionName": "폭염", "status": "발령", "sidoName": "서울특별시"}
                ]}},
            }
        }

    mapper = _mapper()
    resolver = KmaWeatherAlertResolver(_config(), mapper, fetcher=fetcher)
    service = ExternalConditionService(
        ExternalConditionResolverRegistry([resolver]), mapper, cache=ResolverResultCache()
    )
    first = service.resolve_plan(_plan(), ResolutionContext(now=NOW))
    second = service.resolve_plan(_plan(), ResolutionContext(now=NOW + timedelta(minutes=1)))

    assert first["external_condition_resolution"]["status"] == "resolved"
    assert first["external_condition_results"][0]["cache_hit"] is False
    assert second["external_condition_results"][0]["cache_hit"] is True
    assert calls == 1
    third = service.resolve_plan(_plan(), ResolutionContext(now=NOW + timedelta(minutes=11)))
    assert third["external_condition_results"][0]["cache_hit"] is False
    assert calls == 2

    empty = KmaWeatherAlertResolver(
        _config(), mapper,
        fetcher=lambda *_args: {"response": {"header": {"resultCode": "00"}, "body": {"items": []}}},
    ).resolve(_condition(), ResolutionContext(now=NOW))
    assert empty.status == "empty"

    def timeout(*_args):
        raise TimeoutError

    timed_out = KmaWeatherAlertResolver(_config(), mapper, fetcher=timeout).resolve(
        _condition(), ResolutionContext(now=NOW)
    )
    assert timed_out.status == "failed"
    assert timed_out.error_code == "provider_timeout"

    invalid = KmaWeatherAlertResolver(
        _config(), mapper,
        fetcher=lambda *_args: {"response": {"header": {"resultCode": "00"}, "body": {"items": 123}}},
    ).resolve(_condition(), ResolutionContext(now=NOW))
    assert invalid.status == "failed"
    assert invalid.error_code == "provider_response_invalid"

    stale = KmaWeatherAlertResolver(
        _config(), mapper,
        fetcher=lambda *_args: {
            "response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [{
                "conditionName": "폭염", "status": "발령", "sidoName": "서울특별시",
                "tmFc": "202607290000",
            }]}}}
        },
    ).resolve(_condition(), ResolutionContext(now=NOW))
    assert stale.status == "failed"
    assert stale.error_code == "provider_response_stale"


def test_service_marks_unregistered_condition_unsupported() -> None:
    mapper = _mapper()
    plan = ExternalConditionService(
        ExternalConditionResolverRegistry([]), mapper
    ).resolve_plan(_plan(), ResolutionContext(now=NOW))

    assert plan["external_condition_resolution"]["status"] == "failed"
    assert plan["external_condition_results"][0]["status"] == "unsupported"
    assert plan["compound_dimension_filters"] == []


def test_service_rejects_already_expired_resolver_result() -> None:
    class ExpiredResolver:
        provider = "test"
        resolver_name = "expired"
        resolver_version = "1"

        def supports(self, _condition: ExternalCondition) -> bool:
            return True

        def resolve(self, condition: ExternalCondition, _context: ResolutionContext) -> ResolverResult:
            return ResolverResult(
                condition_id=condition.id,
                status="resolved",
                provider=self.provider,
                resolver=self.resolver_name,
                resolver_version=self.resolver_version,
                observed_at=NOW - timedelta(minutes=20),
                expires_at=NOW - timedelta(minutes=10),
                targets=(AdministrativeRegion(sido_code="11", sido_name="서울특별시"),),
            )

    mapper = _mapper()
    plan = ExternalConditionService(
        ExternalConditionResolverRegistry([ExpiredResolver()]), mapper
    ).resolve_plan(_plan(), ResolutionContext(now=NOW))

    assert plan["external_condition_results"][0]["status"] == "failed"
    assert plan["external_condition_results"][0]["error_code"] == "resolver_result_expired"
    assert plan["compound_dimension_filters"] == []


def test_external_failure_and_empty_results_never_generate_sql() -> None:
    for status, error_code in (("failed", "provider_unavailable"), ("empty", None)):
        plan = _plan()
        plan["external_conditions"][0]["resolution_status"] = status
        plan["external_condition_results"] = [{
            "condition_id": "external-condition-1",
            "status": status,
            "error_code": error_code,
        }]
        plan["external_condition_resolution"] = {"status": "failed"}
        plan["compound_dimension_filters"] = []

        result = graph_rag.build_sql_result(
            graph=nx.Graph(),
            query="폭염 지역에 양산 판매",
            query_plan=plan,
            context_nodes=[],
            schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
            default_limit=100,
        )

        assert result["sql"] is None
        assert result["failure_reason"] == "external_condition_resolution_failed"
        assert result["interpretation_status"] == "needs_clarification"
        assert result["failed_conditions"]
        assert graph_rag._api_status(result) == "needs_clarification"


def test_resolved_compound_filter_is_compiled_into_member_predicate() -> None:
    plan = _plan()
    result = ResolverResult(
        condition_id=_condition().id,
        status="resolved",
        provider="test",
        resolver="test",
        resolver_version="1",
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        targets=(AdministrativeRegion(sido_code="11", sido_name="서울특별시"),),
    )
    service = ExternalConditionService(
        ExternalConditionResolverRegistry([]), _mapper()
    )
    compound, _mapped = service.filter_mapper.to_compound_dimension_filter(_condition(), result)
    plan.update({
        "external_conditions": [_condition().to_dict(resolution_status="resolved")],
        "external_condition_results": [result.to_dict()],
        "external_condition_resolution": {"status": "resolved"},
        "compound_dimension_filters": [compound],
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
    })

    compiled = graph_rag.compile_member_target_conditions(plan)

    assert compiled["has_signal"] is True
    assert any("B.SIDO = '서울'" in predicate for predicate in compiled["predicates"])
