"""의미 검증 v2(AST 기반 결과 집합 의미 검증) 단위/통합 테스트(§11).

핵심 회귀: 표현만 다르고 결과 집합이 동등한 정상 SQL 이 fail 처리되지 않아야 한다(BETWEEN↔범위,
IN↔EXISTS, JOIN↔상관 서브쿼리, CTE↔서브쿼리, alias/AND 순서/괄호 차이, DISTINCT↔GROUP BY,
NOT EXISTS↔anti-join). fail 은 구체적 근거(누락/반대/잘못된 날짜창/제외 누락/정책/반례)가 있을 때만.

실행: docker compose exec -T -w /app -e PYTHONPATH=/app python python -m pytest tests/test_semantic_validation_v2.py -q
"""

from __future__ import annotations

import pytest

from sql_semantics import extract_sql_semantics, SqlParseError
from target_spec import (
    Exclusion,
    ExecutionAssertion,
    PolicyViolation,
    Requirement,
    RetrievedEvidence,
    TargetSpecification,
)
from semantic_mapping import evaluate
from semantic_validation import (
    build_target_specification,
    validate_target_sql,
    run_shadow_validation,
    to_legacy_semantic_verification,
)


# 공용 스펙: 나이 >= 30 (R1) + 최근 90일 주문 존재 (R2).
def _age_and_recent_purchase_spec() -> TargetSpecification:
    return TargetSpecification(
        target_entity="customer",
        requirements=[
            Requirement("R1", "filter", "age", ">=", 30),
            Requirement("R2", "date_window", "orders.created_at", ">=", None, window_days=90),
        ],
    )


def _eval(sql: str, spec: TargetSpecification, **kw):
    return validate_target_sql("원문", sql, spec, **kw)


# 1. 동일한 WHERE 조건 → matched → pass
def test_identical_where_passes():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    r = _eval("SELECT DISTINCT customer_id FROM customers WHERE age >= 30", spec)
    assert r.status == "pass"
    assert r.checks[0].status == "matched"


# 2. BETWEEN 과 범위 비교 → equivalent → pass
def test_between_equals_range():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "filter", "age", ">=", 30),
        Requirement("R2", "filter", "age", "<=", 39),
    ])
    r = _eval("SELECT customer_id FROM customers WHERE age BETWEEN 30 AND 39", spec)
    assert r.status == "pass"
    assert {c.status for c in r.checks} <= {"matched", "equivalent"}


# 3. EXISTS 와 JOIN 동치 → pass
def test_exists_equals_join():
    spec = TargetSpecification(requirements=[Requirement("R1", "membership", "orders", "exists", "orders")])
    exists_sql = ("SELECT c.customer_id FROM customers c WHERE EXISTS "
                  "(SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id)")
    join_sql = ("SELECT DISTINCT c.customer_id FROM customers c "
                "JOIN orders o ON o.customer_id=c.customer_id")
    assert _eval(exists_sql, spec).status == "pass"
    assert _eval(join_sql, spec).status == "pass"


# 4. IN 과 EXISTS 동치(긍정) → pass
def test_in_subquery_equals_exists():
    spec = TargetSpecification(requirements=[Requirement("R1", "membership", "orders", "exists", "orders")])
    in_sql = "SELECT customer_id FROM customers WHERE customer_id IN (SELECT customer_id FROM orders)"
    assert _eval(in_sql, spec).status == "pass"


# 5. CTE 와 서브쿼리(날짜 창이 CTE 안) → pass
def test_cte_date_window_found():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "date_window", "orders.created_at", ">=", None, window_days=90)])
    cte_sql = ("WITH recent AS (SELECT customer_id FROM orders "
               "WHERE created_at >= CURRENT_DATE - INTERVAL '90' DAY) "
               "SELECT DISTINCT customer_id FROM recent")
    r = _eval(cte_sql, spec)
    assert r.status == "pass", r.reason_codes


# 6. alias 만 다른 SQL → pass
def test_alias_difference_ignored():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "customers.age", ">=", 30)])
    r = _eval("SELECT x.customer_id FROM customers x WHERE x.age >= 30", spec)
    assert r.status == "pass"


# 7. AND 조건 순서가 다른 SQL → pass
def test_and_order_ignored():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "filter", "age", ">=", 30),
        Requirement("R2", "filter", "gender", "=", "F"),
    ])
    a = _eval("SELECT customer_id FROM customers WHERE age >= 30 AND gender = 'F'", spec)
    b = _eval("SELECT customer_id FROM customers WHERE gender = 'F' AND age >= 30", spec)
    assert a.status == "pass" and b.status == "pass"


# 8. DISTINCT 와 GROUP BY 기반 중복 제거 동치 → pass
def test_distinct_equals_group_by():
    spec = TargetSpecification(requirements=[Requirement("R1", "dedup", "customer_id", None, None)])
    distinct_sql = "SELECT DISTINCT customer_id FROM customers"
    group_sql = "SELECT customer_id FROM customers GROUP BY customer_id"
    assert _eval(distinct_sql, spec).status == "pass"
    assert _eval(group_sql, spec).status == "pass"


# 9. 필수 조건이 실제로 누락된 SQL → fail(missing)
def test_missing_required_fails():
    spec = _age_and_recent_purchase_spec()
    sql = "SELECT DISTINCT customer_id FROM customers WHERE age >= 30"  # 최근 구매 조건 없음
    r = _eval(sql, spec)
    assert r.status == "fail"
    assert "R2" in r.missing_requirements
    assert any(rc.startswith("missing_required:R2") for rc in r.reason_codes)


# 10. 반대 조건이 적용된 SQL → fail(contradicted)
def test_inverted_condition_fails():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    r = _eval("SELECT customer_id FROM customers WHERE age <= 30", spec)
    assert r.status == "fail"
    assert r.checks[0].status == "contradicted"


# 10b. 극성 반전(EXISTS 요구인데 NOT EXISTS) → fail
def test_membership_polarity_inverted_fails():
    spec = TargetSpecification(requirements=[Requirement("R1", "membership", "orders", "exists", "orders")])
    sql = ("SELECT c.customer_id FROM customers c WHERE NOT EXISTS "
           "(SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id)")
    r = _eval(sql, spec)
    assert r.status == "fail"
    assert r.checks[0].status == "contradicted"


# 11. 날짜 범위가 90일 대신 30일 → fail(date_window_mismatch)
def test_wrong_date_window_fails():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "date_window", "orders.created_at", ">=", None, window_days=90)])
    sql = ("SELECT customer_id FROM orders WHERE created_at >= CURRENT_DATE - INTERVAL '30' DAY")
    r = _eval(sql, spec)
    assert r.status == "fail"
    assert r.checks[0].status == "contradicted"
    assert "date_window_mismatch" in r.checks[0].reason_code


# 12. 마케팅 수신 거부 제외 조건 누락 → fail
def test_missing_exclusion_fails():
    spec = TargetSpecification(
        requirements=[Requirement("R1", "filter", "age", ">=", 30)],
        exclusions=[Exclusion("E1", "marketing_opt_out", "=", True)],
    )
    sql = "SELECT customer_id FROM customers WHERE age >= 30"  # 제외 조건 없음
    r = _eval(sql, spec)
    assert r.status == "fail"
    assert "E1" in r.missing_requirements


# 12b. 제외 조건 반영됨(opt_out != true) → pass
def test_exclusion_applied_passes():
    spec = TargetSpecification(
        requirements=[Requirement("R1", "filter", "age", ">=", 30)],
        exclusions=[Exclusion("E1", "marketing_opt_out", "=", True)],
    )
    sql = "SELECT customer_id FROM customers WHERE age >= 30 AND marketing_opt_out = false"
    r = _eval(sql, spec)
    assert r.status == "pass", r.reason_codes


# 13. 불필요한 조건 추가로 결과 축소 → review(extra_restriction)
def test_extra_restriction_reviews():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    sql = "SELECT customer_id FROM customers WHERE age >= 30 AND region = 'SEOUL'"
    r = _eval(sql, spec)
    assert r.status == "review"
    assert r.extra_restrictions


# 14. NULL 때문에 NOT IN 과 NOT EXISTS 결과가 달라지는 사례 → review
def test_not_in_null_semantics_reviews():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "not_membership", "orders", "not_exists", "orders", negated=True)])
    sql = "SELECT customer_id FROM customers WHERE customer_id NOT IN (SELECT customer_id FROM orders)"
    r = _eval(sql, spec)
    assert r.status == "review"
    assert any("null_semantics" in c.reason_code for c in r.checks)


# 14b. NOT EXISTS anti-join 은 안전 동치 → pass
def test_not_exists_antijoin_passes():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "not_membership", "orders", "not_exists", "orders", negated=True)])
    sql = ("SELECT c.customer_id FROM customers c WHERE NOT EXISTS "
           "(SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id)")
    assert _eval(sql, spec).status == "pass"


# 15. RAG 정의 버전 불일치 → review
def test_rag_version_mismatch_reviews():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    r = _eval("SELECT customer_id FROM customers WHERE age >= 30", spec,
              evidence_version_mismatch=True)
    assert r.status == "review"
    assert "rag_evidence_version_mismatch" in r.reason_codes


# 16. parser 가 지원하지 못하는 구문 → review(파서 오류, 의미 fail 아님)
def test_parser_error_reviews_not_fail():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    r = _eval("THIS IS NOT VALID SQL )))(((", spec)
    assert r.status == "review"
    assert r.parser_errors


# 17. LLM confidence 가 낮지만 위반 근거 없음 → fail 아님(모호는 review)
def test_low_confidence_without_violation_not_fail():
    # 같은 필드에 규칙이 대응 못 하는 연산자(LIKE)만 있는 경우 → ambiguous → review (fail 아님).
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "name", "=", "kim")])
    sql = "SELECT customer_id FROM customers WHERE name LIKE '%kim%'"
    r = _eval(sql, spec)
    assert r.status != "fail"
    assert r.status == "review"


# 18. 구체적 정책 위반 → fail
def test_policy_violation_fails():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    pv = [PolicyViolation("consent_required", "마케팅 동의 필수 정책 미반영")]
    r = _eval("SELECT customer_id FROM customers WHERE age >= 30", spec, policy_violations=pv)
    assert r.status == "fail"
    assert any(rc.startswith("policy:") for rc in r.reason_codes)


# 19. 중복 customer_id 발생(실행 assertion fail) → fail
def test_duplicate_key_execution_fails():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    asserts = [ExecutionAssertion("duplicate_target_key_count", "fail", actual=5, expected=0)]
    r = _eval("SELECT customer_id FROM customers WHERE age >= 30", spec, execution_assertions=asserts)
    assert r.status == "fail"
    assert any(rc.startswith("execution:") for rc in r.reason_codes)


# 20. 데이터가 없어 결과 검증 불가 → review
def test_no_data_execution_reviews():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    asserts = [ExecutionAssertion("duplicate_target_key_count", "skipped")]
    r = _eval("SELECT customer_id FROM customers WHERE age >= 30", spec, execution_assertions=asserts)
    assert r.status == "review"
    assert "execution_skipped_no_data" in r.reason_codes


# --- 핵심 회귀: 동치 SQL 이 fail 처리되지 않는다 ---
@pytest.mark.parametrize("sql", [
    "SELECT DISTINCT c.customer_id FROM customers c WHERE c.age BETWEEN 30 AND 39",
    "SELECT customer_id FROM customers WHERE age <= 39 AND age >= 30",
    "SELECT customer_id FROM customers WHERE (age >= 30) AND (age <= 39)",
])
def test_equivalent_sql_never_fails(sql):
    spec = TargetSpecification(requirements=[
        Requirement("R1", "filter", "age", ">=", 30),
        Requirement("R2", "filter", "age", "<=", 39),
    ])
    r = _eval(sql, spec)
    assert r.status == "pass", (r.status, r.reason_codes)


# --- 범주형 값 확장(여성→코드 IN 목록): 컬럼+극성만 판정, 값 확장 완전성 미판정(§4) ---
def test_categorical_value_expansion_passes():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "filter", "GENDER_CD", "=", "female", categorical=True)])
    # 자연어 'female' 이 코드로 확장돼도 컬럼만 맞으면 반영된 것으로 본다.
    for sql in [
        "SELECT customer_id FROM m WHERE GENDER_CD = 'GENDER_CD.FEMALE'",
        "SELECT customer_id FROM m WHERE GENDER_CD IN ('GENDER_CD.FEMALE')",
    ]:
        r = _eval(sql, spec)
        assert r.status == "pass", (sql, r.reason_codes)


def test_categorical_polarity_inverted_fails():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "filter", "GENDER_CD", "=", "female", categorical=True)])
    r = _eval("SELECT customer_id FROM m WHERE GENDER_CD != 'GENDER_CD.FEMALE'", spec)
    assert r.status == "fail" and r.checks[0].status == "contradicted"


def test_categorical_column_absent_missing():
    spec = TargetSpecification(requirements=[
        Requirement("R1", "filter", "GENDER_CD", "=", "female", categorical=True)])
    r = _eval("SELECT customer_id FROM m WHERE AGE >= 30", spec)
    assert r.status == "fail" and r.checks[0].status == "missing"


# --- build_target_specification: query_plan 슬롯 → 요구사항 ---
def test_build_spec_from_query_plan():
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "age_min": 30,
            "gender": "GENDER_CD.FEMALE",
            "aggregate_conditions": [
                {"metric_id": "order_count", "operator": ">=", "threshold": 5, "window_days": 90}],
            "purchase_inactivity": {"min_days": 7},
        },
        "exclude": {"marketing_opt_out": [True]},
    }
    spec = build_target_specification(plan, dedup_key="member_no")
    types = {r.type for r in spec.requirements}
    assert "filter" in types and "aggregate" in types and "dedup" in types
    assert spec.exclusions and spec.exclusions[0].field == "marketing_opt_out"
    assert any(r.type == "not_membership" for r in spec.requirements)  # 미구매


# --- §12 호환 어댑터 + shadow 모드 ---
def test_legacy_adapter_shape():
    spec = TargetSpecification(requirements=[Requirement("R1", "filter", "age", ">=", 30)])
    r = _eval("SELECT customer_id FROM customers WHERE age <= 30", spec)  # contradicted
    legacy = to_legacy_semantic_verification(r)
    assert legacy["ran"] is True and legacy["faithful"] is False
    assert any(i["type"] == "inverted" for i in legacy["issues"])


def test_shadow_mode_never_raises():
    plan = {"target_user": {"age_min": 30}}
    out = run_shadow_validation("30세 이상", "SELECT customer_id FROM customers WHERE age >= 30", plan)
    assert out["ran"] is True
    assert out["result"]["status"] == "pass"


def test_shadow_mode_safe_on_bad_sql():
    plan = {"target_user": {"age_min": 30}}
    out = run_shadow_validation("30세 이상", "))) not sql (((", plan)
    # 파서 오류는 review 로 폴백(예외로 흐름 깨지 않음).
    assert out["ran"] is True
    assert out["result"]["status"] == "review"
