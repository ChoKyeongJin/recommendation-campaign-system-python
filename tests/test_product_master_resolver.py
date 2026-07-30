from __future__ import annotations

import networkx as nx

import graph_rag
import product_master_resolver as resolver


QUERY = "2019년 상반기에 하기스 기저귀를 가장 많이 산 고객 추출해줘"
GENERIC_RANKING_QUERY = "2026년3월 구매에서 가장 많이 팔린상품 5개를 구매한 고객 리스트"
KOREAN_AMOUNT_QUERY = "2019년에 이십만원 이상을 구매한고객에서 남자는 제외해."


def _product_row(*, brand: str = "유한킴벌리") -> dict[str, str]:
    return {
        "PRODUCT_ID": "P1",
        "PRODUCT_NAME": "하기스 매직컴포트",
        "BRAND_NAME": brand,
        "CATEGORY": "육아 > 기저귀",
        "CATEGORYL_NAME": "육아",
        "CATEGORYM_NAME": "기저귀",
        "CATEGORYS_NAME": "일회용기저귀",
    }


def test_untyped_phrase_resolves_product_and_category_on_same_product_row() -> None:
    result = resolver.resolve_product_phrase("하기스 기저귀", lookup=lambda _terms: [_product_row()])

    assert result["status"] == "resolved"
    assert result["confidence"] >= 0.88
    assert [(item["kind"], item["value"]) for item in result["filters"]] == [
        ("product", "하기스"),
        ("category", "기저귀"),
    ]
    assert result["matched_product_ids"] == ["P1"]
    assert result["source"] == "product_master_lookup"


def test_equal_product_and_brand_interpretations_require_clarification() -> None:
    brand_only_row = {
        **_product_row(brand="하기스"),
        "PRODUCT_ID": "P2",
        "PRODUCT_NAME": "다른 기저귀",
    }
    result = resolver.resolve_product_phrase(
        "하기스 기저귀",
        lookup=lambda _terms: [_product_row(), brand_only_row],
    )

    assert result["status"] == "ambiguous"
    assert result["reason"] == "candidate_margin_below_threshold"
    assert result["filters"] == []
    assert len(result["alternatives"]) >= 2


def test_equivalent_product_and_brand_candidates_choose_product_without_question() -> None:
    result = resolver.resolve_product_phrase(
        "하기스 기저귀",
        lookup=lambda _terms: [_product_row(brand="하기스 매직팬티")],
    )

    assert result["status"] == "resolved"
    assert result["equivalent_alternatives"] is True
    assert result["filters"][0]["kind"] == "product"


def test_product_lookup_cache_uses_normalized_terms_once(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def fake_read(_db, sql, params, **_kwargs):
        calls.append((sql, tuple(params)))
        return [_product_row()]

    import db_connections

    monkeypatch.setattr(db_connections, "run_read_query", fake_read)
    resolver.clear_product_lookup_cache()
    first = resolver.resolve_product_phrase("하기스 기저귀")
    second = resolver.resolve_product_phrase("하기스 기저귀")

    assert first["status"] == second["status"] == "resolved"
    assert len(calls) == 1
    assert "%하기스%" in calls[0][1]
    assert "%기저귀%" in calls[0][1]


def test_explicit_purchase_kind_skips_product_kind_inference(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: calls.append(phrase) or {},
    )
    plan = {
        "target_user": {"purchase_object": "하기스", "purchase_object_kind": "product"},
        "purchase_count_ranking": {"top_n": 1},
    }

    graph_rag._apply_product_master_resolution("상품명이 하기스인 상품을 가장 많이 산 고객", plan)

    assert calls == []
    assert "purchase_object_resolution" not in plan["target_user"]


def test_explicit_surface_kind_is_owned_by_user_without_db_inference(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: calls.append(phrase) or {},
    )
    plan = {
        "target_user": {"purchase_object": "알로루 브랜드"},
        "purchase_count_ranking": {"top_n": 1},
    }

    graph_rag._apply_product_master_resolution("알로루 브랜드를 가장 많이 산 고객", plan)

    assert calls == []
    assert plan["target_user"]["purchase_object"] == "알로루"
    assert plan["target_user"]["purchase_object_kind"] == "brand"


def test_non_entity_purchase_phrase_never_queries_product_master(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: calls.append(phrase) or {},
    )
    plan = {"target_user": {"purchase_object": "구매 팔린상품"}}

    graph_rag._apply_product_master_resolution(GENERIC_RANKING_QUERY, plan)

    assert calls == []
    assert "purchase_object" not in plan["target_user"]
    assert "purchase_object_resolution" not in plan["target_user"]


def test_generic_ranking_phrase_is_not_a_product_master_candidate() -> None:
    assert graph_rag._ambiguous_purchase_scope_phrase(GENERIC_RANKING_QUERY) is None
    assert graph_rag._is_concrete_purchase_scope_phrase("구매 팔린상품") is False
    assert graph_rag._is_concrete_purchase_scope_phrase("인기 상품") is False
    assert graph_rag._is_concrete_purchase_scope_phrase("모든 제품") is False
    assert graph_rag._is_concrete_purchase_scope_phrase("하기스 기저귀") is True


def test_ranking_action_is_removed_by_shared_purchase_object_validation() -> None:
    target_user = {
        "purchase_object": "팔린",
        "purchase_object_kind": "product",
    }

    graph_rag._validate_purchase_objects(GENERIC_RANKING_QUERY, target_user)

    assert target_user["purchase_object"] is None
    assert "purchase_object_kind" not in target_user
    assert graph_rag._target_purchase_objects({
        "purchase_object": "팔린",
        "purchase_object_kind": "product",
    }) == []


def test_generic_product_ranking_uses_derived_set_without_product_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda _phrase: (_ for _ in ()).throw(AssertionError("product lookup must not run")),
    )

    plan = graph_rag.build_query_plan(GENERIC_RANKING_QUERY, parser="rules")
    candidate = graph_rag.build_entity_set_targets_sql_candidate(plan)

    assert plan["target_user"].get("purchase_object_resolution") is None
    assert plan["target_user"]["entity_set_condition"]["limit"] == 5
    assert candidate is not None
    assert "SELECT TOP 5" in candidate["sql"]
    assert "20260301" in candidate["sql"] and "20260331" in candidate["sql"]


def test_ranking_action_misread_as_product_never_reaches_entity_set_sql() -> None:
    """LLM의 ``팔린=상품명`` 오인을 순위 AST가 복원하거나 SQL로 컴파일하면 안 된다."""
    query = "2019년 가장 많이 팔린 상품10개를 구매한고객만 추출해"
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "purchase_object": "팔린",
            "purchase_object_kind": "product",
        },
        "campaign_constraints": {"objective": "purchase"},
    }

    graph_rag._apply_entity_set_condition(query, plan)
    candidate = graph_rag.build_entity_set_targets_sql_candidate(plan)

    assert candidate is not None
    assert "SELECT TOP 10" in candidate["sql"]
    assert "20190101" in candidate["sql"] and "20191231" in candidate["sql"]
    assert "CRM_CM_PRODUCT" not in candidate["sql"]
    assert "PRODUCT_NAME" not in candidate["sql"]
    assert "팔린" not in candidate["sql"]


def test_stale_generic_scope_filter_is_not_restored_from_existing_ast() -> None:
    """앞선 패스가 만든 검증 전 AST 필터도 다음 패스에서 제거한다."""
    query = "2019년 가장 많이 팔린 상품10개를 구매한고객만 추출해"
    stale = graph_rag.parse_entity_set_condition(query, graph_rag._entity_set_config())
    assert stale is not None
    aggregation = stale["derived_set_ast"]["source"]["source"]
    aggregation["filters"] = [{
        "type": "dimension_filter",
        "dimension": "product",
        "operator": "contains",
        "value": "팔린",
    }]
    stale["filters"] = list(aggregation["filters"])
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "purchase_object": None,
            "entity_set_condition": stale,
        },
        "campaign_constraints": {"objective": "purchase"},
    }

    graph_rag._apply_entity_set_condition(query, plan)
    candidate = graph_rag.build_entity_set_targets_sql_candidate(plan)

    assert candidate is not None
    assert plan["target_user"]["entity_set_condition"].get("filters") in (None, [])
    assert "CRM_CM_PRODUCT" not in candidate["sql"]
    assert "PRODUCT_NAME" not in candidate["sql"]


def test_concrete_category_scope_still_filters_the_ranked_product_population() -> None:
    query = "2019년 카테고리가 어린이건강인 상품 중 많이 팔린 5개를 구매한 고객"
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "purchase_object": "어린이건강",
            "purchase_object_kind": "category",
        },
        "campaign_constraints": {},
    }

    graph_rag._apply_entity_set_condition(query, plan)
    candidate = graph_rag.build_entity_set_targets_sql_candidate(plan)

    assert candidate is not None
    assert "INNER JOIN CRM_CM_PRODUCT CP" in candidate["sql"]
    assert "CP.CATEGORY LIKE N'%어린이건강%'" in candidate["sql"]


def test_korean_written_amount_is_not_queried_as_a_product(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: calls.append(phrase) or {},
    )

    plan = graph_rag.build_query_plan(KOREAN_AMOUNT_QUERY, parser="rules")
    candidate = graph_rag.build_aggregate_targets_sql_candidate(plan)

    assert calls == []
    assert plan["target_user"].get("purchase_object") is None
    assert plan["target_user"].get("purchase_object_resolution") is None
    assert plan["target_user"]["aggregate_conditions"][0]["threshold"] == 200000.0
    assert plan["exclude"]["gender"] == ["male"]
    assert candidate is not None
    assert "20190101" in candidate["sql"] and "20191231" in candidate["sql"]


def test_no_guess_fallback_infers_nothing_from_a_groundless_phrase() -> None:
    """근거가 없을 때 폴백은 종류·값 분해·컬럼 선택을 **하나도** 추측하지 않는다.

    이 세 가지가 무추측 폴백의 전부다. 하나라도 추측하면 '실DB 근거 없이 지어낸 술어'가 되고,
    그때부터 부정 목록으로 다시 막아야 한다.
    """
    resolution = resolver.resolve_product_phrase("없는브랜드 없는상품", lookup=lambda _terms: [])
    fallback = resolver.no_guess_fallback(resolution)

    assert resolution["status"] == "not_found"
    assert fallback["status"] == "fallback"
    assert fallback["grounded"] is False
    assert fallback["filters"] == [], "종류별 술어를 만들지 않는다"
    assert fallback["fallback_value"] == fallback["input"], "값을 쪼개거나 다듬지 않는다"
    assert fallback["fallback_columns"] == list(resolver.SEARCH_COLUMNS), "컬럼을 골라 좁히지 않는다"
    assert fallback["no_guess"] == list(resolver.NO_GUESS_DIMENSIONS)


def test_no_guess_fallback_leaves_ambiguous_and_unavailable_alone() -> None:
    """근거가 있는데 고를 수 없는 것(ambiguous)·조회 실패(unavailable)는 광역 검색으로 덮지 않는다."""
    for status in ("ambiguous", "unavailable", "resolved"):
        untouched = resolver.no_guess_fallback({"status": status, "input": "하기스"})
        assert untouched["status"] == status
        assert "no_guess" not in untouched


def test_grounded_resolution_is_marked_grounded() -> None:
    """접지 여부는 resolver 가 밝힌다 — 소비처가 status 문자열을 각자 해석하지 않게."""
    grounded = resolver.resolve_product_phrase("하기스 기저귀", lookup=lambda _terms: [_product_row()])
    assert grounded["grounded"] is True

    unavailable = resolver.resolve_product_phrase("하기스", lookup=lambda _terms: (_ for _ in ()).throw(RuntimeError()))
    assert unavailable["grounded"] is False


def test_fallback_product_condition_is_reported_as_ungrounded(monkeypatch) -> None:
    """무추측 폴백은 실행되지만(clarification 아님) 신뢰도 리포트에 근거 없음으로 드러난다."""
    phrase = "하기쓰 기저귀"
    query = f"{phrase}를 구매한 고객"
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda value: {
            "input": value, "status": "not_found", "grounded": False,
            "source": "product_master_lookup", "confidence": 0.0, "filters": [], "alternatives": [],
        },
    )

    plan = graph_rag.build_query_plan(query, parser="rules")
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=query,
    )

    assert plan["target_user"]["purchase_object_resolution"]["status"] == "fallback"
    assert "purchase_object_kind" not in plan["target_user"], "폴백은 종류를 추측하지 않는다"
    assert result["is_success"] is True, "폴백은 실행 가능하다(오탈자·신규 상품이 요청을 막지 않는다)"
    confidence = result.get("confidence") or {}
    assert any("근거를 찾지 못해" in warning for warning in confidence.get("warnings", [])), confidence.get("warnings")
    product_condition = next(c for c in confidence["conditions"] if c["key"] == "purchase_object")
    assert not any(e["source_type"] == "product_master" for e in product_condition["evidence"]), \
        "근거 없는 폴백이 상품 마스터 확인 근거를 달면 안 된다"


def test_not_found_product_uses_whole_phrase_broad_fallback(monkeypatch) -> None:
    phrase = "하기쓰 기저귀"
    query = f"{phrase}를 구매한 고객"
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda value: {
            "input": value,
            "status": "not_found",
            "source": "product_master_lookup",
            "confidence": 0.0,
            "filters": [],
            "alternatives": [],
        },
    )

    plan = graph_rag.build_query_plan(query, parser="rules")
    candidate = graph_rag.build_purchase_history_targets_sql_candidate(plan)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=query,
    )

    resolution = plan["target_user"]["purchase_object_resolution"]
    assert resolution["status"] == "fallback"
    assert resolution["lookup_status"] == "not_found"
    assert resolution["fallback_value"] == phrase
    assert candidate is not None
    assert all(
        f"P.{column} LIKE N'%{phrase}%'" in candidate["sql"]
        for column in ("PRODUCT_NAME", "BRAND_NAME", "CATEGORY", "CATEGORYL_NAME", "CATEGORYM_NAME", "CATEGORYS_NAME")
    )
    assert result["is_success"] is True


def test_resolved_product_still_uses_split_same_row_facets(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: {
            "input": phrase,
            "status": "resolved",
            "source": "product_master_lookup",
            "confidence": 0.97,
            "filters": [
                {"kind": "product", "value": "하기스", "columns": ["PRODUCT_NAME"]},
                {"kind": "category", "value": "기저귀", "columns": ["CATEGORYM_NAME"]},
            ],
            "alternatives": [],
        },
    )

    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    candidate = graph_rag.build_purchase_count_ranking_sql_candidate(plan)

    assert plan["target_user"]["purchase_object_resolution"]["status"] == "resolved"
    assert candidate is not None
    assert "P.PRODUCT_NAME LIKE N'%하기스%'" in candidate["sql"]
    assert "P.CATEGORYM_NAME LIKE N'%기저귀%'" in candidate["sql"]
    assert ") AND (" in candidate["sql"]


def test_resolved_facets_compile_as_same_row_and_predicates(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: {
            "input": phrase,
            "status": "resolved",
            "source": "product_master_lookup",
            "confidence": 0.96,
            "filters": [
                {"kind": "product", "value": "하기스", "columns": ["PRODUCT_NAME"]},
                {"kind": "category", "value": "기저귀", "columns": ["CATEGORYM_NAME"]},
            ],
            "alternatives": [],
        },
    )

    plan = graph_rag.build_query_plan(QUERY, parser="rules")
    candidate = graph_rag.build_purchase_count_ranking_sql_candidate(plan)

    assert plan["target_user"]["purchase_object"] == "하기스 기저귀"
    assert plan["target_user"]["purchase_object_resolution"]["source"] == "product_master_lookup"
    assert plan["purchase_count_ranking"]["top_n"] == 1
    assert candidate is not None
    sql = candidate["sql"]
    assert sql.startswith("SELECT TOP 1 ")
    assert "P.PRODUCT_NAME LIKE N'%하기스%'" in sql
    assert "P.CATEGORYM_NAME LIKE N'%기저귀%'" in sql
    assert ") AND (" in sql
    assert sql.count("INNER JOIN CRM_CM_PRODUCT P") == 1


def test_source_recovery_restores_period_dropped_by_scope_split() -> None:
    plan = {
        "target_user": {},
        "purchase_count_ranking": {"top_n": 1, "metric": "order_count"},
    }

    graph_rag._restore_purchase_date_from_source(QUERY, plan)
    assert "purchase_date" not in plan["target_user"]

    graph_rag._apply_calendar_window_claim_filter(QUERY, plan)

    assert plan["target_user"]["purchase_date"] == {
        "from": "20190101",
        "to": "20190630",
        "label": "2019년 상반기 구매",
    }


def test_ambiguous_resolution_blocks_before_sql_generation(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: {
            "input": phrase,
            "status": "ambiguous",
            "source": "product_master_lookup",
            "confidence": 0.93,
            "filters": [],
            "reason": "candidate_margin_below_threshold",
            "alternatives": [
                {"confidence": 0.93, "filters": [{"kind": "product", "value": "하기스"}]},
                {"confidence": 0.92, "filters": [{"kind": "brand", "value": "하기스"}]},
            ],
        },
    )
    plan = graph_rag.build_query_plan(QUERY, parser="rules")

    result = graph_rag.build_sql_result(
        nx.Graph(), QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=QUERY,
    )

    assert result["is_success"] is False
    assert result["failure_reason"] == "query_plan_required_conditions_missing"
    assert result["sql"] is None
    assert result["missing_input_conditions"][0]["path"] == "source_coverage.product_master_resolution"
