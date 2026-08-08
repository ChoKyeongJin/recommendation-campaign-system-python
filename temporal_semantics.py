"""시간 한정어의 **범용 연산자** 계층 — 표현별 예외처리를 연산자 하나로 접는다.

문제: '기준'·'내내'·'직전'·'한 번이라도'는 서로 다른 낱말이지만 **서로 다른 시간 연산**이 아니라
같은 축의 값들이다. 낱말마다 분기를 만들면 새 표현이 올 때마다 분기가 하나 늘고, 그 분기가
어느 데이터 그레인을 요구하는지는 코드 곳곳에 흩어진다.

여기서는 시간 한정어를 **닫힌 연산자 집합**으로 정규화한다:

    AS_OF                      지정 시점의 값
    IMMEDIATELY_PRECEDING      직전 관측 시점의 값
    WITHIN_INTERVAL            구간 안 어느 시점에서든
    THROUGHOUT_INTERVAL        구간 내내(모든 시점에서)
    AT_LEAST_ONCE_IN_INTERVAL  구간 중 최소 1회
    NEVER_IN_INTERVAL          구간 중 0회
    EVERY_SUBINTERVAL          구간의 모든 하위 구간에서
    UNCHANGED_THROUGHOUT       구간 내내 값이 바뀌지 않음
    CHANGE_BETWEEN             두 관측 시점 사이의 값 전이
    CHANGE_COUNT               구간 내 값 변경 횟수
    CONSECUTIVE_SUBINTERVALS   구간의 하위 칸이 끊기지 않고 이어서 성립

이 모듈이 **모르는 것**(도메인 계층 소유):
  - 한국어 표면형('기준'·'내내')과 그 정규식. 도메인이 `TemporalLexicon` 으로 주입한다.
  - 어떤 속성 축에 이 연산자가 붙는지.
  - 연산자가 어떤 실행 SQL 로 컴파일되는지.

이 모듈이 **아는 것**:
  - 연산자 식별자와 그 인자 요구(anchor/interval/count/subinterval_unit).
  - 인자가 빠졌을 때의 실패 분류(MISSING_ARGUMENT — '미지원'이 아니다).
  - 다중 관측 시점이 필요한 연산자인지(= 데이터 그레인 깊이 요구의 근거).

순수 모듈 규약: graph_rag 를 import 하지 않는다. 도메인 낱말을 하드코딩하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Pattern, Sequence

# ── 연산자 식별자(닫힌 집합) ─────────────────────────────────────────────────────
AS_OF = "AS_OF"
IMMEDIATELY_PRECEDING = "IMMEDIATELY_PRECEDING"
WITHIN_INTERVAL = "WITHIN_INTERVAL"
THROUGHOUT_INTERVAL = "THROUGHOUT_INTERVAL"
AT_LEAST_ONCE_IN_INTERVAL = "AT_LEAST_ONCE_IN_INTERVAL"
NEVER_IN_INTERVAL = "NEVER_IN_INTERVAL"
EVERY_SUBINTERVAL = "EVERY_SUBINTERVAL"
UNCHANGED_THROUGHOUT = "UNCHANGED_THROUGHOUT"
CHANGE_BETWEEN = "CHANGE_BETWEEN"
CHANGE_COUNT = "CHANGE_COUNT"
CONSECUTIVE_SUBINTERVALS = "CONSECUTIVE_SUBINTERVALS"

# 인자 이름(닫힌 집합) — 노드 필드 이름이 아니라 시간 연산의 인자다.
ARG_ANCHOR = "anchor"                    # 시점 하나
ARG_INTERVAL = "interval"                # 구간
ARG_COUNT = "count"                      # 횟수
ARG_SUBINTERVAL_UNIT = "subinterval_unit"  # 하위 구간의 단위(월/주/…)

ARGUMENTS: frozenset[str] = frozenset({
    ARG_ANCHOR, ARG_INTERVAL, ARG_COUNT, ARG_SUBINTERVAL_UNIT,
})


class TemporalSemanticsError(ValueError):
    """시간 연산자 계약 위반(알 수 없는 연산자·인자 등)."""


@dataclass(frozen=True)
class TemporalOperatorSpec:
    """연산자 하나의 선언. 인자 요구가 여기 한 곳에 있어야 결핍 판정이 갈라지지 않는다."""

    id: str
    requires: frozenset[str] = frozenset()
    accepts: frozenset[str] = frozenset()
    # 서로 다른 관측 시점을 여럿 봐야 하는가(데이터 그레인 깊이 요구의 근거).
    multi_point: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        unknown = (self.requires | self.accepts) - ARGUMENTS
        if unknown:
            raise TemporalSemanticsError(
                f"{self.id}: 알 수 없는 시간 연산 인자 {sorted(unknown)}"
            )

    @property
    def allowed(self) -> frozenset[str]:
        return self.requires | self.accepts


_SPECS: tuple[TemporalOperatorSpec, ...] = (
    TemporalOperatorSpec(
        AS_OF,
        accepts=frozenset({ARG_ANCHOR}),
        description="지정 시점의 값. 시점이 없으면 최신 관측 시점을 뜻한다.",
    ),
    TemporalOperatorSpec(
        IMMEDIATELY_PRECEDING,
        accepts=frozenset({ARG_ANCHOR}),
        description="직전 관측 시점의 값.",
    ),
    TemporalOperatorSpec(
        WITHIN_INTERVAL,
        requires=frozenset({ARG_INTERVAL}),
        description="구간 안 어느 시점에서든 성립.",
    ),
    TemporalOperatorSpec(
        THROUGHOUT_INTERVAL,
        requires=frozenset({ARG_INTERVAL}),
        multi_point=True,
        description="구간의 모든 관측 시점에서 성립.",
    ),
    TemporalOperatorSpec(
        AT_LEAST_ONCE_IN_INTERVAL,
        requires=frozenset({ARG_INTERVAL}),
        multi_point=True,
        description="구간 중 최소 한 관측 시점에서 성립.",
    ),
    TemporalOperatorSpec(
        NEVER_IN_INTERVAL,
        requires=frozenset({ARG_INTERVAL}),
        multi_point=True,
        description="구간의 어떤 관측 시점에서도 성립하지 않음.",
    ),
    TemporalOperatorSpec(
        EVERY_SUBINTERVAL,
        requires=frozenset({ARG_INTERVAL, ARG_SUBINTERVAL_UNIT}),
        multi_point=True,
        description="구간을 하위 단위로 나눈 모든 칸에서 관측됨.",
    ),
    TemporalOperatorSpec(
        UNCHANGED_THROUGHOUT,
        requires=frozenset({ARG_INTERVAL}),
        multi_point=True,
        description="구간 내내 값이 한 번도 바뀌지 않음.",
    ),
    TemporalOperatorSpec(
        CHANGE_BETWEEN,
        accepts=frozenset({ARG_ANCHOR, ARG_INTERVAL}),
        description="두 관측 시점 사이의 값 전이. 시점이 없으면 직전→현재.",
    ),
    TemporalOperatorSpec(
        CHANGE_COUNT,
        requires=frozenset({ARG_INTERVAL, ARG_COUNT}),
        multi_point=True,
        description="구간 안에서 값이 바뀐 횟수.",
    ),
    TemporalOperatorSpec(
        CONSECUTIVE_SUBINTERVALS,
        requires=frozenset({ARG_INTERVAL, ARG_SUBINTERVAL_UNIT}),
        multi_point=True,
        description="구간의 하위 칸들이 **끊기지 않고** 이어서 성립함.",
    ),
)

OPERATOR_SPECS: dict[str, TemporalOperatorSpec] = {spec.id: spec for spec in _SPECS}
OPERATORS: frozenset[str] = frozenset(OPERATOR_SPECS)
MULTI_POINT_OPERATORS: frozenset[str] = frozenset(
    spec.id for spec in _SPECS if spec.multi_point
)


def operator_documentation() -> dict[str, dict[str, Any]]:
    """연산자 표(문서·테스트가 읽는 파생 표 — 손 목록을 두지 않는다)."""
    return {
        spec.id: {
            "requires": sorted(spec.requires),
            "accepts": sorted(spec.accepts),
            "multi_point": spec.multi_point,
            "description": spec.description,
        }
        for spec in _SPECS
    }


# ── 한정어 값 ────────────────────────────────────────────────────────────────────
def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


@dataclass(frozen=True)
class TemporalQualifier:
    """정규화된 시간 한정어. 연산자 + 인자만 갖는다(속성 값은 술어 소유)."""

    operator: str
    anchor: Any = None
    interval: Any = None
    count: Any = None
    subinterval_unit: str | None = None
    # 이 한정어의 근거가 된 원문 구절(감사·요구사항 원장용).
    source_span: str = ""

    def __post_init__(self) -> None:
        if self.operator not in OPERATOR_SPECS:
            raise TemporalSemanticsError(f"알 수 없는 시간 연산자: {self.operator!r}")

    @property
    def spec(self) -> TemporalOperatorSpec:
        return OPERATOR_SPECS[self.operator]

    @property
    def multi_point(self) -> bool:
        return self.spec.multi_point

    def argument(self, name: str) -> Any:
        return {
            ARG_ANCHOR: self.anchor,
            ARG_INTERVAL: self.interval,
            ARG_COUNT: self.count,
            ARG_SUBINTERVAL_UNIT: self.subinterval_unit,
        }.get(name)

    def missing_arguments(self) -> tuple[str, ...]:
        """빠진 필수 인자 — 이것은 결핍(MISSING_ARGUMENT)이지 미지원이 아니다."""
        return tuple(
            sorted(name for name in self.spec.requires if _empty(self.argument(name)))
        )

    def surplus_arguments(self) -> tuple[str, ...]:
        """연산자가 받지 않는 인자가 채워졌는가(오배선 감지)."""
        return tuple(
            sorted(
                name
                for name in ARGUMENTS - self.spec.allowed
                if not _empty(self.argument(name))
            )
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"operator": self.operator}
        for name in sorted(ARGUMENTS):
            value = self.argument(name)
            if not _empty(value):
                payload[name] = value
        if self.source_span:
            payload["source_span"] = self.source_span
        missing = self.missing_arguments()
        if missing:
            payload["missing_arguments"] = list(missing)
        return payload


def normalize(raw: Any, *, aliases: Mapping[str, str] | None = None) -> TemporalQualifier | None:
    """연산자 id / 도메인 별칭 / dict → TemporalQualifier.

    `aliases` 는 도메인이 주입하는 '도메인 관계명 → 범용 연산자' 표다. 도메인 낱말을 이
    모듈이 알 필요가 없는 지점이 여기다.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, TemporalQualifier):
        return raw
    alias_map = {str(key): str(value) for key, value in (aliases or {}).items()}
    if isinstance(raw, str):
        operator = _resolve_operator(raw, alias_map)
        return TemporalQualifier(operator=operator) if operator else None
    if not isinstance(raw, Mapping):
        raise TemporalSemanticsError(f"시간 한정어 값을 알 수 없다: {raw!r}")
    operator = _resolve_operator(str(raw.get("operator") or raw.get("relation") or ""), alias_map)
    if operator is None:
        return None
    unknown = {
        key for key in raw
        if key not in ARGUMENTS and key not in {"operator", "relation", "source_span"}
    }
    if unknown:
        raise TemporalSemanticsError(
            f"{operator}: 알 수 없는 시간 연산 인자 {sorted(unknown)}"
        )
    return TemporalQualifier(
        operator=operator,
        anchor=raw.get(ARG_ANCHOR),
        interval=raw.get(ARG_INTERVAL),
        count=raw.get(ARG_COUNT),
        subinterval_unit=(
            str(raw[ARG_SUBINTERVAL_UNIT]) if raw.get(ARG_SUBINTERVAL_UNIT) else None
        ),
        source_span=str(raw.get("source_span") or ""),
    )


def _resolve_operator(surface: str, aliases: Mapping[str, str]) -> str | None:
    text = surface.strip()
    if not text:
        return None
    if text in OPERATOR_SPECS:
        return text
    upper = text.upper()
    if upper in OPERATOR_SPECS:
        return upper
    mapped = aliases.get(text) or aliases.get(text.casefold())
    if mapped in OPERATOR_SPECS:
        return mapped
    if mapped:
        raise TemporalSemanticsError(
            f"별칭 {text!r} 이 알 수 없는 시간 연산자 {mapped!r} 로 매핑됐다"
        )
    return None


# ── 표면 감지(어휘는 도메인이 주입) ──────────────────────────────────────────────
@dataclass(frozen=True)
class TemporalMarker:
    """원문에서 시간 한정어의 존재를 알리는 구간."""

    operator: str
    start: int
    end: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass(frozen=True)
class TemporalLexicon:
    """(연산자, 표면 패턴) 목록. **도메인이 만든다** — 이 모듈은 낱말을 모른다.

    패턴은 컴파일된 정규식이다. 표면형 정규식은 파싱 알고리즘이라 도메인 소스가 소유하고,
    그 안의 값 어휘(등급/상태 값 등)는 카탈로그에서 파생해 끼운다.
    """

    entries: tuple[tuple[str, Pattern[str]], ...] = ()
    # 이 문맥이 아니면 마커를 인정하지 않는 게이트(도메인 소유, 선택).
    context: Pattern[str] | None = None

    def __post_init__(self) -> None:
        unknown = sorted({op for op, _ in self.entries if op not in OPERATOR_SPECS})
        if unknown:
            raise TemporalSemanticsError(f"어휘가 알 수 없는 시간 연산자를 쓴다: {unknown}")

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[str, str | Pattern[str]]],
        *,
        context: str | Pattern[str] | None = None,
        flags: int = 0,
    ) -> "TemporalLexicon":
        entries = tuple(
            (operator, pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, flags))
            for operator, pattern in pairs
        )
        compiled_context = (
            context if isinstance(context, re.Pattern)
            else re.compile(context, flags) if context else None
        )
        return cls(entries=entries, context=compiled_context)

    @property
    def operators(self) -> frozenset[str]:
        return frozenset(operator for operator, _ in self.entries)

    def detect(
        self, query: str, *, clause_bounds: Any = None
    ) -> list[TemporalMarker]:
        """원문의 시간 한정어 마커를 찾는다(가장 긴 매치 우선, 겹치면 하나만).

        `clause_bounds(query, position) -> (start, end)` 가 주어지면 문맥 게이트를 그 절
        범위로 평가한다(절 밖 낱말이 마커를 살리는 과발화 방지).
        """
        if not isinstance(query, str) or not query.strip():
            return []
        found: list[TemporalMarker] = []
        for operator, pattern in self.entries:
            for match in pattern.finditer(query):
                if self.context is not None:
                    scope = query
                    if callable(clause_bounds):
                        start, end = clause_bounds(query, match.start())
                        scope = query[start:end]
                    if self.context.search(scope) is None:
                        continue
                found.append(
                    TemporalMarker(
                        operator=operator,
                        start=match.start(),
                        end=match.end(),
                        text=match.group(0),
                    )
                )
        return _dedupe_overlaps(found)


def _dedupe_overlaps(markers: Sequence[TemporalMarker]) -> list[TemporalMarker]:
    """겹치는 마커는 더 긴 것 하나만 남긴다 — 같은 어구가 두 연산자로 세어지는 것을 막는다.

    비교의 1순위는 **길이**다(시작 위치가 아니다). 앞선 짧은 마커를 먼저 집으면 그 뒤의 더
    긴 마커가 통째로 사라진다 — '최근에 골드에서 VIP로 바뀐'에서 앞의 짧은 선택자 마커가
    전이 구절을 삼켜 문장의 뜻이 '지금 골드'로 바뀌었다(구현 중 실측). 같은 길이면 앞선
    것이 이긴다(결정론).
    """
    ordered = sorted(markers, key=lambda item: (-(item.end - item.start), item.start))
    kept: list[TemporalMarker] = []
    for marker in ordered:
        if any(
            marker.start < existing.end and existing.start < marker.end
            for existing in kept
        ):
            continue
        kept.append(marker)
    return kept


__all__ = [
    "ARGUMENTS",
    "ARG_ANCHOR",
    "ARG_COUNT",
    "ARG_INTERVAL",
    "ARG_SUBINTERVAL_UNIT",
    "AS_OF",
    "AT_LEAST_ONCE_IN_INTERVAL",
    "CHANGE_BETWEEN",
    "CHANGE_COUNT",
    "CONSECUTIVE_SUBINTERVALS",
    "EVERY_SUBINTERVAL",
    "IMMEDIATELY_PRECEDING",
    "MULTI_POINT_OPERATORS",
    "NEVER_IN_INTERVAL",
    "OPERATORS",
    "OPERATOR_SPECS",
    "THROUGHOUT_INTERVAL",
    "TemporalLexicon",
    "TemporalMarker",
    "TemporalOperatorSpec",
    "TemporalQualifier",
    "TemporalSemanticsError",
    "UNCHANGED_THROUGHOUT",
    "WITHIN_INTERVAL",
    "normalize",
    "operator_documentation",
]
