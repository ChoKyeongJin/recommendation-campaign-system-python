"""등급/상태 시점·이력 축(relational_operation) 계약.

26종 프롬프트 감사(2026-08-02)의 최대 실패 군집을 고정한다: 데이터 소스는
CRM_MB_MONTHCRMINFO(단일 월 스냅샷 + PREV_* 직전값)에 실재하고, 컴파일러는
compositional_targeting 이 소유한다. 지원 경계는 attribute_catalog.json 선언이
결정한다 — 다월 적재가 생기면 JSON 숫자 하나로 열린다.
"""

from __future__ import annotations

import compositional_targeting as ct
import graph_rag
import semantic_plan
import semantic_requirements
import targeting_ir
from legacy_plan_compiler import LegacyQueryPlanCompiler
from semantic_plan import SemanticPlanV2


def _catalog():
    return graph_rag._attribute_history_catalog()


def _resolve(slot: dict):
    return ct.resolve_operation(slot, _catalog())


def _compile_slot(node_payload: dict):
    """RelationPredicate 노드 -> target_user.relational_operation 슬롯.

    2026-08-02 이전에는 이 자리에 원문 정규식 감지기(detect_member_attribute_history)가
    있었다. 지금은 원문 -> 노드가 LLM 소관이고, 노드 -> 슬롯이 결정론 컴파일러 소관이다.
    아래 테스트들은 **같은 사용자 요구**(각 docstring 의 프롬프트)를 노드 형태로 넣어
    슬롯 산출을 고정한다.
    """
    node = semantic_plan.node_from_dict({
        "id": "req-1", "type": "relation_predicate", "subject": "member", **node_payload,
    })
    result = LegacyQueryPlanCompiler().compile(
        SemanticPlanV2(nodes=[node]), None, graph_rag._semantic_compile_context()
    )
    return result.target_user.get(ct.SLOT_KEY)


def _plan_with_slot(node_payload: dict, query: str) -> dict:
    """컴파일된 슬롯을 실은 플랜(원문 근거 스팬 포함) — 리졸버 배선 테스트 입력."""
    span = node_payload.get("source_span") or query
    start = query.find(span)
    node = {
        "id": "req-1", "type": "relation_predicate", "subject": "member",
        "source_span": span, "source_start": start if start >= 0 else None,
        "source_end": (start + len(span)) if start >= 0 else None,
        **{key: value for key, value in node_payload.items() if key != "source_span"},
    }
    plan: dict = {"target_user": {}, "semantic_plan": {"nodes": [node]}}
    slot = _compile_slot({key: value for key, value in node.items()
                          if key not in {"id", "type", "subject"}})
    if slot is not None:
        plan["target_user"][ct.SLOT_KEY] = slot
    return plan


# ── 연산자 어휘 드리프트 가드(모듈 순수성 때문에 두 곳 선언 — 동일해야 한다) ─────────────


def test_operator_vocabulary_matches_between_ir_and_compiler() -> None:
    assert targeting_ir.RELATIONAL_OPERATORS == ct.OPERATORS


# ── 의미 노드 → 실행 슬롯 컴파일: 26종 감사 프롬프트의 등급/상태 군집 ────────────────
# (과거 detect_member_attribute_history 정규식 감지기가 검증하던 사용자 요구를 이전한 것)


def test_transition_with_window_compiles() -> None:
    """'지난 6개월 동안 골드에서 VIP로 승급한 회원을 찾아줘.'"""
    assert _compile_slot({
        "source_span": "골드에서 VIP로 승급한", "attribute": "member_grade",
        "relation": "transition", "from_value": "골드", "to_value": "VIP", "months": 6,
    }) == {
        "operator": "transition", "attribute_id": "member_grade",
        "from_value": "gold_grade", "to_value": "vip", "months": 6,
    }


def test_state_transition_compiles() -> None:
    """'정상 회원이었다가 휴면 상태로 변경된 회원을 추출해줘.'"""
    slot = _compile_slot({
        "source_span": "정상 회원이었다가 휴면 상태로 변경된", "attribute": "member_state",
        "relation": "transition", "from_value": "정상회원", "to_value": "휴면",
    })
    assert slot["operator"] == "transition"
    assert slot["attribute_id"] == "member_state"
    assert slot["from_value"] == "normal_member"
    assert slot["to_value"] == "dormant"


def test_as_of_without_anchor_becomes_latest_snapshot() -> None:
    """'지난달 말 기준 VIP였던 회원을 찾아줘.' — 앵커 월이 없으면 최신 스냅샷 기준."""
    slot = _compile_slot({
        "source_span": "지난달 말 기준 VIP였던", "attribute": "member_grade",
        "relation": "as_of", "value": "VIP",
    })
    assert slot["operator"] == "as_of_latest"
    assert slot["attribute_id"] == "member_grade"
    assert slot["value"] == "vip"


def test_as_of_with_calendar_month_becomes_month_snapshot() -> None:
    """'2025년 12월 기준 휴면 상태였던 회원 수를 알려줘.'"""
    slot = _compile_slot({
        "source_span": "2025년 12월 기준 휴면 상태였던", "attribute": "member_state",
        "relation": "as_of", "value": "휴면",
        "period": {"type": "calendar_month", "year": 2025, "month": 12},
    })
    assert slot["operator"] == "as_of_month"
    assert slot["month"] == "202512"
    assert slot["value"] == "dormant"


def test_held_throughout_compiles() -> None:
    """'최근 3개월 내내 VIP 등급을 유지한 회원을 찾아줘.'"""
    assert _compile_slot({
        "source_span": "최근 3개월 내내 VIP 등급을 유지한", "attribute": "member_grade",
        "relation": "held_throughout", "value": "VIP", "months": 3,
    }) == {"operator": "held_throughout", "attribute_id": "member_grade",
           "value": "vip", "months": 3}


def test_change_count_and_stable_compile() -> None:
    """'최근 3개월 동안 등급이 두 번 이상 변경된' / '최근 12개월 동안 한 번도 바뀌지 않은'"""
    slot = _compile_slot({
        "source_span": "등급이 두 번 이상 변경된", "attribute": "member_grade",
        "relation": "changed_n_times", "count": 2, "count_operator": "이상", "months": 3,
    })
    assert slot["operator"] == "changed_n_times"
    assert slot["change_count"] == 2
    assert slot["months"] == 3
    assert _compile_slot({
        "source_span": "등급이 한 번도 바뀌지 않은", "attribute": "member_grade",
        "relation": "stable", "months": 12,
    }) == {"operator": "stable", "attribute_id": "member_grade", "months": 12}


def test_ever_never_and_every_month_compile() -> None:
    slot = _compile_slot({
        "source_span": "한 번이라도 휴면 상태였", "attribute": "member_state",
        "relation": "ever", "value": "휴면",
    })
    assert slot["operator"] == "ever" and slot["value"] == "dormant"
    slot = _compile_slot({
        "source_span": "한 번도 휴면 상태가 아니었던", "attribute": "member_state",
        "relation": "never", "value": "휴면", "months": 6,
    })
    assert slot["operator"] == "never" and slot["months"] == 6
    assert _compile_slot({
        "source_span": "모든 월에 구매등급이 존재하는", "attribute": "member_grade",
        "relation": "exists_every_month", "months": 6,
    }) == {"operator": "exists_every_month", "attribute_id": "member_grade", "months": 6}


def test_value_comparison_survives_compilation() -> None:
    """'최근 6개월 중 적어도 한 달은 골드 이상이었던 회원' — 등급 순서 비교는 값의 일부다."""
    slot = _compile_slot({
        "source_span": "적어도 한 달은 골드 이상이었던", "attribute": "member_grade",
        "relation": "ever", "value": "골드", "value_comparison": "gte", "months": 6,
    })
    assert slot["operator"] == "ever"
    assert slot["value"] == "gold_grade"
    assert slot["value_comparison"] == "gte"


def test_no_relation_node_means_no_history_slot() -> None:
    """'휴면 회원을 추출해줘' 처럼 이력 조건이 없으면 노드가 없고, 슬롯도 생기지 않는다.

    과거 감지기의 '과발화 금지' 계약이 여기로 이전됐다 — 슬롯을 만드는 유일한 입력이
    노드라, 노드가 없으면 슬롯이 생길 경로 자체가 없다."""
    result = LegacyQueryPlanCompiler().compile(
        SemanticPlanV2(nodes=[]), None, graph_rag._semantic_compile_context()
    )
    assert ct.SLOT_KEY not in result.target_user


# ── 리졸버: 지원 경계는 카탈로그 선언이 결정한다 ────────────────────────────────────


def test_resolves_as_of_latest_to_snapshot_join() -> None:
    operation = _resolve({"operator": "as_of_latest", "attribute_id": "member_grade", "value": "vip"})
    assert operation["status"] == "resolved"
    assert operation["aggregate"] == "as_of"
    assert operation["anchor"] == {"type": "latest"}
    assert operation["value_predicate"]["values"] == ["MEM_GRADE_CD.VIP"]


def test_resolves_gte_by_rank_expansion() -> None:
    operation = _resolve({
        "operator": "as_of_latest", "attribute_id": "member_grade",
        "value": "gold_grade", "value_comparison": "gte",
    })
    assert operation["status"] == "resolved"
    assert set(operation["value_predicate"]["values"]) == {"MEM_GRADE_CD.GOLD", "MEM_GRADE_CD.VIP"}


def test_resolves_transition_and_blocks_windowed_transition() -> None:
    operation = _resolve({
        "operator": "transition", "attribute_id": "member_grade",
        "from_value": "gold_grade", "to_value": "vip",
    })
    assert operation["status"] == "resolved"
    assert operation["aggregate"] == "transition"
    assert operation["prev_predicate"]["values"] == ["MEM_GRADE_CD.GOLD"]
    blocked = _resolve({
        "operator": "transition", "attribute_id": "member_grade",
        "from_value": "gold_grade", "to_value": "vip", "months": 6,
    })
    assert blocked["status"] == "unsupported"
    assert "직전 스냅샷 대비" in blocked["message"]


def test_shallow_snapshot_load_advises_instead_of_blocking() -> None:
    """적재가 얕은 것과 컴파일러가 없는 것은 **다른 사유**이고 귀결도 다르다.

    창 CTE 는 관측 월 수를 세므로 1개월만 적재돼도 SQL 의 의미는 그대로다 — 조건을 만족하는
    회원이 없어 0건일 뿐이다. 0건은 정직한 답이므로 내보내고 이름을 대며 고지한다.
    반면 연산 자체의 컴파일러가 없으면 낼 SQL 이 없으므로 여전히 막는다.
    """
    resolved = _resolve({"operator": "stable", "attribute_id": "member_grade", "months": 12})
    assert resolved["status"] == "resolved"
    advisory = resolved["advisories"][0]
    assert advisory["code"] == "data_coverage_shallow"
    assert advisory["required_months"] == 12 and advisory["available_months"] == 1
    assert "0건" in advisory["message"]

    # 대조군: **소스 자체가 없으면** 낼 SQL 이 없으므로 여전히 막는다(적재가 얕은 것과 다르다).
    blocked = _resolve({
        "operator": "held_throughout", "attribute_id": "member_state",
        "value": "dormant", "months": 3,
    })
    assert blocked["status"] == "unsupported"
    assert "적재되어 있지 않습니다" in blocked["message"]


def test_value_anchored_interval_operators_share_one_count() -> None:
    """보유/부재/전구간 유지는 **같은 카운트의 다른 임계**다 — 분기가 아니라 임계표로 갈린다."""
    ever = _resolve({"operator": "ever", "attribute_id": "member_grade",
                     "value": "gold_grade", "months": 6})
    never = _resolve({"operator": "never", "attribute_id": "member_grade",
                      "value": "gold_grade", "months": 6})
    held = _resolve({"operator": "held_throughout", "attribute_id": "member_grade",
                     "value": "gold_grade", "months": 3})

    for operation in (ever, never, held):
        assert operation["status"] == "resolved"
        assert operation["aggregate"] == "value_month_count"
        assert operation["value_predicate"]["values"] == ["MEM_GRADE_CD.GOLD"]
    assert ever["comparison"] == {"operator": "gte", "value": 1}
    assert never["comparison"] == {"operator": "eq", "value": 0}
    # 전구간 유지의 임계는 창 길이에서 파생한다(손 상수가 아니다).
    assert held["comparison"] == {"operator": "eq", "value": 3}


def test_value_anchor_survives_into_sql() -> None:
    """'ever VIP' 가 'ever ANY' 로 조용히 넓어지지 않는다 — 값이 SQL 에 남는지 본다."""
    operation = _resolve({"operator": "ever", "attribute_id": "member_grade",
                          "value": "vip", "months": 6})
    sql = ct.compile_sql(
        operation,
        member_table="CRM_MB_BASEINFO", member_alias="B", member_key="MEMBER_NO",
        member_select_columns=["B.MEMBER_NO"], member_predicates=[], segment_label="seg",
    )
    assert "SUM(CASE WHEN ATTRIBUTE_VALUE IN ('MEM_GRADE_CD.VIP') THEN 1 ELSE 0 END) >= 1" in sql
    # 미관측 월을 '아니었다'로 세지 않는다(never 가 이 함정에 가장 취약하다).
    assert "COUNT(DISTINCT SNAPSHOT_TIME) = 6" in sql
    for term in ct.validation_terms(operation):
        assert term in sql, f"검증 토큰이 SQL 에 없다: {term}"


def test_blocks_state_history_without_binding() -> None:
    blocked = _resolve({
        "operator": "transition", "attribute_id": "member_state",
        "from_value": "normal_member", "to_value": "dormant",
    })
    assert blocked["status"] == "unsupported"
    assert "적재되어 있지 않습니다" in blocked["message"]


# ── 컴파일: as-of/전이 스냅샷 SQL 형태 ───────────────────────────────────────────


def _compile(operation: dict) -> str:
    return ct.compile_sql(
        operation,
        member_table="CRM_MB_BASEINFO",
        member_alias="B",
        member_key="MEMBER_NO",
        member_select_columns=["B.MEMBER_NO AS CUST_ID"],
        member_predicates=["B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"],
        segment_label="grade_history",
    )


def test_compiles_as_of_latest_sql() -> None:
    operation = _resolve({"operator": "as_of_latest", "attribute_id": "member_grade", "value": "vip"})
    sql = _compile(operation)
    assert "INNER JOIN CRM_MB_MONTHCRMINFO S ON S.MEMBER_NO = B.MEMBER_NO" in sql
    assert "S.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)" in sql
    assert "S.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in sql


def test_compiles_as_of_month_sql() -> None:
    # 리졸버는 단일 스냅샷 적재에서 월 지정을 막으므로(가용성 게이트) 컴파일 형태는 연산을
    # 직접 구성해 검증한다 — 다월 적재 시 리졸버가 만들 연산과 같은 모양이다.
    operation = {
        "status": "resolved",
        "attribute": {"id": "member_grade", "label": "회원 등급(월별 스냅샷)"},
        "binding": {"table": "CRM_MB_MONTHCRMINFO", "entity_key": "MEMBER_NO",
                     "time_column": "YYYYMM", "value_column": "ZTS_GRADE",
                     "prev_value_column": "PREV_ZTS_GRADE"},
        "aggregate": "as_of",
        "anchor": {"type": "month", "month": "202512"},
        "value_predicate": {"values": ["MEM_GRADE_CD.VIP"]},
        "semantic_operator": "as_of_month",
    }
    sql = _compile(operation)
    assert "S.YYYYMM = '202512'" in sql


def test_compiles_transition_sql_with_prev_column() -> None:
    operation = _resolve({
        "operator": "transition", "attribute_id": "member_grade",
        "from_value": "gold_grade", "to_value": "vip",
    })
    sql = _compile(operation)
    assert "S.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in sql
    assert "S.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in sql


def test_transition_without_from_value_requires_change() -> None:
    operation = _resolve({
        "operator": "transition", "attribute_id": "member_grade", "to_value": "vip",
    })
    sql = _compile(operation)
    # 출발 값 미지정 전이('승급한')가 현재 값 필터로 조용히 축소되지 않는 최소 보증.
    assert "S.PREV_ZTS_GRADE <> S.ZTS_GRADE" in sql


# ── 백필 + 의무(가짜 성공 구조 차단) ───────────────────────────────────────────────


def test_resolver_turns_held_throughout_slot_into_operations() -> None:
    """'최근 3개월 내내 VIP 등급을 유지한' — 값 앵커 구간 판정으로 컴파일된다(2026-08-02 신설).

    적재가 1개월뿐이라 결과는 0건이지만, 그건 정직한 답이므로 고지를 달고 실행 IR 로 간다.
    """
    query = "최근 3개월 내내 VIP 등급을 유지한 회원을 찾아줘."
    plan = _plan_with_slot({
        "source_span": "최근 3개월 내내 VIP 등급을 유지한", "attribute": "member_grade",
        "relation": "held_throughout", "value": "VIP", "months": 3,
    }, query)
    assert ct.resolve_slot_to_operations(plan, _catalog()) == "resolved"
    operation = plan[ct.PLAN_OPERATIONS_KEY][0]
    assert operation["aggregate"] == "value_month_count"
    assert operation["comparison"] == {"operator": "eq", "value": 3}
    assert operation["advisories"][0]["code"] == "data_coverage_shallow"


def test_resolver_turns_transition_slot_into_operations() -> None:
    query = "최근 상태가 VIP이고 직전 상태는 골드였던 회원을 보여줘."
    plan = _plan_with_slot({
        "source_span": "직전 상태는 골드였던", "attribute": "member_grade",
        "relation": "transition", "from_value": "골드", "to_value": "VIP",
    }, query)
    assert ct.resolve_slot_to_operations(plan, _catalog()) == "resolved"
    assert plan[ct.PLAN_OPERATIONS_KEY][0]["status"] == "resolved"


def test_resolver_is_inert_without_a_compiled_slot() -> None:
    """원문에 이력 어구가 있어도 노드/슬롯이 없으면 아무 일도 하지 않는다 —
    이 모듈은 더 이상 원문을 읽지 않는다(예전 백필의 결정론 감지 경로 제거 확인)."""
    plan: dict = {"target_user": {}}
    assert ct.resolve_slot_to_operations(plan, _catalog()) is None
    assert ct.PLAN_OPERATIONS_KEY not in plan and ct.PLAN_IR_KEY not in plan


def test_time_qualifiers_are_captured_as_obligations() -> None:
    # 가짜 성공 회귀: '지난달 말 기준'·'내내 유지'가 requirement 로 기록되지 않으면
    # 현재 등급 SQL 로 조용히 축소돼도 아무 게이트가 못 잡는다.
    for query in (
        "지난달 말 기준 VIP였던 회원을 찾아줘.",
        "최근 3개월 내내 VIP 등급을 유지한 회원을 찾아줘.",
        "지난 6개월 동안 골드에서 VIP로 승급한 회원을 찾아줘.",
    ):
        kinds = [
            requirement.base.get("name")
            for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        ]
        assert "member_state_history" in kinds, query


def test_obligations_do_not_fire_on_current_state_queries() -> None:
    for query in (
        "휴면 회원을 추출해줘",
        "VIP 등급 회원에게 쿠폰 캠페인을 만들어줘",
        "최근 30일 구매한 회원 수를 알려줘.",
    ):
        kinds = [
            requirement.base.get("name")
            for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        ]
        assert "member_state_history" not in kinds, query


def test_discharge_issues_receipts_only_for_ledgered_obligations() -> None:
    query = "지난달 말 기준 VIP였던 회원을 찾아줘."
    captured = semantic_requirements.capture_source_semantic_obligations(query)
    plan: dict = {
        semantic_requirements.SOURCE_REQUIREMENTS_KEY: [r.to_dict() for r in captured],
    }
    discharged = semantic_requirements.discharge_source_semantic_obligations(
        plan, query, kinds={"member_state_history"},
        status="compiled", compiler="compositional_targeting",
    )
    assert discharged
    assert not semantic_requirements.unresolved_semantic_obligations(plan, query)
    # 원장 없는 plan 에서는 발급하지 않는다(fail-close 유지).
    ledgerless: dict = {}
    assert semantic_requirements.discharge_source_semantic_obligations(
        ledgerless, query, kinds={"member_state_history"},
        status="compiled", compiler="compositional_targeting",
    ) == []


def test_resolved_operation_needs_no_missing_field_sweep() -> None:
    """'지난달 말 기준 VIP' — 시점과 값을 노드가 소유하므로 결핍이 애초에 생기지 않는다.

    과거에는 LLM 이 이 조건을 latest_purchase_grade/current_date 결핍으로 보고했고
    `_drop_history_owned_missing_fields` 가 그것을 사후에 걷어냈다(실측 #13/#15/#19).
    이제 결핍은 노드 스키마에서 계산되고 RelationPredicate 는 attribute/relation/subject 를
    모두 가지므로 걷어낼 stale 이 존재하지 않는다 — sweep 함수도 함께 삭제됐다."""
    import member_attribute_history
    import semantic_pipeline

    query = "지난달 말 기준 VIP였던 회원을 찾아줘."
    plan = _plan_with_slot({
        "source_span": "지난달 말 기준 VIP였던", "attribute": "member_grade",
        "relation": "as_of", "value": "VIP",
    }, query)
    member_attribute_history.apply(plan, query, catalog_loader=_catalog)
    assert plan[ct.PLAN_OPERATIONS_KEY][0]["status"] == "resolved"

    node = semantic_plan.plan_from_dict(plan["semantic_plan"], source_query=query)
    assert node.missing_fields() == ()
    assert semantic_pipeline.project_semantic_ir(node)["status"] == "resolved"
    assert not hasattr(member_attribute_history, "_drop_history_owned_missing_fields")


def _transition_plan(query: str, exclude_lifecycle: list[str]) -> dict:
    span = "직전 상태는 골드였던"
    plan = _plan_with_slot({
        "source_span": span, "attribute": "member_grade",
        "relation": "transition", "from_value": "골드", "to_value": "VIP",
    }, query)
    plan["exclude"] = {"lifecycle": list(exclude_lifecycle)}
    # 오방출된 값의 V4 근거 스팬 — 회수 판정은 이 스팬이 전이 노드 구간 안에 있는가로 한다.
    start = query.index(span)
    plan["semantic_evidence"] = [{
        "path": "exclude.lifecycle", "text": "골드",
        "start": query.index("골드", start), "end": query.index("골드", start) + 2,
        "confidence": 0.9,
    }]
    return plan


def test_transition_reclaims_misrouted_exclude_lifecycle() -> None:
    """'직전 상태는 골드'를 exclude.lifecycle=[gold_grade](골드 제외!)로 오방출한 실사고(#19).

    회수 판정은 원문 '제외/빼' 검사가 아니라 **근거 스팬 겹침**으로 한다 — 그 값의 근거가
    전이 노드의 구간 안에 있으면 전이 소유다."""
    import member_attribute_history

    query = "최근 상태가 VIP이고 직전 상태는 골드였던 회원을 보여줘."
    plan = _transition_plan(query, ["gold_grade"])
    member_attribute_history.apply(plan, query, catalog_loader=_catalog)
    assert plan[ct.PLAN_OPERATIONS_KEY][0]["aggregate"] == "transition"
    assert plan["exclude"]["lifecycle"] == []


def test_transition_leaves_unrelated_exclusions_alone() -> None:
    import member_attribute_history

    query = "최근 상태가 VIP이고 직전 상태는 골드였던 회원을 보여줘."
    plan = _transition_plan(query, ["gold_grade", "withdrawn"])
    member_attribute_history.apply(plan, query, catalog_loader=_catalog)
    # 전이 소유 밖 값(탈퇴)이 섞여 있으면 회수하지 않는다(fail-close).
    assert plan["exclude"]["lifecycle"] == ["gold_grade", "withdrawn"]


def test_transition_claim_yields_without_evidence_spans() -> None:
    """근거가 없으면 회수하지 않는다(fail-open) — 오디언스 반전 위험이 회수 실패보다 크다."""
    import member_attribute_history

    query = "최근 상태가 VIP이고 직전 상태는 골드였던 회원을 보여줘."
    plan = _transition_plan(query, ["gold_grade"])
    plan.pop("semantic_evidence")
    member_attribute_history.apply(plan, query, catalog_loader=_catalog)
    assert plan["exclude"]["lifecycle"] == ["gold_grade"]


def test_row_ownership_is_decided_by_source_span_overlap() -> None:
    """미해결 행의 귀속은 자유 문장 토큰이 아니라 근거 스팬 겹침으로 판정한다.

    과거에는 '기준월/직전/승급/VIP…' 토큰 정규식으로 추정했고, 런마다 다른 문장이 나와
    낱말 목록이 계속 늘었다. 노드가 자기 근거 구간을 갖게 된 뒤로는 겹침 하나면 충분하다."""
    import member_attribute_history

    query = "지난달 말 기준 VIP였던 회원을 찾아줘."
    span = "지난달 말 기준 VIP였던"
    plan = _plan_with_slot({
        "source_span": span, "attribute": "member_grade", "relation": "as_of", "value": "VIP",
    }, query)
    member_attribute_history.apply(plan, query, catalog_loader=_catalog)

    start = query.index(span)
    inside = {"condition": span, "reason": "명시된 타겟 조건이 없음", "source": "llm_semantic_ir",
              "source_span": {"start": start, "end": start + len(span)}}
    assert member_attribute_history.row_owned_by_compiled_operation(inside, plan)
    outside = {"condition": "회원을 찾아줘", "reason": "확정 실패", "source": "llm_semantic_ir",
               "source_span": {"start": start + len(span), "end": len(query)}}
    assert not member_attribute_history.row_owned_by_compiled_operation(outside, plan)
    # 근거 구간이 없는 행은 귀속을 주장하지 않는다(fail-close).
    assert not member_attribute_history.row_owned_by_compiled_operation(
        {"condition": span, "source": "llm_semantic_ir"}, plan
    )


# ── 적대적 리뷰 회귀(2026-08-02): 과발화·오귀속·반전 시나리오 ─────────────────────────


def test_obligations_do_not_fire_on_purchase_negation_with_state_context() -> None:
    # '한 번도/한 번이라도'가 구매 절 표지인데 절에 상태 낱말이 있다고 의무를 봉인하면
    # 지원되는 재활성 상용구가 하드 차단된다(리뷰 실증).
    for query in (
        "한 번도 구매하지 않은 휴면 회원을 추출해줘",
        "한번도 구매하지 않은 휴면 회원",
        "한 번이라도 구매한 실버 고객을 찾아줘",
    ):
        kinds = [
            requirement.base.get("name")
            for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        ]
        assert "member_state_history" not in kinds, query


def test_no_source_text_parser_remains_in_the_history_path() -> None:
    """감지기 문맥 규칙(앵커 인접·제외 문맥 스킵)은 원문 정규식과 함께 삭제됐다.

    그 규칙들은 '원문 어디의 값이 이 연산의 것인가'를 정규식으로 추정하기 위한 보정이었고,
    노드가 근거 구간을 직접 갖는 구조에서는 추정할 것이 없다. 이 테스트는 그 파서가
    되살아나지 않음을 고정한다(이름만 바꾼 부활 포함)."""
    import member_attribute_history

    assert not hasattr(ct, "detect_member_attribute_history")
    assert not hasattr(ct, "apply_member_attribute_history_backfill")
    assert not hasattr(member_attribute_history, "_drop_history_owned_missing_fields")

    # 원문에 이력 어구가 가득해도 슬롯이 없으면 슬롯도 결핍 보고도 만들지 않는다.
    plan: dict = {
        "target_user": {},
        "semantic_ir": {"status": "needs_clarification", "missing_fields": ["latest_purchase_grade"]},
    }
    member_attribute_history.apply(
        plan,
        "정상 회원이었다가 휴면 상태로 변경된 회원은 제외하고 지난달 말 기준 VIP였던 회원을 찾아줘.",
        catalog_loader=_catalog,
    )
    assert plan["target_user"] == {}, "원문 감지로 슬롯을 만드는 경로가 되살아났다."
    assert ct.PLAN_OPERATIONS_KEY not in plan and ct.PLAN_IR_KEY not in plan
    # 결핍 보고도 손대지 않는다 — 이 모듈은 더 이상 missing_fields 의 소유자가 아니다.
    assert plan["semantic_ir"]["missing_fields"] == ["latest_purchase_grade"]


def test_as_of_month_advises_on_single_snapshot() -> None:
    """지정 월 조회는 `YYYYMM = '지정월'` 로 의미가 정확하다 — 미적재면 0건일 뿐이다.

    문제였던 것은 **조용한** 빈 오디언스지 빈 결과 자체가 아니다. 그래서 막는 대신 고지한다.
    """
    resolved = _resolve({
        "operator": "as_of_month", "attribute_id": "member_grade",
        "value": "vip", "month": "202512",
    })
    assert resolved["status"] == "resolved"
    assert resolved["anchor"] == {"type": "month", "month": "202512"}
    advisory = resolved["advisories"][0]
    assert advisory["code"] == "data_coverage_shallow"
    assert advisory["requested_month"] == "202512" and "0건" in advisory["message"]


def test_discharge_skipped_when_history_conditions_span_multiple_clauses() -> None:
    import member_attribute_history

    query = "골드에서 VIP로 승급한 회원, 그리고 지난달 말 기준 실버였던 회원"
    captured = semantic_requirements.capture_source_semantic_obligations(query)
    plan: dict = {
        "target_user": {},
        semantic_requirements.SOURCE_REQUIREMENTS_KEY: [r.to_dict() for r in captured],
    }
    member_attribute_history.apply(plan, query, catalog_loader=_catalog)
    # 연산은 하나만 컴파일되므로 영수증을 발급하지 않는다 — 둘째 조건이 조용히 드롭되지 않게.
    assert semantic_requirements.unresolved_semantic_obligations(plan, query)


# ── LLM 슬롯 coerce: 어휘 이탈은 drop, canonical 정규화 ─────────────────────────────


def test_slot_coerce_normalizes_and_blocks_hallucination() -> None:
    allowed = graph_rag._llm_slot_allowed()["history_attributes"]
    shape = targeting_ir.SLOT_SHAPES["relational_operation"]
    coerced = shape.coerce(
        {"operator": "transition", "attribute_id": "member_grade",
         "from_value": "골드", "to_value": "VIP"},
        allowed=allowed,
    )
    assert coerced == {
        "operator": "transition", "attribute_id": "member_grade",
        "from_value": "gold_grade", "to_value": "vip",
    }
    assert shape.coerce(
        {"operator": "transition", "attribute_id": "member_grade", "to_value": "다이아몬드"},
        allowed=allowed,
    ) is None
    assert shape.coerce(
        {"operator": "teleport", "attribute_id": "member_grade", "value": "vip"},
        allowed=allowed,
    ) is None
