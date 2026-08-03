"""이벤트 의미 scope registry.

파서의 표면어와 의미 검증기의 scope 관계가 서로 다른 표를 보면 같은 조건이 단계마다
다르게 해석된다. 이 모듈은 canonical 값과 ``equal/subset/disjoint/overlap_possible/unknown``
관계만 소유한다. 실제 컬럼·테이블 지원 여부는 :mod:`event_compiler`가 별도로 판단한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

EQUAL = "equal"
SUBSET = "subset"
DISJOINT = "disjoint"
OVERLAP_POSSIBLE = "overlap_possible"
UNKNOWN = "unknown"

_TYPED_PREDICATE_PREFIX = "typed-predicate:v1:"

DEFAULT_REGISTRY_PATH = Path(
    os.getenv(
        "EVENT_SEMANTIC_REGISTRY",
        "docs/data/runtime/semantics/event_semantic_registry.json",
    )
)


class EventSemanticRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ScopeValue:
    canonical: str
    aliases: tuple[str, ...]
    subset_of: frozenset[str]
    disjoint_with: frozenset[str]
    overlap_with: frozenset[str]


@dataclass(frozen=True)
class ScopeDimension:
    name: str
    field_template: str
    unrestricted_aliases: tuple[str, ...]
    qualifier_aliases: tuple[str, ...]
    values: Mapping[str, ScopeValue]
    field_alias_templates: tuple[str, ...] = ()

    def field_for(self, domain: str) -> str:
        return self.field_template.format(domain=domain)

    def matches_field(self, domain: str, field_name: str) -> bool:
        templates = (self.field_template, *self.field_alias_templates)
        return field_name in {template.format(domain=domain) for template in templates}


@dataclass(frozen=True)
class EventSemanticRegistry:
    version: int
    domains: Mapping[str, tuple[str, ...]]
    dimensions: Mapping[str, ScopeDimension]
    coverage_families: Mapping[str, str]

    def domain_dimensions(self, domain: str) -> tuple[str, ...] | None:
        return self.domains.get(domain)

    def coverage_family(self, domain: str) -> str:
        """Return the event family used only for source-coverage checks.

        A line-grain source can prove that its parent event family was
        represented without making the two relations interchangeable for
        joins, aggregation, or scope comparison.  Keeping that weaker
        relationship in the registry prevents graph-level drop detection from
        hard-coding physical source aliases.
        """

        return self.coverage_families.get(domain, domain)

    def dimension_for_field(self, domain: str, field_name: str) -> str | None:
        for dimension_name in self.domains.get(domain, ()):
            dimension = self.dimensions[dimension_name]
            if dimension.matches_field(domain, field_name):
                return dimension_name
        return None

    def relation(self, dimension: str, left: str, right: str) -> str:
        if left == right:
            return EQUAL
        left_predicate = parse_typed_predicate_value(left)
        right_predicate = parse_typed_predicate_value(right)
        if left_predicate is not None or right_predicate is not None:
            if (
                left_predicate is not None
                and right_predicate is not None
                and left_predicate[0] == right_predicate[0]
                and left_predicate[1] != right_predicate[1]
            ):
                return DISJOINT
            # Two arbitrary open-text predicates are not disjoint merely
            # because their literal strings differ: LIKE scopes can overlap.
            return UNKNOWN
        spec = self.dimensions.get(dimension)
        if spec is None:
            return UNKNOWN
        left_value = spec.values.get(left)
        right_value = spec.values.get(right)
        if left_value is None or right_value is None:
            return UNKNOWN
        if right in left_value.subset_of:
            return SUBSET
        if right in left_value.disjoint_with or left in right_value.disjoint_with:
            return DISJOINT
        if right in left_value.overlap_with or left in right_value.overlap_with:
            return OVERLAP_POSSIBLE
        return UNKNOWN


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise EventSemanticRegistryError(f"{label} must be a string list")
    return tuple(value)


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> EventSemanticRegistry:
    registry_path = Path(path)
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventSemanticRegistryError(f"cannot load event semantic registry: {registry_path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), int):
        raise EventSemanticRegistryError("event semantic registry needs an integer version")
    raw_dimensions = payload.get("dimensions")
    raw_domains = payload.get("domains")
    if not isinstance(raw_dimensions, dict) or not isinstance(raw_domains, dict):
        raise EventSemanticRegistryError("event semantic registry needs domains and dimensions")

    dimensions: dict[str, ScopeDimension] = {}
    for name, raw in raw_dimensions.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise EventSemanticRegistryError("invalid scope dimension")
        field_template = raw.get("field_template")
        if not isinstance(field_template, str) or "{domain}" not in field_template:
            raise EventSemanticRegistryError(f"dimension {name!r} needs a domain field template")
        raw_values = raw.get("values")
        if not isinstance(raw_values, dict):
            raise EventSemanticRegistryError(f"dimension {name!r} needs values")
        values: dict[str, ScopeValue] = {}
        for canonical, value in raw_values.items():
            if not isinstance(canonical, str) or not isinstance(value, dict):
                raise EventSemanticRegistryError(f"invalid value in dimension {name!r}")
            values[canonical] = ScopeValue(
                canonical=canonical,
                aliases=_strings(value.get("aliases", []), f"{name}.{canonical}.aliases"),
                subset_of=frozenset(_strings(value.get("subset_of", []), f"{name}.{canonical}.subset_of")),
                disjoint_with=frozenset(
                    _strings(value.get("disjoint_with", []), f"{name}.{canonical}.disjoint_with")
                ),
                overlap_with=frozenset(
                    _strings(value.get("overlap_with", []), f"{name}.{canonical}.overlap_with")
                ),
            )
        known = set(values)
        for value in values.values():
            referenced = set(value.subset_of) | set(value.disjoint_with) | set(value.overlap_with)
            unknown = referenced - known
            if unknown:
                raise EventSemanticRegistryError(
                    f"dimension {name!r} references unknown values: {sorted(unknown)}"
                )
        dimensions[name] = ScopeDimension(
            name=name,
            field_template=field_template,
            unrestricted_aliases=_strings(
                raw.get("unrestricted_aliases", []), f"{name}.unrestricted_aliases"
            ),
            qualifier_aliases=_strings(
                raw.get("qualifier_aliases", []), f"{name}.qualifier_aliases"
            ),
            values=MappingProxyType(values),
            field_alias_templates=_strings(
                raw.get("field_alias_templates", []), f"{name}.field_alias_templates"
            ),
        )

    domains: dict[str, tuple[str, ...]] = {}
    coverage_families: dict[str, str] = {}
    for domain, raw in raw_domains.items():
        if not isinstance(domain, str) or not isinstance(raw, dict):
            raise EventSemanticRegistryError("invalid event domain")
        names = _strings(raw.get("dimensions", []), f"{domain}.dimensions")
        unknown = set(names) - set(dimensions)
        if unknown:
            raise EventSemanticRegistryError(f"domain {domain!r} references unknown dimensions: {sorted(unknown)}")
        domains[domain] = names
        coverage_family = raw.get("coverage_family", domain)
        if not isinstance(coverage_family, str) or not coverage_family:
            raise EventSemanticRegistryError(
                f"{domain}.coverage_family must be a non-empty string"
            )
        coverage_families[domain] = coverage_family
    unknown_families = set(coverage_families.values()) - set(domains)
    if unknown_families:
        raise EventSemanticRegistryError(
            f"coverage families reference unknown domains: {sorted(unknown_families)}"
        )
    return EventSemanticRegistry(
        version=payload["version"],
        domains=MappingProxyType(domains),
        dimensions=MappingProxyType(dimensions),
        coverage_families=MappingProxyType(coverage_families),
    )


@lru_cache(maxsize=1)
def registry() -> EventSemanticRegistry:
    return load_registry()


def typed_predicate_value(fingerprint: str, *, complemented: bool = False) -> str:
    """Encode a typed Event IR predicate without treating its literal as an enum value."""

    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("fingerprint must be a non-empty string")
    polarity = "complement" if complemented else "match"
    return f"{_TYPED_PREDICATE_PREFIX}{polarity}:{fingerprint}"


def parse_typed_predicate_value(value: str) -> tuple[str, bool] | None:
    if not isinstance(value, str) or not value.startswith(_TYPED_PREDICATE_PREFIX):
        return None
    payload = value[len(_TYPED_PREDICATE_PREFIX):]
    polarity, separator, fingerprint = payload.partition(":")
    if not separator or polarity not in {"match", "complement"} or not fingerprint:
        return None
    return fingerprint, polarity == "complement"


__all__ = [
    "DISJOINT",
    "EQUAL",
    "OVERLAP_POSSIBLE",
    "SUBSET",
    "UNKNOWN",
    "EventSemanticRegistry",
    "EventSemanticRegistryError",
    "ScopeDimension",
    "ScopeValue",
    "load_registry",
    "parse_typed_predicate_value",
    "registry",
    "typed_predicate_value",
]
