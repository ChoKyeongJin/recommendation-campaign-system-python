"""Canonical Event IR 이 어떤 원문 신호를 **이미 소유하고 있는가**.

결정론 드롭 감지기(`graph_rag._deterministic_dropped_conditions`)는 원문에서 뽑은 신호가
plan 슬롯에 하나도 안 잡혔으면 경고를 낸다. canonical 경로는 그 슬롯을 쓰지 않고 Event IR 로
같은 의미를 표현하므로, 감지기가 슬롯만 보면 전부 오탐이 된다.

**그렇다고 canonical 이라는 이유로 감지기를 통째로 면제하면 안 된다.** 실측(2026-08-02):
면제가 켜져 있으면 Event IR 이 표현하지 못하는 축(등급·상태 이력, 수신동의)의 조건이
경고 하나 없이 사라진 채 SQL 이 출고된다. 면제는 "감지기가 틀렸다"가 아니라 "감지기를 끈다"였다.

여기서 하는 일은 그 중간이다 — **IR 이 실제로 소유한 신호만** 커버로 인정한다. 판정 근거는
카탈로그의 ``signal_coverage`` 선언(신호 family → 카탈로그 심볼)이고, 선언되지 않은 family 는
언제나 '커버 아님'이다(fail-close). 표현 자체가 불가능한 family 는 ``expressible: false`` 로
**명시 선언**한다 — 빠뜨린 것과 못 하는 것을 드리프트 가드가 구분할 수 있어야 하기 때문이다.

입도: 기본 판정(:func:`covered_families`)은 **family 단위**다. 한 family 안에 절이 여럿인 원문
("장바구니에 3개 이상 그리고 미결제 유형")에서 IR 이 그중 하나만 표현했어도 커버로 읽힌다.
그 정밀형이 :func:`covered_signal_spans` — **근거 스팬 단위**로 "IR 원자의 Evidence 가 그 신호의
원문 구간을 덮는가"를 답한다. 신호 추출기가 스팬을 주는 신호부터 이쪽으로 옮긴다(현재는 구매
부재). 스팬을 남기지 않는 신호는 계속 family 단위이고, 그 한계는
`tests/test_canonical_signal_coverage_drift.py` 가 이름으로 고정한다.

스팬 판정이 필요한 이유는 하나의 실제 사고다. 어떤 소스는 별개 사건이 아니라 **같은 사건의 상태**라
(KEEP_YN='Y' 미결제 장바구니), 그 상태어가 원문의 부정 표면을 소유하는 일이 정당하다. 그런데
"IR 증거가 그 부정을 덮는가"만 보면 모델이 문장 전체를 증거로 적었을 때 **다른 절의 조건까지**
삼킨다('장바구니에 담은 회원 중 구매 이력이 없는'). 그래서 소유 판정의 근거는 덮기가 아니라
**같은 절인가**이고, 그 규칙은 카탈로그의 ``selected_by`` 선언이 갖는다
(:mod:`event_state_selection`). 감지기는 케이스별 어댑터를 부르지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import event_ir
import event_state_selection

Span = tuple[int, int]

CATALOG_SECTION = "signal_coverage"

# 카탈로그가 선언할 수 있는 항목 키. 미지의 키는 오타이므로 드리프트 가드가 잡는다.
DECLARATION_KEYS = frozenset({"sources", "fields", "negated_sources", "expressible", "note"})


def _declarations(catalog: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(catalog, Mapping):
        return {}
    section = catalog.get(CATALOG_SECTION)
    if not isinstance(section, Mapping):
        nested = catalog.get("semantic_catalog")
        section = nested.get(CATALOG_SECTION) if isinstance(nested, Mapping) else None
    if not isinstance(section, Mapping):
        return {}
    return {
        str(family): declaration
        for family, declaration in section.items()
        if isinstance(declaration, Mapping)
    }


def _expression(plan: Mapping[str, Any]) -> event_ir.Condition | None:
    payload = plan.get("event_expression")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("expression"), Mapping):
        return None
    try:
        return event_ir.condition_from_dict(payload["expression"])
    except event_ir.IrSchemaError:
        # 파손 IR 은 아무것도 소유하지 못한다 — 커버 판정에서도 fail-close.
        return None


def covered_families(
    plan: Mapping[str, Any], catalog: Mapping[str, Any] | None
) -> frozenset[str]:
    """이 플랜의 Event IR 이 소유하고 있는 신호 family 집합.

    ``expressible: false`` 로 선언된 family 는 IR 이 무엇을 담고 있든 커버가 아니다 —
    그 축은 Event IR 대수에 자리가 없다는 선언이므로, 우연한 심볼 일치로 덮이면 안 된다.
    """
    expression = _expression(plan)
    if expression is None:
        return frozenset()

    present_sources = event_ir.sources(expression)
    present_fields = event_ir.field_names(expression)
    negated_sources = {
        view.source for view in event_ir.existence_views(expression) if view.negated
    }

    covered: set[str] = set()
    for family, declaration in _declarations(catalog).items():
        if declaration.get("expressible") is False:
            continue
        wanted_sources = {str(item) for item in declaration.get("sources") or []}
        wanted_fields = {str(item) for item in declaration.get("fields") or []}
        wanted_negated = {str(item) for item in declaration.get("negated_sources") or []}
        if (
            (wanted_sources & present_sources)
            or (wanted_fields & present_fields)
            or (wanted_negated & negated_sources)
        ):
            covered.add(family)
    return frozenset(covered)


def _evidence_span(query: str, evidence: event_ir.Evidence | None) -> Span | None:
    """근거가 원문과 **글자까지 일치**할 때만 스팬으로 인정한다(어긋난 좌표는 소유가 아니다)."""
    if evidence is None:
        return None
    start, end = evidence.start, evidence.end
    if not (
        isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= len(query)
        and query[start:end] == evidence.text
    ):
        return None
    return start, end


def _wanted(declaration: Mapping[str, Any], key: str) -> set[str]:
    return {str(item) for item in declaration.get(key) or []}


def covered_signal_spans(
    query: str, plan: Mapping[str, Any], catalog: Mapping[str, Any] | None
) -> dict[str, tuple[Span, ...]]:
    """family → 이 플랜이 원문에서 **근거 스팬 단위로** 소유한 구간들.

    소유 근거는 둘이다.

    * IR 원자의 Evidence — 그 원자가 family 의 소스/필드/부정 소스를 담고 있을 때.
    * 카탈로그 ``selected_by`` 선언 — IR 에 상태 소스가 실재하고, 그 상태가 대체하는 부정 사건의
      표면이 기반 사건 표면과 **같은 절**에 있을 때(:mod:`event_state_selection`).

    ``expressible: false`` family 는 여기서도 아무것도 소유하지 못한다.
    """
    if not isinstance(query, str) or not query:
        return {}
    expression = _expression(plan)
    if expression is None:
        return {}

    atoms = [
        (atom, _evidence_span(query, atom.evidence))
        for atom, _negated in event_ir.iter_signed_atoms(expression)
    ]
    negated_views = [
        view for view in event_ir.existence_views(expression) if view.negated
    ]
    state_owned = event_state_selection.owned_negation_spans(
        query, event_ir.sources(expression), catalog
    )

    covered: dict[str, set[Span]] = {}
    for family, declaration in _declarations(catalog).items():
        if declaration.get("expressible") is False:
            continue
        wanted_sources = _wanted(declaration, "sources")
        wanted_fields = _wanted(declaration, "fields")
        wanted_negated = _wanted(declaration, "negated_sources")
        spans: set[Span] = set()
        if wanted_sources or wanted_fields:
            for atom, span in atoms:
                if span is None:
                    continue
                if (wanted_sources & event_ir.sources(atom)) or (
                    wanted_fields & event_ir.field_names(atom)
                ):
                    spans.add(span)
        for view in negated_views:
            if view.source in wanted_negated:
                span = _evidence_span(query, view.evidence)
                if span is not None:
                    spans.add(span)
        for event, event_spans in state_owned.items():
            if event in wanted_negated:
                spans.update(event_spans)
        if spans:
            covered[family] = tuple(sorted(spans))
    return covered


def owns_signal_span(
    query: str,
    plan: Mapping[str, Any],
    catalog: Mapping[str, Any] | None,
    family: str,
    span: Span | None,
) -> bool:
    """이 원문 구간을 그 family 가 소유하는가 — 소유 스팬 하나가 구간을 **덮어야** 한다."""
    if span is None:
        return False
    start, end = span
    return any(
        owned_start <= start and end <= owned_end
        for owned_start, owned_end in covered_signal_spans(query, plan, catalog).get(
            str(family), ()
        )
    )


def owns_all_signal_spans(
    query: str,
    plan: Mapping[str, Any],
    catalog: Mapping[str, Any] | None,
    family: str,
    spans: Iterable[Span | None],
) -> bool:
    """감지기가 잡은 그 신호의 **모든** 표면 구간을 소유했는가.

    'any' 가 아니라 'all' 인 것이 요점이다. 한 문장에 같은 신호가 두 절로 나오면
    ('… 담아두고 구매하지 않은 회원 중 구매 이력도 없는 회원') 한 구간만 소유한 IR 이 나머지를
    조용히 지운다. 구간을 못 읽은 표면(``None``)이 하나라도 있으면 소유가 아니다.
    """
    candidates = list(spans)
    return (
        bool(candidates)
        and all(span is not None for span in candidates)
        and all(owns_signal_span(query, plan, catalog, family, span) for span in candidates)
    )


def declared_families(catalog: Mapping[str, Any] | None) -> frozenset[str]:
    """선언된 모든 family(표현 가능 여부 무관). 드리프트 가드의 입력."""
    return frozenset(_declarations(catalog))


def inexpressible_families(catalog: Mapping[str, Any] | None) -> frozenset[str]:
    """Event IR 대수가 표현할 수 없다고 **명시 선언**된 family."""
    return frozenset(
        family
        for family, declaration in _declarations(catalog).items()
        if declaration.get("expressible") is False
    )


def declaration_symbols(catalog: Mapping[str, Any] | None) -> dict[str, set[str]]:
    """family → 그 family 가 참조하는 카탈로그 심볼(소스·필드). 실재 검증용."""
    symbols: dict[str, set[str]] = {}
    for family, declaration in _declarations(catalog).items():
        symbols[family] = {
            str(item)
            for key in ("sources", "fields", "negated_sources")
            for item in declaration.get(key) or []
        }
    return symbols
