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

입도 주의(알려진 한계): 판정은 **family 단위**다. 한 family 안에 절이 여럿인 원문
("장바구니에 3개 이상 그리고 미결제 유형")에서 IR 이 그중 하나만 표현했어도 커버로 읽힌다.
정확한 최종형은 근거 스팬 단위(IR 원자의 Evidence 가 그 신호의 원문 구간을 덮는가)이지만,
`_prompt_signal_signature` 의 신호 추출기 대부분이 스팬을 남기지 않아 선행 작업이 필요하다.
`tests/test_canonical_signal_coverage_drift.py` 가 이 한계를 이름으로 고정한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import event_ir

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
