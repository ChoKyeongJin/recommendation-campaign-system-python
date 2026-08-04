"""Deterministic authority receipts for the final SQL semantic verifier."""

from __future__ import annotations

import pytest

import event_ir
import graph_rag
import sql_guard

MEMBER_PLAN = {
    "intent": "find_user_segment",
    "output_contract": {
        "expected_grain": "member",
        "requires_member_id": True,
        "requires_member_no_as_cust_id": True,
    },
}
CART_SQL = (
    "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "WHERE (SELECT COUNT(DISTINCT EC.PRODUCT_ID) FROM ODS_MALL_OMS_CART EC "
    "WHERE EC.CART_ID = B.MEMBER_ID) >= 3"
)
CART_QUERY = "장바구니에 서로 다른 상품을 3개 이상 담아둔 회원"
# 이벤트 컴파일러가 실제로 내보내는 형태(카탈로그 from_sql 의 상품 LEFT JOIN 포함).
PRODUCTION_CART_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, B.EMART_GRADE_CD AS member_grade, "
    "'장바구니 담기 count >= 3' AS segment_label "
    "FROM CRM_MB_BASEINFO B WHERE B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL' "
    "AND (SELECT COUNT(DISTINCT EC.PRODUCT_ID) FROM ODS_MALL_OMS_CART EC "
    "LEFT JOIN CRM_CM_PRODUCT EC_PRODUCT ON EC.PRODUCT_ID = EC_PRODUCT.PRODUCT_ID "
    "WHERE EC.CART_ID = B.MEMBER_ID) >= 3"
)
# 카탈로그로 증명된 조인(CART_ID=MEMBER_ID)과 증명되지 않은 조인이 섞인 SQL.
MIXED_JOIN_SQL = (
    "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "JOIN CRM_CM_PRODUCT P ON B.MEMBER_ID = P.PRODUCT_NM "
    "WHERE (SELECT COUNT(DISTINCT EC.PRODUCT_ID) FROM ODS_MALL_OMS_CART EC "
    "WHERE EC.CART_ID = B.MEMBER_ID) >= 3"
)
ALL_CONSENT_QUERY = "이메일, 문자, 앱푸시에 모두 동의한 회원을 추출해줘."
ALL_CONSENT_SQL = (
    "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "WHERE B.EMAIL_YN = 'Y' AND B.SMS_YN = 'Y' AND B.APP_PUSH_YN = 'Y'"
)


def _canonical_all_consent_plan() -> dict:
    expression = event_ir.And(operands=tuple(
        event_ir.Comparison(
            operator="=",
            left=event_ir.FieldRef(name=field),
            right=event_ir.Literal(value="agreed"),
        )
        for field in (
            "subject.email_consent",
            "subject.sms_consent",
            "subject.app_push_consent",
        )
    ))
    return {
        **MEMBER_PLAN,
        "event_expression": {
            "expression": expression.to_dict(),
            "source": "audience_requirement",
        },
    }


def _join_validation(sql: str = CART_SQL) -> dict:
    return sql_guard.validate_join_keys(
        sql,
        sql_guard.load_column_types(),
        sql_guard.load_join_key_registry(),
    )


def test_verified_cart_member_join_emits_a_catalog_receipt() -> None:
    validation = _join_validation()

    assert validation["is_valid"]
    assert validation["verified_relationships"] == [
        {
            "left_table": "ods_mall_oms_cart",
            "left_column": "cart_id",
            "right_table": "crm_mb_baseinfo",
            "right_column": "member_id",
            "evidence": "schema_catalog.foreign_keys:verified",
            "cast_approved": False,
        }
    ]


def test_catalog_receipt_overrides_only_the_exact_join_dispute() -> None:
    validation = _join_validation()
    false_positive = {
        "type": "wrong_value",
        "condition": "회원과 카트 연관 식별자",
        "detail": "CART_ID = MEMBER_ID 조인은 일반적으로 올바르지 않을 수 있습니다.",
    }
    real_semantic_drop = {
        "type": "dropped",
        "condition": "서로 다른 상품 3개 이상",
        "detail": "COUNT(DISTINCT PRODUCT_ID) >= 3 조건이 없습니다.",
    }

    assert graph_rag._semantic_issue_exemption(
        false_positive,
        CART_SQL,
        MEMBER_PLAN,
        join_key_validation=validation,
    ) == "catalog_verified_relationship"
    assert graph_rag._semantic_issue_exemption(
        real_semantic_drop,
        CART_SQL,
        MEMBER_PLAN,
        join_key_validation=validation,
    ) is None


def test_unregistered_cart_join_has_no_authority_receipt() -> None:
    bad_sql = CART_SQL.replace("B.MEMBER_ID", "B.MEMBER_NO")
    validation = _join_validation(bad_sql)

    assert not validation["is_valid"]
    assert validation["verified_relationships"] == []


def test_receipt_lists_every_equality_join_left_unproven() -> None:
    """is_valid 만으로는 '조인이 전부 증명됐는지'를 알 수 없다 — 미증명 조인도 목록으로 나와야 한다."""
    assert _join_validation(PRODUCTION_CART_SQL)["unverified_relationships"] == []

    mixed = _join_validation(MIXED_JOIN_SQL)

    assert mixed["is_valid"]  # 미등록 관계는 error 가 아니라 '증명 안 됨'이다.
    assert mixed["unverified_relationships"] == [
        {
            "left_table": "crm_mb_baseinfo",
            "left_column": "member_id",
            "right_table": "crm_cm_product",
            "right_column": "product_nm",
        }
    ]


def test_catalog_receipt_overrides_a_join_dispute_that_names_no_column() -> None:
    """실제 오탐 문구: 검증기가 CART_ID/MEMBER_ID 를 부르지 않고 조인을 뭉뚱그려 부정했다.

    컬럼명 매칭에만 의존하면 이런 표현에서 영수증이 새고 정상 SQL 이 차단된다.
    """
    abstract_dispute = {
        "type": "dropped",
        "condition": "장바구니-회원 관계의 올바른 조인",
        "detail": (
            "원문은 '회원의 장바구니에 담긴 서로 다른 상품 수 >=3'를 요구하나, SQL은 잘못된 "
            "컬럼 매칭으로 인해 실제 회원별 카트 라인이 집계되지 않을 가능성이 있음."
        ),
    }

    assert graph_rag._semantic_issue_exemption(
        abstract_dispute,
        PRODUCTION_CART_SQL,
        MEMBER_PLAN,
        join_key_validation=_join_validation(PRODUCTION_CART_SQL),
    ) == "catalog_verified_relationship"


def test_abstract_join_dispute_is_not_exempted_when_a_join_is_unproven() -> None:
    """증명되지 않은 조인이 하나라도 있으면 그 조인을 가리킨 판정일 수 있으므로 면제하지 않는다."""
    abstract_dispute = {
        "type": "dropped",
        "condition": "장바구니-회원 관계의 올바른 조인",
        "detail": "잘못된 컬럼 매칭으로 회원별 라인이 집계되지 않음.",
    }

    assert graph_rag._semantic_issue_exemption(
        abstract_dispute,
        MIXED_JOIN_SQL,
        MEMBER_PLAN,
        join_key_validation=_join_validation(MIXED_JOIN_SQL),
    ) is None


def test_non_join_dispute_is_not_exempted_by_a_join_receipt() -> None:
    """'매칭'·'키' 같은 단어가 섞여도 조인 판정이 아니면 영수증으로 반증되지 않는다."""
    threshold_drop = {
        "type": "dropped",
        "condition": "서로 다른 상품 3개 이상",
        "detail": "상품 수 임계값 매칭이 SQL에 없습니다.",
    }

    assert graph_rag._semantic_issue_exemption(
        threshold_drop,
        PRODUCTION_CART_SQL,
        MEMBER_PLAN,
        join_key_validation=_join_validation(PRODUCTION_CART_SQL),
    ) is None


@pytest.mark.parametrize(
    ("query", "predicate", "expected_status"),
    [
        ("이메일 수신에 동의한 회원", "B.EMAIL_YN = 'Y'", "covered"),
        ("이메일 수신을 거부한 회원", "B.EMAIL_YN = 'N'", "covered"),
        ("이메일 수신을 거부한 회원", "B.EMAIL_YN <> 'Y'", "covered"),
        (
            "이메일 수신을 거부한 회원",
            "NOT (CASE WHEN (B.EMAIL_YN = 'Y') THEN 1 ELSE 0 END = 1)",
            "covered",
        ),
        ("이메일 수신에 동의한 회원", "ISNULL(B.EMAIL_YN, 'N') = 'Y'", "covered"),
        ("이메일 수신에 동의한 회원", "B.EMAIL_YN = 'N'", "polarity_mismatch"),
        (
            "이메일 수신에 동의한 회원",
            "NOT (CASE WHEN (B.EMAIL_YN = 'Y') THEN 1 ELSE 0 END = 1)",
            "polarity_mismatch",
        ),
    ],
)
def test_consent_receipt_tracks_physical_value_and_effective_ast_polarity(
    query: str, predicate: str, expected_status: str,
) -> None:
    sql = f"SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE {predicate}"
    receipts = graph_rag._consent_coverage_receipts(query, sql)

    assert len(receipts) == 1
    assert receipts[0]["column"] == "EMAIL_YN"
    assert receipts[0]["status"] == expected_status
    assert receipts[0]["satisfied"] is (expected_status == "covered")


def test_all_consent_channels_receive_independent_receipts() -> None:
    receipts = graph_rag._consent_coverage_receipts(ALL_CONSENT_QUERY, ALL_CONSENT_SQL)

    assert {receipt["column"] for receipt in receipts} == {
        "EMAIL_YN",
        "SMS_YN",
        "APP_PUSH_YN",
    }
    assert all(receipt["status"] == "covered" for receipt in receipts)


def test_canonical_all_consent_has_no_legacy_slot_warning_and_passes_delivery() -> None:
    plan = _canonical_all_consent_plan()

    assert graph_rag._deterministic_dropped_conditions(ALL_CONSENT_QUERY, plan) == []
    delivery = graph_rag._validate_sql_delivery_contract(
        ALL_CONSENT_QUERY,
        plan,
        ALL_CONSENT_SQL,
        semantic_verification={"ran": True, "status": "pass", "faithful": True, "issues": []},
    )
    assert delivery["is_satisfied"]
    assert all(
        receipt["satisfied"]
        for receipt in delivery["deterministic_receipts"]["consent_fields"]
    )


def test_consent_projection_does_not_count_as_filter_coverage() -> None:
    sql = (
        "SELECT B.MEMBER_NO AS CUST_ID, "
        "CASE WHEN B.EMAIL_YN = 'Y' THEN 1 ELSE 0 END AS email_optin "
        "FROM CRM_MB_BASEINFO B"
    )

    receipt = graph_rag._consent_coverage_receipts(
        "이메일 수신에 동의한 회원", sql
    )[0]

    assert receipt["status"] == "missing"
    assert receipt["satisfied"] is False


def test_all_consent_channels_cannot_be_discharged_by_or_predicates() -> None:
    sql = (
        "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE "
        "B.EMAIL_YN = 'Y' OR B.SMS_YN = 'Y' OR B.APP_PUSH_YN = 'Y'"
    )
    receipts = graph_rag._consent_coverage_receipts(ALL_CONSENT_QUERY, sql)

    assert {receipt["status"] for receipt in receipts} == {"non_conjunctive"}
    assert not any(receipt["satisfied"] for receipt in receipts)
    delivery = graph_rag._validate_sql_delivery_contract(
        ALL_CONSENT_QUERY,
        MEMBER_PLAN,
        sql,
        semantic_verification={"ran": True, "status": "pass", "faithful": True, "issues": []},
    )
    assert not delivery["is_satisfied"]
    assert "consent_conditions_not_covered" in delivery["failure_reasons"]


@pytest.mark.parametrize(
    ("query", "sql"),
    [
        (
            "이메일, 문자, 앱푸시 중 하나 이상에 동의한 회원을 보여줘.",
            "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE "
            "B.EMAIL_YN = 'Y' OR B.SMS_YN = 'Y' OR B.APP_PUSH_YN = 'Y'",
        ),
        (
            "이메일, 문자, 앱푸시 중 정확히 두 개 채널에 동의한 회원을 찾아줘.",
            "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE "
            "(CASE WHEN B.EMAIL_YN = 'Y' THEN 1 ELSE 0 END + "
            "CASE WHEN B.SMS_YN = 'Y' THEN 1 ELSE 0 END + "
            "CASE WHEN B.APP_PUSH_YN = 'Y' THEN 1 ELSE 0 END) = 2",
        ),
    ],
)
def test_cardinality_consent_queries_defer_to_the_existing_semantic_contract(
    query: str, sql: str,
) -> None:
    assert graph_rag._consent_coverage_receipts(query, sql) == []

    delivery = graph_rag._validate_sql_delivery_contract(
        query,
        MEMBER_PLAN,
        sql,
        semantic_verification={"ran": True, "status": "pass", "faithful": True, "issues": []},
    )
    assert delivery["is_satisfied"]
    assert delivery["deterministic_receipts"]["consent_fields"] == []


@pytest.mark.parametrize(
    ("sql", "missing_columns"),
    [
        (ALL_CONSENT_SQL.replace(" AND B.APP_PUSH_YN = 'Y'", ""), {"APP_PUSH_YN"}),
        (
            "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE B.EMAIL_YN = 'Y'",
            {"SMS_YN", "APP_PUSH_YN"},
        ),
    ],
    ids=("one-channel-missing", "only-one-channel-emitted"),
)
def test_incomplete_consent_fields_fail_delivery_even_if_the_llm_passes(
    sql: str, missing_columns: set[str],
) -> None:
    plan = _canonical_all_consent_plan()
    assert graph_rag._deterministic_dropped_conditions(ALL_CONSENT_QUERY, plan) == []
    delivery = graph_rag._validate_sql_delivery_contract(
        ALL_CONSENT_QUERY,
        plan,
        sql,
        semantic_verification={"ran": True, "status": "pass", "faithful": True, "issues": []},
    )

    assert not delivery["is_satisfied"]
    assert "consent_conditions_not_covered" in delivery["failure_reasons"]
    by_column = {
        receipt["column"]: receipt
        for receipt in delivery["deterministic_receipts"]["consent_fields"]
    }
    assert by_column["EMAIL_YN"]["satisfied"]
    assert {
        column for column, receipt in by_column.items() if receipt["status"] == "missing"
    } == missing_columns


def test_llm_consent_false_positive_is_downgraded_by_its_field_receipt() -> None:
    verification = {
        "ran": True,
        "status": "fail",
        "faithful": False,
        "issues": [{
            "type": "dropped",
            "condition": "이메일 수신 동의",
            "detail": "EMAIL_YN 동의 필터가 없습니다.",
        }],
    }
    delivery = graph_rag._validate_sql_delivery_contract(
        ALL_CONSENT_QUERY,
        MEMBER_PLAN,
        ALL_CONSENT_SQL,
        semantic_verification=verification,
    )
    reconciled = graph_rag._reconcile_semantic_verification_with_receipts(
        verification, delivery
    )

    assert delivery["is_satisfied"]
    assert delivery["semantic_issues"][0]["exempt_reason"] == (
        "registered_consent_predicate_present"
    )
    assert reconciled["status"] == "review"
    assert reconciled["faithful"] is True
    assert reconciled["deterministic_override"] is True


def test_llm_join_false_positive_is_downgraded_but_real_join_error_is_not() -> None:
    verification = {
        "ran": True,
        "status": "fail",
        "faithful": False,
        "issues": [{
            "type": "wrong_value",
            "condition": "회원과 카트 연관 식별자",
            "detail": "CART_ID = MEMBER_ID 조인은 일반적으로 올바르지 않을 수 있습니다.",
        }],
    }
    delivery = graph_rag._validate_sql_delivery_contract(
        CART_QUERY,
        MEMBER_PLAN,
        CART_SQL,
        semantic_verification=verification,
        join_key_validation=_join_validation(),
    )
    reconciled = graph_rag._reconcile_semantic_verification_with_receipts(
        verification, delivery
    )

    assert delivery["is_satisfied"]
    assert delivery["semantic_issues"][0]["exempt_reason"] == "catalog_verified_relationship"
    assert reconciled["status"] == "review"
