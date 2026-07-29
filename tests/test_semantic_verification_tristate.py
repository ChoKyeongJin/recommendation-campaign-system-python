from __future__ import annotations

import graph_rag
from target_spec import RequirementCheck, TargetSpecification
from semantic_mapping import decide
from semantic_validation import to_legacy_semantic_verification


def test_review_verdict_is_non_blocking_and_keeps_reason() -> None:
    verdict = graph_rag._normalize_semantic_verification_verdict({
        "status": "review",
        "reason": "두 가지 해석이 모두 가능함",
        "issues": [],
    })

    assert verdict == {
        "ran": True,
        "status": "review",
        "faithful": True,
        "reason": "두 가지 해석이 모두 가능함",
        "issues": [{
            "type": "ambiguous",
            "condition": "복수 해석 가능한 요청",
            "detail": "두 가지 해석이 모두 가능함",
        }],
    }
    assert graph_rag._semantic_verification_is_failure(verdict) is False


def test_fail_verdict_remains_blocking_with_concrete_fallback_issue() -> None:
    verdict = graph_rag._normalize_semantic_verification_verdict({
        "status": "fail",
        "reason": "명시한 기간 조건이 누락됨",
        "issues": [],
    })

    assert verdict is not None
    assert verdict["faithful"] is False
    assert verdict["issues"][0]["type"] == "dropped"
    assert verdict["issues"][0]["detail"] == "명시한 기간 조건이 누락됨"
    assert graph_rag._semantic_verification_is_failure(verdict) is True


def test_legacy_boolean_verdict_is_still_supported() -> None:
    passed = graph_rag._normalize_semantic_verification_verdict({"faithful": True, "issues": []})
    failed = graph_rag._normalize_semantic_verification_verdict({"faithful": False, "issues": []})

    assert passed is not None and passed["status"] == "pass"
    assert failed is not None and failed["status"] == "fail"
    assert graph_rag._semantic_verification_is_failure(failed) is True


def test_condition_evaluation_is_included_in_semantic_contract() -> None:
    evaluation = {
        "capability": "same_product_same_order_quantity_v1",
        "grouping_unit": {"keys": ["member_no", "order_id", "product_id"]},
        "aggregation": {"function": "sum", "measure": "order_quantity"},
        "comparison": {"operator": "gte", "value": 2},
    }

    context = graph_rag._semantic_verification_contract_context({
        "condition_evaluations": [evaluation],
    })

    assert context == {"condition_evaluations": [evaluation]}


def test_delivery_contract_blocks_fail_but_not_review() -> None:
    query_plan = {
        "output_contract": {"expected_grain": "member", "requires_member_id": False},
        "semantic_conditions": [],
    }
    sql = "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B"
    review = {
        "ran": True,
        "status": "review",
        "faithful": True,
        "issues": [{
            "type": "ambiguous",
            "condition": "동시 구매",
            "detail": "복수 해석이 가능함",
        }],
    }
    failed = {
        "ran": True,
        "status": "fail",
        "faithful": False,
        "issues": [{
            "type": "spurious",
            "condition": "정상 회원",
            "detail": "원문에 없는 결과 축소 필터",
        }],
    }

    review_result = graph_rag._validate_sql_delivery_contract(
        "같은 상품을 동시 구매한 고객",
        query_plan,
        sql,
        semantic_verification=review,
    )
    fail_result = graph_rag._validate_sql_delivery_contract(
        "전체 고객",
        query_plan,
        sql,
        semantic_verification=failed,
    )

    assert review_result["failure_reasons"] == []
    assert review_result["semantic_issues"][0]["severity"] == "warning"
    assert "critical_semantic_issue" in fail_result["failure_reasons"]


def test_v2_review_legacy_adapter_is_non_blocking() -> None:
    spec = TargetSpecification(target_entity="customer")
    result = decide(
        spec,
        [RequirementCheck("R1", "ambiguous", [], "multiple_valid_interpretations")],
    )

    legacy = to_legacy_semantic_verification(result)

    assert result.status == "review"
    assert legacy["status"] == "review"
    assert legacy["faithful"] is True
