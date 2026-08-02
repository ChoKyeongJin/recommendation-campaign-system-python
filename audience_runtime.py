"""Composition root for the canonical audience Event IR.

The JSON catalog is the only place where business sources, fields, metrics and
their physical bindings are declared.  This module merely loads that data,
builds the typed :class:`ResolvedSemanticCatalog`, and derives the LLM schema
and prompt glossary from the same snapshot.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from typing import Any, Mapping

import event_compiler
import event_ir
import resolved_semantic_catalog


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_AUDIENCE_CATALOG_PATH = (
    REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "audience_catalog.json"
)


class AudienceCatalogLoadError(ValueError):
    """The canonical audience catalog could not be loaded or resolved."""


@functools.lru_cache(maxsize=4)
def load_audience_catalog_config(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> dict[str, Any]:
    resolved_path = Path(path).resolve()
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudienceCatalogLoadError(
            f"audience catalog load failed: {resolved_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AudienceCatalogLoadError("audience catalog root must be an object")
    return payload


@functools.lru_cache(maxsize=4)
def resolve_audience_catalog(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> resolved_semantic_catalog.ResolvedSemanticCatalog:
    raw = load_audience_catalog_config(path)
    subject_raw = raw.get("subject") if isinstance(raw.get("subject"), Mapping) else {}
    table = subject_raw.get("table")
    key = subject_raw.get("key")
    if not isinstance(table, str) or not table or not isinstance(key, str) or not key:
        raise AudienceCatalogLoadError(
            "audience catalog subject.table and subject.key must be non-empty strings"
        )
    subject = event_compiler.SubjectSpec(
        table=table,
        alias=str(subject_raw.get("alias") or "B"),
        key=key,
        name=str(subject_raw.get("name") or "subject"),
    )
    try:
        return resolved_semantic_catalog.resolve_semantic_catalog(
            runtime_config=raw,
            subject=subject,
        )
    except resolved_semantic_catalog.CatalogError as exc:
        raise AudienceCatalogLoadError(str(exc)) from exc


def audience_expression_json_schema(
    *,
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
    depth: int = 1,
) -> dict[str, Any]:
    """Derive the fixed algebra shape and the open catalog symbols together."""
    catalog = resolve_audience_catalog(path)
    return event_ir.condition_json_schema(
        depth=depth,
        source_names=tuple(catalog.compiler_events),
        field_names=tuple(catalog.compiler_fields),
    )


def audience_catalog_guidance(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> str:
    """Human-readable, declaration-derived glossary for the structuring model."""
    raw = load_audience_catalog_config(path)
    lines = [
        "[Canonical Audience IR]",
        "타겟 조건은 audience_requirement.expression 하나에 Event IR로 작성한다. target_user/exclude/SQL은 만들지 않는다.",
        "부재·제외는 Not, 존재는 Exists, 임계는 Comparison, 횟수·금액은 Aggregate를 조합한다.",
        "각 최상위 Boolean 원자에 원문 그대로의 evidence(text/start/end)를 붙인다.",
        "catalog source에 고정 필터가 선언되어 있으므로 그 SQL 조건을 다시 만들지 않는다.",
        "'최근'처럼 시간 한정이 있으나 기간 값이 없으면 전체 이력으로 간주하지 말고 expression=null과 missing_argument(period) issue를 낸다.",
        "",
        "[Sources]",
    ]
    for source_id, declaration in sorted((raw.get("sources") or {}).items()):
        if not isinstance(declaration, Mapping):
            continue
        label = str(declaration.get("label") or source_id)
        aliases = ", ".join(str(item) for item in declaration.get("aliases") or [])
        lines.append(f"- {source_id}: {label}" + (f" ({aliases})" if aliases else ""))
    lines.extend(["", "[Fields]"])
    for field_id, declaration in sorted((raw.get("fields") or {}).items()):
        if not isinstance(declaration, Mapping):
            continue
        label = str(declaration.get("label") or field_id)
        unit = str(declaration.get("unit") or "")
        lines.append(f"- {field_id}: {label}" + (f" [unit={unit}]" if unit else ""))
    lines.extend(["", "[Metric recipes]"])
    for metric_id, declaration in sorted((raw.get("metrics") or {}).items()):
        if not isinstance(declaration, Mapping):
            continue
        label = str(declaration.get("label") or metric_id)
        kind = str(declaration.get("kind") or declaration.get("semantic_type") or "")
        function = str(declaration.get("function") or declaration.get("aggregate_function") or "")
        source = str(declaration.get("source") or "")
        expression = str(declaration.get("expression") or declaration.get("expression_field") or "*")
        distinct = " distinct" if declaration.get("distinct") else ""
        if kind == "aggregate" or function:
            recipe = (
                f"Aggregate(function={function}, source={source}, "
                f"expression={expression},{distinct or ' all'})"
            )
        elif kind == "existence":
            recipe = f"Exists(Source({source}))"
        else:
            recipe = f"Comparison(Field({expression}))"
        lines.append(f"- {metric_id} ({label}): {recipe}")
    return "\n".join(lines)


def catalog_snapshot(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> dict[str, Any]:
    """Defensive copy for diagnostics/tests; callers cannot mutate the cache."""
    return copy.deepcopy(load_audience_catalog_config(path))


__all__ = [
    "AudienceCatalogLoadError",
    "DEFAULT_AUDIENCE_CATALOG_PATH",
    "audience_catalog_guidance",
    "audience_expression_json_schema",
    "catalog_snapshot",
    "load_audience_catalog_config",
    "resolve_audience_catalog",
]
