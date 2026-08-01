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

    def field_for(self, domain: str) -> str:
        return self.field_template.format(domain=domain)


@dataclass(frozen=True)
class EventSemanticRegistry:
    version: int
    domains: Mapping[str, tuple[str, ...]]
    dimensions: Mapping[str, ScopeDimension]

    def domain_dimensions(self, domain: str) -> tuple[str, ...] | None:
        return self.domains.get(domain)

    def relation(self, dimension: str, left: str, right: str) -> str:
        if left == right:
            return EQUAL
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
        )

    domains: dict[str, tuple[str, ...]] = {}
    for domain, raw in raw_domains.items():
        if not isinstance(domain, str) or not isinstance(raw, dict):
            raise EventSemanticRegistryError("invalid event domain")
        names = _strings(raw.get("dimensions", []), f"{domain}.dimensions")
        unknown = set(names) - set(dimensions)
        if unknown:
            raise EventSemanticRegistryError(f"domain {domain!r} references unknown dimensions: {sorted(unknown)}")
        domains[domain] = names
    return EventSemanticRegistry(
        version=payload["version"],
        domains=MappingProxyType(domains),
        dimensions=MappingProxyType(dimensions),
    )


@lru_cache(maxsize=1)
def registry() -> EventSemanticRegistry:
    return load_registry()


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
    "registry",
]
