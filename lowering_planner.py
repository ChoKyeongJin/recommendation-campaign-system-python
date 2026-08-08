"""지원 여부의 단일 판정자 — **이름이 아니라 실제 lowering 가능성**.

이 모듈이 답하는 질문은 하나다: *이 원문 요구를 지금 있는 실행 primitive 로 낮출 수 있는가.*
답하는 방식이 이 모듈의 존재 이유다 — 목록을 조회하지 않고 **canonical 표현을 실제로 만들어
검증하고 컴파일해 본다**. 컴파일되면 지원되는 것이고, 안 되면 아닌 것이다. 그 사이에 사람이
관리하는 allowlist 가 없다.

왜 이렇게 만들었나(실측 2026-08-07, `2026년 2월과 3월의 구매금액이 증가한 회원` 12회 반복)::

    성공 SQL 생성                     3회
    semantic_ir_unsupported           6회
    semantic_registry_gap             3회   ← 사용자가 본 문구
    ambiguous_requirement             1회

같은 코드·같은 원문인데 귀결이 넷이었다. 원인은 지원 여부의 판정 근거가 **모델이 지어낸
argument 문자열**이었기 때문이다. 모델은 그 자리를 매번 다르게 불렀고(`metric_transition`,
`month_to_month_change`, `temporal_comparison_between_monthly_metrics`,
`monthly_purchase_amount_transition`, `month_over_month_increase`, `comparison`), 그 이름에
``transition`` 이라는 글자가 들어갔는지 / 근거 구간에 ``구매금액`` 이라는 표면어가 들어갔는지에
따라 사용자 문구가 갈렸다. 정작 그 요구는 ``aggregate.scalar`` 두 개와 비교 하나로 이미
컴파일된다 — 12회 중 3회는 실제로 정확한 SQL 이 나왔다.

그러므로 불변식은 이것이다.

    지원 여부는 semantic label 이름이나 surface text matching 으로 결정하지 않는다.
    canonical expression 을 실제 execution primitive 로 lowering 할 수 있는지로 결정한다.

이 모듈은 **판정과 계획만** 한다 — 사용자 문구를 만들지 않고, plan 을 변형하지 않으며,
모델이 쓴 어떤 문자열도 읽지 않는다. 입력은 원문·카탈로그·달력 문법 셋뿐이다.

실패는 전부 fail-safe 다: 카탈로그를 못 읽거나 어휘가 없거나 컴파일이 깨지면 **의무 없음**으로
답한다. 없는 지원을 있다고 말하면 옳은 미지원 신고까지 반박해 재시도만 반복하게 된다.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import lexicon_patterns

# 이 모듈이 캡처하는 의무의 종류 이름. 소비자(반박·종결 판정)가 문자열을 다시 적지 않도록 한다.
AGGREGATE_COMPARISON = "aggregate_comparison"
# 시점·이력 의무의 종류 이름은 의무 원장이 소유한다 — 여기서 다시 적으면 두 곳이 갈라진다.
# (:data:`semantic_requirements.TEMPORAL_QUALIFIER_KIND` 와 같아야 하고, 드리프트는 테스트가 고정한다.)
MEMBER_STATE_HISTORY = "member_state_history"

# 증감 표현의 방향 → canonical 비교 연산자. **나중 창이 왼쪽**일 때의 연산자다.
# ('3월이 2월보다 증가' = later > earlier)
_INCREASE = ">"
_DECREASE = "<"


@dataclass(frozen=True)
class WindowOperand:
    """비교 한쪽 — 카탈로그 지표 하나를 확정된 반개구간 ``[start, end_exclusive)`` 에서 집계한 것."""

    metric_id: str
    window_start: date
    window_end_exclusive: date
    window_label: str
    source_span: tuple[int, int]


@dataclass(frozen=True)
class ComparisonObligation:
    """두 집계 피연산자의 비교. **이름이 아니라 구조**가 이 의무의 정체다.

    ``kind`` 는 소비자가 종류를 구별하기 위한 라벨일 뿐 판정 근거가 아니다 — 판정은
    :func:`try_plan` 이 이 구조를 실제로 낮춰 보고 답한다.

    ``threshold`` 는 **변화의 크기**다(있을 때만). 없으면 방향만 있는 맨 비교이고, 있으면
    그 크기까지 낮춰야 이 의무를 다 읽은 것이다. 이 자리가 비어 있던 동안 '10% 이상 증가'와
    '증가'가 **같은 SQL** 로 컴파일됐다(실측: 바이트 동일). 크기를 의무에 싣는 것이
    :attr:`satisfied_requirement_ids` 로 "다 읽었다"를 증명할 수 있게 하는 전제다.
    """

    left: WindowOperand
    operator: str
    right: WindowOperand
    source_text: str
    source_span: tuple[int, int]
    threshold: Any = None  # semantic_requirements.ChangeMagnitude | None
    kind: str = AGGREGATE_COMPARISON


@dataclass(frozen=True)
class TemporalStateObligation:
    """속성의 시점·이력 조건 하나('지난달 말 기준 …'·'A에서 B로 …'·'N개월 내내 …').

    캡처는 의무 원장(:mod:`semantic_requirements`)이 이미 한다. 여기서 표면어를 다시 읽지
    않는 이유는 :class:`ComparisonObligation` 과 같다 — 판정 근거는 이름이 아니라 이 구간을
    실제로 낮출 수 있는가이고, 그 답은 :func:`try_plan` 이 낮춰 보고 내놓는다.
    """

    source_text: str
    source_span: tuple[int, int]
    kind: str = MEMBER_STATE_HISTORY


Obligation = ComparisonObligation | TemporalStateObligation


@dataclass(frozen=True)
class LoweringPlan:
    """의무를 낮춘 결과. **계획의 존재만으로는 지원의 증명이 아니다.**

    예전 계약은 "이 객체가 있으면 그 자리는 지원됨"이었고, 그것이 이 저장소의 조용한 의미
    손실 한 부류를 통째로 만들었다 — 계획의 ``source_span`` 은 표지들의 min/max hull 이라
    ``10% 이상`` 처럼 **구조가 읽지 않은 텍스트까지 덮는데**, 소비자는 그 겹침만 보고 모델의
    정직한 미지원 신고를 거짓이라고 단언했다. 근거(span)를 의미(support)로 착각한 것이다.

    그래서 계획은 자기가 **무엇을 소비했는지** 함께 들고 다닌다. :attr:`satisfied_requirement_ids`
    는 이 표현이 실제로 낮춘 source requirement 의 id 다. 지원 판정은 겹침이 아니라 이 집합과
    hull 안 요구 전체의 정산으로 답한다(:func:`plan_satisfying_span`).
    """

    obligation: Obligation
    expression: Any  # event_ir.Condition — 실제로 만들어져 검증을 통과한 canonical 표현
    capabilities: frozenset[str]
    sql: str
    # 이 계획이 실제로 소비한 source requirement id. 비어 있을 수 있다(요구가 없는 맨 비교).
    satisfied_requirement_ids: frozenset[str] = frozenset()


# ── 표면 캡처(원문만 읽는다) ──────────────────────────────────────────────────────────


def _alternation(name: str) -> str:
    """어휘 교대 문자열. **비어 있으면 빈 문자열** — 호출자가 패턴을 만들지 않도록 한다.

    빈 교대를 정규식에 넣으면 ``(?:)`` 가 되어 아무 위치에서나 매치된다. 어휘 파일이 없을 때
    조용히 모든 문장을 증감 비교로 읽는 것이 최악이므로 여기서 끊는다.
    """
    try:
        return lexicon_patterns.alternation(name)
    # 사전을 못 읽으면 캡처하지 않는다(추측 금지).
    except Exception:
        return ""


@functools.lru_cache(maxsize=1)
def _direction_pattern() -> re.Pattern[str] | None:
    """증감 방향 표지(자립형). 어휘가 하나도 없으면 None(캡처 안 함).

    '증가/감소' 계열만 자립형이다. '큰/많은' 은 :func:`_relative_direction_pattern` 으로 따로
    본다 — 그 낱말은 순위 표현('구매금액이 큰 순으로')에도 그대로 쓰여서, 창 두 개가 있다는
    이유만으로 비교 의무로 읽으면 순위 요청을 비교로 오인한다.
    """
    increase, decrease = _alternation("trend_increase"), _alternation("trend_decrease")
    if not increase or not decrease:
        return None
    return re.compile(f"(?P<increase>{increase})|(?P<decrease>{decrease})")


@functools.lru_cache(maxsize=1)
def _relative_direction_pattern() -> re.Pattern[str] | None:
    """대소 표지('큰'·'적은'). **비교 표지가 같은 문장에 있을 때만** 방향으로 읽는다."""
    high, low = _alternation("direction_high"), _alternation("direction_low")
    if not high or not low:
        return None
    return re.compile(f"(?P<increase>{high})|(?P<decrease>{low})")


@functools.lru_cache(maxsize=1)
def _comparative_marker_pattern() -> re.Pattern[str] | None:
    """'A보다 B' 어순 표지. 없으면 None — 그때는 시간 순서가 어순을 결정한다."""
    marker = _alternation("comparative_marker")
    return re.compile(marker) if marker else None


def _search_direction(query: str, *, allow_relative: bool) -> re.Match[str] | None:
    """방향 표지 하나. 자립형('증가')이 먼저고, 대소형('큰')은 비교 표지가 있을 때만 본다."""
    standalone = _direction_pattern()
    found = standalone.search(query) if standalone is not None else None
    if found is not None or not allow_relative:
        return found
    relative = _relative_direction_pattern()
    return relative.search(query) if relative is not None else None


def _strip_spaces(text: str) -> tuple[str, list[int]]:
    """공백을 뺀 문자열과 각 글자의 원문 인덱스. 표면어의 띄어쓰기 흔들림을 흡수한다.

    카탈로그 별칭은 '구매 금액'인데 원문은 '구매금액'으로 붙여 쓴다(실측). 두 표기를 같은
    것으로 보되 **원문 좌표는 잃지 않아야** 근거 구간을 낼 수 있다.
    """
    compact: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        compact.append(char)
        offsets.append(index)
    return "".join(compact), offsets


def _aggregate_metric_hits(query: str) -> list[tuple[int, int, str]]:
    """원문에 나타난 **집계 가능한** 카탈로그 지표의 (시작, 끝, metric_id).

    같은 자리에 짧은 별칭이 겹치면 긴 쪽만 남긴다 — '최대 구매금액'이 그 접미사인 '구매금액'
    으로 읽히면 다른 지표가 된다(기존 ranking 캡처와 같은 규칙).
    """
    import audience_runtime  # 지연 import — 판정자는 카탈로그 로딩을 import 부작용으로 만들지 않는다

    try:
        catalog = audience_runtime.resolve_audience_catalog()
    # 카탈로그를 못 읽으면 캡처하지 않는다(추측 금지).
    except Exception:
        return []

    compact_query, offsets = _strip_spaces(query)
    hits: list[tuple[int, int, str]] = []
    for metric_id, spec in catalog.metrics.items():
        if getattr(spec, "kind", "") != "aggregate":
            continue
        surfaces = [metric_id, *(getattr(spec, "aliases", ()) or ())]
        for surface in surfaces:
            compact_surface, _ = _strip_spaces(str(surface))
            if len(compact_surface) < 2:
                continue
            start = compact_query.find(compact_surface)
            while start >= 0:
                end = start + len(compact_surface)
                hits.append((offsets[start], offsets[end - 1] + 1, metric_id))
                start = compact_query.find(compact_surface, start + 1)
    return [
        hit
        for hit in hits
        if not any(
            other[0] <= hit[0] and hit[1] <= other[1] and (other[1] - other[0]) > (hit[1] - hit[0])
            for other in hits
        )
    ]


def _date8(value: str) -> date:
    return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))


def detect_comparison_obligations(
    query: str, *, today: date | None = None
) -> tuple[ComparisonObligation, ...]:
    """원문에서 '두 기간의 같은 지표 비교' 의무를 **구조로** 캡처한다.

    표지는 셋뿐이고 전부 원문에서 나온다.

        1. 서로 다른 두 개 이상의 달력 창(:mod:`calendar_window` 문법)
        2. 집계 가능한 카탈로그 지표 하나
        3. 증감/대소 방향 낱말 하나(사전 소유)

    이 셋이 한 문장에 있으면 요구는 '이 지표를 두 창에서 각각 집계해 비교하라'다. 모델이 그
    자리를 무엇이라 부르든 캡처는 달라지지 않는다.

    어순은 두 규칙으로 결정한다. 'A보다 B' 표지가 있으면 표지 **바로 앞** 창이 기준(오른쪽)
    이고 나머지가 왼쪽이다. 표지가 없으면 **시간 순서**가 어순이다 — '2월과 3월의 …이 증가한'
    은 나중 창(3월)이 왼쪽이다.
    """
    if not isinstance(query, str) or not query.strip():
        return ()

    import calendar_window  # 지연 import — 달력 문법 로딩을 import 부작용으로 만들지 않는다

    try:
        scanned = calendar_window.parse_calendar_window_spans(query, today=today)
    # 창을 못 읽으면 캡처하지 않는다(추측 금지).
    except Exception:
        return ()
    windows = [
        (window, start, end)
        for window, start, end in scanned
        if isinstance(window, dict) and window.get("from") and window.get("to")
    ]
    if len(windows) < 2:
        return ()

    marker = _comparative_marker_pattern()
    has_marker = marker is not None and marker.search(query) is not None
    direction = _search_direction(query, allow_relative=has_marker)
    if direction is None:
        return ()
    operator = _INCREASE if direction.lastgroup == "increase" else _DECREASE

    metric_hits = _aggregate_metric_hits(query)
    if not metric_hits:
        return ()

    import semantic_requirements  # 지연 import — 원장이 소유한 크기 파싱을 그대로 쓴다

    magnitudes = semantic_requirements.parse_change_magnitudes(query)

    # 같은 지표 표면어가 한 문장에 두 번 나오면('3월 구매금액이 2월 구매금액보다 큰') 같은 의무가
    # 두 번 잡힌다. 세는 것은 표면어가 아니라 **의무**이므로 구조가 같으면 하나로 접는다.
    obligations: dict[tuple[Any, ...], ComparisonObligation] = {}
    for metric_start, metric_end, metric_id in metric_hits:
        chosen = _closest_window_pair(windows, metric_start, metric_end)
        if chosen is None:
            continue
        earlier, later = chosen
        # 기본 어순은 **시간 순서**다: '2월과 3월의 …이 증가한' 은 나중 창이 왼쪽이다.
        left, right = later, earlier
        reference = (
            _window_before_marker(query, list(chosen), marker) if marker is not None else None
        )
        if reference is not None:
            # 'A보다 B' / 'A 대비 B' — 표지 앞 창이 기준(오른쪽)이고 나머지가 왼쪽이다.
            right = reference
            left = earlier if reference is later else later
        span_sources = [
            left, right, (None, metric_start, metric_end), (None, *direction.span()),
        ]
        # 크기가 이 비교에 속하려면 방향 낱말이 그 크기의 것이어야 한다 — 원장이 이미 크기와
        # 방향을 한 요구로 묶어 두었으므로 여기서 어휘를 다시 읽지 않고 그 결과를 받는다.
        threshold = next(
            (
                magnitude
                for magnitude in magnitudes
                if magnitude.source_span[0] <= direction.start()
                and direction.end() <= magnitude.source_span[1]
            ),
            None,
        )
        if threshold is not None:
            span_sources.append((None, *threshold.source_span))
        start = min(item[1] for item in span_sources)
        end = max(item[2] for item in span_sources)
        obligation = ComparisonObligation(
            left=_operand(left, metric_id),
            operator=operator,
            right=_operand(right, metric_id),
            source_text=query[start:end],
            source_span=(start, end),
            threshold=threshold,
        )
        obligations.setdefault(
            (
                obligation.left.metric_id,
                obligation.operator,
                obligation.left.window_start,
                obligation.left.window_end_exclusive,
                obligation.right.window_start,
                obligation.right.window_end_exclusive,
            ),
            obligation,
        )
    return tuple(obligations.values())


def _operand(scanned: tuple[Any, int, int], metric_id: str) -> WindowOperand:
    window, start, end = scanned
    return WindowOperand(
        metric_id=metric_id,
        window_start=_date8(str(window["from"])),
        # 달력 문법의 ``to`` 는 **포함** 끝일이고 IR 구간은 반개구간이다. 이 한 줄이 그 변환의
        # 유일한 자리다 — 두 곳에서 하면 하루가 조용히 어긋난다.
        window_end_exclusive=_date8(str(window["to"])) + timedelta(days=1),
        window_label=str(window.get("label") or ""),
        source_span=(start, end),
    )


def _closest_window_pair(
    windows: list[tuple[Any, int, int]], metric_start: int, metric_end: int
) -> tuple[tuple[Any, int, int], tuple[Any, int, int]] | None:
    """이 지표가 쓰는 두 창(시간 순 오름차순). 서로 다른 구간이 둘 미만이면 None.

    지표 언급 위치에서 가까운 두 창을 고른다 — 한 문장에 절이 여럿일 때 다른 절의 창을 끌어
    오지 않기 위해서다.
    """
    del metric_end  # 근접도는 시작 좌표로 충분하다(창은 지표 앞뒤 어느 쪽에나 올 수 있다).
    unique: dict[tuple[str, str], tuple[Any, int, int]] = {}
    for window, start, end in sorted(
        windows, key=lambda item: abs(item[1] - metric_start)
    ):
        key = (str(window["from"]), str(window["to"]))
        unique.setdefault(key, (window, start, end))
        if len(unique) == 2:
            break
    if len(unique) < 2:
        return None
    first, second = unique.values()
    return tuple(sorted((first, second), key=lambda item: str(item[0]["from"])))  # type: ignore[return-value]


def _window_before_marker(
    query: str, windows: list[tuple[Any, int, int]], marker: re.Pattern[str]
) -> tuple[Any, int, int] | None:
    """'…보다' 표지 바로 앞에서 끝나는 창. 없으면 None(어순 규칙 미적용)."""
    for match in marker.finditer(query):
        candidates = [item for item in windows if item[2] <= match.start()]
        if not candidates:
            continue
        nearest = max(candidates, key=lambda item: item[2])
        # 표지와 창 사이에 다른 절이 통째로 끼면 그 창은 이 비교의 기준이 아니다.
        if match.start() - nearest[2] <= _MARKER_GAP_BUDGET:
            return nearest
    return None


# 창과 'A보다' 표지 사이에 허용하는 글자 수(지표 이름 + 조사 정도). 이보다 벌어지면 다른 절이다.
_MARKER_GAP_BUDGET = 20


# ── 판정(실제로 낮춰 본다) ────────────────────────────────────────────────────────────


def _requirement_ids_at(query: str | None, span: tuple[int, int]) -> frozenset[str]:
    """``span`` 이 덮는 source requirement 의 id. 원장을 못 읽으면 빈 집합(fail-safe).

    계획이 "이 요구를 내가 소비했다"고 말하는 유일한 통로다. 좌표로 찾되 **결과는 id** 라는
    점이 중요하다 — 소비자는 좌표가 아니라 id 로 정산한다.
    """
    if not isinstance(query, str) or not query:
        return frozenset()
    import semantic_requirements  # 지연 import — 판정자는 원장 로딩을 import 부작용으로 만들지 않는다

    try:
        captured = semantic_requirements.capture_source_semantic_obligations(query)
    # 원장을 못 읽으면 아무것도 소비하지 않은 것으로 둔다(추측 금지).
    except Exception:
        return frozenset()
    found = set()
    for requirement in captured:
        bounds = semantic_requirements.requirement_span(requirement)
        if bounds is not None and span[0] <= bounds[0] and bounds[1] <= span[1]:
            found.add(str(requirement.id))
    return frozenset(found)


def try_plan(
    obligation: ComparisonObligation, *, query: str | None = None
) -> LoweringPlan | None:
    """의무를 canonical Event IR 로 낮춘 계획. 낮출 수 없으면 None.

    **여기가 이 모듈의 요점이다.** 지원 여부를 목록에서 조회하지 않고 표현을 실제로 만들어
    IR 검증과 SQL 컴파일을 통과시켜 본다. 지표의 소스가 시간 축을 갖지 않거나, 필드가 물리
    바인딩에 없거나, 컴파일러가 그 모양을 못 내면 여기서 자연스럽게 실패한다 — 그 실패들을
    미리 열거하는 표를 두지 않아도 된다는 뜻이다.
    """
    import audience_runtime
    import event_compiler
    import event_ir
    import sql_dialect

    try:
        catalog = audience_runtime.resolve_audience_catalog()
    # 카탈로그를 못 읽으면 계획 없음(추측 금지).
    except Exception:
        return None

    spec = catalog.metrics.get(obligation.left.metric_id)
    if spec is None or obligation.left.metric_id != obligation.right.metric_id:
        return None
    if getattr(spec, "kind", "") != "aggregate":
        return None
    # 카탈로그가 지표에 자체 필터·조인을 선언했다면 그 뜻을 여기서 다시 조립할 수 없다.
    # 조용히 무시하면 뜻이 넓어진 SQL 이 나오므로 계획을 세우지 않는다(fail-safe).
    if getattr(spec, "where", None) or getattr(spec, "joins", ()):
        return None
    allowed = getattr(spec, "allowed_operators", ()) or ()
    if allowed and obligation.operator not in allowed:
        return None
    source_spec = catalog.sources.get(getattr(spec, "source", ""))
    # 선언이 자기 시각 컬럼을 한 값으로 고정한 스냅샷 소스에는 기간 창을 걸 수 없다. 컴파일은
    # 성공하고 술어만 자기모순이 되어 **경고 없이 0명**이 되므로, 그 계획은 지원의 증거가 아니라
    # 왜곡의 증거다. 프롬프트가 이미 "(기간 창 불가)"로 광고하는 그 판정을 판정자도 함께 본다.
    if audience_runtime.source_pins_its_time_column(getattr(spec, "source", "")):
        return None
    time_field = getattr(source_spec, "time_field", "") if source_spec is not None else ""
    # 집계 함수·집계 대상·시간 축 중 하나라도 선언돼 있지 않으면 이 지표는 창을 가진 집계로
    # 세울 수 없다. 비워 둔 자리를 추측해 채우면 뜻이 다른 SQL 이 조용히 나온다.
    function, expression_field = spec.aggregate_function, spec.expression_field
    if not time_field or not function or not expression_field:
        return None

    def operand(item: WindowOperand) -> Any:
        return event_ir.Aggregate(
            function=function,
            expression=event_ir.FieldRef(name=expression_field),
            distinct=bool(getattr(spec, "distinct", False)),
            relation=event_ir.Filter(
                relation=event_ir.Source(name=spec.source),
                where=event_ir.TimeFilter(
                    field=event_ir.FieldRef(name=time_field),
                    window=event_ir.AbsoluteInterval(
                        start=item.window_start, end_exclusive=item.window_end_exclusive
                    ),
                ),
            ),
        )

    try:
        evidence = event_ir.Evidence(
            text=obligation.source_text,
            start=obligation.source_span[0],
            end=obligation.source_span[1],
        )
        if obligation.threshold is None:
            expression = event_ir.Comparison(
                operator=obligation.operator,
                left=operand(obligation.left),
                right=operand(obligation.right),
                evidence=evidence,
            )
        else:
            lowered = _lower_change_threshold(
                obligation.threshold,
                left=operand(obligation.left),
                right=operand(obligation.right),
                evidence=evidence,
                event_ir=event_ir,
            )
            if lowered is None:
                # 크기를 못 낮추면 **계획을 세우지 않는다**. 방향만 담은 계획을 내면 그 계획이
                # '이 자리는 지원된다'고 단언하면서 정작 크기는 버린다(조용한 의미 약화).
                return None
            expression = lowered
        capabilities = event_ir.expression_capabilities(expression)
        if event_compiler.unsupported_capabilities(expression):
            return None
        event_ir.validate_evidence(expression)
        context = catalog.compile_context(
            dialect=sql_dialect.get_dialect("tsql"), literals=True
        )
        sql = event_compiler.compile_expression(expression, context=context).sql
    except (event_ir.IrSchemaError, event_compiler.SqlCompileError, KeyError, ValueError):
        # 낮출 수 없다는 **판정**이다(버그가 아니라 도메인 결과). 넓게 잡지 않는 이유는
        # 예상 못 한 오류를 지원 없음으로 위장하지 않기 위해서다.
        return None
    if not sql.strip():
        return None
    return LoweringPlan(
        obligation=obligation,
        expression=expression,
        capabilities=capabilities,
        sql=sql,
        # 임계를 낮췄다면 그 크기 요구를 소비한 것이다. 임계가 없으면 소비할 요구도 없다.
        satisfied_requirement_ids=_requirement_ids_at(
            query, obligation.threshold.source_span
        ) if obligation.threshold is not None else frozenset(),
    )


def _integer_scale(amount: Any) -> tuple[int, int] | None:
    """Decimal → (정수 값, 스케일). ``1234.56`` → ``(123456, 100)``.

    :class:`event_ir.Literal` 은 ``Decimal`` 을 받지 않고(int|float|str|bool), 금액·비율에
    float 를 쓰는 것은 CLAUDE.md §14 위반이다. 그래서 양변을 같은 정수배로 올려 **정수만으로**
    같은 부등식을 만든다. 스케일이 정수로 떨어지지 않으면 낮추지 않는다(추측 금지).
    """
    from decimal import Decimal, InvalidOperation  # 지역 사용

    if not isinstance(amount, Decimal):
        return None
    scale = 1
    for _ in range(9):  # 소수 9자리까지 — 그 이상은 금액·비율 표현이 아니다
        try:
            scaled = (amount * scale).to_integral_exact()
        except (InvalidOperation, ValueError):
            return None
        if scaled == amount * scale:
            return int(scaled), scale
        scale *= 10
    return None


def _lower_change_threshold(
    threshold: Any,
    *,
    left: Any,
    right: Any,
    evidence: Any,
    event_ir: Any,
) -> Any | None:
    """변화 크기를 canonical 표현으로 낮춘다. **나눗셈도 float 도 쓰지 않는다.**

    비율은 교차곱이다 — ``L * denominator OP R * numerator``. 나눗셈형
    ``(L-R)/NULLIF(R,0) >= 0.1`` 은 정수 지표에서 조용히 틀리고(실측 ``(11-10)/10 = 0``),
    Decimal 리터럴은 IR 이 받지 않는다. 절대 증분은 뺄셈 비교다.

    비교의 향은 **방향 낱말**이 정하고 경계 낱말('이상'/'초과')은 포함 여부만 정한다. 이 분리가
    극성 반전을 구조적으로 막는다 — '20% 이상 감소'의 '이상'을 ``>=`` 로 읽으면 뜻이 뒤집힌다.

    기준값 0 은 명시적으로 막는다(``right > 0``). ``ISNULL(SUM,0)`` 때문에 2월 무주문 회원은
    ``0 >= 0 * 11/10`` 이 참이 되어 **비구매자 전원**이 뽑힌다(실측). '0 → 100' 을 몇 % 증가로
    부를지는 정의되지 않았으므로 포함하지 않는다.
    """
    direction = getattr(threshold, "direction", "")
    inclusive = bool(getattr(threshold, "inclusive", True))
    if direction not in {"increase", "decrease"}:
        return None
    if direction == "increase":
        operator = ">=" if inclusive else ">"
    else:
        operator = "<=" if inclusive else "<"

    def scaled(operand: Any, factor: int) -> Any:
        if factor == 1:
            return operand
        return event_ir.Arithmetic(
            operator="*", left=operand, right=event_ir.Literal(value=factor)
        )

    kind = getattr(threshold, "kind", "")
    if kind == "ratio":
        numerator, denominator = threshold.numerator, threshold.denominator
        if not isinstance(numerator, int) or not isinstance(denominator, int):
            return None
        if numerator <= 0 or denominator <= 0:
            return None
        core = event_ir.Comparison(
            operator=operator,
            left=scaled(left, denominator),
            right=scaled(right, numerator),
            evidence=evidence,
        )
        baseline = event_ir.Comparison(
            operator=">", left=right, right=event_ir.Literal(value=0), evidence=evidence
        )
        return event_ir.And(operands=(baseline, core))

    if kind == "absolute":
        integral = _integer_scale(getattr(threshold, "amount", None))
        if integral is None:
            return None
        value, scale = integral
        if value <= 0:
            return None
        # 증가면 L-R, 감소면 R-L — 크기는 언제나 양수이고 방향은 뺄셈의 순서가 담는다.
        earlier, later = (right, left) if direction == "increase" else (left, right)
        delta = event_ir.Arithmetic(
            operator="-", left=scaled(later, scale), right=scaled(earlier, scale)
        )
        return event_ir.Comparison(
            operator=">=" if inclusive else ">",
            left=delta,
            right=event_ir.Literal(value=value),
            evidence=evidence,
        )
    return None


def can_plan(obligation: ComparisonObligation, *, query: str | None = None) -> bool:
    """이 의무를 지금 있는 실행 primitive 로 낮출 수 있는가."""
    return try_plan(obligation, query=query) is not None


def detect_temporal_state_obligations(
    query: str,
) -> tuple[TemporalStateObligation, ...]:
    """원문의 시점·이력 의무. 캡처의 소유자는 의무 원장이므로 여기서 다시 읽지 않는다."""

    import semantic_requirements  # 지연 import — 판정자는 원장 로딩을 import 부작용으로 만들지 않는다

    try:
        captured = semantic_requirements.capture_source_semantic_obligations(query)
    # 원장을 못 읽으면 캡처하지 않는다(추측 금지).
    except Exception:
        return ()
    obligations: list[TemporalStateObligation] = []
    for requirement in captured:
        if (
            semantic_requirements.obligation_kind(requirement)
            != semantic_requirements.TEMPORAL_QUALIFIER_KIND
        ):
            continue
        span = requirement.source_span
        start = span.get("start") if hasattr(span, "get") else None
        end = span.get("end") if hasattr(span, "get") else None
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        obligations.append(
            TemporalStateObligation(
                source_text=str(requirement.source_text),
                source_span=(start, end),
                kind=semantic_requirements.TEMPORAL_QUALIFIER_KIND,
            )
        )
    return tuple(obligations)


def _temporal_state_plans(
    query: str, *, today: date | None = None
) -> tuple[LoweringPlan, ...]:
    """시점·이력 의무의 계획. **낮춤은 새로 만들지 않고 이미 있는 것을 부른다.**

    :func:`temporal_claims.synthesize_temporal_claim` 이 이 축의 유일한 낮춤이고, 그것은
    전부-또는-아무것도로 동작한다(절 하나가 실패하면 아무 계획도 없다). 그 뒤 컴파일까지
    통과시켜 보는 것은 :func:`try_plan` 과 같은 이유다 — 표현이 서는 것과 실행 primitive 로
    내려가는 것은 다른 문제다.
    """

    obligations = detect_temporal_state_obligations(query)
    if not obligations:
        return ()

    import audience_runtime
    import event_compiler
    import event_ir
    import sql_dialect
    import temporal_claims
    import temporal_ir

    try:
        catalog = audience_runtime.resolve_audience_catalog()
        outcome = temporal_claims.synthesize_temporal_claim(
            query,
            snapshot=audience_runtime.catalog_snapshot(),
            catalog=catalog,
            runtime=temporal_ir.create_temporal_runtime(catalog),
            context=temporal_claims.request_context_for(today),
            today=today,
        )
    # 카탈로그·선언을 못 읽으면 계획 없음(추측 금지). 낮추지 못한 것과 판정 불가를 같은
    # 결말로 두는 이유는 둘 다 "반박하지 않는다"이기 때문이다.
    except Exception:
        return ()
    if not isinstance(outcome, temporal_claims.TemporalClaimSynthesis):
        return ()

    expression = outcome.expression
    try:
        capabilities = event_ir.expression_capabilities(expression)
        if event_compiler.unsupported_capabilities(expression):
            return ()
        event_ir.validate_evidence(expression)
        context = catalog.compile_context(
            dialect=sql_dialect.get_dialect("tsql"), literals=True
        )
        sql = event_compiler.compile_expression(expression, context=context).sql
    except (event_ir.IrSchemaError, event_compiler.SqlCompileError, KeyError, ValueError):
        return ()
    if not sql.strip():
        return ()

    return tuple(
        LoweringPlan(
            obligation=obligation,
            expression=expression,
            capabilities=capabilities,
            sql=sql,
            # 합성이 실제로 읽은 구간의 요구만 소비한 것이다.
            satisfied_requirement_ids=frozenset().union(
                *(_requirement_ids_at(query, span) for span in outcome.spans)
            ) if outcome.spans else frozenset(),
        )
        for obligation in obligations
        # 합성이 실제로 읽은 구간만 그 의무를 덮는다. 한 문장에 시간 절이 둘인데 하나만
        # 낮춰졌다면 나머지는 계획이 없어야 한다(있다고 하면 없는 지원을 광고한다).
        if any(
            start <= obligation.source_span[0] and obligation.source_span[1] <= end
            for start, end in outcome.spans
        )
    )


def plans_for_query(query: str, *, today: date | None = None) -> tuple[LoweringPlan, ...]:
    """원문에서 캡처된 의무 중 **실제로 낮출 수 있는** 것들의 계획."""
    plans = [
        try_plan(item, query=query)
        for item in detect_comparison_obligations(query, today=today)
    ]
    return (
        *(plan for plan in plans if plan is not None),
        *_temporal_state_plans(query, today=today),
    )


def unsettled_requirements(
    query: str, plan: LoweringPlan
) -> tuple[Any, ...]:
    """계획이 덮은 구간 안에서 **그 계획이 소비하지 않은** source requirement.

    비어 있어야 이 계획이 그 자리를 다 읽은 것이다. 하나라도 남으면 계획은 있어도 의미는
    빠진 것이고, 그때 '지원됨'을 단언하면 옳은 미지원 신고를 거짓으로 뒤집는다.
    """
    import semantic_requirements

    hull = plan.obligation.source_span
    try:
        captured = semantic_requirements.capture_source_semantic_obligations(query)
    # 원장을 못 읽으면 정산할 것도 없다 — 반박하지 않는다(추측 금지).
    except Exception:
        return ()
    unsettled = []
    for requirement in captured:
        if str(requirement.id) in plan.satisfied_requirement_ids:
            continue
        bounds = semantic_requirements.requirement_span(requirement)
        if bounds is not None and hull[0] <= bounds[0] and bounds[1] <= hull[1]:
            unsettled.append(requirement)
    return tuple(unsettled)


def plan_satisfying_span(
    query: str, span: Any, *, today: date | None = None
) -> LoweringPlan | None:
    """그 자리를 **실제로 소비한** 계획. 없으면 None.

    좌표(``span``)는 후보를 찾는 데만 쓴다 — 모델이 그 자리를 무엇이라 부르든 판정이 흔들리지
    않게 하려는 것이고, 그 이상의 권위는 없다. 지원 여부는 그다음 질문이 답한다:
    **이 계획이 자기가 덮은 구간의 요구를 전부 소비했는가.**

    이 두 질문을 하나로 합쳐 두었던 것이 조용한 의미 손실의 구조적 원인이었다(실측 2026-08-08).
    ``2026년 2월과 3월의 구매금액차이가 10% 이상 증가`` 의 계획은 hull 이 '10% 이상'을 덮었지만
    그 크기를 한 글자도 컴파일하지 않은 채 미지원 신고를 반박했고, 결과는 아무도 고칠 수 없는
    ``semantic_emission_failure`` 였다.
    """
    import semantic_requirements

    for plan in plans_for_query(query, today=today):
        if not semantic_requirements.spans_overlap(span, plan.obligation.source_span):
            continue
        if unsettled_requirements(query, plan):
            continue
        return plan
    return None


def clear_cache() -> None:
    _direction_pattern.cache_clear()
    _relative_direction_pattern.cache_clear()
    _comparative_marker_pattern.cache_clear()


__all__ = [
    "AGGREGATE_COMPARISON",
    "MEMBER_STATE_HISTORY",
    "ComparisonObligation",
    "LoweringPlan",
    "Obligation",
    "TemporalStateObligation",
    "WindowOperand",
    "can_plan",
    "clear_cache",
    "detect_comparison_obligations",
    "detect_temporal_state_obligations",
    "plan_satisfying_span",
    "plans_for_query",
    "try_plan",
    "unsettled_requirements",
]
