from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import AdministrativeRegion, ExternalCondition, ResolverResult


class RegionMappingError(ValueError):
    def __init__(self, code: str, message: str, unmapped: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.code = code
        self.unmapped = unmapped


@dataclass(frozen=True)
class RegionTargetBinding:
    """Validated physical binding for member-residence filters."""

    table: str
    alias: str
    sido_column: str
    sigungu_column: str
    entity: str
    attribute: str

    @property
    def sido_index_column(self) -> str:
        return self.sido_column.rsplit(".", 1)[-1].upper()

    @property
    def sigungu_index_column(self) -> str:
        return self.sigungu_column.rsplit(".", 1)[-1].upper()


def _binding_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegionMappingError(
            "target_binding_invalid", f"{path} must be an object", []
        )
    return value


def _binding_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegionMappingError(
            "target_binding_invalid", f"{path} must be a non-empty string", []
        )
    return value.strip()


def _binding_column(value: Any, *, alias: str, path: str) -> str:
    column = _binding_text(value, path)
    parts = tuple(part.strip() for part in column.split("."))
    if (
        len(parts) != 2
        or parts[0] != alias
        or any(not part or not part.isidentifier() for part in parts)
    ):
        raise RegionMappingError(
            "target_binding_invalid",
            f"{path} must be a qualified {alias}.column identifier",
            [],
        )
    return ".".join(parts)


def region_target_binding(config: Mapping[str, Any]) -> RegionTargetBinding:
    """Create a fail-closed region binding from the member-target registry."""

    region = _binding_mapping(config.get("region_target"), "region_target")
    target_basis = _binding_mapping(
        region.get("target_basis"), "region_target.target_basis"
    )
    columns = _binding_mapping(region.get("columns"), "region_target.columns")
    alias = _binding_text(region.get("alias"), "region_target.alias")
    return RegionTargetBinding(
        table=_binding_text(region.get("table"), "region_target.table"),
        alias=alias,
        sido_column=_binding_column(
            columns.get("sido"), alias=alias, path="region_target.columns.sido"
        ),
        sigungu_column=_binding_column(
            columns.get("sigungu"),
            alias=alias,
            path="region_target.columns.sigungu",
        ),
        entity=_binding_text(
            target_basis.get("entity"), "region_target.target_basis.entity"
        ),
        attribute=_binding_text(
            target_basis.get("attribute"), "region_target.target_basis.attribute"
        ),
    )


@dataclass(frozen=True)
class MappedRegion:
    external: AdministrativeRegion
    crm_sido_value: str
    crm_sigungu_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "external": self.external.to_dict(),
            "crm_sido_value": self.crm_sido_value,
            "crm_sigungu_value": self.crm_sigungu_value,
        }


class AdministrativeRegionMapper:
    """Map official administrative regions to exact values stored in CRM.

    No fuzzy matching is used.  Sido aliases come from a versioned mapping file;
    sigungu values must exist in the CRM member-value snapshot and belong to the
    mapped sido.  A provider city such as ``수원시`` can deterministically expand
    to the exact CRM children (``수원시 권선구`` etc.) when the CRM has no parent row.
    """

    def __init__(
        self,
        mapping_path: Path | str,
        member_value_index_path: Path | str,
        *,
        target_binding: RegionTargetBinding,
    ) -> None:
        self.mapping_path = Path(mapping_path)
        self.member_value_index_path = Path(member_value_index_path)
        self.target_binding = target_binding
        self._mapping = self._read_json(self.mapping_path)
        self._member_index = self._read_json(self.member_value_index_path)
        self.mapping_version = str(self._mapping.get("version") or "unknown")
        self._sido_entries = [
            item for item in self._mapping.get("sido", []) if isinstance(item, dict)
        ]
        hierarchy = self._member_index.get("region_hierarchy", {})
        self._sigungu_to_sido = hierarchy.get("sigungu_to_sido", {}) if isinstance(hierarchy, dict) else {}
        self._crm_values = self._column_values()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegionMappingError("mapping_config_unavailable", str(exc), []) from exc
        if not isinstance(payload, dict):
            raise RegionMappingError("mapping_config_invalid", f"{path} must contain an object", [])
        return payload

    def _column_values(self) -> dict[str, set[str]]:
        values: dict[str, set[str]] = {}
        for column in self._member_index.get("columns", []):
            if not isinstance(column, dict) or not isinstance(column.get("column"), str):
                continue
            values[column["column"].upper()] = {
                str(item.get("value"))
                for item in column.get("values", [])
                if isinstance(item, dict) and item.get("value") not in (None, "")
            }
        return values

    def parse_area_name(
        self, area_name: str, *, area_code: str | None = None
    ) -> list[AdministrativeRegion]:
        normalized = " ".join(str(area_name or "").replace(",", " ").split())
        if not normalized:
            return []
        sido_entry = self._find_sido(external_name=normalized, external_code=area_code, prefix=True)
        if sido_entry is not None:
            matched_alias = max(
                (
                    alias
                    for alias in self._sido_aliases(sido_entry)
                    if normalized == alias or normalized.startswith(alias + " ")
                ),
                key=len,
                default=normalized,
            )
            sigungu = normalized[len(matched_alias):].strip() or None
            return [AdministrativeRegion(
                country_code="KR",
                sido_code=str(sido_entry.get("external_code") or "") or None,
                sido_name=str(sido_entry.get("external_name") or matched_alias),
                sigungu_name=sigungu,
                source_area_code=area_code,
                source_area_name=area_name,
            )]

        parents = self._sigungu_to_sido.get(normalized, [])
        if len(parents) == 1:
            parent = self._find_sido(crm_value=str(parents[0]))
            if parent is not None:
                return [AdministrativeRegion(
                    country_code="KR",
                    sido_code=str(parent.get("external_code") or "") or None,
                    sido_name=str(parent.get("external_name") or parents[0]),
                    sigungu_name=normalized,
                    source_area_code=area_code,
                    source_area_name=area_name,
                )]
        return []

    def _sido_aliases(self, entry: dict[str, Any]) -> set[str]:
        return {
            str(value)
            for value in [entry.get("external_name"), entry.get("crm_value"), *(entry.get("aliases") or [])]
            if isinstance(value, str) and value
        }

    def _find_sido(
        self,
        *,
        external_name: str | None = None,
        external_code: str | None = None,
        crm_value: str | None = None,
        prefix: bool = False,
    ) -> dict[str, Any] | None:
        for entry in self._sido_entries:
            if external_code and str(entry.get("external_code") or "") == external_code:
                return entry
            if crm_value and str(entry.get("crm_value") or "") == crm_value:
                return entry
            if external_name:
                aliases = self._sido_aliases(entry)
                if external_name in aliases or (prefix and any(external_name.startswith(alias + " ") for alias in aliases)):
                    return entry
        return None

    def map_targets(self, targets: Iterable[AdministrativeRegion]) -> list[MappedRegion]:
        mapped: list[MappedRegion] = []
        unmapped: list[dict[str, Any]] = []
        for target in targets:
            entry = self._find_sido(
                external_name=target.sido_name,
                external_code=target.sido_code,
            )
            if entry is None:
                unmapped.append({**target.to_dict(), "reason": "sido_mapping_missing"})
                continue
            crm_sido = str(entry.get("crm_value") or "")
            if crm_sido not in self._crm_values.get(
                self.target_binding.sido_index_column, set()
            ):
                unmapped.append({**target.to_dict(), "reason": "crm_sido_value_missing"})
                continue
            if not target.sigungu_name:
                mapped.append(MappedRegion(target, crm_sido, None))
                continue

            candidates = self._sigungu_candidates(target.sigungu_name, crm_sido)
            if not candidates:
                unmapped.append({**target.to_dict(), "reason": "sigungu_mapping_missing"})
                continue
            mapped.extend(MappedRegion(target, crm_sido, value) for value in candidates)

        if unmapped:
            raise RegionMappingError(
                "region_mapping_incomplete",
                "one or more external regions could not be mapped to CRM",
                unmapped,
            )
        return self._minimize(mapped)

    def _sigungu_candidates(self, external_name: str, crm_sido: str) -> list[str]:
        values = self._crm_values.get(
            self.target_binding.sigungu_index_column, set()
        )
        exact_parents = self._sigungu_to_sido.get(external_name, [])
        if external_name in values and crm_sido in exact_parents:
            return [external_name]
        prefix = external_name.rstrip() + " "
        return sorted(
            value
            for value in values
            if value.startswith(prefix) and crm_sido in self._sigungu_to_sido.get(value, [])
        )

    @staticmethod
    def _minimize(regions: list[MappedRegion]) -> list[MappedRegion]:
        parent_sidos = {region.crm_sido_value for region in regions if region.crm_sigungu_value is None}
        result: list[MappedRegion] = []
        seen: set[tuple[str, str | None]] = set()
        for region in regions:
            if region.crm_sigungu_value is not None and region.crm_sido_value in parent_sidos:
                continue
            key = (region.crm_sido_value, region.crm_sigungu_value)
            if key not in seen:
                seen.add(key)
                result.append(region)
        return result

    def to_compound_dimension_filter(
        self,
        condition: ExternalCondition,
        result: ResolverResult,
    ) -> tuple[dict[str, Any], list[MappedRegion]]:
        if result.status != "resolved":
            raise RegionMappingError("result_not_resolved", "only resolved results can be mapped", [])
        if (
            condition.target_basis.get("entity") != self.target_binding.entity
            or condition.target_basis.get("attribute") != self.target_binding.attribute
        ):
            raise RegionMappingError(
                "target_basis_unsupported",
                "only member residence targeting is configured",
                [],
            )
        mapped = self.map_targets(result.targets)
        groups: list[dict[str, Any]] = []
        for region in mapped:
            filters = [{
                "table": self.target_binding.table,
                "column": self.target_binding.sido_column,
                "operator": "=",
                "value": region.crm_sido_value,
            }]
            if region.crm_sigungu_value:
                filters.append({
                    "table": self.target_binding.table,
                    "column": self.target_binding.sigungu_column,
                    "operator": "=",
                    "value": region.crm_sigungu_value,
                })
            groups.append({"logic": "AND", "filters": filters})
        if not groups:
            raise RegionMappingError("mapped_target_empty", "resolved targets produced no CRM filters", [])
        return ({
            "dimension_id": f"external:{condition.domain}:{condition.condition_type}:{condition.condition_code}",
            "condition_id": condition.id,
            "source": result.provider,
            "logic": "OR",
            "groups": groups,
            "mapping_version": self.mapping_version,
        }, mapped)
