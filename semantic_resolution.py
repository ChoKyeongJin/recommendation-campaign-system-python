"""Declarative reconciliation of model-declared missing fields.

The LLM semantic IR may name a requirement differently from the executable
plan (for example ``customer_location`` versus a SIDO dimension filter).  This
module keeps those names and ownership rules out of the pipeline orchestrator.
It builds a set of grounded requirement evidence from the final plan and then
performs an auditable set match.  Unknown or structurally incomplete fields
remain unresolved by design.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_REGISTRY_PATH = Path("docs/data/semantic_resolution_registry.json")


class SemanticResolutionRegistryError(ValueError):
    """Raised when the declarative resolution registry is malformed."""


@dataclass(frozen=True)
class ResolutionEvidence:
    requirement_id: str
    resolved_by: str
    reason: str
    aliases: tuple[str, ...] = ()

    def to_result(self, field: str) -> dict[str, str]:
        return {
            "field": field,
            "resolved_by": self.resolved_by,
            "reason": self.reason,
        }


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _normalize_identifier(value: str) -> str:
    value = re.sub(r"\[\d+\]", "", value.strip().casefold())
    return re.sub(r"[^0-9a-z가-힣]+", "_", value).strip("_")


def _basic_identifier_keys(value: Any, registry: Mapping[str, Any]) -> set[str]:
    if not isinstance(value, str) or not value.strip():
        return set()
    compact = _normalize_identifier(value)
    if not compact:
        return set()
    keys = {compact}
    candidate = compact
    prefixes = registry.get("structural_prefixes") or []
    changed = True
    while changed:
        changed = False
        for raw_prefix in prefixes:
            prefix = _normalize_identifier(str(raw_prefix)) + "_"
            if candidate.startswith(prefix) and len(candidate) > len(prefix):
                candidate = candidate[len(prefix):]
                keys.add(candidate)
                changed = True
                break
    for key in tuple(keys):
        for raw_suffix in registry.get("structural_suffixes") or []:
            suffix = "_" + _normalize_identifier(str(raw_suffix))
            if key.endswith(suffix) and len(key) > len(suffix):
                keys.add(key[:-len(suffix)])
        if key.endswith("s") and len(key) > 4:
            keys.add(key[:-1])
    return keys


def _requirements(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = registry.get("requirements")
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _identifier_keys(value: Any, registry: Mapping[str, Any]) -> set[str]:
    keys = _basic_identifier_keys(value, registry)
    if not keys:
        return keys
    # An alias may intentionally point to more than one owner (purchase_date is
    # owned by either a trend or an entity ranking).  Preserve every candidate;
    # only evidence actually present in the plan can resolve it.
    for requirement in _requirements(registry):
        requirement_id = str(requirement.get("id") or "")
        aliases = [requirement_id, *(requirement.get("aliases") or [])]
        if any(keys & _basic_identifier_keys(alias, registry) for alias in aliases):
            keys.update(_basic_identifier_keys(requirement_id, registry))
    return keys


def _read_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _complete_calendar_window(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and isinstance(value.get("from"), str)
        and value["from"]
        and isinstance(value.get("to"), str)
        and value["to"]
    )


def _valid_value(value: Any, validator: str) -> bool:
    if validator == "non_empty":
        return _non_empty(value)
    if validator == "non_empty_string":
        return isinstance(value, str) and bool(value.strip())
    if validator == "positive_integer":
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if validator == "calendar_window":
        return _complete_calendar_window(value)
    if validator == "relative_change":
        return bool(
            isinstance(value, Mapping)
            and any(
                isinstance(item, Mapping)
                and item.get("operator") in {">=", ">", "<=", "<"}
                and isinstance(item.get("value"), (int, float))
                and not isinstance(item.get("value"), bool)
                for item in value.get("comparisons", [])
            )
        )
    raise SemanticResolutionRegistryError(f"unsupported semantic evidence validator: {validator}")


def _source_span(entity_set: Mapping[str, Any], name: str) -> str | None:
    surface = entity_set.get("surface")
    spans = entity_set.get("spans") if isinstance(entity_set.get("spans"), Mapping) else {}
    span = spans.get(name)
    if not (
        isinstance(surface, str)
        and isinstance(span, (list, tuple))
        and len(span) == 2
        and all(isinstance(index, int) and not isinstance(index, bool) for index in span)
    ):
        return None
    start, end = span
    return surface[start:end] if 0 <= start < end <= len(surface) else None


def _entity_window_has_provenance(plan: Mapping[str, Any], entity_set: Mapping[str, Any]) -> bool:
    ownership = any(
        isinstance(item, Mapping)
        and item.get("slot") == "target_user.purchase_date"
        and item.get("owner") == "entity_set_condition"
        and item.get("outcome") == "removed"
        for item in (plan.get("superseded_conditions") or [])
    )
    spans = entity_set.get("spans") if isinstance(entity_set.get("spans"), Mapping) else {}
    window_span = spans.get("window")
    window = entity_set.get("window")
    literal_match = any(
        isinstance(item, Mapping)
        and item.get("kind") == "date_window"
        and isinstance(window_span, (list, tuple))
        and len(window_span) == 2
        and [item.get("start"), item.get("end")] == list(window_span)
        and isinstance(item.get("normalized"), Mapping)
        and isinstance(window, Mapping)
        and item["normalized"].get("from") == window.get("from")
        and item["normalized"].get("to") == window.get("to")
        for item in (plan.get("literal_bindings") or [])
    )
    decision = any(
        isinstance(item, Mapping)
        and item.get("filter") == "apply_entity_set_condition"
        and item.get("action") == "set"
        and item.get("slot") == "target_user.entity_set_condition"
        for item in (plan.get("decisions") or [])
    )
    return ownership or literal_match or decision


def _validate_registry(registry: Any) -> dict[str, Any]:
    if not isinstance(registry, dict) or registry.get("version") != 1:
        raise SemanticResolutionRegistryError("semantic resolution registry version must be 1")
    for key in (
        "structural_prefixes", "structural_suffixes", "dimension_operators",
        "top_level_plan_fields", "requirements",
    ):
        if not isinstance(registry.get(key), list):
            raise SemanticResolutionRegistryError(f"semantic resolution registry {key} must be an array")
    for key in ("structural_prefixes", "structural_suffixes", "dimension_operators", "top_level_plan_fields"):
        if not all(isinstance(value, str) and value for value in registry[key]):
            raise SemanticResolutionRegistryError(f"semantic resolution registry {key} must contain strings")
    seen: set[str] = set()
    supported_kinds = {"path", "composite_path", "dimension_group", "entity_path"}
    supported_validators = {
        "non_empty", "non_empty_string", "positive_integer", "calendar_window", "relative_change",
    }
    for index, requirement in enumerate(registry["requirements"]):
        if not isinstance(requirement, dict):
            raise SemanticResolutionRegistryError(f"requirements[{index}] must be an object")
        requirement_id = requirement.get("id")
        evidence = requirement.get("evidence")
        if not isinstance(requirement_id, str) or not requirement_id or requirement_id in seen:
            raise SemanticResolutionRegistryError(f"requirements[{index}].id is invalid or duplicated")
        seen.add(requirement_id)
        if not isinstance(requirement.get("aliases"), list) or not all(
            isinstance(alias, str) and alias for alias in requirement["aliases"]
        ):
            raise SemanticResolutionRegistryError(f"requirements[{index}].aliases must contain strings")
        if not isinstance(evidence, dict) or evidence.get("kind") not in supported_kinds:
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.kind is unsupported")
        if not isinstance(requirement.get("reason"), str) or not requirement["reason"]:
            raise SemanticResolutionRegistryError(f"requirements[{index}].reason is required")
        kind = evidence["kind"]
        if kind in {"path", "entity_path"} and not (
            isinstance(evidence.get("path"), str) and evidence["path"]
        ):
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.path is required")
        if kind == "composite_path" and not (
            isinstance(evidence.get("paths"), list)
            and evidence["paths"]
            and all(isinstance(path, str) and path for path in evidence["paths"])
        ):
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.paths is required")
        if kind == "dimension_group" and not (
            isinstance(evidence.get("group"), str) and evidence["group"]
        ):
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.group is required")
        validator = evidence.get("validator")
        if kind != "dimension_group" and validator not in supported_validators:
            raise SemanticResolutionRegistryError(
                f"requirements[{index}].evidence.validator is unsupported"
            )
        if evidence.get("resolved_by") is not None and not isinstance(evidence["resolved_by"], str):
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.resolved_by must be a string")
        if evidence.get("source_span") is not None and not isinstance(evidence["source_span"], str):
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.source_span must be a string")
        if evidence.get("provenance") not in {None, "entity_window"}:
            raise SemanticResolutionRegistryError(f"requirements[{index}].evidence.provenance is unsupported")
    return registry


@lru_cache(maxsize=4)
def _load_registry_cached(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticResolutionRegistryError(f"semantic resolution registry load failed: {exc}") from exc
    return _validate_registry(payload)


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    return _load_registry_cached(str(Path(path).resolve()))


def _direct_plan_evidence(plan: Mapping[str, Any], registry: Mapping[str, Any]) -> list[ResolutionEvidence]:
    evidence: list[ResolutionEvidence] = []
    for container_name in ("target_user", "exclude", "campaign_constraints"):
        container = plan.get(container_name)
        if not isinstance(container, Mapping):
            continue
        for slot, value in container.items():
            if not _non_empty(value):
                continue
            path = f"{container_name}.{slot}"
            evidence.append(ResolutionEvidence(
                requirement_id=path,
                aliases=(slot, f"customer_{slot}", f"member_{slot}", f"audience_{slot}"),
                resolved_by=path,
                reason="grounded_plan_slot",
            ))
    for field in registry.get("top_level_plan_fields") or []:
        if isinstance(field, str) and _non_empty(plan.get(field)):
            evidence.append(ResolutionEvidence(
                requirement_id=field,
                aliases=(f"plan.{field}",),
                resolved_by=field,
                reason="grounded_plan_slot",
            ))
    return evidence


def _dimension_evidence(
    plan: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    member_table: str,
    region_columns: set[str],
) -> list[ResolutionEvidence]:
    evidence: list[ResolutionEvidence] = []
    normalized_regions = {column.split(".")[-1].upper() for column in region_columns}
    allowed_operators = {str(value).upper() for value in registry.get("dimension_operators") or []}
    for index, item in enumerate(plan.get("dimension_filters") or []):
        if not isinstance(item, Mapping):
            continue
        operator = str(item.get("operator") or "IN").upper()
        codes = item.get("codes")
        column = str(item.get("column") or "")
        table = str(item.get("table") or member_table)
        if (
            operator not in allowed_operators
            or not column
            or not isinstance(codes, list)
            or not any(isinstance(code, str) and code for code in codes)
            or (table != member_table and not item.get("join_column"))
        ):
            continue
        column_short = column.split(".")[-1]
        dimension_id = str(item.get("dimension_id") or "")
        label = dimension_id or column_short or str(index)
        aliases = tuple(str(value) for value in (
            dimension_id,
            dimension_id.rsplit(":", 1)[-1],
            item.get("prompt_label") or "",
            column_short,
        ) if value)
        evidence.append(ResolutionEvidence(
            requirement_id=f"dimension.{label}",
            aliases=aliases,
            resolved_by=f"dimension_filters.{label}",
            reason="grounded_dimension_filter",
        ))
        if table == member_table and column_short.upper() in normalized_regions:
            for requirement in _requirements(registry):
                spec = requirement.get("evidence") or {}
                if spec.get("kind") == "dimension_group" and spec.get("group") == "member_region":
                    evidence.append(ResolutionEvidence(
                        requirement_id=requirement["id"],
                        aliases=tuple(requirement.get("aliases") or []),
                        resolved_by=f"dimension_filters.{label}",
                        reason=requirement["reason"],
                    ))
    return evidence


def _registered_evidence(
    plan: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    compiled_entity_set: Mapping[str, Any] | None,
) -> list[ResolutionEvidence]:
    evidence_rows: list[ResolutionEvidence] = []
    for requirement in _requirements(registry):
        spec = requirement.get("evidence") or {}
        kind = spec.get("kind")
        if kind == "dimension_group":
            continue
        resolved_by = str(spec.get("resolved_by") or spec.get("path") or requirement["id"])
        valid = False
        if kind == "path":
            valid = _valid_value(_read_path(plan, str(spec.get("path") or "")), str(spec.get("validator") or "non_empty"))
        elif kind == "composite_path":
            paths = spec.get("paths") or []
            validator = str(spec.get("validator") or "non_empty")
            valid = bool(paths) and all(_valid_value(_read_path(plan, str(path)), validator) for path in paths)
        elif kind == "entity_path" and isinstance(compiled_entity_set, Mapping):
            path = str(spec.get("path") or "")
            value = _read_path(compiled_entity_set, path)
            valid = _valid_value(value, str(spec.get("validator") or "non_empty"))
            source_span = spec.get("source_span")
            if valid and isinstance(source_span, str):
                valid = bool(_source_span(compiled_entity_set, source_span))
            if valid and spec.get("provenance") == "entity_window":
                valid = _entity_window_has_provenance(plan, compiled_entity_set)
        if valid:
            evidence_rows.append(ResolutionEvidence(
                requirement_id=requirement["id"],
                aliases=tuple(requirement.get("aliases") or []),
                resolved_by=resolved_by,
                reason=requirement["reason"],
            ))
    return evidence_rows


def build_resolution_evidence(
    plan: Mapping[str, Any],
    *,
    compiled_entity_set: Mapping[str, Any] | None,
    member_table: str,
    region_columns: Iterable[str],
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
) -> list[ResolutionEvidence]:
    registry = load_registry(registry_path)
    evidence = _direct_plan_evidence(plan, registry)
    evidence.extend(_dimension_evidence(
        plan,
        registry,
        member_table=member_table,
        region_columns={str(column) for column in region_columns},
    ))
    evidence.extend(_registered_evidence(
        plan,
        registry,
        compiled_entity_set=compiled_entity_set,
    ))
    # Stable de-duplication keeps reconciliation audit output deterministic.
    unique: list[ResolutionEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        key = (item.requirement_id, item.resolved_by, item.reason)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def resolve_missing_field(
    field: str,
    evidence: Iterable[ResolutionEvidence],
    *,
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
) -> dict[str, str] | None:
    registry = load_registry(registry_path)
    field_keys = _identifier_keys(field, registry)
    for item in evidence:
        evidence_keys: set[str] = set()
        for identifier in (item.requirement_id, *item.aliases):
            evidence_keys.update(_identifier_keys(identifier, registry))
        if field_keys & evidence_keys:
            return item.to_result(field.strip())
    return None
