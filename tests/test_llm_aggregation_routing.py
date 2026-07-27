import graph_rag as g


QUERY = "장바구니에 상품을 담고 결제하지 않은 고객에게"


def _member_field(field: str) -> dict:
    return {
        "entity": "member",
        "field": field,
        "table": "crm_mb_baseinfo",
        "column": field,
    }


def _list_shaped_aggregation_request() -> dict:
    return {
        "targetEntity": "member",
        "outputColumns": [
            {**_member_field("MEMBER_NO"), "alias": "member_no"},
            {**_member_field("MEMBER_ID"), "alias": "member_id"},
        ],
        "filters": [],
        "groupings": [],
        "aggregations": [],
        "derivedMetrics": [],
        "sorting": [],
    }


def test_member_list_request_is_not_promoted_to_analytical_grain():
    fallback = g.build_query_plan(QUERY, parser="rules")
    plan = g._coerce_llm_query_plan(
        {
            "intent": "find_user_segment",
            "target_user": {"behaviors": ["cart_abandoner"]},
            "aggregation_request": _list_shaped_aggregation_request(),
        },
        fallback,
    )

    assert "aggregation_request" not in plan
    g._attach_query_output_contract(QUERY, plan)
    assert plan["intent"] == "find_user_segment"
    assert plan["output_contract"]["expected_grain"] == "member"
    assert plan["output_contract"]["requires_member_id"] is True


def test_real_aggregation_request_keeps_analytical_grain():
    fallback = g.build_query_plan("회원 수를 알려줘", parser="rules")
    request = _list_shaped_aggregation_request()
    request["aggregations"] = [{
        "id": "member_count",
        "function": "count_distinct",
        **_member_field("MEMBER_NO"),
        "distinct": True,
        "alias": "member_count",
    }]
    plan = g._coerce_llm_query_plan(
        {
            "intent": "analyze_aggregation",
            "target_user": {},
            "aggregation_request": request,
        },
        fallback,
    )

    assert plan["aggregation_request"]["aggregations"]
    g._attach_query_output_contract("회원 수를 알려줘", plan)
    assert plan["intent"] == "analyze_aggregation"
    assert plan["output_contract"]["expected_grain"] == "analytical"


def test_llm_cannot_invent_aggregation_for_member_targeting_query():
    fallback = g.build_query_plan(QUERY, parser="rules")
    request = _list_shaped_aggregation_request()
    request["groupings"] = [{**_member_field("MEMBER_NO"), "alias": "member_no"}]
    request["aggregations"] = [{
        "id": "count_cart_lines",
        "function": "count",
        "entity": "*",
        "field": "*",
        "table": None,
        "column": None,
        "distinct": False,
    }]

    plan = g._coerce_llm_query_plan(
        {
            "intent": "analyze_aggregation",
            "target_user": {"behaviors": ["cart_abandoner"]},
            "aggregation_request": request,
        },
        fallback,
    )

    assert "aggregation_request" not in plan
    assert plan["intent"] == "find_user_segment"
    g._attach_query_output_contract(QUERY, plan)
    assert plan["output_contract"]["expected_grain"] == "member"
