"""'캠페인당 평균 구매금액'의 **모순 탐지기 + 복합 집계 합성기**.

무엇이 문제인가
---------------
실측(2026-08-05):

    "캠페인별 구매반응 금액이 평균 10만 원 이상인 회원"

    캠페인 분모 평균: SUM(BUY_AMT) * 1.0 / COUNT(DISTINCT (CAMP_ID, CAMP_EXEC_NO)) >= 100000
    반응 행당 평균:   AVG(BUY_AMT) >= 100000

둘 다 SQL 이 나오고 둘 다 성공으로 보이지만 뜻이 다르다. 모델은 후자를 낸다 — Event IR 의
``Aggregate(avg)`` 가 행 단위 평균이기 때문이다. 그래서 이 모듈이 "모델이 낸 집계식이 실은
캠페인 분모 평균을 뜻한다"를 판정하고, 그 자리를 **정확한 복합 집계식으로 바꿔 넣는다**.

    2026-08-05 ~ : 표현할 대수가 없어 fail-close 했다(축 폐기).
    현재         : Event IR 이 행 값(tuple)과 0 분모 가드(null_if)를 갖게 되어 합성으로 끝난다.

모델이 표현을 아예 내지 않는 경우
---------------------------------
실측(2026-08-06): 같은 문장에서 모델은 표현 대신 모호 신고를 낸다.

    expression: null
    issues: [{"code": "ambiguous_requirement", "argument": "grouping",
              "evidence": {"text": "캠페인별", …}}]

바꿔 넣을 자리가 없으니 재작성 경로는 성립하지 않고 문장은 되묻기로 닫힌다. 그런데 그 모호는
**이미 풀려 있다** — 카탈로그가 이 grain 의 뜻을 하나로 선언하고 있고(분자 SUM / 분모 서로 다른
캠페인 실행 수), 사용자 확인(2026-08-06)도 같은 뜻이었다: *회원이 반응한 캠페인들에 대한 평균*.
그래서 :func:`synthesize_campaign_average_predicate` 가 같은 선언으로 술어를 **처음부터** 만든다.

재작성 경로와 달리 모델 표현이 없으므로 '평균'이라는 뜻을 말하는 것은 원문 부사뿐이다. 그래서
이 경로만 ``average_terms`` 를 읽고, 임계값·비교 연산자는 리터럴 원장에서, 지표는 ``aliases`` 로
고른다. 그리고 **문장이 이 조건 하나뿐일 때만** 합성한다(:func:`audience_frame.is_frame_only`) —
다른 절이 남아 있는데 합성하면 그 절이 조용히 사라진 SQL 이 나간다.

**판정 축은 표면 어순이 아니다.** 2026-08-05 실측: '<캠페인별> <지표어> <평균>' 이 그 순서로
인접해야 한다고 보던 판정은 같은 뜻의 흔한 변형에서 통째로 우회됐다(7종 중 1종만 닫혔다) —
'캠페인별 **평균** 구매반응 금액'(평균이 앞), '캠페인별**로**'(조사), '캠페인 별'(띄어쓰기),
'캠페인당 평균 …', '… 대략 평균 …'(수식어 삽입). 어순은 뜻이 아니므로 판정은 두 가지의
논리곱으로만 한다.

    (a) 모델 표현 트리에 **선언된 분모 필드가 속한 소스**의 금액 필드를 행 단위로 평균 내는
        집계(``avg``)가 있다.
    (b) 원문에 카탈로그가 선언한 **grain 표면어가 등장한다**(어순·인접·조사 무관, 공백을 지운
        문자열에서 찾는다).

합성하는 식
-----------
::

    Arithmetic('/',
        Arithmetic('*', Aggregate(sum, <numerator_field>), Literal(<decimal_multiplier>)),
        NullIf(Aggregate(count, distinct, Tuple(<denominator_key_fields>)),
               Literal(<zero_denominator_value>)))

``Aggregate(avg)`` 로 바꾸지 않는다 — 분모가 행 수가 아니라 **서로 다른 캠페인 실행의 수**다.
분자와 분모는 같은 관계(모델이 낸 ``Aggregate.relation``, 기간 필터 포함)를 그대로 물려받으므로
기간 한정이 한쪽에만 걸리는 일이 없다.

판정·합성 근거는 전부 카탈로그 선언에서 온다
(``metrics[*].claim_synthesis.average_per_campaign``). 이 모듈에 새 어휘도 물리 이름도
하드코딩하지 않는다 — 선언이 불완전하면 그 지표는 후보에서 빠지고, 후보가 없으면 판정도 없다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import audience_frame
import event_ir

# 합성 소유자 표기(감사 로그의 filter_name 과 issue 인자에서 같은 이름을 쓴다).
SYNTHESIS_OWNER = "campaign_metric_claims.average_per_campaign"
# 모델 표현 없이 선언만으로 세운 술어의 소유자. 재작성과 이름을 나누는 이유는 감사 로그가
# "무엇을 고쳤는가"와 "무엇을 세웠는가"를 구분해야 하기 때문이다.
DECLARED_SYNTHESIS_OWNER = "campaign_metric_claims.average_per_campaign.from_declaration"
AVERAGE_AXIS_ARGUMENT = "campaign_metric.average_amount"

# 합성이 성립하지 않을 때의 고정 문구. 사용자에게 나가는 문장은 모델 산문이 아니라 이 상수다.
# 뜻이 다른 SQL(행당 평균)을 내느니 막는다 — 판정은 됐는데 선언이 불완전한 경우가 여기 온다.
UNSYNTHESIZABLE_MESSAGE = (
    "캠페인당 평균 구매금액 조건을 실행식으로 확정하지 못했습니다. "
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


def _number(value: Any) -> int | float | None:
    """JSON 수치(불리언 제외). 리터럴 어휘 밖의 값이면 None — 선언이 불완전한 것으로 본다."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _declared_average_axes(catalog: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """캠페인 분모 평균을 **선언한** 지표만, 선언이 완전한 것만 반환한다.

    '완전하다'는 판정과 합성 **둘 다** 가능하다는 뜻이다: 판정에 필요한 grain 표면어·집계 함수·
    금액 필드에 더해, 합성에 필요한 분모 키 필드(행 값 구성 요소)·소수 승수·0 분모 값까지
    같은 소스에 속한 채로 선언돼야 한다. 하나라도 빠지면 그 지표는 후보에서 빠진다 —
    부분 선언으로 절반만 합성하면 뜻이 조용히 달라진다.
    """

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
            # 아래 넷은 **재작성 판정에 쓰지 않는다**(그래서 위 관문에 넣지 않았다). 모델
            # 표현이 없을 때만 필요한 선언이므로, 여기서 필수로 만들면 선언 한 줄이 빠지는
            # 순간 재작성 판정까지 조용히 사라지고 뜻이 다른 행당 평균 SQL 이 나간다.
            "aliases": _strings(metric.get("aliases")),
            "average_terms": _strings(average.get("average_terms")),
            "unit": metric.get("unit"),
            "allowed_operators": _strings(metric.get("allowed_operators")),
            # 합성 바인딩은 **별도 단계**다(아래 _synthesis_binding). 판정에 필요한 키와 합성에
            # 필요한 키를 한 관문으로 묶으면, 합성 키 하나가 빠지는 순간 판정 자체가 조용히
            # 사라지고 뜻이 다른 행당 평균 SQL 이 그대로 나간다(fail-open).
            "synthesis": _synthesis_binding(average, fields, source_id, amount_field_id),
        })
    return tuple(axes)


def _synthesis_binding(
    average: Mapping[str, Any],
    fields: Mapping[str, Any],
    source_id: str,
    amount_field_id: Any,
) -> dict[str, Any] | None:
    """복합 집계식을 만들 수 있을 만큼 선언이 완전하면 그 바인딩, 아니면 ``None``."""

    # 분자는 선언이 있으면 그것을, 없으면 지표의 금액 필드를 쓴다. 둘이 어긋나면 판정한
    # 집계와 합성한 분자가 다른 컬럼이 되므로 같은 필드여야 한다.
    numerator_field_id = average.get("numerator_field", amount_field_id)
    key_field_ids = _strings(average.get("denominator_key_fields"))
    multiplier = _number(average.get("decimal_multiplier"))
    zero_value = _number(average.get("zero_denominator_value"))
    if not (
        numerator_field_id == amount_field_id
        and bool(average.get("denominator_distinct"))
        # 행 값은 둘 이상이어야 뜻이 있다. 하나짜리 키는 그냥 단일 컬럼 distinct 다.
        and len(key_field_ids) >= 2
        and all(
            isinstance(fields.get(field_id), Mapping)
            and fields[field_id].get("source") == source_id
            for field_id in key_field_ids
        )
        and multiplier is not None
        and zero_value is not None
    ):
        return None
    return {
        "numerator_field": str(numerator_field_id),
        "denominator_key_fields": key_field_ids,
        "decimal_multiplier": multiplier,
        "zero_denominator_value": zero_value,
        "required_capabilities": _strings(average.get("required_capabilities")),
    }


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


def _executable_relation(relation: Any) -> Any:
    """조건 없는 Filter 한 겹을 벗긴다(:func:`_source_name` 과 같은 규칙).

    모델은 같은 관계를 ``{"type":"filter","relation":…,"where":null}`` 로도 낸다. 판정은 그것을
    같은 관계로 보면서 합성만 그대로 물려받으면, 대수적으로 **조건이 없는 Filter** 라 IR
    스키마가 거부하고 표현할 수 있는 요청이 닫힌다. 벗기는 것은 빈 겹뿐이므로 기간 필터 같은
    실제 조건은 그대로 남는다.
    """

    if (
        isinstance(relation, Mapping)
        and relation.get("type") == "filter"
        and relation.get("where") is None
    ):
        return _executable_relation(relation.get("relation"))
    return relation


def _campaign_average_scalar(
    relation: Any, synthesis: Mapping[str, Any]
) -> dict[str, Any]:
    """관계 하나 → 캠페인 분모 평균 복합식(wire dict).

    분자와 분모가 **같은 관계**를 물려받는 것이 계약이다. 재작성 경로에서 그 관계는 모델이 낸
    집계의 관계(기간 필터 포함)이고, 선언 합성 경로에서는 선언 소스 그 자체다. 새 관계를
    따로 만들면 기간이 한쪽에만 걸리거나 창이 통째로 사라진다.
    """

    numerator = {
        "type": "aggregate",
        "function": "sum",
        "relation": relation,
        "expression": {"type": "field", "name": synthesis["numerator_field"]},
        "distinct": False,
    }
    denominator = {
        "type": "aggregate",
        "function": "count",
        "relation": relation,
        "expression": {
            "type": "tuple",
            "items": [
                {"type": "field", "name": field_id}
                for field_id in synthesis["denominator_key_fields"]
            ],
        },
        "distinct": True,
    }
    return {
        "type": "arithmetic",
        "operator": "/",
        # 정수 나눗셈 방지는 기존 SQL 과 같은 방식(× 1.0)이다. CAST 로 바꾸면 결과 타입과
        # 정밀도가 기존 산출물과 달라진다 — 같은 값을 내는 것이 이 합성의 목적이다.
        "left": {
            "type": "arithmetic",
            "operator": "*",
            "left": numerator,
            "right": {"type": "literal", "value": synthesis["decimal_multiplier"]},
        },
        "right": {
            "type": "null_if",
            "expression": denominator,
            "value": {"type": "literal", "value": synthesis["zero_denominator_value"]},
        },
    }


def _replace_node(value: Any, target: Mapping[str, Any], replacement: Any) -> Any:
    """트리에서 ``target`` **그 객체**를 ``replacement`` 로 바꾼 새 트리(원본 불변)."""

    if value is target:
        return replacement
    if isinstance(value, Mapping):
        return {key: _replace_node(item, target, replacement) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_node(item, target, replacement) for item in value]
    return value


def detect_campaign_average_claim(
    query: str,
    expression: Any,
    literal_bindings: Any,
    catalog: Mapping[str, Any],
) -> dict[str, Any] | None:
    """모델 표현이 실은 '캠페인당 평균'을 뜻하는지 판정하고, 맞으면 정확한 식으로 바꾼다.

    반환 dict 는 호출자가 그대로 쓸 수 있는 결과다.

    * ``expression``  합성된 표현(wire dict). ``None`` 이면 합성 불가 — 호출자는 표현을 버리고
      ``message`` 로 fail-close 한다(뜻이 다른 행당 평균 SQL 을 내보내지 않는다).
    * ``receipt``     어떤 선언이 판정·합성을 만들었는지.
    * ``evidence``    원문 구간(판정이 소비한 grain 표면어·비교 근거·금액 리터럴의 외곽).
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
    aggregate = matched[0]["aggregate"]
    grain_term, grain_span = matched[0]["grain"]

    spans = [
        span
        for span in (
            grain_span,
            _comparison_evidence(expression, aggregate, query),
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

    synthesis = axis["synthesis"]
    receipt: dict[str, Any] = {
        "owner": SYNTHESIS_OWNER,
        "metric_id": axis["metric_id"],
        "source": axis["source"],
        "amount_field": axis["amount_field"],
        "denominator_field": axis["denominator_field"],
        "aggregation": axis["aggregation"],
        "grain_term": receipt_grain,
    }
    synthesized: dict[str, Any] | None = None
    if synthesis is not None:
        replacement = _campaign_average_scalar(
            _executable_relation(aggregate.get("relation")), synthesis
        )
        candidate = _replace_node(expression, aggregate, replacement)
        # 만든 식이 실제로 이 IR 대수의 값인지 여기서 확인한다. 검증되지 않은 wire dict 를
        # 하류로 흘리면 파싱 실패가 '모델이 이상한 것을 냈다'로 잘못 귀속된다.
        try:
            parsed = event_ir.condition_from_dict(candidate)
        except event_ir.IrSchemaError:
            synthesized = None
        else:
            synthesized = parsed.to_dict()
            receipt.update({
                "numerator": {"function": "sum", "field": synthesis["numerator_field"]},
                "denominator": {
                    "function": "count",
                    "distinct": True,
                    "key_fields": list(synthesis["denominator_key_fields"]),
                    "zero_value": synthesis["zero_denominator_value"],
                },
                "decimal_multiplier": synthesis["decimal_multiplier"],
                "declared_capabilities": list(synthesis["required_capabilities"]),
                "used_capabilities": sorted(event_ir.expression_capabilities(parsed)),
            })
    return {
        "argument": AVERAGE_AXIS_ARGUMENT,
        "message": UNSYNTHESIZABLE_MESSAGE,
        "evidence": {"text": query[start:end], "start": start, "end": end},
        "expression": synthesized,
        "receipt": receipt,
    }


@dataclass(frozen=True)
class CampaignAverageSynthesis:
    """선언만으로 세운 술어 하나 + 그것을 증명한 카탈로그·리터럴 영수증."""

    expression: event_ir.Condition
    receipt: dict[str, Any]
    # 합성이 **스칼라 임계값으로** 소비한 원문 구간(임계 금액·비교어). 시간 검증기가 이
    # 구간을 소실된 창으로 세지 않게 한다.
    consumed_spans: tuple[tuple[int, int], ...]


def _money_amount(binding: Mapping[str, Any]) -> tuple[str, int | float] | None:
    """금액 리터럴의 (통화, 값). 통화나 값을 읽지 못하면 ``None`` — 단위를 추측하지 않는다."""

    normalized = binding.get("normalized")
    if not isinstance(normalized, Mapping):
        return None
    currency, amount = normalized.get("currency"), normalized.get("amount")
    if not (isinstance(currency, str) and currency):
        return None
    value = _number(amount)
    return None if value is None else (currency, value)


def _contains(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _surface_receipt(
    query: str, declared: str, span: tuple[int, int]
) -> dict[str, Any]:
    """선언 표면어와 그것이 걸린 원문 구간. 둘이 다를 수 있어서(띄어쓰기·조사) 함께 남긴다."""

    return {
        "declared": declared,
        "text": query[span[0] : span[1]],
        "start": span[0],
        "end": span[1],
    }


def synthesize_campaign_average_predicate(
    query: str,
    issue: Mapping[str, Any],
    literal_bindings: Sequence[Any],
    catalog: Mapping[str, Any],
) -> CampaignAverageSynthesis | None:
    """모델이 표현을 내지 않은 캠페인 분모 평균 문장 하나를 세우거나, 아니면 닫는다.

    성립 조건은 전부 선언과 원문 구조다 — 산문을 훑거나 임계값·기간을 지어내지 않는다.

    * 모델 issue 하나가 이 문장의 모호/미지원을 신고했고, 그 근거가 grain 또는 평균 표면어를 덮는다;
    * 카탈로그에 캠페인 분모 평균을 **선언한** 지표가 하나뿐이고 그 선언이 합성까지 완전하다;
    * 원문에 그 지표의 표면어·grain 표면어·평균 표면어가 모두 있다;
    * 리터럴 원장이 임계 금액 하나와 비교 연산자 하나뿐이고 그 순서가 '금액 → 비교어'다;
    * 금액의 통화가 지표 선언 단위와 같고 비교 연산자가 허용 목록 안에 있다;
    * 그 구간들 밖의 잔여물이 조건을 담지 않는 프레임뿐이다.

    마지막 조건이 이 경로의 안전장치다. 기간이든 성별이든 절이 하나라도 더 있으면 합성하지
    않는다 — 여기서 세우는 것은 문장 전체의 뜻이지 절 하나가 아니기 때문이다.
    """

    if not isinstance(query, str) or not isinstance(catalog, Mapping):
        return None
    if not isinstance(issue, Mapping) or issue.get("code") not in {
        "ambiguous_requirement",
        "unsupported_semantics",
    }:
        return None
    issue_evidence = issue.get("evidence")
    issue_span = _span(issue_evidence, query) if isinstance(issue_evidence, Mapping) else None
    if issue_span is None:
        return None

    bindings = [item for item in literal_bindings if isinstance(item, Mapping)]
    # 원장 전량 소비. 리터럴이 하나라도 더 있으면 이 문장은 이 조건 하나가 아니다.
    if len(bindings) != 2 or len(literal_bindings) != 2:
        return None
    amounts = [item for item in bindings if item.get("kind") == "money"]
    operators = [item for item in bindings if item.get("kind") == "comparison_operator"]
    if len(amounts) != 1 or len(operators) != 1:
        return None
    threshold, comparison = amounts[0], operators[0]
    threshold_span = _span(threshold, query)
    operator_span = _span(comparison, query)
    money = _money_amount(threshold)
    operator = comparison.get("normalized")
    if not (
        threshold_span is not None
        and operator_span is not None
        and money is not None
        and isinstance(operator, str)
        # 한국어 비교는 후치다('10만 원 이상'). 뒤집힌 배치는 이 문형이 아니므로 닫는다.
        and threshold_span[1] <= operator_span[0]
    ):
        return None
    currency, value = money

    matched: list[dict[str, Any]] = []
    for axis in _declared_average_axes(catalog):
        synthesis = axis["synthesis"]
        grain = _grain_match(query, axis["grain_terms"])
        average = _grain_match(query, axis["average_terms"])
        alias = _grain_match(query, axis["aliases"])
        if synthesis is None or grain is None or average is None or alias is None:
            continue
        # 근거 구간을 그리지 못하면 잔여물 검사를 할 수 없다 — 못 그린 채 통과시키면 fail-open.
        if grain[1] is None or average[1] is None or alias[1] is None:
            continue
        if axis["unit"] != currency or operator not in axis["allowed_operators"]:
            continue
        matched.append({
            "axis": axis,
            "grain": grain,
            "average": average,
            "alias": alias,
        })
    # 두 지표가 같은 문장을 주장하면 그 문장은 모호하다 — 카탈로그가 풀 문제다.
    if len(matched) != 1:
        return None
    axis = matched[0]["axis"]
    grain_term, grain_span = matched[0]["grain"]
    average_term, average_span = matched[0]["average"]
    alias_term, alias_span = matched[0]["alias"]

    owned = (grain_span, alias_span, average_span, threshold_span, operator_span)
    if not audience_frame.is_frame_only(query, owned):
        return None
    # 모델이 신고한 구간이 **이 합성이 푸는 모호**를 가리켜야 반박이 성립한다. 그 모호는
    # 집계 수준(grain)이거나 평균이라는 뜻이므로 둘 중 하나를 덮어야 한다.
    if not (_contains(issue_span, grain_span) or _contains(issue_span, average_span)):
        return None

    start, end = min(span[0] for span in owned), max(span[1] for span in owned)
    synthesis = axis["synthesis"]
    candidate = {
        "type": "comparison",
        "operator": operator,
        "left": _campaign_average_scalar(
            {"type": "source", "name": axis["source"]}, synthesis
        ),
        "right": {"type": "literal", "value": value},
        "evidence": {"text": query[start:end], "start": start, "end": end},
    }
    try:
        parsed = event_ir.condition_from_dict(candidate)
    except event_ir.IrSchemaError:
        # 만든 식이 이 IR 대수의 값이 아니면 합성하지 않는다. 검증되지 않은 wire dict 를
        # 하류로 흘리면 파싱 실패가 '모델이 이상한 것을 냈다'로 잘못 귀속된다.
        return None
    return CampaignAverageSynthesis(
        expression=parsed,
        receipt={
            "owner": DECLARED_SYNTHESIS_OWNER,
            "metric_id": axis["metric_id"],
            "source": axis["source"],
            "grain_term": _surface_receipt(query, grain_term, grain_span),
            "average_term": _surface_receipt(query, average_term, average_span),
            "metric_alias": _surface_receipt(query, alias_term, alias_span),
            "threshold_unit": currency,
            "operator": operator,
            "numerator": {"function": "sum", "field": synthesis["numerator_field"]},
            "denominator": {
                "function": "count",
                "distinct": True,
                "key_fields": list(synthesis["denominator_key_fields"]),
                "zero_value": synthesis["zero_denominator_value"],
            },
            "decimal_multiplier": synthesis["decimal_multiplier"],
            "declared_capabilities": list(synthesis["required_capabilities"]),
            "used_capabilities": sorted(event_ir.expression_capabilities(parsed)),
            "consumed_literal_binding_ids": [
                threshold.get("id"),
                comparison.get("id"),
            ],
            "issue": {
                "code": issue.get("code"),
                "argument": issue.get("argument"),
                "start": issue_span[0],
                "end": issue_span[1],
            },
        },
        consumed_spans=(threshold_span, operator_span),
    )


__all__ = [
    "AVERAGE_AXIS_ARGUMENT",
    "DECLARED_SYNTHESIS_OWNER",
    "SYNTHESIS_OWNER",
    "UNSYNTHESIZABLE_MESSAGE",
    "CampaignAverageSynthesis",
    "detect_campaign_average_claim",
    "synthesize_campaign_average_predicate",
]
