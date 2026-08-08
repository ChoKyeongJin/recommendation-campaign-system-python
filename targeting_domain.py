"""회원 타기팅 **도메인 계층** — 범용 코어에 주입하는 지식의 단일 소유자.

코어(semantic_normalizers / temporal_semantics / semantic_domain_binding)는 이 모듈을
**import 하지 않는다**. 반대 방향이다:

    targeting_domain  ──주입──▶  core
    (도메인 낱말·축·값·슬롯)      (requirement / expression tree / capability / validation)

여기 있는 것:
  1. `docs/data/runtime/semantics/targeting_domain.json` 선언(어휘·축·컨테이너·라벨)
  2. 표면형 정규식 원자(파싱 알고리즘이라 JSON 이 아니라 소스가 소유 — 3계층 규약)
  3. 그 정규식에 끼울 **값 어휘의 파생**(eq_filters / attribute_catalog 단일 소유에서)

없는 것:
  - SQL·컬럼(→ member_target_filters.json / attribute_catalog.json)
  - 실행 슬롯 이름(→ targeting_ir SLOT_SHAPES)
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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import lexicon_patterns
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
    """시간 한정어가 붙는 **속성 축**의 표면형(등급·상태 등).

    최소 어간이므로 **감지**용이다 — 어느 축인지 가리는 데 쓰면 안 된다(:func:`attribute_axis_
    identifying_terms` 참고). '가치등급'이 '등급'에 흡수돼 사라지는 것이 여기서는 옳다.
    """
    terms: list[str] = []
    for spec in (attribute_catalog().get("attributes") or {}).values():
        terms.extend(spec.get("surface_terms") or [])
    stems = _minimal_stems(terms)
    if stems:
        return stems
    fallback = (_temporal().get("attribute_axis_terms") or {}).get("fallback")
    return _minimal_stems(fallback) if isinstance(fallback, list) else ()


def attribute_axis_identifying_terms() -> tuple[str, ...]:
    """축을 **가리기 위한** 표면형 전량(긴 것 먼저).

    감지 어휘(:func:`attribute_axis_terms`)와 다른 목록인 것이 요점이다. 감지는 "여기 속성
    축이 있다"만 알면 되므로 최소 어간이 낫지만, 식별은 "어느 축인가"를 답해야 하므로 짧은
    어간에 흡수된 표면어('가치등급')가 살아 있어야 한다. 두 목록을 하나로 합치면 둘 중 하나가
    반드시 틀린다 — 합쳐 두던 동안 '가치등급이 승급한'이 ZTS 축으로 해석됐다(2026-08-08 실측).
    """
    terms = {
        str(term)
        for spec in (attribute_catalog().get("attributes") or {}).values()
        for term in spec.get("surface_terms") or []
        if str(term).strip()
    }
    return tuple(sorted(terms, key=lambda item: (-len(item), item)))


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
# 하위 구간 표면어 → 칸 단위(temporal_ir 의 닫힌 TimeUnit 어휘). 방향어와 같은 규약이다 —
# 마커 어휘와 단위 해석이 같은 표를 읽어야 '매월'이 마커로는 잡히는데 단위는 못 읽는 조합이
# 생기지 않는다.
_SUBINTERVAL_UNITS: dict[str, str] = {
    "매월": "month",
    "매달": "month",
    "달마다": "month",
    "모든 달": "month",
    "매주": "week",
    "주마다": "week",
    "매년": "year",
    "해마다": "year",
}
# 관측 선택자 표면어 → **선택자 후보** 연산자. 이 표가 뜻하는 것은 "이 낱말이 어느 관측을
# 고르라는 말인가" 하나뿐이고, **그 말이 기간인지 관측인지는 여기서 정하지 않는다** — 그것은
# 이 낱말이 수식하는 머리(head)가 정한다(:func:`temporal_head_kind`).
#
# '최근'을 무조건 기간으로 읽던 동안 '최근 상태'가 기간 결핍으로 닫혔다(2026-08-08 실측).
# 반대로 여기서 '최근'을 무조건 관측 선택자로 바꾸면 '최근 30일 구매'가 망가진다. 그래서
# 아래 템플릿은 **머리가 속성 축이거나 그 축의 값일 때만** 마커를 만든다 — 머리가 달력 단위나
# 사건이면 이 템플릿은 아예 물지 않고 기존 기간 문법이 그대로 읽는다.
_OBSERVATION_SELECTOR_CUES: dict[str, str] = {
    "최근": temporal_semantics.AS_OF,
    "최신": temporal_semantics.AS_OF,
    "현재": temporal_semantics.AS_OF,
    "지금": temporal_semantics.AS_OF,
    "직전": temporal_semantics.IMMEDIATELY_PRECEDING,
    "이전": temporal_semantics.IMMEDIATELY_PRECEDING,
}

# ── 존재/부재 한정어 어휘(선언형) ────────────────────────────────────────────────
# '한 번이라도'·'적어도 한 달은'·'한 번도 … 없는' 은 서로 다른 낱말이지만 **같은 축의 값**
# 이다(∃ / ¬∃). 어형마다 정규식을 하나씩 늘리면 같은 뜻이 열 곳에 흩어지므로, 여기서는
# 구성 요소만 선언하고 조합은 아래 템플릿 **두 줄**이 한다. 새 어형은 이 표 한 줄이다.
#
# 수(number)를 어휘로 두지 않는 것이 이 표의 핵심 계약이다 — 받는 수는 관형사 '한' 하나뿐이고
# 숫자형('3번')은 어디에도 없다. `적어도`·`최소`·`이상` 이 이미 **수치 비교 축의 소유어**라
# (:func:`condition_normalizers.comparison_literal_operators`), 수를 열어 두면 '적어도 3번
# 구매'가 시간 존재 한정으로 오탐된다. 뜻이 '최소 하나'로 고정될 때만 이 축이 발화한다.

# 한 번의 발생/관측을 세는 단위. 사건('번·차례·회')과 달력 칸('달·개월·주·년·해')이 같은
# 자리에 오는 이유는 둘 다 "관측 하나"를 세기 때문이다 — 스냅샷 축에서 ∃관측 ≡ ∃칸이다.
_OCCURRENCE_UNITS: tuple[str, ...] = (
    "번",
    "차례",
    "회",
    "달",
    "개월",
    "주",
    "년",
    "해",
)
# '한 <단위>' **앞**에 와서 그것을 최소성으로 만드는 표지.
_AT_LEAST_PREFIXES: tuple[str, ...] = ("적어도", "최소한", "최소")
# '한 <단위>' **뒤**에 와서 그것을 최소성으로 만드는 표지. 접두가 없을 때는 이 중 하나가
# 반드시 있어야 한다 — 없으면 '최근 한 달'(창)과 구분되지 않는다.
_AT_LEAST_SUFFIXES: tuple[str, ...] = ("이라도", "라도", "이상")
# 접두가 이미 최소성을 말했을 때 뒤에 붙을 수 있는 잉여 조사('적어도 한 달은').
_OCCURRENCE_PARTICLES: tuple[str, ...] = (*_AT_LEAST_SUFFIXES, "은", "는", "만")
# 부재 표지('한 번도'). 접두는 받지 않는다 — '최소 한 번도'는 한국어가 아니다.
_NEVER_SUFFIXES: tuple[str, ...] = ("도",)
# 값 뒤에서 그 값의 성립을 부정하는 꼬리. '없'이 빠져 있던 동안 '한 번도 VIP였던 적이
# 없는'이 마커를 내지 못했다(2026-08-08 실측).
_EXISTENCE_NEGATION_TAILS: tuple[str, ...] = ("아니", "않", "없")
# 값 뒤에서 그 값의 성립을 긍정하는 꼬리('…였던 적이 있는').
_EXISTENCE_AFFIRMATION_TAILS: tuple[str, ...] = ("있",)
# 경험 명사 — 이 낱말이 있어야 꼬리가 그 값의 것이다('골드였던 **적**이 없는'). 없으면
# 같은 절의 다른 서술어('… 구매한 적이 없는')가 값의 꼬리 행세를 한다.
_EXPERIENCE_NOUNS: tuple[str, ...] = ("적", "경험")

# 값과 그 꼬리 사이에 허용하는 글자 수. 활용형('이었던'·'등급이었던')은 통과시키되 다른 절의
# 서술어는 배제한다 — 절 경계 어휘가 끼면 아예 물지 않는다(아래 `_boundary_free`).
_EXPERIENCE_GAP = 8


def _boundary_free(length: int) -> str:
    """절 경계 낱말을 만나지 않는 한글/공백 구간 정규식(최대 ``length`` 글자).

    경계 어휘의 소유자는 :mod:`lexicon_patterns` 다 — 여기서 낱말을 다시 적지 않는다.
    이 가드가 없으면 '골드 회원 중 구매한 적이 없는'의 '적이 없는'이 골드의 꼬리가 된다.
    """

    boundary = lexicon_patterns.alternation(
        "audience_frame_noun", "clause_scope_marker", "and_connective", "or_connective"
    )
    if not boundary:
        return rf"[가-힣\s]{{0,{length}}}?"
    return rf"(?:(?!{boundary})[가-힣\s]){{0,{length}}}?"
# 선택자 낱말과 머리 사이에 올 수 있는 조사. 뜻을 바꾸지 않으므로 마커 구간에 함께 넣는다
# ('지금은 VIP'의 머리는 값이고 '직전에는 골드'도 같은 모양이다).
_SELECTOR_PARTICLES = r"(?:에는|엔|은|는|의|에|이|가)?"

# 머리의 종류. 값은 선택자 낱말의 뜻을 확정하는 **유일한** 근거다(I1).
HEAD_ATTRIBUTE = "attribute"  # 속성 축 또는 그 축의 값 → 관측 선택자
HEAD_OTHER = "other"          # 달력 단위·사건 등 → 기존 기간/칸 문법

_TEMPORAL_MARKER_TEMPLATES: tuple[tuple[str, str], ...] = (
    (temporal_semantics.CHANGE_BETWEEN, r"{directions}"),
    (
        # 'A에서 B로 …' 한 구절이 마커가 되는 형태. 서술어에 방향어({directions})와 '되다'
        # 계열을 함께 두는 이유는 근거 구간이다 — 방향어만 마커가 되면('승급') 값 두 개가
        # 마커 밖에 남아 근거 스팬이 전이 구절을 덮지 못한다. 뜻은 같고 구간만 정확해진다.
        temporal_semantics.CHANGE_BETWEEN,
        r"(?:{values})\s*(?:에서|부터)\s*(?:{values})\s*"
        r"(?:으로|로|이|가)\s*(?:바뀐|바뀌|변하|변경|전환|되었|됐|된|{directions})",
    ),
    # 관측 선택자(선택자 낱말 + 머리). 머리를 패턴 안에 두는 것이 이 두 줄의 요점이다 —
    # 낱말만으로 마커를 만들면 '최근 30일 구매'까지 관측 선택자가 되고, 머리를 마커 밖에서
    # 찾으면 같은 절의 다른 낱말이 머리 행세를 한다.
    (
        temporal_semantics.AS_OF,
        r"(?:{as_of_cues})\s*{particles}\s*(?:{axis}|{values})",
    ),
    (
        temporal_semantics.IMMEDIATELY_PRECEDING,
        r"(?:{previous_cues})\s*{particles}\s*(?:{axis}|{values})",
    ),
    (temporal_semantics.CHANGE_BETWEEN, r"(?:{values})[가-힣\s]{{0,6}}?(?:이었|였)다가"),
    (temporal_semantics.THROUGHOUT_INTERVAL, r"내내\s*(?:{values})"),
    # 존재/부재 한정. 낱말은 위 어휘 표가 소유하고 여기 있는 것은 **결합 구조**뿐이다:
    # (최소성 표지 + 한 <단위>) + 값, 그리고 값 + (경험 명사 + 긍정/부정 꼬리).
    (
        temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL,
        r"(?:{at_least_cues})\s*(?:{values})",
    ),
    (
        temporal_semantics.NEVER_IN_INTERVAL,
        r"(?:{never_cues})\s*(?:{values}){boundary_free}(?:{negation_tails})",
    ),
    # 꼬리만으로 서는 경험형('골드였던 적이 있는' / '… 적이 없는'). 최소성 표지가 없어도
    # '적'이 그 값의 **과거 성립**을 말하므로 같은 두 연산자로 간다.
    (
        temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL,
        r"(?:{values}){boundary_free}(?:{experience_nouns})\s*이?\s*(?:{affirmation_tails})",
    ),
    (
        temporal_semantics.NEVER_IN_INTERVAL,
        r"(?:{values}){boundary_free}(?:{experience_nouns})\s*이?\s*(?:{negation_tails})",
    ),
    (
        temporal_semantics.UNCHANGED_THROUGHOUT,
        r"(?:{axis})이?\s*한\s*번도[가-힣\s]{{0,10}}?(?:바뀌|변하|변경)",
    ),
    (
        # 횟수는 숫자로 온다 — 사이 글자에 숫자를 허용하지 않으면 '등급이 3회 이상 변경'이
        # 통째로 감지되지 않는다(실측). 감지되지 않으면 미지원 사유조차 남지 않는다.
        temporal_semantics.CHANGE_COUNT,
        r"(?:{axis})이?[가-힣\d\s]{{0,8}}?(?:번|회)\s*이상\s*변경",
    ),
    (
        temporal_semantics.AS_OF,
        r"기준월|(?:\d{{4}}\s*년\s*\d{{1,2}}\s*월|지난\s*달|전월)\s*(?:말\s*)?기준",
    ),
    (
        temporal_semantics.THROUGHOUT_INTERVAL,
        r"(?:{values})[가-힣\s]{{0,6}}?(?:{axis})?[을를]?\s*유지",
    ),
    # 하위 구간 전칭·연속. 단위 표면어를 마커 구간 **안에** 두는 이유는 근거 스팬이다 —
    # 단위를 마커 밖에서 찾으면 같은 문장의 다른 기간 표현('최근 6개월')을 칸 단위로 오해한다.
    (temporal_semantics.EVERY_SUBINTERVAL, r"(?:{subintervals})"),
    (
        temporal_semantics.CONSECUTIVE_SUBINTERVALS,
        r"(?:\d+\s*(?:개월|달|주|년)|{subintervals})\s*(?:연속|연달아|잇달아)",
    ),
)


def _at_least_once_cues() -> str:
    """'최소 하나'를 뜻하는 표면 구절의 정규식(어휘는 위 표들이 소유).

    갈래가 둘인 이유는 최소성이 어디에 실리느냐가 다르기 때문이다. 어느 쪽도 없는 맨
    ``한 <단위>`` 는 **창**이므로('최근 한 달') 여기서 발화하지 않는다.
    """

    units = _alternation(_OCCURRENCE_UNITS)
    prefixed = (
        rf"(?:{_alternation(_AT_LEAST_PREFIXES)})\s*한\s*(?:{units})"
        rf"\s*(?:{_alternation(_OCCURRENCE_PARTICLES)})?"
    )
    suffixed = rf"한\s*(?:{units})\s*(?:{_alternation(_AT_LEAST_SUFFIXES)})"
    return f"(?:{prefixed})|(?:{suffixed})"


def _never_cues() -> str:
    """'한 번도'류 부재 표지의 정규식."""

    return (
        rf"한\s*(?:{_alternation(_OCCURRENCE_UNITS)})"
        rf"\s*(?:{_alternation(_NEVER_SUFFIXES)})"
    )


def temporal_lexicon() -> temporal_semantics.TemporalLexicon:
    """도메인 표면형 → 범용 시간 연산자 어휘(코어가 소비하는 형태로 조립).

    문맥 게이트(축/값이 같은 절에 있어야 인정)는 구매 절의 '한 번이라도/한 번도'가 속성
    이력 조건으로 과발화하던 사고의 방지 장치다 — 게이트 자체도 파생 어휘로 만든다.
    """
    values = _alternation(attribute_value_terms())
    axis = _alternation(attribute_axis_terms())
    directions = _alternation(tuple(_TRANSITION_DIRECTIONS))
    subintervals = _alternation(tuple(_SUBINTERVAL_UNITS))
    pairs = [
        (
            operator,
            template.format(
                values=values,
                axis=axis,
                directions=directions,
                subintervals=subintervals,
                as_of_cues=_alternation(_selector_cues(temporal_semantics.AS_OF)),
                previous_cues=_alternation(
                    _selector_cues(temporal_semantics.IMMEDIATELY_PRECEDING)
                ),
                particles=_SELECTOR_PARTICLES,
                at_least_cues=_at_least_once_cues(),
                never_cues=_never_cues(),
                negation_tails=_alternation(_EXISTENCE_NEGATION_TAILS),
                affirmation_tails=_alternation(_EXISTENCE_AFFIRMATION_TAILS),
                experience_nouns=_alternation(_EXPERIENCE_NOUNS),
                boundary_free=_boundary_free(_EXPERIENCE_GAP),
            ),
        )
        for operator, template in _TEMPORAL_MARKER_TEMPLATES
    ]
    context = f"(?:{axis})|(?:{values})"
    return temporal_semantics.TemporalLexicon.from_pairs(
        pairs, context=context, flags=re.IGNORECASE
    )


def _selector_cues(operator: str) -> tuple[str, ...]:
    """그 선택자 연산자를 뜻하는 표면어들(어휘의 단일 소유자는 위 표 하나다)."""

    return tuple(
        cue for cue, mapped in _OBSERVATION_SELECTOR_CUES.items() if mapped == operator
    )


@dataclass(frozen=True, slots=True)
class ObservationSelectorToken:
    """선택자 낱말 하나와 그 낱말이 **무엇을 수식하는지**의 판정.

    ``span`` 은 낱말('최근')이고 ``marker_span`` 은 머리까지 포함한 구간('최근 상태')이다.
    둘을 함께 들고 다니는 이유는 소비자가 다르기 때문이다 — 기간 결핍을 신고하는 쪽은 낱말
    좌표로 묻고, 조건을 세우는 쪽은 마커 구간으로 묻는다.
    """

    operator: str
    head_kind: str
    span: tuple[int, int]
    marker_span: tuple[int, int]

    @property
    def observation(self) -> bool:
        """이 낱말이 기간이 아니라 **관측 선택자**로 해석됐는가."""

        return self.head_kind == HEAD_ATTRIBUTE


def temporal_head_kind(text: str) -> str:
    """마커 구절의 머리 종류. 선언된 축·값 어휘에서 파생한다(손 목록 없음).

    낱말이 아니라 **머리**가 뜻을 정한다는 규칙(I1)이 코드로 있는 자리다. 속성 축이나 그
    축의 값이 머리면 관측 선택자이고, 그 밖(달력 단위·사건·기간)이면 기존 기간/칸 문법이
    그대로 소유한다.
    """

    if not isinstance(text, str) or not text.strip():
        return HEAD_OTHER
    body = text
    for cue in sorted(_OBSERVATION_SELECTOR_CUES, key=len, reverse=True):
        index = body.find(cue)
        if index >= 0:
            body = body[index + len(cue) :]
            break
    folded = body.casefold()
    heads = (*attribute_axis_terms(), *attribute_value_terms())
    if any(term.casefold().replace(" ", "") in folded.replace(" ", "") for term in heads):
        return HEAD_ATTRIBUTE
    return HEAD_OTHER


def selects_current_value(operator: str, text: str) -> bool:
    """이 마커가 속성 축의 **지금 값**을 고르는가 — 그러면 이력 요구가 아니다.

    소비자가 둘이다: 청구 계층은 이 조건의 소유권을 현재값 자산에 두고(짝을 이루지 못하면
    조건을 만들지 않는다), 요구 원장은 이력 의무를 기록하지 않는다. 둘이 각자 판단하면 한쪽은
    소유를 포기했는데 다른 쪽은 이력을 요구하는 상태가 되고, 그 문장은 아무도 방면할 수 없는
    의무로 막힌다(구현 중 실측).

    달력 시점을 말한 as_of('지난달 말 기준')는 여기 들지 않는다 — 그 조건은 과거 관측을
    골라야 하므로 이력 관측이 소유한다.
    """

    return operator == temporal_semantics.AS_OF and (
        temporal_head_kind(text) == HEAD_ATTRIBUTE
    )


def observation_selector_tokens(query: str) -> tuple[ObservationSelectorToken, ...]:
    """원문의 선택자 낱말 중 **관측 선택자로 해석된** 것들.

    기간 결핍을 신고하는 계층이 이 판정을 보고 신고를 **만들지 않는다**(I4). 신고한 뒤
    겹침으로 취소하는 구조가 아니라는 점이 중요하다 — 취소는 판정이 두 벌이라는 뜻이고,
    두 벌은 곧 서로 다른 답을 낸다.
    """

    try:
        markers = temporal_lexicon().detect(query)
    # 어휘를 못 만들면 아무것도 주장하지 않는다(추측 금지) — 소비자는 종전 판정을 그대로 쓴다.
    except Exception:
        return ()
    tokens: list[ObservationSelectorToken] = []
    for marker in markers:
        if marker.operator not in set(_OBSERVATION_SELECTOR_CUES.values()):
            continue
        cue_span = _cue_span(query, marker.start, marker.end)
        if cue_span is None:
            continue
        tokens.append(
            ObservationSelectorToken(
                operator=marker.operator,
                head_kind=temporal_head_kind(marker.text),
                span=cue_span,
                marker_span=(marker.start, marker.end),
            )
        )
    return tuple(tokens)


def _cue_span(query: str, start: int, end: int) -> tuple[int, int] | None:
    """마커 구간 안에서 선택자 낱말이 차지한 구간(없으면 ``None``)."""

    fragment = query[start:end]
    best: tuple[int, int] | None = None
    for cue in _OBSERVATION_SELECTOR_CUES:
        index = fragment.find(cue)
        if index < 0:
            continue
        candidate = (start + index, start + index + len(cue))
        if best is None or candidate[0] < best[0]:
            best = candidate
    return best


def transition_direction_cues() -> dict[str, str]:
    """방향 표면어 → 서열 방향(compact 키). 어휘의 단일 소유자는 위 표 하나다.

    원문에서 방향어를 **찾아야 하는** 소비자(전이 청구)가 손 목록을 만들면 마커 어휘와 갈라지고,
    그 순간 '승급'은 마커로 잡히는데 방향 검증에는 안 걸리는 조합이 생긴다.
    """

    return {
        re.sub(r"\s+", "", cue).casefold(): direction
        for cue, direction in _TRANSITION_DIRECTIONS.items()
    }


def subinterval_unit_cues() -> dict[str, str]:
    """하위 구간 표면어 → 칸 단위(compact 키). 어휘의 단일 소유자는 위 표 하나다.

    :func:`transition_direction_cues` 와 같은 규약이다 — 마커 정규식과 단위 해석이 같은
    선언에서 파생되므로 '매월'이 마커로만 잡히고 단위는 못 읽는 상태가 생기지 않는다.
    """

    return {
        re.sub(r"\s+", "", cue).casefold(): unit
        for cue, unit in _SUBINTERVAL_UNITS.items()
    }


def transition_direction(value: Any) -> str | None:
    """Directional transition cue -> ordered-domain direction.

    The cue vocabulary and the temporal detector share the same table above,
    so recognizing ``승급`` can never drift from its ``CHANGE_BETWEEN`` marker.
    """

    if not isinstance(value, str):
        return None
    compact = re.sub(r"\s+", "", value).casefold()
    return transition_direction_cues().get(compact)


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


# 앵커 공급자 등록(`install()`)은 2026-08-05 삭제됐다. 그 공급자를 소비하던 coverage 검증기
# (`semantic_coverage`)는 SemanticPlanV2 파이프라인 전용이었고, 파이프라인과 함께 폐기됐다.
# 이 모듈은 그 뒤로 코어에 등록할 것이 없다 — 도메인 지식은 `core_bindings()` 로만 나간다.


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
            "새 오디언스 표현 종류(audience_schema 대수 + event_ir 노드 + event_compiler lowering)",
            "새 값 종류(kind)와 그 정규화기(semantic_normalizers)",
            "새 실행 슬롯(targeting_ir SLOT_SHAPES + 빌더)",
            "새 표면 표현 패턴(targeting_domain 마커 템플릿 — 같은 연산자면 한 줄)",
        ],
    }


__all__ = [
    "DEFAULT_DOMAIN_PATH",
    "TargetingDomainError",
    "attribute_axis_identifying_terms",
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
