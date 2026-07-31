from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

import canonical_targeting
import conceptual_targeting
import graph_rag
from external_conditions.resolvers.kma_weather_alert import KmaWeatherAlertResolver


class FakeCompletion:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []
        self.tool_schemas: list[dict[str, Any]] = []

    def __call__(
        self,
        messages: list[dict[str, str]],
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        self.messages.append(copy.deepcopy(messages))
        self.tool_schemas.append(copy.deepcopy(tool_schema))
        if not self.responses:
            raise AssertionError("unexpected conceptual-targeting completion")
        return copy.deepcopy(self.responses.pop(0))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _catalog(
    tmp_path: Path,
    *,
    table: str = "CRM_MB_BASEINFO",
    column: str = "SIDO",
) -> conceptual_targeting.CapabilityCatalog:
    filters_path = tmp_path / "member_target_filters.json"
    values_path = tmp_path / "member_value_index.json"
    schema_path = tmp_path / "schema_catalog.json"
    _write_json(
        filters_path,
        {
            "version": "test-filters-v1",
            "base_entity": {
                "table": table,
                "alias": "B",
                "member_key": "MEMBER_NO",
            },
            "region_target": {
                "table": table,
                "target_basis": {
                    "entity": "member",
                    "attribute": "residence",
                },
                "default_capability": "sido",
                "columns": {
                    "sido": f"B.{column}",
                    "sigungu": "B.RESIDENCE_DISTRICT",
                },
            },
            "eq_filters": [],
            "boolean_filters": [],
            "numeric_filters": [],
        },
    )
    _write_json(
        values_path,
        {
            "version": "test-values-v1",
            "table": table,
            "columns": [
                {
                    "column": column,
                    "values": [
                        {"value": "서울", "name": "서울", "count": 100},
                        {"value": "대구", "name": "대구", "count": 80},
                        {"value": "경북", "name": "경북", "count": 70},
                    ],
                },
            ],
        },
    )
    _write_json(
        schema_path,
        {
            "version": "test-schema-v1",
            "tables": {
                table: {
                    "columns": [
                        {
                            "name": column,
                            "human_note": "회원이 거주하는 광역 지역",
                        },
                    ],
                },
            },
        },
    )
    return conceptual_targeting.discover_capabilities(
        member_filters_path=filters_path,
        member_value_index_path=values_path,
        schema_path=schema_path,
    )


def _categorical(catalog: conceptual_targeting.CapabilityCatalog):
    matches = [
        capability
        for capability in catalog.capabilities
        if capability.kind == "categorical"
    ]
    assert len(matches) == 1
    return matches[0]


def _response(
    capability: conceptual_targeting.Capability,
    *,
    evidence: str = "폭염지역",
    value_ids: list[str] | None = None,
    confidence: float = 0.91,
) -> dict[str, Any]:
    selected = (
        value_ids
        if value_ids is not None
        else [
            value.value_id
            for value in capability.values
            if value.stored_value in {"대구", "경북"}
        ]
    )
    selected_labels = [
        value.label
        for value in capability.values
        if value.value_id in selected
    ]
    return {
        "interpretations": [
            {
                "evidence": evidence,
                "capability_id": capability.capability_id,
                "operator": "IN",
                "value_ids": selected,
                "lower_bound": None,
                "upper_bound": None,
                "threshold": None,
                "confidence": confidence,
                "rationale": (
                    "일반적으로 무더운 지역으로 통용되는 후보 "
                    + ", ".join(selected_labels)
                    + "를 선택했다."
                ),
            },
        ],
        "unsupported": [],
        "ignored": [],
        "coverage_complete": True,
    }


def _numeric_response(
    capability: conceptual_targeting.Capability,
    *,
    evidence: str,
    operator: str,
    lower_bound: float | None = None,
    upper_bound: float | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    return {
        "interpretations": [{
            "evidence": evidence,
            "capability_id": capability.capability_id,
            "operator": operator,
            "value_ids": [],
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "threshold": threshold,
            "confidence": 0.82,
            "rationale": "캠페인용 주관적 수치 경계다.",
        }],
        "unsupported": [],
        "ignored": [],
        "coverage_complete": True,
    }


def _empty_response() -> dict[str, Any]:
    return {
        "interpretations": [],
        "unsupported": [],
        "ignored": [],
        "coverage_complete": True,
    }


def _plan(*, external: bool = False) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "dimension_filters": [],
    }
    if external:
        plan["external_conditions"] = [
            {
                "id": "external-heatwave-1",
                "domain": "weather",
                "condition_type": "alert",
                "condition_code": "heatwave",
                "source_text": "폭염지역",
                "target_basis": {
                    "entity": "member",
                    "attribute": "residence",
                },
                "resolution_status": "pending",
            },
        ]
    return plan


def _service(
    catalog: conceptual_targeting.CapabilityCatalog,
    completion: FakeCompletion,
) -> conceptual_targeting.ConceptualTargetingService:
    return conceptual_targeting.ConceptualTargetingService(
        catalog=catalog,
        complete=completion,
        model="fake-common-sense-model",
        system_prompt="Select only supplied opaque capability and value IDs.",
    )


def test_resolved_event_claims_skip_conceptual_review_and_compile(
    tmp_path: Path,
) -> None:
    query = "올해 상반기 구매 기록이 있는 고객중 하반기 구매 기록이 없는 고객"
    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["_conceptual_scope"] = {
        "targeting": plan["event_expression"]["source_text"],
        "channel": "",
    }
    completion = FakeCompletion()

    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 0
    assert plan["conceptual_targeting_resolution"]["status"] == "not_required"
    assert plan.get("unresolved_source_conditions") in (None, [])

    result = graph_rag.build_sql_result(
        graph=nx.Graph(),
        query=query,
        query_plan=plan,
        context_nodes=[],
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        original_query=query,
    )
    assert result["sql"] is not None
    assert "EXISTS" in result["sql"]
    assert "NOT EXISTS" in result["sql"]


def test_conceptual_review_sees_only_unowned_text_and_cannot_reject_owned_claims(
    tmp_path: Path,
) -> None:
    owned = "올해 상반기 구매 기록이 있는 고객 중 하반기 구매 기록이 없는 고객"
    query = f"{owned} 중 폭염지역 고객"
    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}
    completion = FakeCompletion({
        "interpretations": [],
        "unsupported": [{
            "evidence": owned,
            "reason": "No closed capability corresponds to purchase-period existence.",
        }],
        "ignored": [],
        "coverage_complete": True,
    })

    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    payload = json.loads(completion.messages[0][1]["content"])
    assert "폭염지역" in payload["request"]
    assert "상반기" not in payload["request"]
    assert "하반기" not in payload["request"]
    assert plan.get("unresolved_source_conditions") in (None, [])
    assert plan["conceptual_targeting_resolution"]["ignored_count"] == 1


def test_resolved_purchase_window_cannot_return_as_conceptual_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "3개월 이내에 우주복 구매한 고객"
    monkeypatch.setattr(
        graph_rag,
        "_apply_product_master_resolution",
        lambda _query, _plan: None,
    )
    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}

    membership = plan["target_user"]["purchase_membership"]
    assert membership["window_days"] == 90
    assert conceptual_targeting._plan_summary(plan)["purchase_membership"] == {
        "domain": "purchase",
        "operator": "exists",
        "window_days": 90,
    }
    membership_claim = next(
        claim
        for claim in plan["condition_claims"]
        if claim["semantic_key"].startswith("legacy:purchase_membership:")
    )
    assert membership_claim["source_spans"]
    source_span = membership_claim["source_spans"][0]
    assert query[source_span["start"]:source_span["end"]] == "3개월 이내에"

    completion = FakeCompletion({
        "interpretations": [],
        "unsupported": [{
            "evidence": "3개월 이내",
            "reason": "No capability supports this expression.",
        }],
        "ignored": [],
        "coverage_complete": True,
    })
    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 0
    assert plan.get("unresolved_source_conditions") in (None, [])
    assert plan["conceptual_targeting_resolution"]["status"] == "not_required"
    assert plan["conceptual_targeting_resolution"]["unsupported_count"] == 0
    assert plan["conceptual_targeting_resolution"]["ignored_count"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "2026년 2월과 3월의 구매금액차이가 10% 이상 증가한 고객 리스트",
        "2026년 3월 구매에서 가장 많이 팔린 상품 5개를 구매한 고객 리스트",
        "장바구니에 상품을 담고 결제하지 않은 고객에게 재구매를 유도하고 싶어요",
    ],
)
def test_compilable_claims_are_removed_before_conceptual_review(
    query: str,
    tmp_path: Path,
) -> None:
    """Any parser kind with a resolved canonical claim owns its source phrase."""

    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}
    completion = FakeCompletion()

    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 0
    assert plan["conceptual_targeting_resolution"]["status"] == "not_required"
    assert plan.get("unresolved_source_conditions") in (None, [])
    assert any(
        claim.get("source_spans")
        for claim in plan.get("condition_claims") or []
        if claim.get("disposition") == "owned"
    )


def test_recovered_calendar_window_keeps_provenance_across_all_consumers(
    tmp_path: Path,
) -> None:
    query = "2019년 상반기에 두부랑 음료수 산 사람들 찾아줘"
    # This is the deterministic merge boundary reached by the auto parser:
    # product facts already exist, while the shorthand verb "산" made the
    # direct purchase-date parser yield no value.
    plan = {
        "target_user": {
            "purchase_object": "두부",
            "purchase_objects": [
                {"value": "두부", "kind": None},
                {"value": "음료수", "kind": None},
            ],
        },
        "exclude": {},
        "planning_query": query,
        "normalized_query": query,
        "original_query": query,
    }
    graph_rag._apply_named_filter("calendar_window_claim", query, plan)
    canonical_targeting.attach_canonical_targeting(plan)
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}

    assert plan["target_user"]["purchase_date"] == {
        "from": "20190101",
        "to": "20190630",
        "label": "2019년 상반기 구매",
    }
    recorded = plan["_slot_spans"]["target_user.purchase_date"]
    assert query[recorded["start"]:recorded["end"]] == "2019년 상반기"

    date_claim = next(
        claim
        for claim in plan["condition_claims"]
        if claim["semantic_key"].startswith("legacy:purchase_date:")
    )
    assert date_claim["source_spans"] == [{"start": 0, "end": 9}]

    # Producer metadata is not the only line of defence.  A plan assembled by
    # another parser still receives the same exact, grammar-verified span at
    # the canonical boundary.
    detached = {
        "target_user": {"purchase_date": copy.deepcopy(plan["target_user"]["purchase_date"])},
        "original_query": query,
    }
    canonical_targeting.attach_canonical_targeting(detached)
    detached_claim = detached["condition_claims"][0]
    assert detached_claim["source_spans"] == [{"start": 0, "end": 9}]

    completion = FakeCompletion()
    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 0
    assert plan["conceptual_targeting_resolution"]["status"] == "not_required"
    assert plan.get("unresolved_source_conditions") in (None, [])


def test_member_ast_projection_preserves_provenance_without_changing_sql(
    tmp_path: Path,
) -> None:
    query = "7년전 기저귀를 구매한 여자 고객 찾아줘"
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "gender": "female",
            "purchase_object": "기저귀",
            "purchase_objects": [{"value": "기저귀", "kind": None}],
            "purchase_date": {
                "from": "20190101",
                "to": "20191231",
                "label": "2019년 구매",
            },
            "purchase_membership": {
                "domain": "purchase",
                "operator": "exists",
                "satisfied_by": "purchase_date",
            },
        },
        "exclude": {},
        "campaign_constraints": {},
        "original_query": query,
        "planning_query": query,
        "normalized_query": query,
        "source_requirements": [
            {
                "base": {"type": "target_user", "name": "purchase_date"},
                "source_text": "7년전 ",
                "source_span": {"start": 0, "end": 4},
            },
            {
                "base": {"type": "target_user", "name": "purchase_object"},
                "source_text": "기저귀",
                "source_span": {"start": 4, "end": 7},
            },
            {
                "base": {"type": "target_user", "name": "purchase_membership"},
                "source_text": "구매한",
                "source_span": {"start": 9, "end": 12},
            },
            {
                "base": {"type": "target_user", "name": "gender"},
                "source_text": "여자 고객",
                "source_span": {"start": 13, "end": 18},
            },
        ],
    }
    sql_before = graph_rag.build_purchase_history_targets_sql_candidate(
        copy.deepcopy(plan)
    )

    canonical_targeting.attach_canonical_targeting(plan)

    member_claim = next(
        claim
        for claim in plan["condition_claims"]
        if claim["predicate_kind"] == "MemberPredicate"
    )
    assert member_claim["source_spans"] == [{"start": 13, "end": 18}]
    sql_after = graph_rag.build_purchase_history_targets_sql_candidate(plan)
    assert sql_before is not None and sql_after is not None
    assert sql_after["sql"] == sql_before["sql"]

    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}
    completion = FakeCompletion()
    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 0
    assert plan["conceptual_targeting_resolution"]["status"] == "not_required"
    assert plan.get("unresolved_source_conditions") in (None, [])


def test_member_provenance_does_not_hide_unresolved_neighbor(
    tmp_path: Path,
) -> None:
    query = "여자 고객 중 인구 50만 이상 도시 거주자"
    plan = {
        "target_user": {"gender": "female"},
        "exclude": {},
        "original_query": query,
        "planning_query": query,
        "normalized_query": query,
        "source_requirements": [{
            "base": {"type": "target_user", "name": "gender"},
            "source_text": "여자 고객",
            "source_span": {"start": 0, "end": 5},
        }],
    }
    canonical_targeting.attach_canonical_targeting(plan)
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}
    completion = FakeCompletion({
        "interpretations": [],
        "unsupported": [{
            "evidence": "인구 50만 이상 도시",
            "reason": "No executable population capability.",
        }],
        "ignored": [],
        "coverage_complete": True,
    })

    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 1
    assert plan["unresolved_source_conditions"][0]["label"] == "인구 50만 이상 도시"


def test_top_level_registered_condition_preserves_exact_source_ownership(
    tmp_path: Path,
) -> None:
    query = "2026년 3월 같은 상품을 동시에 구매한 고객수"
    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}

    claim = next(
        item
        for item in plan["condition_claims"]
        if item["semantic_key"].startswith("legacy:condition_evaluation:")
    )
    assert claim["source_spans"] == [{"start": 9, "end": len(query)}]
    assert query[9:] == "같은 상품을 동시에 구매한 고객수"

    completion = FakeCompletion()
    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 0
    assert plan["conceptual_targeting_resolution"]["status"] == "not_required"
    assert plan.get("unresolved_source_conditions") in (None, [])


def test_top_level_condition_span_does_not_claim_an_unrelated_suffix(
    tmp_path: Path,
) -> None:
    owned_query = "2026년 3월 같은 상품을 동시에 구매한 고객수"
    query = owned_query + ", 인구 50만 이상 도시만"
    # Assemble the neighboring clause after parsing so this test isolates the
    # canonical provenance boundary rather than the region-condition parser.
    plan = graph_rag.build_query_plan(owned_query, parser="rules")
    for key in ("original_query", "planning_query", "normalized_query"):
        plan[key] = query
    canonical_targeting.attach_canonical_targeting(plan)
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}
    completion = FakeCompletion({
        "interpretations": [],
        "unsupported": [{
            "evidence": "인구 50만 이상 도시",
            "reason": "No executable population capability.",
        }],
        "ignored": [],
        "coverage_complete": True,
    })

    claim = next(
        item
        for item in plan["condition_claims"]
        if item["semantic_key"].startswith("legacy:condition_evaluation:")
    )
    span = claim["source_spans"][0]
    assert query[span["start"]:span["end"]] == "같은 상품을 동시에 구매한 고객수"

    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 1
    assert any(
        item.get("label") == "인구 50만 이상 도시"
        for item in plan["unresolved_source_conditions"]
    )


def test_resolved_claim_does_not_hide_a_separate_unsupported_conjunct(
    tmp_path: Path,
) -> None:
    query = (
        "2026년 2월과 3월의 구매금액이 증가한 고객 중 "
        "인구 50만 이상 도시 거주자"
    )
    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["_conceptual_scope"] = {"targeting": query, "channel": ""}
    completion = FakeCompletion({
        "interpretations": [],
        "unsupported": [{
            "evidence": "인구 50만 이상 도시",
            "reason": "No executable population capability.",
        }],
        "ignored": [],
        "coverage_complete": True,
    })

    _service(_catalog(tmp_path), completion).apply_plan(query, plan)

    assert completion.calls == 1
    assert plan["unresolved_source_conditions"][0]["label"] == "인구 50만 이상 도시"


def test_registry_discovery_and_compiler_follow_renamed_table_and_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = "RENAMED_MEMBER_BASE"
    column = "RESIDENCE_PROVINCE"
    catalog = _catalog(tmp_path, table=table, column=column)
    capability = _categorical(catalog)

    assert capability.table == table
    assert capability.column == column
    assert {value.stored_value for value in capability.values} == {"서울", "대구", "경북"}
    # The model-facing view intentionally contains no physical binding.
    model_view = json.dumps(capability.llm_view(), ensure_ascii=False)
    assert table not in model_view
    assert column not in model_view

    completion = FakeCompletion(_response(capability, evidence="더운 지역"))
    plan = _plan()
    _service(catalog, completion).apply_plan("더운 지역 고객", plan)

    generated = plan["dimension_filters"][0]
    assert generated["table"] == table
    assert generated["column"] == f"{table}.{column}"
    expected_codes = [
        value.stored_value
        for value in capability.values
        if value.stored_value in {"대구", "경북"}
    ]
    assert generated["codes"] == expected_codes

    monkeypatch.setitem(
        graph_rag._MEMBER_TARGET_FILTERS,
        "base_entity",
        {"table": table, "alias": "B", "member_key": "MEMBER_NO"},
    )
    monkeypatch.setitem(
        graph_rag._MEMBER_TARGET_FILTERS,
        "region_target",
        {
            "columns": {
                "sido": f"B.{column}",
                "sigungu": "B.RESIDENCE_DISTRICT",
            },
        },
    )
    compiled = graph_rag.compile_member_target_conditions(plan)

    assert compiled["has_signal"] is True
    assert (
        f"B.{column} IN (" + ", ".join(f"'{code}'" for code in expected_codes) + ")"
        in compiled["predicates"]
    )


def test_heatwave_fake_completion_materializes_sido_and_compiles_without_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def provider_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("OpenAI/KMA provider must not run in this unit test")

    monkeypatch.setattr(
        conceptual_targeting.OpenAIStructuredCompletion,
        "__call__",
        provider_must_not_run,
    )
    monkeypatch.setattr(KmaWeatherAlertResolver, "resolve", provider_must_not_run)

    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    completion = FakeCompletion(_response(capability))
    plan = _plan(external=True)
    plan["campaign_constraints"]["sell_object"] = "양산"

    _service(catalog, completion).apply_plan(
        "폭염지역에 양산을 팔 캠페인 대상 고객",
        plan,
    )

    assert completion.calls == 1
    generated = plan["dimension_filters"][0]
    assert generated["table"] == "CRM_MB_BASEINFO"
    assert generated["column"] == "CRM_MB_BASEINFO.SIDO"
    expected_codes = [
        value.stored_value
        for value in capability.values
        if value.stored_value in {"대구", "경북"}
    ]
    assert generated["codes"] == expected_codes
    assert generated["source"] == "llm_common_sense"
    assert generated["grounding"]["capability_id"] == capability.capability_id
    assert plan["external_condition_results"][0]["status"] == "resolved"
    assert plan["external_condition_results"][0]["resolver"] == "llm_common_sense"
    assert plan["external_condition_results"][0]["metadata"]["realtime"] is False
    assert graph_rag._common_sense_external_contract_errors(
        plan["external_conditions"][0],
        plan["external_condition_results"][0],
    ) == []
    reversed_result = copy.deepcopy(plan["external_condition_results"][0])
    reversed_result["generated_filter"]["operator"] = "NOT_IN"
    assert "positive external concept must materialize as IN" in (
        graph_rag._common_sense_external_contract_errors(
            plan["external_conditions"][0],
            reversed_result,
        )
    )

    compiled = graph_rag.compile_member_target_conditions(plan)
    assert (
        "B.SIDO IN (" + ", ".join(f"'{code}'" for code in expected_codes) + ")"
        in compiled["predicates"]
    )

    # The injected completion sees descriptions and opaque IDs, not SQL bindings.
    model_payload = completion.messages[0][1]["content"]
    assert "CRM_MB_BASEINFO" not in model_payload
    assert '"column":"SIDO"' not in model_payload
    assert '"request":"폭염지역에 [상품]을 팔 캠페인 대상 고객"' in model_payload
    assert completion.tool_schemas[0]["function"]["strict"] is True


@pytest.mark.parametrize(
    ("value_ids", "confidence", "expected_reason"),
    [
        (["value_not_in_catalog"], 0.95, "unknown_value_id"),
        (None, 0.20, "confidence_below_threshold"),
    ],
)
def test_unknown_value_or_low_confidence_fails_closed_and_stays_unresolved(
    tmp_path: Path,
    value_ids: list[str] | None,
    confidence: float,
    expected_reason: str,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    completion = FakeCompletion(
        _response(
            capability,
            value_ids=(
                value_ids
                if value_ids is not None
                else [capability.values[1].value_id]
            ),
            confidence=confidence,
        )
    )
    plan = _plan(external=True)

    _service(catalog, completion).apply_plan("폭염지역 고객", plan)

    assert plan["dimension_filters"] == []
    assert plan["conceptual_resolutions"] == []
    assert plan["conceptual_targeting_resolution"]["status"] == "unsupported"
    assert any(
        item["reason"] == expected_reason
        and any("가" <= char <= "힣" for char in item["display_reason"])
        for item in plan["unresolved_source_conditions"]
    )
    assert plan["external_condition_resolution"]["status"] == "failed"
    assert plan["external_condition_results"][0]["status"] == "failed"

    result = graph_rag.build_sql_result(
        graph=nx.Graph(),
        query="폭염지역 고객",
        query_plan=plan,
        context_nodes=[],
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
    )
    assert result["sql"] is None
    assert result["failure_reason"] == "external_condition_resolution_failed"


def test_explicit_same_column_filter_takes_precedence_over_inference(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    completion = FakeCompletion(_response(capability))
    plan = _plan()
    plan["dimension_filters"] = [
        {
            "dimension_id": "explicit:sido",
            "prompt_label": "거주 시도",
            "table": "CRM_MB_BASEINFO",
            "column": "CRM_MB_BASEINFO.SIDO",
            "operator": "IN",
            "codes": ["서울"],
            "names": ["서울"],
            "evidence": "서울 고객",
            "source": "deterministic",
        },
    ]

    _service(catalog, completion).apply_plan("서울 고객 중 폭염지역 고객", plan)

    assert len(plan["dimension_filters"]) == 1
    assert plan["dimension_filters"][0]["codes"] == ["서울"]
    assert plan["conceptual_resolutions"][0]["status"] == "skipped_explicit_precedence"
    assert plan["conceptual_resolutions"][0]["generated_filter"] is None
    compiled = graph_rag.compile_member_target_conditions(plan)
    assert "B.SIDO IN ('서울')" in compiled["predicates"]
    assert all("대구" not in predicate for predicate in compiled["predicates"])


def test_valid_resolution_is_cached_and_grounding_is_revalidated_at_compiler_boundary(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    completion = FakeCompletion(_response(capability))
    service = _service(catalog, completion)

    first_plan = _plan()
    second_plan = _plan()
    service.apply_plan("폭염지역 고객", first_plan)
    service.apply_plan("  폭염지역   고객  ", second_plan)

    assert completion.calls == 1
    assert first_plan["conceptual_targeting_resolution"]["cache_hit"] is False
    assert second_plan["conceptual_targeting_resolution"]["cache_hit"] is True
    valid_filter = first_plan["dimension_filters"][0]
    assert conceptual_targeting.validate_grounded_dimension_filter(
        valid_filter,
        catalog,
    ) == []
    assert graph_rag._validate_dimension_filters(first_plan) == []

    tampered_plan = copy.deepcopy(first_plan)
    tampered_plan["dimension_filters"][0]["codes"] = ["서울"]
    errors = graph_rag._validate_dimension_filters(tampered_plan)
    assert {
        error["code"] for error in errors
    } == {"CONCEPTUAL_GROUNDING_INVALID", "CONCEPTUAL_FILTER_DETACHED"}
    assert "candidate ID" in errors[0]["message"]

    malformed_plan = copy.deepcopy(first_plan)
    malformed_plan["conceptual_resolutions"][0]["selected_value_ids"] = [{}]
    malformed_errors = graph_rag._validate_dimension_filters(malformed_plan)
    assert any(
        error["code"] == "CONCEPTUAL_GROUNDING_INVALID"
        for error in malformed_errors
    )


def test_generic_common_sense_can_use_boolean_and_numeric_native_capabilities() -> None:
    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=graph_rag.DEFAULT_MEMBER_TARGET_FILTERS_PATH,
        member_value_index_path=graph_rag.DEFAULT_MEMBER_VALUE_INDEX_PATH,
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
    )
    children = next(
        capability
        for capability in catalog.capabilities
        if "children_registered" in capability.aliases
    )
    age = next(
        capability
        for capability in catalog.capabilities
        if capability.materializer == "age"
    )
    child_value = next(
        value for value in children.values if value.stored_value == "Y"
    )
    completion = FakeCompletion({
        "interpretations": [
            {
                "evidence": "아이가 있는 고객",
                "capability_id": children.capability_id,
                "operator": "IN",
                "value_ids": [child_value.value_id],
                "lower_bound": None,
                "upper_bound": None,
                "threshold": None,
                "confidence": 0.90,
                "rationale": f"{child_value.label} 후보로 해석했다.",
            },
            {
                "evidence": "젊은 고객",
                "capability_id": age.capability_id,
                "operator": "BETWEEN",
                "value_ids": [],
                "lower_bound": 20,
                "upper_bound": 39,
                "threshold": None,
                "confidence": 0.78,
                "rationale": "캠페인용 주관적 연령 대리 기준을 20~39세로 두었다.",
            },
        ],
        "unsupported": [],
        "ignored": [],
        "coverage_complete": True,
    })
    plan = _plan()

    _service(catalog, completion).apply_plan(
        "아이가 있는 고객 중 젊은 고객에게 캠페인",
        plan,
    )

    assert plan["target_user"]["age_min"] == 20
    assert plan["target_user"]["age_max"] == 39
    assert plan["dimension_filters"][0]["codes"] == ["Y"]
    compiled = graph_rag.compile_member_target_conditions(plan)
    assert "B.AGE >= 20" in compiled["predicates"]
    assert "B.AGE <= 39" in compiled["predicates"]
    assert "B.CHILDREN_YN IN ('Y')" in compiled["predicates"]


def test_external_condition_cannot_attach_an_unrelated_single_categorical_receipt(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    completion = FakeCompletion(
        _response(capability, evidence="VIP 고객")
    )
    plan = _plan(external=True)

    _service(catalog, completion).apply_plan(
        "폭염지역의 VIP 고객",
        plan,
    )

    assert plan["dimension_filters"]
    assert plan["external_condition_results"][0]["status"] == "failed"
    assert plan["external_condition_resolution"]["status"] == "failed"


def test_external_capability_uses_logical_role_not_policy_physical_column(
    tmp_path: Path,
) -> None:
    catalog = _catalog(
        tmp_path,
        table="RENAMED_MEMBER",
        column="RESIDENCE_PROVINCE",
    )
    capability = _categorical(catalog)
    completion = FakeCompletion(_response(capability))
    plan = _plan(external=True)
    plan["external_conditions"][0]["target_basis"] = {
        "entity": "member",
        "attribute": "residence",
    }
    plan["semantic_resolutions"] = [{
        "ambiguous_term": "지역",
        "default_capability_role": "member.residence.default",
        # A stale physical hint must not participate in capability selection.
        "default_column": "STALE_TABLE.STALE_COLUMN",
    }]

    _service(catalog, completion).apply_plan("폭염지역 고객", plan)

    model_payload = json.loads(completion.messages[0][1]["content"])
    constraint = model_payload["required_common_sense_concepts"][0]
    assert constraint["required_capability_id"] == capability.capability_id
    assert constraint["required_capability_role"] == "member.residence.default"
    assert plan["dimension_filters"][0]["table"] == "RENAMED_MEMBER"
    assert plan["dimension_filters"][0]["column"].endswith(
        ".RESIDENCE_PROVINCE"
    )
    assert plan["semantic_resolutions"] == []


@pytest.mark.parametrize("ambiguous", [False, True])
def test_external_logical_role_must_resolve_to_exactly_one_capability(
    tmp_path: Path,
    ambiguous: bool,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    if ambiguous:
        duplicate = replace(
            capability,
            capability_id="cap_ambiguous_duplicate",
            table="OTHER_MEMBER",
            column="OTHER_RESIDENCE",
        )
        catalog = conceptual_targeting.CapabilityCatalog(
            capabilities=(capability, duplicate),
            digest="ambiguous-catalog",
        )
    plan = _plan(external=True)
    if not ambiguous:
        plan["external_conditions"][0].pop("target_basis")

    _service(
        catalog,
        FakeCompletion(_response(capability)),
    ).apply_plan("폭염지역 고객", plan)

    assert plan["dimension_filters"] == []
    assert plan["external_condition_results"][0]["status"] == "failed"
    assert any(
        item["reason"] == "external_concept_capability_unresolved"
        for item in plan["unresolved_source_conditions"]
    )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("full_domain", "categorical_full_domain_forbidden"),
        ("not_in", "external_concept_operator_mismatch"),
    ],
)
def test_external_categorical_response_cannot_be_noop_or_reverse_polarity(
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    response = _response(
        capability,
        value_ids=[value.value_id for value in capability.values],
    )
    if mutation == "not_in":
        response = _response(capability)
        response["interpretations"][0]["operator"] = "NOT_IN"
    plan = _plan(external=True)

    _service(catalog, FakeCompletion(response)).apply_plan(
        "폭염지역 고객",
        plan,
    )

    assert plan["dimension_filters"] == []
    assert plan["external_condition_results"][0]["status"] == "failed"
    assert any(
        item["reason"] == expected_reason
        for item in plan["unresolved_source_conditions"]
    )


def test_explicit_realtime_external_request_is_not_weakened_to_common_sense(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    evidence = "폭염특보 발령 지역"
    plan = _plan(external=True)
    plan["external_conditions"][0].update({
        "source_text": evidence,
        "freshness_requirement": "live",
    })

    _service(
        catalog,
        FakeCompletion(_response(capability, evidence=evidence)),
    ).apply_plan(f"현재 {evidence} 고객", plan)

    assert plan["dimension_filters"] == []
    assert plan["external_condition_results"][0]["status"] == "failed"
    assert any(
        item["reason"] == "realtime_external_condition_requires_live_provider"
        for item in plan["unresolved_source_conditions"]
    )


def test_explicit_only_sensitive_capability_rejects_indirect_inference() -> None:
    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=graph_rag.DEFAULT_MEMBER_TARGET_FILTERS_PATH,
        member_value_index_path=graph_rag.DEFAULT_MEMBER_VALUE_INDEX_PATH,
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
    )
    gender = next(
        capability
        for capability in catalog.capabilities
        if "female" in capability.aliases
    )
    female = next(
        value
        for value in gender.values
        if value.stored_value.casefold().endswith("female")
    )
    completion = FakeCompletion(
        _response(
            gender,
            evidence="립스틱 애호가",
            value_ids=[female.value_id],
        )
    )
    plan = _plan()

    _service(catalog, completion).apply_plan("립스틱 애호가 고객", plan)

    assert gender.inference_mode == "explicit_only"
    assert plan["dimension_filters"] == []
    assert any(
        item["reason"]
        == "explicit_only_capability_requires_direct_evidence"
        for item in plan["unresolved_source_conditions"]
    )


def test_explicit_only_requires_the_selected_value_not_an_opposite_alias() -> None:
    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=graph_rag.DEFAULT_MEMBER_TARGET_FILTERS_PATH,
        member_value_index_path=graph_rag.DEFAULT_MEMBER_VALUE_INDEX_PATH,
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
    )
    gender = next(
        capability
        for capability in catalog.capabilities
        if "female" in capability.aliases
    )
    female = next(
        value
        for value in gender.values
        if value.stored_value.casefold().endswith("female")
    )
    plan = _plan()

    _service(
        catalog,
        FakeCompletion(
            _response(
                gender,
                evidence="남성 고객",
                value_ids=[female.value_id],
            )
        ),
    ).apply_plan("남성 고객", plan)

    assert plan["dimension_filters"] == []
    assert any(
        item["reason"]
        == "explicit_only_capability_requires_direct_evidence"
        for item in plan["unresolved_source_conditions"]
    )


def test_unverified_ignored_audience_concept_blocks_and_is_not_cached(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    response = _response(capability, evidence="더운 지역")
    response["ignored"] = [{
        "evidence": "부유한 고객",
        "reason": "추가 해석이 필요하지 않다.",
    }]
    completion = FakeCompletion(response, response)
    service = _service(catalog, completion)

    for _ in range(2):
        plan = _plan()
        service.apply_plan("더운 지역과 부유한 고객", plan)
        assert any(
            item["reason"] == "ignored_evidence_not_server_grounded"
            for item in plan["unresolved_source_conditions"]
        )

    assert completion.calls == 2


def test_campaign_boilerplate_can_be_safely_ignored(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    response = _response(capability, evidence="더운 지역")
    response["ignored"] = [{
        "evidence": "캠페인 만들어줘",
        "reason": "캠페인 실행 문구다.",
    }]
    plan = _plan()

    _service(catalog, FakeCompletion(response)).apply_plan(
        "더운 지역 고객 캠페인 만들어줘",
        plan,
    )

    assert plan["dimension_filters"]
    assert plan.get("unresolved_source_conditions") in (None, [])
    assert plan["conceptual_targeting_resolution"]["ignored_count"] == 1


def test_provider_english_unsupported_reason_is_not_exposed_to_the_user(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    response = {
        "interpretations": [],
        "unsupported": [{
            "evidence": "새로운 복합 조건",
            "reason": "No capability supports this expression.",
        }],
        "ignored": [],
        "coverage_complete": True,
    }
    plan = _plan()

    _service(catalog, FakeCompletion(response)).apply_plan(
        "새로운 복합 조건의 고객",
        plan,
    )

    unresolved = plan["unresolved_source_conditions"][0]
    assert unresolved["display_reason"] == (
        "'새로운 복합 조건' 조건을 현재 실행 가능한 타겟 조건으로 구조화하지 못했습니다."
    )
    assert unresolved["reason"] == "No capability supports this expression."

    sql_result = graph_rag._unresolved_source_blocking_sql_result([unresolved])
    assert "No capability" not in sql_result["clarification_questions"][0]
    assert any(
        "가" <= char <= "힣"
        for char in sql_result["clarification_questions"][0]
    )

    api_response = graph_rag.build_recommendation_api_response(
        "새로운 복합 조건의 고객",
        plan,
        sql_result,
        {},
    )
    public_condition = api_response["missing_input_conditions"][0]
    assert "No capability" not in public_condition["reason"]
    assert "display_reason" not in public_condition
    assert any("가" <= char <= "힣" for char in public_condition["reason"])


def test_sell_object_redaction_preserves_unknown_concept_prefix(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    completion = FakeCompletion(
        _response(capability, evidence="추운지역")
    )
    plan = _plan()
    plan["campaign_constraints"]["sell_object"] = "추운지역 패딩"

    _service(catalog, completion).apply_plan(
        "추운지역 패딩을 팔고 싶어",
        plan,
    )

    payload = json.loads(completion.messages[0][1]["content"])
    assert payload["request"] == "추운지역 [상품]을 팔고 싶어"
    assert plan["dimension_filters"]


@pytest.mark.parametrize(
    ("operator", "threshold", "expected"),
    [
        (">", 20.0, {"age_min": 21}),
        (">=", 20.2, {"age_min": 21}),
        ("<", 20.0, {"age_max": 19}),
        ("<=", 20.8, {"age_max": 20}),
    ],
)
def test_integer_thresholds_materialize_exactly(
    operator: str,
    threshold: float,
    expected: dict[str, int],
) -> None:
    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=graph_rag.DEFAULT_MEMBER_TARGET_FILTERS_PATH,
        member_value_index_path=graph_rag.DEFAULT_MEMBER_VALUE_INDEX_PATH,
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
    )
    age = next(
        capability
        for capability in catalog.capabilities
        if capability.materializer == "age"
    )
    plan = _plan()
    _service(
        catalog,
        FakeCompletion(
            _numeric_response(
                age,
                evidence="주관적 연령",
                operator=operator,
                threshold=threshold,
            )
        ),
    ).apply_plan("주관적 연령 고객", plan)

    assert {
        key: plan["target_user"][key]
        for key in expected
    } == expected
    assert graph_rag._validate_dimension_filters(plan) == []


@pytest.mark.parametrize(
    ("operator", "lower", "upper", "threshold", "reason"),
    [
        ("BETWEEN", 20.2, 20.3, None, "numeric_integer_range_empty"),
        ("=", None, None, 20.5, "numeric_integer_equality_empty"),
        (">", None, None, 120.0, "numeric_predicate_empty"),
        ("<", None, None, 0.0, "numeric_predicate_empty"),
        ("BETWEEN", 0.0, 120.0, None, "numeric_full_domain_forbidden"),
        (">=", None, None, 0.0, "numeric_full_domain_forbidden"),
        ("<=", None, None, 120.0, "numeric_full_domain_forbidden"),
    ],
)
def test_empty_integer_predicates_fail_closed(
    operator: str,
    lower: float | None,
    upper: float | None,
    threshold: float | None,
    reason: str,
) -> None:
    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=graph_rag.DEFAULT_MEMBER_TARGET_FILTERS_PATH,
        member_value_index_path=graph_rag.DEFAULT_MEMBER_VALUE_INDEX_PATH,
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
    )
    age = next(
        capability
        for capability in catalog.capabilities
        if capability.materializer == "age"
    )
    plan = _plan()

    _service(
        catalog,
        FakeCompletion(
            _numeric_response(
                age,
                evidence="주관적 연령",
                operator=operator,
                lower_bound=lower,
                upper_bound=upper,
                threshold=threshold,
            )
        ),
    ).apply_plan("주관적 연령 고객", plan)

    assert "age_min" not in plan["target_user"]
    assert "age_max" not in plan["target_user"]
    assert any(
        item["reason"] == reason
        for item in plan["unresolved_source_conditions"]
    )


def test_provider_failure_blocks_generic_concept_instead_of_dropping_it(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    plan = _plan()

    _service(catalog, FakeCompletion()).apply_plan(
        "서울 고객 중 젊은 고객",
        plan,
    )

    assert plan["conceptual_targeting_resolution"]["status"] == "failed"
    assert plan["unresolved_source_conditions"][0]["source_text"] == (
        "서울 고객 중 젊은 고객"
    )
    result = graph_rag.build_sql_result(
        graph=nx.Graph(),
        query="서울 고객 중 젊은 고객",
        query_plan=plan,
        context_nodes=[],
        schema_path=graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
    )
    assert result["sql"] is None
    assert result["failure_reason"] == "query_plan_required_conditions_missing"


def test_resolved_injected_external_provenance_is_not_rewritten(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    plan = _plan(external=True)
    plan["external_conditions"][0]["resolution_status"] = "resolved"
    plan["external_condition_results"] = [{
        "condition_id": "external-heatwave-1",
        "status": "resolved",
        "provider": "injected-official-provider",
        "resolver": "injected-resolver",
        "generated_filter": {"logic": "OR", "groups": [{"logic": "AND", "filters": []}]},
    }]
    plan["external_condition_resolution"] = {
        "status": "resolved",
        "basis": "injected-provider-snapshot",
    }
    before_results = copy.deepcopy(plan["external_condition_results"])
    before_summary = copy.deepcopy(plan["external_condition_resolution"])

    _service(
        catalog,
        FakeCompletion(_empty_response()),
    ).apply_plan("폭염지역 고객", plan)

    assert plan["external_condition_results"] == before_results
    assert plan["external_condition_resolution"] == before_summary


def test_channel_only_concept_is_rejected_by_audience_scope(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    capability = _categorical(catalog)
    plan = _plan()
    plan["_conceptual_scope"] = {
        "targeting": "서울 고객",
        "channel": "더운 지역 이미지로 RCS 발송",
    }

    _service(
        catalog,
        FakeCompletion(_response(capability, evidence="더운 지역")),
    ).apply_plan("서울 고객에게 더운 지역 이미지로 RCS 발송", plan)

    assert plan["dimension_filters"] == []
    assert any(
        item["reason"] == "evidence_outside_targeting_scope"
        for item in plan["unresolved_source_conditions"]
    )


def test_explicit_only_policy_dominates_generated_value_index(
    tmp_path: Path,
) -> None:
    _catalog(tmp_path)
    filters_path = tmp_path / "member_target_filters.json"
    config = json.loads(filters_path.read_text(encoding="utf-8"))
    config["eq_filters"] = [
        {
            "canonical": "direct_region",
            "category": "protected_test",
            "column": "B.SIDO",
            "value": "서울",
            "synonyms": ["서울"],
            "conceptual_inference": "explicit_only",
        }
    ]
    _write_json(filters_path, config)

    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=filters_path,
        member_value_index_path=tmp_path / "member_value_index.json",
        schema_path=tmp_path / "schema_catalog.json",
    )

    assert _categorical(catalog).inference_mode == "explicit_only"


def test_discovery_exposes_only_bindings_the_member_compiler_can_execute(
    tmp_path: Path,
) -> None:
    filters_path = tmp_path / "member_target_filters.json"
    values_path = tmp_path / "member_value_index.json"
    schema_path = tmp_path / "schema_catalog.json"
    _write_json(filters_path, {
        "version": "execution-contract-v1",
        "base_entity": {
            "table": "MEMBER_BASE",
            "alias": "B",
            "member_key": "MEMBER_NO",
        },
        "eq_filters": [],
        "boolean_filters": [],
        "numeric_filters": [
            {
                "canonical": "unsafe_score",
                "category": "score",
                "table": "AUX_PROFILE",
                "column": "P.UNSAFE_SCORE",
                "type": "integer",
                "min": 0,
                "max": 100,
            },
            {
                "canonical": "safe_score",
                "category": "score",
                "table": "AUX_PROFILE",
                "column": "P.SAFE_SCORE",
                "type": "integer",
                "min": 0,
                "max": 100,
                "profile_source": {
                    "table": "AUX_PROFILE",
                    "alias": "P",
                    "column": "SAFE_SCORE",
                    "member_column": "MEMBER_NO",
                    "base_member_column": "MEMBER_NO",
                },
            },
        ],
    })
    _write_json(values_path, {
        "version": "execution-values-v1",
        "table": "MEMBER_BASE",
        "columns": [
            {
                "source_table": "AUX_PROFILE",
                "column": "UNSAFE_KIND",
                "values": [{"value": "A"}, {"value": "B"}],
            },
            {
                "source_table": "AUX_PROFILE",
                "column": "SAFE_KIND",
                "join_column": "MEMBER_NO",
                "values": [{"value": "A"}, {"value": "B"}],
            },
        ],
    })
    _write_json(schema_path, {
        "version": "execution-schema-v1",
        "tables": {
            "MEMBER_BASE": {
                "columns": [{"name": "MEMBER_NO"}],
            },
            "AUX_PROFILE": {
                "columns": [
                    {"name": "MEMBER_NO"},
                    {"name": "UNSAFE_KIND"},
                    {"name": "SAFE_KIND"},
                    {"name": "UNSAFE_SCORE"},
                    {"name": "SAFE_SCORE"},
                ],
            },
        },
    })

    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=filters_path,
        member_value_index_path=values_path,
        schema_path=schema_path,
    )
    columns = {capability.column for capability in catalog.capabilities}

    assert "SAFE_KIND" in columns
    assert "SAFE_SCORE" in columns
    assert "UNSAFE_KIND" not in columns
    assert "UNSAFE_SCORE" not in columns


def test_conceptual_age_compiler_follows_renamed_registry_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filters_path = tmp_path / "member_target_filters.json"
    values_path = tmp_path / "member_value_index.json"
    schema_path = tmp_path / "schema_catalog.json"
    config = {
        "version": "renamed-age-filters-v1",
        "base_entity": {
            "table": "MEMBER_BASE",
            "alias": "B",
            "member_key": "MEMBER_NO",
        },
        "eq_filters": [],
        "boolean_filters": [],
        "numeric_filters": [{
            "canonical": "age",
            "category": "demographic",
            "column": "B.RESIDENT_YEARS",
            "type": "integer",
            "min": 0,
            "max": 120,
            "synonyms": ["나이", "연령"],
        }],
    }
    _write_json(filters_path, config)
    _write_json(values_path, {
        "version": "renamed-age-values-v1",
        "table": "MEMBER_BASE",
        "columns": [],
    })
    _write_json(schema_path, {
        "version": "renamed-age-schema-v1",
        "tables": {
            "MEMBER_BASE": {
                "columns": [
                    {"name": "MEMBER_NO"},
                    {"name": "RESIDENT_YEARS", "human_note": "회원 연령"},
                ],
            },
        },
    })
    catalog = conceptual_targeting.discover_capabilities(
        member_filters_path=filters_path,
        member_value_index_path=values_path,
        schema_path=schema_path,
    )
    age = next(
        capability
        for capability in catalog.capabilities
        if capability.materializer == "age"
    )
    plan = _plan()
    monkeypatch.setattr(graph_rag, "_MEMBER_TARGET_FILTERS", config)

    _service(
        catalog,
        FakeCompletion(
            _numeric_response(
                age,
                evidence="젊은 고객",
                operator="BETWEEN",
                lower_bound=20,
                upper_bound=39,
            )
        ),
    ).apply_plan("젊은 고객", plan)
    compiled = graph_rag.compile_member_target_conditions(plan)

    assert "B.RESIDENT_YEARS >= 20" in compiled["predicates"]
    assert "B.RESIDENT_YEARS <= 39" in compiled["predicates"]
    assert not any("B.AGE" in predicate for predicate in compiled["predicates"])


def test_external_gate_requires_each_exact_generated_filter() -> None:
    first = {
        "dimension_id": "external:e1",
        "table": "CRM_MB_BASEINFO",
        "column": "CRM_MB_BASEINFO.SIDO",
        "operator": "IN",
        "codes": ["대구"],
    }
    second = {
        "dimension_id": "external:e2",
        "table": "CRM_MB_BASEINFO",
        "column": "CRM_MB_BASEINFO.SIDO",
        "operator": "IN",
        "codes": ["경북"],
    }
    plan = {
        "external_conditions": [
            {"id": "e1", "resolution_status": "resolved"},
            {"id": "e2", "resolution_status": "resolved"},
        ],
        "external_condition_results": [
            {
                "condition_id": "e1",
                "status": "resolved",
                "generated_filter": copy.deepcopy(first),
            },
            {
                "condition_id": "e2",
                "status": "resolved",
                "generated_filter": copy.deepcopy(second),
            },
        ],
        "external_condition_resolution": {"status": "resolved"},
        "dimension_filters": [copy.deepcopy(first)],
        "compound_dimension_filters": [{
            "logic": "OR",
            "groups": [{
                "logic": "AND",
                "filters": [{"column": "B.SIDO", "operator": "IN", "values": ["서울"]}],
            }],
        }],
    }

    blocked = graph_rag._external_condition_blocking_sql_result(plan)

    assert blocked is not None
    assert any(
        item["condition_id"] == "e2"
        and item["reason"] == "external_condition_filter_missing"
        for item in blocked["failed_conditions"]
    )


@pytest.mark.parametrize(
    ("summary_status", "expected_stage_status"),
    [
        ("empty", "info"),
        ("failed", "fail"),
    ],
)
def test_trace_records_conceptual_invocation_even_without_a_resolution(
    summary_status: str,
    expected_stage_status: str,
) -> None:
    result = {
        "query": "일반 요청",
        "query_plan": {
            "target_user": {},
            "exclude": {},
            "retrieval": {},
            "parser": {"type": "rules"},
            "conceptual_targeting_resolution": {
                "status": summary_status,
                "model": "fake-common-sense-model",
                "basis": "general_knowledge_non_realtime",
            },
        },
        "sql_result": {
            "is_success": False,
            "candidates": [],
            "semantic_verification": {"ran": False},
        },
        "api_response": {},
        "prompt_normalization": {},
        "vector_matches": [],
        "keyword_matches": [],
        "graph_context": [],
        "timings_ms": {},
    }

    trace = graph_rag.build_retrieve_trace(result)
    stage = next(item for item in trace["stages"] if item["step"] == 5)
    used_names = {
        ref["name"]
        for ref in stage["refs"]
        if ref.get("used")
    }

    assert stage["status"] == expected_stage_status
    assert "conceptual_targeting_system.txt" in used_names
    assert "conceptual_targeting.py" in used_names
    assert any(
        ref.get("kind") == "모델" and ref.get("used")
        for ref in stage["refs"]
    )


def test_unmentioned_llm_recent_login_is_removed_before_plan_merge() -> None:
    hallucinated = {
        "target_user": {
            "recent_login": {"value": 30, "unit": "days"},
        },
    }

    graph_rag._reconcile_llm_candidate_source_bound_slots(
        "폭염지역에 양산을 팔 캠페인 대상",
        hallucinated,
    )

    assert hallucinated["target_user"].get("recent_login") is None

    explicit = {"target_user": {"recent_login": {"value": 30, "unit": "days"}}}
    graph_rag._reconcile_llm_candidate_source_bound_slots(
        "최근 7일 로그인한 고객",
        explicit,
    )
    assert explicit["target_user"].get("recent_login") is None

    rules = graph_rag._build_rule_query_plan("최근 7일 로그인한 고객")
    assert rules["target_user"]["recent_login"]["value"] == 7


def test_unmentioned_llm_age_and_interest_are_removed_before_plan_merge() -> None:
    hallucinated = {
        "target_user": {
            "age_min": 18,
            "age_max": 65,
            "interests": ["travel"],
        },
    }

    graph_rag._reconcile_llm_candidate_source_bound_slots(
        "폭염지역에 양산을 팔고 싶은 고객",
        hallucinated,
    )

    assert hallucinated["target_user"].get("age_min") is None
    assert hallucinated["target_user"].get("age_max") is None
    assert hallucinated["target_user"].get("interests") is None


def test_explicit_llm_age_and_interest_are_reparsed_from_source() -> None:
    candidate = {
        "target_user": {
            "age_min": 18,
            "age_max": 65,
            "interests": ["travel", "fashion"],
        },
    }

    graph_rag._reconcile_llm_candidate_source_bound_slots(
        "20대 여행 관심 고객",
        candidate,
    )

    assert candidate["target_user"].get("age_min") is None
    assert candidate["target_user"].get("age_max") is None
    assert candidate["target_user"].get("interests") is None

    rules = graph_rag._build_rule_query_plan("20대 여행 관심 고객")
    assert rules["target_user"]["age_min"] == 20
    assert rules["target_user"]["age_max"] == 29
    assert rules["target_user"]["interests"] == ["travel"]


def test_all_rule_owned_execution_slots_are_removed_from_broad_llm_candidate() -> None:
    query = "폭염지역에 양산을 팔고 싶은 고객"
    hallucinated = {
        "intent": "find_user_segment",
        "target_user": {
            "gender": "female",
            "lifecycle": ["vip"],
            "interests": ["travel"],
            "preferred_channels": ["sms"],
            "behaviors": ["student"],
            "price_sensitivity": "high",
        },
        "exclude": {
            "gender": ["male"],
            "interests": ["fashion"],
            "lifecycle": ["dormant"],
        },
        "campaign_constraints": {
            "category": ["fashion"],
            "objective": "purchase",
            "offer_type": "coupon",
            "channels": ["sms"],
        },
    }

    graph_rag._reconcile_llm_candidate_source_bound_slots(
        query,
        hallucinated,
    )

    assert "intent" not in hallucinated
    assert hallucinated["target_user"] == {}
    assert hallucinated["exclude"] == {}
    assert hallucinated["campaign_constraints"] == {}

    rules = graph_rag._build_rule_query_plan(query)
    candidates = [
        graph_rag.plan_resolver.PlanCandidate("rules", rules, priority=300),
        graph_rag.plan_resolver.PlanCandidate(
            "llm_query_structurer", hallucinated, priority=100
        ),
    ]
    merged = graph_rag.plan_resolver.resolve_plan_candidates(candidates)
    graph_rag._attach_candidate_source_requirements(merged, query, candidates)
    assert not any(
        item.get("source") == "llm_query_structurer"
        and item.get("path", "").startswith(
            ("target_user.", "exclude.", "campaign_constraints.")
        )
        for item in merged["source_requirements"]
    )


def test_validated_llm_owned_purchase_object_survives_ownership_gate() -> None:
    candidate = {
        "target_user": {
            "purchase_object": "기저귀",
            "purchase_object_kind": "product",
        },
    }

    graph_rag._reconcile_llm_candidate_source_bound_slots(
        "기저귀를 구매한 고객",
        candidate,
    )

    assert candidate["target_user"] == {
        "purchase_object": "기저귀",
        "purchase_object_kind": "product",
    }
