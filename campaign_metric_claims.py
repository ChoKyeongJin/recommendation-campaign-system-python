"""폐기된 '캠페인당 평균 구매금액' 축의 **모순 탐지기**(생성기가 아니다).

이 축은 2026-08-05 폐기됐다. 예전에는 이 모듈이 SemanticPlan ``aggregate_predicate``
노드를 합성했고, 그 노드만이 캠페인 분모(``COUNT(DISTINCT CAMP_ID:CAMP_EXEC_NO)``)로
나누는 실행 경로를 갖고 있었다. 그 노드를 컴파일하는 경로가 사라졌으므로 합성도 함께
사라진다.

문제는 **그냥 지우면 뜻이 조용히 바뀐다**는 것이다. 실측(2026-08-05):

    "캠페인별 구매반응 금액이 평균 10만 원 이상인 회원"

    합성 있음: SUM(BUY_AMT) / COUNT(DISTINCT CAMP_ID:CAMP_EXEC_NO) >= 100000  (캠페인당 평균)
    합성 없음: AVG(BUY_AMT) >= 100000                                        (반응 **행당** 평균)

둘 다 SQL 이 나오고 둘 다 성공으로 보이지만 뜻이 다르다. 그래서 이 모듈은 "모델이 낸 Event IR
집계식이 실은 캠페인 분모 평균을 뜻한다"만 판정하고, 호출자가 그 요청을 fail-close 시킨다.

**판정 축은 표면 어순이 아니다.** 2026-08-05 실측: '<캠페인별> <지표어> <평균>' 이 그 순서로
인접해야 한다고 보던 판정은 같은 뜻의 흔한 변형에서 통째로 우회됐다(7종 중 1종만 닫혔다) —
'캠페인별 **평균** 구매반응 금액'(평균이 앞), '캠페인별**로**'(조사), '캠페인 별'(띄어쓰기),
'캠페인당 평균 …', '… 대략 평균 …'(수식어 삽입). 어순은 뜻이 아니므로 판정은 두 가지의
논리곱으로만 한다.

    (a) 모델 표현 트리에 **선언된 분모 필드가 속한 소스**의 금액 필드를 행 단위로 평균 내는
        집계(``avg``)가 있다.
    (b) 원문에 카탈로그가 선언한 **grain 표면어가 등장한다**(어순·인접·조사 무관, 공백을 지운
        문자열에서 찾는다).

판정 근거는 전부 카탈로그 선언에서 온다(``metrics[*].claim_synthesis.average_per_campaign``의
``grain_terms`` / ``denominator_field`` / ``aggregation``). 이 모듈에 새 어휘를
하드코딩하지 않는다 — 선언이 불완전하면 그 지표는 후보에서 빠진다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import audience_frame

# 폐기 축의 고정 issue 인자·문구. 사용자에게 나가는 문장은 모델 산문이 아니라 이 상수다.
RETIRED_AXIS_ARGUMENT = "campaign_metric.average_amount"
RETIRED_AXIS_MESSAGE = (
    "캠페인당 평균 구매금액 조건은 현재 실행 경로에서 지원하지 않습니다. "
    "캠페인 수로 나눈 평균과 반응 건당 평균은 서로 다른 값이므로 임의로 대체하지 않습니다."
)

def _strings(value: Any) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in value or []
        if isinstance(item, str) and item.strip()
    )


def _span(row: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    start, end, text = row.get("start"), row.get("end"), row.get("text")
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and isinstance(text, str)
        and 0 <= start < end <= len(query)
        and query[start:end] == text
    ):
        return None
    return start, end


def _grain_match(
    query: str, terms: Sequence[str]
) -> tuple[str, tuple[int, int] | None] | None:
    """선언된 grain 표면어가 원문에 **등장하는가**(어순·인접·조사 무관).

    공백을 지운 문자열에서 찾으므로 '캠페인별'과 '캠페인 별'은 같은 표면어다 — 띄어쓰기는
    뜻이 아니다. 여러 표면어가 걸리면 가장 긴 것, 같으면 가장 앞선 것을 근거로 고른다
    (재현 가능한 선택이며, 판정 자체는 '하나라도 있는가'다).

    돌려주는 스팬은 근거 표시용이다. 접기로 좌표를 1:1로 되돌릴 수 없는 입력에서는 ``None``
    이지만 판정은 그대로 성립한다 — 근거 구간을 못 그린다고 통과시키면 그것이 fail-open 이다.
    """

    compact = query.replace(" ", "").casefold()
    best: tuple[int, int, str] | None = None
    for term in terms:
        needle = term.replace(" ", "").casefold()
        if not needle:
            continue
        cursor = 0
        while (start := compact.find(needle, cursor)) >= 0:
            candidate = (-len(needle), start, term)
            if best is None or candidate < best:
                best = candidate
            cursor = start + 1
    if best is None:
        return None
    length, start, term = -best[0], best[1], best[2]
    return term, audience_frame.compact_to_source_span(query, start, start + length)


def _declared_average_axes(catalog: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """캠페인 분모 평균을 **선언한** 지표만, 선언이 완전한 것만 반환한다."""

    metrics = catalog.get("metrics")
    fields = catalog.get("fields")
    sources = catalog.get("sources")
    if not all(isinstance(section, Mapping) for section in (metrics, fields, sources)):
        return ()
    assert isinstance(metrics, Mapping)
    assert isinstance(fields, Mapping)
    assert isinstance(sources, Mapping)

    axes: list[dict[str, Any]] = []
    for metric_id, metric in metrics.items():
        if not isinstance(metric, Mapping):
            continue
        claim = metric.get("claim_synthesis")
        if not isinstance(claim, Mapping):
            continue
        average = claim.get("average_per_campaign")
        if not isinstance(average, Mapping):
            continue
        source_id = metric.get("source")
        amount_field_id = metric.get("expression_field")
        denominator_field_id = average.get("denominator_field")
        amount_field = (
            fields.get(amount_field_id) if isinstance(amount_field_id, str) else None
        )
        denominator_field = (
            fields.get(denominator_field_id)
            if isinstance(denominator_field_id, str)
            else None
        )
        aggregation = average.get("aggregation")
        grain_terms = _strings(average.get("grain_terms"))
        if not (
            isinstance(source_id, str)
            and isinstance(sources.get(source_id), Mapping)
            and isinstance(amount_field, Mapping)
            and isinstance(denominator_field, Mapping)
            and amount_field.get("source") == source_id
            and denominator_field.get("source") == source_id
            and isinstance(aggregation, str)
            and aggregation
            and grain_terms
        ):
            continue
        axes.append({
            "metric_id": str(metric_id),
            "source": source_id,
            "amount_field": amount_field_id,
            "denominator_field": denominator_field_id,
            "aggregation": aggregation,
            "grain_terms": grain_terms,
        })
    return tuple(axes)


def _iter_nodes(value: Any) -> Any:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nodes(child)


def _source_name(relation: Any) -> str | None:
    """Source 이름을 얻는다(빈 Filter 한 겹은 같은 관계로 본다)."""

    if not isinstance(relation, Mapping):
        return None
    if relation.get("type") == "filter":
        return _source_name(relation.get("relation"))
    if relation.get("type") == "source" and isinstance(relation.get("name"), str):
        return str(relation["name"])
    return None


def _row_average_aggregate(
    expression: Any, axis: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """선언 지표의 금액 필드를 **행 단위**로 평균 내는 집계 노드를 찾는다.

    소스는 선언된 분모 필드가 속한 소스다(``_declared_average_axes`` 가 금액 필드·분모 필드가
    같은 소스임을 이미 확인했다) — 캠페인 분모로 나눠야 할 값을 행 수로 나눈 자리가 곧 모순이다.
    """

    for node in _iter_nodes(expression):
        field = node.get("expression")
        if not (
            node.get("type") == "aggregate"
            and node.get("function") == axis["aggregation"]
            and isinstance(field, Mapping)
            and field.get("type") == "field"
            and field.get("name") == axis["amount_field"]
            and _source_name(node.get("relation")) == axis["source"]
        ):
            continue
        return node
    return None


def _comparison_evidence(
    expression: Any, aggregate: Mapping[str, Any], query: str
) -> tuple[int, int] | None:
    for node in _iter_nodes(expression):
        if node.get("type") != "comparison" or node.get("left") is not aggregate:
            continue
        evidence = node.get("evidence")
        return _span(evidence, query) if isinstance(evidence, Mapping) else None
    return None


def _money_span(literal_bindings: Any, query: str) -> tuple[int, int] | None:
    if not isinstance(literal_bindings, list):
        return None
    money = [
        binding
        for binding in literal_bindings
        if isinstance(binding, Mapping) and binding.get("kind") == "money"
    ]
    if len(money) != 1:
        return None
    return _span(money[0], query)


def detect_retired_campaign_average_claim(
    query: str,
    expression: Any,
    literal_bindings: Any,
    catalog: Mapping[str, Any],
) -> dict[str, Any] | None:
    """모델 표현이 실은 '캠페인당 평균'을 뜻하는지 판정한다. 아니면 ``None``.

    반환 dict 는 호출자가 그대로 오디언스 issue 로 만들 수 있는 근거다 — 원문 구간
    (``evidence``)과 어떤 선언 지표·표면어가 판정을 만들었는지(``receipt``)를 담는다.
    """

    if not isinstance(expression, Mapping) or not isinstance(catalog, Mapping):
        return None
    matched: list[dict[str, Any]] = []
    for axis in _declared_average_axes(catalog):
        grain = _grain_match(query, axis["grain_terms"])
        if grain is None:
            continue
        aggregate = _row_average_aggregate(expression, axis)
        if aggregate is None:
            continue
        matched.append({"axis": axis, "grain": grain, "aggregate": aggregate})
    # 두 지표가 같은 문장을 주장하면 그 문장은 모호하다 — 카탈로그가 풀 문제다.
    if len(matched) != 1:
        return None
    axis = matched[0]["axis"]
    grain_term, grain_span = matched[0]["grain"]

    spans = [
        span
        for span in (
            grain_span,
            _comparison_evidence(expression, matched[0]["aggregate"], query),
            _money_span(literal_bindings, query),
        )
        if span is not None
    ]
    # 근거 구간은 grain 표면어·비교 근거·금액 리터럴의 외곽이다. 하나도 그릴 수 없으면 원문
    # 전체를 근거로 삼는다(구간을 못 그린다고 판정을 버리지 않는다).
    start, end = (
        (min(span[0] for span in spans), max(span[1] for span in spans))
        if spans
        else (0, len(query))
    )
    receipt_grain: dict[str, Any] = {"declared": grain_term}
    if grain_span is not None:
        receipt_grain.update({
            "text": query[grain_span[0] : grain_span[1]],
            "start": grain_span[0],
            "end": grain_span[1],
        })
    return {
        "argument": RETIRED_AXIS_ARGUMENT,
        "message": RETIRED_AXIS_MESSAGE,
        "evidence": {"text": query[start:end], "start": start, "end": end},
        "receipt": {
            "metric_id": axis["metric_id"],
            "source": axis["source"],
            "amount_field": axis["amount_field"],
            "denominator_field": axis["denominator_field"],
            "aggregation": axis["aggregation"],
            "grain_term": receipt_grain,
        },
    }


__all__ = [
    "RETIRED_AXIS_ARGUMENT",
    "RETIRED_AXIS_MESSAGE",
    "detect_retired_campaign_average_claim",
]
