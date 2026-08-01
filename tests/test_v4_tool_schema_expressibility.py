"""V4 strict 도구 스키마 표현 가능성 가드 — closed-empty 노드 0개 불변식.

strict 함수 호출은 properties 없는 object 를 ``additionalProperties: false`` 빈 객체로 닫는다.
그런 노드는 LLM 이 ``{}`` 또는 ``null`` 로만 채울 수 있어, 그 슬롯이 필요한 프롬프트는 조용히
오모델링되거나(가장 가까운 표현 가능한 슬롯으로 우회) junk unresolved 로 차단된다. 실제 사고
3건이 전부 이 형태였다: 장바구니 이탈 → entity_set_condition 오모델링, entity_set window ``{}``
→ invalid_derived_set_window 재시도 소진, 미접속 6개월 → purchase_inactivity(미구매) 둔갑.

여기서 강제하는 계약:
  1. LLM 에 노출되는 strict 스키마에 closed-empty 노드가 하나도 없어야 한다.
     새 target_user 슬롯은 targeting_ir.SLOT_SHAPES 한 항목(설명 + properties + coerce)으로
     추가하고 campaign_plan_v4 의 _slot_schema() 로 배선하라. LLM 이 채울 수 없는(물리 스키마
     참조 등) 필드는 _APPLICATION_OWNED_* 로 노출 자체를 빼라 — 빈 껍데기로 광고하지 마라.
  2. SLOT_SHAPES 의 target_user 슬롯은 전부 실제로 노출돼 있어야 한다(레지스트리에는 있는데
     도구에는 없는 반대 방향 드리프트 차단).
  3. SLOT_SHAPES 의 target_user object 조각은 properties 를 선언해야 한다(1의 근원 차단).

여기서 실패하면 프로덕션 프롬프트가 아니라 CI 가 먼저 알려 준다.
"""

from __future__ import annotations

import targeting_ir
from query_structurer.campaign_plan_v4 import (
    _APPLICATION_OWNED_TARGET_USER_FIELDS,
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
)


def _closed_empty(node: object) -> bool:
    return (
        isinstance(node, dict)
        and node.get("type") == "object"
        and node.get("additionalProperties") is False
        and not node.get("properties")
    )


def _collect_closed_empty_paths(node: object, path: str, holes: list[str]) -> None:
    if not isinstance(node, dict):
        return
    if _closed_empty(node):
        holes.append(path)
        return
    for key, child in (node.get("properties") or {}).items():
        _collect_closed_empty_paths(child, f"{path}.{key}", holes)
    if isinstance(node.get("items"), dict):
        _collect_closed_empty_paths(node["items"], path + "[]", holes)
    for branch in node.get("anyOf") or []:
        if isinstance(branch, dict) and branch.get("type") != "null":
            _collect_closed_empty_paths(branch, path, holes)
    for name, child in (node.get("$defs") or {}).items():
        _collect_closed_empty_paths(child, f"$defs.{name}", holes)


def test_llm_schema_has_no_inexpressible_nodes() -> None:
    holes: list[str] = []
    _collect_closed_empty_paths(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA, "$", holes)
    assert not holes, (
        "V4 strict 도구 스키마에 LLM 이 표현할 수 없는(빈 닫힌 객체) 노드가 있다: "
        f"{holes}\n슬롯이면 targeting_ir.SLOT_SHAPES 조각에 properties 를 선언해 배선하고, "
        "모델이 채우면 안 되는 필드면 _APPLICATION_OWNED_* 로 노출을 빼라."
    )


def test_every_target_user_slot_shape_is_exposed() -> None:
    target_user = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]["target_user"]
    exposed = set(target_user.get("properties") or {})
    expected = {
        shape.name
        for shape in targeting_ir.structured_slot_shapes()
        if shape.container == "target_user"
    } - set(_APPLICATION_OWNED_TARGET_USER_FIELDS)
    missing = expected - exposed
    assert not missing, (
        f"SLOT_SHAPES 에 선언된 target_user 슬롯이 V4 도구 스키마에 노출되지 않았다: {sorted(missing)}\n"
        "campaign_plan_v4._TARGET_USER_SCHEMA 에 _slot_schema()로 배선하라."
    )


def test_target_user_slot_fragments_declare_properties() -> None:
    undeclared = [
        shape.name
        for shape in targeting_ir.structured_slot_shapes()
        if shape.container == "target_user"
        and shape.schema.get("type") == "object"
        and not shape.schema.get("properties")
    ]
    assert not undeclared, (
        f"properties 없는 object 슬롯 조각은 strict 변환에서 빈 닫힌 객체가 된다: {undeclared}"
    )
