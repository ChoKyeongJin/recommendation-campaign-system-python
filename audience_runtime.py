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
import member_filters_config
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


@functools.lru_cache(maxsize=4)
def resolve_audience_catalog(
    path: str | Path = DEFAULT_AUDIENCE_CATALOG_PATH,
) -> resolved_semantic_catalog.ResolvedSemanticCatalog:
    raw = dict(load_audience_catalog_config(path))
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
    raw = load_audience_catalog_config(path)
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
        '- Order/Limit: {"type":"limit","relation":{"type":"order","relation":<Summarize>,"keys":[{"name":"measure_value","direction":"desc"},{"name":"entity_key","direction":"asc"}]},"count":<N>}',
        '- semi Join: {"type":"join","kind":"semi","left":<member-correlated Relation>,"right":<Limit>,"on":{"type":"comparison","operator":"=","left":<member entity FieldRef>,"right":<rank entity FieldRef>,"evidence":<exact evidence object>}}',
        '- 순위 회원 조건: expression = Exists(semi Join). Join 자체는 Relation이므로 expression 루트에 직접 둘 수 없음. Join.left 회원 Source는 correlation 생략, Join.right의 Summarize 아래 전역 Source는 correlation:"none" 필수',
        '- Summarize의 name은 Order.keys.name에서만 쓰는 로컬 alias다. Join.on 양쪽은 catalog FieldRef를 쓰며 같은 field id라도 left/right relation scope로 구분됨',
        '- 내부 상위 N은 Limit.count이고 최종 회원 반환 수만 root result_limit이다.',
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
        lines.append(f"- {field_id}: {label}" + (f" [unit={unit}]" if unit else ""))
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
        for vocabulary_key in ("directions", "entities", "measures"):
            vocabulary = recipe.get(vocabulary_key)
            if isinstance(vocabulary, Mapping):
                lines.append(
                    f"  - {vocabulary_key}="
                    + json.dumps(vocabulary, ensure_ascii=False, sort_keys=True)
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
    snapshot = copy.deepcopy(load_audience_catalog_config(path))
    materialized = materialize_value_domains(snapshot)
    if materialized:
        snapshot["value_domains"] = materialized
    return snapshot


__all__ = [
    "AudienceCatalogLoadError",
    "DEFAULT_AUDIENCE_CATALOG_PATH",
    "audience_catalog_guidance",
    "audience_expression_json_schema",
    "catalog_snapshot",
    "load_audience_catalog_config",
    "resolve_audience_catalog",
]
