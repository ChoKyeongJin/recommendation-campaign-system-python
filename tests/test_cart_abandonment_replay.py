from __future__ import annotations

import json

import networkx as nx
import pytest

import audience_runtime
import canonical_signal_coverage
import event_ir
import event_state_selection
import graph_rag
import sql_guard
from query_structurer.campaign_plan_v4 import (
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.semantic_ir import extract_literal_bindings
from query_structurer.structurer import LLMCampaignQueryPlanV4Structurer
from query_structurer.types import QueryStructuringInput, StructuringContext

QUERY = "최근 30일 장바구니에 담아두고 결제하지 않은 회원"
STATE_SELECTION_FILTER = "event_state_selection.active_cart"


def _catalog() -> dict:
    return audience_runtime.catalog_snapshot()


def _base_payload(requirement: dict) -> dict:
    return {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": requirement,
        "semantic_plan": {"nodes": []},
    }


def _live_missing_period_payload() -> dict:
    return _base_payload({
        "expression": None,
        "issues": [{
            "code": "missing_argument",
            "argument": "period",
            "message": (
                "The query specifies '최근' without a duration, which is "
                "insufficient to define a time period."
            ),
            # Exact malformed evidence offsets observed in final5.  The closed
            # synthesis runs before model-owned issue evidence is trusted.
            "evidence": {"text": "최근 30일", "start": 0, "end": 3},
        }],
    })


def _simple_timed_exists(source: str, *, window_type: str = "relative") -> dict:
    return {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": source},
            "where": {
                "type": "time_filter",
                "field": {"type": "field", "name": f"{source}.occurred_at"},
                "window": {
                    "type": window_type,
                    "value": 30,
                    "unit": "days",
                    "label": "30일",
                },
            },
        },
        "evidence": {"text": QUERY, "start": 0, "end": len(QUERY)},
    }


def _live_overconstrained_payload() -> dict:
    return _base_payload({
        "expression": {
            "type": "and",
            "operands": [
                _simple_timed_exists("cart"),
                {
                    "type": "not",
                    "operand": _simple_timed_exists("purchase"),
                },
            ],
        },
        "issues": [],
    })


def _compile(plan: dict) -> dict:
    candidate = graph_rag.compile_executable_plan(dict(plan))
    assert candidate is not None
    assert candidate["id"] == "sql_template:event_expression"
    assert candidate["validation"]["issues"] == []
    guarded = sql_guard.validate_sql(
        candidate["sql"],
        allowed_tables=set(candidate["tables"]),
        default_limit=None,
        dialect="tsql",
        schema_columns=sql_guard.load_schema_columns(),
    )
    assert guarded["is_valid"], guarded["issues"]
    return candidate


def test_active_cart_catalog_mapping_owns_unpaid_state_and_added_time() -> None:
    catalog = audience_runtime.resolve_audience_catalog()
    generic_cart = catalog.compiler_events["cart"]
    active_cart = catalog.compiler_events["active_cart"]

    assert generic_cart.time_column == "UPD_DT"
    assert generic_cart.extra_predicates == ()
    assert active_cart.table == "ODS_MALL_OMS_CART"
    assert active_cart.event_subject_key == "CART_ID"
    assert active_cart.subject_key == "MEMBER_ID"
    assert active_cart.time_column == "INS_DT"
    assert active_cart.extra_predicates == ("{alias}.KEEP_YN = 'Y'",)
    assert active_cart.from_sql == ""


def test_live_11_missing_period_response_is_resolved_on_the_first_attempt() -> None:
    calls = 0
    events: list[str] = []

    def complete(_messages: list[dict[str, str]]) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(_live_missing_period_payload(), ensure_ascii=False)

    plan = LLMCampaignQueryPlanV4Structurer(
        complete,
        max_retries=2,
        on_event=lambda name, _payload: events.append(name),
    ).structure(QueryStructuringInput(
        query=QUERY,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))

    assert calls == 1
    assert "campaign_query_plan_v4_success" in events
    assert "campaign_query_plan_v4_fallback" not in events
    assert plan["semantic_ir"]["status"] == "resolved"
    expression = event_ir.condition_from_dict(
        plan["event_expression"]["expression"]
    )
    views = event_ir.existence_views(expression)
    assert [(view.source, view.negated) for view in views] == [
        ("active_cart", False)
    ]
    assert isinstance(views[0].window, event_ir.RollingWindow)
    assert (views[0].window.value, views[0].window.unit) == (30, "day")
    assert any(
        decision.get("filter") == STATE_SELECTION_FILTER
        for decision in plan.get("decisions", [])
    )

    candidate = _compile(plan)
    sql = candidate["sql"]
    assert "ODS_MALL_OMS_CART EAC" in sql
    assert "EAC.CART_ID = B.MEMBER_ID" in sql
    assert "EAC.KEEP_YN = 'Y'" in sql
    assert "EAC.INS_DT >= DATEADD(DAY, -30, GETDATE())" in sql
    assert "CRM_CM_PRODUCT" not in sql
    assert "CRM_SL_ORDERHEADERMALL" not in sql
    assert "CRM_SL_ORDERDETAILMALL" not in sql
    assert "NOT EXISTS (" not in sql
    assert graph_rag._deterministic_dropped_conditions(QUERY, plan) == []
    assert graph_rag._verify_sql_semantic_invariants(
        QUERY, plan, sql, []
    ) == {"ran": True, "ok": True, "issues": []}


def test_live_11_two_source_model_shape_is_narrowed_to_active_cart() -> None:
    plan = attach_campaign_query_plan_v4_identity(
        _live_overconstrained_payload(),
        QUERY,
        current_date="2026-08-04",
    )

    expression = event_ir.condition_from_dict(
        plan["event_expression"]["expression"]
    )
    assert [(view.source, view.negated) for view in event_ir.existence_views(expression)] == [
        ("active_cart", False)
    ]
    # active_cart is not a global proof of member-wide purchase absence: the
    # family stays uncovered.  The catalog-declared state selection owns this
    # request only after rechecking the source text and the complete IR.
    assert "purchase_absence" not in canonical_signal_coverage.covered_families(
        plan, audience_runtime.load_audience_catalog_config()
    )
    assert event_state_selection.owns_state_selection_claim(
        QUERY,
        plan["event_expression"]["expression"],
        plan["literal_bindings"],
        _catalog(),
    )


def test_live_11_full_sql_result_keeps_candidate_through_all_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = LLMCampaignQueryPlanV4Structurer(
        lambda _messages: json.dumps(
            _live_missing_period_payload(), ensure_ascii=False
        ),
        max_retries=0,
    ).structure(QueryStructuringInput(
        query=QUERY,
        context=StructuringContext(
            current_date="2026-08-04", timezone="Asia/Seoul"
        ),
    ))
    plan["strict_source_coverage"] = True
    monkeypatch.setattr(
        graph_rag,
        "_verify_sql_semantics",
        lambda *_args, **_kwargs: {
            "ran": True,
            "status": "pass",
            "faithful": True,
            "reason": "",
            "issues": [],
        },
    )

    result = graph_rag.build_sql_result(
        graph=nx.Graph(),
        query=QUERY,
        query_plan=plan,
        context_nodes=[],
        schema_path=sql_guard.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=QUERY,
        semantic_verification_model="replay-verifier",
    )

    assert result["is_success"], result
    assert result["failure_reason"] is None
    assert result["candidate_count"] == 1
    assert result["selected"]["id"] == "sql_template:event_expression"
    assert result["selected"]["validation"]["is_valid"]
    assert result["selected"]["join_keys"]["is_valid"]
    assert result["selected"]["coverage"]["is_satisfied"]
    assert result["delivery_validation"]["is_satisfied"]
    assert "EAC.CART_ID = B.MEMBER_ID" in result["sql"]
    assert "EAC.KEEP_YN = 'Y'" in result["sql"]
    assert "EAC.INS_DT >= DATEADD(DAY, -30, GETDATE())" in result["sql"]
    assert "CRM_SL_ORDER" not in result["sql"]
    assert "NOT EXISTS (" not in result["sql"]


@pytest.mark.parametrize(
    ("query", "why"),
    [
        ("30일 전 장바구니에 담아두고 결제하지 않은 회원", "past-point-not-rolling"),
        ("최근 30일 장바구니에 담은 회원", "no-negation"),
        ("최근 30일 결제하지 않은 회원", "no-cart-surface"),
        ("최근 30일 장바구니에 담아두고 다른 상품을 결제하지 않은 회원", "scope-noun-between"),
        ("최근 30일 장바구니에 담아두고 결제하지 않은 여성 회원", "non-frame-residue"),
        ("최근 30일 장바구니에 담아두고 결제하지 않은 회원 또는 VIP 회원", "or-branch"),
        ("최근 30일 장바구니에 담은 회원 중 구매 이력이 없는 회원", "different-clause"),
        # 구조가 아니라 **어휘**의 한계다. 띄어 쓴 부정 부사('안 한')는 generic_negation 에 없고
        # (event_negation_marker 의 '안한'은 공백을 지운 문자열용이다), 그 어휘를 넓히는 일은
        # 구매 존재/부재 판정 전체를 흔들어 별건이다 — NOTES_fix §2-1 의 '결제'와 같은 성격.
        ("최근 30일 장바구니에 담아두고 결제 안 한 회원", "spaced-negation-adverb-lexicon-gap"),
    ],
)
def test_active_cart_synthesis_refuses_nearby_but_distinct_meanings(
    query: str, why: str
) -> None:
    assert event_state_selection.synthesize_state_selection(
        query, extract_literal_bindings(query, current_date="2026-08-04"), _catalog()
    ) is None, why


@pytest.mark.parametrize(
    "query",
    [
        # 문형이 아니라 절 구조로 판정하므로, 뜻이 같은 어미·요청어·표기 변형은 같은 답이다.
        "최근 30일 장바구니에 담아두고 구매하지 않은 회원을 찾아줘.",
        "최근 30일 카트에 담아두고 결제하지 않은 고객 추출해 주세요",
        "최근 30일 장바구니에 담아두고 결제하지 않은 고객님",
        "최근 3개월 장바구니에 담아두고 결제하지 않은 회원",
    ],
)
def test_active_cart_synthesis_generalizes_beyond_one_sentence_template(
    query: str,
) -> None:
    claim = event_state_selection.synthesize_state_selection(
        query, extract_literal_bindings(query, current_date="2026-08-04"), _catalog()
    )

    assert claim is not None
    assert claim.selection.selected == "active_cart"
    views = event_ir.existence_views(claim.expression)
    assert [(view.source, view.negated) for view in views] == [("active_cart", False)]


def test_generic_cart_source_does_not_claim_general_purchase_absence() -> None:
    query = "최근 30일 장바구니에 담은 회원 중 구매 이력이 없는 회원"
    generic_cart = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("cart"),
            event_ir.TimeFilter(
                event_ir.FieldRef("cart.occurred_at"),
                event_ir.RollingWindow(30, "day"),
            ),
        ),
        evidence=event_ir.Evidence(query, 0, len(query)),
    )
    plan = {
        "target_user": {},
        "event_expression": {"expression": generic_cart.to_dict()},
    }

    covered = canonical_signal_coverage.covered_families(
        plan, audience_runtime.load_audience_catalog_config()
    )
    assert "cart" in covered
    assert "purchase_absence" not in covered
    assert any(
        "구매 미발생" in warning
        for warning in graph_rag._deterministic_dropped_conditions(query, plan)
    )


def test_active_cart_cannot_hide_a_distinct_member_wide_purchase_absence() -> None:
    query = "최근 30일 장바구니에 담은 회원 중 구매 이력이 없는 회원"
    active_cart = event_ir.Exists(
        event_ir.Filter(
            event_ir.Source("active_cart"),
            event_ir.TimeFilter(
                event_ir.FieldRef("active_cart.occurred_at"),
                event_ir.RollingWindow(30, "day"),
            ),
        ),
        evidence=event_ir.Evidence(query, 0, len(query)),
    )
    plan = attach_campaign_query_plan_v4_identity(
        _base_payload({"expression": active_cart.to_dict(), "issues": []}),
        query,
        current_date="2026-08-04",
    )

    assert not event_state_selection.owns_state_selection_claim(
        query,
        plan["event_expression"]["expression"],
        plan["literal_bindings"],
        _catalog(),
    )
    assert "purchase_absence" not in canonical_signal_coverage.covered_families(
        plan, audience_runtime.load_audience_catalog_config()
    )
    # 부채 ②의 핵심: IR 증거가 문장 전체라 "증거가 부정 표면을 덮는가"만 보면 통과한다.
    # 소유 판정이 **같은 절인가**이므로 이 구간은 소유되지 않는다.
    absence_span = (query.index("구매"), query.index("없는") + len("없는"))
    assert not canonical_signal_coverage.owns_signal_span(
        query,
        plan,
        audience_runtime.load_audience_catalog_config(),
        "purchase_absence",
        absence_span,
    )
    dropped = graph_rag._deterministic_dropped_conditions(query, plan)
    assert any("구매 미발생" in warning for warning in dropped)

    candidate = graph_rag.compile_executable_plan(plan)
    assert candidate is not None
    invariants = graph_rag._verify_sql_semantic_invariants(
        query, plan, candidate["sql"], dropped
    )
    assert not invariants["ok"]
    assert any(
        issue.get("type") == "source_condition_dropped"
        for issue in invariants["issues"]
    )


def test_live_10_compound_ranked_set_is_not_declared_unsupported() -> None:
    """이 조합은 미지원이 아니다 — 방출 실패다.

    **기대값 변경(2026-08-07).** 예전 이름은 `..._stays_unsupported` 였고 이 payload 가
    `semantic_ir.status == "unsupported"` 로 끝나는 것을 고정했다. 그 고정이 틀렸다는 근거는
    `tests/test_ranked_set_cardinality.py` 다 — 같은 의미의 canonical Event IR 이 지금 코드에서
    그대로 SQL 로 컴파일된다(COUNT(DISTINCT …) >= 2 over TOP 5). 즉 실패의 위치는 실행 계층이
    아니라 방출 계층이고, 그것을 '표현할 수 없습니다'로 부르면 없는 한계를 있다고 말하게 된다.

    바뀌지 않은 것: 표현이 없으므로 SQL 은 여전히 나가지 않는다(fail-close). 바뀐 것은 그
    실패를 **무엇이라고 부르는가** 하나다.
    """
    query = "작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
    payload = _base_payload({
        "expression": None,
        "issues": [{
            "code": "unsupported_semantics",
            "argument": "ranked_set_cardinality",
            "message": "랭킹 파생 집합과 회원별 교집합 개수 임계는 지원하지 않습니다.",
            "evidence": {
                "text": "가장 많이 팔린 상품 5개 중 2개 이상 구매",
                "start": query.index("가장"),
                "end": query.index("구매") + len("구매"),
            },
        }],
    })

    plan = attach_campaign_query_plan_v4_identity(
        payload, query, current_date="2026-08-04"
    )

    semantic_ir = plan["semantic_ir"]
    assert semantic_ir["status"] != "unsupported", (
        "canonical 경로가 컴파일하는 의미가 '표현할 수 없다'로 종결됐다"
    )
    assert semantic_ir["failure_kind"] == "system_failure"
    assert semantic_ir["failure_reason"] == "semantic_emission_failure"
    assert plan["audience_emission_failures"][0]["obligation_kind"] == "ranked_entity_set"
    assert plan.get("event_expression") is None
    assert not any(
        decision.get("filter") == STATE_SELECTION_FILTER
        for decision in plan.get("decisions", [])
    )
