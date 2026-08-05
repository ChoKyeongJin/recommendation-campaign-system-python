"""Canonical alias 레지스트리 — 사전이 하는 일을 **하나**로 줄인 계층.

기존 사전이 커진 원인은 사전이 두 가지 일을 겸했기 때문이다.

    (1) 표면어 → canonical 의미  (예: ``구입`` → ``purchase``)
    (2) 문맥별 판정 규칙          (예: ``purchase_verb_colloquial_object_bound``)

(2)는 사전에 담을 수 없는 일이다. ``사서``/``사고``가 구매인지 아닌지는 낱말이 아니라
**문맥**이 정하는데, 문맥을 낱말 목록으로 흉내 내면 목록이 무한히 갈라진다(그래서
``purchase_verb`` 하나가 ``_completed_stem`` · ``_colloquial`` · ``_colloquial_object_bound``
로 번식했다). 이 모듈은 (1)만 소유한다. (2)는 정규화기와 extractor 의 구조가 담당한다.

그래서 여기 실리는 목록에는 규칙이 하나 있다: **활용형을 넣지 않는다.** ``샀``/``산``/``사서``
는 alias 가 아니라 :mod:`nl_event_ir.normalizer` 가 ``사다`` 로 되돌릴 대상이다.

브랜드·상품·카테고리 같은 **실제 데이터 값**도 여기 넣지 않는다(설계 원칙 4). 그 값은
:mod:`nl_event_ir.resolver` 가 외부 repository 에서 해석한다. 사전에 데이터를 넣기 시작하면
사전이 카탈로그의 낡은 사본이 된다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "AliasConflictError",
    "AliasFileError",
    "AliasRegistry",
    "AliasSchemaError",
    "AliasSection",
    "DEFAULT_ALIAS_PATH",
    "load_alias_registry",
]

logger = logging.getLogger(__name__)

DEFAULT_ALIAS_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "resources" / "canonical_aliases.json"
)

# 절이 아닌, 사람이 읽는 메타데이터 키. 이 목록에 없는 최상위 키는 오타로 간주한다.
_METADATA_KEYS: Final[frozenset[str]] = frozenset({"version", "description", "how_to_add"})


class AliasSection(StrEnum):
    """alias 파일의 최상위 절. 각 절은 하나의 canonical enum 에 대응한다."""

    EVENT = "event"
    ENTITY = "entity"
    COMPARISON = "comparison"
    LOGIC = "logic"
    POLARITY = "polarity"
    TIME_UNIT = "time_unit"
    METRIC = "metric"
    SORT = "sort"


class AliasError(Exception):
    """alias 레지스트리 계열 오류의 기반."""


class AliasFileError(AliasError):
    """파일이 없거나 JSON 으로 읽을 수 없다."""


class AliasSchemaError(AliasError):
    """파일은 읽혔지만 구조가 계약과 다르다."""


class AliasConflictError(AliasError):
    """같은 표면어가 한 절 안에서 서로 다른 canonical 값에 등록됐다."""


@dataclass(frozen=True, slots=True)
class AliasRegistry:
    """표면어 → canonical 값 조회기.

    생성 이후 변경되지 않는다. 전역 인스턴스를 두지 않고 애플리케이션 초기화 단계에서
    만들어 명시적으로 주입한다(CLAUDE.md 29·30).
    """

    sections: Mapping[str, Mapping[str, str]]
    source_path: Path | None = None

    # ── 조회 ────────────────────────────────────────────────────────────────────

    def lookup(self, section: AliasSection | str, surface: str) -> str | None:
        """표면어의 canonical 값. 등록되지 않았으면 ``None`` (추측하지 않는다)."""

        return self.sections.get(str(section), {}).get(surface)

    def canonical_values(self, section: AliasSection | str) -> tuple[str, ...]:
        return tuple(sorted(set(self.sections.get(str(section), {}).values())))

    def surfaces(self, section: AliasSection | str) -> tuple[str, ...]:
        """그 절의 모든 표면어를 **긴 것 우선**으로 정렬해 반환한다.

        정렬 기준이 중요하다. ``보다 많`` 과 ``많`` 이 같은 절에 있을 때 짧은 쪽이 교대의
        앞에 오면 긴 쪽은 영영 매치되지 않는다. 데이터 작성자가 정규식 교대 순서를 알아야
        하는 함정을 여기서 없앤다.
        """

        return tuple(
            sorted(self.sections.get(str(section), {}), key=lambda word: (-len(word), word))
        )

    def alternation(self, section: AliasSection | str) -> str:
        """그 절의 표면어로 만든 정규식 교대. 낱말은 전부 이스케이프된다 — 사전에 정규식을 쓸 수 없다."""

        surfaces = self.surfaces(section)
        if not surfaces:
            raise AliasSchemaError(f"alias section is empty: {section}")
        return "|".join(re.escape(surface) for surface in surfaces)

    def compiled(self, section: AliasSection | str, *, flags: int = 0) -> re.Pattern[str]:
        """절 전체를 감싼 캡처 그룹 정규식. 호출부에서 매번 컴파일하지 않도록 여기서 만든다."""

        return re.compile(f"({self.alternation(section)})", flags)

    # ── 생성 ────────────────────────────────────────────────────────────────────

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        source_path: Path | None = None,
    ) -> AliasRegistry:
        """검증된 매핑에서 레지스트리를 만든다. 스키마 위반은 여기서 예외가 된다."""

        # 오타 난 절 이름을 조용히 무시하면 그 절의 어휘가 통째로 사라진 채 파서가 '정상'
        # 동작한다. 알 수 없는 최상위 키는 여기서 막는다(메타데이터 키만 허용).
        known_keys = {section.value for section in AliasSection} | _METADATA_KEYS
        unknown = sorted(set(raw) - known_keys)
        if unknown:
            raise AliasSchemaError(
                f"unknown alias section(s): {', '.join(unknown)}. "
                f"Known sections: {', '.join(section.value for section in AliasSection)}"
            )

        sections: dict[str, Mapping[str, str]] = {}
        for section in AliasSection:
            raw_section = raw.get(section.value)
            if raw_section is None:
                # 절이 통째로 없는 것은 허용한다(부분 사전). 다만 조용히 비우지는 않고 남긴다.
                logger.debug("alias section missing: %s", section.value)
                sections[section.value] = MappingProxyType({})
                continue
            sections[section.value] = _build_section(section.value, raw_section)
        return cls(sections=MappingProxyType(sections), source_path=source_path)


def _build_section(section_name: str, raw_section: Any) -> Mapping[str, str]:
    """``{canonical: [surface, ...]}`` 을 ``{surface: canonical}`` 로 뒤집으며 검증한다."""

    if not isinstance(raw_section, dict):
        raise AliasSchemaError(
            f"alias section '{section_name}' must be an object of "
            f"{{canonical: [surface, ...]}}, got {type(raw_section).__name__}"
        )

    surface_to_canonical: dict[str, str] = {}
    for canonical, surfaces in raw_section.items():
        if not isinstance(canonical, str) or not canonical:
            raise AliasSchemaError(
                f"alias section '{section_name}' has a non-string canonical key: {canonical!r}"
            )
        if not isinstance(surfaces, list):
            raise AliasSchemaError(
                f"alias '{section_name}.{canonical}' must map to a list of surfaces, "
                f"got {type(surfaces).__name__}"
            )
        seen_in_this_value: set[str] = set()
        for surface in surfaces:
            if not isinstance(surface, str) or not surface.strip():
                raise AliasSchemaError(
                    f"alias '{section_name}.{canonical}' contains an empty or non-string "
                    f"surface: {surface!r}"
                )
            normalized_surface = surface.strip()
            if normalized_surface in seen_in_this_value:
                # 같은 값 안의 중복은 데이터 실수다. 조용히 삼키면 사전이 계속 자란다.
                raise AliasSchemaError(
                    f"duplicate surface '{normalized_surface}' in alias "
                    f"'{section_name}.{canonical}'"
                )
            seen_in_this_value.add(normalized_surface)

            previous = surface_to_canonical.get(normalized_surface)
            if previous is not None and previous != canonical:
                raise AliasConflictError(
                    f"surface '{normalized_surface}' is registered to both "
                    f"'{section_name}.{previous}' and '{section_name}.{canonical}'. "
                    f"A surface may mean only one canonical value inside a section."
                )
            surface_to_canonical[normalized_surface] = canonical

    return MappingProxyType(surface_to_canonical)


def load_alias_registry(path: Path | str | None = None) -> AliasRegistry:
    """alias 파일을 읽어 레지스트리를 만든다.

    파일 문제를 조용히 넘기지 않는다 — 사전이 비어 있는 채로 도는 파서는 모든 문장을
    fallback 으로 보내면서도 '정상'처럼 보이기 때문에 가장 찾기 어려운 고장이 된다.
    """

    resolved = Path(path) if path is not None else DEFAULT_ALIAS_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise AliasFileError(f"cannot read alias file: {resolved}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AliasFileError(f"alias file is not valid JSON: {resolved} ({exc})") from exc

    if not isinstance(raw, dict):
        raise AliasSchemaError(f"alias file root must be an object: {resolved}")

    registry = AliasRegistry.from_mapping(raw, source_path=resolved)
    logger.debug(
        "alias registry loaded",
        extra={
            "path": str(resolved),
            "sections": len(registry.sections),
            "surfaces": sum(len(values) for values in registry.sections.values()),
        },
    )
    return registry


def find_alias_matches(
    text: str,
    registry: AliasRegistry,
    section: AliasSection | str,
) -> tuple[tuple[int, int, str, str], ...]:
    """``text`` 안의 alias 출현을 ``(start, end, surface, canonical)`` 로 반환한다.

    겹치는 매치는 **긴 쪽만** 남긴다. ``회원가입`` 이 ``가입`` 을 포함하므로, 짧은 쪽까지
    남기면 같은 자리가 두 번 읽히고 composer 가 중복 조건을 만든다.
    """

    pattern = registry.compiled(section)
    matches: list[tuple[int, int, str, str]] = []
    for match in pattern.finditer(text):
        surface = match.group(1)
        canonical = registry.lookup(section, surface)
        if canonical is None:  # pragma: no cover - 정규식이 사전에서 나왔으므로 도달 불가
            continue
        matches.append((match.start(1), match.end(1), surface, canonical))
    return _drop_contained_matches(matches)


def _drop_contained_matches(
    matches: Iterable[tuple[int, int, str, str]],
) -> tuple[tuple[int, int, str, str], ...]:
    ordered = sorted(matches, key=lambda item: (item[0], -(item[1] - item[0])))
    kept: list[tuple[int, int, str, str]] = []
    for candidate in ordered:
        if any(
            existing[0] <= candidate[0] and candidate[1] <= existing[1] for existing in kept
        ):
            continue
        kept.append(candidate)
    return tuple(sorted(kept, key=lambda item: item[0]))
