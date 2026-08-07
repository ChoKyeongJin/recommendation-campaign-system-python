"""시간 표현의 **결속 절**(temporal scope binding) — 한 문장, 두 기간.

배경(2026-08-07)
----------------
``작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객`` 의 SQL 은 회원측 서브쿼리에 기간
필터가 없었고, 의미 검증 게이트가 이를 '기간 누락'으로 판정해 출고를 막았다. 실제로는 누락이
아니라 **결속의 문제**였다: 랭킹 집합 문장에는 기간이 둘 있을 수 있는데(모집단의 기간 / 회원이
행동한 기간) 시스템에는 슬롯이 하나뿐이었고, 그 하나를 암묵적으로 랭킹에 붙였다. 결속을 표현할
자리도, 정할 주인도, 기록할 곳도 없으니

  · 회원 행동 기간을 말한 요청은 표현 자체가 불가능했고(레거시 컴파일러는 그 창을 조용히 버렸다),
  · 모델이 회원 쪽에 창을 붙이든 말든 영수증이 똑같이 통과했으며(같은 요청, 다른 오디언스),
  · 검증기는 결속 사실을 알 길이 없어 정상 SQL 을 반려했다.

이 파일이 지키는 계약:

  1. 결속은 순수 기하다 — 시간 수식어는 자기 머리말 **앞**에 온다.
  2. 두 기간은 서로 다른 자리에 걸린다(랭킹 창 ↛ 회원 술어, 회원 창 ↛ 랭킹 술어).
  3. 원문이 회원 행동 기간을 말했으면 **항상** 그것이 우선한다.
  4. 말하지 않았으면 기본 결속(ranking_only)이고, 그 기본조차 결정 로그에 남는다.

기준일은 주입값만 쓴다(호스트 시계를 읽지 않는다).

실행: python -m pytest tests/test_temporal_scope_binding.py -q
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import canonical_audience_claims  # noqa: E402
import entity_set  # noqa: E402
import event_ir  # noqa: E402
import plan_decisions  # noqa: E402
import semantic_requirements  # noqa: E402
import semantic_verification_receipts  # noqa: E402
import targeting_expression  # noqa: E402
import temporal_clause  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.prompt import (  # noqa: E402
    build_campaign_query_plan_v4_user_prompt,
)
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402
from query_structurer.types import QueryStructuringInput, StructuringContext  # noqa: E402

CURRENT_DATE = "2026-08-07"
ENTITY_SET_CONFIG_PATH = REPO_ROOT / "docs/data/runtime/sql/member_target_filters.json"

RANKING_ONLY = "작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
BOTH_SCOPES = "작년에 가장 많이 팔린 상품 5개를 올해 구매한 고객"
MEMBERSHIP_ONLY = "가장 많이 팔린 상품 5개를 올해 구매한 고객"
ABSOLUTE_BOTH = "2019년 가장 많이 팔린 상품 10개를 2020년에 구매한 고객"


@pytest.fixture(scope="module")
def entity_set_config() -> dict[str, Any]:
    payload = json.loads(ENTITY_SET_CONFIG_PATH.read_text(encoding="utf-8"))
    return payload["entity_set_targets"]


def _windows(query: str) -> list[tuple[dict[str, Any], int, int]]:
    """기준일이 푼 창 + 원문 구간(상대 연도 '작년/올해'도 여기서 값이 된다)."""
    return [
        (binding["normalized"], binding["start"], binding["end"])
        for binding in extract_literal_bindings(query, current_date=CURRENT_DATE)
        if binding["kind"] == "date_window"
    ]


def _scopes(query: str) -> dict[str, str]:
    """결속 결과를 {절: 원문 표면어} 로."""
    return {
        binding.scope: query[binding.window_span[0]:binding.window_span[1]]
        for binding in semantic_requirements.ranked_window_scope_bindings(
            query, _windows(query)
        )
    }


# ── 1. 결속 기하 ──────────────────────────────────────────────────────────────────


def test_a_window_before_the_ranking_phrase_scopes_the_population() -> None:
    assert _scopes(RANKING_ONLY) == {temporal_clause.SCOPE_RANKING: "작년"}


def test_a_window_before_the_action_verb_scopes_the_member_behaviour() -> None:
    assert _scopes(MEMBERSHIP_ONLY) == {temporal_clause.SCOPE_MEMBERSHIP: "올해"}


def test_two_windows_bind_to_their_own_clauses() -> None:
    assert _scopes(BOTH_SCOPES) == {
        temporal_clause.SCOPE_RANKING: "작년",
        temporal_clause.SCOPE_MEMBERSHIP: "올해",
    }


def test_one_window_is_never_shared_by_two_clauses() -> None:
    """같은 창을 두 절이 나눠 갖는 해석은 만들지 않는다(공유는 명시 선언의 몫)."""
    bindings = semantic_requirements.ranked_window_scope_bindings(
        RANKING_ONLY, _windows(RANKING_ONLY)
    )

    assert len(bindings) == 1


def test_a_modifier_after_its_head_does_not_bind() -> None:
    """어순 규칙: 시간 수식어는 머리말 앞에 온다."""
    assert temporal_clause.bind_window_scopes(
        "구매한 2019년",
        windows=[({"from": "20190101", "to": "20191231"}, 4, 9)],
        anchors=((temporal_clause.SCOPE_MEMBERSHIP, 0, 3),),
    ) == ()


def test_a_clause_boundary_blocks_the_binding() -> None:
    query = "2019년, 가장 많이 팔린 상품"
    assert temporal_clause.bind_window_scopes(
        query,
        windows=[({"from": "20190101", "to": "20191231"}, 0, 5)],
        anchors=((temporal_clause.SCOPE_RANKING, 7, len(query)),),
    ) == ()


def test_a_distant_window_does_not_bind() -> None:
    query = "2019년 이야기는 접어두고 가장 많이 팔린 상품"
    assert temporal_clause.bind_window_scopes(
        query,
        windows=[({"from": "20190101", "to": "20191231"}, 0, 5)],
        anchors=((temporal_clause.SCOPE_RANKING, query.index("가장"), len(query)),),
    ) == ()


# ── 2. 의무 원장 ──────────────────────────────────────────────────────────────────


def _ranked_obligation(query: str) -> semantic_requirements.SourceRequirement:
    obligations = [
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        if semantic_requirements.obligation_kind(requirement) == "ranked_entity_set"
    ]
    assert len(obligations) == 1, [item.to_dict() for item in obligations]
    return obligations[0]


def test_an_absolute_membership_window_is_recorded_in_its_own_slot() -> None:
    value = _ranked_obligation(ABSOLUTE_BOTH).value

    assert dict(value["time_window"])["from"] == "20190101"
    assert dict(value["membership_time_window"])["from"] == "20200101"


def test_the_membership_window_span_belongs_to_this_obligation() -> None:
    """소유자를 밝히지 않으면 그 글자를 다른 파서가 다시 읽거나 주인 없는 창으로 남는다."""
    assert "2020년" in _ranked_obligation(ABSOLUTE_BOTH).source_text


def test_a_ranking_only_request_carries_no_membership_key() -> None:
    """기본 결속의 의무는 이 변경 전과 바이트 동일해야 한다(의무 id 가 값에서 파생된다)."""
    value = _ranked_obligation("2019년 가장 많이 팔린 상품 10개를 구매한 고객").value

    assert "membership_time_window" not in value


# ── 3. 프롬프트 계약 ──────────────────────────────────────────────────────────────


def _contract(query: str) -> dict[str, Any]:
    prompt = build_campaign_query_plan_v4_user_prompt(
        QueryStructuringInput(
            query=query, context=StructuringContext(current_date=CURRENT_DATE)
        )
    )
    recipe = prompt[prompt.index("[Ranked Entity Set Recipe]"):]
    line = recipe[recipe.index("Required contract per obligation:"):].splitlines()[1]
    return json.loads(line[line.index("{"):])


def test_the_recipe_carries_both_scopes_including_relative_years() -> None:
    contract = _contract(BOTH_SCOPES)

    assert contract["time_window"]["from"] == "20250101"
    assert contract["membership_time_window"]["from"] == "20260101"


def test_the_recipe_states_where_each_scope_belongs() -> None:
    prompt = build_campaign_query_plan_v4_user_prompt(
        QueryStructuringInput(
            query=RANKING_ONLY, context=StructuringContext(current_date=CURRENT_DATE)
        )
    )
    recipe = prompt[prompt.index("[Ranked Entity Set Recipe]"):]

    assert "membership_time_window scopes WHEN THE MEMBER ACTED" in recipe
    assert "the left side carries NO TimeFilter at all" in recipe


# ── 4. 영수증과 청구 게이트 ───────────────────────────────────────────────────────


def _ranked_expression(membership_window: dict[str, str] | None = None) -> dict[str, Any]:
    import test_ranked_set_cardinality as ranked

    expression = copy.deepcopy(ranked.ranked_set_cardinality_expression())
    if membership_window is not None:
        expression["left"]["relation"]["left"] = {
            "type": "filter",
            "relation": {"type": "source", "name": ranked.RANK_SOURCE},
            "where": {
                "type": "time_filter",
                "field": {
                    "type": "field",
                    "name": f"{ranked.RANK_SOURCE}.{event_ir.TIME_FIELD_SUFFIX}",
                },
                "window": {"type": "interval", **membership_window},
            },
        }
    return expression


def test_a_declared_membership_window_must_be_compiled_to_discharge_the_receipt() -> None:
    """의무가 회원 기간을 선언했으면 그 자리에 창이 있어야 방면된다."""
    value = {
        **dict(_ranked_obligation(RANKING_ONLY).value),
        "membership_time_window": {"from": "20200101", "to": "20201231"},
    }

    assert not canonical_audience_claims.ranked_obligation_is_compiled(
        event_ir.condition_from_dict(_ranked_expression()), value
    )
    assert canonical_audience_claims.ranked_obligation_is_compiled(
        event_ir.condition_from_dict(
            _ranked_expression({"start": "2020-01-01", "end_exclusive": "2021-01-01"})
        ),
        value,
    )


def _scope_issues(query: str, expression: dict[str, Any]) -> list[str]:
    return [
        issue["argument"]
        for issue in canonical_audience_claims.ranked_window_scope_issues(
            query,
            event_ir.condition_from_dict(expression),
            extract_literal_bindings(query, current_date=CURRENT_DATE),
        )
    ]


def test_the_ranking_window_on_the_member_side_is_rejected() -> None:
    """랭킹 기간을 회원 행동에 옮기면 요청보다 **좁은** 오디언스가 된다."""
    assert _scope_issues(RANKING_ONLY, _ranked_expression()) == []
    assert _scope_issues(
        RANKING_ONLY,
        _ranked_expression({"start": "2025-01-01", "end_exclusive": "2026-01-01"}),
    ) == ["ranked_window_scope.membership"]


def test_an_unbound_window_suspends_the_exclusion_rule() -> None:
    """소속을 확정하지 못한 창이 있으면 '회원 쪽은 비어야 한다'를 주장하지 않는다.

    결속 문법이 아직 읽지 못하는 어순에서 옳은 표현을 반려하지 않기 위한 보수적 규칙이다 —
    있어야 할 창이 있는지(양의 방향)는 그대로 본다.
    """
    query = "작년에 가장 많이 팔린 상품 5개를 올해 들어서 처음 구매한 고객"
    assert {binding.scope for binding in semantic_requirements.ranked_window_scope_bindings(
        query, _windows(query)
    )} == {temporal_clause.SCOPE_RANKING}

    assert _scope_issues(
        query, _ranked_expression({"start": "2026-01-01", "end_exclusive": "2027-01-01"})
    ) == []


def test_a_stated_membership_window_must_reach_the_member_side() -> None:
    """원문이 말한 회원 기간은 언제나 우선한다 — 빠지면 요청보다 넓다."""
    assert _scope_issues(BOTH_SCOPES, _ranked_expression()) == [
        "ranked_window_scope.membership"
    ]
    assert _scope_issues(
        BOTH_SCOPES,
        _ranked_expression({"start": "2026-01-01", "end_exclusive": "2027-01-01"}),
    ) == []


# ── 5. 결정 기록 ──────────────────────────────────────────────────────────────────


def _with_query_evidence(node: Any, query: str) -> Any:
    """모든 근거 구간을 이 질의의 것으로 바꾼다(결정 기록만 보는 테스트의 준비물)."""
    if isinstance(node, dict):
        return {
            key: (
                {"text": query, "start": 0, "end": len(query)}
                if key == "evidence" else _with_query_evidence(value, query)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_with_query_evidence(item, query) for item in node]
    return node


def _plan_with_decisions(query: str) -> dict[str, Any]:
    import test_ranked_set_cardinality as ranked

    return attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None, "offer_type": None, "channels": None, "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": _with_query_evidence(
                    ranked.ranked_set_cardinality_expression(), query
                ),
                "issues": [],
            },
        },
        query,
        current_date=CURRENT_DATE,
    )


def _scope_decisions(query: str) -> dict[str, Any]:
    return {
        str((entry.get("value") or {}).get("scope")): entry
        for entry in plan_decisions.decisions(_plan_with_decisions(query))
        if entry.get("filter") == temporal_clause.SCOPE_BINDING_FILTER
    }


def test_every_scope_binding_is_audited_including_the_default() -> None:
    """'랭킹 전용으로 결속했다'와 '회원 기간을 잃었다'는 기록이 없으면 구별되지 않는다."""
    decisions = _scope_decisions(RANKING_ONLY)

    assert decisions[temporal_clause.SCOPE_RANKING]["value"]["window"]["from"] == "20250101"
    membership = decisions[temporal_clause.SCOPE_MEMBERSHIP]["value"]
    assert membership["window"] is None
    assert membership["binding"] == temporal_clause.DEFAULT_SCOPE_BINDING


def test_a_stated_membership_window_is_audited_as_stated_not_default() -> None:
    membership = _scope_decisions(BOTH_SCOPES)[temporal_clause.SCOPE_MEMBERSHIP]["value"]

    assert membership["window"]["from"] == "20260101"
    assert "binding" not in membership


# ── 5-b. 의미 검증 게이트와의 대조 ────────────────────────────────────────────────
#
# 이 결함의 증상이 바로 여기였다: 정상 SQL 이 '기간 누락'으로 반려됐다. 면제는 결정 로그를
# 근거로만 서고, 회원 기간이 정말 빠진 SQL 은 그대로 막혀야 한다.

RANKING_ONLY_SQL = (
    "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE EXISTS ("
    "SELECT 1 FROM CRM_SL_ORDERDETAILMALL OD WHERE OD.MEMBER_NO = B.MEMBER_NO AND EXISTS ("
    "SELECT 1 FROM (SELECT TOP 5 OD.PRODUCT_ID AS entity_key FROM CRM_SL_ORDERDETAILMALL OD "
    "WHERE OD.ORDER_DATE >= '20250101' AND OD.ORDER_DATE <= '20251231' "
    "GROUP BY OD.PRODUCT_ID) ER1 WHERE OD.PRODUCT_ID = ER1.entity_key))"
)


def _dropped_period_issue(condition: str) -> dict[str, Any]:
    return {"type": "dropped", "condition": condition, "detail": ""}


def test_a_ranking_scope_binding_refutes_the_missing_period_verdict() -> None:
    """판정문이 원문 어휘('작년')로 쓰여도 같은 창임을 알아본다."""
    plan = _plan_with_decisions(RANKING_ONLY)

    assert semantic_verification_receipts.window_scope_issue_is_covered(
        _dropped_period_issue("회원의 구매 기간 비교(원문: '작년에 가장 많이 팔린 …')"),
        plan,
        RANKING_ONLY_SQL,
    )


def test_an_unrelated_dropped_verdict_is_not_exempted() -> None:
    plan = _plan_with_decisions(RANKING_ONLY)

    assert not semantic_verification_receipts.window_scope_issue_is_covered(
        _dropped_period_issue("성별 조건이 SQL 에 없음"), plan, RANKING_ONLY_SQL
    )


def test_a_missing_membership_window_is_never_exempted() -> None:
    """회원 기간이 결속됐는데 SQL 에 없으면 누락 판정은 옳다 — 면제하지 않는다."""
    plan = _plan_with_decisions(BOTH_SCOPES)

    assert not semantic_verification_receipts.window_scope_issue_is_covered(
        _dropped_period_issue("'작년' 기간이 회원 구매에 반영되지 않음"),
        plan,
        RANKING_ONLY_SQL,
    )


def test_the_verifier_is_told_which_clause_each_window_belongs_to() -> None:
    context = semantic_verification_receipts.render_window_scope_context(
        _plan_with_decisions(RANKING_ONLY)
    )

    assert "[기간의 결속 절]" in context
    assert '"scope": "ranking"' in context
    # 회원 절은 창이 null 이고, 그것이 누락이 아니라는 사실까지 함께 알린다.
    assert '"window": null' in context
    assert "누락(dropped)이 아니다" in context


# ── 6. 레거시 파생집합 컴파일러 ───────────────────────────────────────────────────


def _legacy_node(member_window: dict[str, Any] | None) -> dict[str, Any]:
    return entity_set.entity_set_node_from_ast(entity_set.build_derived_set_ast(
        member_relation="purchase",
        rank_relation="purchase",
        entity="product",
        measure="sales_quantity",
        direction="top",
        limit=5,
        window={"from": "20190101", "to": "20191231", "label": "2019년"},
        member_window=member_window,
    ))


def test_the_member_window_compiles_into_the_outer_scope(
    entity_set_config: dict[str, Any],
) -> None:
    predicate = entity_set.compile_entity_set_predicate(
        _legacy_node({"from": "20200101", "to": "20201231", "label": "2020년"}),
        entity_set_config,
        "B",
        "MEMBER_NO",
    )

    assert predicate is not None
    outer, inner = predicate.split("IN (", 1)
    # 회원 기간은 바깥(회원 연결) 스코프에, 랭킹 기간은 안쪽(순위) 스코프에.
    assert "OD.ORDER_DATE BETWEEN '20200101' AND '20201231'" in outer
    assert "D.ORDER_DATE BETWEEN '20190101' AND '20191231'" in inner
    assert "20200101" not in inner


def test_no_member_window_leaves_the_outer_scope_unfiltered(
    entity_set_config: dict[str, Any],
) -> None:
    predicate = entity_set.compile_entity_set_predicate(
        _legacy_node(None), entity_set_config, "B", "MEMBER_NO"
    )

    assert predicate is not None
    assert predicate.split("IN (", 1)[0].count("ORDER_DATE") == 0


def test_a_member_window_without_a_date_column_fails_closed(
    entity_set_config: dict[str, Any],
) -> None:
    """물리 매핑이 없으면 조용히 무시하지 않고 사유를 돌려준다."""
    config = copy.deepcopy(entity_set_config)
    config["relations"]["purchase"] = {
        **config["relations"]["purchase"], "dateColumn": None,
    }
    node = _legacy_node({"from": "20200101", "to": "20201231"})
    node["window"] = None
    node["derived_set_ast"]["source"]["source"].pop("window", None)

    assert entity_set.entity_set_capability(node, config) == (
        "unsupported_entity_set_member_period"
    )


def test_an_invalid_member_window_is_a_named_ast_error() -> None:
    ast = entity_set.build_derived_set_ast(
        member_relation="purchase", rank_relation="purchase", entity="product",
        measure="sales_quantity", direction="top", limit=5,
        member_window={"days": 0},
    )

    assert entity_set.derived_set_ast_error(ast) == "invalid_derived_set_member_window"


def test_the_relation_window_is_no_longer_dropped_when_a_ranking_is_present(
    entity_set_config: dict[str, Any],
) -> None:
    """검증이 받아들인 창을 컴파일러가 말없이 버리던 구멍(2026-08-07 이전 동작)."""
    node = {
        "relation": {
            "name": "purchase",
            "exists": True,
            "year": 2020,
            "entitySet": {
                "entity": "product", "measure": "sales_quantity",
                "direction": "top", "limit": 5, "year": 2019,
            },
        }
    }
    targeting_expression.validate_targeting_expression(node, entity_set_config, {}, {})

    sql = targeting_expression.compile_targeting_expression(
        node,
        entity_set_config,
        member_predicate=lambda name: None,
        member_alias="B",
        member_key="MEMBER_NO",
        age_column="AGE",
    )

    assert "OD.ORDER_DATE BETWEEN '20200101' AND '20201231'" in sql.split("IN (", 1)[0]
    assert "D.ORDER_DATE BETWEEN '20190101' AND '20191231'" in sql


# ── 7. 존재 조건 투영 ─────────────────────────────────────────────────────────────


def test_a_ranking_window_is_not_read_as_the_member_action_window() -> None:
    """랭킹 창을 회원 창으로 읽으면 라벨·커버리지가 요청과 달라진다."""
    views = event_ir.existence_views(
        event_ir.condition_from_dict({
            "type": "exists",
            "relation": _ranked_expression()["left"]["relation"],
            "evidence": {"text": RANKING_ONLY, "start": 0, "end": len(RANKING_ONLY)},
        })
    )

    assert [view.window for view in views] == [None]


def test_the_member_action_window_is_still_read() -> None:
    views = event_ir.existence_views(
        event_ir.condition_from_dict({
            "type": "exists",
            "relation": _ranked_expression(
                {"start": "2026-01-01", "end_exclusive": "2027-01-01"}
            )["left"]["relation"],
            "evidence": {"text": BOTH_SCOPES, "start": 0, "end": len(BOTH_SCOPES)},
        })
    )

    assert [view.window is not None for view in views] == [True]
