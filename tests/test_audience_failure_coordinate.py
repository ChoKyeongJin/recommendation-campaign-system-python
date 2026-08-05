"""종착 상태 아홉 갈래가 **하나의 좌표**로 읽히는가 — ``audience_failure.diagnose``.

이 파일이 지키는 것 넷.

1. **죽은 선언이 없다.** ``LANE_STAGES`` 의 모든 행이 실제 실행에서 산출된다. 선언만 있고
   아무도 도달하지 못하는 레인은 진단표가 아니라 장식이다.
2. **사유가 게이트를 지목하면 잔여물이 덮지 않는다.** 앞 단계가 남긴 좌표가 실제 종착 게이트를
   가리는 순간, 이 좌표는 "어디서 막혔나"에 옛 답을 준다.
3. **``code`` 는 한국어 문장이 아니다.** 같은 함정이 Event IR 좌표에서 이미 한 번 났다 —
   ``code`` 가 없으면 ``plan_validation._marker`` 가 한국어 ``reason`` 을 코드로 승격했다.
4. **실패에는 반드시 좌표가 있다.** 어디에도 안 걸리면 ``unclassified`` + ``sources`` 로 보고한다.
   조용한 ``None`` 은 "실패를 관측하지 못했다"를 "실패가 없었다"처럼 보이게 한다.

좌표는 fixture 위가 아니라 **실행**에서 나와야 하므로, 가능한 갈래는 전부 실제 차단 결과
생산자(``graph_rag._*_blocking_sql_result`` · 빌더)를 태워서 만든다.
"""

from __future__ import annotations

import audience_authority
import audience_failure
import failure_messages
import graph_rag
import plan_validation
import query_pipeline

# 파싱은 되지만 컴파일 단계에서 터뜨릴 저장 표현(지난달 구매).
_WIRE = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {"type": "interval", "start": "2026-07-01", "end_exclusive": "2026-08-01"},
        },
    },
    "evidence": {"text": "지난달 구매", "start": 0, "end": 6},
}


def _plan(**overrides) -> dict:
    base = {
        "intent": "find_user_segment",
        "original_query": "지난달 구매한 회원",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
    }
    base.update(overrides)
    return base


def _event_ir_plan() -> dict:
    """빌더를 실제로 태워 Event IR 좌표를 남긴 플랜."""
    plan = _plan(
        event_expression={"expression": _WIRE, "source": "audience_requirement", "receipts": []}
    )

    def explode(payload, **kwargs):
        raise query_pipeline.QueryPipelineError("sql_compilation", "표현할 수 없습니다")

    original = graph_rag.query_pipeline.compile_audience_predicate
    try:
        graph_rag.query_pipeline.compile_audience_predicate = explode  # type: ignore[assignment]
        assert graph_rag.build_event_expression_sql_candidate(plan) is None
    finally:
        graph_rag.query_pipeline.compile_audience_predicate = original  # type: ignore[assignment]
    return plan


def _semantic_plan() -> tuple[dict, dict]:
    plan = _plan(
        semantic_ir={
            "status": "needs_clarification",
            "missing_fields": ["req-1.member_entity"],
            "message": "비교할 대상을 지정해 주세요.",
        }
    )
    result = graph_rag._semantic_ir_blocking_sql_result(plan)
    assert result is not None
    return plan, result


def _unresolved_plan() -> tuple[dict, dict]:
    items = [{
        "id": "usr_1",
        "path": "target_user.purchase_object",
        "label": "구매 상품",
        "reason": "'알로루' 를 실행 가능한 상품 조건으로 확정하지 못했습니다.",
        "status": "unresolved",
        "source": "llm_semantic_ir",
    }]
    return _plan(unresolved_source_conditions=items), graph_rag._unresolved_source_blocking_sql_result(items)


def _plan_validation_pair() -> tuple[dict, dict]:
    """검증이 실제로 막는 플랜(권위는 event_ir 인데 canonical 표현이 없다)."""
    plan = _plan(audience_requirement={"expression": None, "issues": []})
    validation = plan_validation.validate_executable_plan(plan)
    assert validation.status != plan_validation.EXECUTABLE
    return plan, graph_rag._plan_validation_blocking_sql_result(validation, plan)


# (레인, 플랜, sql_result) — 아래 세 테스트가 같은 표를 읽는다.
def _scenarios() -> list[tuple[str, dict, dict]]:
    authority_plan = _plan(**{audience_authority.PLAN_AUTHORITY_KEY: "event-ir"})
    authority_result = graph_rag._audience_authority_blocking_sql_result(authority_plan)
    assert authority_result is not None

    semantic_plan_, semantic_result = _semantic_plan()
    unresolved_plan, unresolved_result = _unresolved_plan()
    validation_plan, validation_result = _plan_validation_pair()

    event_plan = _event_ir_plan()
    # 빌더가 좌표를 남기고 후보가 0개가 된 상태 = canonical 레인의 실제 종착 모습.
    event_result = {"sql": None, "is_success": False, "failure_reason": "no_sql_candidates"}

    capability_plan = _plan(
        unsupported={
            "reason": "event_compiler_unsupported",
            "message": "이벤트 의미는 일관되지만 일부 조건을 현재 SQL 컴파일러가 지원하지 않습니다.",
            "clarification": "조건을 나눠 입력해 주세요.",
        }
    )
    capability_result = {
        "sql": None, "is_success": False, "failure_reason": "event_compiler_unsupported",
    }

    structuring_plan = _plan(
        parser={
            "type": "rules",
            "requested": "llm",
            "fallback_used": True,
            "fallback_reason": "llm_semantic_structuring_unavailable",
        }
    )
    structuring_result = {"sql": None, "is_success": False, "failure_reason": "no_sql_candidates"}

    return [
        ("audience_authority", authority_plan, authority_result),
        ("semantic_resolution", semantic_plan_, semantic_result),
        ("source_coverage", unresolved_plan, unresolved_result),
        ("plan_validation", validation_plan, validation_result),
        ("event_ir_compile", event_plan, event_result),
        ("execution_capability", capability_plan, capability_result),
        ("structuring", structuring_plan, structuring_result),
        (
            "sql_generation",
            _plan(),
            {"sql": None, "is_success": False, "failure_reason": "sql_guard_failed"},
        ),
        (
            audience_failure.UNCLASSIFIED,
            _plan(),
            {"sql": None, "is_success": False, "failure_reason": None},
        ),
    ]


def test_success_yields_no_coordinate() -> None:
    """성공에 좌표를 붙이면 소비자가 '실패했나?'를 다시 판정하게 된다."""
    assert audience_failure.diagnose(_plan(), {"sql": "SELECT 1", "is_success": True}) is None


def test_every_declared_lane_is_reachable() -> None:
    """선언만 있고 아무도 도달 못 하는 레인은 진단표가 아니라 장식이다."""
    produced = {lane for lane, _, _ in _scenarios()}
    declared = {row["stage"] for row in audience_failure.LANE_STAGES}
    assert declared - produced == set(), f"도달 불가로 선언된 레인: {sorted(declared - produced)}"
    assert produced - declared == set(), f"선언표에 없는 레인이 나왔다: {sorted(produced - declared)}"


def test_each_scenario_lands_on_its_lane() -> None:
    for lane, plan, result in _scenarios():
        coordinate = audience_failure.diagnose(plan, result)
        assert coordinate is not None, f"{lane}: 실패인데 좌표가 없다"
        assert coordinate["stage"] == lane, f"{lane} 이어야 하는데 {coordinate['stage']}"


def test_coordinate_shape_is_one_shape() -> None:
    """모양이 두 벌이면 '어디서 막혔는가'가 경로마다 다른 답을 준다."""
    expected = {"stage", "stage_label", "stage_detail", "code", "evidence", "message", "sources"}
    evidence_keys = {"path", "code", "detail"}
    for lane, plan, result in _scenarios():
        coordinate = audience_failure.diagnose(plan, result)
        assert set(coordinate) == expected, f"{lane}: {sorted(set(coordinate) ^ expected)}"
        for item in coordinate["evidence"]:
            assert set(item) == evidence_keys, f"{lane}: 근거 항목 모양이 다르다 {sorted(item)}"


def test_code_is_never_a_korean_sentence() -> None:
    """한국어는 message 로만 나간다 — code 는 닫힌 코드다(좌표에서 이미 한 번 난 함정)."""
    for lane, plan, result in _scenarios():
        code = audience_failure.diagnose(plan, result)["code"]
        assert code and not any("가" <= char <= "힣" for char in code), f"{lane}: {code!r}"
        assert " " not in code, f"{lane}: 코드에 공백이 있다 {code!r}"


def test_unclassified_still_reports_where_to_look() -> None:
    """조용한 None 금지 — 흔적이 없다는 사실도 좌표다."""
    plan = _plan(
        semantic_ir={"status": "executable"},
        parser={"type": "llm", "fallback_used": False},
    )
    coordinate = audience_failure.diagnose(plan, {"sql": None, "is_success": False})
    assert coordinate["code"] == audience_failure.UNCLASSIFIED
    # 값이 있던 입력만 담는다 — 미분류일 때 운영자가 열어 볼 자리의 목록이다.
    assert coordinate["sources"] == ["query_plan.semantic_ir"]


def test_named_gate_wins_over_leftover_coordinates() -> None:
    """앞 단계 잔여물이 실제 종착 게이트를 덮으면 좌표가 이미 지나간 자리를 가리킨다."""
    plan = _event_ir_plan()  # Event IR 좌표가 남아 있다
    plan["semantic_ir"] = {
        "status": "needs_clarification", "missing_fields": [], "message": "확인이 필요합니다.",
    }
    blocked = graph_rag._semantic_ir_blocking_sql_result(plan)
    assert blocked is not None

    coordinate = audience_failure.diagnose(plan, blocked)
    assert coordinate["stage"] == "semantic_resolution"
    assert coordinate["code"] == blocked["failure_reason"]


def test_leftover_coordinate_refines_a_coarse_reason() -> None:
    """반대 방향 — 거친 사유(no_sql_candidates)는 잔여 좌표가 정밀화해야 한다."""
    plan = _event_ir_plan()
    coordinate = audience_failure.diagnose(
        plan, {"sql": None, "is_success": False, "failure_reason": "no_sql_candidates"}
    )
    assert coordinate["stage"] == "event_ir_compile"
    assert coordinate["code"] == "event_ir_compile_failed"
    # 빌더가 붙인 파이프라인 단계가 하위 좌표로 승계된다.
    assert coordinate["stage_detail"] == "sql_compilation"


def test_parser_fallback_never_masks_a_real_sql_failure() -> None:
    """폴백은 실패가 아니다(rules 로 떨어지고도 SQL 은 나온다) — 조건이 있었던 실패를 덮으면 안 된다."""
    plan = _plan(
        parser={"type": "rules", "fallback_used": True, "fallback_reason": "llm_query_parser_unavailable"}
    )
    coordinate = audience_failure.diagnose(
        plan, {"sql": None, "is_success": False, "failure_reason": "sql_guard_failed"}
    )
    assert coordinate["stage"] == "sql_generation"


def test_literal_keys_match_their_owning_modules() -> None:
    """순수성 대가로 리터럴을 들고 있다 — 드리프트는 이 테스트가 잡는다."""
    assert audience_failure.PLAN_AUTHORITY_KEY == audience_authority.PLAN_AUTHORITY_KEY
    # 의미 게이트가 실제로 내는 사유 3종이 전부 semantic_resolution 으로 간다.
    for kind, expected in (
        ("structurer", "semantic_structurer_failure"),
        ("system", "semantic_registry_gap"),
        (None, "semantic_ir_unsupported"),
    ):
        import semantic_outcome

        failure_kind = {
            "structurer": semantic_outcome.FAILURE_KIND_STRUCTURER,
            "system": semantic_outcome.FAILURE_KIND_SYSTEM,
            None: None,
        }[kind]
        reason = failure_messages.semantic_failure_reason("unsupported", failure_kind)
        assert reason == expected
        coordinate = audience_failure.diagnose(
            _plan(semantic_ir={"status": "unsupported"}),
            {"sql": None, "is_success": False, "failure_reason": reason},
        )
        assert coordinate["stage"] == "semantic_resolution", reason


def test_condition_ir_gates_share_the_plan_validation_lane() -> None:
    """접두어가 없어도 같은 레인이다 — 빌더 전에 조건 IR 결함으로 막는 게이트 둘.

    sql_generation 으로 흘리면 "조건은 다 갔고 관문에서 떨어졌다"가 되어 고칠 곳을 잘못 가리킨다.
    """
    conflict = graph_rag._semantic_conflict_sql_result(
        [{"code": "include_exclude_overlap", "path": "target_user.grade", "message": "조건이 서로 충돌합니다."}]
    )
    dimension = graph_rag._invalid_dimension_filters_sql_result(
        [{"code": "unknown_dimension", "path": "dimension_filters[0]"}]
    )
    for result in (conflict, dimension):
        coordinate = audience_failure.diagnose(_plan(), result)
        assert coordinate["stage"] == "plan_validation", result["failure_reason"]
        # 근거 자리가 둘(plan_validation.issues / validation_errors)이어도 모양은 하나다.
        assert coordinate["evidence"][0]["code"], result["failure_reason"]


def test_plan_validation_prefix_matches_the_producer() -> None:
    """``plan_validation_`` 접두어는 graph_rag 가 조립한다 — 접두어가 갈리면 레인이 사라진다."""
    _, result = _plan_validation_pair()
    assert result["failure_reason"].startswith("plan_validation_")
    coordinate = audience_failure.diagnose(_plan(), result)
    assert coordinate["stage"] == "plan_validation"
    assert coordinate["stage_detail"] in set(plan_validation.PlanValidationStatus.__args__)  # type: ignore[attr-defined]


def test_no_declaration_in_the_module_is_dead() -> None:
    """이 모듈의 논지가 "죽은 선언 금지"다 — 자기 자신에게 먼저 적용한다.

    실측: 구조를 뒤집으면서 `_BLOCKING_SEMANTIC_STATUSES` 가 아무도 안 읽는 채로 남았다.
    죽은 상수는 다음 사람에게 "이 판정이 여기 있다"고 거짓말한다.
    """
    import ast
    from pathlib import Path

    source = (Path(audience_failure.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    # `__all__` 은 반출 선언이라 모듈 안에서 읽히지 않는 것이 정상이다.
    dunders = {name for name in declared if name.startswith("__") and name.endswith("__")}
    dead = sorted(declared - used - dunders)
    assert not dead, "아무도 읽지 않는 모듈 상수:\n  " + "\n  ".join(dead)


def test_every_lane_declares_why_it_is_not_merged() -> None:
    """이유 없는 레인은 다음 사람이 합친다 — 선언표가 그 이유를 데이터로 든다."""
    for row in audience_failure.LANE_STAGES:
        assert set(row) == {"stage", "label", "reads", "distinct_because"}
        assert len(row["distinct_because"]) > 30, row["stage"]
        assert row["label"], row["stage"]
