"""graph_rag façade 계약: 외부 소비 심볼은 분리(rag/ 패키지 이행) 중에도 반드시 보존된다.

graph_rag.py 를 역할별 모듈(rag/)로 나누는 동안 graph_rag 는 재수출 façade 로 남는다.
아래 목록은 외부 소비처(tests/, tools/, api.py,
build_member_value_index.py)가 실제로 참조하는 심볼의 전수 스냅샷(2026-07-31)이다 —
속성 접근(graph_rag.X), from-import, monkeypatch 문자열 참조까지 포함한다.

함수를 rag/ 하위 모듈로 옮길 때 graph_rag 의 명시 재수출을 빠뜨리면 이 테스트가
즉시 잡는다. 목록에서 심볼을 지우는 것은 '모든 외부 소비처가 새 경로로 이관됐다'는
확인 뒤에만 허용된다(단계 7).
"""

from __future__ import annotations

import graph_rag

# 외부 소비처가 참조하는 graph_rag 심볼 전수(공개 + 프라이빗 + 네임스페이스 경유 서브모듈).
FACADE_SYMBOLS: tuple[str, ...] = (
    "CONDITION_EVALUATION_CANDIDATE_ID",
    "CampaignQueryPlanV4",
    "DEFAULT_COLLECTION",
    "DEFAULT_DATA_PATH",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_MEMBER_TARGET_FILTERS_PATH",
    "DEFAULT_MEMBER_VALUE_INDEX_PATH",
    "DEFAULT_MESSAGE_POLICY_PATH",
    "DEFAULT_NORMALIZATION_PATH",
    "DEFAULT_POLICY_PATH",
    "DEFAULT_PROMPT_DIR",
    "DEFAULT_SCHEMA_PATH",
    "MEMBER_EQ_FILTERS",
    "_DEFAULT_TARGETING_LEXICON",
    "_MEMBER_TARGET_FILTERS",
    "_aggregation_schema_prompt_context",
    "_api_status",
    "_attach_candidate_source_requirements",
    "_audience_polarity_signals",
    "_build_llm_sql_fallback_candidate",
    "_build_llm_targeting_ir_candidate",
    "_coerce_llm_query_plan_candidate",
    "_common_sense_external_contract_errors",
    "_compile_compound_dimension_filter",
    "_compile_set_expression_ast",
    "_compile_targeting_ir_candidate",
    "_deterministic_dropped_conditions",
    "_entity_set_config",
    "_external_condition_blocking_sql_result",
    "_gender_polarity_signals",
    "_grade_threshold_registry",
    "_has_purchase_history_signal",
    "_is_concrete_purchase_scope_phrase",
    "_lexicon_signal",
    "_llm_extract_semantic_signal",
    "_llm_split_prompt_scopes",
    "_member_key_column",
    "_member_login_id_column",
    "_member_table",
    "_message_repair_context",
    "_normalize_semantic_verification_verdict",
    "_parse_amount_comparison",
    "_parse_amount_comparison_candidates",
    "_plan_semantic_expr",
    "_prompt_signal_signature",
    "_purchase_brand_names",
    "_purchase_conservative_signal",
    "_purchase_date_predicate",
    "_purchase_semantics",
    "_query_plan_authority",
    "_query_plan_user_prompt",
    "_read_prompt_template",
    "_refresh_unresolved_source_conditions",
    "_resolve_surface_signals",
    "_rewrite_dropped_signals",
    "_semantic_ir_blocking_sql_result",
    "_semantic_signal_scope",
    "_semantic_verification_contract_context",
    "_semantic_verification_is_failure",
    "_sql_target_builders",
    "_surface_signal_scope",
    "_target_purchase_objects",
    "_targeting_aggregate_capabilities",
    "_targeting_fallback_schema_tables",
    "_validate_dimension_filters",
    "_validate_purchase_objects",
    "_validate_sql_delivery_contract",
    "_verify_compiled_sql_semantics",
    "_verify_plan_semantic_conflicts",
    "_verify_sql_semantic_invariants",
    "apply_execution_to_trace",
    "build_aggregate_targets_sql_candidate",
    "build_condition_evaluation_sql_candidate",
    "build_entity_set_targets_sql_candidate",
    "build_event_expression_sql_candidate",
    "build_graph",
    "build_member_targets_sql_candidate",
    "build_message_context",
    "build_message_response",
    "build_metric_trend_targets_sql_candidate",
    "build_partial_retrieve_trace",
    "build_purchase_count_ranking_sql_candidate",
    "build_purchase_history_targets_sql_candidate",
    "build_query_plan",
    "build_retrieve_trace",
    "build_sql_result",
    "build_verified_condition_tokens",
    "compile_executable_plan",
    "compile_member_target_conditions",
    "condition_evaluation_locked",
    "graph_stats",
    "load_payload",
    "normalize_prompt",
    "plan_decisions",
    "plan_resolver",
    "rag_llm_run_scope",
    "render_answer_prompt",
    "render_message_variant_prompt",
    "retrieve",
    "slot_ownership",
    "validate_required_input_conditions",
)

# 테스트가 graph_rag 모듈 속성으로 monkeypatch 하는 심볼. 이 심볼(또는 그 호출부)을
# rag/ 하위로 옮길 때는 호출부를 모듈 속성 경유로 유지하고 patch 경로를 함께 갱신해야
# 패치가 계속 효력을 가진다.
MONKEYPATCHED_SYMBOLS: tuple[str, ...] = (
    "_MEMBER_TARGET_FILTERS",
    "_build_llm_sql_fallback_candidate",
    "_build_llm_targeting_ir_candidate",
    "_llm_extract_semantic_signal",
    "_prompt_signal_signature",
    "_purchase_brand_names",
    "_resolve_surface_signals",
)


def test_facade_preserves_externally_consumed_symbols() -> None:
    missing = [name for name in FACADE_SYMBOLS if not hasattr(graph_rag, name)]
    assert not missing, (
        "graph_rag façade 에서 외부 소비 심볼이 사라졌다 — rag/ 하위로 옮겼다면 "
        f"graph_rag 의 명시 재수출을 추가하라: {missing}"
    )


def test_monkeypatched_symbols_are_part_of_the_facade_contract() -> None:
    stray = set(MONKEYPATCHED_SYMBOLS) - set(FACADE_SYMBOLS)
    assert not stray, f"monkeypatch 대상이 façade 목록에 없다: {stray}"
