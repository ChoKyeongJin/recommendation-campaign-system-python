"""회원 타기팅 **도메인 계층** — 범용 코어에 주입하는 지식의 단일 소유자.

코어(semantic_plan / semantic_capability / semantic_coverage / semantic_candidates /
semantic_normalizers / semantic_pipeline / semantic_reemission / requirement_ledger /
temporal_semantics)는 이 모듈을 **import 하지 않는다**. 반대 방향이다:

    targeting_domain  ──주입──▶  core
    (도메인 낱말·축·값·슬롯)      (requirement / expression tree / capability / validation)

여기 있는 것:
  1. `docs/data/runtime/semantics/targeting_domain.json` 선언(어휘·축·컨테이너·라벨)
  2. 표면형 정규식 원자(파싱 알고리즘이라 JSON 이 아니라 소스가 소유 — 3계층 규약)
  3. 그 정규식에 끼울 **값 어휘의 파생**(eq_filters / attribute_catalog 단일 소유에서)

없는 것:
  - SQL·컬럼(→ member_target_filters.json / attribute_catalog.json)
  - 실행 슬롯 이름(→ legacy_plan_compiler)
  - 시간 연산자의 **의미**(→ temporal_semantics 의 닫힌 집합)

확장 경계:
  · 새 속성 축·새 값·새 집계 도메인·새 엔터티·새 단위  → JSON 선언만
  · 새 표면 표현(같은 연산자)                          → 이 파일의 마커 템플릿 한 줄
  · 새 시간 연산 **의미**                              → temporal_semantics + 컴파일러 + 검증기
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import member_filters_config
import temporal_semantics

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DOMAIN_PATH = (
    _REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "targeting_domain.json"
)


class TargetingDomainError(ValueError):
    """도메인 선언 파일이 계약을 위반했을 때."""


# ── 선언 로딩 ────────────────────────────────────────────────────────────────────
def _domain_path() -> Path:
    return Path(os.getenv("TARGETING_DOMAIN_PATH") or DEFAULT_DOMAIN_PATH)


@lru_cache(maxsize=4)
def _load(path_text: str) -> str:
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TargetingDomainError(f"도메인 선언을 읽지 못했다: {exc}") from exc
    if not isinstance(payload, dict):
        raise TargetingDomainError("도메인 선언은 객체여야 한다")
    return json.dumps(payload, ensure_ascii=False)


def catalog() -> dict[str, Any]:
    """도메인 선언 사본(호출자가 변형해도 캐시가 오염되지 않는다)."""
    return json.loads(_load(str(_domain_path())))


def reset_cache() -> None:
    """테스트에서 선언 파일을 갈아 끼울 때."""
    _load.cache_clear()
    _attribute_catalog_cached.cache_clear()


def _section(name: str) -> dict[str, Any]:
    value = catalog().get(name)
    return value if isinstance(value, dict) else {}


def _drop_comments(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if not key.startswith("_")}


# ── 코어에 주입하는 선언들 ───────────────────────────────────────────────────────
def vocabulary(name: str) -> tuple[str, ...]:
    """노드 필드의 닫힌 값 목록. 선언이 없으면 빈 튜플(= 제약 없음)."""
    values = _section("vocabularies").get(name)
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise TargetingDomainError(f"vocabularies.{name} 은 문자열 배열이어야 한다")
    return tuple(values)


def vocabularies() -> dict[str, tuple[str, ...]]:
    return {
        name: tuple(values)
        for name, values in _drop_comments(_section("vocabularies")).items()
        if isinstance(values, list)
    }


def vocabulary_glossary(name: str) -> dict[str, str]:
    """어휘 값의 짧은 설명 — LLM 노출 스키마의 description 이 여기서 파생한다."""
    glossaries = _section("vocabulary_glossary")
    entry = glossaries.get(name)
    if not isinstance(entry, Mapping):
        return {}
    return {str(key): str(value) for key, value in _drop_comments(entry).items()}


def plan_container(kind: str = "member_condition") -> str | None:
    """컴파일 산출물이 들어갈 실행 플랜 컨테이너 이름(없으면 플랜 루트)."""
    value = _section("plan_containers").get(kind)
    return str(value) if isinstance(value, str) and value else None


def capability_axes() -> tuple[tuple[str, str], ...]:
    """(선언 키, 노드 필드) — capability 선언이 어떤 노드 필드로 세분되는가."""
    axes = catalog().get("capability_axes")
    if not isinstance(axes, list):
        return ()
    return tuple(
        (str(item["declaration_key"]), str(item["node_field"]))
        for item in axes
        if isinstance(item, Mapping) and item.get("declaration_key") and item.get("node_field")
    )


def subject_fields() -> tuple[str, ...]:
    """노드의 '주어 지표' 후보 필드(capability 판정·라벨링이 읽는다)."""
    values = catalog().get("subject_fields")
    return tuple(str(item) for item in values) if isinstance(values, list) else ()


def identity_fields() -> tuple[str, ...]:
    """'같은 의미인가' 판정에 쓰는 필드 — 후보 병합·충돌 판정의 키."""
    values = catalog().get("identity_fields")
    return tuple(str(item) for item in values) if isinstance(values, list) else ()


def entity_aliases() -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in _drop_comments(_section("entity_aliases")).items()
        if isinstance(value, str)
    }


def counter_units() -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in _drop_comments(_section("counter_units")).items()
        if isinstance(value, str)
    }


def bind_counter_unit(
    surface_unit: str,
    *,
    text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> str | None:
    """계수 표면을 같은 절의 가장 가까운 업무 근거로 결속한다.

    ``개/종``처럼 표면 자체가 뜻을 고정하는 단위는 ``counter_units`` 값을 그대로 쓴다.
    ``회/번/건``처럼 여러 지표에 붙는 단위는 ``counter_contexts`` 선언이 있으며, 유일한
    최단 근거를 찾지 못하면 일반 count로 남긴다. 임의의 order_count 추측은 하지 않는다.
    """

    unit = str(surface_unit or "").strip()
    contexts = catalog().get("counter_contexts")
    candidates = contexts.get(unit) if isinstance(contexts, Mapping) else None
    if not isinstance(candidates, list):
        return counter_units().get(unit)
    if not isinstance(text, str) or not isinstance(start, int) or not isinstance(end, int):
        return None
    if not 0 <= start <= end <= len(text):
        return None

    clause_start = max(
        (text.rfind(marker, 0, start) + 1 for marker in (".", "。", "!", "?", ";", "\n")),
        default=0,
    )
    following = [
        index
        for marker in (".", "。", "!", "?", ";", "\n")
        if (index := text.find(marker, end)) >= 0
    ]
    clause_end = min(following) if following else len(text)
    clause = text[clause_start:clause_end].casefold()
    local_start, local_end = start - clause_start, end - clause_start
    distances: dict[str, int] = {}
    for spec in candidates:
        if not isinstance(spec, Mapping) or not isinstance(spec.get("semantic_unit"), str):
            continue
        semantic_unit = str(spec["semantic_unit"])
        for term in spec.get("terms") or []:
            if not isinstance(term, str) or not term.strip():
                continue
            pattern = re.compile(r"\s*".join(re.escape(part) for part in term.casefold().split()))
            for match in pattern.finditer(clause):
                distance = (
                    local_start - match.end()
                    if match.end() <= local_start
                    else match.start() - local_end
                    if match.start() >= local_end
                    else 0
                )
                distances[semantic_unit] = min(distances.get(semantic_unit, distance), distance)
    if not distances:
        return None
    nearest = min(distances.values())
    winners = [unit_name for unit_name, distance in distances.items() if distance == nearest]
    return winners[0] if len(winners) == 1 else None


def condition_label(node_type: str) -> str:
    labels = _drop_comments(_section("condition_labels"))
    return str(labels.get(node_type) or node_type)


def scope_label(scope: str) -> str:
    labels = _drop_comments(_section("scope_labels"))
    return str(labels.get(scope) or scope)


def node_field_vocabularies() -> dict[str, Any]:
    """'노드 타입.필드 → 닫힌 어휘 키' 결속 선언."""
    return _drop_comments(_section("node_field_vocabularies"))


def node_field_vocabulary_guidance(available: Mapping[str, Any]) -> str:
    """결속 선언 + 실행 어휘 목록 → LLM 안내 문구(파생 — 손으로 쓴 두 번째 권위 금지).

    `available` 은 실행 레지스트리가 준 '어휘 키 → 값 목록'이다. 선언이 가리키는 키가
    그 목록에 없으면 조용히 건너뛴다(어휘가 아직 없는 축은 자유 문자열로 남는다).
    """
    lines: list[str] = []
    for path, spec in sorted(node_field_vocabularies().items()):
        if not isinstance(spec, Mapping):
            continue
        node_type, _, field_name = str(path).partition(".")
        discriminator = spec.get("discriminator")
        by_value = spec.get("by_value") if isinstance(spec.get("by_value"), Mapping) else {}
        default_key = spec.get("default")
        if discriminator and by_value:
            branches = [
                f"{discriminator}='{value}'이면 {key}"
                for value, key in sorted(by_value.items())
                if key in available
            ]
            if branches:
                lines.append(
                    f"- {node_type}.{field_name} 은 " + ", ".join(branches)
                    + " 목록의 값만 쓴다."
                )
        elif isinstance(default_key, str) and default_key in available:
            lines.append(f"- {node_type}.{field_name} 은 {default_key} 목록의 값만 쓴다.")
    if not lines:
        return ""
    return (
        "의미 노드의 지표/속성 값은 아래 닫힌 어휘에서 고른다(목록에 없는 id 를 지어내지 마라 — "
        "적절한 값이 없으면 그 필드를 비워 두면 시스템이 사용자에게 묻는다).\n"
        + "\n".join(lines)
    )


# ── 시간 한정어 결속 ─────────────────────────────────────────────────────────────
def _temporal() -> dict[str, Any]:
    section = catalog().get("temporal")
    return section if isinstance(section, dict) else {}


def temporal_relation_aliases() -> dict[str, str]:
    """도메인 관계명 → 범용 시간 연산자. 알 수 없는 연산자를 가리키면 즉시 실패한다."""
    aliases = _temporal().get("relation_aliases")
    if not isinstance(aliases, Mapping):
        return {}
    resolved: dict[str, str] = {}
    for relation, operator in _drop_comments(aliases).items():
        if str(operator) not in temporal_semantics.OPERATORS:
            raise TargetingDomainError(
                f"temporal.relation_aliases.{relation} 이 알 수 없는 연산자 {operator!r} 를 가리킨다"
            )
        resolved[str(relation)] = str(operator)
    return resolved


def temporal_operator_of(relation: Any) -> str | None:
    """도메인 관계명(또는 범용 연산자 id) → 범용 시간 연산자."""
    qualifier = temporal_semantics.normalize(relation, aliases=temporal_relation_aliases())
    return qualifier.operator if qualifier else None


def execution_operator(operator: str, *, anchored: bool = False) -> str | None:
    """범용 시간 연산자 → 실행 컴파일러의 연산자 이름(없으면 실행 경로 없음)."""
    table = _temporal().get("execution_operators")
    spec = table.get(operator) if isinstance(table, Mapping) else None
    if not isinstance(spec, Mapping):
        return None
    if anchored and isinstance(spec.get("anchored"), str):
        return str(spec["anchored"])
    value = spec.get("unanchored") or spec.get("anchored")
    return str(value) if isinstance(value, str) else None


def temporal_subinterval_unit() -> str:
    return str(_temporal().get("subinterval_unit") or "month")


# ── 값 어휘 파생(이중 소유 금지 — eq_filters / attribute_catalog 가 단일 소유) ──
@lru_cache(maxsize=4)
def _attribute_catalog_cached(path_text: str) -> str:
    import compositional_targeting  # 순환 없음(컴파일러는 이 모듈을 모른다)

    catalog_payload = compositional_targeting.load_attribute_catalog(
        member_filters_config.eq_filters(), path_text
    )
    return json.dumps(catalog_payload, ensure_ascii=False)


def attribute_catalog() -> dict[str, Any]:
    """물리 바인딩 + 값 사전이 조인된 런타임 속성 카탈로그(읽기 실패는 빈 카탈로그)."""
    import compositional_targeting  # 순환 없음

    try:
        return json.loads(
            _attribute_catalog_cached(str(compositional_targeting.DEFAULT_ATTRIBUTE_CATALOG_PATH))
        )
    except (OSError, ValueError):
        return {"attributes": {}}


def _minimal_stems(terms: Iterable[str]) -> tuple[str, ...]:
    """다른 표면형을 **포함하는** 표면형은 버린다.

    '웰컴 등급'은 '웰컴'이 이미 잡으므로 감지력을 더하지 않고, 마커 구간만 길어져 소유권
    판정(스팬 포함 관계)을 흔든다. 남는 것은 서로를 포함하지 않는 최소 어간 집합이다.
    """
    cleaned = sorted({re.sub(r"\s+", " ", term).strip() for term in terms if term and term.strip()})
    stems: list[str] = []
    for term in sorted(cleaned, key=len):
        compact = term.replace(" ", "")
        if any(stem.replace(" ", "") in compact for stem in stems):
            continue
        stems.append(term)
    return tuple(sorted(stems, key=lambda item: (-len(item), item)))


def attribute_value_terms() -> tuple[str, ...]:
    """시간 한정어가 물 수 있는 **속성 값**의 표면형(등급/상태 값 등).

    낱말을 여기 나열하지 않는다 — eq_filters 의 값 사전과 감지 전용 보조 표면형에서
    파생한다. 값이 늘면 설정 한 줄로 감지가 함께 열린다.
    """
    import compositional_targeting  # 순환 없음

    terms: list[str] = []
    for spec in (attribute_catalog().get("attributes") or {}).values():
        for canonical, value_spec in (spec.get("values") or {}).items():
            terms.extend(value_spec.get("synonyms") or [])
            terms.extend(
                compositional_targeting._DETECTOR_EXTRA_VALUE_TOKENS.get(canonical, [])
            )
    return _minimal_stems(terms)


def attribute_axis_terms() -> tuple[str, ...]:
    """시간 한정어가 붙는 **속성 축**의 표면형(등급·상태 등)."""
    terms: list[str] = []
    for spec in (attribute_catalog().get("attributes") or {}).values():
        terms.extend(spec.get("surface_terms") or [])
    stems = _minimal_stems(terms)
    if stems:
        return stems
    fallback = (_temporal().get("attribute_axis_terms") or {}).get("fallback")
    return _minimal_stems(fallback) if isinstance(fallback, list) else ()


def _alternation(terms: Sequence[str]) -> str:
    """표면형 목록 → 공백에 관대한 정규식 교체(긴 것 먼저)."""
    if not terms:
        # 어휘가 비면 아무것도 매치시키지 않는다(빈 교체는 모든 위치에 매치돼 과발화한다).
        return r"(?!)"
    return "|".join(
        r"\s*".join(re.escape(part) for part in term.split())
        for term in sorted(terms, key=lambda item: (-len(item), item))
    )


# ── 시간 한정어 마커 템플릿(정규식 원자 = 소스 소유) ────────────────────────────
# `{values}` = 속성 값 표면형 교체, `{axis}` = 속성 축 표면형 교체.
# 오른쪽은 **범용** 연산자다 — 낱말별 분기가 아니라 낱말→연산자 사상이다.
_TRANSITION_DIRECTIONS: dict[str, str] = {
    "승급": "ascending",
    "강등": "descending",
}
_TEMPORAL_MARKER_TEMPLATES: tuple[tuple[str, str], ...] = (
    (temporal_semantics.CHANGE_BETWEEN, r"{directions}"),
    (
        temporal_semantics.CHANGE_BETWEEN,
        r"(?:{values})\s*(?:에서|부터)\s*(?:{values})\s*"
        r"(?:으로|로)\s*(?:바뀐|바뀌|변하|변경|전환)",
    ),
    (temporal_semantics.IMMEDIATELY_PRECEDING, r"직전\s*(?:{axis})"),
    (temporal_semantics.CHANGE_BETWEEN, r"(?:{values})[가-힣\s]{{0,6}}?(?:이었|였)다가"),
    (temporal_semantics.THROUGHOUT_INTERVAL, r"내내\s*(?:{values})"),
    (temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL, r"한\s*번이라도\s*(?:{values})"),
    (
        temporal_semantics.NEVER_IN_INTERVAL,
        r"한\s*번도\s*(?:{values})[가-힣\s]{{0,10}}?(?:아니|않)",
    ),
    (
        temporal_semantics.UNCHANGED_THROUGHOUT,
        r"(?:{axis})이?\s*한\s*번도[가-힣\s]{{0,10}}?(?:바뀌|변하|변경)",
    ),
    (
        temporal_semantics.CHANGE_COUNT,
        r"(?:{axis})이?[가-힣\s]{{0,8}}?(?:번|회)\s*이상\s*변경",
    ),
    (
        temporal_semantics.AS_OF,
        r"기준월|(?:\d{{4}}\s*년\s*\d{{1,2}}\s*월|지난\s*달|전월)\s*(?:말\s*)?기준",
    ),
    (
        temporal_semantics.THROUGHOUT_INTERVAL,
        r"(?:{values})[가-힣\s]{{0,6}}?(?:{axis})?[을를]?\s*유지",
    ),
)


def temporal_lexicon() -> temporal_semantics.TemporalLexicon:
    """도메인 표면형 → 범용 시간 연산자 어휘(코어가 소비하는 형태로 조립).

    문맥 게이트(축/값이 같은 절에 있어야 인정)는 구매 절의 '한 번이라도/한 번도'가 속성
    이력 조건으로 과발화하던 사고의 방지 장치다 — 게이트 자체도 파생 어휘로 만든다.
    """
    values = _alternation(attribute_value_terms())
    axis = _alternation(attribute_axis_terms())
    directions = _alternation(tuple(_TRANSITION_DIRECTIONS))
    pairs = [
        (
            operator,
            template.format(values=values, axis=axis, directions=directions),
        )
        for operator, template in _TEMPORAL_MARKER_TEMPLATES
    ]
    context = f"(?:{axis})|(?:{values})"
    return temporal_semantics.TemporalLexicon.from_pairs(
        pairs, context=context, flags=re.IGNORECASE
    )


def transition_direction(value: Any) -> str | None:
    """Directional transition cue -> ordered-domain direction.

    The cue vocabulary and the temporal detector share the same table above,
    so recognizing ``승급`` can never drift from its ``CHANGE_BETWEEN`` marker.
    """

    if not isinstance(value, str):
        return None
    compact = re.sub(r"\s+", "", value).casefold()
    return {
        re.sub(r"\s+", "", cue).casefold(): direction
        for cue, direction in _TRANSITION_DIRECTIONS.items()
    }.get(compact)


# ── 자리표시자(= 사용자가 값을 말하지 않은 자리) ────────────────────────────────
# 정규식 원자이므로 JSON 이 아니라 소스가 소유한다(3계층 규약). 오른쪽 캡처는 **무엇을**
# 물어야 하는지다 — 이 판정이 없으면 '특정 브랜드'가 구조화기 실패로 뭉개져서, 사용자가
# 답할 수 있는 유일한 결핍이 답할 수 없는 내부 오류로 보고된다(실측 2026-08-02).
_PLACEHOLDER_RE = re.compile(
    r"(?:특정|어떤|무슨|어느|임의의?|아무)\s*(?P<axis>브랜드|상품|제품|카테고리)"
)


def user_omission_reason(text: str) -> dict[str, str] | None:
    """이 구절이 **사용자 정보 누락**인가. 맞으면 무엇을 물어야 하는지 함께 돌려준다.

    '모든'(무필터)·'해당/그'(지시 참조)는 값을 묻는 표현이 아니므로 여기 없다.
    """
    match = _PLACEHOLDER_RE.search(str(text or ""))
    if match is None:
        return None
    axis = match.group("axis")
    return {
        "axis": axis,
        "matched": match.group(0),
        "question": f"어떤 {axis}를 기준으로 할까요? {axis}명을 알려주세요.",
    }


# ── 코어 주입 번들 ───────────────────────────────────────────────────────────────
def core_bindings() -> dict[str, Any]:
    """코어가 필요로 하는 도메인 지식 한 묶음(주입 지점을 하나로 유지한다)."""
    return {
        "vocabularies": vocabularies(),
        "capability_axes": capability_axes(),
        "subject_fields": subject_fields(),
        "identity_fields": identity_fields(),
        "entity_aliases": entity_aliases(),
        "counter_units": counter_units(),
        "temporal_aliases": temporal_relation_aliases(),
        "condition_labels": _drop_comments(_section("condition_labels")),
    }


# ── 코어에 앵커 공급자 등록(조립) ───────────────────────────────────────────────
def _literal_anchor_provider(query: str) -> list[Any]:
    """값 원자(날짜창·수량·퍼센트 …) 앵커. 추출기는 도메인 구조화 계층 소유."""
    import semantic_coverage  # 순환 없음(코어는 도메인을 모른다)
    from query_structurer.semantic_ir import extract_literal_bindings

    return semantic_coverage.literal_anchors_from(extract_literal_bindings(query))


def _obligation_anchor_provider(query: str) -> list[Any]:
    """값 없는 의미 연산자(반복·집합·스냅샷·시간 한정어)의 요구 원장 앵커."""
    import semantic_coverage  # 순환 없음
    import semantic_requirements

    return semantic_coverage.obligation_anchors_from(
        query, semantic_requirements.capture_source_semantic_obligations(query)
    )


def install() -> None:
    """도메인 지식을 코어에 등록한다(조립 지점 — 여러 번 호출해도 멱등)."""
    import semantic_coverage  # 순환 없음

    semantic_coverage.register_anchor_provider("literal", _literal_anchor_provider)
    semantic_coverage.register_anchor_provider("obligation", _obligation_anchor_provider)


def extension_boundary() -> dict[str, list[str]]:
    """JSON 선언만으로 열리는 범위 vs 코드가 필요한 범위(문서·테스트가 읽는 표)."""
    return {
        "declaration_only": [
            "새 속성 축(attribute_catalog.json 항목)",
            "새 속성 값·동의어(member_target_filters.json eq_filters)",
            "새 집계 도메인/그레인/방향 등 노드 필드 값(targeting_domain.json vocabularies)",
            "새 엔터티 별칭·계수 단위(targeting_domain.json)",
            "기존 시간 연산자의 실행 연산자 이름(targeting_domain.json temporal.execution_operators)",
            "지표/노드 종류의 지원 여부·데이터 그레인·적재 구간(semantic_capabilities.json)",
        ],
        "code_required": [
            "새로운 시간 연산 의미(temporal_semantics 닫힌 집합 + 컴파일러 + 검증기)",
            "새 의미 노드 종류(semantic_plan 클래스 + capability 선언 + 컴파일러 핸들러)",
            "새 값 종류(kind)와 그 정규화기(semantic_normalizers)",
            "새 실행 슬롯(legacy_plan_compiler + targeting_ir SLOT_SHAPES + 빌더)",
            "새 표면 표현 패턴(targeting_domain 마커 템플릿 — 같은 연산자면 한 줄)",
        ],
    }


__all__ = [
    "DEFAULT_DOMAIN_PATH",
    "TargetingDomainError",
    "attribute_axis_terms",
    "attribute_catalog",
    "attribute_value_terms",
    "capability_axes",
    "catalog",
    "condition_label",
    "core_bindings",
    "bind_counter_unit",
    "counter_units",
    "entity_aliases",
    "execution_operator",
    "extension_boundary",
    "identity_fields",
    "install",
    "node_field_vocabularies",
    "node_field_vocabulary_guidance",
    "plan_container",
    "reset_cache",
    "scope_label",
    "subject_fields",
    "temporal_lexicon",
    "temporal_operator_of",
    "temporal_relation_aliases",
    "temporal_subinterval_unit",
    "vocabularies",
    "vocabulary",
    "vocabulary_glossary",
]


# 이 모듈이 import 되는 순간 코어에 도메인 지식이 붙는다. 등록 자체는 지연 클로저라
# 무거운 의존을 끌어오지 않고, 순환도 만들지 않는다.
install()
