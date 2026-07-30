"""조건 소유권 재조정 — 표현형이 달라도 같은 canonical IR 이 나오는가.

배경(이 스위트가 막는 회귀): 회원 속성·지역·파생 엔터티 집합은 전용 슬롯이 실DB 컬럼까지 확정해
소유하는데, 일반 집합식 파서가 같은 어구를 한 번 더 소비해 ``unknown_operand`` 를 남기면 그 하나
때문에 플랜 전체가 clarification 으로 막혔다. 같은 요청이 "…빼줘"냐 "… 중 … 제외"냐에 따라
통과/차단으로 갈렸다.

여기서 고정하는 계약:

  1. 이미 권위 슬롯이 소유한 조건은 집합식에서 억제되고 clarification 사유가 되지 않는다.
  2. 어느 슬롯도 소유하지 못한 항목은 여전히 clarification 을 발생시킨다(그 항목만).
  3. 진짜 집합 연산(전용 슬롯이 해석하지 못한 것)은 지워지지 않는다.
  4. 소유권/억제/매칭 규칙은 JSON 정책이 소유한다 — 정책을 바꾸면 동작이 바뀐다(코드 하드코딩 아님).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import condition_reconciliation as cr
import graph_rag


# ────────────────────────────── 헬퍼 ──────────────────────────────


def _plan(prompt: str) -> dict[str, Any]:
    return graph_rag.build_query_plan(prompt, parser="rules")


def _region_filters(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in plan.get("dimension_filters", [])
        if isinstance(item, dict) and str(item.get("column", "")).upper().endswith(".SIDO")
    ]


def _requires_clarification(plan: dict[str, Any]) -> bool:
    """플랜 전체의 최종 확인요청 여부(집합식 잔여 미해결 + 권위 슬롯 충돌)."""
    expressions = [item for item in plan.get("set_expressions", []) or [] if isinstance(item, dict)]
    return any(item.get("requires_clarification") for item in expressions) or bool(
        cr.conflict_clarifications(plan)
    )


def _canonical(plan: dict[str, Any]) -> dict[str, Any]:
    """표현형 불변으로 같아야 하는 결과(성별/지역/확인요청)."""
    regions = _region_filters(plan)
    return {
        "gender": {"polarity": "exclude", "values": sorted(plan.get("exclude", {}).get("gender", []))},
        "region": {
            "polarity": regions[0]["polarity"] if regions else None,
            "values": sorted(regions[0].get("names", [])) if regions else [],
        },
        "requires_clarification": _requires_clarification(plan),
    }


EXPECTED_CANONICAL = {
    "gender": {"polarity": "exclude", "values": ["male"]},
    "region": {"polarity": "exclude", "values": ["서울"]},
    "requires_clarification": False,
}


def _operand_canonicals(plan: dict[str, Any]) -> list[str]:
    return [
        str(node.get("canonical"))
        for expression in plan.get("set_expressions", []) or []
        for node, _polarity, _path in cr.iter_set_operands(expression.get("set_ast"))
        if node.get("type") == "operand"
    ]


# ─────────────────────── 1~4. 표현형이 달라도 같은 IR ───────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        # 1) 자연스러운 구어체
        "2019년 하반기 판매량 상위 11개 상품 산 고객에서 남성도 빼주고 서울 사는 고객도 빼줘",
        # 2) LLM 재작성체(원래 집합식 파서가 발동해 막히던 표현)
        "2019년 하반기 판매량 상위 11개 상품을 구매한 고객 중 남성 제외, 서울 거주 고객 제외",
        # 3) 어순 변경(제외 절이 앞)
        "서울 거주 고객과 남성을 제외하고 2019년 하반기 판매량 상위 11개 상품을 구매한 고객",
        # 4) 재작성이 만드는 '및' 나열형
        "2019년 하반기 판매량 상위 11개 상품을 구매한 고객 중 남성 및 서울 거주 고객을 제외한 고객",
    ],
    ids=["colloquial", "rewritten", "reordered", "conjunction"],
)
def test_entity_set_exclusions_are_owned_without_clarification(prompt: str) -> None:
    """상위 N개 상품 구매 + 성별/지역 제외는 전용 슬롯이 소유하고, 집합식은 확인요청을 만들지 않는다."""
    plan = _plan(prompt)

    assert _canonical(plan) == EXPECTED_CANONICAL
    assert (plan.get("target_user") or {}).get("entity_set_condition"), "파생 엔터티 집합 조건이 사라졌다"


@pytest.mark.parametrize(
    "prompt",
    [
        "2019년 5월에 상품을 가장 많이 산 고객 100명 중 남성 제외",
        "2019년 5월에 상품을 가장 많이 산 고객 100명, 남성 제외",
    ],
)
def test_member_purchase_ranking_exclusion_is_owned_without_clarification(prompt: str) -> None:
    """회원 구매 랭킹을 일반 집합명으로 재해석해 unknown clarification을 만들지 않는다."""
    plan = _plan(prompt)

    assert plan.get("purchase_count_ranking") == {"top_n": 100}
    assert (plan.get("target_user") or {}).get("purchase_date") == {
        "from": "20190501",
        "to": "20190531",
        "label": "2019년 5월 구매",
    }
    assert (plan.get("exclude") or {}).get("gender") == ["male"]
    assert not plan.get("set_expressions"), "이미 소유된 구매 랭킹이 미해결 집합식으로 남았다"


def test_member_purchase_ranking_does_not_swallow_unknown_named_segment() -> None:
    """랭킹 문법 밖의 이름 붙은 후보군은 중복 제거로 조용히 사라지면 안 된다."""
    source = "블루 후보군 2019년 5월에 상품을 가장 많이 산 고객 100명 중 남성 제외"
    left_text = "블루 후보군 2019년 5월에 상품을 가장 많이 산 고객 100명"
    plan = {
        "purchase_count_ranking": {"top_n": 100},
        "target_user": {
            "purchase_date": {"from": "20190501", "to": "20190531", "label": "2019년 5월 구매"}
        },
        "exclude": {"gender": ["male"]},
        "set_expressions": [
            {
                "expression_id": "segment_set_expression",
                "expression_text": source,
                "set_ast": {
                    "type": "set_op",
                    "op": "-",
                    "left": {"type": "unknown_operand", "text": left_text},
                    "right": {"type": "operand", "canonical": "male", "matched_text": "남성"},
                },
                "requires_clarification": True,
                "detection": "natural",
            }
        ],
    }
    owned_span = graph_rag._purchase_count_ranking_clause_span(source, plan)
    assert owned_span == (source.index("2019년"), source.index(" 중 남성"))
    graph_rag.slot_ownership.record_owned_span(
        plan,
        owner="purchase_count_ranking",
        span=owned_span,
        source_text=source,
        reason="테스트 랭킹 절 소유",
    )

    graph_rag._drop_deterministically_owned_set_expressions(plan)

    assert len(plan["set_expressions"]) == 1


def test_llm_set_expression_is_dropped_only_with_exact_ranking_source_span() -> None:
    """LLM 집합식도 동일 좌표의 전체 좌변을 랭킹 IR이 소유할 때만 중복으로 제거한다."""
    source = "2019년 5월에 상품을 가장 많이 산 고객 100명 중 남성 제외"
    left_text = "2019년 5월에 상품을 가장 많이 산 고객 100명"
    plan = {
        "purchase_count_ranking": {"top_n": 100},
        "target_user": {
            "purchase_date": {"from": "20190501", "to": "20190531", "label": "2019년 5월 구매"}
        },
        "exclude": {"gender": ["male"]},
        "set_expressions": [
            {
                "expression_id": "segment_set_expression",
                "expression_text": source,
                "set_ast": {
                    "type": "set_op",
                    "op": "-",
                    "left": {"type": "unknown_operand", "text": left_text},
                    "right": {"type": "operand", "canonical": "male", "matched_text": "남성"},
                },
                "requires_clarification": True,
                "source": "llm_set_expression_ast",
            }
        ],
    }
    graph_rag.slot_ownership.record_owned_span(
        plan,
        owner="purchase_count_ranking",
        span=(0, len(left_text)),
        source_text=source,
        reason="테스트 랭킹 절 소유",
    )

    graph_rag._drop_deterministically_owned_set_expressions(plan)

    assert plan["set_expressions"] == []


def test_josa_variants_are_owned_by_dedicated_slots() -> None:
    """조사/표현이 바뀌어도 성별·지역은 전용 슬롯 소유이고 집합식 중복이 남지 않는다."""
    plan = _plan("서울에 사는 회원은 제외하고 남자 고객도 제외해줘")

    assert _canonical(plan) == EXPECTED_CANONICAL
    assert not plan.get("set_expressions"), "전용 슬롯이 소유한 조건이 집합식으로도 남았다"


@pytest.mark.parametrize(
    "prompt",
    [
        "남성도 빼주고 서울 사는 고객도 빼줘",
        "남성 제외, 서울 거주 고객 제외",
        "남자와 서울 지역 회원은 제외",
        "서울 회원을 빼고 남성도 빼줘",
        "서울에 거주하는 고객 및 남성 고객 제외",
    ],
)
def test_rewrite_invariant_canonical_result(prompt: str) -> None:
    """같은 뜻의 표현 변형들은 모두 같은 canonical 결과를 낸다(rewrite invariant)."""
    assert _canonical(_plan(prompt)) == EXPECTED_CANONICAL


# ─────────────────── 5. 진짜 unknown 만 clarification 으로 남는다 ───────────────────


def test_only_unowned_condition_remains_unresolved() -> None:
    plan = _plan("남성과 서울 거주 고객, 그리고 블루 후보군을 제외해줘")

    assert _requires_clarification(plan) is True
    trace = plan.get(cr.TRACE_KEY) or {}
    assert [item["raw_text"] for item in trace.get("remaining_unresolved", [])] == ["블루 후보군"]

    questions = [
        str(expression.get("clarification_question"))
        for expression in plan["set_expressions"]
        if expression.get("requires_clarification")
    ]
    assert questions, "미해결이 남았는데 확인 질문이 없다"
    joined = " ".join(questions)
    assert "블루 후보군" in joined
    assert "남성" not in joined and "서울" not in joined, "이미 해석된 조건을 다시 묻고 있다"


# ─────────────────────── 6. 진짜 집합 연산은 유지된다 ───────────────────────


def test_genuine_set_operation_survives() -> None:
    plan = _plan("VIP 고객군과 휴면 예정 고객군의 교집합에서 남성을 제외해줘")

    expressions = plan.get("set_expressions") or []
    assert expressions, "전용 슬롯이 해석하지 못한 진짜 집합 연산이 사라졌다"
    canonicals = _operand_canonicals(plan)
    assert "vip" in canonicals and any(item.startswith("inactive") for item in canonicals)
    assert _requires_clarification(plan) is False
    # 한 조건은 한 소유자만 — 성별이 집합식과 exclude.gender 양쪽에 동시에 남으면 안 된다.
    assert not ("male" in canonicals and "male" in plan.get("exclude", {}).get("gender", []))


# ─────────────────────── 7. 중복과 충돌은 다르게 처리한다 ───────────────────────


def _conflicting_plan() -> dict[str, Any]:
    """같은 속성(성별)을 두 권위 슬롯이 상반된 방향으로 잡은 플랜."""
    return {
        "intent": "find_user_segment",
        "target_user": {"gender": "male"},
        "exclude": {"gender": ["male"]},
        "dimension_filters": [],
        "set_expressions": [],
    }


def test_opposite_polarity_on_same_attribute_is_a_conflict() -> None:
    plan = _conflicting_plan()

    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    conflicts = cr.conflict_clarifications(plan)
    assert conflicts, "포함/제외가 같은 값에 동시에 걸렸는데 충돌로 잡히지 않았다"
    assert conflicts[0]["attribute"] == "gender"
    assert conflicts[0]["resolution"] == "clarify"


def test_conflict_blocks_with_ownership_path() -> None:
    """충돌은 '조건 소유권' 경로의 확인요청으로 승격된다(집합식 파싱 단계로 오인되지 않는다)."""
    plan = _conflicting_plan()
    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    validation = graph_rag.validate_required_input_conditions(plan, [])

    assert validation["is_satisfied"] is False
    assert validation["missing_conditions"][0]["path"].startswith("condition_ownership.")


def test_duplicate_is_deduplicated_not_clarified() -> None:
    """같은 방향의 중복(제외 male + 집합식 operand male)은 충돌이 아니라 억제 대상이다."""
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {"gender": ["male"]},
        "dimension_filters": [],
        "set_expressions": [
            {
                "expression_id": "segment_set_expression",
                "expression_text": "남성 고객 제외",
                "set_ast": {"type": "operand", "canonical": "male", "matched_text": "남성"},
                "requires_clarification": False,
                "detection": "natural",
            }
        ],
    }

    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    assert plan["set_expressions"] == []
    assert not cr.conflict_clarifications(plan)


def test_include_only_gender_is_not_dropped_by_reconciliation() -> None:
    """전용 슬롯이 소유하지 않은 조건은 집합식에 그대로 남는다(조용한 소실 금지)."""
    plan = _plan("남성은 포함하되 남성 고객은 제외해줘")

    assert "male" in _operand_canonicals(plan) or "male" in plan.get("exclude", {}).get("gender", [])


# ─────────────────────── 정책 엔진 단위 계약 ───────────────────────


def _owned_operands_plan() -> dict[str, Any]:
    return {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {"gender": ["male"]},
        "dimension_filters": [
            {
                "column": "CRM_MB_BASEINFO.SIDO",
                "operator": "NOT_IN",
                "names": ["서울"],
                "codes": ["서울"],
                "polarity": "exclude",
                "evidence": "서울 거주 고객 제외",
                "value_spans": [[16, 18]],
            }
        ],
        "set_expressions": [
            {
                "expression_id": "segment_set_expression",
                "expression_text": "고객 중 남성 제외, 서울 거주 고객 제외",
                "set_ast": {
                    "type": "set_op",
                    "op": "-",
                    "operation": "difference",
                    "left": {"type": "operand", "canonical": "male", "matched_text": "남성"},
                    "right": {"type": "unknown_operand", "text": "서울 거주 고객"},
                },
                "requires_clarification": True,
                "clarification_question": "집합식의 다음 항목을 정규화 사전에서 찾지 못했습니다: 서울 거주 고객",
                "detection": "natural",
            }
        ],
    }


def test_unknown_operand_alone_is_not_a_clarification_reason() -> None:
    """핵심 불변식: unknown_operand 가 있다는 사실이 아니라, 조정 후에도 미해결인지로 판단한다."""
    plan = _owned_operands_plan()

    trace = cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    assert plan["set_expressions"] == []
    assert trace["requires_clarification"] is False
    assert trace["remaining_unresolved"] == []
    methods = {entry.get("match_method") for entry in trace["trace"] if entry.get("match_method")}
    assert methods, "무엇으로 매칭했는지 흔적이 없다"
    assert all(entry.get("condition_id") and entry.get("text_hash") for entry in trace["trace"] if entry.get("condition_id"))


def test_suppression_is_policy_driven_not_hardcoded() -> None:
    """정책에서 억제를 끄면 억제하지 않는다 — 특정 문구가 아니라 정책이 동작을 정한다."""
    policy_raw = copy.deepcopy(cr.ConditionPolicyLoader.load().raw)
    policy_raw["suppression"]["set_expression_operands"]["enabled"] = False
    plan = _owned_operands_plan()

    cr.reconcile_plan(plan, policy=cr.ConditionPolicy(raw=policy_raw))

    assert len(plan["set_expressions"]) == 1, "정책을 껐는데도 집합식이 사라졌다"


def test_owner_priority_comes_from_policy() -> None:
    """우선순위 목록이 tier 순서를 정한다(코드에 슬롯 순서를 나열하지 않는다)."""
    policy = cr.ConditionPolicyLoader.load()

    assert policy.priority_index("exclude") < policy.priority_index("set_expressions")
    assert policy.is_authoritative("dimension_filters") is True
    assert policy.is_authoritative("set_expressions") is False


def test_positive_union_with_different_owners_is_preserved() -> None:
    """긍정 문맥의 OR 은 소유자가 다르면 손대지 않는다(AND 로 좁아지는 것 방지)."""
    plan = {
        "intent": "find_user_segment",
        "target_user": {"lifecycle": ["vip"]},
        "exclude": {"gender": []},
        "dimension_filters": [
            {
                "column": "CRM_MB_BASEINFO.SIDO",
                "operator": "IN",
                "names": ["서울"],
                "codes": ["서울"],
                "polarity": "include",
                "evidence": "서울 거주 고객",
                "value_spans": [[0, 2]],
            }
        ],
        "set_expressions": [
            {
                "expression_id": "segment_set_expression",
                "expression_text": "VIP 또는 서울 거주 고객",
                "set_ast": {
                    "type": "set_op",
                    "op": "+",
                    "operation": "union",
                    "left": {"type": "operand", "canonical": "vip", "matched_text": "VIP"},
                    "right": {"type": "unknown_operand", "text": "서울 거주 고객"},
                },
                "requires_clarification": True,
                "detection": "operator_scan",
            }
        ],
    }

    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    assert len(plan["set_expressions"]) == 1
    assert plan["set_expressions"][0]["set_ast"]["op"] == "+"


def test_negated_union_operands_are_suppressed_individually() -> None:
    """부정 문맥의 OR('A 또는 B 제외')은 드모르간으로 각 제외의 AND 라 개별 억제가 안전하다."""
    plan = _owned_operands_plan()
    plan["set_expressions"][0]["set_ast"] = {
        "type": "set_op",
        "op": "-",
        "operation": "difference",
        "left": {"type": "unknown_operand", "text": "고객"},
        "right": {
            "type": "set_op",
            "op": "+",
            "operation": "union",
            "left": {"type": "operand", "canonical": "male", "matched_text": "남성"},
            "right": {"type": "unknown_operand", "text": "서울 거주 고객"},
        },
    }

    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    remaining = [
        node.get("text")
        for expression in plan["set_expressions"]
        for node, _polarity, _path in cr.iter_set_operands(expression.get("set_ast"))
        if node.get("type") == "unknown_operand"
    ]
    assert "서울 거주 고객" not in remaining


def test_consumed_difference_left_preserves_negation() -> None:
    """모집합만 소유되면 남은 우변의 '제외' 의미를 전칭 노드로 보존한다(포함으로 뒤집히지 않게)."""
    plan = _owned_operands_plan()
    plan["set_expressions"][0]["set_ast"] = {
        "type": "set_op",
        "op": "-",
        "operation": "difference",
        "left": {"type": "operand", "canonical": "male", "matched_text": "남성"},
        "right": {"type": "unknown_operand", "text": "블루 후보군"},
    }

    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    ast = plan["set_expressions"][0]["set_ast"]
    assert ast["op"] == "-"
    assert ast["left"]["type"] == cr.UNIVERSE_TYPE
    assert ast["right"]["text"] == "블루 후보군"


def test_universe_node_compiles_to_tautology() -> None:
    """전칭 노드는 항진식으로 컴파일돼 '전체 - X' 가 그대로 NOT X 로 남는다."""
    compiled = graph_rag._compile_set_expression_ast(
        {
            "type": "set_op",
            "op": "-",
            "left": {"type": cr.UNIVERSE_TYPE},
            "right": {"type": "operand", "canonical": "male"},
        }
    )

    assert compiled["is_valid"] is True
    assert "NOT" in compiled["expression_sql"]


def test_policy_file_is_valid_json_and_declares_every_tier() -> None:
    """정책 파일이 스스로 일관적인가 — owners 의 tier 는 전부 우선순위 목록에 있어야 한다."""
    policy = cr.ConditionPolicyLoader.load()
    assert policy.version, "정책 버전이 없다"
    tiers = {str(spec.get("tier")) for spec in policy.owners.values()}
    assert tiers <= set(policy.priority), f"우선순위에 없는 tier: {sorted(tiers - set(policy.priority))}"
    json.dumps(policy.raw)  # 직렬화 가능해야 응답/로그로 나갈 수 있다


def test_trace_is_not_a_condition_slot() -> None:
    """조정 흔적은 계측이지 조건이 아니다 — IR 스냅샷/골든 비교를 오염시키면 안 된다."""
    import ir_snapshot

    plan = _owned_operands_plan()
    cr.reconcile_plan(plan, policy=cr.ConditionPolicyLoader.load())

    assert cr.TRACE_KEY in plan
    assert ir_snapshot.is_condition_plan_key(cr.TRACE_KEY) is False
    assert not ir_snapshot.unclassified_plan_keys(plan)
