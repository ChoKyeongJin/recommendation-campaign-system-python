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
from collections.abc import Mapping, Sequence
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
class ClauseObligation:
    """절 하나가 주장하는 것 — 사건·극성·수량자·기간(:mod:`clause_semantics` 소유).

    :class:`ComparisonObligation` 과 나란한 종류다. 저쪽이 '두 창의 지표 비교'라면 이쪽은
    '이 사건이 이 구간에 있었는가 / 없었는가 / 칸마다 있었는가'다. 두 의무 모두 판정 근거는
    이름이 아니라 :func:`try_plan_clause` 가 실제로 낮춰 본 결과다.
    """

    clause: Any  # clause_semantics.ClauseSemantics
    kind: str = "clause_occurrence"

    # 반환 타입이 ``Any`` 인 이유는 ``clause`` 가 ``Any`` 이기 때문이다(순환 import 회피).
    # ``cast`` 로 좁히면 런타임 검증 없이 타입 오류를 숨긴다(§36) — 경계를 정직하게 남긴다.
    @property
    def source_span(self) -> Any:
        return self.clause.span

    @property
    def source_text(self) -> Any:
        return self.clause.evidence


@dataclass(frozen=True)
class FutureWindowObligation:
    """미래 방향 창 하나 — ``향후 7일 안에 <미래 담는 필드>가 도래하는``.

    :class:`ClauseObligation` 과 나란한 종류이고, 다른 점은 창의 **방향**이다. 방향은 확정
    시점에만 쓰인다 — 실행 IR 은 여전히 절대 구간을 받는다(그래서 IR 을 열지 않았다).
    """

    field: str
    window_start: date
    window_end_exclusive: date
    source_text: str
    source_span: tuple[int, int]
    kind: str = "future_window"


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


Obligation = (
    ComparisonObligation
    | TemporalStateObligation
    | ClauseObligation
    | FutureWindowObligation
)


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


# ── 절 의무: 존재 / 부재 / 칸별 발생 ────────────────────────────────────────────────
#
# 이 갈래가 이 모듈에 있는 이유는 Phase 3 의 목표 그대로다 — 판정자가 **거부만** 하는 것이
# 아니라 실행 가능한 계획을 스스로 생산해야 미지원 문구를 반박하는 것을 넘어 그 자리를 열 수
# 있다. 낮춤은 전부 기존 범용 노드 조합이고 새 노드는 없다.


def _binding_capabilities(event: str) -> tuple[frozenset[str], tuple[str, ...]] | None:
    """이 카탈로그 사건을 관측하는 선언들이 **합쳐서** 갖는 능력. 선언이 없으면 ``None``.

    합집합인 이유: 한 사건에 관측이 여럿일 수 있고(현재값 + 월별 스냅샷), 질문에 답할 수 있는
    관측이 하나라도 있으면 그 질문은 답할 수 있다. 어느 관측이 쓰일지는 낮춤이 정한다.
    """
    import audience_runtime  # 지연 import — 판정자는 선언 로딩을 import 부작용으로 만들지 않는다
    import temporal_ir

    try:
        catalog = temporal_ir.create_temporal_runtime(
            audience_runtime.resolve_audience_catalog()
        ).temporal_catalog
    # 선언을 못 읽으면 판정하지 않는다(추측 금지). 넓게 잡지 않는 이유는 이 자리가 정확히
    # 그 함정을 밟았기 때문이다 — 인자 하나가 빠진 호출을 ``except Exception`` 이 '선언 없음'
    # 으로 위장해, 부재 능력 계약이 **한 번도 돌지 않은 채** 통과했다(구현 중 실측).
    except temporal_ir.TemporalCatalogError:
        return None
    granted: set[str] = set()
    binding_ids: list[str] = []
    for binding_id, binding in catalog.bindings.items():
        if str(getattr(binding, "source", "")) != event:
            continue
        binding_ids.append(str(binding_id))
        capabilities = binding.observation_capabilities
        granted.update(
            name for name in capabilities.to_dict() if capabilities.has(name)
        )
    if not binding_ids:
        return None
    return frozenset(granted), tuple(sorted(binding_ids))


def clause_capability_gap(clause: Any) -> Any:
    """이 절의 수량자가 요구하는 능력을 어떤 관측도 갖고 있지 않으면 그 진단. 아니면 ``None``.

    **부재(never)를 최종값 스냅샷으로 근사하지 않게 하는 자리다.** ``앱으로 로그인하지 않은
    회원`` 을 ``LAST_LOGIN_CHANNEL != 'APP'`` 로 답하면 어제 앱으로 로그인한 회원이 대상에
    들어간다 — 그 표현은 그 질문에 답할 수 없고, 답할 수 없다는 사실은 이미
    ``observation_capabilities.supports_all_occurrences`` 로 선언돼 있다.
    """
    import semantic_diagnostics

    required = clause.required_capabilities
    if not required:
        return None
    resolved = _binding_capabilities(clause.event)
    if resolved is None:
        # 그 사건에 시간 관측 선언이 없다 — 능력 판정의 재료가 없으므로 반박도 차단도 하지
        # 않는다(추측 금지). 이 자리는 다른 계층의 판정으로 넘어간다.
        return None
    granted, binding_ids = resolved
    if required & granted:
        # 하나라도 있으면 답할 수 있다. 맨 부재가 마지막 발생 시각만으로 답하는 자리다.
        return None
    label = _QUANTIFIER_LABELS.get(str(clause.quantifier), str(clause.quantifier))
    qualifier = (
        f"'{clause.qualifier_evidence}' 한정이 붙은 " if clause.qualified else ""
    )
    names = " 또는 ".join(sorted(required))
    return semantic_diagnostics.missing_capability(
        capability=names,
        symbol=clause.event,
        clause_id=clause.clause_id,
        evidence=clause.evidence,
        available=binding_ids,
        user_action=(
            f"'{clause.evidence}' 은(는) {qualifier}{label} 조건인데, "
            f"'{clause.event}' 관측은 그 판정에 필요한 이력을 보관하지 않습니다"
            f"(필요: {names}). 마지막 값으로 근사하면 뜻이 달라지므로 "
            "이 조건은 지원하지 않습니다."
        ),
        developer_detail=(
            f"quantifier={clause.quantifier} event={clause.event} "
            f"qualified={clause.qualified} required={sorted(required)} "
            f"bindings={list(binding_ids)} granted={sorted(granted)}"
        ),
    )


# 수량자 → 사용자 문구에 쓰는 한국어 이름. 진단 생성자에 낱말을 적지 않기 위한 표 하나다.
_QUANTIFIER_LABELS: dict[str, str] = {
    "never": "부재(한 번도 없음)",
    "every_bucket_occurrence": "칸별 전칭(각 기간마다 최소 1회)",
    "every_bucket_state": "칸별 상태 전칭",
}


def _clause_window(clause: Any, *, query: str | None = None) -> Any:
    """절이 소유한 기간 → IR 창. 기간이 없으면 ``None``(창 없는 술어는 정상이다).

    **미래 방향 표지가 붙은 기간은 과거 창으로 만들지 않는다.** Event IR 의 창은 둘 다 과거를
    보므로(``rolling``·``relative``) 미래 기간을 그 모양으로 옮기면 뜻이 뒤집힌다 — 구현 중
    실측: ``향후 7일`` 이 ``>= 7일 전`` 으로 컴파일됐다. 그 부류가 가장 나쁜 결함이므로
    (조용한 의미 반전) 여기서 fail-close 한다. 미래 창의 자리는
    :func:`try_plan_future_window` 이고, 그쪽은 필드 능력 선언을 요구한다.
    """
    import event_ir

    if clause.temporal is None or not clause.temporal.is_quantified:
        return None
    if isinstance(query, str) and query:
        import calendar_window

        span = clause.temporal.span
        if span is not None and calendar_window.is_future_directed_duration(query, span[0]):
            return None
    wire = clause.temporal.clause.wire_window
    if not isinstance(wire, dict):
        return None
    if wire.get("type") == "rolling":
        unit = str(wire.get("unit") or "")
        value = wire.get("value")
        if unit not in event_ir.WINDOW_UNITS or not isinstance(value, int):
            return None
        return event_ir.RollingWindow(value=value, unit=unit)
    if wire.get("type") == "interval":
        start, end = wire.get("start"), wire.get("end_exclusive")
        if not (isinstance(start, str) and isinstance(end, str)):
            return None
        try:
            return event_ir.AbsoluteInterval(
                start=date.fromisoformat(start), end_exclusive=date.fromisoformat(end)
            )
        except ValueError:
            return None
    return None


def _occurrence_relation(event: str, window: Any) -> Any:
    """``Source(event)`` + (창이 있으면) 시간 필터. 창이 없으면 소스 그대로."""
    import event_ir

    source = event_ir.Source(name=event)
    if window is None:
        return source
    return event_ir.Filter(
        relation=source,
        where=event_ir.TimeFilter(
            field=event_ir.FieldRef(name=f"{event}.occurred_at"), window=window
        ),
    )


def _bucket_intervals(
    clause: Any, *, today: date | None, query: str | None = None
) -> tuple[Any, ...]:
    """칸별 전칭의 각 칸을 **확정된 절대 구간**으로 편다. 못 펴면 빈 튜플.

    ``최근 3개월 동안 매월`` = 세 달 각각이다. 칸의 경계 계산은 달력이 소유하므로
    (:mod:`temporal_ir.calendar`) 여기서 날짜 산술을 다시 쓰지 않는다 — 달·분기의 길이는
    ``timedelta`` 로 접히지 않는다(§16).
    """
    import event_ir
    from datetime import datetime

    from temporal_ir import calendar as tcal
    from temporal_ir import semantic_ir as tsir

    unit = clause.bucket_unit
    if unit not in {item.value for item in tsir.TimeUnit}:
        return ()
    window = _clause_window(clause, query=query)
    anchor = today or date.today()
    if isinstance(window, event_ir.AbsoluteInterval):
        start, end = window.start, window.end_exclusive
    elif isinstance(window, event_ir.RollingWindow):
        if window.unit != unit:
            # 칸 단위와 창 단위가 다르면 칸 수가 정수로 떨어지지 않는다. 근사하면 요청하지
            # 않은 칸이 들어가거나 빠지므로 펴지 않는다.
            return ()
        try:
            time_unit = tsir.TimeUnit(unit)
            zone = tcal.zone("UTC")
            current = tcal.bucket_containing(
                datetime(anchor.year, anchor.month, anchor.day, tzinfo=zone),
                time_unit,
                "UTC",
            )
            first = tcal.shift_bucket(current, time_unit, -(window.value - 1), "UTC")
        except Exception:
            return ()
        start, end = first.start.date(), current.end_exclusive.date()
    else:
        return ()

    try:
        time_unit = tsir.TimeUnit(unit)
        zone = tcal.zone("UTC")
        buckets = tcal.enumerate_buckets(
            tcal.TimeInterval(
                start=datetime(start.year, start.month, start.day, tzinfo=zone),
                end_exclusive=datetime(end.year, end.month, end.day, tzinfo=zone),
            ),
            time_unit,
            "UTC",
        )
        return tuple(
            event_ir.AbsoluteInterval(start=first_day, end_exclusive=last_day)
            for bucket in buckets
            for first_day, last_day in (bucket.dates(),)
        )
    except Exception:
        return ()


def try_plan_clause(
    obligation: ClauseObligation,
    *,
    today: date | None = None,
    query: str | None = None,
) -> LoweringPlan | None:
    """절 의무를 canonical Event IR 로 낮춘 계획. 낮출 수 없으면 ``None``.

    세 수량자가 전부 **기존 범용 노드**로 내려간다.

    ==========================  ==================================================
    수량자                       canonical 표현
    ==========================  ==================================================
    ``exists``                   ``Exists(Filter(Source, TimeFilter))``
    ``never``                    ``Not(Exists(...))``
    ``every_bucket_occurrence``  ``And(Exists(칸_i) for i)`` — 칸마다 하나
    ==========================  ==================================================

    능력 계약을 **먼저** 본다. ``never`` 는 그 구간의 발생을 전부 볼 수 있는 관측에서만 뜻이
    보존되므로, 능력이 없으면 계획을 세우지 않는다 — 여기서 계획을 내면 그것이 곧 근사다.
    """
    import audience_runtime
    import clause_semantics
    import event_compiler
    import event_ir
    import sql_dialect

    clause = obligation.clause
    if clause_capability_gap(clause) is not None:
        return None

    window = _clause_window(clause, query=query)
    if window is None and clause.temporal is not None:
        # **절이 기간을 말했는데 창을 만들지 못했다.** 그대로 낮추면 그 기간이 사라진 SQL 이
        # 나가고(부재 조건에서는 대상이 통째로 달라진다), 경고도 남지 않는다. 구현 중 실측:
        # 기준일이 전달되지 않아 창이 fail-close 된 문장이 **구간 없는** ``NOT EXISTS`` 로
        # 컴파일됐다. 하드 의미 제약이 SQL 에서 사라지면 성공이 아니다.
        return None
    evidence = event_ir.Evidence(
        text=clause.evidence, start=clause.span[0], end=clause.span[1]
    )

    quantifier = str(clause.quantifier)
    expression: Any
    if quantifier == clause_semantics.Quantifier.EVERY_BUCKET_OCCURRENCE:
        intervals = _bucket_intervals(clause, today=today, query=query)
        if len(intervals) < 2:
            # 칸이 하나면 전칭이 아니라 존재다. 그 뜻은 다른 수량자가 이미 갖고 있으므로
            # 여기서 만들면 같은 집합을 두 이름이 말하게 된다.
            return None
        expression = event_ir.And(
            operands=tuple(
                event_ir.Exists(
                    relation=_occurrence_relation(clause.event, interval),
                    evidence=evidence,
                )
                for interval in intervals
            )
        )
    elif quantifier in {
        clause_semantics.Quantifier.EXISTS,
        clause_semantics.Quantifier.NEVER,
    }:
        exists = event_ir.Exists(
            relation=_occurrence_relation(clause.event, window), evidence=evidence
        )
        expression = (
            event_ir.Not(operand=exists)
            if quantifier == clause_semantics.Quantifier.NEVER
            else exists
        )
    else:
        return None

    try:
        capabilities = event_ir.expression_capabilities(expression)
        if event_compiler.unsupported_capabilities(expression):
            return None
        event_ir.validate_evidence(expression)
        catalog = audience_runtime.resolve_audience_catalog()
        context = catalog.compile_context(
            dialect=sql_dialect.get_dialect("tsql"), literals=True
        )
        sql = event_compiler.compile_expression(expression, context=context).sql
    except (event_ir.IrSchemaError, event_compiler.SqlCompileError, KeyError, ValueError):
        # 낮출 수 없다는 **판정**이다(버그가 아니라 도메인 결과).
        return None
    if not sql.strip():
        return None
    return LoweringPlan(
        obligation=obligation,
        expression=expression,
        capabilities=capabilities,
        sql=sql,
    )


def clause_obligations(
    query: str,
    *,
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
    today: date | None = None,
) -> tuple[ClauseObligation, ...]:
    """원문의 절 의무. 캡처의 소유자는 :mod:`clause_semantics` 이므로 여기서 다시 읽지 않는다.

    맨 존재 절(``exists`` + 창 없음)은 의무로 세지 않는다. 그 모양은 "이 사건을 한 적이 있는
    회원"이라 거의 모든 문장에 있고, 의무로 세면 판정자가 문장마다 같은 계획을 내며 다른
    계층의 정상 판정을 흔든다. 이 판정자가 답해야 하는 것은 **막히기 쉬운 모양**이다 —
    부재·칸별 전칭·창을 가진 존재.
    """
    import audience_runtime
    import clause_semantics

    if not isinstance(query, str) or not query.strip():
        return ()
    try:
        snapshot = audience_runtime.catalog_snapshot()
    # 카탈로그를 못 읽으면 캡처하지 않는다(추측 금지).
    except Exception:
        return ()
    rows = (
        list(literal_bindings)
        if literal_bindings is not None
        else _literal_bindings(query, today=today)
    )
    owned = _stronger_obligation_spans(query)
    found = []
    for clause in clause_semantics.analyze_clauses(query, snapshot, rows):
        if (
            str(clause.quantifier) == clause_semantics.Quantifier.EXISTS
            and not clause.has_period
        ):
            continue
        if any(
            start <= clause.span[0] and clause.span[1] <= end for start, end in owned
        ):
            # 같은 자리를 더 구체적인 의무가 이미 소유한다(두 기간의 지표 비교 · 시점 이력).
            # 존재 조건으로 덮으면 그 의무가 읽은 뜻이 조용히 좁아진다 — 소유 대장의 규칙이다.
            continue
        found.append(ClauseObligation(clause=clause))
    return tuple(found)


def _stronger_obligation_spans(query: str) -> tuple[tuple[int, int], ...]:
    """더 구체적인 의무가 소유한 원문 구간들. 캡처가 깨지면 빈 튜플(추측 금지).

    '더 구체적'의 기준은 **읽은 요구의 수**다. 비교 의무는 창 둘·지표 하나·방향 하나를 읽고,
    절 의무는 사건 하나와 창 하나를 읽는다. 겹칠 때 후자가 이기면 앞의 셋이 사라진다.
    """
    spans: list[tuple[int, int]] = []
    try:
        spans.extend(item.source_span for item in detect_comparison_obligations(query))
        spans.extend(item.source_span for item in detect_temporal_state_obligations(query))
    except Exception:
        return ()
    return tuple(sorted(spans))


def _literal_bindings(query: str, *, today: date | None = None) -> list[Mapping[str, Any]]:
    """원문의 리터럴 바인딩(결정론 추출). 못 읽으면 빈 목록(추측 금지).

    ``today`` 를 반드시 넘긴다. 기준일 없이 부르면 기준일에 의존하는 창(``지난달``·
    ``3개월 전부터 1개월 전까지``)이 fail-close 로 **사라지고**, 그 사실을 아래 계층은 알 수
    없다 — 창이 없는 절로 보여 부재 조건이 **구간 없는** ``NOT EXISTS`` 로 컴파일됐다
    (구현 중 실측: 사용자가 말한 기간이 조용히 사라진 SQL).
    """
    try:
        from query_structurer.semantic_ir import extract_literal_bindings

        return list(
            extract_literal_bindings(
                query, current_date=today.isoformat() if today is not None else None
            )
        )
    except Exception:
        return []


# ── 지원 여부의 typed 답 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Resolution:
    """"이 요청을 실행할 수 있는가"에 대한 **하나의** 답.

    다섯 갈래로 나누는 이유는 귀결이 다르기 때문이다 — 사용자가 고칠 수 있는 것(되묻기), 지금
    자산의 한계(미지원), 배선 결함(내부 실패)은 사용자에게 줄 다음 행동이 전부 다르다.
    """


@dataclass(frozen=True)
class Executable(Resolution):
    """낮출 수 있다. ``plans`` 가 그 증거이고, 각 계획은 이미 SQL 까지 통과했다."""

    plans: tuple[LoweringPlan, ...]

    def __post_init__(self) -> None:
        if not self.plans:
            raise ValueError("Executable 은 계획 없이 만들 수 없다(지원의 증거가 계획이다)")


@dataclass(frozen=True)
class NeedUserInput(Resolution):
    """사용자가 말하지 않았거나 애매하다 — 문장을 고치면 열린다."""

    diagnostic: Any  # semantic_diagnostics.Diagnostic


@dataclass(frozen=True)
class MissingCapability(Resolution):
    """지금 실행 자산이 그 의미를 담지 못한다 — 문장을 고쳐도 열리지 않는다."""

    diagnostic: Any  # semantic_diagnostics.Diagnostic


@dataclass(frozen=True)
class InvalidSemantics(Resolution):
    """요청 자체가 성립하지 않는다(주체가 다르다 · 값이 모순이다)."""

    diagnostic: Any  # semantic_diagnostics.Diagnostic


@dataclass(frozen=True)
class InternalFailure(Resolution):
    """배선·불변식 결함. 사용자 요청의 의미적 결과가 아니다."""

    diagnostic: Any  # semantic_diagnostics.Diagnostic


@dataclass(frozen=True)
class Undetermined(Resolution):
    """이 판정자에게 **관할이 없다**. 반박도 차단도 하지 않는다.

    이 갈래가 필요한 이유는 정직함이다. 판정자가 캡처하는 의무 종류는 아직 전부가 아니라서,
    의무를 하나도 못 잡은 요청에 대해 "실행 가능하다"고도 "불가능하다"고도 말할 수 없다.
    :data:`Undetermined` 는 모순 불변식(:func:`contradicts`)에서 **아무것도 주장하지 않는다** —
    Phase 3 의 진척은 이 답이 줄어드는 것으로 측정된다.
    """

    reason: str


def resolve_executable(
    query: str, *, today: date | None = None,
    literal_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> Resolution:
    """**지원 여부의 단일 판정.** 목록을 조회하지 않고 실제로 낮춰 보고 답한다.

    순서가 계약이다.

    1. 절의 **능력 계약**을 먼저 본다. 근사로 답할 수 있는 모양(부재 → 최종값 부등호)이
       여기서 걸리지 않으면, 아래 낮춤이 성공해 버려 조용한 오답이 지원으로 기록된다.
    2. 그다음 캡처된 의무를 전부 낮춰 본다(비교·시점 이력·절).
    3. 하나라도 낮춰지면 :class:`Executable`, 아니면 :class:`Undetermined` 다.

    낮추지 못한 것을 곧바로 미지원이라 부르지 않는 이유는 이 판정자가 아직 모든 의무 종류를
    캡처하지 않기 때문이다 — 관할 밖을 한계로 보고하면 없는 한계를 광고한다.
    """
    import semantic_diagnostics

    if not isinstance(query, str) or not query.strip():
        return Undetermined("빈 요청")

    # **주체가 다르면 어떤 표현도 옳지 않다.** 그러므로 이 질문이 가장 먼저다 — 조건을 낮춰
    # 보는 것은 그 조건이 회원을 고르는 술어일 때만 뜻이 있다. 감사 #44 가 이 자리다:
    # 브랜드를 행으로 달라는 요청이 회원 세그먼트 경로에서 배선 결함(``failure``)으로 끝났고
    # 사용자에게 줄 문장이 하나도 없었다.
    subject = unsupported_subject_diagnostic(query)
    if subject is not None:
        return InvalidSemantics(subject)

    # **시각 해상도는 이해와 표현이 다른 문제다.** ``최근 24시간`` 은 기간을 말한 문장이므로
    # '기간 값이 없습니다'로 되물으면 사실이 아니다(감사 #82). 담을 수 없다는 것이 정확한
    # 사실이고, 그 사실은 창 단위 선언에서 나온다.
    precision = _subday_precision_gap(query)
    if precision is not None:
        return MissingCapability(precision)

    rows = (
        list(literal_bindings)
        if literal_bindings is not None
        else _literal_bindings(query, today=today)
    )
    try:
        obligations = clause_obligations(query, literal_bindings=rows, today=today)
    except Exception as exc:  # noqa: BLE001 - 판정자 배선 결함은 숨기지 않는다
        return InternalFailure(
            semantic_diagnostics.compiler_invariant_violation(
                symbol="lowering_planner.clause_obligations",
                developer_detail=f"{type(exc).__name__}: {exc}",
            )
        )

    for obligation in obligations:
        gap = clause_capability_gap(obligation.clause)
        if gap is not None:
            return MissingCapability(gap)

    plans: list[LoweringPlan] = []
    plans.extend(plans_for_query(query, today=today))
    future = try_plan_future_window(query, today=today)
    if future is not None:
        plans.append(future)
    plans.extend(
        plan
        for plan in (
            try_plan_clause(item, today=today, query=query) for item in obligations
        )
        if plan is not None
    )
    if plans:
        return Executable(tuple(plans))
    return Undetermined("이 판정자가 캡처한 의무가 없다")


def try_plan_future_window(query: str, *, today: date | None = None) -> LoweringPlan | None:
    """미래 방향 창을 낮춘 계획. 낮출 수 없으면 ``None``.

    ``direction="future"`` 를 모든 날짜 필드에 열지 않는다. 미래 창이 뜻을 갖는 것은 그 컬럼이
    **미래 값을 담을 때**뿐이고, 그 사실은 카탈로그가 ``supports_future_values`` 로 선언한다 —
    과거 사건 컬럼(``ORDER_DATE``)에 미래 창을 걸면 경고 없이 항상 0건이다.

    표면 문법(``향후``·``앞으로``)의 소유자는 :mod:`calendar_window` 이고, 여기서는 그 판정
    결과와 필드 선언을 맞춰 보기만 한다.
    """
    import calendar_window
    import event_compiler
    import event_ir
    import sql_dialect

    if not isinstance(query, str) or not query.strip():
        return None
    fields = future_capable_fields()
    if not fields:
        return None

    anchor = today or date.today()
    compact = query.replace(" ", "")
    offsets = tuple(index for index, char in enumerate(query) if char != " ")
    try:
        candidates = calendar_window.duration_window_candidates(
            compact, source=query, source_offsets=offsets if len(offsets) == len(compact) else None
        )
    except Exception:
        return None
    forward = [
        candidate
        for candidate in candidates
        if candidate.kind == calendar_window.KIND_ROLLING
        and calendar_window.is_future_directed_duration(compact, candidate.start)
    ]
    if len(forward) != 1:
        # 미래 표지가 없거나 둘 이상이면 어느 구간이 미래인지 어순만으로 알 수 없다(추측 금지).
        return None
    candidate = forward[0]
    days = calendar_window.duration_candidate_days(candidate)
    if days is None or days <= 0:
        return None

    import audience_runtime

    try:
        catalog = audience_runtime.resolve_audience_catalog()
        snapshot = audience_runtime.catalog_snapshot()
    except Exception:
        return None
    declarations = snapshot.get("fields") if isinstance(snapshot, Mapping) else None
    if not isinstance(declarations, Mapping):
        return None
    named = [
        field_id
        for field_id in sorted(fields)
        if _field_surface_in(query, declarations.get(field_id))
    ]
    if len(named) != 1:
        # 어느 미래 필드인지 원문이 지목하지 않았다 — 지어내면 없는 조건이 생긴다.
        return None
    field_id = named[0]

    span = _compact_span_to_source(query, candidate)
    if span is None:
        return None
    obligation = FutureWindowObligation(
        field=field_id,
        window_start=anchor,
        window_end_exclusive=anchor + timedelta(days=days),
        source_text=query[span[0]:span[1]],
        source_span=span,
    )
    evidence = event_ir.Evidence(
        text=obligation.source_text, start=span[0], end=span[1]
    )
    source = field_id.rpartition(".")[0]
    if not source:
        return None
    # 필드가 사실 테이블에 있으면 그 관계를 스코프에 세워야 참조된다. 주체 컬럼이면 ``Exists``
    # 가 필요 없지만, 그 구분을 여기서 추측하지 않고 **컴파일해 보고** 판정한다(이 모듈의 규칙).
    bounds: Any = event_ir.And(operands=(
        event_ir.Comparison(
            operator=">=",
            left=event_ir.FieldRef(name=field_id),
            right=event_ir.Literal(value=obligation.window_start.strftime("%Y%m%d")),
            evidence=evidence,
        ),
        event_ir.Comparison(
            operator="<",
            left=event_ir.FieldRef(name=field_id),
            right=event_ir.Literal(
                value=obligation.window_end_exclusive.strftime("%Y%m%d")
            ),
            evidence=evidence,
        ),
    ))
    expression: Any = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name=source), where=bounds
        ),
        evidence=evidence,
    )
    try:
        capabilities = event_ir.expression_capabilities(expression)
        if event_compiler.unsupported_capabilities(expression):
            return None
        event_ir.validate_evidence(expression)
        context = catalog.compile_context(
            dialect=sql_dialect.get_dialect("tsql"), literals=True
        )
        sql = event_compiler.compile_expression(expression, context=context).sql
    except (event_ir.IrSchemaError, event_compiler.SqlCompileError, KeyError, ValueError):
        return None
    if not sql.strip():
        return None
    return LoweringPlan(
        obligation=obligation, expression=expression, capabilities=capabilities, sql=sql
    )


def _field_surface_in(query: str, declaration: Any) -> bool:
    """이 필드의 표면어가 원문에 있는가. 선언이 없으면 거짓(추측 금지)."""
    if not isinstance(declaration, Mapping):
        return False
    aliases = declaration.get("aliases")
    compact = query.replace(" ", "")
    return any(
        term and str(term).replace(" ", "") in compact
        for term in (declaration.get("label"), *(aliases or ()))
    )


def _compact_span_to_source(query: str, candidate: Any) -> tuple[int, int] | None:
    """압축 좌표 후보 → 원문 좌표 구간. 변환의 소유자는 :mod:`audience_frame` 이다."""
    import audience_frame

    try:
        return audience_frame.compact_to_source_span(query, candidate.start, candidate.end)
    except Exception:
        return None


def _subday_precision_gap(query: str) -> Any:
    """하루보다 잘은 롤링 창을 요청했으면 그 진단. 아니면 ``None``.

    ``최근 24시간`` 을 ``최근 1일`` 로 접지 않는다 — 롤링 24시간은 **기준 시각**부터 거슬러
    세는 창이고, 날짜 칸으로 접으면 요청하지 않은 시간대가 들어온다. 그 접기가 가능한지는
    시간 컬럼의 저장 단위(``time_format``)가 정하고, 지금 모든 사건 소스의 시간 컬럼은 날짜
    단위다(``char8``/``char6``) — 그래서 이 요청은 이해되지만 표현되지 않는다.

    구간이 함께 명시된 문장은 여기서 막지 않는다. 그때 시각은 창이 아니라 술어이고, 그 경로는
    :func:`event_compiler.compile_time_window` 가 경계일 시각으로 이미 낮춘다.
    """
    import calendar_window
    import semantic_diagnostics

    try:
        found = calendar_window.subday_duration_spans(query)
    # 표면 문법을 못 읽으면 판정하지 않는다(추측 금지).
    except Exception:
        return None
    if not found:
        return None
    try:
        if calendar_window.parse_calendar_window_spans(query):
            # 날짜 창이 함께 있으면 시각은 경계일 술어로 낮아진다 — 여기서 막을 일이 아니다.
            return None
    except Exception:
        return None
    start, end, value, unit = found[0]
    return semantic_diagnostics.unsupported_temporal_precision(
        requested=f"{value}{unit}",
        supported="day",
        evidence=query[start:end],
    )


# 미래 방향 창을 걸 수 있는 필드가 선언해야 하는 능력. ``ORDER_DATE`` 같은 과거 사건 컬럼에
# 미래 창을 걸면 **항상 0건**이 되므로, 방향은 필드 선언이 허락한 자리에서만 열린다.
FUTURE_VALUES_CAPABILITY = "supports_future_values"


def future_capable_fields() -> frozenset[str]:
    """미래 값을 담는다고 **선언된** 필드 심볼. 선언을 못 읽으면 빈 집합(fail-close).

    코드에 컬럼 이름을 적지 않는다 — 어느 컬럼이 미래를 담는지는 카탈로그가 안다.
    """
    import audience_runtime

    try:
        snapshot = audience_runtime.catalog_snapshot()
    except Exception:
        return frozenset()
    fields = snapshot.get("fields") if isinstance(snapshot, Mapping) else None
    if not isinstance(fields, Mapping):
        return frozenset()
    return frozenset(
        str(field_id)
        for field_id, declaration in fields.items()
        if isinstance(declaration, Mapping)
        and bool(declaration.get(FUTURE_VALUES_CAPABILITY))
    )


def unsupported_subject_diagnostic(query: str) -> Any:
    """결과 주체가 선언된 주체가 아니면 그 진단. 아니면 ``None``.

    주체 어휘의 소유자는 :mod:`result_shape`(요청의 결과 축)이고 선언된 주체의 소유자는
    카탈로그다. 이 함수는 둘을 맞춰 보기만 한다 — 여기서 낱말을 읽지 않는다.
    """
    import audience_runtime
    import result_shape
    import semantic_diagnostics

    found = result_shape.requested_non_subject_entity(query)
    if found is None:
        return None
    term, start, end = found
    try:
        catalog = audience_runtime.catalog_snapshot()
    # 선언을 못 읽으면 판정하지 않는다(추측 금지).
    except Exception:
        return None
    subject = catalog.get("subject") if isinstance(catalog, Mapping) else None
    label = (
        str(subject.get("label") or subject.get("entity") or "회원")
        if isinstance(subject, Mapping)
        else "회원"
    )
    if term == label:
        return None
    return semantic_diagnostics.unsupported_subject(
        requested=term, supported=(label,), evidence=query[start:end]
    )


def contradicts(resolution: Resolution, final_outcome: str) -> bool:
    """판정자가 실행 가능하다고 답했는데 최종 귀결이 미지원인가 — **금지된 상태**.

    같은 canonical 요청이 낮춰지는데 미지원으로 끝나면 그것은 도메인 한계가 아니라 버그다.
    :class:`Undetermined` 는 아무것도 주장하지 않으므로 어떤 귀결과도 모순되지 않는다.
    """
    import semantic_diagnostics

    return isinstance(resolution, Executable) and final_outcome == str(
        semantic_diagnostics.Outcome.UNSUPPORTED
    )


def clear_cache() -> None:
    _direction_pattern.cache_clear()
    _relative_direction_pattern.cache_clear()
    _comparative_marker_pattern.cache_clear()


__all__ = [
    "AGGREGATE_COMPARISON",
    "MEMBER_STATE_HISTORY",
    "ClauseObligation",
    "ComparisonObligation",
    "FUTURE_VALUES_CAPABILITY",
    "FutureWindowObligation",
    "Executable",
    "InternalFailure",
    "InvalidSemantics",
    "LoweringPlan",
    "MissingCapability",
    "NeedUserInput",
    "Obligation",
    "Resolution",
    "TemporalStateObligation",
    "Undetermined",
    "WindowOperand",
    "can_plan",
    "clause_capability_gap",
    "clause_obligations",
    "clear_cache",
    "contradicts",
    "detect_comparison_obligations",
    "detect_temporal_state_obligations",
    "future_capable_fields",
    "plan_satisfying_span",
    "plans_for_query",
    "resolve_executable",
    "try_plan",
    "try_plan_clause",
    "try_plan_future_window",
    "unsupported_subject_diagnostic",
    "unsettled_requirements",
]
