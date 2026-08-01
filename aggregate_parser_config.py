"""집계 조건 파서 설정의 단일 소스 — 표면어·정책 수치는 JSON, 알고리즘은 코드.

이 모듈이 지키는 계약은 셋이다.

1. **표면어를 코드에 두지 않는다.** 단위 토큰·미지원 속성 별칭·접속 경계·안내 문구는 전부
   ``docs/data/runtime/language/aggregate_parser_rules.json`` 과
   ``docs/data/runtime/language/clarification_messages.ko.json``
   이 소유한다. 새 표현은 JSON 한 줄이지 파이썬 분기가 아니다.
2. **조용한 폴백을 하지 않는다.** 설정이 스키마를 어기거나 참조가 끊기면 코드 기본값으로 계속
   달리지 않고 :class:`AggregateParserConfigError` 로 즉시 실패한다 — 운영에서 설정과 동작이
   갈리는 것이 조용한 오답의 출발점이기 때문이다.
3. **JSON 을 실행하지 않는다.** 설정값은 정규식 소스로 그대로 쓰이지 않는다. 표면어는 항상
   :func:`re.escape` 를 거쳐 교체 문법으로만 조립된다(``eval``/``exec`` 없음).

로드 결과는 불변 객체로 캐시하고 락으로 보호한다 — 파싱 경로가 요청마다 파일을 읽지 않게 하되,
여러 스레드가 처음 로드를 동시에 밟아도 한 번만 읽는다. 설정 교체는 재시작이 기준이며,
테스트는 :func:`use_rules` 로 임시 설정을 주입한다.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

import jsonschema

SUPPORTED_VERSION = 1

_DATA_DIR = Path(__file__).resolve().parent / "docs" / "data"
_LANGUAGE_DIR = _DATA_DIR / "runtime" / "language"
_SCHEMA_DIR = _DATA_DIR / "schemas"
DEFAULT_RULES_PATH = Path(
    os.getenv("AGGREGATE_PARSER_RULES", _LANGUAGE_DIR / "aggregate_parser_rules.json")
)
DEFAULT_RULES_SCHEMA_PATH = _SCHEMA_DIR / "aggregate_parser_rules.schema.json"
DEFAULT_MESSAGES_PATH = Path(
    os.getenv("CLARIFICATION_MESSAGES", _LANGUAGE_DIR / "clarification_messages.ko.json")
)
DEFAULT_MESSAGES_SCHEMA_PATH = _SCHEMA_DIR / "clarification_messages.schema.json"


class AggregateParserConfigError(RuntimeError):
    """설정 파일이 계약을 어겼다. 조용한 기본값 폴백 대신 시작 단계에서 터뜨린다."""


# ── 불변 값 객체 ────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpanBinding:
    """숫자 스팬에 단위·속성을 붙일 때의 근접성 정책(전부 설정 소유)."""

    max_unit_whitespace: int
    attribute_search_left_tokens: int
    attribute_search_right_tokens: int
    stop_at_conjunction: bool
    stop_at_clause_boundary: bool
    conjunction_tokens: tuple[str, ...]
    disjunction_tokens: tuple[str, ...]
    boundary_punctuation: frozenset[str]
    generic_attribute_particles: tuple[str, ...]
    generic_attribute_min_length: int


@dataclass(frozen=True)
class UnitTokenSpec:
    """단위 토큰 선언. surface 가 표면형, kind 가 의미 종류, priority 는 동일 길이 동점의 결선."""

    surface: str
    kind: str
    canonical: str
    priority: int


@dataclass(frozen=True)
class ContextAlias:
    """문맥이 있어야 성립하는 별칭('매출'은 지점/매장 문맥에서만 지점매출이다)."""

    surface: str
    context_terms: tuple[str, ...]


@dataclass(frozen=True)
class UnsupportedAttributeHint:
    key: str
    aliases: tuple[str, ...]
    context_aliases: tuple[ContextAlias, ...]
    message_key: str


@dataclass(frozen=True)
class SupportedAttributeSource:
    """지원 속성 별칭을 읽어올 member_target_filters.json 안의 위치."""

    section: str
    key_field: str | None
    alias_fields: tuple[str, ...]


@dataclass(frozen=True)
class SemanticDomain:
    presence_node_kinds: frozenset[str]
    absence_node_kinds: frozenset[str]
    event_dimensions: tuple[str, ...]


@dataclass(frozen=True)
class PrefixOperator:
    surface: str
    operator: str


@dataclass(frozen=True)
class ClarificationMessages:
    """사용자 노출 문구. 내부 사유 코드는 여기 들어오지 않는다."""

    version: int
    _messages: Mapping[str, str]

    def has(self, key: str) -> bool:
        return key in self._messages

    def render(self, key: str, **fields: str) -> str:
        """메시지 키를 채워 렌더한다. 없는 키는 조용히 넘어가지 않고 즉시 실패한다."""
        try:
            template = self._messages[key]
        except KeyError as exc:  # pragma: no cover - 로더가 미리 막지만 방어적으로 남긴다
            raise AggregateParserConfigError(f"등록되지 않은 안내 문구 키: {key}") from exc
        try:
            return template.format(**fields)
        except KeyError as exc:
            raise AggregateParserConfigError(f"안내 문구 '{key}' 의 치환 필드가 없다: {exc}") from exc

    def keys(self) -> frozenset[str]:
        return frozenset(self._messages)


@dataclass(frozen=True)
class AggregateParserRules:
    """검증을 마친 파서 설정 한 벌. 파싱 경로는 이 객체만 본다."""

    version: int
    span_binding: SpanBinding
    number_multipliers: tuple[tuple[str, float], ...]
    prefix_operators: tuple[PrefixOperator, ...]
    unit_kinds: frozenset[str]
    aggregate_threshold_unit_kinds: frozenset[str]
    unit_tokens: tuple[UnitTokenSpec, ...]
    supported_attribute_sources: tuple[SupportedAttributeSource, ...]
    unsupported_attribute_hints: tuple[UnsupportedAttributeHint, ...]
    generic_unsupported_message_key: str
    semantic_domains: Mapping[str, SemanticDomain]
    messages: ClarificationMessages

    def unit_alternation(self) -> str:
        """단위 표면어의 교체 문법. 표면어는 반드시 escape 한다 — 설정을 정규식으로 실행하지 않는다.

        긴 표면형이 먼저 오도록 정렬해, 교체 문법 자체가 longest-match 를 흉내내게 한다
        ('개월' 이 '개' 보다 앞) — 최종 선택은 :mod:`aggregate_spans` 가 스팬으로 다시 정한다."""
        surfaces = sorted({token.surface for token in self.unit_tokens}, key=lambda s: (-len(s), s))
        return "|".join(re.escape(surface) for surface in surfaces)

    def unit_spec(self, surface: str) -> UnitTokenSpec | None:
        return _unit_index(self).get(surface)

    def is_aggregate_threshold_unit(self, kind: str) -> bool:
        return kind in self.aggregate_threshold_unit_kinds


# ── 로딩·검증 ──────────────────────────────────────────────────────────────────────────
def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise AggregateParserConfigError(f"{label} 설정 파일을 읽을 수 없다: {path} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise AggregateParserConfigError(f"{label} 설정 파일이 올바른 JSON 이 아니다: {path} ({exc})") from exc


def _validate_schema(payload: Any, schema_path: Path, label: str) -> None:
    schema = _read_json(schema_path, f"{label} 스키마")
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "(root)"
        raise AggregateParserConfigError(f"{label} 설정이 스키마를 위반했다 [{location}]: {exc.message}") from exc
    except jsonschema.SchemaError as exc:  # pragma: no cover - 스키마 자체가 깨진 경우
        raise AggregateParserConfigError(f"{label} 스키마 자체가 잘못됐다: {exc.message}") from exc


def _require_unique(values: list[str], what: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        (duplicates if value in seen else seen).add(value)
    if duplicates:
        raise AggregateParserConfigError(f"{what} 가 중복 선언됐다: {', '.join(sorted(duplicates))}")


def load_messages(
    path: Path = DEFAULT_MESSAGES_PATH, schema_path: Path = DEFAULT_MESSAGES_SCHEMA_PATH
) -> ClarificationMessages:
    payload = _read_json(path, "안내 문구")
    _validate_schema(payload, schema_path, "안내 문구")
    version = payload["version"]
    if version != SUPPORTED_VERSION:
        raise AggregateParserConfigError(
            f"안내 문구 설정 버전이 맞지 않는다: 파일 {version}, 지원 {SUPPORTED_VERSION}"
        )
    return ClarificationMessages(version=version, _messages=MappingProxyType(dict(payload["messages"])))


def load_rules(
    path: Path = DEFAULT_RULES_PATH,
    schema_path: Path = DEFAULT_RULES_SCHEMA_PATH,
    messages: ClarificationMessages | None = None,
) -> AggregateParserRules:
    """설정 한 벌을 읽어 검증한 불변 객체로 만든다. 어떤 위반도 조용히 넘어가지 않는다."""
    payload = _read_json(path, "집계 파서 규칙")
    _validate_schema(payload, schema_path, "집계 파서 규칙")

    version = payload["version"]
    if version != SUPPORTED_VERSION:
        raise AggregateParserConfigError(
            f"집계 파서 규칙 버전이 맞지 않는다: 파일 {version}, 지원 {SUPPORTED_VERSION}"
        )

    resolved_messages = messages if messages is not None else load_messages()

    raw_binding = payload["span_binding"]
    binding = SpanBinding(
        max_unit_whitespace=raw_binding["max_unit_whitespace"],
        attribute_search_left_tokens=raw_binding["attribute_search_left_tokens"],
        attribute_search_right_tokens=raw_binding["attribute_search_right_tokens"],
        stop_at_conjunction=raw_binding["stop_at_conjunction"],
        stop_at_clause_boundary=raw_binding["stop_at_clause_boundary"],
        conjunction_tokens=tuple(raw_binding["conjunction_tokens"]),
        disjunction_tokens=tuple(raw_binding["disjunction_tokens"]),
        boundary_punctuation=frozenset(raw_binding["boundary_punctuation"]),
        generic_attribute_particles=tuple(raw_binding["generic_attribute_particles"]),
        generic_attribute_min_length=raw_binding["generic_attribute_min_length"],
    )
    # 스키마가 minimum 으로 막지만, 음수·비정상 값은 파싱을 조용히 무력화하므로 여기서 한 번 더 닫는다.
    for field_name in ("max_unit_whitespace", "attribute_search_left_tokens", "attribute_search_right_tokens"):
        if getattr(binding, field_name) < 0:
            raise AggregateParserConfigError(f"span_binding.{field_name} 는 음수일 수 없다")

    unit_kinds = frozenset(payload["unit_kinds"])
    _require_unique(list(payload["unit_kinds"]), "단위 종류(unit_kinds)")

    unit_tokens = tuple(
        UnitTokenSpec(
            surface=entry["surface"], kind=entry["kind"],
            canonical=entry["canonical"], priority=entry["priority"],
        )
        for entry in payload["unit_tokens"]
    )
    _require_unique([token.surface for token in unit_tokens], "단위 토큰 표면어")
    unknown_kinds = sorted({token.kind for token in unit_tokens} - unit_kinds)
    if unknown_kinds:
        raise AggregateParserConfigError(f"선언되지 않은 단위 종류를 쓰는 토큰이 있다: {', '.join(unknown_kinds)}")

    threshold_kinds = frozenset(payload["aggregate_threshold_unit_kinds"])
    unknown_threshold = sorted(threshold_kinds - unit_kinds)
    if unknown_threshold:
        raise AggregateParserConfigError(
            f"aggregate_threshold_unit_kinds 에 선언되지 않은 단위 종류가 있다: {', '.join(unknown_threshold)}"
        )

    hints = tuple(
        UnsupportedAttributeHint(
            key=entry["key"],
            aliases=tuple(entry["aliases"]),
            context_aliases=tuple(
                ContextAlias(surface=item["surface"], context_terms=tuple(item["context_terms"]))
                for item in entry["context_aliases"]
            ),
            message_key=entry["message_key"],
        )
        for entry in payload["unsupported_attribute_hints"]
    )
    _require_unique([hint.key for hint in hints], "미지원 속성 키")
    _require_unique(
        [alias for hint in hints for alias in hint.aliases]
        + [item.surface for hint in hints for item in hint.context_aliases],
        "미지원 속성 별칭",
    )
    generic_key = payload["generic_unsupported_message_key"]
    referenced_keys = {hint.message_key for hint in hints} | {generic_key}
    missing_keys = sorted(referenced_keys - resolved_messages.keys())
    if missing_keys:
        raise AggregateParserConfigError(f"안내 문구에 없는 message_key 를 참조한다: {', '.join(missing_keys)}")

    domains = {
        name: SemanticDomain(
            presence_node_kinds=frozenset(spec["presence_node_kinds"]),
            absence_node_kinds=frozenset(spec["absence_node_kinds"]),
            event_dimensions=tuple(spec["event_dimensions"]),
        )
        for name, spec in payload["semantic_domains"].items()
    }
    for name, domain in domains.items():
        overlap = sorted(domain.presence_node_kinds & domain.absence_node_kinds)
        if overlap:
            raise AggregateParserConfigError(
                f"의미 도메인 '{name}' 의 존재/부재 노드 종류가 겹친다: {', '.join(overlap)}"
            )

    multipliers = tuple(
        sorted(
            ((surface, float(value)) for surface, value in payload["number_multipliers"].items()),
            key=lambda item: (-len(item[0]), -item[1]),
        )
    )

    return AggregateParserRules(
        version=version,
        span_binding=binding,
        number_multipliers=multipliers,
        prefix_operators=tuple(
            PrefixOperator(surface=entry["surface"], operator=entry["operator"])
            for entry in payload["prefix_operators"]
        ),
        unit_kinds=unit_kinds,
        aggregate_threshold_unit_kinds=threshold_kinds,
        unit_tokens=unit_tokens,
        supported_attribute_sources=tuple(
            SupportedAttributeSource(
                section=entry["section"],
                key_field=entry["key_field"],
                alias_fields=tuple(entry["alias_fields"]),
            )
            for entry in payload["supported_attribute_sources"]
        ),
        unsupported_attribute_hints=hints,
        generic_unsupported_message_key=generic_key,
        semantic_domains=MappingProxyType(domains),
        messages=resolved_messages,
    )


# ── 캐시(불변 + 스레드 안전) ─────────────────────────────────────────────────────────────
_LOCK = threading.RLock()
_CACHED: AggregateParserRules | None = None
_UNIT_INDEX: dict[int, Mapping[str, UnitTokenSpec]] = {}


def _unit_index(rules: AggregateParserRules) -> Mapping[str, UnitTokenSpec]:
    key = id(rules)
    cached = _UNIT_INDEX.get(key)
    if cached is None:
        cached = MappingProxyType({token.surface: token for token in rules.unit_tokens})
        _UNIT_INDEX[key] = cached
    return cached


def rules() -> AggregateParserRules:
    """검증된 설정을 돌려준다(프로세스 수명 동안 1회 로드). 설정 오류는 여기서 터진다."""
    global _CACHED
    cached = _CACHED
    if cached is not None:
        return cached
    with _LOCK:
        if _CACHED is None:
            _CACHED = load_rules()
        return _CACHED


def messages() -> ClarificationMessages:
    return rules().messages


# ── 표면어 → 정규식 조각 ────────────────────────────────────────────────────────────────
# 조립은 항상 여기를 거친다. 표면어를 정규식 소스로 그대로 흘리지 않고 re.escape 로만 끼워 넣는
# 단일 지점이라, '설정을 실행하지 않는다'는 계약을 한 곳에서 지킬 수 있다.
def unit_alternation(rules: AggregateParserRules) -> str:
    return rules.unit_alternation()


def magnitude_alternation(rules: AggregateParserRules) -> str:
    """배수 표현의 교체 문법(긴 표면형 우선) — '3천만'이 '3천'으로 잘리지 않게."""
    surfaces = sorted((surface for surface, _ in rules.number_multipliers), key=lambda s: (-len(s), s))
    return "|".join(re.escape(surface) for surface in surfaces)


def prefix_operator_alternation(rules: AggregateParserRules) -> str:
    """숫자 앞 비교어('최소 10억')의 교체 문법. 선언이 없으면 절대 매치되지 않는 조각을 돌려준다."""
    return "|".join(re.escape(op.surface) for op in rules.prefix_operators) or r"(?!)"


@contextmanager
def use_rules(replacement: AggregateParserRules) -> Iterator[AggregateParserRules]:
    """테스트가 임시 설정을 주입하는 유일한 통로(프로덕션 경로는 파일만 읽는다)."""
    global _CACHED
    with _LOCK:
        previous = _CACHED
        _CACHED = replacement
    try:
        yield replacement
    finally:
        with _LOCK:
            _CACHED = previous


def reset_cache() -> None:
    """설정 파일을 갈아끼운 테스트가 다음 로드를 강제할 때 쓴다."""
    global _CACHED
    with _LOCK:
        _CACHED = None
        _UNIT_INDEX.clear()
