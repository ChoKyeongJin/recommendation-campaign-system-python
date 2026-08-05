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

import audience_schema
import event_compiler
import event_ir
import member_filters_config
import member_policy
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
        '- TimeFilter: {"type":"time_filter","field":<FieldRef>,"window":<TimeWindow>}; 절대 기간은 literal_bindings.normalized.event_ir_window를 그대로 복사하고 rolling/relative 기간은 binding의 값·단위를 사용',
        '- 창의 **종류도 애플리케이션 소유**다: binding.normalized.temporal_kind 가 "rolling_duration"이면 window.type="rolling"(기준일에서 거슬러 세는 길이), "past_point"이면 window.type="relative"(그 시점이 속한 달력 칸). 표면어를 다시 읽어 고르지 않는다',
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
        '- 프로필 값: Comparison(FieldRef("subject.<field>"), Literal); subject Source나 프로필 Exists를 만들지 않음',
        '- evidence 객체는 Comparison/Exists/TemporalRelation에만 둔다. 문자열 evidence나 임의 키는 금지한다.',
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
            recipe = json.dumps({
                "type": "aggregate",
                "function": function,
                "relation": {"type": "source", "name": source},
                "expression": (
                    {"type": "field", "name": expression}
                    if expression != "*" else None
                ),
                "distinct": bool(declaration.get("distinct")),
            }, ensure_ascii=False, separators=(",", ":"))
        elif kind == "existence":
            recipe = json.dumps({
                "type": "exists",
                "relation": {"type": "source", "name": source},
                "evidence": {"text": "<exact source phrase>", "start": 0, "end": 1},
            }, ensure_ascii=False, separators=(",", ":"))
        else:
            recipe = json.dumps({"type": "field", "name": expression}, ensure_ascii=False)
        lines.append(f"- {metric_id} ({label}): {recipe}")
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
    "audience_catalog_guidance",
    "audience_expression_json_schema",
    "catalog_snapshot",
    "compiler_source_aliases",
    "extend_sql_validation_aliases",
    "ranked_membership_labels",
    "load_audience_catalog_config",
    "materialize_member_metric_rankings",
    "member_metric_registry_snapshot",
    "resolve_audience_catalog",
]
