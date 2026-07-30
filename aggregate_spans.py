"""숫자·단위·속성의 **출처와 소유권**을 추적하는 스팬 모델.

집계 조건 파서가 오답을 내는 방식은 늘 같았다: 문장 어딘가의 숫자와, 다른 어딘가의 단위와, 또 다른
어딘가의 속성어를 합쳐 하나의 조건을 만든다. '인구가 50만 이상인 도시중에 3개월 동안 구매내역 없는
사람'에서 임계값 50만(인구)과 단위 '개'(3**개**월의 부분문자열)가 합쳐져 '상품 수량 50만개 이상'이
되는 식이다. 절 단위 첫-매치·최근접 탐색으로는 이런 결합을 구조적으로 막을 수 없다.

이 모듈이 두는 규칙은 셋이다.

* 모든 값·단위·속성은 자기 :class:`TextSpan` 을 들고 다닌다(어디서 왔는지 추적 가능).
* 단위는 **값 스팬에 인접할 때만** 결합한다(사이에 조사·접속사·다른 숫자가 오면 남이다).
* 한 스팬은 한 의미만 소유한다 — 미지원 속성이 가져간 숫자는 다른 지표가 재사용할 수 없다.

표면어(단위·미지원 속성 별칭·접속 경계)는 전부 :mod:`aggregate_parser_config` 가 JSON 에서 읽어온다.
여기 있는 것은 스팬 계산과 결합 알고리즘뿐이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

import aggregate_parser_config
from aggregate_parser_config import AggregateParserRules

# 소유 상태 — 한 번 claimed 된 숫자는 다른 파서가 다시 쓸 수 없다.
UNCLAIMED = "unclaimed"
CLAIMED_SUPPORTED = "claimed_supported"
CLAIMED_UNSUPPORTED = "claimed_unsupported"
REJECTED_AMBIGUOUS = "rejected_ambiguous"


@dataclass(frozen=True)
class TextSpan:
    """원문 안의 반개방 구간 ``[start, end)`` — end 는 파이썬 슬라이스와 같은 exclusive 인덱스다."""

    start: int
    end: int
    text: str

    def overlaps(self, other: "TextSpan") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class UnitToken:
    surface: str
    kind: str
    canonical: str
    span: TextSpan
    priority: int


@dataclass(frozen=True)
class AttributeRef:
    """비교 스팬에 결합된 속성. ``supported`` 가 판정 결과이며 근거는 스팬으로 남는다."""

    key: str
    surface: str
    span: TextSpan
    supported: bool
    filter_key: str | None = None
    message_key: str | None = None


@dataclass
class ComparisonCandidate:
    """하나의 비교 표현('50만 이상')과 그것이 소유한 값·단위·속성."""

    candidate_id: str
    operator: str
    normalized_value: float
    value_span: TextSpan
    comparison_span: TextSpan
    attribute_ref: AttributeRef | None = None
    unit_ref: UnitToken | None = None
    ownership: str = UNCLAIMED
    consumed_by: str | None = None
    rejection_reason: str | None = None

    @property
    def is_available(self) -> bool:
        """다른 지표 파서가 이 숫자를 쓸 수 있는가. claimed/rejected 면 못 쓴다."""
        return self.ownership == UNCLAIMED or self.ownership == CLAIMED_SUPPORTED

    @property
    def blocks_sql(self) -> bool:
        return self.ownership in (CLAIMED_UNSUPPORTED, REJECTED_AMBIGUOUS)


# ── 값 파싱 ────────────────────────────────────────────────────────────────────────────
def normalize_amount(number_text: str, magnitude_text: str, rules: AggregateParserRules) -> float | None:
    """'100'+'만' → 1000000. 배수 표현은 설정(number_multipliers)이 소유한다."""
    try:
        value = float(number_text.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None
    for surface, multiplier in rules.number_multipliers:
        if magnitude_text and magnitude_text.startswith(surface):
            return value * multiplier
    return value


# ── 단위 토큰화 ────────────────────────────────────────────────────────────────────────
_UNIT_PATTERN_CACHE: dict[int, "re.Pattern[str]"] = {}


def _unit_pattern(rules: AggregateParserRules) -> "re.Pattern[str]":
    key = id(rules)
    pattern = _UNIT_PATTERN_CACHE.get(key)
    if pattern is None:
        # 표면어는 escape 해서만 조립한다 — 설정 문자열을 정규식으로 실행하지 않는다.
        unit_alternation = aggregate_parser_config.unit_alternation(rules)
        pattern = re.compile(unit_alternation)
        _UNIT_PATTERN_CACHE[key] = pattern
    return pattern


def find_unit_tokens(clause: str, rules: AggregateParserRules) -> list[UnitToken]:
    """절 안의 **모든** 단위 토큰을 스팬과 함께 돌려준다(절당 단일 primary unit 휴리스틱의 대체).

    같은 위치에서 여러 표면형이 매치되면 (1) 더 긴 surface, (2) 더 높은 priority 순으로 고른다.
    그래서 '3개월'은 duration 토큰 '개월' 하나가 되고 quantity '개' 는 아예 생성되지 않는다 —
    이것이 임시 정규식 가드 ``개(?![월년])`` 를 대체하는 구조적 보호다."""
    tokens: list[UnitToken] = []
    for match in _unit_pattern(rules).finditer(clause):
        surface = match.group(0)
        spec = rules.unit_spec(surface)
        if spec is None:  # pragma: no cover - unit_alternation 이 표면어에서 생성되므로 도달 불가
            continue
        tokens.append(
            UnitToken(
                surface=spec.surface, kind=spec.kind, canonical=spec.canonical,
                span=TextSpan(match.start(), match.end(), surface), priority=spec.priority,
            )
        )
    return tokens


def _select_unit_at(tokens: Sequence[UnitToken], start: int) -> UnitToken | None:
    """같은 시작 위치의 후보 중 longest-match → priority 순으로 하나를 고른다."""
    same_start = [token for token in tokens if token.span.start == start]
    if not same_start:
        return None
    return max(same_start, key=lambda token: (len(token.surface), token.priority))


def _adjacent_unit(
    clause: str, value_span: TextSpan, tokens: Sequence[UnitToken], rules: AggregateParserRules,
    taken: set[tuple[int, int]] | None = None,
) -> UnitToken | None:
    """값 스팬 **바로 뒤**의 단위 토큰. 사이에 공백 말고 다른 것이 오면 그 단위는 남의 것이다.

    ``taken`` 을 주면 이미 다른 값이 소유한 단위를 건너뛴다(한 단위 토큰은 한 주인)."""
    allowed_gap = rules.span_binding.max_unit_whitespace
    for gap in range(allowed_gap + 1):
        start = value_span.end + gap
        gap_text = clause[value_span.end:start]
        if gap_text.strip() != "":
            return None
        token = _select_unit_at(tokens, start)
        if token is None:
            continue
        key = (token.span.start, token.span.end)
        if taken is not None and key in taken:
            return None
        if taken is not None:
            taken.add(key)
        return token
    return None


_NUMBER_PATTERN_CACHE: dict[int, "re.Pattern[str]"] = {}


def _number_pattern(rules: AggregateParserRules) -> "re.Pattern[str]":
    key = id(rules)
    pattern = _NUMBER_PATTERN_CACHE.get(key)
    if pattern is None:
        magnitude_alternation = aggregate_parser_config.magnitude_alternation(rules)
        pattern = re.compile(rf"(?P<num>[\d,]*\d)\s*(?P<mag>{magnitude_alternation})?")
        _NUMBER_PATTERN_CACHE[key] = pattern
    return pattern


def find_value_unit_pairs(
    clause: str, rules: AggregateParserRules,
) -> list[tuple[TextSpan, UnitToken | None]]:
    """절 안의 숫자 표현과 **그 숫자에 인접한** 단위 쌍. 비교어가 없는 표현('상품 5개')도 포함한다.

    절 전체에서 단위 하나를 골라 모든 숫자에 씌우던 휴리스틱을 대신한다 — 그 방식은 '3개월 동안'의
    '개'를 옆 절 숫자의 단위로 쓸 수 있었다."""
    tokens = find_unit_tokens(clause, rules)
    taken: set[tuple[int, int]] = set()
    pairs: list[tuple[TextSpan, UnitToken | None]] = []
    for match in _number_pattern(rules).finditer(clause):
        end = match.end("mag") if match.group("mag") else match.end("num")
        span = TextSpan(match.start("num"), end, clause[match.start("num"):end])
        pairs.append((span, _adjacent_unit(clause, span, tokens, rules, taken)))
    return pairs


# ── 비교 표현 스캔 ─────────────────────────────────────────────────────────────────────
_COMPARISON_PATTERN_CACHE: dict[tuple[int, str], "re.Pattern[str]"] = {}
_NUMBER = r"[\d,]+(?:\.\d+)?"


def _comparison_pattern(rules: AggregateParserRules, operator_alternation: str) -> "re.Pattern[str]":
    """``<숫자><배수어>?<단위>?<조사>?<비교어>`` 와 ``<선행비교어><숫자><배수어>?<단위>?`` 를 함께 잡는다.

    단위를 선택으로 두는 이유는 이 스캐너의 목적이 **소유권 판정**이기 때문이다 — '100평 이상'처럼
    집계 단위가 아닌 단위가 붙은 비교도 스팬으로는 보여야 미지원 속성에 귀속시킬 수 있다."""
    key = (id(rules), operator_alternation)
    pattern = _COMPARISON_PATTERN_CACHE.get(key)
    if pattern is None:
        magnitudes = aggregate_parser_config.magnitude_alternation(rules)
        units = aggregate_parser_config.unit_alternation(rules)
        prefixes = aggregate_parser_config.prefix_operator_alternation(rules)
        pattern = re.compile(
            rf"(?P<prefix_op>{prefixes})\s*(?P<pnum>{_NUMBER})\s*(?P<pmag>{magnitudes})?\s*(?:{units})?"
            rf"|(?P<num>{_NUMBER})\s*(?P<mag>{magnitudes})?\s*(?:{units})?\s*(?:을|를|이|가)?\s*"
            rf"(?P<op>{operator_alternation})"
        )
        _COMPARISON_PATTERN_CACHE[key] = pattern
    return pattern


def scan_comparisons(
    clause: str,
    rules: AggregateParserRules,
    *,
    operator_alternation: str,
    operator_normalizer,
    id_prefix: str = "cmp",
) -> list[ComparisonCandidate]:
    """절에서 비교 표현을 스팬과 함께 뽑는다. 값 스팬은 정규식 전체 매치가 아니라 숫자+배수어 구간이다.

    ``operator_alternation`` 과 ``operator_normalizer`` 는 호출부(도메인)가 소유한 비교어 표에서
    온다 — 비교어 어휘는 이 모듈이 다시 선언하지 않는다."""
    candidates: list[ComparisonCandidate] = []
    for index, match in enumerate(_comparison_pattern(rules, operator_alternation).finditer(clause)):
        if match.group("prefix_op") is not None:
            operator = next(
                (op.operator for op in rules.prefix_operators if op.surface == match.group("prefix_op")), None
            )
            number_group, magnitude_group = "pnum", "pmag"
        else:
            operator = operator_normalizer(match.group("op"))
            number_group, magnitude_group = "num", "mag"
        if not operator:
            continue
        value = normalize_amount(match.group(number_group), match.group(magnitude_group) or "", rules)
        if value is None:
            continue
        value_start = match.start(number_group)
        value_end = match.end(magnitude_group) if match.group(magnitude_group) else match.end(number_group)
        candidates.append(
            ComparisonCandidate(
                candidate_id=f"{id_prefix}:{index}:{value_start}",
                operator=operator,
                normalized_value=value,
                value_span=TextSpan(value_start, value_end, clause[value_start:value_end]),
                comparison_span=TextSpan(match.start(), match.end(), match.group(0)),
            )
        )
    return candidates


# ── 값 ↔ 단위 인접성 ───────────────────────────────────────────────────────────────────
def bind_units(
    clause: str,
    candidates: Sequence[ComparisonCandidate],
    tokens: Sequence[UnitToken],
    rules: AggregateParserRules,
) -> None:
    """각 비교 candidate 에 **자기 값 스팬에 인접한** 단위만 붙인다(제자리 변경).

    인접성은 ``clause[value_span.end:unit.span.start]`` 이 허용 공백 이내인지로만 판정한다. 조사·
    접속사·문장부호·다른 숫자가 끼면 그 단위는 남의 것이다. 한 단위 토큰은 한 candidate 만 소유한다."""
    taken: set[tuple[int, int]] = set()
    for candidate in candidates:
        candidate.unit_ref = _adjacent_unit(clause, candidate.value_span, tokens, rules, taken)


# ── 속성 결합 ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AttributeIndex:
    """지원/미지원 속성 별칭의 조회 인덱스.

    지원 목록은 ``member_target_filters.json`` 이 소유하고(단일 진실 소스), 미지원 힌트는
    ``aggregate_parser_rules.json`` 이 소유한다. 어느 쪽도 이 모듈이 다시 나열하지 않는다."""

    supported: tuple[tuple[str, str], ...]          # (별칭, filter_key)
    unsupported: tuple[tuple[str, str, str], ...]   # (별칭, attribute_key, message_key)
    context_unsupported: tuple[tuple[str, str, str, tuple[str, ...]], ...]


def build_attribute_index(
    filters: Mapping[str, Any], rules: AggregateParserRules
) -> AttributeIndex:
    """member_target_filters.json 에서 지원 속성 별칭을, 규칙 JSON 에서 미지원 힌트를 인덱싱한다."""
    supported: list[tuple[str, str]] = []
    for source in rules.supported_attribute_sources:
        section: Any = filters
        for part in source.section.split("."):
            section = section.get(part) if isinstance(section, Mapping) else None
        entries: Iterable[tuple[str | None, Any]]
        if isinstance(section, Mapping):
            entries = list(section.items())
        elif isinstance(section, list):
            entries = [(None, entry) for entry in section]
        else:
            continue
        for entry_key, entry in entries:
            if not isinstance(entry, Mapping):
                continue
            filter_key = entry_key
            if source.key_field and isinstance(entry.get(source.key_field), str):
                filter_key = entry[source.key_field]
            if not isinstance(filter_key, str):
                continue
            for field in source.alias_fields:
                for alias in entry.get(field) or []:
                    if isinstance(alias, str) and alias.strip():
                        supported.append((alias.strip(), filter_key))
    unsupported: list[tuple[str, str, str]] = []
    context_unsupported: list[tuple[str, str, str, tuple[str, ...]]] = []
    for hint in rules.unsupported_attribute_hints:
        unsupported.extend((alias, hint.key, hint.message_key) for alias in hint.aliases)
        context_unsupported.extend(
            (item.surface, hint.key, hint.message_key, item.context_terms) for item in hint.context_aliases
        )
    # 긴 별칭이 먼저 매치되도록 정렬한다(포함 관계 최장 일치).
    supported.sort(key=lambda item: -len(item[0]))
    unsupported.sort(key=lambda item: -len(item[0]))
    return AttributeIndex(
        supported=tuple(supported),
        unsupported=tuple(unsupported),
        context_unsupported=tuple(context_unsupported),
    )


def _tokens_with_spans(text: str, offset: int) -> list[TextSpan]:
    return [TextSpan(offset + m.start(), offset + m.end(), m.group(0)) for m in re.finditer(r"\S+", text)]


def _boundary_cut_left(text: str, rules: AggregateParserRules) -> str:
    """왼쪽 탐색 구간을 가장 가까운 경계 **뒤**에서 끊는다(경계 너머 속성은 남의 것이다)."""
    binding = rules.span_binding
    cut = 0
    if binding.stop_at_clause_boundary:
        for punctuation in binding.boundary_punctuation:
            position = text.rfind(punctuation)
            if position >= 0:
                cut = max(cut, position + len(punctuation))
    if binding.stop_at_conjunction:
        for token in (*binding.conjunction_tokens, *binding.disjunction_tokens):
            position = text.rfind(token)
            if position >= 0:
                cut = max(cut, position + len(token))
    return text[cut:]


def _boundary_cut_right(text: str, rules: AggregateParserRules) -> str:
    """오른쪽 탐색 구간을 가장 가까운 경계 **앞**에서 끊는다."""
    binding = rules.span_binding
    cut = len(text)
    if binding.stop_at_clause_boundary:
        for punctuation in binding.boundary_punctuation:
            position = text.find(punctuation)
            if position >= 0:
                cut = min(cut, position)
    if binding.stop_at_conjunction:
        for token in (*binding.conjunction_tokens, *binding.disjunction_tokens):
            position = text.find(token)
            if position >= 0:
                cut = min(cut, position)
    return text[:cut]


def attribute_search_windows(
    clause: str, candidate: ComparisonCandidate, candidates: Sequence[ComparisonCandidate],
    rules: AggregateParserRules,
) -> tuple[TextSpan | None, TextSpan | None]:
    """비교 스팬 좌·우의 속성 탐색 구간을 만든다(다른 비교 표현·절/접속 경계에서 끊는다)."""
    binding = rules.span_binding
    left_bound = 0
    right_bound = len(clause)
    for other in candidates:
        if other.candidate_id == candidate.candidate_id:
            continue
        if other.comparison_span.end <= candidate.comparison_span.start:
            left_bound = max(left_bound, other.comparison_span.end)
        elif other.comparison_span.start >= candidate.comparison_span.end:
            right_bound = min(right_bound, other.comparison_span.start)

    left_raw = clause[left_bound:candidate.comparison_span.start]
    left_text = _boundary_cut_left(left_raw, rules)
    left_offset = left_bound + len(left_raw) - len(left_text)
    left_tokens = _tokens_with_spans(left_text, left_offset)[-binding.attribute_search_left_tokens:]
    left_span = (
        TextSpan(left_tokens[0].start, left_tokens[-1].end, clause[left_tokens[0].start:left_tokens[-1].end])
        if left_tokens and binding.attribute_search_left_tokens else None
    )

    right_raw = clause[candidate.comparison_span.end:right_bound]
    right_text = _boundary_cut_right(right_raw, rules)
    right_tokens = _tokens_with_spans(right_text, candidate.comparison_span.end)[
        : binding.attribute_search_right_tokens
    ]
    right_span = (
        TextSpan(right_tokens[0].start, right_tokens[-1].end, clause[right_tokens[0].start:right_tokens[-1].end])
        if right_tokens and binding.attribute_search_right_tokens else None
    )
    return left_span, right_span


def _hits_in(window: TextSpan | None, index: AttributeIndex, clause: str) -> list[tuple[int, AttributeRef]]:
    """탐색 구간 안의 속성 별칭 히트를 (비교 스팬으로부터의 거리, 참조)로 돌려준다."""
    if window is None:
        return []
    hits: list[tuple[int, AttributeRef]] = []
    text = window.text
    for alias, filter_key in index.supported:
        position = text.rfind(alias)
        if position >= 0:
            span = TextSpan(window.start + position, window.start + position + len(alias), alias)
            hits.append((position, AttributeRef(
                key=filter_key, surface=alias, span=span, supported=True, filter_key=filter_key,
            )))
    for alias, attribute_key, message_key in index.unsupported:
        position = text.rfind(alias)
        if position >= 0:
            span = TextSpan(window.start + position, window.start + position + len(alias), alias)
            hits.append((position, AttributeRef(
                key=attribute_key, surface=alias, span=span, supported=False, message_key=message_key,
            )))
    for alias, attribute_key, message_key, context_terms in index.context_unsupported:
        position = text.rfind(alias)
        if position < 0 or not any(term in clause for term in context_terms):
            continue
        span = TextSpan(window.start + position, window.start + position + len(alias), alias)
        # 문맥 근거를 더 들고 있으므로 같은 표면어의 지원 해석보다 구체적이다 — 거리를 앞당겨 우선한다.
        hits.append((position, AttributeRef(
            key=attribute_key, surface=alias, span=span, supported=False, message_key=message_key,
        )))
    return hits


def _generic_attribute(
    clause: str, candidate: ComparisonCandidate, rules: AggregateParserRules,
) -> AttributeRef | None:
    """힌트 목록에 없는 미지원 속성을 **구조**로 잡는다: ``<한글 명사><주격/주제 조사> <숫자>``.

    조사 앵커가 있다는 것은 그 명사가 숫자의 기준 항목이라는 뜻이다 — 목록에 없다고 조용히 숫자를
    다른 지표에 넘기면 '인구가 50만' 이 '상품 수량 50만개' 가 되는 그 사고가 다시 난다."""
    binding = rules.span_binding
    particles = "|".join(re.escape(p) for p in binding.generic_attribute_particles)
    head = clause[: candidate.value_span.start]
    match = re.search(rf"(?P<noun>[가-힣]{{{binding.generic_attribute_min_length},}})(?:{particles})\s*$", head)
    if match is None:
        return None
    start = match.start("noun")
    return AttributeRef(
        key="unknown_attribute", surface=match.group("noun"),
        span=TextSpan(start, start + len(match.group("noun")), match.group("noun")),
        supported=False, message_key=rules.generic_unsupported_message_key,
    )


def bind_attributes(
    clause: str,
    candidates: Sequence[ComparisonCandidate],
    index: AttributeIndex,
    rules: AggregateParserRules,
) -> None:
    """각 candidate 에 속성을 결합하고 소유 상태를 확정한다(제자리 변경).

    좌·우 양방향의 제한된 로컬 구간에서 후보를 찾고, 가장 가까운 하나를 고른다. 지원/미지원 후보가
    같은 거리로 맞붙으면 임의로 고르지 않고 ``rejected_ambiguous`` 로 남겨 clarification 경로로 보낸다."""
    for candidate in candidates:
        left_window, right_window = attribute_search_windows(clause, candidate, candidates, rules)
        scored: list[tuple[int, AttributeRef]] = []
        for _distance, ref in _hits_in(left_window, index, clause):
            scored.append((candidate.comparison_span.start - ref.span.end, ref))
        for _distance, ref in _hits_in(right_window, index, clause):
            scored.append((ref.span.start - candidate.comparison_span.end, ref))
        if not scored:
            generic = _generic_attribute(clause, candidate, rules)
            if generic is not None:
                candidate.attribute_ref = generic
                candidate.ownership = CLAIMED_UNSUPPORTED
                candidate.consumed_by = f"unsupported_attribute:{generic.key}"
            continue
        best_distance = min(distance for distance, _ in scored)
        nearest = [ref for distance, ref in scored if distance == best_distance]
        if len({ref.supported for ref in nearest}) > 1:
            candidate.ownership = REJECTED_AMBIGUOUS
            candidate.rejection_reason = "ambiguous_attribute_binding"
            candidate.attribute_ref = nearest[0]
            continue
        # 같은 거리의 동일 극성 후보 중에서는 긴 별칭(더 구체적인 표현)을 고른다.
        chosen = max(nearest, key=lambda ref: len(ref.surface))
        candidate.attribute_ref = chosen
        if chosen.supported:
            candidate.ownership = CLAIMED_SUPPORTED
            candidate.consumed_by = f"supported_attribute:{chosen.key}"
        else:
            candidate.ownership = CLAIMED_UNSUPPORTED
            candidate.consumed_by = f"unsupported_attribute:{chosen.key}"


def bind_clause(
    clause: str,
    index: AttributeIndex,
    rules: AggregateParserRules,
    *,
    operator_alternation: str,
    operator_normalizer,
    id_prefix: str = "cmp",
) -> list[ComparisonCandidate]:
    """한 절에 대해 스캔 → 단위 결합 → 속성 결합을 한 번에 수행한다."""
    candidates = scan_comparisons(
        clause, rules,
        operator_alternation=operator_alternation,
        operator_normalizer=operator_normalizer,
        id_prefix=id_prefix,
    )
    bind_units(clause, candidates, find_unit_tokens(clause, rules), rules)
    bind_attributes(clause, candidates, index, rules)
    return candidates


def has_disjunction(text: str, rules: AggregateParserRules) -> bool:
    """문장에 OR 접속이 있는가 — 미지원 조건을 빼고 '나머지만'을 제안해도 되는지를 가른다."""
    return any(token in text for token in rules.span_binding.disjunction_tokens)


__all__ = [
    "UNCLAIMED", "CLAIMED_SUPPORTED", "CLAIMED_UNSUPPORTED", "REJECTED_AMBIGUOUS",
    "TextSpan", "UnitToken", "AttributeRef", "ComparisonCandidate", "AttributeIndex",
    "normalize_amount", "find_unit_tokens", "find_value_unit_pairs", "scan_comparisons", "bind_units",
    "build_attribute_index", "bind_attributes", "bind_clause", "attribute_search_windows",
    "has_disjunction", "replace",
]
