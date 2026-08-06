from __future__ import annotations

import copy
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import member_filters_config

from .mappers.administrative_region_mapper import (
    AdministrativeRegionMapper,
    RegionMappingError,
    region_target_binding,
)
from .models import ExternalCondition, ResolutionContext, ResolverResult
from .registry import ExternalConditionResolverRegistry
from .resolvers.kma_weather_alert import KmaWeatherAlertConfig, KmaWeatherAlertResolver


logger = logging.getLogger("external_conditions")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING_PATH = _PROJECT_ROOT / "docs/data/runtime/external/external_region_mapping.json"
DEFAULT_MEMBER_VALUE_INDEX_PATH = _PROJECT_ROOT / "docs/data/generated/member_value_index.json"


class ResolverResultCache:
    def __init__(self) -> None:
        self._values: dict[str, ResolverResult] = {}
        self._lock = threading.Lock()

    def get(self, key: str, now: datetime) -> ResolverResult | None:
        with self._lock:
            result = self._values.get(key)
            if result is None:
                return None
            if result.expires_at <= now:
                self._values.pop(key, None)
                return None
            return result

    def put(self, key: str, result: ResolverResult) -> None:
        with self._lock:
            self._values[key] = result


class ExternalConditionService:
    def __init__(
        self,
        registry: ExternalConditionResolverRegistry,
        filter_mapper: AdministrativeRegionMapper,
        *,
        cache: ResolverResultCache | None = None,
        enabled: bool = True,
    ) -> None:
        self.registry = registry
        self.filter_mapper = filter_mapper
        self.cache = cache or ResolverResultCache()
        self.enabled = enabled

    def resolve_plan(
        self,
        plan: dict[str, Any],
        context: ResolutionContext,
    ) -> dict[str, Any]:
        raw_conditions = plan.get("external_conditions")
        if not isinstance(raw_conditions, list) or not raw_conditions:
            return plan

        conditions = self._dedupe_conditions(raw_conditions)
        results: list[dict[str, Any]] = []
        filters: list[dict[str, Any]] = []
        updated_conditions: list[dict[str, Any]] = []
        blocked = False

        for raw in conditions:
            try:
                condition = ExternalCondition.from_dict(raw)
            except (TypeError, ValueError) as exc:
                blocked = True
                results.append(self._invalid_condition_result(raw, context, str(exc)))
                updated_conditions.append(copy.deepcopy(raw) | {"resolution_status": "failed"})
                continue

            resolver = self.registry.find_resolver(condition) if self.enabled else None
            if resolver is None:
                result = ResolverResult(
                    condition_id=condition.id,
                    status="unsupported",
                    provider="none",
                    resolver="none",
                    resolver_version="1.0",
                    observed_at=context.now,
                    expires_at=context.now + timedelta(seconds=1),
                    error_code=("external_conditions_disabled" if not self.enabled else "resolver_not_found"),
                    error_detail="No resolver is registered for this external condition",
                )
            else:
                cache_key = condition.cache_key(resolver.provider, context.country_code)
                result = self.cache.get(cache_key, context.now)
                cache_hit = result is not None
                if result is None:
                    try:
                        result = resolver.resolve(condition, context)
                        if not isinstance(result, ResolverResult):
                            raise TypeError("resolver must return ResolverResult")
                    except Exception as exc:  # provider adapters must never bypass fail-close
                        logger.warning(
                            "external_condition_resolver_failed resolver=%s error=%s",
                            resolver.resolver_name,
                            exc.__class__.__name__,
                        )
                        result = ResolverResult(
                            condition_id=condition.id,
                            status="failed",
                            provider=resolver.provider,
                            resolver=resolver.resolver_name,
                            resolver_version=resolver.resolver_version,
                            observed_at=context.now,
                            expires_at=context.now + timedelta(seconds=1),
                            error_code="resolver_execution_failed",
                            error_detail=exc.__class__.__name__,
                        )
                    if result.expires_at <= context.now:
                        result = ResolverResult(
                            condition_id=condition.id,
                            status="failed",
                            provider=resolver.provider,
                            resolver=resolver.resolver_name,
                            resolver_version=resolver.resolver_version,
                            observed_at=context.now,
                            expires_at=context.now + timedelta(seconds=1),
                            error_code="resolver_result_expired",
                            error_detail="Resolver returned an already expired result",
                        )
                    self.cache.put(cache_key, result)
                result_payload = result.to_dict()
                result_payload["cache_hit"] = cache_hit
                results.append(result_payload)

            if resolver is None:
                results.append(result.to_dict() | {"cache_hit": False})

            if result.status == "resolved":
                try:
                    generated_filter, mapped = self.filter_mapper.to_compound_dimension_filter(condition, result)
                    filters.append(generated_filter)
                    results[-1]["mapped_targets"] = [item.to_dict() for item in mapped]
                    results[-1]["generated_filter"] = copy.deepcopy(generated_filter)
                    results[-1]["mapping_version"] = self.filter_mapper.mapping_version
                except RegionMappingError as exc:
                    blocked = True
                    results[-1].update({
                        "status": "failed",
                        "error_code": exc.code,
                        "error_detail": str(exc),
                        "unmapped_targets": copy.deepcopy(exc.unmapped),
                        "generated_filter": None,
                    })
            else:
                blocked = True

            final_status = str(results[-1].get("status") or result.status)
            updated_conditions.append(
                condition.to_dict(resolution_status=final_status)  # type: ignore[arg-type]
            )

        plan["external_conditions"] = updated_conditions
        plan["external_condition_results"] = results
        plan["compound_dimension_filters"] = filters if not blocked else []
        plan["external_condition_resolution"] = {
            "status": "failed" if blocked else "resolved",
            "condition_count": len(conditions),
            "filter_count": len(filters) if not blocked else 0,
            "resolved_at": context.now.isoformat(),
        }
        logger.info(
            "external_condition_resolution status=%s conditions=%d filters=%d",
            plan["external_condition_resolution"]["status"],
            len(conditions),
            len(filters) if not blocked else 0,
        )
        return plan

    @staticmethod
    def _dedupe_conditions(values: list[Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for value in values:
            if not isinstance(value, dict):
                output.append({"invalid_value": copy.deepcopy(value)})
                continue
            signature = (
                str(value.get("domain") or ""),
                str(value.get("condition_type") or ""),
                str(value.get("condition_code") or ""),
                str(value.get("state") or "active"),
            )
            if signature not in seen:
                seen.add(signature)
                output.append(copy.deepcopy(value))
        return output

    @staticmethod
    def _invalid_condition_result(
        raw: dict[str, Any], context: ResolutionContext, detail: str
    ) -> dict[str, Any]:
        return {
            "condition_id": str(raw.get("id") or "invalid-external-condition"),
            "status": "failed",
            "provider": "none",
            "resolver": "none",
            "resolver_version": "1.0",
            "observed_at": context.now.isoformat(),
            "expires_at": (context.now + timedelta(seconds=1)).isoformat(),
            "targets": [],
            "error_code": "external_condition_invalid",
            "error_detail": detail[:240],
            "metadata": {},
            "cache_hit": False,
        }


def build_default_service() -> ExternalConditionService:
    mapping_path = Path(os.getenv("EXTERNAL_REGION_MAPPING_PATH", str(DEFAULT_MAPPING_PATH)))
    member_index_path = Path(
        os.getenv("EXTERNAL_MEMBER_VALUE_INDEX_PATH", str(DEFAULT_MEMBER_VALUE_INDEX_PATH))
    )
    mapper = AdministrativeRegionMapper(
        mapping_path,
        member_index_path,
        target_binding=region_target_binding(member_filters_config.load_config()),
    )
    kma = KmaWeatherAlertResolver(KmaWeatherAlertConfig.from_environment(), mapper)
    enabled = os.getenv("EXTERNAL_CONDITIONS_ENABLED", "true").strip().casefold() not in {
        "0", "false", "no", "off",
    }
    return ExternalConditionService(
        ExternalConditionResolverRegistry([kma]), mapper, enabled=enabled
    )


# 프로세스 전역 싱글턴(``get_default_service``/``reset_default_service``)은 2026-08-06 삭제됐다.
# 호출자가 없었고(기본 ``retrieve()`` 는 이 서비스를 만들지 않는다 — docs/guides/external_conditions.md),
# 전역 mutable state 는 테스트 간 상태 공유를 만든다. 주입 지점은 ``build_default_service`` 하나다.


def default_resolution_context(*, now: datetime) -> ResolutionContext:
    """Build a resolver context from an explicit request-scoped instant."""

    return ResolutionContext(now=now)
