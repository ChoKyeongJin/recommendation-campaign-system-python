from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import audience_frame
import calendar_window
import event_ir
from calendar_window import (
    DurationCandidate,
    duration_window_candidates,
    parse_calendar_window_spans,
)
import condition_normalizers
import lexicon_patterns
import semantic_domain_binding
from semantic_normalizers import (
    AmountNormalizer,
    Money,
    NormalizationError,
    decimal_json_value,
    exact_decimal,
)
from .semantic_outcome import (
    FAILURE_KINDS,
    SEMANTIC_STATUSES,
    FailureKind,
    FailureReason,
    SemanticOutcome,
    parse_semantic_outcome_projection,
    semantic_outcome_json_schema,
    validate_semantic_outcome_state,
)


SEMANTIC_IR_STATUSES = SEMANTIC_STATUSES
SEMANTIC_FAILURE_KINDS = FAILURE_KINDS

_COMPARISON_TERMS: tuple[tuple[str, str], ...] = tuple(
    condition_normalizers.comparison_literal_operators().items()
)
_COMPARISON_RE = re.compile(
    "|".join(re.escape(surface) for surface, _canonical in _COMPARISON_TERMS)
)
# 퍼센트포인트는 퍼센트가 아니다. 꼬리를 버리고 값만 가져오면 '비중 차이 10%포인트'가 '10%'라는
# **다른 뜻**의 리터럴이 된다(차이 vs 값). 지금 이 시스템에는 %p 를 받을 지표가 없으므로 여기서는
# 리터럴로 주장하지 않는 데까지만 한다 — 지표가 생기면 percentage_point 종류를 따로 추가한다.
_PERCENTAGE_POINT_TAIL = r"(?!\s*(?:포인트|p(?![a-z])))"
_PERCENT_RE = re.compile(
    rf"(?<![\d.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>%|퍼센트|프로){_PERCENTAGE_POINT_TAIL}"
)
# 문장 안에서 AmountNormalizer 에 넘길 금액 표면의 경계만 찾는다. 배수 계산과 한글 수사
# 해석은 이 정규식이 아니라 AmountNormalizer 가 소유한다. 통화 표식이 필수이므로 기간이나
# 단순 수량을 금액으로 추측하지 않는다.
_MONEY_MAGNITUDE_GRAMMAR = r"(?:천만|백만|조|억|만|천)"
_MONEY_ARABIC_GRAMMAR = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY_SINO_GRAMMAR = r"[영공일이삼사오육칠팔구십백천]+"
_MONEY_VALUE_GRAMMAR = (
    rf"(?:{_MONEY_ARABIC_GRAMMAR}(?:\s*{_MONEY_MAGNITUDE_GRAMMAR})?"
    rf"|{_MONEY_SINO_GRAMMAR}\s*{_MONEY_MAGNITUDE_GRAMMAR})"
)
_MONEY_SUFFIX_CURRENCY_GRAMMAR = r"(?:원|won|krw|₩)"
# 한국어 '원'은 접미 통화 단위다. 접두사로도 허용하면 '지원 20만 명'의 끝 글자부터
# '원 20만'을 금액으로 오인한다. 접두 통화 표식은 실제 접두 표기인 기호/영문만 연다.
_MONEY_PREFIX_CURRENCY_GRAMMAR = r"(?:won|krw|₩)"
MONEY_LITERAL_RE = re.compile(
    rf"(?<![\d.,A-Za-z영공일이삼사오육칠팔구십백천])(?:"
    rf"{_MONEY_VALUE_GRAMMAR}\s*{_MONEY_SUFFIX_CURRENCY_GRAMMAR}"
    rf"|{_MONEY_PREFIX_CURRENCY_GRAMMAR}\s*{_MONEY_VALUE_GRAMMAR}"
    rf")(?![\d.,A-Za-z])",
    re.IGNORECASE,
)
COUNTER_UNIT_SEMANTICS = semantic_domain_binding.counter_units()
_COUNTER_SURFACE_PATTERN = "|".join(
    re.escape(unit)
    for unit in sorted(COUNTER_UNIT_SEMANTICS, key=lambda item: (-len(item), item))
) or r"(?!)"
COUNTER_LITERAL_RE = re.compile(
    r"(?<![\d.])(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    rf"(?P<unit>{_COUNTER_SURFACE_PATTERN})"
    # Korean counters normally carry a case/topic particle (``10개를``,
    # ``3회는``). Keep the particle outside the literal evidence span.
    r"(?=(?:을|를|이|가|은|는|의|만|중|에서|으로|로)?(?:\s|[,.;!?]|$))"
)
# 상대 기간 표면('6개월', '30일', '2주'). 이것이 없으면 '최근 6개월'의 '6' 이 **주인 없는 맨 숫자**
# 원자로 남는다 — 그 절의 노드가 period 를 소유해도 커버리지는 그 사실을 모르므로 정상 요청이
# 누락으로 오보고되고 재방출까지 돌게 된다(실측 2026-08-02: '최근 6개월 주문 5건 이상' 0/5 실패).
# 달력 창(2019년 3월)은 위에서 date_window 로 이미 점유되므로 여기 걸리지 않는다.
DURATION_UNIT_SEMANTICS = condition_normalizers.numeric_duration_unit_semantics()
_DURATION_SURFACE_PATTERN = "|".join(
    re.escape(unit)
    for unit in sorted(DURATION_UNIT_SEMANTICS, key=lambda item: (-len(item), item))
) or r"(?!)"
# 단위 뒤에 올 수 있는 **조사·범위 표지**. 한국어에는 낱말 경계가 없으므로 "단위 뒤에 한글이
# 오면 다른 낱말"이라는 규칙만으로는 ``30일에는`` 의 ``30일`` 을 놓친다 — 그리고 그 누락은
# 그대로 ``기간 값이 없습니다`` 가 되어, 사용자가 **말한** 기간을 되묻게 된다(실측 2026-08-08
# 감사 #62). 목록을 여기 적지 않고 어휘 선언에서 만드는 이유는 조사 목록이 이 저장소에 이미
# 다섯 벌 있기 때문이다(§53) — 새 벌을 만들면 여섯 번째가 된다.
_DURATION_TRAILING_PATTERN = "|".join(
    re.escape(term)
    for term in sorted(
        {
            term
            for name in (
                "frame_particle",
                "bound_particle",
                "range_opener",
                "range_closer",
                "temporal_within_marker",
            )
            for term in lexicon_patterns.vocabulary(name)
            if term
        },
        key=lambda item: (-len(item), item),
    )
)
DURATION_LITERAL_RE = re.compile(
    rf"(?<![\d.])(?P<value>\d+)\s*(?P<unit>{_DURATION_SURFACE_PATTERN})"
    rf"(?![가-힣A-Za-z0-9])"
    if not _DURATION_TRAILING_PATTERN
    else rf"(?<![\d.])(?P<value>\d+)\s*(?P<unit>{_DURATION_SURFACE_PATTERN})"
    rf"(?=(?:{_DURATION_TRAILING_PATTERN})|[^가-힣A-Za-z0-9]|$)"
)
# 평가 기준일은 일반 달력 창과 다르다. ``2026년 8월 3일 기준 최근 30일``의
# 첫 날짜는 독립 사건 구간이 아니라 뒤 rolling window의 고정 anchor다. 이 역할은
# 모델이 추측하지 않고 애플리케이션이 원문 표면에서만 부여한다.
AS_OF_DATE_LITERAL_RE = re.compile(
    r"(?P<year>\d{4})\s*년\s*(?P<month>\d{1,2})\s*월\s*"
    r"(?P<day>\d{1,2})\s*일(?=\s*기준)"
)
_NUMBER_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")

# 기준일이 없는 호출에서 달력 파서의 시스템 시계 fallback 이 의미 결과로 새어 나오지 않게 한다.
# 서로 멀리 떨어진 두 기준일에서도 **동일한** 창만 남기면 명시 연도 같은 절대 표현은 보존되고,
# 지난달/올해/연도 없는 분기처럼 기준일에 의존하는 표현은 fail-close 된다.
_REFERENCE_DATE_PROBES = (date(2000, 1, 15), date(2400, 7, 15))


def _source_duration_candidates(query: str) -> list[tuple[int, int, DurationCandidate]]:
    """기간 표현 후보(calendar_window 판정)를 **원문 좌표계**로 옮긴다 → (start, end, candidate).

    종류를 정하는 표면 문법의 소유자는 :mod:`calendar_window` 다 — 여기서 '최근'/'전' 을 다시
    읽지 않는다. 그 모듈의 후보 구간은 공백을 제거한 좌표계이므로 원문 좌표로 옮기기만 한다.
    대응표를 그쪽에 함께 넘겨야 단어형 후보의 낱말 경계 판정이 돈다(원문에만 남아 있는 정보다).
    """
    original_index = [index for index, char in enumerate(query) if not char.isspace()]
    compact = "".join(query[index] for index in original_index)
    rows: list[tuple[int, int, DurationCandidate]] = []
    for candidate in duration_window_candidates(
        compact, source=query, source_offsets=original_index
    ):
        if not 0 <= candidate.start < candidate.end <= len(original_index):
            continue
        rows.append(
            (original_index[candidate.start], original_index[candidate.end - 1] + 1, candidate)
        )
    return rows


def _duration_temporal_kinds(query: str) -> list[tuple[int, int, str]]:
    """기간 표현의 **의미 종류**(calendar_window 판정)를 원문 좌표계로 옮긴다.

    '최근 30일'과 '30일 전'은 리터럴 원자가 완전히 같다(value=30, unit=days) — 값만 넘기고
    종류를 창 생산자의 판단에 맡기면 두 뜻이 조용히 뒤바뀐다. 실측(2026-08-03): '최근 30일'이
    relative 창으로 와서 30일 전 **하루**만 보는 조건이 됐고, 그 응답은 성공으로 나갔다.
    """
    return [(start, end, candidate.kind) for start, end, candidate in _source_duration_candidates(query)]


SEMANTIC_IR_LLM_JSON_SCHEMA: dict[str, Any] = semantic_outcome_json_schema()


def _number(value: str) -> int | str:
    """Project a numeric surface to JSON without a binary-float round trip."""

    parsed = exact_decimal(value, allow_string=True)
    if parsed is None:
        raise ValueError(f"invalid finite numeric literal: {value!r}")
    return decimal_json_value(parsed)


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied)


# 기간 리터럴이 **창이 될 때**의 wire 모양. 종류는 이미 판정돼 있고(``temporal_kind``) 창의
# 타입·값·단위 표기는 IR 이 소유하므로 여기서 dict 을 손으로 조립하지 않는다.
#
# 이 투영이 없던 동안 구조화 안내는 모델에게 "binding 의 값·단위를 사용"하라고 시켰는데, 이
# 추출기의 단위 표기는 복수형('days')이고 툴 스키마 enum 은 단수형(day|week|month|year)이다.
# 실측(2026-08-07)에서 모델이 그 'days' 를 그대로 복사한 응답이 스키마 검증에서 떨어져 교정
# 라운드가 실패했다. 이제 모델이 옮기는 것은 값이 아니라 **객체 하나**이므로 옮기다 틀릴 자리가
# 없다 — 절대 구간(``date_window``)이 이미 쓰던 계약과 같다.
_TEMPORAL_KIND_WINDOW_TYPES: dict[str, Any] = {
    "rolling_duration": event_ir.RollingWindow,  # 기준일에서 거슬러 세는 길이
    "past_point": event_ir.RelativeWindow,       # 그 시점이 속한 달력 칸
}


def _duration_event_ir_window(
    value: Any, unit: Any, temporal_kind: Any, *, future_directed: bool = False
) -> dict[str, Any] | None:
    """기간 리터럴 → wire 창. 창으로 표현할 수 없으면 ``None`` (지어내지 않는다).

    표현할 수 없는 경우가 실제로 있다: 종류가 판정되지 않은 리터럴, IR 어휘 밖의 단위(시간),
    정수가 아닌 값, 그리고 **미래를 보는 기간**('향후 7일'). IR 의 창은 둘 다 과거를 보므로
    미래 기간을 창으로 옮기면 방향이 뒤집힌다. 그때 빈 창을 짓느니 이 리터럴은 창 후보로
    제시되지 않는다 — 결핍으로 남는 편이 지어낸 창보다 낫다(CLAUDE.md §11·§12).
    """

    window_type = _TEMPORAL_KIND_WINDOW_TYPES.get(str(temporal_kind))
    canonical = event_ir.canonical_unit(unit)
    if window_type is None or canonical is None or future_directed:
        return None
    try:
        return window_type(value=value, unit=canonical).to_dict()
    except event_ir.IrSchemaError:
        return None


def _deterministic_calendar_window_spans(
    query: str,
    reference_date: date | None,
) -> list[tuple[dict[str, Any], int, int]]:
    """Return only calendar windows whose value does not hide a system clock read."""

    if reference_date is not None:
        return parse_calendar_window_spans(query, today=reference_date)

    probed = [
        parse_calendar_window_spans(query, today=probe)
        for probe in _REFERENCE_DATE_PROBES
    ]
    first_by_span = {(start, end): window for window, start, end in probed[0]}
    second_by_span = {(start, end): window for window, start, end in probed[1]}
    return [
        (window, start, end)
        for (start, end), window in sorted(first_by_span.items())
        if second_by_span.get((start, end)) == window
    ]


def scan_literal_bindings(
    query: str,
    *,
    current_date: str | date | None = None,
) -> list[dict[str, Any]]:
    """Extract surface evidence without choosing a domain metric.

    Dates, money, counter-bearing numbers, percentages, and comparison operators
    are application-owned. Counter literals retain their exact surface unit, but
    this scanner does not decide whether ``3회`` means orders, logins, or sends.
    The LLM may only connect the returned IDs to semantic roles; it cannot submit
    replacement values in the semantic operation payload.
    """

    if not isinstance(query, str) or not query:
        return []
    reference_date: date | None
    if isinstance(current_date, date):
        reference_date = current_date
    elif isinstance(current_date, str):
        try:
            reference_date = date.fromisoformat(current_date)
        except ValueError:
            reference_date = None
    else:
        reference_date = None

    literals: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    counters: dict[str, int] = {}
    duration_candidates = _source_duration_candidates(query)
    temporal_kinds = [(start, end, candidate.kind) for start, end, candidate in duration_candidates]

    def append(kind: str, start: int, end: int, value: Any, normalized: Any) -> None:
        counters[kind] = counters.get(kind, 0) + 1
        literals.append(
            {
                "id": f"{kind}_{counters[kind]}",
                "kind": kind,
                "text": query[start:end],
                "start": start,
                "end": end,
                "value": value,
                "normalized": normalized,
            }
        )
        occupied.append((start, end))

    # An explicit ``<date> 기준`` becomes an anchor only when this query also
    # contains a rolling-duration surface.  Otherwise the ordinary calendar
    # parser retains ownership (for snapshot/as-of relation semantics).
    if any(kind == "rolling_duration" for _start, _end, kind in temporal_kinds):
        for match in AS_OF_DATE_LITERAL_RE.finditer(query):
            try:
                anchor = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
            append(
                "as_of_date",
                match.start(),
                match.end(),
                match.group(0),
                {"date": anchor.isoformat(), "role": "rolling_anchor"},
            )

    # **두 상대 경계는 하나의 구간이다.** ``3개월 전부터 1개월 전까지`` 를 경계 둘로 두면
    # 소비자가 반쪽만 읽어 다른 기간이 나간다(감사 #85). 합성은 달력 문법이 소유하고
    # (:func:`calendar_window.compose_boundary_interval`) 여기서는 그 결과를 **원자 하나**로
    # 옮긴다 — 원자를 하나로 만드는 것이 요점이다. 둘로 두면 리터럴 정산이 두 소비자를
    # 요구하고, 구간 하나는 그중 하나만 소비할 수 있다.
    if reference_date is not None:
        composed = calendar_window.compose_boundary_interval(
            [candidate for _start, _end, candidate in duration_candidates],
            today=reference_date,
        )
        if composed is not None:
            window, compact_span = composed
            span = audience_frame.compact_to_source_span(query, *compact_span)
            if span is not None and not _overlaps(span[0], span[1], occupied):
                start_date = date(
                    int(window["from"][:4]), int(window["from"][4:6]), int(window["from"][6:8])
                )
                inclusive_end = date(
                    int(window["to"][:4]), int(window["to"][4:6]), int(window["to"][6:8])
                )
                append(
                    "date_window",
                    span[0],
                    span[1],
                    query[span[0]:span[1]],
                    {
                        "from": window["from"],
                        "to": window["to"],
                        "label": window.get("label"),
                        "event_ir_window": {
                            "type": "interval",
                            "start": start_date.isoformat(),
                            "end_exclusive": (inclusive_end + timedelta(days=1)).isoformat(),
                        },
                    },
                )

    for window, start, end in _deterministic_calendar_window_spans(query, reference_date):
        if _overlaps(start, end, occupied):
            continue
        start_date = date(
            int(window["from"][:4]), int(window["from"][4:6]), int(window["from"][6:8])
        )
        inclusive_end = date(
            int(window["to"][:4]), int(window["to"][4:6]), int(window["to"][6:8])
        )
        append(
            "date_window",
            start,
            end,
            query[start:end],
            {
                "from": window["from"],
                "to": window["to"],
                "label": window.get("label"),
                "event_ir_window": {
                    "type": "interval",
                    "start": start_date.isoformat(),
                    "end_exclusive": (inclusive_end + timedelta(days=1)).isoformat(),
                },
                # 시각 경계는 있을 때만 싣는다 — 날짜만 있는 창의 normalized shape 를 바꾸지 않는다.
                **{key: window[key] for key in ("from_time", "to_time") if window.get(key) is not None},
            },
        )

    for match in MONEY_LITERAL_RE.finditer(query):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        try:
            normalized_money = AmountNormalizer.normalize(match.group(0))
        except NormalizationError:
            continue
        if not isinstance(normalized_money, Money):
            continue
        money_payload = normalized_money.to_dict()
        append(
            "money",
            match.start(),
            match.end(),
            money_payload["amount"],
            money_payload,
        )

    for match in _PERCENT_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            exact = exact_decimal(match.group("value"), allow_string=True)
            if exact is None:
                continue
            value = decimal_json_value(exact)
            append("percentage", match.start(), match.end(), value, {"value": value, "unit": "percent"})

    for match in COUNTER_LITERAL_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group("value").replace(",", ""))
            unit = match.group("unit")
            append(
                "number_with_unit",
                match.start(),
                match.end(),
                value,
                {
                    "value": value,
                    "surface_unit": unit,
                    "unit": "count",
                },
            )

    for match in DURATION_LITERAL_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group("value"))
            unit = match.group("unit")
            normalized = {
                "value": value,
                "surface_unit": unit,
                "semantic_unit": DURATION_UNIT_SEMANTICS[unit],
            }
            # 종류를 판정하는 단어('최근'/'전')는 이 원자 **밖**에 있다. 원자 구간은 그대로 두고
            # (다른 소비자가 이 좌표로 소유권을 계산한다) 판정 결과만 실어 보낸다.
            kind = next(
                (
                    kind
                    for start, end, kind in temporal_kinds
                    if start < match.end() and match.start() < end
                ),
                None,
            )
            if kind is not None:
                normalized["temporal_kind"] = kind
            window = _duration_event_ir_window(
                value,
                DURATION_UNIT_SEMANTICS[unit],
                kind,
                future_directed=calendar_window.is_future_directed_duration(
                    query, match.start()
                ),
            )
            if window is not None:
                normalized["event_ir_window"] = window
            append("duration", match.start(), match.end(), value, normalized)

    # 숫자 없는 단어형 기간('일주일', '한 달', '반년', '보름', '석달', '한해'). 값·단위 선언과
    # 낱말 경계 판정의 소유자는 :mod:`calendar_window` 하나다 — 여기서는 그 후보를 숫자형과
    # **같은 모양**의 원자로 옮기기만 한다. 이 원자가 없던 동안 사용자가 분명히 말한 기간이
    # 리터럴 근거에 남지 않아, '기간을 말했는가' 판정이 거짓 결핍(되묻기)으로 닫혔다.
    #
    # ``surface_unit`` 은 canonical 한국어 단위다(days→일, weeks→주, months→개월, years→년).
    # 단어형은 값과 단위가 한 낱말에 붙어 있어 떼어낼 단위 표면이 없는데, 이 필드는 숫자형에서
    # **단위**를 담는 자리이고(:func:`condition_normalizers.numeric_duration_unit_semantics` 의
    # 키), 실제로 읽는 코드는 계수 결속(number_with_unit)뿐이라 기간에서는 표시·대조용이다.
    # 원문 표면은 binding["text"] 가 그대로 보존하므로 근거는 잃지 않는다. 같은 투영을
    # :func:`calendar_window.relative_window_label` 이 이미 쓴다('일주일' → '최근 7일').
    for start, end, candidate in duration_candidates:
        if not calendar_window.is_word_duration_surface(query[start:end]):
            continue
        if _overlaps(start, end, occupied):
            continue
        surface_unit = calendar_window.CANON_TO_KO_UNIT.get(candidate.unit)
        if surface_unit is None:
            continue
        normalized = {
            "value": candidate.value,
            "surface_unit": surface_unit,
            "semantic_unit": candidate.unit,
            "temporal_kind": candidate.kind,
        }
        window = _duration_event_ir_window(
            candidate.value,
            candidate.unit,
            candidate.kind,
            future_directed=calendar_window.is_future_directed_duration(query, start),
        )
        if window is not None:
            normalized["event_ir_window"] = window
        append("duration", start, end, candidate.value, normalized)

    comparison_map = dict(_COMPARISON_TERMS)
    for match in _COMPARISON_RE.finditer(query):
        append(
            "comparison_operator",
            match.start(),
            match.end(),
            match.group(0),
            comparison_map[match.group(0)],
        )

    for match in _NUMBER_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group(0))
            append("number", match.start(), match.end(), value, value)

    return sorted(literals, key=lambda item: (item["start"], item["end"], item["kind"]))


def bind_counter_literals(
    literals: list[dict[str, Any]],
    *,
    counter_units: Mapping[str, str] | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """확정 가능한 계수 표면만 도메인 지표에 결속한다.

    명시적으로 ``counter_units``를 주입한 호출은 그 문맥 자체가 권위다. 기본 경로는 원문과
    evidence span을 도메인 resolver에 전달하며, 모호한 ``회/번/건``은 결속하지 않는다.
    """

    bindings = copy.deepcopy(literals)
    semantics = (
        None
        if counter_units is None
        else {str(key): str(value) for key, value in counter_units.items()}
    )
    for binding in bindings:
        if binding.get("kind") != "number_with_unit":
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, dict):
            continue
        surface_unit = normalized.get("surface_unit")
        if not isinstance(surface_unit, str):
            continue
        semantic_unit = (
            semantics.get(surface_unit)
            if semantics is not None
            else semantic_domain_binding.bind_counter_unit(
                surface_unit,
                text=query,
                start=binding.get("start"),
                end=binding.get("end"),
            )
        )
        if semantic_unit:
            normalized.pop("unit", None)
            normalized["semantic_unit"] = semantic_unit
    return bindings


def extract_literal_bindings(
    query: str,
    *,
    current_date: str | date | None = None,
) -> list[dict[str, Any]]:
    """Extract application-owned surface evidence without domain binding.

    Domain metric selection belongs to a later binder after the semantic node
    has established its source/metric.  Call :func:`bind_counter_literals`
    explicitly when that context is available.
    """

    return scan_literal_bindings(query, current_date=current_date)


def empty_semantic_ir(
    status: str = "needs_clarification",
    *,
    missing_fields: list[str] | None = None,
    message: str | None = None,
    failure_kind: FailureKind | None = None,
    # 결핍의 **원인**. 이 인자가 없던 동안 canonical 경로의 causes 는 구조적으로 항상 []
    # 였고, 그래서 원문에 값이 있는 결핍까지 전부 '사용자에게 묻기'로 귀결됐다.
    missing_field_causes: list[dict[str, Any]] | None = None,
    unsupported_operations: list[dict[str, Any]] | None = None,
    policy_applications: list[dict[str, Any]] | None = None,
    # 파생 사유(kind → reason)로 구별되지 않는 실패의 **명시 선언**. needs_clarification 에서만
    # 쓴다 — 다른 상태에는 그 구별이 필요한 실패가 아직 없다.
    failure_reason: FailureReason | None = None,
) -> dict[str, Any]:
    if status == "resolved":
        outcome = SemanticOutcome.resolved(message=message, failure_kind=failure_kind)
    elif status == "unsupported":
        outcome = SemanticOutcome.unsupported(
            operations=unsupported_operations or [],
            message=message,
            failure_kind=failure_kind or "unsupported",
        )
    elif status == "policy_applied":
        outcome = SemanticOutcome.policy_applied(
            applications=policy_applications or [],
            message=message,
            failure_kind=failure_kind,
        )
    elif status == "needs_clarification":
        outcome = SemanticOutcome.needs_clarification(
            missing_fields=missing_fields or [],
            missing_field_causes=missing_field_causes or [],
            message=message,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
        )
    else:
        raise ValueError(f"unknown semantic outcome status: {status!r}")
    return outcome.to_legacy_dict()


def write_semantic_ir(payload: dict[str, Any], projection: dict[str, Any]) -> None:
    """The sole writer for the application-owned ``semantic_ir`` projection."""

    payload["semantic_ir"] = copy.deepcopy(projection)


def _has_plan_meaning(payload: dict[str, Any]) -> bool:
    def has_value(value: Any) -> bool:
        if value is None or value is False or value == "" or value == [] or value == {}:
            return False
        if isinstance(value, dict):
            return any(has_value(child) for child in value.values())
        if isinstance(value, list):
            return any(has_value(child) for child in value)
        return True

    # '근거 있는 resolved' 의 판정 기준은 canonical 오디언스 계약(audience_requirement /
    # event_expression)과 실행 슬롯이다. ``semantic_plan``(의미 노드 그 자체)은 2026-08-05
    # 폐기되어 이 목록에서 빠졌다 — 그 키가 채워질 수 있는 생산자가 남아 있지 않다.
    return any(
        has_value(payload.get(key))
        for key in (
            "target_user", "exclude", "campaign_constraints", "aggregation_request",
            "set_expressions", "condition_evaluations",
            "audience_requirement", "event_expression",
        )
    )


def validate_semantic_ir(
    semantic_ir: Any,
    literal_bindings: Any,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # 필드, 중첩 객체, enum, 타입은 wire 선언 한 곳에서 파싱한다. 이 함수는 그 뒤의
    # literal 교차 참조와 상태 불변식만 검증한다.
    semantic_ir = parse_semantic_outcome_projection(semantic_ir)
    status = semantic_ir["status"]

    literal_items = literal_bindings if isinstance(literal_bindings, list) else []
    literal_by_id = {
        item.get("id"): item
        for item in literal_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(literal_by_id) != len(literal_items):
        raise ValueError("literal_bindings must contain unique object IDs")

    for index, operation in enumerate(semantic_ir["operations"]):
        bindings = operation["bindings"]
        by_role: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            role, literal_id = binding["role"], binding["literal_id"]
            if role in by_role:
                raise ValueError(f"semantic_ir.operations[{index}] contains duplicate role {role}")
            literal = literal_by_id.get(literal_id)
            if literal is None:
                raise ValueError(f"semantic_ir.operations[{index}] references unknown literal {literal_id}")
            expected_kind = {
                "baseline": "date_window",
                "current": "date_window",
                "threshold": "percentage",
                "comparison": "comparison_operator",
            }.get(str(role))
            if expected_kind is None or literal.get("kind") != expected_kind:
                raise ValueError(f"semantic_ir.operations[{index}].{role} has the wrong literal kind")
            by_role[str(role)] = literal
        if not {"baseline", "current"} <= set(by_role):
            raise ValueError(f"semantic_ir.operations[{index}] requires baseline and current date literals")
        if ("threshold" in by_role) != ("comparison" in by_role):
            raise ValueError(f"semantic_ir.operations[{index}] requires threshold and comparison together")
        if by_role["baseline"]["id"] == by_role["current"]["id"]:
            raise ValueError(f"semantic_ir.operations[{index}] requires two distinct periods")

    missing_fields = semantic_ir["missing_fields"]
    policy_applications = semantic_ir["policy_applications"]
    unsupported = semantic_ir["unsupported_operations"]
    validate_semantic_outcome_state(
        status=status,
        missing_fields=missing_fields,
        missing_field_causes=semantic_ir["missing_field_causes"],
        policy_applications=policy_applications,
        unsupported_operations=unsupported,
    )
    if status in {"needs_clarification", "unsupported"} and semantic_ir["operations"]:
        raise ValueError(f"{status} cannot contain executable operations")
    if status in {"resolved", "policy_applied"} and not semantic_ir["operations"]:
        if payload is None or not _has_plan_meaning(payload):
            raise ValueError(f"{status} requires an operation or another grounded plan condition")
    return copy.deepcopy(semantic_ir)


# `materialize_semantic_operations` 는 2026-08-02 삭제됐다 — LLM 이 낸 semantic_ir 연산을
# metric_trend 실행 슬롯으로 직접 투영하던 함수다. 그 함수를 되살리면 의미의 소유자가 다시
# LLM 이 된다.


# 결핍 사후 삭제(`drop_satisfied_missing_fields`)는 2026-08-02 제거됐다. 그 함수는 "LLM 이 만든
# 결핍 보고 중 이미 채워진 것"을 걷는 sweep 이었고, 존재 이유는 결핍의 소유자가 LLM 이었다는
# 점 하나였다. 결핍은 이제 애플리케이션이 canonical 오디언스 계약에서 판정하므로(2026-08-05
# SemanticPlan 폐기 이후에도 같다) 걷어낼 stale 이 구조적으로 생기지 않는다.
