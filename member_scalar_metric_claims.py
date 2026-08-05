"""선언된 회원 지표 임계 문장 → 애플리케이션 소유 canonical Event IR.

모델은 '구매주기가 30일 이하인 회원'을 표현하지 못한다고 신고하곤 한다. 그 신고가 맞을 때도
있지만, **선언이 완전한 경우**에는 애플리케이션이 그 신고를 반박할 수 있다. 이 모듈은 그
완전한 경우 하나만 닫는다 — 산문을 훑거나 임계값을 지어내지 않는다:

* 모델 issue 가 하나뿐이고 그 근거 구간이 지표 표면어 또는 임계값·비교 구간을 덮는다;
* 원문 전체가 하나의 닫힌 '지표 / 임계값 / 비교' 회원 술어이고 나머지는 프레임뿐이다;
* 리터럴은 단위를 가진 임계값 하나와 비교 연산자 하나뿐이다;
* 임계값 단위와 지표가 선언한 ``threshold_unit`` 이 일치한다;
* 지표가 카탈로그에 **회원별 스칼라 계약**(:mod:`member_scalar_metrics`)으로 선언돼 있다.

임계값 리터럴을 **단위를 가진 스칼라**로 쓰는 것이 의도다. 범용 추출기는 맨 기간을 기본값으로
``rolling_duration`` 이라 이름 붙이므로, 소유권은 여기 닫힌 문형이 정한다: 임계값은 지표와
비교 연산자 **사이에** 있어야 하고 그 사이에 조사·공백 외의 것이 없어야 한다. 그 구분이
``30일`` 을 지어낸 롤링 창이나 표면 정규화의 한자어 수사 ``일``(하나)로 만들지 않는다.

낮추기 자체는 하지 않는다 — 계약 검증과 IR 조립은 :mod:`member_scalar_metrics` 한 곳이
소유하고, 이 모듈은 **원문에서 무엇을 골랐는가**만 책임진다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import audience_frame
import event_ir
import member_scalar_metrics
import resolved_semantic_catalog

OWNER = "member_scalar_metrics.catalog_literal_operator"

# 임계값을 담는 리터럴 종류 → 그 리터럴이 말하는 단위를 읽는 법. 지표 선언의
# ``threshold_unit`` 과 대조하는 것이 목적이므로, 여기서 단위를 **추측하지 않는다**:
# 읽어 낸 단위가 없으면 그 리터럴은 임계값 후보에서 빠진다.
_THRESHOLD_KINDS = ("duration", "money", "number_with_unit")
# 기간 리터럴의 semantic_unit 표기(복수형)를 지표 단위 어휘로 접는다. event_ir 이 단위 표기
# 흔들림의 단일 소유자이므로 그 함수로 정규화한다.
_DURATION_UNITS = frozenset({"day", "week", "month", "year"})
# 지표와 임계값 사이에 허용되는 조사(그 밖의 글자가 있으면 닫힌 문형이 아니다).
_METRIC_TO_VALUE_RE = re.compile(r"(?:이|가|은|는)?\s*")
_VALUE_TO_OPERATOR_RE = re.compile(r"\s*")


@dataclass(frozen=True)
class MemberScalarSynthesis:
    """조건 하나 + 그것을 증명한 카탈로그·리터럴 영수증."""

    expression: event_ir.Condition
    receipt: dict[str, Any]


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


def _threshold_unit_and_value(binding: Mapping[str, Any]) -> tuple[str, Any] | None:
    """리터럴이 말하는 (단위, 값). 단위를 읽어 낼 수 없으면 ``None``."""

    kind = binding.get("kind")
    normalized = binding.get("normalized")
    if kind == "money" and isinstance(normalized, Mapping):
        currency, amount = normalized.get("currency"), normalized.get("amount")
        if isinstance(currency, str) and currency and _is_number(amount):
            return currency, amount
        return None
    if kind == "number_with_unit" and isinstance(normalized, Mapping):
        unit, value = normalized.get("unit"), normalized.get("value")
        if isinstance(unit, str) and unit and _is_number(value):
            return unit, value
        return None
    if kind == "duration" and isinstance(normalized, Mapping):
        # 시간 의미가 명시된 종류(과거 시점·경계)는 임계 스칼라가 될 수 없다. 범용 추출기의
        # 기본값(rolling_duration)과 무표기만 단위 있는 수로 받는다.
        if normalized.get("temporal_kind") not in (None, "", "rolling_duration"):
            return None
        unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
        value = normalized.get("value")
        if unit in _DURATION_UNITS and _is_number(value):
            return str(unit), value
        return None
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _declared_metrics(registry: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """회원별 스칼라 임계에 쓸 수 있을 만큼 선언이 완전한 지표만."""

    metrics = registry.get("metrics")
    if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
        return ()
    declared_units = registry.get("canonical_member_scalar")
    units = (
        declared_units.get("threshold_units")
        if isinstance(declared_units, Mapping)
        else None
    )
    prefix = (
        str(declared_units.get("source_prefix") or "")
        if isinstance(declared_units, Mapping)
        else ""
    )
    if not (isinstance(units, Sequence) and not isinstance(units, (str, bytes)) and prefix):
        return ()
    allowed_units = {str(item) for item in units}
    resolved: list[dict[str, Any]] = []
    for entry in metrics:
        if not isinstance(entry, Mapping):
            continue
        metric_id = entry.get("metric_id")
        unit = entry.get("threshold_unit")
        synonyms = [
            item.strip()
            for item in entry.get("synonyms") or ()
            if isinstance(item, str) and item.strip()
        ]
        if not (
            isinstance(metric_id, str)
            and metric_id
            and isinstance(unit, str)
            and unit in allowed_units
            and synonyms
        ):
            continue
        resolved.append({
            "metric_id": metric_id,
            "catalog_metric_id": f"{prefix}{metric_id}",
            "threshold_unit": unit,
            "synonyms": synonyms,
        })
    return tuple(resolved)


def _alias_candidates(
    query: str, declarations: Sequence[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], str, int, int]]:
    """원문에 등장한 (지표, 표면어, 구간) 후보 — **더 긴 이름 안에 든 짧은 이름은 뺀다**.

    '평균 구매금액'에는 '구매금액'(다른 지표의 표면어)이 통째로 들어 있다. 포함된 쪽을 독립된
    지표 언급으로 세면 한 문장이 두 지표를 주장하는 것으로 보여 모호로 닫히고, 표현할 수 있는
    요청이 전부 막힌다. 포함 관계는 어휘가 아니라 구간이 판정하므로 새 표면어가 늘어도
    이 규칙은 그대로다.
    """

    candidates: list[tuple[Mapping[str, Any], str, int, int]] = []
    for declaration in declarations:
        aliases = sorted(
            set(declaration["synonyms"]), key=lambda item: (-len(item), item)
        )
        for alias in aliases:
            cursor = 0
            while (start := query.find(alias, cursor)) >= 0:
                end = start + len(alias)
                cursor = end
                candidates.append((declaration, alias, start, end))
    return [
        candidate
        for candidate in candidates
        if not any(
            other is not candidate
            and other[2] <= candidate[2]
            and candidate[3] <= other[3]
            and (other[3] - other[2]) > (candidate[3] - candidate[2])
            for other in candidates
        )
    ]


def _whole_phrase_matches(
    query: str,
    *,
    alias_start: int,
    alias_end: int,
    value_bounds: tuple[int, int],
    operator_bounds: tuple[int, int],
) -> bool:
    """닫힌 회원 술어 하나이고 주인 없는 산문 잔여물이 없는가.

    지표 → 임계값 → 비교가 그 순서로 인접해야 한다 — 그것이 기간 표현을 **스칼라**로 만드는
    근거다. 술어 바깥은 절 구조로 판정한다(:mod:`audience_frame`): 요청 동사와 어미는 달라도
    되지만 양쪽에 조건이 하나만 더 있어도 닫힌다.
    """

    value_start, value_end = value_bounds
    operator_start, operator_end = operator_bounds
    if not (alias_end <= value_start < value_end <= operator_start < operator_end):
        return False
    if _METRIC_TO_VALUE_RE.fullmatch(query[alias_end:value_start]) is None:
        return False
    if _VALUE_TO_OPERATOR_RE.fullmatch(query[value_end:operator_start]) is None:
        return False
    return audience_frame.is_frame_only(query, [(alias_start, operator_end)])


def synthesize_member_scalar_predicate(
    query: str,
    issue: Mapping[str, Any],
    literal_bindings: Sequence[Any],
    registry: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> MemberScalarSynthesis | None:
    """선언된 회원별 스칼라 임계 하나를 합성하거나, 아니면 닫는다."""

    if issue.get("code") != "unsupported_semantics":
        return None
    bindings = [item for item in literal_bindings if isinstance(item, Mapping)]
    if len(bindings) != 2 or len(literal_bindings) != 2:
        return None
    thresholds = [item for item in bindings if item.get("kind") in _THRESHOLD_KINDS]
    operators = [item for item in bindings if item.get("kind") == "comparison_operator"]
    if len(thresholds) != 1 or len(operators) != 1:
        return None
    threshold, comparison = thresholds[0], operators[0]
    threshold_bounds = _span(threshold, query)
    operator_bounds = _span(comparison, query)
    unit_value = _threshold_unit_and_value(threshold)
    operator = comparison.get("normalized")
    if not (
        threshold_bounds is not None
        and operator_bounds is not None
        and unit_value is not None
        and isinstance(operator, str)
    ):
        return None
    literal_unit, literal_value = unit_value

    candidates = _alias_candidates(query, _declared_metrics(registry))
    metric_ids = {declaration["metric_id"] for declaration, _a, _s, _e in candidates}
    # 두 지표가 같은 문장을 주장하면 그 문장은 모호하다 — 추측하지 않고 닫는다.
    if len(metric_ids) != 1:
        return None
    declaration, alias, alias_start, alias_end = min(
        (item for item in candidates),
        key=lambda item: (-(item[3] - item[2]), item[2], item[1]),
    )
    if declaration["threshold_unit"] != literal_unit:
        return None
    if not _whole_phrase_matches(
        query,
        alias_start=alias_start,
        alias_end=alias_end,
        value_bounds=threshold_bounds,
        operator_bounds=operator_bounds,
    ):
        return None

    issue_bounds = _span(
        issue.get("evidence") if isinstance(issue.get("evidence"), Mapping) else {}, query
    )
    if issue_bounds is None:
        return None
    issue_start, issue_end = issue_bounds
    issue_owns_alias = issue_start <= alias_start and alias_end <= issue_end
    issue_owns_literals = (
        issue_start <= threshold_bounds[0]
        and threshold_bounds[1] <= issue_end
        and issue_start <= operator_bounds[0]
        and operator_bounds[1] <= issue_end
    )
    if not (issue_owns_alias or issue_owns_literals):
        return None

    evidence = event_ir.Evidence(
        text=query[alias_start : operator_bounds[1]],
        start=alias_start,
        end=operator_bounds[1],
    )
    try:
        predicate = member_scalar_metrics.lower_member_scalar_metric(
            catalog,
            str(declaration["catalog_metric_id"]),
            operator=operator,
            value=literal_value,
            evidence=evidence,
        )
    except member_scalar_metrics.MemberScalarContractError:
        # 계약이 어긋나면 합성하지 않는다. 모델의 미지원 신고가 그대로 남아 fail-close 된다 —
        # 반박할 근거가 없는데 반박하는 것이 이 경로가 피하려는 실패다.
        return None

    receipt = dict(predicate.receipt)
    receipt.update({
        "owner": OWNER,
        "registry_metric_id": declaration["metric_id"],
        "metric_alias": alias,
        "threshold_unit": literal_unit,
        "consumed_literal_binding_ids": [threshold.get("id"), comparison.get("id")],
        "issue": {
            "code": issue.get("code"),
            "argument": issue.get("argument"),
            "start": issue_start,
            "end": issue_end,
            "ownership": "metric_alias" if issue_owns_alias else "threshold_and_operator",
        },
    })
    return MemberScalarSynthesis(expression=predicate.expression, receipt=receipt)


__all__ = [
    "OWNER",
    "MemberScalarSynthesis",
    "synthesize_member_scalar_predicate",
]
