"""자연어 → 조건 IR. **절 단위**로 읽어 '무엇/있나없나/얼마나/언제'를 한 노드 묶음에 함께 붙인다.

이 모듈이 고치는 것은 낱말 누락이 아니라 **추출 순서**다. 예전 경로는 문장 전체에서

  1. 시간 표현을 전역 목록으로 뽑고,
  2. 구매 긍정/부정을 별도 정규식으로 뽑은 뒤,
  3. 가장 가까운 것끼리 나중에 짝지었다.

짝짓기는 문장에 조건이 하나일 때만 맞는다. '상반기 구매 있음 + 하반기 구매 없음'처럼 극성이 다른
두 창이 있으면 한쪽 창이 반대 극성에 붙거나 주인을 잃는다. 여기서는 문장을 먼저 절로 나누고
**그 절 안에서만** 시간을 읽는다 — 시간은 자기 절의 조건에 직접 귀속되므로 짝짓기 단계가 아예 없다.

    문장 → (OR 경계로 분할) → (AND 경계로 분할) → 절마다 조건 노드 → 논리식

절 하나가 만드는 것은 **문장 유형별 타입이 아니라 범용 노드의 조합**이다(:mod:`event_ir` 참조):

    '하반기 구매 없음'      → Not(Exists(Filter(Source("purchase"), TimeFilter(…))))
    '최근 30일 3회 이상 구매' → Comparison(">=", Aggregate("count", Filter(…)), Literal(3))
    '첫 구매 후 30일 이내 재구매' → TemporalRelation("within_after", …)

그래서 새 문장 유형은 새 조합이지 새 타입이 아니다. 업무 명사(구매/로그인/가입/환불)는 IR 타입이
아니라 :data:`event_compiler.EVENT_REGISTRY` 의 심볼이고, 이 파일이 아는 것은 그 심볼의 **표면어**뿐이다
(사전 ``event_alias_<event>``). 새 사건은 사전 어휘 + 레지스트리 한 줄로 열린다.

낱말은 전부 :mod:`lexicon_patterns` 사전이 소유하고 이 파일에는 조합 구조만 있다. 기간 문법은
:mod:`calendar_window` 가 단일 소유자다 — 여기서 달력을 다시 구현하지 않는다.

정규식의 역할은 여기서도 **보조**다: 절 경계 후보와 도메인 별칭 정규화까지이고, 최종 의미(극성·기간
귀속·논리 구조)는 절 구조가 정한다. LLM 파서를 쓰는 경우에도 산출물은 같은 :mod:`event_ir` 스키마를
통과해야 하며(:func:`parse_llm_expression`), 자유 형식 JSON 은 받지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import calendar_window
import event_compiler
import event_ir
import lexicon_patterns
import targeting_ir
from event_ir import (
    AbsoluteInterval,
    Aggregate,
    Comparison,
    Condition,
    Duration,
    EventReference,
    Evidence,
    FieldRef,
    Literal,
    RelativeWindow,
    RollingWindow,
    Source,
    TemporalRelation,
    TimeWindow,
    conjunction,
    disjunction,
    event_relation,
    existence,
)

# ── 어휘(사전 소유) ────────────────────────────────────────────────────────────────
# 빈 교대(`(?:)`)는 **모든 위치에 매치**한다 — 사전 키가 없을 때 조용히 '전부 경계/전부 부정'이 되는
# 사고를 막으려고, 낱말이 하나도 없으면 절대 매치되지 않는 패턴으로 바꾼다(fail-close).
_NEVER = r"(?!)"


def _alt(*vocabularies: str) -> str:
    return lexicon_patterns.alternation(*vocabularies) or _NEVER


_OR_ALT = _alt("or_connective")
_AND_ALT = _alt("and_connective")
_VERB_STEM_ALT = _alt("clause_verb_stem")
_MEMBER_ALT = _alt("member_noun", "member_noun_informal", "member_noun_role")
_SCOPE_ALT = _alt("clause_scope_marker")
_RECORD_ALT = _alt("event_record_noun")
_EXISTS_ALT = _alt("event_existence_marker")
_NEG_ALT = _alt("event_negation_marker")
_COUNT_UNIT_ALT = _alt("event_count_unit")
_CURRENCY_ALT = _alt("currency_unit")
_SUM_ALT = _alt("aggregate_sum_marker")
_AFTER_ALT = _alt("temporal_after_marker")
_WITHIN_ALT = _alt("temporal_within_marker")
_FIRST_ALT = _alt("first_occurrence_marker")
# 비교어 → 부등호 매핑은 targeting_ir 이 단일 소유한다(정규식과 IR 정규화가 같은 표를 쓴다).
_OPERATOR_ALT = "|".join(
    re.escape(word) for word in sorted(targeting_ir.COMPARISON_WORD_OPERATORS, key=len, reverse=True)
)

# OR 이 AND 보다 느슨하다(먼저 분할). '상반기 구매했고 하반기 미구매거나 최근 미접속'은
# (A AND B) OR C 이지 A AND (B OR C) 가 아니다.
_OR_BOUNDARY_RE = re.compile(rf"(?:{_OR_ALT})")
# AND 경계 셋: 범위 표지('… 고객 중'), 용언 어미형('구매했고'), 접속어('이고','그리고').
# 어미형은 **어간 뒤**에서 끊는다(tail 그룹) — '구매하지 않았거나'의 '않았'처럼 극성을 나르는 어간이
# 경계에 먹히면 왼쪽 절이 긍정으로 뒤집힌다.
_AND_BOUNDARY_RE = re.compile(
    rf"(?:{_MEMBER_ALT})\s*(?P<scope>{_SCOPE_ALT})"
    rf"|(?:{_VERB_STEM_ALT})(?P<tail>고)(?=\s)"
    rf"|(?P<conn>{_AND_ALT})"
)
_NEGATION_RE = re.compile(rf"(?:{_NEG_ALT})")
_POLARITY_END_RE = re.compile(rf"(?:{_NEG_ALT}|{_EXISTS_ALT})(?:\S*)")
# 집계 임계: '<수><단위> <비교어>'. 단위·비교어는 사전/레지스트리가 소유하고 구조만 여기 있다.
_COUNT_THRESHOLD_RE = re.compile(rf"(?P<num>\d+)\s*(?:{_COUNT_UNIT_ALT})\s*(?P<op>{_OPERATOR_ALT})")
_AMOUNT_THRESHOLD_RE = re.compile(rf"(?P<num>\d+)\s*(?:{_CURRENCY_ALT})\s*(?P<op>{_OPERATOR_ALT})")
_SUM_CONTEXT_RE = re.compile(rf"(?:{_SUM_ALT})")
# 다른 도메인의 날짜 앵커가 절 안에 있으면 그 시점이 이 사건의 것이라고 단정할 수 없다(fail-close).
# graph_rag 의 고아 창 귀속과 같은 기준을 쓴다 — 판정 기준이 갈라지면 같은 표현이 경로마다 달라진다.
_OTHER_DATE_ANCHOR_RE = re.compile(rf"(?:{_alt('non_event_date_anchor')})")
# 시간 관계: '<사건A> 후 <N><단위> 이내 … <사건B>'. 사건 표면어는 아래 _event_term_patterns 가 소유한다.
_DURATION_UNIT_ALT = "|".join(
    re.escape(unit) for unit in sorted({**calendar_window.KO_UNIT_TO_CANON, "시간": "hour"}, key=len, reverse=True)
)
_TEMPORAL_RE = re.compile(
    rf"(?:{_AFTER_ALT})\s*(?P<num>\d+)\s*(?P<unit>{_DURATION_UNIT_ALT})\s*(?:{_WITHIN_ALT})"
)
_KO_DURATION_UNITS = {**calendar_window.KO_UNIT_TO_CANON, "시간": "hour"}


def _event_term_patterns() -> dict[str, re.Pattern[str]]:
    """사건 심볼 → 표면어 패턴. 어휘 이름은 ``event_alias_<event>`` 규약이라 새 사건은 사전 한 줄이면 된다.

    '구매 기록/구매 이력/구매 내역'의 차이는 **꼬리 명사**일 뿐이므로 구조로 흡수한다 — 표현형마다
    낱말을 늘리는 대신 (동사 별칭 × 기록 명사) 조합이 자동으로 열린다. 긍정형과 부정형이 서로 다른
    명사 목록을 갖는 사고는 두 극성이 이 **한 패턴**을 공유해서 구조적으로 막는다.
    """
    patterns: dict[str, re.Pattern[str]] = {}
    for event in sorted(event_compiler.EVENT_REGISTRY):
        alias_alt = lexicon_patterns.alternation(f"event_alias_{event}")
        if not alias_alt:
            continue  # 별칭 어휘가 없는 사건은 자연어로 부를 수 없다(레지스트리에만 존재)
        patterns[event] = re.compile(rf"(?:{alias_alt})(?:\s*(?:{_RECORD_ALT}))?")
    return patterns


# ── 절 분할 ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Clause:
    """원문의 한 조각과 그 절대 위치. 시간·사건은 이 안에서만 읽는다."""

    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def _split_on(text: str, start: int, pattern: re.Pattern[str]) -> list[Clause]:
    """경계 패턴으로 자른 절 목록(경계 문구 자체는 어느 절에도 넣지 않는다).

    경계의 **시작**은 그 종류가 정한다: 범위 표지('… 고객 중')는 앞의 회원 명사를, 용언 어미형
    ('구매했 + 고')은 어간을 왼쪽 절에 남긴다. 이 구분이 없으면 극성을 나르는 어간이 경계에 먹힌다.
    """
    pieces: list[Clause] = []
    cursor = 0
    for match in pattern.finditer(text):
        groups = match.groupdict()
        boundary_start = next(
            (match.start(name) for name in ("scope", "tail") if groups.get(name)), match.start()
        )
        if boundary_start <= cursor:
            continue
        pieces.append(Clause(text[cursor:boundary_start], start + cursor))
        cursor = match.end()
    pieces.append(Clause(text[cursor:], start + cursor))
    return [piece for piece in pieces if piece.text.strip()]


def split_clauses(text: str) -> tuple[str, list[Clause]]:
    """문장을 논리 구조로 나눈다 → (최상위 연산자, 절 목록).

    최상위가 OR 이면 각 OR 피연산자는 다시 AND 로 나뉜다(:func:`parse_expression` 이 재귀 처리).
    """
    or_pieces = _split_on(text, 0, _OR_BOUNDARY_RE)
    if len(or_pieces) > 1:
        return "or", or_pieces
    return "and", _split_on(text, 0, _AND_BOUNDARY_RE)


# ── 절 안의 조각 읽기 ─────────────────────────────────────────────────────────────


def _clause_windows(clause: Clause, today: date | None) -> list[TimeWindow]:
    """절 안의 기간 표현 → 시간 창 목록(문법 소유자는 calendar_window).

    한 절이 여러 구간을 가리키는 나열('2018, 2019년')은 구간마다 별도 창이 된다 — 하나로 뭉개면
    나머지가 조용히 사라지고, 사이 기간까지 포함하는 한 구간으로 합치면 없는 기간이 딸려 들어온다.
    'N단위 전'은 절대 구간으로 미리 굳히지 않고 :class:`RelativeWindow` 로 남긴다(원문에 더 가깝고,
    확정은 컴파일러가 기준일로 한다).
    """
    relative = next(iter(calendar_window.past_point_matches(clause.text)), None)
    if relative is not None and _OTHER_DATE_ANCHOR_RE.search(clause.text) is None:
        unit = event_ir.canonical_unit(calendar_window.KO_UNIT_TO_CANON.get(relative.group("unit"), ""))
        if unit is not None:
            return [RelativeWindow(value=int(relative.group("num")), unit=unit)]

    windows = calendar_window.parse_time_windows(
        clause.text, today=today,
        allow_relative_past=_OTHER_DATE_ANCHOR_RE.search(clause.text) is None,
        include_duration=True,
    )
    out: list[TimeWindow] = []
    for window in windows:
        days = window.get("days")
        if isinstance(days, int) and days > 0:
            out.append(RollingWindow(value=days, unit="day"))
            continue
        interval = AbsoluteInterval.from_calendar_window(window)
        if interval is not None:
            out.append(interval)
    return out


def _clause_time_span(clause: Clause, today: date | None) -> tuple[int, int] | None:
    span = calendar_window.parse_time_window_group_span(
        clause.text, today=today, allow_relative_past=_OTHER_DATE_ANCHOR_RE.search(clause.text) is None
    )
    if span is not None:
        return span
    match = calendar_window.NUMERIC_DURATION_PATTERN.search(clause.text.replace(" ", ""))
    if match is None:
        return None
    offsets = [index for index, char in enumerate(clause.text) if char != " "]
    start, end = match.span()
    if end > len(offsets):
        return None
    return offsets[start], offsets[end - 1] + 1


def _clause_event(clause: Clause, event_terms: dict[str, re.Pattern[str]]) -> tuple[int, int, str] | None:
    """절의 사건 심볼과 그 표면어 구간. 여럿이면 서술어에 가장 가까운 것(마지막)이 그 절의 사건이다."""
    hits = [
        (match.start(), match.end(), event)
        for event, pattern in event_terms.items()
        for match in pattern.finditer(clause.text)
    ]
    return max(hits, key=lambda hit: (hit[0], hit[1])) if hits else None


def _clause_evidence(clause: Clause, event_span: tuple[int, int], time_span: tuple[int, int] | None) -> Evidence:
    start = min(event_span[0], time_span[0]) if time_span else event_span[0]
    ends = [event_span[1], *(match.end() for match in _POLARITY_END_RE.finditer(clause.text) if match.end() > event_span[0])]
    end = max(ends)
    return Evidence(text=clause.text[start:end].strip(), start=clause.start + start, end=clause.start + end)


def _aggregate_condition(
    clause: Clause, event: str, evidence: Evidence, windows: list[TimeWindow]
) -> Condition | None:
    """'N회 이상 구매' / '구매 금액 합계 X원 이상' → ``Comparison(Aggregate, Literal)``.

    횟수·합계·평균이 각각의 타입이 아니라 **함수 이름만 다른 같은 노드**다. 새 지표는 필드 레지스트리
    한 줄이고, 새 비교어는 targeting_ir 의 비교어 표 한 줄이다.
    """
    window = windows[0] if windows else None
    fields = event_compiler.resolve_fields(event_compiler.EVENT_REGISTRY)

    amount = _AMOUNT_THRESHOLD_RE.search(clause.text)
    if amount is not None and _SUM_CONTEXT_RE.search(clause.text) is not None:
        field_name = f"{event}.amount"
        if field_name in fields:
            return Comparison(
                operator=targeting_ir.COMPARISON_WORD_OPERATORS[amount.group("op")],
                left=Aggregate(
                    function="sum", relation=event_relation(event, window), expression=FieldRef(name=field_name)
                ),
                right=Literal(value=int(amount.group("num"))),
                evidence=evidence,
            )

    count = _COUNT_THRESHOLD_RE.search(clause.text)
    if count is None:
        return None
    # 주문 식별자가 등록돼 있으면 '몇 번 샀나'는 주문 단위 distinct 다(같은 주문의 여러 라인 = 1회).
    key_field = f"{event}.order_id"
    expression = FieldRef(name=key_field) if key_field in fields else None
    return Comparison(
        operator=targeting_ir.COMPARISON_WORD_OPERATORS[count.group("op")],
        left=Aggregate(
            function="count", relation=event_relation(event, window),
            expression=expression, distinct=expression is not None,
        ),
        right=Literal(value=int(count.group("num"))),
        evidence=evidence,
    )


def _temporal_condition(
    clause: Clause, event_terms: dict[str, re.Pattern[str]]
) -> Condition | None:
    """'<A> 후 <N><단위> 이내 <B>' → :class:`TemporalRelation`.

    소스 이름만 바뀌면 '가입 후 7일 이내 구매'·'배송 후 30일 이내 반품'·'쿠폰 발급 후 14일 이내 사용'이
    같은 노드로 표현된다 — 문장마다 타입을 만들지 않는 이유가 이것이다.
    """
    marker = _TEMPORAL_RE.search(clause.text)
    if marker is None:
        return None
    unit = event_ir.canonical_unit(_KO_DURATION_UNITS.get(marker.group("unit"), ""))
    if unit is None:
        return None
    before = [
        (match.start(), event)
        for event, pattern in event_terms.items()
        for match in pattern.finditer(clause.text[: marker.start()])
    ]
    after = [
        (match.start(), event)
        for event, pattern in event_terms.items()
        for match in pattern.finditer(clause.text[marker.end():])
    ]
    if not before or not after:
        return None
    anchor_start, anchor_event = max(before, key=lambda hit: hit[0])
    _target_start, target_event = min(after, key=lambda hit: hit[0])
    selector = "first" if re.search(rf"(?:{_FIRST_ALT})\s*$", clause.text[:anchor_start]) else "first"
    end = marker.end() + max(match.end() for pattern in event_terms.values() for match in pattern.finditer(clause.text[marker.end():]))
    return TemporalRelation(
        operator="within_after",
        left=EventReference(source=anchor_event, selector=selector),
        right=EventReference(source=target_event, selector="subsequent"),
        duration=Duration(value=int(marker.group("num")), unit=unit),
        evidence=Evidence(
            text=clause.text[:end].strip(), start=clause.start, end=clause.start + end
        ),
    )


def _clause_conditions(
    clause: Clause, event_terms: dict[str, re.Pattern[str]], today: date | None
) -> list[Condition]:
    """절 하나 → 조건 노드(들). 사건어가 없으면 빈 목록(다른 도메인의 절이다)."""
    temporal = _temporal_condition(clause, event_terms)
    if temporal is not None:
        return [temporal]

    hit = _clause_event(clause, event_terms)
    if hit is None:
        return []
    event_start, event_end, event = hit

    windows = _clause_windows(clause, today)
    evidence = _clause_evidence(clause, (event_start, event_end), _clause_time_span(clause, today))

    aggregate = _aggregate_condition(clause, event, evidence, windows)
    if aggregate is not None:
        return [aggregate]

    negated = _NEGATION_RE.search(clause.text) is not None
    if not windows:
        return [existence(event, negated=negated, evidence=evidence)]
    # 같은 절의 여러 구간은 극성에 따라 결합이 다르다 — '2018, 2019년에 구매'는 둘 중 **하나라도**
    # 있으면 참(OR)이고, '2018, 2019년에 구매 없음'은 둘 **다** 없어야 참(AND)이다(드모르간).
    conditions = [
        existence(event, negated=negated, window=window, evidence=evidence) for window in windows
    ]
    if len(conditions) == 1:
        return conditions
    return [conjunction(conditions) if negated else disjunction(conditions)]


# ── 진입점 ────────────────────────────────────────────────────────────────────────


def parse_expression(text: str, *, today: date | None = None) -> Condition | None:
    """자연어 → 조건 논리식(사건 조건이 하나도 없으면 None).

    ``today`` 는 상대 연도('올해/작년')와 롤링 창의 기준일이다 — 호출자가 주입해 테스트가 달력에
    의존하지 않게 한다(calendar_window 와 같은 관례).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    event_terms = _event_term_patterns()
    operator, clauses = split_clauses(text)

    if operator == "or":
        operands: list[Condition] = []
        for clause in clauses:
            inner = [
                condition
                for and_clause in _split_on(clause.text, clause.start, _AND_BOUNDARY_RE)
                for condition in _clause_conditions(and_clause, event_terms, today)
            ]
            if inner:
                operands.append(conjunction(inner))
        return disjunction(operands) if operands else None

    conditions = [
        condition for clause in clauses for condition in _clause_conditions(clause, event_terms, today)
    ]
    return conjunction(conditions) if conditions else None


# ── 검증용 근거 텍스트 ─────────────────────────────────────────────────────────────


def _event_clauses(text: str) -> list[Clause]:
    event_terms = _event_term_patterns()
    _operator, clauses = split_clauses(text or "")
    return [
        sub
        for clause in clauses
        for sub in _split_on(clause.text, clause.start, _AND_BOUNDARY_RE)
        if any(pattern.search(sub.text) for pattern in event_terms.values())
    ]


def event_clause_text(text: str) -> str:
    """사건어가 들어 있는 절만 이어 붙인 문자열.

    의미 보존 검증(부정 소실 탐지)의 대상 범위다. 문장 전체를 넘기면 다른 도메인의 부정
    ('VIP 등급은 제외')까지 사건 부정으로 세어 멀쩡한 해석을 fail-close 시킨다.
    """
    return " ".join(clause.text for clause in _event_clauses(text))


def source_time_span_count(text: str, *, today: date | None = None) -> int:
    """원문의 사건 절이 담은 기간 표현 수(시간 소실 탐지의 기준값).

    IR 이 아니라 **달력 파서**가 직접 센다 — 같은 코드로 세면 놓친 창을 둘 다 못 본다. 시간 관계 절
    ('A 후 30일 이내 B')의 기간은 창이 아니라 관계의 폭이므로 세지 않는다(IR 에도 TimeFilter 가 없다).
    """
    event_terms = _event_term_patterns()
    return sum(
        len(_clause_windows(clause, today))
        for clause in _event_clauses(text)
        if _temporal_condition(clause, event_terms) is None
    )


# ── LLM 경로(같은 스키마 강제) ─────────────────────────────────────────────────────


def parse_llm_expression(payload: Any) -> Condition:
    """LLM 구조화 출력 → IR. 스키마 위반은 :class:`event_ir.IrSchemaError` 로 거부한다.

    자유 형식 JSON 을 받지 않는 이유는 규칙 파서와 같다 — 무엇이 사건이고 어느 기간이 어느 조건의
    것인지는 **스키마가 강제**해야 하고, 나중에 짝지으면 그때 또 틀린다.
    """
    return event_ir.condition_from_dict(payload)


def llm_tool_schema() -> dict[str, Any]:
    """LLM 도구 스키마 조각(자유 JSON 금지). 정의는 :mod:`event_ir` 에서 파생한다."""
    return {
        "type": "object",
        "properties": {"expression": event_ir.condition_json_schema()},
        "required": ["expression"],
    }
