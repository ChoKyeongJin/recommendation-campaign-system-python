from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ..classifier import configured_catalog
from ..mappers.administrative_region_mapper import AdministrativeRegionMapper
from ..models import (
    AdministrativeRegion,
    ExternalCondition,
    ResolutionContext,
    ResolverResult,
    parse_datetime,
)
from .base import ExternalConditionResolver


JsonFetcher = Callable[[str, Mapping[str, str], float], Any]
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class KmaWeatherAlertConfig:
    api_url: str
    api_key: str | None
    timeout_seconds: float
    cache_ttl_seconds: int
    max_response_age_seconds: int

    @classmethod
    def from_environment(cls) -> "KmaWeatherAlertConfig":
        return cls(
            api_url=os.getenv(
                "KMA_WEATHER_ALERT_API_URL",
                "https://apis.data.go.kr/1360000/WthrWrnInfoService/getPwnStatus",
            ),
            api_key=os.getenv("KMA_WEATHER_ALERT_API_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY"),
            timeout_seconds=float(os.getenv("KMA_WEATHER_ALERT_TIMEOUT_SECONDS", "5")),
            cache_ttl_seconds=int(os.getenv("KMA_WEATHER_ALERT_CACHE_TTL_SECONDS", "600")),
            max_response_age_seconds=int(os.getenv("KMA_WEATHER_ALERT_MAX_AGE_SECONDS", "21600")),
        )


def _fetch_json(url: str, params: Mapping[str, str], timeout: float) -> Any:
    query = urllib.parse.urlencode(params)
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        url + separator + query,
        headers={"Accept": "application/json", "User-Agent": "campaign-external-condition-resolver/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured official endpoint
        return json.loads(response.read().decode("utf-8"))


class KmaWeatherAlertResolver(ExternalConditionResolver):
    provider = "kma"
    resolver_name = "kma_weather_alert"
    resolver_version = "1.0"

    def __init__(
        self,
        config: KmaWeatherAlertConfig,
        region_mapper: AdministrativeRegionMapper,
        *,
        fetcher: JsonFetcher | None = None,
        catalog: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.region_mapper = region_mapper
        self.fetcher = fetcher or _fetch_json
        self.catalog = catalog or configured_catalog()
        self._provider_values = self._provider_value_map()

    def _provider_value_map(self) -> dict[str, set[str]]:
        values: dict[str, set[str]] = {}
        for spec in self.catalog.get("conditions", []):
            if not isinstance(spec, dict) or spec.get("domain") != "weather":
                continue
            code = str(spec.get("condition_code") or "")
            values[code] = {
                str(item).casefold()
                for item in [code, *(spec.get("aliases") or []), *(spec.get("provider_values") or [])]
                if str(item)
            }
        return values

    def supports(self, condition: ExternalCondition) -> bool:
        return (
            condition.domain == "weather"
            and condition.condition_type == "alert"
            and condition.condition_code in self._provider_values
        )

    def resolve(
        self, condition: ExternalCondition, context: ResolutionContext
    ) -> ResolverResult:
        observed_at = context.now
        expires_at = observed_at + timedelta(seconds=max(1, self.config.cache_ttl_seconds))
        if not self.config.api_key:
            return self._error(
                condition, observed_at, expires_at, "provider_not_configured",
                "KMA weather alert API key is not configured",
            )
        try:
            payload = self.fetcher(
                self.config.api_url,
                {
                    "ServiceKey": self.config.api_key,
                    "pageNo": "1",
                    "numOfRows": "1000",
                    "dataType": "JSON",
                },
                self.config.timeout_seconds,
            )
        except (TimeoutError, socket.timeout):
            return self._error(condition, observed_at, expires_at, "provider_timeout", "KMA request timed out")
        except (urllib.error.URLError, OSError) as exc:
            return self._error(condition, observed_at, expires_at, "provider_unavailable", exc.__class__.__name__)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return self._error(condition, observed_at, expires_at, "provider_response_invalid", exc.__class__.__name__)

        try:
            response_time = self._response_time(payload) or observed_at
            if response_time.tzinfo is None:
                # 기상청의 숫자형 발표 시각은 별도 offset이 없으며 한국 표준시 기준이다.
                response_time = response_time.replace(tzinfo=KST)
            if response_time > observed_at + timedelta(minutes=5):
                raise ValueError("response timestamp is in the future")
            if observed_at - response_time > timedelta(seconds=self.config.max_response_age_seconds):
                return self._error(condition, response_time, expires_at, "provider_response_stale", "KMA response is stale")
            items = self._items(payload)
            targets: list[AdministrativeRegion] = []
            for item in items:
                if not self._matches_condition(item, condition) or not self._is_active(item):
                    continue
                targets.extend(self._regions(item))
            targets = self._dedupe(targets)
            source_reference = "sha256:" + hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if not targets:
                return ResolverResult(
                    condition_id=condition.id,
                    status="empty",
                    provider=self.provider,
                    resolver=self.resolver_name,
                    resolver_version=self.resolver_version,
                    observed_at=response_time,
                    expires_at=max(expires_at, response_time + timedelta(seconds=1)),
                    source_reference=source_reference,
                    metadata={"item_count": len(items)},
                )
            return ResolverResult(
                condition_id=condition.id,
                status="resolved",
                provider=self.provider,
                resolver=self.resolver_name,
                resolver_version=self.resolver_version,
                observed_at=response_time,
                expires_at=max(expires_at, response_time + timedelta(seconds=1)),
                source_reference=source_reference,
                targets=tuple(targets),
                metadata={"item_count": len(items)},
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(condition, observed_at, expires_at, "provider_response_invalid", str(exc))

    def _error(
        self,
        condition: ExternalCondition,
        observed_at: datetime,
        expires_at: datetime,
        code: str,
        detail: str,
    ) -> ResolverResult:
        return ResolverResult(
            condition_id=condition.id,
            status="failed",
            provider=self.provider,
            resolver=self.resolver_name,
            resolver_version=self.resolver_version,
            observed_at=observed_at,
            expires_at=max(expires_at, observed_at + timedelta(seconds=1)),
            error_code=code,
            error_detail=detail[:240],
        )

    @staticmethod
    def _body(payload: Any) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("KMA response must be an object")
        response = payload.get("response")
        if isinstance(response, Mapping):
            header = response.get("header")
            if isinstance(header, Mapping) and str(header.get("resultCode") or "00") not in {"00", "0"}:
                raise ValueError("KMA response resultCode is not successful")
            body = response.get("body")
            return body if isinstance(body, Mapping) else response
        return payload

    @classmethod
    def _items(cls, payload: Any) -> list[Mapping[str, Any]]:
        body = cls._body(payload)
        items: Any = body.get("items", body.get("item", []))
        if isinstance(items, Mapping) and "item" in items:
            items = items["item"]
        if items in (None, ""):
            return []
        if isinstance(items, Mapping):
            return [items]
        if isinstance(items, list) and all(isinstance(item, Mapping) for item in items):
            return list(items)
        raise ValueError("KMA response items are invalid")

    @classmethod
    def _response_time(cls, payload: Any) -> datetime | None:
        body = cls._body(payload)
        for key in ("generatedAt", "observedAt", "dataTime", "tmFc", "timestamp"):
            parsed = parse_datetime(body.get(key))
            if parsed:
                return parsed
        timestamps = [
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=KST)
            for item in cls._items(payload)
            for key in ("tmFc", "issuedAt", "dataTime", "timestamp")
            if (parsed := parse_datetime(item.get(key))) is not None
        ]
        if timestamps:
            return max(timestamps)
        return None

    def _matches_condition(self, item: Mapping[str, Any], condition: ExternalCondition) -> bool:
        allowed = self._provider_values.get(condition.condition_code, set())
        candidates = [
            item.get(key)
            for key in (
                "conditionCode", "conditionName", "warnVar", "warnType", "warningType",
                "pwnCd", "event", "phenomenon", "title",
            )
            if item.get(key) not in (None, "")
        ]
        for candidate in candidates:
            folded = str(candidate).strip().casefold()
            if folded in allowed or any(
                value and (value in folded if not value.isdigit() else value == folded)
                for value in allowed
            ):
                return True
        return False

    @staticmethod
    def _is_active(item: Mapping[str, Any]) -> bool:
        state = " ".join(
            str(item.get(key) or "")
            for key in ("state", "status", "command", "cmd", "warnStress", "title")
        ).casefold()
        return not any(term in state for term in ("해제", "종료", "취소", "cancel", "ended", "inactive"))

    def _regions(self, item: Mapping[str, Any]) -> list[AdministrativeRegion]:
        sido_name = next((str(item[key]) for key in ("sidoName", "sido", "provinceName") if item.get(key)), None)
        sigungu_name = next((str(item[key]) for key in ("sigunguName", "sigungu", "cityName") if item.get(key)), None)
        sido_code = next((str(item[key]) for key in ("sidoCode", "provinceCode") if item.get(key)), None)
        sigungu_code = next((str(item[key]) for key in ("sigunguCode", "cityCode") if item.get(key)), None)
        area_code = next((str(item[key]) for key in ("areaCode", "regId", "zoneCode") if item.get(key)), None)
        area_name = next((str(item[key]) for key in ("areaName", "regionName", "zoneName", "area") if item.get(key)), None)
        if sido_name or sido_code:
            return [AdministrativeRegion(
                country_code="KR",
                sido_code=sido_code,
                sido_name=sido_name,
                sigungu_code=sigungu_code,
                sigungu_name=sigungu_name,
                source_area_code=area_code,
                source_area_name=area_name,
            )]
        if not area_name:
            return []
        regions: list[AdministrativeRegion] = []
        for name in (part.strip() for part in re_split_area_names(area_name)):
            regions.extend(self.region_mapper.parse_area_name(name, area_code=area_code))
        return regions

    @staticmethod
    def _dedupe(regions: list[AdministrativeRegion]) -> list[AdministrativeRegion]:
        output: list[AdministrativeRegion] = []
        seen = set()
        for region in regions:
            if region.identity() not in seen:
                seen.add(region.identity())
                output.append(region)
        return output


def re_split_area_names(value: str) -> list[str]:
    import re

    return [part for part in re.split(r"\s*(?:,|·|/|\n)\s*", value) if part]
