from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping


ResolutionStatus = Literal["pending", "resolved", "empty", "failed", "unsupported"]
RESULT_STATUSES = frozenset({"resolved", "empty", "failed", "unsupported"})


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    for candidate in (normalized, normalized.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d%H", "%Y%m%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class ExternalCondition:
    id: str
    domain: str
    condition_type: str
    condition_code: str
    state: str = "active"
    target_basis: Mapping[str, str] = field(
        default_factory=lambda: {"entity": "member", "attribute": "residence"}
    )
    resolution_status: ResolutionStatus = "pending"
    freshness_requirement: str = "unspecified"
    source_text: str | None = None
    source_span: tuple[int, int] | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExternalCondition":
        target_basis = value.get("target_basis")
        if not isinstance(target_basis, Mapping):
            target_basis = {"entity": "member", "attribute": "residence"}
        span = value.get("source_span")
        parsed_span = None
        if (
            isinstance(span, Mapping)
            and isinstance(span.get("start"), int)
            and isinstance(span.get("end"), int)
        ):
            parsed_span = (span["start"], span["end"])
        status = str(value.get("resolution_status") or "pending")
        if status not in RESULT_STATUSES | {"pending"}:
            raise ValueError("external condition resolution_status is invalid")
        freshness = str(
            value.get("freshness_requirement") or "unspecified"
        ).strip().casefold()
        if freshness not in {
            "unspecified",
            "live",
            "general_knowledge_non_realtime",
        }:
            raise ValueError("external condition freshness_requirement is invalid")
        required = {
            "id": _text(value.get("id")),
            "domain": _text(value.get("domain")),
            "condition_type": _text(value.get("condition_type")),
            "condition_code": _text(value.get("condition_code")),
        }
        missing = [name for name, item in required.items() if item is None]
        if missing:
            raise ValueError("external condition missing fields: " + ", ".join(missing))
        return cls(
            id=required["id"] or "",
            domain=required["domain"] or "",
            condition_type=required["condition_type"] or "",
            condition_code=required["condition_code"] or "",
            state=str(value.get("state") or "active"),
            target_basis={
                "entity": str(target_basis.get("entity") or "member"),
                "attribute": str(target_basis.get("attribute") or "residence"),
            },
            resolution_status=status,  # type: ignore[arg-type]
            freshness_requirement=freshness,
            source_text=_text(value.get("source_text")),
            source_span=parsed_span,
        )

    def cache_key(self, provider: str, country_code: str) -> str:
        return ":".join(
            (
                self.domain,
                self.condition_type,
                self.condition_code,
                self.state,
                provider,
                country_code,
                str(self.target_basis.get("entity") or "member"),
                str(self.target_basis.get("attribute") or "residence"),
            )
        )

    def to_dict(self, *, resolution_status: ResolutionStatus | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "domain": self.domain,
            "condition_type": self.condition_type,
            "condition_code": self.condition_code,
            "state": self.state,
            "target_basis": dict(self.target_basis),
            "resolution_status": resolution_status or self.resolution_status,
            "freshness_requirement": self.freshness_requirement,
        }
        if self.source_text:
            payload["source_text"] = self.source_text
        if self.source_span:
            payload["source_span"] = {
                "start": self.source_span[0],
                "end": self.source_span[1],
            }
        return payload


@dataclass(frozen=True)
class AdministrativeRegion:
    country_code: str = "KR"
    sido_code: str | None = None
    sido_name: str | None = None
    sigungu_code: str | None = None
    sigungu_name: str | None = None
    source_area_code: str | None = None
    source_area_name: str | None = None

    def identity(self) -> tuple[str | None, str | None, str | None, str | None]:
        return self.sido_code, self.sido_name, self.sigungu_code, self.sigungu_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "administrative_region",
            "country_code": self.country_code,
            "sido_code": self.sido_code,
            "sido_name": self.sido_name,
            "sigungu_code": self.sigungu_code,
            "sigungu_name": self.sigungu_name,
            "source_area_code": self.source_area_code,
            "source_area_name": self.source_area_name,
        }


@dataclass(frozen=True)
class ResolverResult:
    condition_id: str
    status: Literal["resolved", "empty", "failed", "unsupported"]
    provider: str
    resolver: str
    resolver_version: str
    observed_at: datetime
    expires_at: datetime
    targets: tuple[AdministrativeRegion, ...] = ()
    source_reference: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        errors = validate_resolver_result(self)
        if errors:
            raise ValueError("; ".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "status": self.status,
            "provider": self.provider,
            "resolver": self.resolver,
            "resolver_version": self.resolver_version,
            "source_reference": self.source_reference,
            "observed_at": self.observed_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "targets": [target.to_dict() for target in self.targets],
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True)
class ResolutionContext:
    now: datetime
    country_code: str = "KR"
    request_id: str | None = None


def validate_resolver_result(result: ResolverResult) -> list[str]:
    errors: list[str] = []
    if result.status == "resolved" and not result.targets:
        errors.append("resolved result must contain targets")
    if result.status in {"failed", "unsupported"} and not result.error_code:
        errors.append(f"{result.status} result must contain error_code")
    if result.expires_at <= result.observed_at:
        errors.append("expires_at must be later than observed_at")
    seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
    for target in result.targets:
        if not target.sido_code and not target.sido_name:
            errors.append("administrative region must contain sido code or name")
        identity = target.identity()
        if identity in seen:
            errors.append("resolver result contains duplicate administrative regions")
        seen.add(identity)
    return errors
