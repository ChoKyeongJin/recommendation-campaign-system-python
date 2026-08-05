"""Graph grounding may repair only the Canonical Event IR execution path."""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import audience_runtime
import audience_authority
import canonical_event_ir_grounding
import canonical_audience_claims
import event_ir
import graph_rag
from query_structurer.campaign_plan_v4 import (
    CampaignQueryPlanV4,
    attach_campaign_query_plan_v4_identity,
)
from query_structurer.semantic_ir import empty_semantic_ir
from query_structurer.semantic_ir import extract_literal_bindings


CURRENT_DATE = "2026-08-04"
EXACT_QUERY = (
    "2026년 8월 3일 기준 정상 회원 중 서울 또는 경기 거주, 30~44세 여성, "
    "VIP 또는 GOLD 등급이고 최근 30일 안에 로그인했으며 "
    "마케팅 활용·SMS·앱푸시에 모두 동의한 회원을 추출해줘. "
    "블랙리스트, 휴면, 사이트 전용회원, 임직원은 제외하고 "
    "회원번호는 중복 없이 보여줘."
)


def _evidence(query: str, text: str) -> event_ir.Evidence:
    start = query.index(text)
    return event_ir.Evidence(text=text, start=start, end=start + len(text))


def _comparison(
    query: str, field: str, operator: str, value: Any, evidence: str
) -> event_ir.Comparison:
    return event_ir.Comparison(
        operator,
        event_ir.FieldRef(field),
        event_ir.Literal(value),
        _evidence(query, evidence),
    )


def _exact_expression() -> event_ir.Condition:
    consent = "마케팅 활용·SMS·앱푸시에 모두 동의한"
    exclusions = "블랙리스트, 휴면, 사이트 전용회원, 임직원은 제외"
    return event_ir.And(
        (
            _comparison(
                EXACT_QUERY,
                "subject.member_state",
                "=",
                "normal_member",
                "정상 회원",
            ),
            event_ir.Or(
                (
                    _comparison(
                        EXACT_QUERY,
                        "subject.residence_sido",
                        "=",
                        "sido_11",
                        "서울",
                    ),
                    _comparison(
                        EXACT_QUERY,
                        "subject.residence_sido",
                        "=",
                        "sido_41",
                        "경기",
                    ),
                )
            ),
            _comparison(EXACT_QUERY, "subject.age", ">=", 30, "30~44세"),
            _comparison(EXACT_QUERY, "subject.age", "<=", 44, "30~44세"),
            _comparison(EXACT_QUERY, "subject.gender", "=", "female", "여성"),
            event_ir.Or(
                (
                    _comparison(
                        EXACT_QUERY, "subject.grade", "=", "vip", "VIP"
                    ),
                    _comparison(
                        EXACT_QUERY,
                        "subject.grade",
                        "=",
                        "gold_grade",
                        "GOLD",
                    ),
                )
            ),
            event_ir.Exists(
                event_ir.Filter(
                    event_ir.Source("login"),
                    event_ir.TimeFilter(
                        event_ir.FieldRef("login.occurred_at"),
                        event_ir.RollingWindow(30, "day"),
                    ),
                ),
                _evidence(EXACT_QUERY, "최근 30일 안에 로그인"),
            ),
            _comparison(
                EXACT_QUERY,
                "subject.marketing_consent",
                "=",
                "agreed",
                consent,
            ),
            _comparison(
                EXACT_QUERY, "subject.sms_consent", "=", "agreed", consent
            ),
            _comparison(
                EXACT_QUERY,
                "subject.app_push_consent",
                "=",
                "agreed",
                consent,
            ),
            event_ir.Not(
                _comparison(
                    EXACT_QUERY,
                    "subject.blacklisted",
                    "=",
                    "blacklisted",
                    exclusions,
                )
            ),
            event_ir.Not(
                _comparison(
                    EXACT_QUERY,
                    "subject.member_state",
                    "=",
                    "dormant",
                    exclusions,
                )
            ),
            event_ir.Not(
                _comparison(
                    EXACT_QUERY,
                    "subject.site_only",
                    "=",
                    "site_only",
                    exclusions,
                )
            ),
            event_ir.Not(
                _comparison(
                    EXACT_QUERY,
                    "subject.employee",
                    "=",
                    "employee",
                    exclusions,
                )
            ),
        )
    )


def _structured_plan(query: str, expression: event_ir.Condition) -> CampaignQueryPlanV4:
    return CampaignQueryPlanV4(
        attach_campaign_query_plan_v4_identity(
            {
                "intent": "find_user_segment",
                "campaign_constraints": {
                    "objective": None,
                    "offer_type": None,
                    "channels": [],
                    "sell_object": None,
                },
                "result_limit": None,
                "audience_requirement": {
                    "expression": expression.to_dict(),
                    "issues": [],
                },
                "semantic_plan": {"nodes": []},
            },
            query,
            current_date=CURRENT_DATE,
        )
    )


def _registry_gap_from(success: CampaignQueryPlanV4) -> CampaignQueryPlanV4:
    gap = CampaignQueryPlanV4(copy.deepcopy(success))
    gap.pop("event_expression", None)
    gap.pop("audience_authority", None)
    text = "여성"
    start = str(gap["original_query"]).index(text)
    gap["audience_requirement"] = {
        "expression": None,
        "issues": [
            {
                "code": "unsupported_semantics",
                "argument": "subject.gender",
                "message": "표현할 수 없습니다.",
                "evidence": {"text": text, "start": start, "end": start + len(text)},
            }
        ],
    }
    gap["semantic_ir"] = empty_semantic_ir(
        status="needs_clarification",
        missing_fields=["audience.requirement"],
        message="canonical registry gap",
        failure_kind="system_failure",
    )
    gap["audience_execution_assets"] = [
        {
            "argument": "subject.gender",
            "evidence": text,
            "assets": [{"symbol": "gender"}],
        }
    ]
    return gap


def test_graph_projection_contains_only_query_backed_canonical_ids() -> None:
    query = EXACT_QUERY
    node = {
        "id": "schema_column:CRM_MB_BASEINFO.SMS_YN",
        "type": "schema_table",
        "table_name": "CRM_MB_BASEINFO",
        "text_for_embedding": (
            "CRM_MB_BASEINFO MEMBER_STATE_CD SIDO AGE GENDER_CD EMART_GRADE_CD "
            "LAST_LOGIN_DATE AGREE_YN SMS_YN APP_PUSH_YN EMAIL_YN BLACKLIST_YN "
            "SITE_MEMBER_YN EMPLOYEE_YN; SELECT * FROM SECRET_TABLE"
        ),
    }
    catalog = audience_runtime.resolve_audience_catalog()
    projection = canonical_event_ir_grounding.project_canonical_event_ir_grounding(
        query,
        [node],
        audience_runtime.catalog_snapshot(),
        allowed_fields=catalog.compiler_fields,
        allowed_sources=catalog.compiler_events,
    )

    assert projection["canonical_sources"] == ["login"]
    assert set(projection["canonical_fields"]) == {
        "subject.age",
        "subject.app_push_consent",
        "subject.blacklisted",
        "subject.employee",
        "subject.gender",
        "subject.grade",
        "subject.marketing_consent",
        "subject.member_state",
        "subject.residence_sido",
        "subject.site_only",
        "subject.sms_consent",
    }
    assert "subject.email_consent" not in projection["canonical_fields"]
    assert projection["canonical_values"]["subject.residence_sido"] == [
        "sido_11",
        "sido_41",
    ]
    instruction = canonical_event_ir_grounding.render_canonical_event_ir_grounding_instruction(
        projection
    )
    assert instruction is not None
    assert "CRM_MB_BASEINFO" not in instruction
    assert "SMS_YN" not in instruction
    assert "SECRET_TABLE" not in instruction
    assert node["id"] not in instruction
    assert "target_user" in instruction  # It appears only in an explicit prohibition.


def test_grounding_retry_is_limited_to_the_registry_gap_state() -> None:
    success = _structured_plan(
        "여성 회원을 찾아줘",
        _comparison(
            "여성 회원을 찾아줘", "subject.gender", "=", "female", "여성 회원"
        ),
    )
    gap = _registry_gap_from(success)

    assert canonical_event_ir_grounding.is_registry_gap_repair_candidate(gap)
    user_missing = copy.deepcopy(gap)
    user_missing["semantic_ir"]["failure_kind"] = "user_clarification"
    assert not canonical_event_ir_grounding.is_registry_gap_repair_candidate(user_missing)
    unsupported = copy.deepcopy(gap)
    unsupported["semantic_ir"]["status"] = "unsupported"
    assert not canonical_event_ir_grounding.is_registry_gap_repair_candidate(unsupported)
    partial = copy.deepcopy(gap)
    partial["audience_requirement"]["issues"].append(
        {
            "code": "missing_argument",
            "argument": "period",
            "message": "기간이 필요합니다.",
            "evidence": {"text": "여성", "start": 0, "end": 2},
        }
    )
    assert not canonical_event_ir_grounding.is_registry_gap_repair_candidate(partial)
    legacy_surface = copy.deepcopy(gap)
    legacy_surface["target_user"] = {"gender": "female"}
    assert not canonical_event_ir_grounding.is_registry_gap_repair_candidate(
        legacy_surface
    )


def test_graph_grounding_retries_same_structurer_once_and_adopts_whole_plan(
    monkeypatch: Any,
) -> None:
    query = "여성 회원을 찾아줘"
    success = _structured_plan(
        query, _comparison(query, "subject.gender", "=", "female", "여성 회원")
    )
    gap = _registry_gap_from(success)
    node = {
        "id": "schema_table:CRM_MB_BASEINFO",
        "table_name": "CRM_MB_BASEINFO",
        "text_for_embedding": "CRM_MB_BASEINFO GENDER_CD",
    }
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(graph_rag, "vector_search", lambda **_kwargs: [])
    monkeypatch.setattr(graph_rag, "keyword_search", lambda **_kwargs: [])
    monkeypatch.setattr(graph_rag, "merge_hits", lambda hits: hits)
    monkeypatch.setattr(graph_rag, "expand_context", lambda **_kwargs: [node])
    monkeypatch.setattr(graph_rag, "_write_rag_llm_log", lambda *_args, **_kwargs: None)

    def structure(*_args: Any, **kwargs: Any) -> CampaignQueryPlanV4:
        calls.append(kwargs)
        return success

    monkeypatch.setattr(graph_rag, "_structure_campaign_query_plan_v4", structure)
    repaired = graph_rag._grounded_canonical_event_ir_repair(
        gap,
        query=query,
        context=graph_rag.StructuringContext(current_date=CURRENT_DATE),
        graph=graph_rag.nx.Graph(),
        collection="test",
        url="http://unused",
        api_key=None,
        embedding_model_name="unused",
        vector_top_k=2,
        keyword_top_k=2,
        graph_top_k=2,
        hops=0,
        llm_model="test-model",
        query_structurer=None,
    )

    assert len(calls) == 1
    assert "canonical_fields" in calls[0]["extra_instruction"]
    assert "GENDER_CD" not in calls[0]["extra_instruction"]
    assert repaired == success
    assert repaired["audience_authority"] == "event_ir"

    hybrid = copy.deepcopy(success)
    hybrid["target_user"] = {"gender": "female"}
    rejected, reason = graph_rag._admit_grounded_canonical_event_ir_repair(
        gap,
        hybrid,
        projection={
            "canonical_fields": ["subject.gender"],
            "canonical_sources": [],
            "canonical_values": {"subject.gender": ["female"]},
        },
        query=query,
        current_date=CURRENT_DATE,
    )
    assert rejected is None
    assert reason == "second_audience_language_populated"

    injected = copy.deepcopy(success)
    injected_expression = event_ir.And(
        (
            event_ir.condition_from_dict(
                injected["audience_requirement"]["expression"]
            ),
            _comparison(
                query,
                "subject.email_consent",
                "=",
                "agreed",
                "여성 회원",
            ),
        )
    ).to_dict()
    injected["audience_requirement"]["expression"] = injected_expression
    injected["event_expression"]["expression"] = copy.deepcopy(injected_expression)
    rejected, reason = graph_rag._admit_grounded_canonical_event_ir_repair(
        gap,
        injected,
        projection={
            "canonical_fields": ["subject.gender"],
            "canonical_sources": [],
            "canonical_values": {"subject.gender": ["female"]},
        },
        query=query,
        current_date=CURRENT_DATE,
    )
    assert rejected is None
    assert reason == "event_ir_field_outside_graph_projection"


def test_exact_query_pins_time_and_compiles_only_event_ir(monkeypatch: Any) -> None:
    bindings = extract_literal_bindings(EXACT_QUERY, current_date=CURRENT_DATE)
    assert [
        (binding["id"], binding["kind"], binding["start"], binding["end"])
        for binding in bindings
    ] == [
        ("as_of_date_1", "as_of_date", 0, 11),
        ("number_1", "number", 36, 38),
        ("number_2", "number", 39, 41),
        ("duration_1", "duration", 67, 70),
    ]
    structured = _structured_plan(EXACT_QUERY, _exact_expression())
    expression = event_ir.condition_from_dict(
        structured["event_expression"]["expression"]
    )
    windows = event_ir.time_windows(expression)

    assert structured["semantic_ir"]["status"] == "resolved"
    assert structured["audience_authority"] == "event_ir"
    assert windows == [
        event_ir.AbsoluteInterval(
            start=date(2026, 7, 5), end_exclusive=date(2026, 8, 4)
        )
    ]
    as_of_receipt = next(
        decision["value"]
        for decision in structured["decisions"]
        if decision.get("filter") == "canonical_audience_claims.explicit_as_of"
    )
    assert as_of_receipt == {
        "literal_id": "as_of_date_1",
        "anchor_literal_id": "as_of_date_1",
        "duration_literal_id": "duration_1",
        "anchor_date": "2026-08-03",
        "value": 30,
        "unit": "day",
        "start": "2026-07-05",
        "end_exclusive": "2026-08-04",
    }

    plan = graph_rag.build_query_plan(
        EXACT_QUERY, parser="llm", query_plan_v4=structured
    )
    assert plan["output_contract"]["member_id_only"] is True
    assert plan["output_contract"]["distinct_member_id"] is True
    assert plan["output_contract"]["evidence"] == {
        "text": "회원번호는 중복 없이",
        "start": 145,
        "end": 156,
    }

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("canonical plan attempted a second audience language")

    monkeypatch.setattr(graph_rag, "_build_llm_targeting_ir_candidate", forbidden)
    monkeypatch.setattr(graph_rag, "_build_llm_sql_fallback_candidate", forbidden)
    monkeypatch.setattr(graph_rag, "compile_member_target_conditions", forbidden)
    monkeypatch.setattr(
        graph_rag,
        "_verify_sql_semantics",
        lambda *_args, **_kwargs: {"ran": False},
    )
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(),
        EXACT_QUERY,
        plan,
        [],
        graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100,
        llm_model="must-not-be-used",
        original_query=EXACT_QUERY,
    )

    assert result["is_success"]
    assert result["selected"]["id"] == "sql_template:event_expression"
    assert {item["id"] for item in result["candidates"]} == {
        "sql_template:event_expression"
    }
    sql = result["sql"]
    assert sql.splitlines()[0] == "SELECT DISTINCT B.MEMBER_NO AS CUST_ID"
    assert "GETDATE" not in sql.upper()
    assert "B.LAST_LOGIN_DATE >= '20260705'" in sql
    assert "B.LAST_LOGIN_DATE < '20260804'" in sql
    assert sql.count("B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'") == 1
    assert "B.SIDO = '서울'" in sql and "B.SIDO = '경기'" in sql
    assert "(B.SIDO = '서울') OR (B.SIDO = '경기')" in sql
    assert "B.AGE >= 30" in sql and "B.AGE <= 44" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.GOLD'" in sql
    assert (
        "(B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP') OR "
        "(B.EMART_GRADE_CD = 'MEM_GRADE_CD.GOLD')"
    ) in sql
    assert "B.AGREE_YN = 'Y'" in sql
    assert "B.SMS_YN = 'Y'" in sql
    assert "B.APP_PUSH_YN = 'Y'" in sql
    assert (
        "(B.AGREE_YN = 'Y') AND (B.SMS_YN = 'Y') AND "
        "(B.APP_PUSH_YN = 'Y')"
    ) in sql
    assert "B.EMAIL_YN" not in sql
    assert "ACTIVITY_MEMBER_YN" not in sql
    assert "SLEEP_MEMBER_YN" not in sql
    assert "B.SITE_MEMBER_YN = 'N'" in sql
    assert "NOT (CASE WHEN (B.BLACKLIST_YN = 'Y')" in sql
    assert "NOT (CASE WHEN (B.EMPLOYEE_YN = 'Y')" in sql


def test_exact_query_requires_all_named_consents_not_any_consent() -> None:
    valid = _exact_expression()
    consent_evidence = "마케팅 활용·SMS·앱푸시에 모두 동의한"
    invalid_operands = list(valid.operands)
    invalid_operands[7:10] = [
        event_ir.Or(
            (
                _comparison(
                    EXACT_QUERY,
                    "subject.marketing_consent",
                    "=",
                    "agreed",
                    consent_evidence,
                ),
                _comparison(
                    EXACT_QUERY,
                    "subject.sms_consent",
                    "=",
                    "agreed",
                    consent_evidence,
                ),
                _comparison(
                    EXACT_QUERY,
                    "subject.app_push_consent",
                    "=",
                    "agreed",
                    consent_evidence,
                ),
            )
        )
    ]
    invalid = event_ir.And(tuple(invalid_operands))
    bindings = extract_literal_bindings(EXACT_QUERY, current_date=CURRENT_DATE)
    issues = canonical_audience_claims.canonical_claim_issues(
        EXACT_QUERY,
        invalid,
        bindings,
        audience_runtime.catalog_snapshot(),
    )

    assert any(issue["argument"] == "consent_cardinality" for issue in issues)


def test_exact_query_preserves_region_and_grade_or_connectors() -> None:
    valid = _exact_expression()
    bindings = extract_literal_bindings(EXACT_QUERY, current_date=CURRENT_DATE)
    for operand_index, expected_argument in (
        (1, "catalog_boolean.residence_sido"),
        (5, "catalog_boolean.grade"),
    ):
        invalid_operands = list(valid.operands)
        disjunction = invalid_operands[operand_index]
        assert isinstance(disjunction, event_ir.Or)
        invalid_operands[operand_index] = event_ir.And(disjunction.operands)
        issues = canonical_audience_claims.canonical_claim_issues(
            EXACT_QUERY,
            event_ir.And(tuple(invalid_operands)),
            bindings,
            audience_runtime.catalog_snapshot(),
        )
        assert any(issue["argument"] == expected_argument for issue in issues)


def test_negative_comparison_shapes_share_catalog_null_policy() -> None:
    context = audience_runtime.resolve_audience_catalog().compile_context(literals=True)
    for field, value in (
        ("subject.blacklisted", "blacklisted"),
        ("subject.employee", "employee"),
        ("subject.site_only", "site_only"),
    ):
        equality = event_ir.Comparison(
            "=", event_ir.FieldRef(field), event_ir.Literal(value)
        )
        direct_inequality = event_ir.Comparison(
            "!=", event_ir.FieldRef(field), event_ir.Literal(value)
        )
        wrapped_not = event_ir.Not(equality)

        direct_sql = graph_rag.event_compiler.compile_condition(
            direct_inequality, context
        ).sql
        wrapped_sql = graph_rag.event_compiler.compile_condition(
            wrapped_not, context
        ).sql
        assert direct_sql == wrapped_sql
        if field == "subject.site_only":
            assert direct_sql == "B.SITE_MEMBER_YN = 'N'"
        else:
            assert "CASE WHEN" in direct_sql


def test_canonical_contract_without_an_expression_fails_closed() -> None:
    """계약은 섰는데 표현이 없으면 종결한다 — 두 번째 오디언스 언어로 우회하지 않는다.

    2026-08-05 이전에는 이 자리에서 SemanticPlan 노드를 legacy 슬롯으로 컴파일하는 bridge 로
    떨어질 수 있었고, 그래서 '절대 bridge 를 부르지 않는다'가 계약이었다. bridge 는 폐기됐다 —
    남은 계약은 **결과**다: 표현 없는 canonical 요청은 조용한 성공이 아니라 정직한 차단이다.
    """
    plan: dict[str, Any] = {
        "audience_requirement": {"expression": None, "issues": []},
        "semantic_ir": empty_semantic_ir(status="resolved"),
    }
    assert audience_authority.requires_event_ir(plan)

    graph_rag._settle_canonical_audience_authority(plan)

    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert plan["semantic_ir"]["failure_kind"] == "system_failure"
    assert plan["semantic_ir"]["missing_fields"] == ["audience.event_ir"]
    assert plan.get("event_expression") is None


def test_canonical_ingress_cannot_call_a_public_legacy_sql_builder() -> None:
    plan: dict[str, Any] = {
        "intent": "find_user_segment",
        "audience_requirement": {"expression": None, "issues": []},
        "target_user": {"gender": "female"},
        "exclude": {},
    }

    assert audience_authority.requires_event_ir(plan)
    validation = graph_rag.plan_validation.validate_executable_plan(plan)
    assert validation.status == graph_rag.plan_validation.INTERNAL_INVALID
    assert graph_rag.build_member_targets_sql_candidate(plan) is None


def test_llm_candidate_cannot_inject_legacy_authority() -> None:
    candidate = {
        "intent": "find_user_segment",
        "audience_requirement": {"expression": None, "issues": []},
        "audience_authority": "legacy",
    }
    coerced = graph_rag._coerce_llm_query_plan_candidate(candidate, {})

    assert "audience_authority" not in coerced
    assert audience_authority.requires_event_ir(coerced)


def test_projector_rejects_short_alias_false_positive_and_disallowed_source() -> None:
    snapshot = audience_runtime.catalog_snapshot()
    node = {
        "id": "schema:member",
        "table_name": "CRM_MB_BASEINFO",
        "text_for_embedding": "CRM_MB_BASEINFO AGE",
    }
    projection = canonical_event_ir_grounding.project_canonical_event_ir_grounding(
        "회원번호를 중복 없이 보여주세요",
        [node],
        snapshot,
        allowed_fields=["subject.age"],
        allowed_sources=[],
    )
    assert projection["canonical_fields"] == []

    non_subject_field = next(
        field_id
        for field_id, declaration in snapshot["fields"].items()
        if declaration.get("source") not in {None, "subject"}
    )
    declaration = snapshot["fields"][non_subject_field]
    source = declaration["source"]
    source_declaration = snapshot["sources"][source]
    graph_node = {
        "id": "schema:foreign",
        "text_for_embedding": (
            f"{source_declaration['table']} {declaration['column']}"
        ),
    }
    projection = canonical_event_ir_grounding.project_canonical_event_ir_grounding(
        " ".join(
            [
                str(declaration.get("label") or non_subject_field),
                str(source_declaration.get("label") or source),
            ]
        ),
        [graph_node],
        snapshot,
        allowed_fields=[non_subject_field],
        allowed_sources=[],
    )
    assert projection["canonical_fields"] == []


def test_lowering_failure_preserves_user_clarification_and_quarantines_expression() -> None:
    """종결은 두 가지를 지킨다: 부분 표현 격리와 **원래 사유의 소유자**.

    부분적으로만 내려간 표현을 차단 사유 옆에 남겨 두면 플랜이 자기모순이고, 뒤 단계가 그
    부분 오디언스를 컴파일할 여지가 생긴다. 반대로 사용자에게 물어야 할 확인 요청을
    애플리케이션 실패로 다시 라벨링하면 사용자는 답할 수 없는 것을 요구받는다.
    """
    query = "여성 회원"
    expression = _comparison(query, "subject.gender", "=", "female", query)
    plan: dict[str, Any] = {
        "audience_requirement": {"expression": expression.to_dict(), "issues": []},
        "event_expression": {
            "source": "audience_requirement",
            "expression": expression.to_dict(),
        },
        "semantic_ir": empty_semantic_ir(
            status="needs_clarification",
            missing_fields=["audience.user_choice"],
            message="사용자 선택이 필요합니다.",
            failure_kind="user_clarification",
        ),
    }

    graph_rag._mark_canonical_event_ir_lowering_failure(plan, "표현을 완성하지 못했습니다.")

    assert plan["semantic_ir"]["failure_kind"] == "user_clarification"
    assert plan["semantic_ir"]["message"] == "사용자 선택이 필요합니다."
    assert "event_expression" not in plan
