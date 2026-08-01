"""추론 과정 트레이스 — 10단계 파이프라인을 사용자에게 보이는 형태로 조립한다.

graph_rag.py 에서 분리했다. 여기 있는 것은 전부 **표현**이다: 이미 끝난 실행의 산출물
(query_plan · sql_result · timings)을 읽어 "무엇을 어떤 순서로 했고, 각 단계가 어떤 자산을
실제로 참조했고, 어디서 왜 막혔는지"를 API 응답용 구조로 만든다. 판단은 하지 않는다 —
그래서 상위 계층에 두고, 아래 계층(실패단계 분류·속성토큰 레지스트리·plan 검사)에 얹는다.

세 축:
  - 단계 조립: _TRACE_STAGES_META 가 10단계의 이름·기법·방법을 선언하고,
    _TRACE_TIMING_TO_STEPS 가 실측 타이밍을 그 단계에 귀속시킨다.
  - 참조 배지: _TRACE_STAGE_REFS 는 각 단계가 **런타임에 실제로 로딩하는 자산만** 싣는다.
    _mark_trace_refs_used 가 이번 요청에 실제로 기여한 것에만 used 를 붙인다 —
    목록 전체를 항상 강조하면 "무엇이 이 답을 만들었는가"를 구분할 수 없다.
  - 실패 진단: SQL 실패·예외·실행 실패를 각각 사용자가 다음 행동을 정할 수 있는 문장으로.

retrieve() 가 도중에 터져도 '오류 직전까지'의 부분 트레이스를 낼 수 있어야 한다
(:func:`build_partial_retrieve_trace`) — 실패했을 때야말로 어디까지 갔는지가 필요하다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 하위 rag.* 리프만 쓴다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import lexicon_llm
import plan_decisions

from rag.attribute_tokens import _attribute_token_groups
from rag.config import DEFAULT_EMBEDDING_MODEL, DEFAULT_LLM_MODEL
from rag.failure_stage import (
    _UNSUPPORTED_INTENT_REASONS,
    _classify_failure_stage,
    _recognized_domains,
)
from rag.plan_inspect import _generated_filter_is_attached
from rag.search import SearchHit


# 위 경량 단계에 해당하는 트레이스 step 번호(배지 모델명을 메인이 아니라 fast 모델로 표기).
_TRACE_FAST_MODEL_STEPS = {1, 2, 4, 9}


# 9단계(SQL 안전 검증 → 의미검증)는 전용 모델(OPENAI_SEMANTIC_VERIFY_MODEL) override 대상.
_TRACE_SEMANTIC_VERIFY_STEP = 9


def build_stage_log(
    query_plan: dict[str, Any],
    vector_hits: list[SearchHit],
    keyword_hits: list[SearchHit],
    merged_hits: list[SearchHit],
    context_nodes: list[dict[str, Any]],
    context_assembly: dict[str, Any],
    sql_result: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "stage": "1. Query Planning",
            "summary": f"intent={query_plan['intent']}, normalized_query={query_plan['retrieval']['query']}",
            "metrics": {
                "matched_terms": len(query_plan["matched_terms"]),
                "retrieval_terms": len(query_plan["retrieval"]["terms"]),
            },
        },
        {
            "stage": "2. Hybrid Retrieval",
            "summary": "Dense vector hits and BM25 keyword hits were collected as seed candidates.",
            "metrics": {"vector_hits": len(vector_hits), "keyword_hits": len(keyword_hits)},
        },
        {
            "stage": "3. Merge / Score Sort",
            "summary": "Duplicate node ids were merged by highest score and sorted by relevance.",
            "metrics": {"merged_hits": len(merged_hits)},
        },
        {
            "stage": "4. Graph Expansion",
            "summary": "Seed nodes were expanded through graph relationships to build retrieval context.",
            "metrics": {"context_nodes": len(context_nodes)},
        },
        {
            "stage": "5. Context Assembly",
            "summary": "Top-K chunks, graph context, metadata, and prompt context were assembled.",
            "metrics": context_assembly["metadata"],
        },
        {
            "stage": "6. SQL Template / Guard",
            "summary": "Verified condition tokens were assembled into an intent SQL template and validated by sql_guard.",
            "metrics": {
                "candidate_count": sql_result["candidate_count"],
                "condition_tokens": len(sql_result.get("condition_tokens", [])),
                "selected_sql": bool(sql_result["sql"]),
                "selected_valid": bool(sql_result["selected"] and sql_result["selected"].get("is_eligible")),
                "required_conditions": len(sql_result["required_conditions"]),
                "expected_grain": (sql_result.get("delivery_validation") or {}).get("expected_grain"),
                "actual_grain": (sql_result.get("delivery_validation") or {}).get("actual_grain"),
                "extracted_conditions": (sql_result.get("delivery_validation") or {}).get("extracted_conditions", []),
                "missing_conditions": (sql_result.get("delivery_validation") or {}).get("missing_conditions", []),
                "semantic_issues": (sql_result.get("delivery_validation") or {}).get("semantic_issues", []),
                "sql_evidence": (sql_result.get("delivery_validation") or {}).get("sql_evidence", {}),
                "failure_reason": sql_result["failure_reason"] or "none",
            },
        },
    ]


# 트레이스 화면 10단계 메타. (step, 한글 단계명, 기술명, method['혼합'=LLM 사용 / '규칙'=결정론]).
# retrieve() 내부에서 실제로 따로 실행·계측되는 단계들을 사용자용 10단계로 노출한다.
_TRACE_STAGES_META: tuple[tuple[int, str, str, str], ...] = (
    (1, "프롬프트 재작성", "정규화·재작성 (normalize_prompt)", "혼합"),
    (2, "타겟/채널 분리", "스코프 분리 (split_prompt_scopes)", "혼합"),
    (3, "질의 계획 수립", "Query Plan (build_query_plan)", "혼합"),
    (4, "상품·구매이력 추출", "타겟 오브젝트 추출 (정규식+LLM)", "혼합"),
    (5, "값·상식 개념 해석", "디멘션 값 + 상식 grounding", "혼합"),
    (6, "집합식 파싱", "집합식 (parse_set_expressions)", "규칙"),
    (7, "지식그래프 검색", "벡터+키워드+Graph 확장 (GraphRAG)", "혼합"),
    (8, "타겟팅 SQL 생성", "SelectAst 조립·렌더 (sql_ast.py) + 조건빌더/LLM", "혼합"),
    (9, "SQL 안전 검증", "sql_guard + 의미 검증", "혼합"),
    (10, "실행·결과", "DB 실행", "규칙"),
)


# timings_ms 키 → 그 키가 있으면 '완료된' 단계 번호들. 부분 트레이스에서 어디까지 갔는지 판정한다.
# (2·4·5·6 은 build_query_plan 안에서 함께 수행되므로 query_plan 계측 하나로 묶어 완료 판정한다.)
_TRACE_TIMING_TO_STEPS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("prompt_normalization", (1,)),
    ("prompt_scopes", (2,)),
    # 2·4·5·6 중 3~6 은 build_query_plan 안에서 함께 수행되므로 query_plan 계측 하나로 완료 판정한다.
    ("query_plan", (3, 4, 5, 6)),
    ("conceptual_targeting", (5,)),
    ("vector_search", (7,)),
    ("keyword_search", (7,)),
    ("context_assembly", (7,)),
    ("sql_generation", (8, 9)),
)


# target_user 중 '상품·구매이력' 단계(4)로 보낼 키. 나머지는 '질의 계획'(3)의 프로필 조건으로 본다.
_PURCHASE_PLAN_KEYS = {
    "aggregate_conditions", "cart_aggregate", "purchase_objects", "sales_objects",
    "target_objects", "purchase_history", "purchase_object", "sales_object",
}


def _trace_line(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _format_captured_prompt(captured: Any, cap: int = 2500) -> list[str]:
    """캡처한 실제 LLM 프롬프트(system/user/response)를 트레이스 details 줄로 만든다.
    system 은 정적·장문이라 길면 자르고(전체는 logs/rag_llm), 값이 치환된 user·응답이 핵심이다."""
    if not isinstance(captured, dict):
        return []

    def _cap(text: str, limit: int) -> str:
        text = text or ""
        return text if len(text) <= limit else text[:limit] + f"\n…(생략 {len(text) - limit}자, 전체는 logs/rag_llm 로그)"

    lines: list[str] = []
    if captured.get("system"):
        lines.append("[system 프롬프트]\n" + _cap(captured["system"], 1500))
    if captured.get("user"):
        lines.append("[user 프롬프트 — 값 치환 완료]\n" + _cap(captured["user"], cap))
    if captured.get("response"):
        lines.append("[LLM 응답]\n" + _cap(captured["response"], 1500))
    return lines


# 단계별 참조 자산(프롬프트/데이터/모델). 화면 "참조" 배지로 노출한다 — 어느 프롬프트·docs/data 를
# 근거로 그 단계가 동작하는지 사용자가 바로 알 수 있게. (정적 매핑 — 실제 로딩 경로는 코드 주석 참고.)
_TRACE_STAGE_REFS: dict[int, tuple[dict[str, str], ...]] = {
    1: (
        # 기본 unresolved_only는 원문을 보존한다. 아래 프롬프트는 명시적 targeting/conservative 모드에서만 쓴다.
        {"kind": "프롬프트", "name": "prompt_rewrite_system.txt (targeting opt-in)"},
        {"kind": "프롬프트", "name": "prompt_normalize_system.txt (보수 모드)"},
        {"kind": "모델", "name": "{model}"},
    ),
    2: (
        {"kind": "프롬프트", "name": "prompt_scope_split_system.txt"},
        # 대상 방향 표지는 어휘 사전이, 채널 신호는 표면 개념(LLM 해석)이 소유한다(split_prompt_scopes).
        {"kind": "데이터", "name": "targeting_lexicon.json"},
        {"kind": "데이터", "name": "surface_concepts.json"},
        {"kind": "프롬프트", "name": "surface_signal_extract_system.txt"},
        {"kind": "모델", "name": "{model}"},
    ),
    3: (
        {"kind": "프롬프트", "name": "query_plan_system.txt"},
        {"kind": "프롬프트", "name": "query_plan_user.txt"},
        # 규칙 계획(_build_rule_query_plan)이 항상 로딩: 정규화 사전·업무정책·어휘/속성 사전·지표 사전·스키마.
        {"kind": "데이터", "name": "normalization_rules.sample.json"},
        {"kind": "데이터", "name": "business_policies.sample.json"},
        {"kind": "데이터", "name": "targeting_lexicon.json"},
        {"kind": "데이터", "name": "surface_concepts.json"},
        {"kind": "데이터", "name": "attribute_token_groups.json"},
        {"kind": "데이터", "name": "member_metrics.json"},
        {"kind": "데이터", "name": "schema_catalog.json"},
        {"kind": "모델", "name": "{model} (tool calling)"},
    ),
    4: (
        {"kind": "프롬프트", "name": "target_object_extract_system.txt"},
        # 구매이력·판매 동사 신호로 LLM 추출을 게이팅한다(동결 백스톱이 놓치면 표면 개념 해석이 메움).
        {"kind": "데이터", "name": "surface_concepts.json"},
        {"kind": "모델", "name": "{model} (폴백)"},
    ),
    5: (
        {"kind": "프롬프트", "name": "conceptual_targeting_system.txt"},
        {"kind": "코드", "name": "conceptual_targeting.py"},
        {"kind": "데이터", "name": "dimension_catalog.sample.json"},
        {"kind": "데이터", "name": "member_value_index.json"},
        {"kind": "데이터", "name": "member_target_filters.json"},
        {"kind": "데이터", "name": "schema_catalog.json"},
        {"kind": "모델", "name": "{conceptual_model} (상식 grounding)"},
        # 디멘션 DS_SQL 을 실DB 에서 실행해 이름→코드로 값을 해석한다(런타임 조회).
        {"kind": "인프라", "name": "실DB (DS_SQL 값 조회)"},
    ),
    6: (
        {"kind": "데이터", "name": "normalization_rules.sample.json"},
        {"kind": "코드", "name": "set_expression_engine.py"},
        {"kind": "코드", "name": "logical_expression.py"},
    ),
    7: (
        # rag_knowledge_base·sql_examples 는 적재(빌드) 시 Qdrant/그래프로 들어간다 — 검색 단계는 그 인덱스를 조회.
        {"kind": "데이터", "name": "rag_knowledge_base.json (적재시)"},
        {"kind": "데이터", "name": "sql_examples.sample.sql (적재시)"},
        {"kind": "모델", "name": "{embed_model}"},
        {"kind": "인프라", "name": "Qdrant + 인메모리 그래프"},
    ),
    8: (
        {"kind": "코드", "name": "sql_ast.py (SelectAst)"},
        {"kind": "코드", "name": "조건빌더 build_*_sql_candidate"},
        {"kind": "데이터", "name": "schema_catalog.json"},
        {"kind": "데이터", "name": "member_target_filters.json"},
        {"kind": "데이터", "name": "member_metrics.json"},
        # 수치 지표 측정단위는 지표 레지스트리(docs/data/runtime/sql/metrics/*.json)에서 읽는다(metric_registry).
        {"kind": "데이터", "name": "docs/data/runtime/sql/metrics/*.json"},
        {"kind": "프롬프트", "name": "(LLM 폴백 인라인)"},
        {"kind": "모델", "name": "{model} (폴백)"},
    ),
    9: (
        {"kind": "데이터", "name": "schema_catalog.json"},
        # 의미검증 시스템 프롬프트는 member_target_filters.json(컬럼·코드·팩트테이블)에서 렌더된다.
        {"kind": "데이터", "name": "member_target_filters.json"},
        {"kind": "프롬프트", "name": "(의미검증 인라인)"},
        {"kind": "모델", "name": "{model} (의미검증)"},
    ),
    10: (
        # 커넥션은 8단계(infer_target_connection)에서 이미 확정 — 실행은 커넥션 어댑터로 실DB 를 읽는다.
        {"kind": "코드", "name": "db_connections.py (run_read_query / psycopg)"},
    ),
}


def _trace_stage_shell(step: int, name: str, tech_name: str, method: str, status: str) -> dict[str, Any]:
    shell: dict[str, Any] = {"step": step, "name": name, "tech_name": tech_name, "method": method, "status": status}
    refs = _TRACE_STAGE_REFS.get(step)
    if refs:
        # 모델명은 하드코딩하지 않고 실제 설정을 읽는다(OPENAI_MODEL / QDRANT_EMBEDDING_MODEL).
        # 경량 단계(1·2·4·9)는 fast 모델(gpt-4o-mini)로, 나머지 LLM 단계는 메인 모델로 표기 —
        # 실제 호출 라우팅(_fast_llm_model)과 배지를 일치시킨다.
        main_model = os.getenv("OPENAI_MODEL") or DEFAULT_LLM_MODEL
        if step == _TRACE_SEMANTIC_VERIFY_STEP:
            # 9단계 의미검증은 전용 override(OPENAI_SEMANTIC_VERIFY_MODEL) 가 있으면 그걸, 없으면 fast 모델.
            model = os.getenv("OPENAI_SEMANTIC_VERIFY_MODEL") or os.getenv("OPENAI_FAST_MODEL") or "gpt-4o-mini"
        elif step in _TRACE_FAST_MODEL_STEPS:
            model = os.getenv("OPENAI_FAST_MODEL") or "gpt-4o-mini"
        else:
            model = main_model
        embed_model = os.getenv("QDRANT_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
        conceptual_model = (
            os.getenv("OPENAI_CONCEPTUAL_TARGETING_MODEL")
            or main_model
        )
        shell["refs"] = [
            {
                **ref,
                "name": (
                    ref["name"]
                    .replace("{model}", model)
                    .replace("{embed_model}", embed_model)
                    .replace("{conceptual_model}", conceptual_model)
                ),
                "used": False,
            }
            for ref in refs
        ]
    return shell


def _mark_trace_refs_used(stages: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """이번 요청의 Query Plan·검색·SQL 결과에 실제 기여한 참조만 ``used``로 표시한다.

    ``refs`` 전체 목록은 처리 단계에서 사용할 수 있는 자산을 보여주기 위해 유지한다. 이 함수는
    입력이 특정 분기·조건·검색 결과를 만들었을 때만 해당 자산을 강조하도록 응답 메타데이터를 보강한다.
    """
    stages_by_step = {stage.get("step"): stage for stage in stages}

    def mark(step: int, *names: str, kind: str | None = None) -> None:
        wanted_names = set(names)
        for ref in stages_by_step.get(step, {}).get("refs", []):
            if ref.get("name") in wanted_names or (kind is not None and ref.get("kind") == kind):
                ref["used"] = True

    query_plan = result.get("query_plan") or {}
    sql_result = result.get("sql_result") or {}
    prompt_normalization = result.get("prompt_normalization") or {}
    target_user = query_plan.get("target_user") or {}
    exclude = query_plan.get("exclude") or {}
    retrieval = query_plan.get("retrieval") or {}
    parser = query_plan.get("parser") or {}
    matched_terms = query_plan.get("matched_terms") or []
    dimension_filters = query_plan.get("dimension_filters") or []
    conceptual_resolutions = query_plan.get("conceptual_resolutions") or []
    conceptual_summary = query_plan.get("conceptual_targeting_resolution") or {}
    semantic_resolutions = query_plan.get("semantic_resolutions") or []
    set_expressions = query_plan.get("set_expressions") or []
    computed_metrics = query_plan.get("computed_metrics") or []
    policy_constraints = query_plan.get("policy_constraints") or []
    condition_tokens = sql_result.get("condition_tokens") or []
    generation_source = str(sql_result.get("generation_source") or "")
    llm_query_plan_prompt = result.get("llm_query_plan_prompt")
    llm_sql_prompt = result.get("llm_sql_prompt")
    semantic_verification = sql_result.get("semantic_verification") or {}
    timings_ms = result.get("timings_ms") or {}

    normalization_mode = str(prompt_normalization.get("mode") or "")
    if normalization_mode == "llm_rewrite":
        mark(1, "prompt_rewrite_system.txt", kind="모델")
    elif normalization_mode == "llm":
        mark(1, "prompt_normalize_system.txt (보수 모드)", kind="모델")

    # 스코프 분리는 규칙 우선이며, LLM 분리가 실제로 선택됐을 때만 프롬프트/모델을 강조한다.
    mark(2, "targeting_lexicon.json")
    # 표면 개념 해석이 실제로 무언가를 읽었을 때만 강조한다 — 백스톱만으로 돈 질의와 구분된다.
    if lexicon_llm.current_signals():
        mark(2, "surface_concepts.json")
        mark(2, "surface_signal_extract_system.txt", kind="모델")
        mark(3, "surface_concepts.json")
    if retrieval.get("scope_mode") == "llm":
        mark(2, "prompt_scope_split_system.txt", kind="모델")

    parser_used_llm = parser.get("type") == "llm" and isinstance(llm_query_plan_prompt, dict)
    if parser_used_llm:
        mark(3, "query_plan_system.txt", "query_plan_user.txt", kind="모델")
    if matched_terms:
        mark(3, "normalization_rules.sample.json")
    if policy_constraints or any(
        item.get("source") == "business_policies"
        for item in semantic_resolutions
        if isinstance(item, dict)
    ):
        mark(3, "business_policies.sample.json")
    mark(3, "targeting_lexicon.json")

    attribute_canonicals = {
        canonical
        for group in _attribute_token_groups().values()
        for canonical, _terms in group.canonicals
    }
    lifecycle_values = set(target_user.get("lifecycle") or []) | set(exclude.get("lifecycle") or [])
    if attribute_canonicals & lifecycle_values:
        mark(3, "attribute_token_groups.json")
    if query_plan.get("member_metric_selection") is not None or target_user.get("balance_conditions"):
        mark(3, "member_metrics.json")
    if computed_metrics:
        mark(3, "schema_catalog.json")

    if query_plan.get("_trace_target_object_llm_used"):
        mark(4, "target_object_extract_system.txt", kind="모델")
    if target_user.get("purchase_object") or (query_plan.get("campaign_constraints") or {}).get("sell_object"):
        mark(4, "surface_concepts.json")
    product_resolution = target_user.get("purchase_object_resolution")
    if isinstance(product_resolution, dict):
        mark(5, "CRM_CM_PRODUCT", kind="인프라")

    uses_member_value_index = any(
        item.get("source") == "member_value_index"
        for item in dimension_filters
        if isinstance(item, dict)
    )
    uses_dimension_catalog = any(
        item.get("codes")
        and item.get("source")
        not in {"member_value_index", "llm_common_sense"}
        for item in dimension_filters
        if isinstance(item, dict)
    )
    conceptual_used = any(
        isinstance(item, Mapping)
        and item.get("status") == "resolved"
        and _generated_filter_is_attached(
            query_plan, item.get("generated_filter")
        )
        for item in conceptual_resolutions
    )
    conceptual_invoked = (
        isinstance(conceptual_summary, Mapping)
        and bool(conceptual_summary.get("model"))
    )
    has_member_condition = any(value not in (None, [], {}) for value in target_user.values()) or bool(exclude) or bool(dimension_filters)
    if uses_dimension_catalog:
        mark(5, "dimension_catalog.sample.json", "실DB (DS_SQL 값 조회)")
    if uses_member_value_index:
        mark(5, "member_value_index.json")
    if conceptual_used or conceptual_invoked:
        mark(
            5,
            "conceptual_targeting_system.txt",
            "conceptual_targeting.py",
            "member_value_index.json",
            "member_target_filters.json",
            "schema_catalog.json",
        )
    if conceptual_invoked:
        mark(5, kind="모델")
    if has_member_condition:
        mark(5, "member_target_filters.json")

    if set_expressions:
        mark(6, "normalization_rules.sample.json", "set_expression_engine.py", "logical_expression.py")

    search_executed = "vector_search" in timings_ms or "keyword_search" in timings_ms
    search_hits = [
        *result.get("vector_matches", []),
        *result.get("keyword_matches", []),
        *result.get("graph_context", []),
    ]
    if search_executed:
        mark(7, "rag_knowledge_base.json (적재시)", "Qdrant + 인메모리 그래프")
    if "vector_search" in timings_ms:
        mark(7, kind="모델")
    if any(item.get("type") == "sql_example" for item in search_hits if isinstance(item, dict)):
        mark(7, "sql_examples.sample.sql (적재시)")

    has_sql_candidate = bool(sql_result.get("candidates"))
    if has_sql_candidate:
        mark(8, "sql_ast.py (SelectAst)", "schema_catalog.json")
    if has_sql_candidate and not generation_source.startswith("llm"):
        mark(8, "조건빌더 build_*_sql_candidate")
    if any(
        str(token.get("path") or "").startswith(("target_user.", "exclude.", "dimension_filters."))
        for token in condition_tokens
        if isinstance(token, dict)
    ):
        mark(8, "member_target_filters.json")
    if any(
        str(token.get("path") or "").startswith(("computed_metrics.", "member_metric_selection", "target_user.aggregate_conditions"))
        for token in condition_tokens
        if isinstance(token, dict)
    ):
        mark(8, "member_metrics.json", "docs/data/runtime/sql/metrics/*.json")
    if isinstance(llm_sql_prompt, dict):
        mark(8, "(LLM 폴백 인라인)", kind="모델")

    if has_sql_candidate:
        mark(9, "schema_catalog.json")
    if semantic_verification.get("ran"):
        mark(9, "member_target_filters.json", "(의미검증 인라인)", kind="모델")


def _trace_failure_diagnosis(
    category: str,
    label: str,
    confidence: str,
    summary: str,
    evidence: list[str],
    next_action: str,
) -> dict[str, Any]:
    return {
        "category": category,
        "label": label,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
        "next_action": next_action,
    }


def _trace_sql_failure_diagnosis(
    query_plan: dict[str, Any],
    sql_result: dict[str, Any],
) -> dict[str, Any] | None:
    """검증 SQL 미생성 사유를 입력·참조 데이터·개발/정책 점검으로 분류한다.

    참조 JSON이 로드돼도 사용자의 조건을 실DB 컬럼으로 매핑하지 못할 수 있으므로, 근거가 명확한
    ``unsupported_conditions`` 계열만 데이터/매핑 부족으로 단정한다. 나머지는 입력 부족 또는
    개발/정책 설정 점검으로 분리하고, 근거가 약하면 낮은 신뢰도로 반환한다.
    """
    reason = str(sql_result.get("failure_reason") or "")
    if not reason:
        return None

    unsupported_labels = [
        str(label)
        for label in sql_result.get("unsupported_condition_labels", [])
        if str(label)
    ]
    missing_paths = [
        str(condition.get("path"))
        for condition in sql_result.get("missing_input_conditions", [])
        if isinstance(condition, dict) and condition.get("path")
    ]
    clarification_questions = [
        str(question)
        for question in sql_result.get("clarification_questions", [])
        if str(question)
    ]
    recognized_domains = _recognized_domains(query_plan)

    if reason == "query_plan_required_conditions_missing":
        evidence = [f"미확정 조건: {path}" for path in missing_paths]
        evidence.extend(f"확인 질문: {question}" for question in clarification_questions)
        return _trace_failure_diagnosis(
            "input_incomplete",
            "입력 조건 추가 필요",
            "high",
            "필수 조건이 확정되지 않아 SQL을 만들지 못했습니다. 참조 JSON 부족으로 단정하지 않습니다.",
            evidence or ["Query Plan 필수 조건이 비어 있습니다."],
            "실패 단계에 표시된 조건을 더 구체적으로 입력한 뒤 다시 실행하세요.",
        )

    data_gap_reasons = {
        "real_db_unsupported_conditions",
        "recognized_domain_unsupported",
        *_UNSUPPORTED_INTENT_REASONS,
    }
    if reason in data_gap_reasons or (reason == "no_sql_candidates" and recognized_domains):
        evidence = [f"실DB 미매핑 조건: {label}" for label in unsupported_labels]
        evidence.extend(
            f"인식된 도메인: {domain.get('label', domain.get('code', 'unknown'))}"
            for domain in recognized_domains
            if isinstance(domain, dict)
        )
        return _trace_failure_diagnosis(
            "reference_data_gap",
            "참조 데이터/매핑 보강 필요",
            "high" if unsupported_labels else "medium",
            "입력 조건은 인식됐지만 현재 참조 JSON 또는 실DB 매핑으로 SQL 조건을 만들 수 없습니다.",
            evidence or [f"실패 사유: {reason}"],
            "강조된 member_target_filters.json, dimension_catalog.sample.json, schema_catalog.json의 조건·컬럼·코드 매핑을 확인하세요.",
        )

    if reason == "no_sql_candidates":
        return _trace_failure_diagnosis(
            "input_unrecognized",
            "타겟 조건 인식 부족",
            "medium",
            "명시적인 타겟 조건을 찾지 못해 SQL 후보를 만들지 못했습니다.",
            ["인식된 실DB 타겟 도메인이 없습니다."],
            "성별, 연령, 등급, 지역, 구매 이력처럼 지원되는 타겟 조건을 추가해 다시 실행하세요.",
        )

    implementation_reasons = {
        "sql_guard_failed",
        "query_plan_conditions_missing",
        "query_plan_unmentioned_conditions_added",
        "intent_scope_mismatch",
        "semantic_verification_failed",
    }
    if reason in implementation_reasons:
        failure_stage = _classify_failure_stage(reason, sql_result)
        return _trace_failure_diagnosis(
            "implementation_or_policy_review",
            "개발/정책 설정 점검 필요",
            "high",
            "입력 데이터 부족으로 판정할 근거보다 생성·검증 규칙에서 막힌 근거가 더 강합니다.",
            [
                f"실패 사유: {reason}",
                f"실패 단계: {failure_stage['label']}" if failure_stage else "실패 단계 미분류",
            ],
            "해당 단계의 기술 정보와 SQL 가드·조건 커버리지·의미 검증 결과를 개발자가 확인하세요.",
        )

    return _trace_failure_diagnosis(
        "unknown",
        "원인 추가 확인 필요",
        "low",
        "현재 실패 사유만으로는 참조 데이터 부족과 개발 오류를 신뢰성 있게 구분할 수 없습니다.",
        [f"실패 사유: {reason}"],
        "실패 단계의 상세 로그와 참조 파일을 함께 확인하세요.",
    )


def _trace_exception_diagnosis(error_message: str) -> dict[str, Any]:
    """중간 예외를 데이터 파일·인프라·개발 오류로 보수적으로 분류한다."""
    normalized = error_message.casefold()
    if "filenotfounderror" in normalized or "no such file" in normalized:
        return _trace_failure_diagnosis(
            "reference_data_error",
            "참조 파일 누락",
            "high",
            "처리에 필요한 JSON 또는 프롬프트 파일을 찾지 못했습니다.",
            [error_message],
            "docs/data 또는 docs/prompts 경로와 배포된 파일 목록을 확인하세요.",
        )
    if "jsondecodeerror" in normalized or "json decode" in normalized:
        return _trace_failure_diagnosis(
            "reference_data_error",
            "참조 JSON 형식 오류",
            "high",
            "참조 JSON을 읽는 중 형식 오류가 발생했습니다.",
            [error_message],
            "최근 수정한 docs/data JSON의 문법과 필수 필드를 확인하세요.",
        )
    if any(token in normalized for token in ("connectionerror", "timeouterror", "operationalerror", "importerror", "modulenotfounderror", "qdrant", "openai", "missing_openai_api_key")):
        return _trace_failure_diagnosis(
            "infrastructure_or_configuration",
            "실행 환경/연결 설정 점검 필요",
            "high",
            "참조 데이터나 애플리케이션 코드보다 외부 서비스 또는 환경 설정 문제 가능성이 높습니다.",
            [error_message],
            "Qdrant·DB·OpenAI 연결과 관련 환경 변수를 확인하세요.",
        )
    if any(token in normalized for token in ("keyerror", "attributeerror", "assertionerror", "nameerror", "typeerror", "unboundlocalerror", "programmingerror", "syntaxerror", "undefinedtable", "undefinedcolumn")):
        return _trace_failure_diagnosis(
            "implementation_error",
            "개발 오류 가능성",
            "high",
            "처리 코드의 예외 유형이 감지됐습니다. 참조 JSON 부족으로 보이지 않습니다.",
            [error_message],
            "실패 단계의 서버 로그와 스택 트레이스를 개발자가 확인하세요.",
        )
    if "valueerror" in normalized:
        return _trace_failure_diagnosis(
            "input_or_configuration_error",
            "입력값 또는 설정값 확인 필요",
            "medium",
            "허용되지 않은 입력값 또는 설정값이 처리 단계에서 감지됐습니다.",
            [error_message],
            "요청 파라미터와 관련 환경 설정의 허용값을 확인하세요.",
        )
    return _trace_failure_diagnosis(
        "unknown",
        "원인 추가 확인 필요",
        "low",
        "예외 유형만으로 참조 데이터 부족과 개발 오류를 신뢰성 있게 구분할 수 없습니다.",
        [error_message],
        "서버 로그와 실패 단계의 기술 정보를 확인하세요.",
    )


def _trace_execution_failure_diagnosis(execution: dict[str, Any]) -> dict[str, Any]:
    error = str(execution.get("error") or execution.get("failure_reason") or "database_execution_failed")
    diagnosis = _trace_exception_diagnosis(error)
    if diagnosis["category"] != "unknown":
        return diagnosis
    return _trace_failure_diagnosis(
        "infrastructure_or_configuration",
        "DB 실행 설정 점검 필요",
        "medium",
        "검증 SQL은 생성됐지만 DB 실행 단계에서 실패했습니다.",
        [error],
        "대상 DB 연결, 권한, 방언 설정과 실행된 SQL을 확인하세요.",
    )


def build_partial_retrieve_trace(query: str, timings_ms: dict[str, Any], error_message: str) -> dict[str, Any]:
    """retrieve() 가 중간에 예외로 죽었을 때, 그때까지 채워진 timings_ms 로 '오류 전까지'의 부분 트레이스를 만든다.

    완료된 단계는 status=ok, 처음으로 도달하지 못한 단계는 status=fail(여기서 막힘), 그 이후는 skipped.
    실제 예외 지점을 timings_ms 계측 단위(정규화/Query Plan/검색/SQL 생성)로만 좁힐 수 있어, 그 묶음의 첫
    단계에 오류를 귀속한다(예: Query Plan LLM 파싱 실패 → '질의 계획 수립'에 표시)."""
    completed: set[int] = set()
    for key, steps in _TRACE_TIMING_TO_STEPS:
        if timings_ms.get(key) is not None:
            completed.update(steps)

    stages: list[dict[str, Any]] = []
    error_marked = False
    for step, name, tech_name, method in _TRACE_STAGES_META:
        if step in completed:
            status = "ok"
        elif not error_marked and step != 10:
            status, error_marked = "fail", True
        else:
            status = "skipped"  # 오류 이후 단계이거나(도달 못함), 실행 전 중단된 10단계
        shell = _trace_stage_shell(step, name, tech_name, method, status)
        if status == "fail":
            shell["summary"] = "이 단계에서 처리가 중단되었습니다"
            shell["details"] = [f"오류: {error_message}"]
        elif status == "skipped" and step != 10:
            shell["summary"] = "앞 단계 오류로 실행되지 않음"
        stages.append(shell)

    return {
        "query": query,
        "partial": True,
        "stages": stages,
        "timings_ms": timings_ms,
        "failure_diagnosis": _trace_exception_diagnosis(error_message),
        "result": {"status": "error", "message": f"처리 중 오류가 발생했습니다: {error_message}"},
    }


def _trace_metric_ranking_line(ranking: dict[str, Any] | None) -> str | None:
    """지표 랭킹 조건을 '원문 어느 말이 어느 지표가 됐는가' 한 줄로 만든다(3단계 표시용).

    지표어를 그대로 말한 문장은 사전이 읽고, 에두른 표현('돈이 많아 보이는 고객')은 닫힌 지표 집합에서
    LLM 이 고른다. 두 경로는 결과 SQL 이 같아서 화면에서 구분이 안 되는데, 사용자가 확인해야 할 것은
    바로 그 차이다 — 뜻으로 읽은 해석은 사람이 맞는지 봐야 하기 때문에 경로를 명시한다."""
    if not ranking:
        return None
    side = "하위" if ranking.get("direction") == "low" else "상위"
    if ranking.get("limit_type") == "percent":
        percent = ranking.get("percent")
        # 5.0 → '5' (정수 퍼센트를 소수로 보이지 않게). 소수 퍼센트는 그대로 둔다.
        shown = int(percent) if isinstance(percent, float) and percent.is_integer() else percent
        limit = f"{side} {shown}%"
    else:
        limit = f"{side} {ranking.get('top_n')}명"
    label = ranking.get("metric_label") or ranking.get("metric_id")
    route = "뜻 해석(LLM이 지표 목록에서 선택)" if ranking.get("resolution_source") == "llm" else "지표어 사전 매칭"
    source = ranking.get("matched_text")
    head = f"‘{source}’ → " if source else ""
    return f"{head}{label}({ranking.get('metric_id')}) 기준 {limit} · {route}"


def build_retrieve_trace(result: dict[str, Any]) -> dict[str, Any]:
    """retrieve() 결과를 사용자용 10단계 트레이스(프롬프트 재작성 → … → 실행·결과)로 재구성한다.
    각 단계에 method(혼합/규칙)·status(ok/info/fail/skipped)를 붙인다. LLM 호출 없이 결정론적으로 동작."""
    query_plan = result.get("query_plan", {})
    sql_result = result.get("sql_result", {})
    api_response = result.get("api_response", {})
    target_user = query_plan.get("target_user", {})
    retrieval = query_plan.get("retrieval", {})
    prompt_normalization = result.get("prompt_normalization", {})
    # 정규화 프롬프트를 타겟팅/채널 절로 나눈 결과(검색·그래프 스코프의 근거).
    prompt_scopes = {
        "mode": retrieval.get("scope_mode"),
        "targeting": retrieval.get("targeting_query"),
        "channel": retrieval.get("channel_query"),
    }

    def _hit_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "rank": index + 1,
                "id": hit.get("id"),
                "type": hit.get("type"),
                "score": hit.get("score"),
                "snippet": (hit.get("text") or "")[:160],
            }
            for index, hit in enumerate(hits)
        ]

    graph_rows = [
        {
            "rank": index + 1,
            "id": node.get("id"),
            "type": node.get("type"),
            "title": node.get("title"),
            "score": node.get("score"),
            "seed_score": node.get("seed_score"),
            "is_seed": node.get("seed_score") is not None,
            "reached_via": node.get("reasons", []),
            "path": node.get("path", []),
        }
        for index, node in enumerate(result.get("graph_context", []))
    ]

    candidate_rows = [
        {
            "id": candidate.get("id"),
            "source": candidate.get("source"),
            "tables": candidate.get("tables", []),
            "is_eligible": candidate.get("is_eligible"),
            "guard_valid": candidate.get("validation", {}).get("is_valid"),
            "coverage_ok": candidate.get("coverage", {}).get("is_satisfied"),
            "coverage_missing": [
                condition.get("path")
                for condition in candidate.get("coverage", {}).get("missing_conditions", [])
            ],
            "intent_scope_ok": candidate.get("intent_scope", {}).get("is_satisfied"),
            "unmentioned_ok": candidate.get("unmentioned_conditions", {}).get("is_satisfied"),
            # 집계·grain 정합성 경고(DISTINCT 은폐·1:N 조인 의심 등) — 후보를 탈락시키진 않지만 검토용으로 노출.
            "analytics_warnings": [issue.get("message") for issue in candidate.get("analytics_warnings", [])],
            "sql": candidate.get("sql"),
        }
        for candidate in sql_result.get("candidates", [])
    ]

    # 트레이스 1단계(요청 이해)는 '타겟팅 프롬프트' 기준으로 보여준다. 캠페인 목표·발송 채널 절에서만
    # 나온 정규화 매칭(예: "재구매를"->repeat_buyer)은 오디언스 조건이 아니므로 추론 표시에서 제외한다.
    _targeting_compact = (prompt_scopes.get("targeting") or "").replace(" ", "").casefold()
    _channel_compact = (prompt_scopes.get("channel") or "").replace(" ", "").casefold()

    def _is_targeting_match(match: dict[str, Any]) -> bool:
        matched = (match.get("matched_text") or "").replace(" ", "").casefold()
        # 채널/목표 절에만 등장하는 표현이면 타겟팅 추론에서 뺀다(양쪽에 있거나 어디에도 없으면 유지).
        return not (matched and matched in _channel_compact and matched not in _targeting_compact)

    targeting_matched_terms = [
        match for match in query_plan.get("matched_terms", []) if _is_targeting_match(match)
    ]

    # ── 단계별 페이로드 준비 ─────────────────────────────────────────────────────
    tu = {key: value for key, value in target_user.items() if value not in (None, [], {})}
    tu_purchase = {k: v for k, v in tu.items() if k in _PURCHASE_PLAN_KEYS}
    tu_profile = {k: v for k, v in tu.items() if k not in _PURCHASE_PLAN_KEYS}
    set_expressions = query_plan.get("set_expressions", []) or []
    dimension_filters = query_plan.get("dimension_filters", []) or []
    semantic_resolutions = query_plan.get("semantic_resolutions", []) or []
    conceptual_resolutions = query_plan.get("conceptual_resolutions", []) or []
    conceptual_summary = (
        query_plan.get("conceptual_targeting_resolution") or {}
    )
    conceptual_status = str(
        conceptual_summary.get("status") or ""
    ).casefold()
    conceptual_stage_status = (
        "fail"
        if conceptual_status in {"failed", "unavailable", "unsupported"}
        else (
            "info"
            if (
                dimension_filters
                or semantic_resolutions
                or conceptual_resolutions
                or conceptual_summary
            )
            else "skipped"
        )
    )
    semantic_verification = sql_result.get("semantic_verification", {"ran": False}) or {"ran": False}
    failure_stage = _classify_failure_stage(sql_result.get("failure_reason"), sql_result)
    cart_context = query_plan.get("cart_context")
    corrections = prompt_normalization.get("corrections", []) or []
    vcount = len(result.get("vector_matches", []))
    kcount = len(result.get("keyword_matches", []))
    gcount = len(graph_rows)
    selected_sql = sql_result.get("sql")
    is_success = sql_result.get("is_success")

    # ── 3단계용: 원문 → 계획 문장 → Query Plan JSON (실제 예문이 잘려 JSON 이 되는 모습) ──
    planning_query = query_plan.get("planning_query") or prompt_normalization.get("normalized")
    campaign_constraints = {k: v for k, v in query_plan.get("campaign_constraints", {}).items() if v not in (None, [], {})}
    # 지표 랭킹은 target_user 가 아니라 plan 최상위에 사는 슬롯이라 위 묶음에 안 잡혔다 — 화면의
    # Query Plan JSON 에서 '무엇 기준 상위 N명인가'가 통째로 안 보이던 구멍이었다.
    metric_ranking = query_plan.get("member_metric_ranking")
    metric_ranking = metric_ranking if isinstance(metric_ranking, dict) else None
    plan_json_slots = {k: v for k, v in {
        "intent": query_plan.get("intent"),
        "target_user": tu,
        "member_metric_ranking": metric_ranking,
        "dimension_filters": dimension_filters,
        "conceptual_resolutions": conceptual_resolutions,
        "set_expressions": [e.get("ko_label") or e.get("expression_id") for e in set_expressions],
        "campaign_constraints": campaign_constraints,
    }.items() if v not in (None, [], {}, "")}
    plan_json = json.dumps(plan_json_slots, ensure_ascii=False, indent=2)
    metric_ranking_line = _trace_metric_ranking_line(metric_ranking)

    # 실제 전송된 LLM 프롬프트(질의 계획 tool calling / SQL 폴백). retrieve 가 result 상단에 담아 준다.
    llm_query_plan_prompt = result.get("llm_query_plan_prompt")
    llm_sql_prompt = result.get("llm_sql_prompt")

    # ── 8단계용: 선택된 SQL 을 어떤 방식으로 만들었나(결정론 조건빌더 vs LLM 폴백; 둘 다 SelectAst 로 렌더) ──
    generation_source = sql_result.get("generation_source")
    if not generation_source:
        generation_label = None
    elif str(generation_source).startswith("llm"):
        generation_label = f"LLM 폴백 생성 ({generation_source})"
    else:
        generation_label = f"결정론 조건빌더 ({generation_source})"

    def _stage(step: int, status: str, **payload: Any) -> dict[str, Any]:
        _, name, tech_name, method = _TRACE_STAGES_META[step - 1]
        stage = _trace_stage_shell(step, name, tech_name, method, status)
        # 빈 값(None/[]/{}/"")은 화면에 노이즈라 떨어뜨린다. 0/False 는 의미가 있어 남긴다.
        stage.update({k: v for k, v in payload.items() if v not in (None, [], {}, "")})
        return stage

    # 후보별 검증 플래그(9단계) 한 줄 요약.
    candidate_flag_lines = []
    for candidate in candidate_rows:
        parts = []
        for label, key in (("guard", "guard_valid"), ("coverage", "coverage_ok"), ("scope", "intent_scope_ok"), ("unmentioned", "unmentioned_ok"), ("eligible", "is_eligible")):
            val = candidate.get(key)
            if val is not None:
                parts.append(f"{label}={'✓' if val else '✗'}")
        candidate_flag_lines.append(f"candidate {candidate.get('id')}: {' '.join(parts)}".rstrip())

    stages = [
        _stage(
            1, "info",
            description="고객 문장의 오타·표현을 시스템이 이해할 표준 문장으로 다시 씁니다(LLM 재작성).",
            summary=prompt_normalization.get("summary") or None,
            plain=[line for line in [
                f"입력: {prompt_normalization.get('original', result.get('query'))}",
                f"재작성: {prompt_normalization.get('normalized', result.get('query'))}",
            ] if line],
            details=[f"교정: {_trace_line(c)}" for c in corrections],
        ),
        _stage(
            2, "info",
            description="재작성 문장을 '누구를(타겟)' 절과 '무엇을 보낼지(채널·혜택)' 절로 나눕니다.",
            plain=[line for line in [
                f"타겟 절: {prompt_scopes.get('targeting')}" if prompt_scopes.get("targeting") else None,
                f"채널 절: {prompt_scopes.get('channel')}" if prompt_scopes.get("channel") else None,
                None if (prompt_scopes.get("targeting") or prompt_scopes.get("channel")) else "분리 없음 — 전체 문장을 타겟 절로 사용",
            ] if line],
            details=[f"scope_mode={prompt_scopes.get('mode')}"] if prompt_scopes.get("mode") else [],
        ),
        _stage(
            3, "info",
            summary=f"intent={query_plan.get('intent')}" if query_plan.get("intent") else None,
            description="타겟 절을 규칙 파싱 + LLM tool calling 으로 구조화된 Query Plan JSON(슬롯)으로 바꿉니다. 이 JSON 이 다음 SQL 조립의 입력입니다.",
            plain=[line for line in (
                [f"계획 문장(파싱 대상): {planning_query}" if planning_query else None]
                + [f"‘{m.get('matched_text')}’ → {m.get('canonical')}" for m in targeting_matched_terms if m.get("matched_text") and m.get("canonical")]
                # 지표 랭킹은 matched_terms 에 안 남는다(사전 매칭이 아니라 슬롯 확정이라). 사전이
                # 침묵해 LLM 이 뜻으로 읽은 경우엔 특히 이 줄이 유일한 표시라 함께 노출한다.
                + [metric_ranking_line]
            ) if line],
            # 원문이 어떻게 잘려 어떤 JSON 값이 되는지 그대로 노출한다.
            details=[
                f"① 입력 원문: {result.get('query')}",
                f"② 계획 문장(타겟 절만): {planning_query}",
                "③ Query Plan JSON:\n" + plan_json,
            ] + ([f"④ 이 JSON 으로 만들 SQL 생성 방식: {generation_label}"] if generation_label else [])
            + (["── 조건 결정 감사 로그(필터 → 액션 슬롯: 사유) ──"] + plan_decisions.render(query_plan)
               if plan_decisions.decisions(query_plan) else [])
            + (["── 실제 LLM 프롬프트(질의 계획, tool calling) ──"] + _format_captured_prompt(llm_query_plan_prompt)
               if llm_query_plan_prompt else ["LLM 미사용 — 규칙 파싱으로 계획 수립(parser=" + str((query_plan.get("parser") or {}).get("type") or "rules") + ")"]),
        ),
        _stage(
            4, "info" if (tu_purchase or cart_context) else "skipped",
            description="상품 구매·판매 이력은 LLM으로 추출한 뒤 원문 검증하고, 장바구니 같은 행동 조건은 규칙으로 구조화합니다.",
            plain=None if (tu_purchase or cart_context) else ["구매·상품 관련 조건 없음"],
            details=[f"{k}: {_trace_line(v)}" for k, v in tu_purchase.items()]
                    + ([f"cart_context: {_trace_line(cart_context)}"] if cart_context else []),
        ),
        _stage(
            5,
            conceptual_stage_status,
            description=(
                "명시 디멘션 값은 레지스트리/DS_SQL로 변환하고, 상식 표현은 LLM이 "
                "동적으로 발견된 닫힌 후보 ID만 선택한 뒤 서버가 실제 DB 필터로 연결합니다."
            ),
            plain=[
                (
                    f"{d.get('prompt_label') or d.get('dimension_id')} → "
                    f"{', '.join(map(str, d.get('codes', []))) or _trace_line(d.get('names', []))}"
                    + (
                        " (LLM 일반지식·비실시간)"
                        if d.get("source") == "llm_common_sense"
                        else ""
                    )
                )
                for d in dimension_filters
            ] or (
                [f"상식 해석 결과: {conceptual_status or 'unknown'}"]
                if conceptual_summary
                else (
                    ["의미 해석 적용"]
                    if semantic_resolutions
                    else None
                )
            ),
            details=[f"dimension: {_trace_line(d)}" for d in dimension_filters]
                    + [f"semantic: {_trace_line(s)}" for s in semantic_resolutions]
                    + [
                        (
                            "conceptual receipt: "
                            + _trace_line({
                                "evidence": item.get("source_text"),
                                "selected_values": item.get("selected_values"),
                                "operator": item.get("operator"),
                                "confidence": item.get("confidence"),
                                "rationale": item.get("rationale"),
                                "model": item.get("model"),
                                "catalog_digest": item.get("catalog_digest"),
                                "realtime": item.get("realtime"),
                                "status": item.get("status"),
                            })
                        )
                        for item in conceptual_resolutions
                        if isinstance(item, Mapping)
                    ]
                    + (
                        [
                            "conceptual summary: "
                            + _trace_line({
                                "status": conceptual_summary.get("status"),
                                "model": conceptual_summary.get("model"),
                                "prompt_digest": conceptual_summary.get("prompt_digest"),
                                "catalog_digest": conceptual_summary.get("catalog_digest"),
                                "cache_hit": conceptual_summary.get("cache_hit"),
                                "basis": conceptual_summary.get("basis"),
                            })
                        ]
                        if conceptual_summary
                        else []
                    ),
        ),
        _stage(
            6, "info" if set_expressions else "skipped",
            description="‘A 또는 B’·‘A이면서 B’ 같은 집합 연산을 파싱해 SQL 술어로 만듭니다(결정론).",
            plain=None if set_expressions else ["집합식 없음"],
            details=[f"{e.get('ko_label') or e.get('expression_id')}: {_trace_line(e.get('set_ast'))}" for e in set_expressions],
        ),
        _stage(
            7, "info",
            summary=f"벡터 {vcount} · 키워드 {kcount} → 확장 {gcount}",
            description="벡터(의미)·키워드(글자) 검색으로 지식을 찾고 관계 그래프로 확장합니다.",
            plain=[f"AI 유사도 검색 {vcount}건, 키워드 검색 {kcount}건에서 출발해 관계 그래프로 {gcount}건까지 넓혔습니다."],
            hits=graph_rows,
            count=gcount,
            seed_count=len(result.get("seed_matches", [])),
            details=[f"vector: {_trace_line(h.get('id'))} ({h.get('score')})" for h in result.get("vector_matches", [])[:5]]
                    + [f"keyword: {_trace_line(h.get('id'))} ({h.get('score')})" for h in result.get("keyword_matches", [])[:5]],
        ),
        _stage(
            8, "ok" if selected_sql else "info",
            summary=(f"{generation_label} · " if generation_label else "") + f"후보 {len(candidate_rows)}개" + (" · 선택됨" if selected_sql else " · 미선택"),
            description="Query Plan JSON 을 조건빌더가 SelectAst(sql_ast.py)로 조립해 SQL 로 렌더합니다. 결정론 빌더가 표현 못 하면 LLM 폴백으로 초안을 만들고 같은 가드를 태웁니다.",
            plain=[line for line in [
                f"생성 방식: {generation_label}" if generation_label else None,
                f"조건 토큰 {len(sql_result.get('condition_tokens', []))}개로 SQL 후보 {len(candidate_rows)}개를 SelectAst 로 조립했습니다.",
                "검증 통과 SQL이 선택되었습니다." if selected_sql else "선택된 SQL이 아직 없습니다(다음 단계에서 사유 표시).",
            ] if line],
            details=[line for line in [
                "condition_tokens: " + ", ".join(t.get("path") for t in sql_result.get("condition_tokens", []) if t.get("path")),
                "required_conditions: " + ", ".join(c.get("path") for c in sql_result.get("required_conditions", []) if c.get("path")),
            ] if line.split(": ", 1)[-1]]
                    + [f"candidate {c.get('id')}: tables={','.join(c.get('tables', []))}" for c in candidate_rows]
                    + (["── 실제 LLM 프롬프트(SQL 폴백) ──"] + _format_captured_prompt(llm_sql_prompt)
                       if llm_sql_prompt else ["결정론 조건빌더가 SelectAst 로 직접 조립 — LLM 프롬프트 없음(생성 SQL 은 아래 '실행된 SQL' 참조)"]),
        ),
        _stage(
            9, "ok" if is_success else "fail",
            summary="검증 통과" if is_success else ("검증 실패 · " + (failure_stage.get("label") if failure_stage else str(sql_result.get("failure_reason")))),
            description="생성된 SQL이 허용 테이블·컬럼만 쓰는지, 요청 의도와 어긋나지 않는지 자동 점검합니다.",
            plain=[line for line in [
                "안전 검증을 통과했습니다." if is_success else (f"‘{failure_stage.get('label')}’ 단계에서 막혔습니다." if failure_stage else "검증에서 막혔습니다."),
                (
                    "의미 검증: "
                    + {
                        "pass": "원문과 일치",
                        "review": "복수 해석 가능(비차단 검토)",
                        "fail": "불일치(확인 필요)",
                    }.get(
                        str(semantic_verification.get("status") or ""),
                        "원문과 일치" if semantic_verification.get("faithful") else "불일치(확인 필요)",
                    )
                ) if semantic_verification.get("ran") else None,
            ] if line],
            details=candidate_flag_lines
                    + ([f"semantic_verification: ran={semantic_verification.get('ran')} status={semantic_verification.get('status')} faithful={semantic_verification.get('faithful')} issues={_trace_line(semantic_verification.get('issues', []))}"] if semantic_verification.get("ran") else [])
                    + ([f"failure_reason: {sql_result.get('failure_reason')}"] if sql_result.get("failure_reason") else []),
            failure_stage=failure_stage,
        ),
        # 10단계(실행·결과)는 엔드포인트가 execute 후 채운다. 여기선 대기 상태로 둔다.
        _stage(
            10, "skipped",
            summary="실행 대기",
            description="확정된 SQL을 실DB에서 실행해 대상 고객 수를 집계합니다.",
        ),
    ]

    # 3단계 배지를 실제 parser 에 맞춰 정직하게: 규칙 파싱이면 혼합/모델/프롬프트 배지를 규칙으로 정정한다
    # (query_parser=rules 기본이면 LLM 질의계획이 호출되지 않아 모델·프롬프트가 실제로 안 쓰인다).
    if (query_plan.get("parser") or {}).get("type") != "llm":
        stage3 = stages[2]
        stage3["method"] = "규칙"
        stage3["refs"] = [ref for ref in stage3.get("refs", []) if ref["kind"] not in ("모델", "프롬프트")]

    _mark_trace_refs_used(stages, result)

    failure_diagnosis = _trace_sql_failure_diagnosis(query_plan, sql_result)
    return {
        "query": result.get("query"),
        "collection": result.get("collection"),
        "retrieval_scope": result.get("retrieval_scope"),
        "prompt_scopes": prompt_scopes,
        "stages": stages,
        "stage_log": result.get("stage_log", []),
        # 조건 결정 감사 로그(필터, 액션, 슬롯, 사유) — 어느 단계가 어떤 조건을 만들고 지웠는지의 원본 기록.
        "decisions": plan_decisions.decisions(query_plan),
        "decisions_truncated": bool(query_plan.get(plan_decisions.TRUNCATED_KEY)),
        "plan_resolution": query_plan.get("plan_resolution", {}),
        "failure_diagnosis": failure_diagnosis,
        "result": {
            "status": api_response.get("status"),
            "sql": api_response.get("sql"),
            "blocked_sql": api_response.get("blocked_sql"),
            "target_connection": api_response.get("target_connection"),
            "message": api_response.get("message"),
        },
        "timings_ms": result.get("timings_ms", {}),
    }


def apply_execution_to_trace(trace: dict[str, Any], execution: dict[str, Any]) -> None:
    """execute_target_sql 결과를 트레이스 10단계(실행·결과)에 반영한다(엔드포인트에서 호출)."""
    targeting_result = execution.get("targeting_result", {}) or {}
    count = targeting_result.get("target_customer_count")
    ok = execution.get("is_success")
    # 실행할 SQL 이 없어(미지원/needs_clarification 로 sql=None) 또는 요청이 실행을 끈 경우는 '실패' 가
    # 아니라 '생략(skipped)' 이다 — 앞 단계에서 이미 막혀 실행에 도달하지 않은 것을 빨간 '실행 실패' 로
    # 표시하면 오해를 준다(mode='skipped').
    skipped = execution.get("mode") == "skipped"
    for stage in trace.get("stages", []):
        if stage.get("step") != 10:
            continue
        stage["status"] = "ok" if ok else ("skipped" if skipped else "fail")
        stage["summary"] = (
            f"대상 {count:,}명" if isinstance(count, int)
            else ("실행 성공" if ok else ("실행 생략(생성된 SQL 없음)" if skipped else "실행 실패"))
        )
        if not skipped:
            for ref in stage.get("refs", []):
                ref["used"] = True
        if not ok and not skipped:
            trace["failure_diagnosis"] = _trace_execution_failure_diagnosis(execution)
        details = []
        if targeting_result.get("result_row_count") is not None:
            details.append(f"result_row_count={targeting_result.get('result_row_count')}")
        if targeting_result.get("target_campaign_count") is not None:
            details.append(f"target_campaign_count={targeting_result.get('target_campaign_count')}")
        if execution.get("cardinality_diagnostic"):
            details.append(f"cardinality={_trace_line(execution.get('cardinality_diagnostic'))}")
        if execution.get("error"):
            details.append(f"error={_trace_line(execution.get('error'))}")
        if details:
            stage["details"] = details
        break
