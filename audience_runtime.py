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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import audience_schema
import event_compiler
import event_ir
import member_filters_config
import member_policy
import member_scalar_metrics
import metric_recipe_selection
import resolved_semantic_catalog


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_AUDIENCE_CATALOG_PATH = (
    REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "audience_catalog.json"
)
DEFAULT_EXTERNAL_REGION_MAPPING_PATH = (
    REPO_ROOT / "docs" / "data" / "runtime" / "external" / "external_region_mapping.json"
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


def materialize_value_domains(raw: Mapping[str, Any]) -> dict[str, Any]:
    """``source_category`` 로 선언된 값 도메인을 eq_filters 에서 **런타임 조인**한다.

    값 사전(canonical·물리코드·서열·동의어)의 단일 소유자는 ``member_target_filters.json`` 이다
    (`attribute_catalog.json` 의 이중 소유 금지 조항과 같은 계약). 카탈로그는 "이 필드는 grade
    범주의 값을 쓰고 순서가 있다"만 말하고, 값과 rank 는 여기서 붙인다 — 카탈로그에 값을 다시
    적으면 두 파일이 같은 사실을 말하게 되고 곧 어긋난다.
    """
    domains = raw.get("value_domains")
    if not isinstance(domains, Mapping):
        return {}
    materialized: dict[str, Any] = {}
    for name, declaration in domains.items():
        if not isinstance(declaration, Mapping):
            continue
        category = declaration.get("source_category")
        region_group = declaration.get("source_external_region_mapping")
        if category and region_group:
            raise AudienceCatalogLoadError(
                f"value domain {name!r} cannot declare two value sources"
            )
        if region_group:
            try:
                region_payload = json.loads(
                    DEFAULT_EXTERNAL_REGION_MAPPING_PATH.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise AudienceCatalogLoadError(
                    f"external region mapping load failed: {exc}"
                ) from exc
            entries = region_payload.get(str(region_group))
            if not isinstance(entries, list) or not entries:
                raise AudienceCatalogLoadError(
                    f"value domain {name!r} references empty external region group "
                    f"{region_group!r}"
                )
            values: dict[str, dict[str, Any]] = {}
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                code = str(entry.get("external_code") or "").strip()
                physical = str(entry.get("crm_value") or "").strip()
                external_name = str(entry.get("external_name") or "").strip()
                if not code or not physical:
                    continue
                aliases = [
                    external_name,
                    *(
                        [str(item) for item in entry.get("aliases")]
                        if isinstance(entry.get("aliases"), list)
                        else []
                    ),
                    physical,
                ]
                values[f"sido_{code}"] = {
                    "physical": physical,
                    "aliases": list(dict.fromkeys(item for item in aliases if item)),
                }
            if not values:
                raise AudienceCatalogLoadError(
                    f"value domain {name!r} resolved no usable external region values"
                )
            resolved = dict(declaration)
            resolved["values"] = values
            materialized[name] = resolved
            continue
        if not category:
            materialized[name] = declaration
            continue
        entries = member_filters_config.eq_filter_values(str(category))
        if not entries:
            raise AudienceCatalogLoadError(
                f"value domain {name!r} references empty eq_filters category {category!r}"
            )
        resolved = dict(declaration)
        resolved["values"] = {
            canonical: {"physical": entry.get("value"), "aliases": entry.get("synonyms") or []}
            for canonical, entry in entries.items()
        }
        if declaration.get("ordered"):
            ranked = [
                (entry.get("rank"), canonical)
                for canonical, entry in entries.items()
                if isinstance(entry.get("rank"), int)
            ]
            if len(ranked) != len(entries):
                raise AudienceCatalogLoadError(
                    f"value domain {name!r} is declared ordered but eq_filters category "
                    f"{category!r} does not give every value a rank"
                )
            resolved["order"] = [canonical for _rank, canonical in sorted(ranked)]
        materialized[name] = resolved
    return materialized


def _member_metric_registry_path(
    raw: Mapping[str, Any], catalog_path: str | Path
) -> Path | None:
    imports = raw.get("imports")
    declaration = (
        imports.get("member_metric_rankings")
        if isinstance(imports, Mapping)
        else None
    )
    if declaration is None:
        return None
    if isinstance(declaration, Mapping):
        declared_path = declaration.get("path")
        relative_to = declaration.get("relative_to")
        if (
            not isinstance(declared_path, str)
            or not declared_path.strip()
            or relative_to not in {"repository", "catalog"}
        ):
            raise AudienceCatalogLoadError(
                "imports.member_metric_rankings needs path and "
                "relative_to='repository'|'catalog'"
            )
        base = REPO_ROOT if relative_to == "repository" else Path(catalog_path).resolve().parent
        return (base / declared_path).resolve()
    if not isinstance(declaration, str) or not declaration.strip():
        raise AudienceCatalogLoadError(
            "imports.member_metric_rankings must be a path or import object"
        )
    return (Path(catalog_path).resolve().parent / declaration).resolve()


def materialize_member_metric_rankings(
    raw: Mapping[str, Any], *, catalog_path: str | Path
) -> dict[str, Any]:
    """Materialize generic ranking assets from the member metric registry.

    The metric file owns metric vocabulary and physical columns.  The audience
    catalog only imports that registry; adding another metric therefore does
    not require a Python branch or a second catalog declaration.
    """
    registry_path = _member_metric_registry_path(raw, catalog_path)
    if registry_path is None:
        return copy.deepcopy(dict(raw))
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudienceCatalogLoadError(
            f"member metric registry load failed: {registry_path}: {exc}"
        ) from exc
    if not isinstance(registry, Mapping):
        raise AudienceCatalogLoadError("member metric registry root must be an object")
    canonical = registry.get("canonical_event_ir")
    metrics = registry.get("metrics")
    if not isinstance(canonical, Mapping) or not isinstance(metrics, list):
        raise AudienceCatalogLoadError(
            "member metric registry needs canonical_event_ir and metrics"
        )

    result = copy.deepcopy(dict(raw))
    # The returned catalog is a self-contained resolved snapshot.  Keeping the
    # relative import after materialization makes a copied snapshot try to load
    # the registry relative to its new location and duplicates generated IDs.
    resolved_imports = result.get("imports")
    if isinstance(resolved_imports, dict):
        resolved_imports.pop("member_metric_rankings", None)
        if not resolved_imports:
            result.pop("imports", None)
    sources = result.setdefault("sources", {})
    fields = result.setdefault("fields", {})
    catalog_metrics = result.setdefault("metrics", {})
    recipes = result.setdefault("relation_recipes", {})
    if not all(isinstance(item, dict) for item in (sources, fields, catalog_metrics, recipes)):
        raise AudienceCatalogLoadError(
            "audience catalog sources/fields/metrics/relation_recipes must be objects"
        )

    subject = result.get("subject")
    if not isinstance(subject, Mapping):
        raise AudienceCatalogLoadError("audience catalog subject must be an object")
    subject_table = str(subject.get("table") or "")
    subject_key = str(subject.get("key") or "")
    value_table = str(registry.get("value_table") or "")
    join_column = str(registry.get("join_column") or "")
    grain_filter = str(registry.get("grain_filter") or "")
    source_alias = str(canonical.get("source_alias") or "")
    source_prefix = str(canonical.get("source_prefix") or "")
    time_column = str(canonical.get("time_column") or "")
    time_format = str(canonical.get("time_format") or "char6")
    if not all(
        (subject_table, subject_key, value_table, join_column, grain_filter,
         source_alias, source_prefix, time_column)
    ):
        raise AudienceCatalogLoadError(
            "member metric canonical binding contains an empty required value"
        )
    templated_grain = grain_filter.replace(f"{source_alias}.", "{alias}.")
    active_predicate = member_policy.active_member_predicate("{alias}_MEMBER")
    ranking_entities = canonical.get("entities")
    if not isinstance(ranking_entities, Mapping) or len(ranking_entities) != 1:
        raise AudienceCatalogLoadError(
            "member metric canonical_event_ir.entities must declare exactly one entity"
        )
    ranking_entity = str(next(iter(ranking_entities)))
    ranking_limit_units = copy.deepcopy(canonical.get("limit_units") or [])
    policy = canonical.get("policy")
    if not isinstance(policy, Mapping):
        raise AudienceCatalogLoadError("member metric ranking policy must be an object")
    if (
        not isinstance(ranking_limit_units, list)
        or not ranking_limit_units
        or any(not isinstance(unit, str) for unit in ranking_limit_units)
        or len(set(ranking_limit_units)) != len(ranking_limit_units)
        or not set(ranking_limit_units).issubset({"count", "percent"})
    ):
        raise AudienceCatalogLoadError(
            "member metric ranking limit_units must be a non-empty unique subset "
            "of count and percent"
        )
    supported_policy = {
        "population": "active_members",
        "tie_policy": "exact_count",
        "null_policy": "exclude",
        "missing_policy": "exclude",
        "small_population_policy": "ceil",
    }
    if dict(policy) != supported_policy:
        raise AudienceCatalogLoadError(
            "member metric ranking policy is not supported by the generated "
            f"physical relation: expected {supported_policy!r}"
        )

    recipe_id = str(canonical.get("relation_recipe") or "member_metric_ranking")
    if recipe_id in recipes:
        raise AudienceCatalogLoadError(
            f"generated relation recipe collides with {recipe_id!r}"
        )
    recipes[recipe_id] = {
        "label": str(canonical.get("label") or "회원 지표 전역 순위"),
        "directions": copy.deepcopy(canonical.get("directions") or {}),
        "entities": copy.deepcopy(canonical.get("entities") or {}),
        "metrics": {},
        "limit_units": ranking_limit_units,
        "policy": copy.deepcopy(policy),
    }

    for declaration in metrics:
        if not isinstance(declaration, Mapping):
            raise AudienceCatalogLoadError("member metric entries must be objects")
        metric_id = str(declaration.get("metric_id") or "")
        column = str(declaration.get("column") or "")
        function = str(declaration.get("agg") or "").lower()
        label = str(declaration.get("ko_label") or metric_id)
        aliases = [str(item) for item in declaration.get("synonyms") or []]
        if not metric_id or not column or function not in {"sum", "avg", "min", "max", "count"}:
            raise AudienceCatalogLoadError(
                f"invalid member metric declaration: {metric_id or '<missing id>'}"
            )
        source_id = f"{source_prefix}{metric_id}"
        member_field = f"{source_id}.member_id"
        value_field = f"{source_id}.value"
        collisions = [
            identifier
            for section, identifier in (
                (sources, source_id),
                (fields, member_field),
                (fields, value_field),
                (catalog_metrics, metric_id),
            )
            if identifier in section
        ]
        if collisions:
            raise AudienceCatalogLoadError(
                "generated member metric symbols collide: " + ", ".join(collisions)
            )
        sources[source_id] = {
            "table": value_table,
            "alias": source_alias,
            "from_sql": (
                f"{value_table} {{alias}} INNER JOIN {subject_table} {{alias}}_MEMBER "
                f"ON {{alias}}_MEMBER.{subject_key} = {{alias}}.{join_column}"
            ),
            "subject_key": subject_key,
            "event_subject_key": join_column,
            "time_column": time_column,
            "time_format": time_format,
            "binding": "fact_table",
            "grain": "subject",
            "extra_predicates": [
                templated_grain,
                active_predicate,
                f"{{alias}}.{column} IS NOT NULL",
            ],
            "label": label,
            "aliases": aliases,
        }
        fields[member_field] = {
            "source": source_id,
            "column": join_column,
            "data_type": "number",
            "nullable": False,
            "label": "회원 식별자",
        }
        fields[value_field] = {
            "source": source_id,
            "column": column,
            "data_type": "number",
            "nullable": False,
            "label": label,
            "aliases": aliases,
        }
        catalog_metrics[metric_id] = {
            "source": source_id,
            "kind": "aggregate",
            "function": function,
            "expression_field": value_field,
            "data_type": "number",
            "grain": "subject",
            "relation_recipe": recipe_id,
            "ranking_entity": ranking_entity,
            "ranking_entity_field": member_field,
            "ranking_limit_units": ranking_limit_units,
            "ranking_tie_policy": policy.get("tie_policy"),
            "label": label,
            "aliases": aliases,
        }
        recipes[recipe_id]["metrics"][metric_id] = {
            "terms": aliases,
            "source": source_id,
            "entity_field": member_field,
            "measure_function": function,
            "measure_field": value_field,
        }

    _materialize_member_scalar_metrics(
        registry,
        sources=sources,
        fields=fields,
        catalog_metrics=catalog_metrics,
        value_table=value_table,
        join_column=join_column,
        templated_grain=templated_grain,
    )
    return result


def _materialize_member_scalar_metrics(
    registry: Mapping[str, Any],
    *,
    sources: dict[str, Any],
    fields: dict[str, Any],
    catalog_metrics: dict[str, Any],
    value_table: str,
    join_column: str,
    templated_grain: str,
) -> None:
    """같은 지표 목록에서 **회원별 스칼라** 계약을 파생한다(선언 없으면 아무것도 하지 않는다).

    순위 계약(:func:`materialize_member_metric_rankings` 본문)과 같은 지표 줄을 읽지만 만드는
    것이 다르다: 모집단이 아니라 회원 한 명의 값이므로 active_members 조인도, 값 NOT NULL
    술어도 붙이지 않는다. 최신 월 스냅샷 고정(``grain_filter``)만 공유한다.

    지표 id 와 소스 id 가 같은 이름(``member_scalar_<metric_id>``)인 것은 의도다 — 그 소스에서
    자동 파생되는 존재(EXISTS) 지표를 이 명시 선언이 덮는다. '스냅샷 행이 있는가'는 회원별
    스칼라 계약이 말하려는 것이 아니고, 같은 심볼이 두 뜻을 갖는 편이 더 위험하다.
    """

    declaration = registry.get("canonical_member_scalar")
    if declaration is None:
        return
    if not isinstance(declaration, Mapping):
        raise AudienceCatalogLoadError("canonical_member_scalar must be an object")
    metrics = registry.get("metrics")
    if not isinstance(metrics, list):
        raise AudienceCatalogLoadError("member metric registry needs metrics")

    source_prefix = str(declaration.get("source_prefix") or "")
    source_alias = str(declaration.get("source_alias") or "")
    time_column = str(declaration.get("time_column") or "")
    time_format = str(declaration.get("time_format") or "char6")
    value_type = str(declaration.get("value_type") or "")
    if not all((source_prefix, source_alias, time_column, value_type)):
        raise AudienceCatalogLoadError(
            "canonical_member_scalar needs source_prefix, source_alias, time_column and value_type"
        )
    allowed_operators = copy.deepcopy(declaration.get("allowed_operators") or [])
    required_capabilities = copy.deepcopy(declaration.get("required_capabilities") or [])
    if not (
        isinstance(allowed_operators, list)
        and allowed_operators
        and all(isinstance(item, str) for item in allowed_operators)
        and isinstance(required_capabilities, list)
        and all(isinstance(item, str) for item in required_capabilities)
    ):
        raise AudienceCatalogLoadError(
            "canonical_member_scalar allowed_operators/required_capabilities must be string lists"
        )
    null_behavior = str(declaration.get("null_behavior") or "exclude")
    zero_row_behavior = str(declaration.get("zero_row_behavior") or "exclude")
    threshold_units = declaration.get("threshold_units")
    if not (
        isinstance(threshold_units, list)
        and threshold_units
        and all(isinstance(item, str) and item for item in threshold_units)
    ):
        raise AudienceCatalogLoadError(
            "canonical_member_scalar needs a non-empty threshold_units vocabulary"
        )
    allowed_units = set(threshold_units)

    for entry in metrics:
        if not isinstance(entry, Mapping):
            raise AudienceCatalogLoadError("member metric entries must be objects")
        metric_id = str(entry.get("metric_id") or "")
        column = str(entry.get("column") or "")
        label = str(entry.get("ko_label") or metric_id)
        # 단위는 선언에서만 온다. 선언이 없는 지표는 **회원별 스칼라 계약을 갖지 않는다**
        # (순위 계약은 그대로다) — 없는 선언을 오류로 만들면 순위만 쓰는 지표를 등록할 때
        # 쓰지도 않을 단위를 지어내야 하고, 지어낸 단위는 곧 틀린 임계 비교가 된다.
        threshold_unit = entry.get("threshold_unit")
        if not metric_id or not column:
            raise AudienceCatalogLoadError(
                f"invalid member metric declaration: {metric_id or '<missing id>'}"
            )
        if not (isinstance(threshold_unit, str) and threshold_unit in allowed_units):
            continue
        source_id = f"{source_prefix}{metric_id}"
        value_field = f"{source_id}.value"
        collisions = [
            identifier
            for section, identifier in (
                (sources, source_id),
                (fields, value_field),
                (catalog_metrics, source_id),
            )
            if identifier in section
        ]
        if collisions:
            raise AudienceCatalogLoadError(
                "generated member scalar symbols collide: " + ", ".join(collisions)
            )
        sources[source_id] = {
            "table": value_table,
            "alias": source_alias,
            "subject_key": join_column,
            "event_subject_key": join_column,
            "time_column": time_column,
            "time_format": time_format,
            "binding": "fact_table",
            "grain": "subject",
            "extra_predicates": [templated_grain],
            "label": label,
        }
        fields[value_field] = {
            "source": source_id,
            "column": column,
            "data_type": value_type,
            "nullable": True,
            "unit": threshold_unit,
            "label": label,
        }
        catalog_metrics[source_id] = {
            "source": source_id,
            "kind": "member_scalar",
            "cardinality": "scalar",
            "grain": "subject",
            "expression_field": value_field,
            "value_type": value_type,
            "unit": threshold_unit,
            "allowed_operators": list(allowed_operators),
            "null_behavior": null_behavior,
            "zero_row_behavior": zero_row_behavior,
            "required_capabilities": list(required_capabilities),
            "label": label,
        }


@functools.lru_cache(maxsize=4)
def member_metric_registry_snapshot(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> dict[str, Any] | None:
    """카탈로그가 import 하는 회원 지표 레지스트리 원본(없으면 ``None``).

    회원별 스칼라 임계의 표면어·단위 선언(``synonyms``/``threshold_unit``)은 이 파일이 소유한다.
    소비자가 경로를 다시 계산하지 않도록 여기서 한 번만 읽는다 — 두 번째 독자가 생기는 순간
    "어느 파일이 지표 어휘의 주인인가"의 답이 호출 지점마다 달라진다.
    """

    raw = load_audience_catalog_config(path)
    registry_path = _member_metric_registry_path(raw, path)
    if registry_path is None:
        return None
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudienceCatalogLoadError(
            f"member metric registry load failed: {registry_path}: {exc}"
        ) from exc
    if not isinstance(registry, dict):
        raise AudienceCatalogLoadError("member metric registry root must be an object")
    return copy.deepcopy(registry)


@functools.lru_cache(maxsize=4)
def resolve_audience_catalog(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> resolved_semantic_catalog.ResolvedSemanticCatalog:
    raw = materialize_member_metric_rankings(
        load_audience_catalog_config(path), catalog_path=path
    )
    materialized = materialize_value_domains(raw)
    if materialized:
        raw["value_domains"] = materialized
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
    """Return the fixed algebra schema after validating the selected catalog.

    ``depth`` remains for API compatibility, but recursive ``$ref`` definitions
    no longer expand once per depth.  Catalog membership is intentionally not a
    JSON Schema enum: the canonical runtime validator checks resolved source and
    field symbols before producing executable IR.
    """
    if depth < 1:
        raise ValueError("audience expression schema depth must be at least 1")
    resolve_audience_catalog(path)
    return audience_schema.audience_expression_json_schema()


# ``[Metric recipes]`` 자리표시자. 연산자·임계값·canonical 값은 요청마다 달라지므로 안내에는
# 토큰만 남기고 모델이 원문에서 채운다. 토큰을 **모듈 상수로** 노출하는 이유는 recipe 가 실제로
# 컴파일되는지 재는 테스트가 같은 문자열을 손으로 다시 적지 않게 하기 위해서다 — 안내와 검증이
# 갈라지면 "안내는 고쳤는데 컴파일은 여전히 불가" 상태가 조용히 남는다.
METRIC_RECIPE_EVIDENCE_TEXT = "<exact source phrase>"
METRIC_RECIPE_OPERATOR_PLACEHOLDER = "<operator>"
METRIC_RECIPE_THRESHOLD_PLACEHOLDER = "<threshold number>"
METRIC_RECIPE_VALUE_PLACEHOLDER = "<canonical value>"
METRIC_RECIPE_PREVIOUS_VALUE_PLACEHOLDER = "<canonical value before transition>"
# 선언이 불완전해 컴파일 가능한 모양을 만들 수 없을 때 그 자리에 남기는 문구. 빈 줄로 지우거나
# 아무 모양이나 채우지 않는다(규칙 11) — 모델에게 "이 지표는 쓰지 말라"고 말해야 한다.
METRIC_RECIPE_UNAVAILABLE = (
    "recipe 없음 — 선언이 불완전해 컴파일 가능한 모양이 없다. 이 지표를 사용하지 않는다."
)


def _metric_recipe_evidence() -> dict[str, Any]:
    """자리표시자 evidence. 호출마다 새 dict 를 만든다(전역 mutable 공유 금지)."""
    return {"text": METRIC_RECIPE_EVIDENCE_TEXT, "start": 0, "end": 1}


def _metric_comparison_wire(field: str, operator: str, value: Any) -> dict[str, Any]:
    return {
        "type": "comparison",
        "operator": operator,
        "left": {"type": "field", "name": field},
        "right": {"type": "literal", "value": value},
        "evidence": _metric_recipe_evidence(),
    }


def _member_row_exists_wire(source: str, where: Mapping[str, Any]) -> dict[str, Any]:
    """회원당 0..1 행을 읽는 지표의 유일한 컴파일 가능 골격."""
    return {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": source},
            "where": dict(where),
        },
        "evidence": _metric_recipe_evidence(),
    }


def _recipe_function(declaration: Mapping[str, Any]) -> str:
    return str(declaration.get("function") or declaration.get("aggregate_function") or "")


def _recipe_symbols(declaration: Mapping[str, Any]) -> tuple[str, str]:
    """(소스 심볼, 값 표현). 값 표현이 선언되지 않았으면 ``"*"`` — 관계 전체를 뜻한다."""
    return (
        str(declaration.get("source") or ""),
        str(declaration.get("expression") or declaration.get("expression_field") or "*"),
    )


def _member_row_symbols(declaration: Mapping[str, Any]) -> tuple[str, str] | None:
    """회원 행 recipe 가 요구하는 (소스, 값 필드). 둘 중 하나라도 없으면 ``None``.

    이 요구를 kind 별 builder 안에 두는 이유는 **kind 마다 요구가 다르기 때문**이다. aggregate
    ``count`` 는 값 표현 없이도(``expression=None``) 성립하고 existence 는 값 필드를 선언하는
    것이 오히려 금지다(:mod:`resolved_semantic_catalog`). 공통 전제로 끌어올리면 그 둘이 조용히
    'recipe 없음'이 된다.
    """
    source, expression = _recipe_symbols(declaration)
    if not source or expression == "*":
        return None
    return source, expression


def _aggregate_recipe_wire(declaration: Mapping[str, Any]) -> dict[str, Any] | None:
    """모집단 집계 — Condition 이 아니라 **Scalar** 다(비교로 감싸야 조건이 된다)."""
    source, expression = _recipe_symbols(declaration)
    return {
        "type": "aggregate",
        "function": _recipe_function(declaration),
        "relation": {"type": "source", "name": source},
        "expression": (
            {"type": "field", "name": expression} if expression != "*" else None
        ),
        "distinct": bool(declaration.get("distinct")),
    }


def _existence_recipe_wire(declaration: Mapping[str, Any]) -> dict[str, Any] | None:
    """행이 하나라도 있는가 — 값 비교가 없으므로 Filter 도 없다."""
    source, _expression = _recipe_symbols(declaration)
    return {
        "type": "exists",
        "relation": {"type": "source", "name": source},
        "evidence": _metric_recipe_evidence(),
    }


def _member_scalar_recipe_wire(declaration: Mapping[str, Any]) -> dict[str, Any] | None:
    """회원 한 명의 값 ↔ 숫자 임계값 비교."""
    symbols = _member_row_symbols(declaration)
    if symbols is None:
        return None
    source, expression = symbols
    return _member_row_exists_wire(
        source,
        _metric_comparison_wire(
            expression,
            METRIC_RECIPE_OPERATOR_PLACEHOLDER,
            METRIC_RECIPE_THRESHOLD_PLACEHOLDER,
        ),
    )


def _field_value_recipe_wire(declaration: Mapping[str, Any]) -> dict[str, Any] | None:
    """기준월 스냅샷의 값 하나 ↔ canonical 값 일치."""
    symbols = _member_row_symbols(declaration)
    if symbols is None:
        return None
    source, expression = symbols
    return _member_row_exists_wire(
        source,
        _metric_comparison_wire(expression, "=", METRIC_RECIPE_VALUE_PLACEHOLDER),
    )


def _transition_recipe_wire(declaration: Mapping[str, Any]) -> dict[str, Any] | None:
    """도착값과 출발값을 **함께** 비교하는 한 Exists.

    ``prev_expression_field`` 가 없으면 출발값을 표현할 방법이 없다. 현재 시점 비교 하나로
    낮추지 않고 recipe 자체를 내지 않는다 — 낮추면 전이 요청이 조용히 다른 뜻이 된다(규칙 11).
    """
    symbols = _member_row_symbols(declaration)
    if symbols is None:
        return None
    source, expression = symbols
    previous_field = str(declaration.get("prev_expression_field") or "")
    if not previous_field:
        return None
    return _member_row_exists_wire(
        source,
        {
            "type": "and",
            "operands": [
                _metric_comparison_wire(expression, "=", METRIC_RECIPE_VALUE_PLACEHOLDER),
                _metric_comparison_wire(
                    previous_field, "=", METRIC_RECIPE_PREVIOUS_VALUE_PLACEHOLDER
                ),
            ],
        },
    )


@dataclass(frozen=True, slots=True)
class MetricRecipeBuilder:
    """kind 하나가 컴파일되는 모양과, 그 모양을 채우는 데 필요한 선언."""

    build: Callable[[Mapping[str, Any]], dict[str, Any] | None]
    # 자리표시자를 채우는 근거가 되는 선언 키. 안내의 세부 줄(_metric_recipe_detail_line)과
    # 경쟁 후보 선택(metric_recipe_selection 의 constraint_count)이 같은 목록을 읽는다 —
    # 두 곳이 서로 다른 목록을 들면 "안내는 채울 수 있다는데 고르지는 못한다"가 된다.
    constraint_keys: tuple[str, ...] = ()


# kind → builder. 새 kind 는 여기 한 줄이고, 분기문을 여러 파일에 흩지 않는다(규칙 9).
METRIC_RECIPE_BUILDERS: Mapping[str, MetricRecipeBuilder] = MappingProxyType({
    "aggregate": MetricRecipeBuilder(
        build=_aggregate_recipe_wire,
        constraint_keys=("function", "distinct"),
    ),
    "existence": MetricRecipeBuilder(build=_existence_recipe_wire),
    member_scalar_metrics.MEMBER_SCALAR_KIND: MetricRecipeBuilder(
        build=_member_scalar_recipe_wire,
        constraint_keys=("allowed_operators", "unit"),
    ),
    "field": MetricRecipeBuilder(
        build=_field_value_recipe_wire,
        constraint_keys=("allowed_operators",),
    ),
    "transition": MetricRecipeBuilder(
        build=_transition_recipe_wire,
        constraint_keys=("allowed_operators", "prev_expression_field"),
    ),
})


def metric_recipe_builder_kind(declaration: Mapping[str, Any]) -> str:
    """이 선언이 어느 builder 로 가는가.

    ``kind`` 를 그대로 쓰지 않는 이유는 집계 함수 선언이 **kind 추론 규칙**이기 때문이다:
    ``function``/``aggregate_function`` 이 있으면 kind 표기와 무관하게 집계 recipe 다. 이 우선
    순위는 registry 이전의 분기 순서 그대로다 — 바꾸면 같은 선언이 다른 모양을 안내받는다.
    """
    kind = str(declaration.get("kind") or declaration.get("semantic_type") or "")
    if kind == "aggregate" or _recipe_function(declaration):
        return "aggregate"
    return kind


def metric_recipe_wire(declaration: Mapping[str, Any]) -> dict[str, Any] | None:
    """지표 선언 하나를 모델에게 보여줄 wire recipe 로 조립한다.

    kind 마다 **컴파일되는 모양이 다르다.** 회원별 값(member_scalar/field/transition)은 스칼라
    하나가 아니라 회원당 0..1 행을 읽는 관계이므로 bare FieldRef 를 참조할 관계가 스코프에 없고,
    그대로 안내하면 모델이 낸 식이 전부 ``compiler_operation_unsupported`` 로 죽는다. 그래서
    ``Exists(Filter(Source, Comparison))`` 골격을 지시한다 — ``member_scalar_metrics`` 의
    lowering 산출과 같은 모양이다.

    모양 자체는 kind 별 builder(:data:`METRIC_RECIPE_BUILDERS`)가 갖는다. 지표별 차이는 전부
    **선언 데이터**에서 읽고 지표 이름으로 분기하지 않는다.

    선언이 그 kind 에 필요한 필드를 갖추지 못하면 ``None`` 을 돌려준다. 등록되지 않은 kind 도
    같다 — 가장 비슷해 보이는 모양으로 대신 채우지 않는다(규칙 11).
    """
    builder = METRIC_RECIPE_BUILDERS.get(metric_recipe_builder_kind(declaration))
    if builder is None:
        return None
    return builder.build(declaration)


def _metric_recipe_detail_line(
    declaration: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> str | None:
    """자리표시자를 채우는 데 필요한 선언 값(허용 연산자·단위·값 도메인)을 한 줄로 붙인다.

    aggregate/existence 는 자리표시자가 없으므로 줄을 붙이지 않는다 — 기존 안내 문자열을
    그대로 유지한다.

    kind 는 recipe 를 실제로 만든 builder 기준(:func:`metric_recipe_builder_kind`)으로 읽는다.
    선언 표기만 보면 집계 recipe 를 받은 선언에 회원 행 세부 줄이 붙어 안내가 서로 어긋난다.
    """
    kind = metric_recipe_builder_kind(declaration)
    if kind not in (member_scalar_metrics.MEMBER_SCALAR_KIND, "field", "transition"):
        return None
    details: list[str] = []
    operators = declaration.get("allowed_operators")
    if isinstance(operators, list) and operators:
        details.append(
            "allowed_operators="
            + json.dumps(
                [str(item) for item in operators],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    if kind == member_scalar_metrics.MEMBER_SCALAR_KIND:
        unit = str(declaration.get("unit") or "")
        if unit:
            details.append(f"unit={json.dumps(unit, ensure_ascii=False)}")
    else:
        field_declaration = fields.get(
            str(declaration.get("expression_field") or "")
        )
        domain = (
            field_declaration.get("value_domain")
            if isinstance(field_declaration, Mapping) else None
        )
        if domain:
            details.append(
                f"value_domain={json.dumps(str(domain), ensure_ascii=False)}"
            )
    if not details:
        return None
    return "  - " + ", ".join(details)


def metric_recipe_candidate(
    metric_id: str, declaration: Mapping[str, Any]
) -> metric_recipe_selection.RecipeCandidate:
    """지표 선언 하나 → 경쟁 후보. 점수 재료는 전부 **선언에서** 읽는다.

    ``constraint_count`` 는 그 kind 의 builder 가 요구하는 선언(:attr:`MetricRecipeBuilder.
    constraint_keys`) 중 실제로 채워진 개수다. 자리표시자를 채울 근거를 더 많이 선언한 recipe 가
    더 구체적이라는 뜻이며, kind 이름이나 지표 이름을 읽지 않는다.
    """
    kind = metric_recipe_builder_kind(declaration)
    builder = METRIC_RECIPE_BUILDERS.get(kind)
    keys = builder.constraint_keys if builder is not None else ()
    priority = declaration.get("recipe_priority")
    return metric_recipe_selection.RecipeCandidate(
        recipe_id=str(metric_id),
        kind=kind,
        # 카탈로그가 우선순위를 선언하면 계산된 근거보다 앞선다. 지금은 아무 지표도 선언하지
        # 않으므로 전부 0 이고, 선택은 아래 계산 기준이 정한다.
        priority=priority if isinstance(priority, int) and not isinstance(priority, bool) else 0,
        constraint_count=sum(1 for key in keys if declaration.get(key)),
        # recipe 를 만들지 못하는 선언은 후보이긴 하되 다른 후보가 있으면 지는 자리다 —
        # 목록에서 지우면 모델은 그 지표를 계속 지어낸다(규칙 11).
        fallback=metric_recipe_wire(declaration) is None,
        declaration=declaration,
    )


EVENT_TIME_FIELD_HEADING = "[Event time fields]"


def _pins_its_own_time_column(declaration: Mapping[str, Any], time_column: str) -> bool:
    """이 소스 선언이 **자기 시각 컬럼을 이미 한 값으로 고정**하는가.

    회원 지표 소스들은 월 스냅샷 테이블을 최신 달 한 칸에 고정한다
    (``extra_predicates`` 의 ``{alias}.YYYYMM = (SELECT MAX(YYYYMM) …)``). 그 컬럼에 기간
    창을 더 걸면 컴파일러는 두 술어를 AND 로 붙일 뿐이라 **예외도 경고도 없이** 최신 칸이
    아닌 모든 기간이 공집합이 된다(실측: ``MAX(YYYYMM)=201701`` 인 배포에서 다른 달은 0명).

    판정 재료는 선언뿐이다 — 소스 이름이나 컬럼 이름을 여기 적지 않는다. 술어가 자기
    ``time_column`` 을 별칭과 함께 지목하면 그 컬럼은 자유롭게 필터할 수 있는 축이 아니다.
    """

    if not time_column:
        return False
    predicates = declaration.get("extra_predicates")
    if not isinstance(predicates, (list, tuple)):
        return False
    needle = f"{{alias}}.{time_column}"
    return any(isinstance(item, str) and needle in item for item in predicates)


def _source_time_field_lines(sources: Mapping[str, Any]) -> list[str]:
    """소스 선언에서 **파생되는** ``<source>.occurred_at`` 목록.

    :func:`event_compiler.resolve_fields` 는 사건마다 이 시각 필드를 자동으로 파생한다
    (원본은 소스 선언의 ``time_column``/``time_format``). 그런데 안내의 ``[Fields]`` 는
    카탈로그의 ``fields`` 선언만 렌더하므로, **기간 창을 걸 수 있는 유일한 필드가 프롬프트에
    한 번도 등장하지 않았다**. 그 결과 모델은 '최근 30일 구매' 같은 요청에서 문자열로 적재된
    시각 컬럼(purchase.order_time, data_type="string")에 TimeFilter 를 걸었고 컴파일이
    "시간 컬럼이 아닌 타입에 기간 조건을 걸 수 없습니다" 로 죽었다(실측 2026-08-07).
    광고하지 않은 심볼을 모델이 알아낼 방법은 없으므로, 선언에서 파생해 그대로 보여준다.

    grain 은 :data:`event_compiler.TIME_GRAINS` **한 곳에서만** 읽는다 — 여기에 포맷별 표를
    다시 적으면 grain 지식이 두 벌이 되어 곧 어긋난다(``time_format_data_type`` 도 같은
    레지스트리로 판정한다). 월 단위 컬럼은 ``rolling_cutoff`` 가 없어 '최근 N일' 롤링 창을
    표현할 수 없으므로, 그 사실도 같은 선언에서 파생해 함께 보여준다.

    등록되지 않은 ``time_format`` 은 조용히 빠뜨리지 않는다(규칙 11). 그 소스는 심볼을
    광고하는 대신 "기간 창을 걸 수 없다"고 명시해, 모델이 다른 컬럼으로 우회하지 않게 한다.

    선언이 **자기 시각 컬럼을 고정한** 소스도 같은 취급이다
    (:func:`_pins_its_own_time_column`). 그 컬럼에 기간 창을 더 걸면 컴파일은 성공하고 술어만
    자기모순이 되어 조용히 0명이 된다 — fail-close 가 아니라 왜곡이므로, 걸 수 있다고
    광고하는 것이 가장 나쁘다.
    """
    entries: list[str] = []
    for source_id, declaration in sorted(sources.items()):
        if not isinstance(declaration, Mapping):
            continue
        time_column = str(declaration.get("time_column") or "")
        if not time_column:
            # 시각 컬럼을 선언하지 않은 소스에는 파생될 시각 필드 자체가 없다.
            continue
        if _pins_its_own_time_column(declaration, time_column):
            entries.append(
                f"- (기간 창 불가) {source_id}: 선언이 {time_column} 을(를) 한 값으로 고정한 "
                "스냅샷이다 — 기간 창을 걸면 서로 모순된 술어가 되어 결과가 조용히 비어 있다"
            )
            continue
        time_format = str(declaration.get("time_format") or "")
        grain = event_compiler.TIME_GRAINS.get(time_format)
        if grain is None:
            entries.append(
                f"- (기간 창 불가) {source_id}: 등록되지 않은 시간 저장 포맷 "
                f"{time_format!r} — 이 소스에는 TimeFilter 를 걸 수 없다"
            )
            continue
        details = [f"grain={grain.unit}"]
        if grain.rolling_cutoff is None:
            details.append(f"롤링 창 불가({grain.unit} 칸 경계 기간만 가능)")
        label = str(declaration.get("label") or source_id)
        entries.append(
            f"- {source_id}.{event_ir.TIME_FIELD_SUFFIX}: {label} 발생 시각"
            f" [{', '.join(details)}]"
        )
    if not entries:
        return []
    return [
        "",
        EVENT_TIME_FIELD_HEADING,
        "기간 창(TimeFilter)을 걸 수 있는 필드는 소스마다 아래 하나뿐이다. 소스 선언의 "
        "time_column 에서 파생되므로 [Fields] 목록에는 나오지 않는다.",
        "grain=day 소스에만 '최근 N일' 같은 롤링 창을 걸 수 있고, grain=month 소스는 달 칸 "
        "경계에 맞는 기간만 걸 수 있다.",
        "'(기간 창 불가)' 로 시작하는 줄의 소스에는 어떤 기간 창도 걸 수 없다. 다른 컬럼으로 "
        "우회하지 말고, 기간이 꼭 필요하면 그 뜻을 표현할 수 없다고 답한다.",
        *entries,
    ]


def _competing_metric_kind_lines(metrics: Mapping[str, Any]) -> list[str]:
    """같은 표면어를 두 kind 가 나눠 갖는 지표 쌍을 **카탈로그에서 파생**해 안내한다.

    둘 다 컴파일되지만 뜻이 다른 SQL 이라 어느 쪽도 지울 수 없다(회원 1인의 스냅샷 한 행 vs
    모집단 집계). 지우는 대신 "언제 어느 쪽인가"를 알려준다. 지표 이름을 코드에 적지 않으므로
    카탈로그에서 쌍이 사라지면 이 절도 함께 사라진다.

    쌍마다 붙는 기본값과 그 근거는 문구를 심지 않고 :mod:`metric_recipe_selection` 이 실제로
    계산한 기준에서 만든다 — 기준이 바뀌면 안내도 함께 바뀐다.
    """
    by_label: dict[str, dict[str, list[str]]] = {}
    for metric_id, declaration in sorted(metrics.items()):
        if not isinstance(declaration, Mapping):
            continue
        kind = str(declaration.get("kind") or declaration.get("semantic_type") or "")
        if not kind:
            continue
        label = str(declaration.get("label") or metric_id)
        by_label.setdefault(label, {}).setdefault(kind, []).append(str(metric_id))
    pairs: list[str] = []
    for label in sorted(by_label):
        owners = by_label[label]
        if len(owners) < 2:
            continue
        listing = ", ".join(
            f"{kind}={'/'.join(sorted(ids))}" for kind, ids in sorted(owners.items())
        )
        selection = metric_recipe_selection.select_recipe(
            metric_recipe_candidate(metric_id, metrics[metric_id])
            for ids in owners.values()
            for metric_id in ids
            if isinstance(metrics.get(metric_id), Mapping)
        )
        default = (
            ""
            if selection is None
            else "; 원문이 가르지 않으면 "
            + metric_recipe_selection.describe_selection(selection)
        )
        pairs.append(f'- "{label}": {listing}{default}')
    if not pairs:
        return []
    return [
        "",
        "[Metric kind selection]",
        "같은 표면어에 kind가 다른 지표가 함께 걸려 있다. 회원 한 명의 값을 임계와 비교하는 "
        "조건이면 회원별 지표(member_scalar/field/transition)의 Exists recipe를, 모집단 전체의 "
        "집계·평균·순위·상위 N%면 aggregate 지표를 쓴다.",
        "둘은 모집단과 NULL 정책이 다른 별개의 SQL이다. 한 조건 안에서 둘을 섞거나 번갈아 쓰지 않는다.",
        *pairs,
    ]


def audience_catalog_guidance(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> str:
    """Human-readable, declaration-derived glossary for the structuring model."""
    # ``source_category`` domains (gender/grade/login_channel, …) keep their
    # values in member_target_filters.json.  Render the resolved snapshot so
    # the model sees those canonical values too; using the raw JSON here made
    # every referenced domain silently disappear from the prompt glossary.
    raw = catalog_snapshot(path)
    lines = [
        "[Canonical Audience IR]",
        "타겟 조건은 audience_requirement.expression 하나에 Event IR로 작성한다. target_user/exclude/SQL은 만들지 않는다.",
        "부재·제외는 Not, 존재는 Exists, 임계는 Comparison, 횟수·금액은 Aggregate를 조합한다.",
        "전역 순위 집합은 Limit(Order(Summarize(...)))로 만들고, 회원 행동과의 포함/제외는 semi/anti Join으로 조합한다.",
        "전역 집계 Source에만 correlation=none을 쓰고, 회원별 행동 Source는 correlation 키를 생략한다.",
        "wire 형식은 아래 [Fixed wire shapes]를 그대로 따른다. source/field/unit 같은 축약 키를 새로 만들지 않는다.",
        "Comparison/Exists/TemporalRelation에 원문 그대로의 evidence(text/start/end)를 붙인다. Comparison evidence는 그 비교가 소비한 값과 비교 연산자 문구를 모두 포함한다.",
        "subject.* 프로필 필드는 현재 회원 행의 scalar FieldRef다. subject를 Source/Filter/Exists로 감싸지 않고 Comparison에서 직접 사용한다.",
        "catalog source에 고정 필터가 선언되어 있으므로 그 SQL 조건을 다시 만들지 않는다.",
        "'최근'처럼 시간 한정이 있으나 기간 값이 없으면 전체 이력으로 간주하지 말고 expression=null과 missing_argument(period) issue를 낸다.",
        "",
        "[Fixed wire shapes]",
        '- Source: {"type":"source","name":"<source_id>"}; 전역이면 correlation:"none"만 추가',
        '- FieldRef: {"type":"field","name":"<field_id>"}',
        '- Literal: {"type":"literal","value":<application literal value>}',
        '- TimeFilter: {"type":"time_filter","field":<FieldRef>,"window":<TimeWindow>}; window는 그 기간을 말한 literal binding의 normalized.event_ir_window를 **그대로 복사**한다. 절대 구간·rolling·relative가 모두 같은 계약이다',
        # 창의 값·단위·종류를 모델이 옮겨 적게 하던 동안, 이 추출기의 단위 표기(복수형 'days')가
        # 툴 스키마 enum(day|week|month|year) 밖이라 그대로 복사한 응답이 검증에서 떨어졌다
        # (실측 2026-08-07). 지금은 옮길 것이 객체 하나뿐이라 옮기다 틀릴 자리가 없다.
        '- 창의 값·단위·종류는 전부 애플리케이션 소유다: semantic_unit(내부 표기)이나 표면어를 읽어 window를 조립하지 않는다. binding에 event_ir_window가 없으면 그 리터럴은 기간 창이 아니므로(임계값 등) 창을 지어내지 않는다',
        '- Filter: {"type":"filter","relation":<Relation>,"where":<Condition>}',
        '- Aggregate: {"type":"aggregate","function":"sum|count|avg|min|max","relation":<Relation>,"expression":<Scalar|null>,"distinct":false}',
        '- Comparison: {"type":"comparison","operator":"=|!=|>|>=|<|<=","left":<Scalar>,"right":<Scalar>,"evidence":{"text":"...","start":0,"end":1}}',
        '- Exists: {"type":"exists","relation":<Relation>,"evidence":{"text":"...","start":0,"end":1}}',
        '- Not/And/Or: {"type":"not","operand":<Condition>} / {"type":"and|or","operands":[<Condition>,...]}',
        '- Summarize: {"type":"summarize","relation":<Relation>,"keys":[{"name":"entity_key","expression":<FieldRef>}],"measures":[{"name":"measure_value","function":"sum","expression":<FieldRef>,"distinct":false}]}',
        '- Order + count Limit: {"type":"limit","relation":{"type":"order","relation":<Summarize>,"keys":[{"name":"measure_value","direction":"desc"},{"name":"entity_key","direction":"asc"}]},"count":<N>}',
        '- Order + percent Limit: {"type":"limit","relation":{"type":"order","relation":<Summarize>,"keys":[{"name":"measure_value","direction":"desc"},{"name":"entity_key","direction":"asc"}]},"percent":<P>}; 0<P<100',
        '- semi Join: {"type":"join","kind":"semi","left":<member-correlated Relation>,"right":<Limit>,"on":{"type":"comparison","operator":"=","left":<member entity FieldRef>,"right":<rank entity FieldRef>,"evidence":<exact evidence object>}}',
        '- 순위 회원 조건: expression = Exists(semi Join). Join 자체는 Relation이므로 expression 루트에 직접 둘 수 없음. Join.left 회원 Source는 correlation 생략, Join.right의 Summarize 아래 전역 Source는 correlation:"none" 필수',
        '- Summarize의 name은 Order.keys.name에서만 쓰는 로컬 alias다. Join.on 양쪽은 catalog FieldRef를 쓰며 같은 field id라도 left/right relation scope로 구분됨',
        '- 순위 회원 조건의 Join.left.name과 Join.on 양쪽 field name은 relation recipe metrics의 source/entity_field를 그대로 재사용한다. "member"/"subject" Source나 member.member_id 같은 새 심볼을 만들지 않는다.',
        '- 내부 상/하위 N명은 Limit.count, 상/하위 N%는 Limit.percent다. 상위는 첫 정렬키 desc, 하위는 asc이며 회원키 asc를 두 번째 키로 둔다. 둘 다 최종 회원 반환 수인 root result_limit과 별개다.',
        '- 기간 집계: Aggregate.relation = Filter(Source, TimeFilter(<source>.occurred_at, event_ir_window))',
        '- TimeFilter.field 는 언제나 그 소스의 <source>.occurred_at 이다(소스별 목록과 grain은 아래 [Event time fields]). '
        '문자열로 적재된 시각 컬럼처럼 그 밖의 field 에는 기간 창을 걸 수 없다 — 시간 컬럼이 아니라 컴파일되지 않는다',
        '- 회원별 건수·종류 임계: Comparison(Aggregate(count, distinct, <source>.<field>, relation=Source 또는 Filter), <operator>, Literal(N)). 이 비교가 곧 회원 조건이므로 Exists로 감싸지 않는다 — Exists 안에 Aggregate 임계를 넣으면 같은 판정이 두 번 붙거나 컴파일되지 않는다',
        '- "서로 다른 N개"·"N종"·"상품 종류 수"는 distinct:true(세는 대상은 상품 식별자 field)이고, "라인 수"·"건수"·"총 개수"는 distinct:false다. 둘은 같은 카트에서 값이 다르다',
        '- Aggregate.expression의 field는 Aggregate.relation이 제공하는 소스에 선언된 field여야 한다. 같은 테이블을 쓰는 형제 소스라도 다른 소스의 field를 참조하면 관계 스코프 밖이라 컴파일되지 않는다 — 세려는 field가 [Fields]에 선언된 소스를 relation으로 고른다',
        '- 회원별 지표(member_scalar_*, 기준월 스냅샷 field/transition)는 회원당 0..1행을 읽는 관계다. bare FieldRef나 Comparison 단독으로 쓰면 참조할 관계가 없어 컴파일되지 않는다. [Metric recipes]의 Exists(Filter(Source, Comparison)) 골격을 그대로 쓴다',
        '- member_scalar_*의 Literal은 숫자 임계값이고 operator는 그 지표의 allowed_operators 중 하나다. 값의 단위는 지표 선언의 unit이며 원문의 단위를 그대로 옮긴다',
        '- 기준월 스냅샷 field/transition 지표의 Literal은 [Canonical value domains]의 canonical 값이다. 물리코드나 원문 표기를 그대로 넣지 않는다',
        '- 전이 지표는 도착값(expression_field)과 출발값(prev_expression_field) 비교를 And로 묶은 한 Exists다. 현재 시점 비교 하나로 낮추면 전이가 아니다',
        '- 프로필 값: Comparison(FieldRef("subject.<field>"), Literal); subject Source나 프로필 Exists를 만들지 않음',
        '- evidence 객체는 Comparison/Exists/TemporalRelation에만 둔다. 문자열 evidence나 임의 키는 금지한다.',
        "",
        "[Sources]",
    ]
    for source_id, declaration in sorted((raw.get("sources") or {}).items()):
        if not isinstance(declaration, Mapping):
            continue
        label = str(declaration.get("label") or source_id)
        # 같은 물리 테이블을 상태로 나눠 가진 소스끼리는 별칭만으로 구분되지 않는다
        # (cart 이력 ↔ active_cart 현재 보관). 어느 쪽을 고르는지는 도메인 사실이라
        # 카탈로그가 선언하고, 여기서는 그 선언을 그대로 옮기기만 한다.
        #
        # selection_surfaces 를 aliases 와 **따로** 읽는 이유는 aliases 가 겸용 어휘이기
        # 때문이다 — rolling_absence_claims 가 같은 목록으로 '이 사건의 국소 부정'을
        # 접지한다. 모델에게 소스를 알려주려고 긍정 상태 표면어를 aliases 에 넣으면 그쪽
        # 판정이 함께 넓어져 창 방향이 뒤집힌 부재식이 생긴다(실측 2026-08-07).
        surfaces = ", ".join(
            str(item)
            for item in (*(declaration.get("aliases") or []), *(declaration.get("selection_surfaces") or []))
        )
        note = str(declaration.get("selection_note") or "").strip()
        lines.append(
            f"- {source_id}: {label}"
            + (f" ({surfaces})" if surfaces else "")
            + (f" — {note}" if note else "")
        )
    lines.extend(_source_time_field_lines(raw.get("sources") or {}))
    lines.extend(["", "[Fields]"])
    for field_id, declaration in sorted((raw.get("fields") or {}).items()):
        if not isinstance(declaration, Mapping):
            continue
        label = str(declaration.get("label") or field_id)
        unit = str(declaration.get("unit") or "")
        aliases = ", ".join(str(item) for item in declaration.get("aliases") or [])
        match_mode = str(declaration.get("match_mode") or "")
        details = []
        if unit:
            details.append(f"unit={unit}")
        if match_mode:
            details.append(f"match={match_mode}")
        lines.append(
            f"- {field_id}: {label}"
            + (f" ({aliases})" if aliases else "")
            + (f" [{', '.join(details)}]" if details else "")
        )
    text_search_fields = [
        field_id
        for field_id, declaration in sorted((raw.get("fields") or {}).items())
        if isinstance(declaration, Mapping) and declaration.get("match_mode") == "contains"
    ]
    if text_search_fields:
        lines.extend([
            "",
            "[Product text matching]",
            "- 상품명·카테고리명 같은 자연어 값은 PRODUCT_ID와 비교하지 않는다. "
            "구매는 purchase_line.product_text/product_name/product_category, 장바구니는 cart.product_text/product_name/product_category 중 맞는 필드에 = 비교를 사용한다. 컴파일러가 CRM_CM_PRODUCT의 안전한 LIKE 부분검색으로 바꾼다.",
            "- 상품 텍스트 검색 필드는 상품 마스터 조인이 있는 소스에만 있다. active_cart에는 없으므로, "
            "'담아둔 <상품명>'처럼 현재 보관 + 상품명이 함께 오면 cart의 product_text 계열을 쓴다 — "
            "active_cart 관계에 cart.* 필드를 거는 것은 관계 스코프 밖이라 컴파일되지 않는다.",
            "- 'A, B, C를 모두 구매'는 각 값마다 독립된 Exists(Filter(Source(purchase_line), Comparison))를 만들고 그 Exists들을 And로 묶는다. 한 상품 행에 A/B/C 비교를 모두 걸지 않는다.",
            "- 'A, B, C 중 하나라도 구매'는 한 Exists의 Filter.where에서 비교들을 Or로 묶거나, 독립 Exists들을 Or로 묶는다.",
            "- 'A를 구매하지 않은'은 Not(Exists(Filter(... A 비교 ...)))이다.",
            "- 'A 외 상품을 구매'는 Exists(Filter(... Not(A 비교) ...))이다. 이는 A 미구매와 뜻이 다르다.",
            "- 'A를 산 적은 없지만 다른 상품은 구매'는 And(Not(Exists(A 필터)), Exists(Not(A) 필터))이다.",
            "- 자연어 검색 필드: " + ", ".join(text_search_fields),
        ])
    lines.extend(["", "[Canonical value domains]"])
    for domain_id, declaration in sorted((raw.get("value_domains") or {}).items()):
        if not isinstance(declaration, Mapping):
            continue
        values = declaration.get("values")
        if not isinstance(values, Mapping):
            continue
        rendered: list[str] = []
        for canonical, value_declaration in sorted(values.items()):
            aliases = (
                value_declaration.get("aliases")
                if isinstance(value_declaration, Mapping) else []
            )
            rendered.append(
                str(canonical)
                + (f" ({', '.join(map(str, aliases))})" if aliases else "")
            )
        lines.append(f"- {domain_id}: " + ", ".join(rendered))
    lines.extend(["", "[Relational recipes]"])
    for recipe_id, recipe in sorted((raw.get("relation_recipes") or {}).items()):
        if not isinstance(recipe, Mapping):
            continue
        lines.append(
            f"- {recipe_id}: label={recipe.get('label') or recipe_id}, "
            f"default_measure={recipe.get('defaultMeasure')}, "
            f"default_relation={recipe.get('defaultRankRelation')}; "
            "Exists(semi/anti Join(member Source, Limit(Order(Summarize(global Source)))))"
        )
        for vocabulary_key in ("directions", "entities", "measures", "metrics"):
            vocabulary = recipe.get(vocabulary_key)
            if isinstance(vocabulary, Mapping):
                lines.append(
                    f"  - {vocabulary_key}="
                    + json.dumps(vocabulary, ensure_ascii=False, sort_keys=True)
                )
        if isinstance(recipe.get("policy"), Mapping):
            lines.append(
                "  - policy="
                + json.dumps(recipe["policy"], ensure_ascii=False, sort_keys=True)
            )
        if isinstance(recipe.get("limit_units"), list):
            lines.append(
                "  - limit_units="
                + json.dumps(recipe["limit_units"], ensure_ascii=False)
            )
        relations = recipe.get("relations")
        if not isinstance(relations, Mapping):
            continue
        for relation_id, declaration in sorted(relations.items()):
            if not isinstance(declaration, Mapping):
                continue
            source = declaration.get("canonicalSource")
            entities = declaration.get("canonicalEntities") or {}
            measures = declaration.get("canonicalMeasures") or {}
            lines.append(
                f"  - relation={relation_id}, source={source}, "
                f"entities={json.dumps(entities, ensure_ascii=False, sort_keys=True)}, "
                f"measures={json.dumps(measures, ensure_ascii=False, sort_keys=True)}"
            )
    lines.extend(["", "[Metric recipes]"])
    metrics = raw.get("metrics") or {}
    fields = raw.get("fields") or {}
    for metric_id, declaration in sorted(metrics.items()):
        if not isinstance(declaration, Mapping):
            continue
        label = str(declaration.get("label") or metric_id)
        wire = metric_recipe_wire(declaration)
        if wire is None:
            lines.append(f"- {metric_id} ({label}): {METRIC_RECIPE_UNAVAILABLE}")
            continue
        lines.append(
            f"- {metric_id} ({label}): "
            + json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
        )
        detail = _metric_recipe_detail_line(declaration, fields)
        if detail is not None:
            lines.append(detail)
    lines.extend(_competing_metric_kind_lines(metrics))
    return "\n".join(lines)


def catalog_snapshot(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> dict[str, Any]:
    """Defensive copy for diagnostics/tests; callers cannot mutate the cache.

    값 도메인은 **해석된 뷰**로 돌려준다 — 소비자(청구 감지기 등)가 필요로 하는 것은
    "이 필드가 어떤 값을 갖는가"이고, 그 값의 소유자가 eq_filters 라는 사실은 로딩 상세다.
    원본을 그대로 주면 source_category 참조만 보이고 값 어휘가 통째로 사라진다.
    """
    snapshot = materialize_member_metric_rankings(
        load_audience_catalog_config(path), catalog_path=path
    )
    materialized = materialize_value_domains(snapshot)
    if materialized:
        snapshot["value_domains"] = materialized
    return snapshot


def compiler_source_aliases(context: event_compiler.CompileContext) -> set[str]:
    """Derive aliases owned by trusted catalog source bindings."""
    from sql_ast import SelectAst, collect_aliases

    aliases = {context.subject.alias}
    for spec in context.registry.values():
        source_sql = event_compiler.render_source_binding(spec, context)
        aliases.update(
            collect_aliases(SelectAst(columns=[], from_lines=[f"FROM {source_sql}"]))
        )
    return aliases


def extend_sql_validation_aliases(
    config: Any, context: event_compiler.CompileContext
) -> dict[str, Any] | None:
    """Defensively add only aliases rendered from trusted catalog bindings."""
    if not isinstance(config, dict):
        return None
    resolved = copy.deepcopy(config)
    allowed = resolved.get("allowed_table_aliases")
    if not isinstance(allowed, list) or not allowed:
        return resolved
    try:
        catalog_aliases = compiler_source_aliases(context)
    except Exception:  # noqa: BLE001 - retain the static fail-closed set.
        catalog_aliases = set()
    resolved["allowed_table_aliases"] = sorted(
        {str(alias) for alias in allowed} | catalog_aliases,
        key=lambda alias: alias.upper(),
    )
    return resolved


def ranked_membership_labels(
    expression: event_ir.Condition,
    registry: Mapping[str, event_compiler.EventSpec],
) -> dict[tuple[str, bool, int], str]:
    """Render catalog-aware labels from the generic ranked-membership view."""
    labels: dict[tuple[str, bool, int], str] = {}
    for view in event_ir.ranked_membership_views(expression):
        spec = registry.get(view.source)
        source_label = spec.label if spec is not None and spec.label else view.source
        direction = "상위" if view.direction == "desc" else "하위"
        unit = "%" if view.unit == "percent" else "개"
        excluded = " 제외" if view.negated else ""
        labels[(view.source, view.negated, id(view.evidence))] = (
            f"{source_label} {direction} {view.value}{unit}{excluded}"
        )
    return labels


__all__ = [
    "AudienceCatalogLoadError",
    "DEFAULT_AUDIENCE_CATALOG_PATH",
    "DEFAULT_EXTERNAL_REGION_MAPPING_PATH",
    # 프롬프트 절 제목은 모듈 밖(안내를 읽는 쪽·테스트)에서 절을 잘라내는 좌표다.
    # METRIC_RECIPE_* 와 같은 성격이므로 같은 자리에 노출한다.
    "EVENT_TIME_FIELD_HEADING",
    "METRIC_RECIPE_BUILDERS",
    "METRIC_RECIPE_EVIDENCE_TEXT",
    "METRIC_RECIPE_OPERATOR_PLACEHOLDER",
    "METRIC_RECIPE_PREVIOUS_VALUE_PLACEHOLDER",
    "METRIC_RECIPE_THRESHOLD_PLACEHOLDER",
    "METRIC_RECIPE_UNAVAILABLE",
    "METRIC_RECIPE_VALUE_PLACEHOLDER",
    "audience_catalog_guidance",
    "audience_expression_json_schema",
    "catalog_snapshot",
    "compiler_source_aliases",
    "extend_sql_validation_aliases",
    "ranked_membership_labels",
    "load_audience_catalog_config",
    "materialize_member_metric_rankings",
    "member_metric_registry_snapshot",
    "MetricRecipeBuilder",
    "metric_recipe_builder_kind",
    "metric_recipe_candidate",
    "metric_recipe_wire",
    "resolve_audience_catalog",
]
