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
import metric_recipe_selection
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
    """원문에 등장한 (지표, 표면어, 구간) 후보 — **같은 자리를 주장하는 것끼리는 하나만 남긴다**.

    '평균 구매금액'에는 '구매금액'(다른 지표의 표면어)이 통째로 들어 있다. 포함된 쪽을 독립된
    지표 언급으로 세면 한 문장이 두 지표를 주장하는 것으로 보여 모호로 닫히고, 표현할 수 있는
    요청이 전부 막힌다. 포함 관계는 어휘가 아니라 구간이 판정하므로 새 표면어가 늘어도
    이 규칙은 그대로다.

    포함은 겹침의 한 경우일 뿐이다. 어느 후보를 남길지는
    :func:`metric_recipe_selection.resolve_overlapping_candidates` 가 정한다 — 첫 기준이 매칭
    구간 길이라 포함 관계에서는 **긴 쪽이 이겨** 종전과 같은 답이 나오고, 길이까지 같은 겹침도
    후보를 지우는 대신 결정론적으로 하나를 고른다(마지막 기준이 recipe id 라 동률이 없다).
    구간이 겹치지 않는 후보는 서로 다른 자리를 말하므로 그대로 남는다 — 거기서 줄이면 한 문장의
    다른 조건이 조용히 사라진다.
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
    # recipe_id 는 후보 집합 안에서만 유일하면 된다. 같은 지표의 표면어가 여러 번 등장할 수
    # 있으므로 (지표, 표면어, 구간)을 함께 넣어 서로 다른 등장이 하나로 접히지 않게 한다.
    keyed = {
        f"{declaration['metric_id']}|{alias}|{start}|{end}": (declaration, alias, start, end)
        for declaration, alias, start, end in candidates
    }
    resolved = metric_recipe_selection.resolve_overlapping_candidates([
        metric_recipe_selection.RecipeCandidate(
            recipe_id=recipe_id,
            kind=member_scalar_metrics.MEMBER_SCALAR_KIND,
            span=(item[2], item[3]),
            surface=item[1],
        )
        for recipe_id, item in sorted(keyed.items())
    ])
    return [keyed[candidate.recipe_id] for candidate in resolved]


def _threshold_phrase_is_adjacent(
    query: str,
    *,
    alias_end: int,
    value_bounds: tuple[int, int],
    operator_bounds: tuple[int, int],
) -> bool:
    """지표 → 임계값 → 비교가 그 순서로 **국소 인접**한가(사이에 조사·공백만).

    이 순서 규칙이 기간 표면어를 **스칼라 임계값**으로 만드는 근거다. 한 문장에 같은 ``30일``
    이 둘 있어도('구매주기가 30일 이하이고 최근 30일 이내 구매한') 비교어 앞에 붙은 쪽만
    여기를 통과하므로 뒤에 남은 진짜 창은 지워지지 않는다 — 값·단위만 대조하면 둘이 구별되지
    않는다.

    문장 전역 판정(:func:`audience_frame.is_frame_only`)은 일부러 여기 두지 않는다. 그것은
    '문장 전체가 이 술어 하나인가'라는 **다른 질문**이고, 술어 하나를 합성해도 되는지를 묻는
    :func:`_whole_phrase_matches` 만 그 답을 요구한다.

    순서·겹침·사이 전량 일치라는 **구조**는 :func:`audience_frame.spans_are_locally_adjacent`
    가 갖고, 사이에 무엇을 허용할지는 여기 남는다. 이 문형이 받아들이는 조사를 넓히는 결정은
    이 판정의 소유자인 이 모듈의 것이고, 공용 헬퍼에 조사 목록을 심으면 다른 문법을 쓰는
    소비자들이 한 목록을 공유하게 된다.
    """

    value_start, value_end = value_bounds
    operator_start, operator_end = operator_bounds
    if not (value_start < value_end and operator_start < operator_end):
        return False
    return audience_frame.spans_are_locally_adjacent(
        query,
        ((alias_end, alias_end), value_bounds, operator_bounds),
        gaps=(_METRIC_TO_VALUE_RE, _VALUE_TO_OPERATOR_RE),
    )


def _whole_phrase_matches(
    query: str,
    *,
    alias_start: int,
    alias_end: int,
    value_bounds: tuple[int, int],
    operator_bounds: tuple[int, int],
) -> bool:
    """닫힌 회원 술어 하나이고 주인 없는 산문 잔여물이 없는가.

    지표 → 임계값 → 비교가 그 순서로 인접해야 한다(:func:`_threshold_phrase_is_adjacent`) —
    그것이 기간 표현을 **스칼라**로 만드는 근거다. 술어 바깥은 절 구조로 판정한다
    (:mod:`audience_frame`): 요청 동사와 어미는 달라도 되지만 양쪽에 조건이 하나만 더 있어도
    닫힌다.
    """

    if not _threshold_phrase_is_adjacent(
        query,
        alias_end=alias_end,
        value_bounds=value_bounds,
        operator_bounds=operator_bounds,
    ):
        return False
    return audience_frame.is_frame_only(query, [(alias_start, operator_bounds[1])])


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


def _member_scalar_threshold_atoms(
    expression: event_ir.Condition,
) -> tuple[tuple[event_ir.Source, event_ir.Comparison], ...]:
    """``Exists(Filter(Source, Comparison(FieldRef, Literal)))`` 원자만 (소스, 비교)로.

    :func:`event_ir.iter_atoms` 를 쓰지 않는 이유는 그것이 같은 술어에서 ``Exists`` 와 그 안의
    ``Comparison`` 을 **둘 다** 내기 때문이다 — 한 술어를 두 번 세게 된다.
    :func:`event_ir.existence_views` 도 쓸 수 없다: 비교를 버리므로 임계값이 사라진다.
    """

    atoms: list[tuple[event_ir.Source, event_ir.Comparison]] = []
    for node in event_ir.walk(expression):
        if not isinstance(node, event_ir.Exists):
            continue
        relation = node.relation
        if not isinstance(relation, event_ir.Filter):
            continue
        source, comparison = relation.relation, relation.where
        if (
            isinstance(source, event_ir.Source)
            and isinstance(comparison, event_ir.Comparison)
            and isinstance(comparison.left, event_ir.FieldRef)
            and isinstance(comparison.right, event_ir.Literal)
        ):
            atoms.append((source, comparison))
    return tuple(atoms)


def _member_scalar_metric(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    source_name: str,
    field_name: str,
) -> resolved_semantic_catalog.MetricSpec | None:
    """소스 심볼이 가리키는 회원별 스칼라 지표(아니면 ``None``).

    회원별 스칼라는 지표 id 와 소스 id 를 같게 선언한다. 비교 좌변이 그 지표가 선언한
    ``expression_field`` 인지까지 확인하는 이유는, 같은 소스의 다른 필드를 비교한 식이
    임계값 청구를 얻어 가지 않게 하기 위해서다.

    모델이 낸 표현에는 카탈로그에 없는 심볼이 들어올 수 있다. 그때 카탈로그가 내는
    :class:`resolved_semantic_catalog.CatalogError` **하나만** 국소적으로 받아 '해당 없음'으로
    접는다 — 청구가 없으면 마스킹도 없으므로 이 방향의 실패는 더 엄격한 쪽이다.
    """

    try:
        metric = catalog.metric(source_name)
    except resolved_semantic_catalog.CatalogError:
        return None
    if metric.kind != member_scalar_metrics.MEMBER_SCALAR_KIND:
        return None
    if metric.source != source_name or metric.expression_field != field_name:
        return None
    return metric


def _threshold_literal_rows(
    query: str, literal_bindings: Sequence[Any]
) -> tuple[tuple[tuple[tuple[int, int], str, Any], ...], tuple[tuple[tuple[int, int], str], ...]]:
    """원장에서 (임계값 후보, 비교어 후보)를 각각 (구간, 단위, 값) / (구간, 기호)로."""

    thresholds: list[tuple[tuple[int, int], str, Any]] = []
    operators: list[tuple[tuple[int, int], str]] = []
    for row in literal_bindings:
        if not isinstance(row, Mapping):
            continue
        bounds = _span(row, query)
        if bounds is None:
            continue
        kind = row.get("kind")
        if kind in _THRESHOLD_KINDS:
            unit_value = _threshold_unit_and_value(row)
            if unit_value is not None:
                thresholds.append((bounds, unit_value[0], unit_value[1]))
        elif kind == "comparison_operator":
            symbol = event_ir.canonical_comparison_operator(row.get("normalized"))
            if symbol is not None:
                operators.append((bounds, symbol))
    return tuple(thresholds), tuple(operators)


def consumed_scalar_threshold_spans(
    query: str,
    expression: event_ir.Condition,
    literal_bindings: Sequence[Any],
    registry: Mapping[str, Any],
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> tuple[tuple[int, int], ...]:
    """최종 표현이 **스칼라 임계값으로 소비한** 원문 구간(임계값 + 비교어).

    ``30일`` 자체에는 고정된 뜻이 없다 — '구매주기가 30일 이하'의 ``30일`` 과 '최근 30일
    구매'의 ``30일`` 은 원장 원자가 완전히 같다. 무엇인지는 **최종 표현의 구조**와 원문의
    지표 표면어·비교어가 함께 정한다. 그래서 이 함수는 표현의 생산자(모델인지 합성기인지)를
    묻지 않고 식을 역산한다 — 그 지식이 합성 부산물에만 있으면 모델이 식을 내는 순간 사라진다.

    :func:`synthesize_member_scalar_predicate` 를 재사용하지 않는 이유는 그쪽이 **다른 질문**에
    답하기 때문이다: 리터럴 두 개뿐인 닫힌 한 문장을 애플리케이션이 세워도 되는가. 혼합문은
    거기서 즉시 닫히지만, 소비 구간 청구는 혼합문에서도 나와야 한다.

    성립 조건은 전부 필요하고 하나라도 증명하지 못하면 그 원자는 청구하지 않는다(fail-close):
    지표 kind, 레지스트리의 ``threshold_unit`` 선언, 원문의 지표 표면어 등장, 지표 → 임계값 →
    비교어의 국소 인접, (값·단위) 일치, 비교 기호 일치. 표면어 등장과 국소 인접이 특히
    중요하다 — 그 둘이 '최근 30일 구매한 회원'에 환각 ``buy_cycle <= 30`` 이 붙었을 때 뜻이
    조용히 바뀌는 것을 막는 **유일한** 방어선이다(검증기는 창의 개수만 비교하므로 과도한
    마스킹을 스스로 검출하지 못한다).

    청구는 절이나 문장이 아니라 **정확한 리터럴 구간**만 낸다. 절 단위로 넓히면 같은 문장의
    진짜 시간 창까지 지운다.
    """

    declarations = _declared_metrics(registry)
    if not declarations:
        return ()
    candidates = _alias_candidates(query, declarations)
    if not candidates:
        return ()
    thresholds, operators = _threshold_literal_rows(query, literal_bindings)
    if not (thresholds and operators):
        return ()

    claimed: set[tuple[int, int]] = set()
    for source, comparison in _member_scalar_threshold_atoms(expression):
        metric = _member_scalar_metric(
            catalog, source.name, str(comparison.left.name)
        )
        if metric is None:
            continue
        alias_ends = {
            end
            for declaration, _alias, _start, end in candidates
            if declaration["catalog_metric_id"] == metric.id
        }
        declared_units = {
            str(declaration["threshold_unit"])
            for declaration, _alias, _start, _end in candidates
            if declaration["catalog_metric_id"] == metric.id
        }
        # 표면어가 원문에 없으면 이 지표를 주장할 근거가 없다. 단위 선언이 갈리면 어느 쪽으로
        # 대조해야 하는지 증명되지 않은 것이다 — 둘 다 청구하지 않는다.
        if not alias_ends or len(declared_units) != 1:
            continue
        declared_unit = next(iter(declared_units))
        literal_value = comparison.right.value
        if isinstance(literal_value, bool):
            continue
        matches = {
            (threshold_bounds, operator_bounds)
            for alias_end in alias_ends
            for threshold_bounds, unit, value in thresholds
            for operator_bounds, symbol in operators
            if unit == declared_unit
            and value == literal_value
            and symbol == comparison.operator
            and _threshold_phrase_is_adjacent(
                query,
                alias_end=alias_end,
                value_bounds=threshold_bounds,
                operator_bounds=operator_bounds,
            )
        }
        # 두 조합이 같은 원자를 주장하면 어느 구간이 임계값인지 증명되지 않았다. 과도한 청구는
        # 같은 문장의 진짜 창을 지우고 검증기가 그것을 잡아 주지 않으므로 여기서 닫는다.
        if len(matches) != 1:
            continue
        threshold_bounds, operator_bounds = next(iter(matches))
        claimed.update((threshold_bounds, operator_bounds))
    return tuple(sorted(claimed))


__all__ = [
    "OWNER",
    "MemberScalarSynthesis",
    "consumed_scalar_threshold_spans",
    "synthesize_member_scalar_predicate",
]
