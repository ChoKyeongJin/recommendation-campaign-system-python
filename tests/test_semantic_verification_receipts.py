"""Deterministic authority receipts for the final SQL semantic verifier."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import default_period_policy
import event_ir
import graph_rag
import qualitative_defaults
import semantic_verification_receipts
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


# --- 호출 계층 기본 기간 정책이 채운 창 --------------------------------------------------------
# 원문이 기간을 말하지 않은 '최근'은 카탈로그 기본값(qualitative_defaults: 최근 → 최근 5일)으로
# 채워진다. 그 창은 **원문에 없는 것이 맞아서** 검증기가 spurious 로 부르고, 그대로 두면 정책이
# 켜진 배포가 정책을 적용할 때마다 자기 SQL 을 막는다.
RECENT_CAMPAIGN_QUERY = "최근 캠페인 발송 성공 횟수가 3회 이상인 회원"
RECENT_CAMPAIGN_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "WHERE (SELECT COUNT(*) FROM MCS_CAMP_MBR_RSPN_FT ZC "
    "WHERE ZC.MEMBER_NO = B.MEMBER_NO AND ZC.SEND_RSLT_CD = 'SUCCESS' "
    "AND ZC.CAMP_SDATE >= '20260802') >= 3"
)


def _applied_default_period(value: int = 5, unit: str = "day") -> dict:
    """default_period_policy 가 채택 후에만 남기는 영수증 모양."""
    return {
        "source": default_period_policy.POLICY_SOURCE,
        "value": value,
        "unit": unit,
        "window": {"type": "rolling", "value": value, "unit": unit},
        "origin": f"{qualitative_defaults.ENABLED_ENV}:recent",
        "expression": "최근",
        "evidence": [{"text": "최근 캠페인 발송 성공 횟수가 3회 이상", "start": 0, "end": 23}],
    }


def _spurious_window_verdict() -> dict:
    """실제로 관측된 판정 문구(원문에 없는 5일 창 지적)."""
    return {
        "ran": True,
        "status": "fail",
        "faithful": False,
        "issues": [{
            "type": "spurious",
            "condition": "최근 기간(기간 미지정)",
            "detail": (
                "원문은 기간을 명시하지 않았으나 SQL은 최근 5일(ZC.CAMP_SDATE >= 기준일-5일)로 "
                "기간 조건을 추가해 원문에 없던 시간창 제한을 적용함."
            ),
        }],
    }


def _covered(issue: dict, applied_period: dict | None) -> bool:
    return semantic_verification_receipts.applied_period_issue_is_covered(
        issue, applied_period
    )


def test_policy_applied_window_is_not_a_spurious_defect() -> None:
    verification = _spurious_window_verdict()
    plan = {
        **MEMBER_PLAN,
        default_period_policy.DEFAULT_PERIOD_KEY: _applied_default_period(),
    }

    delivery = graph_rag._validate_sql_delivery_contract(
        RECENT_CAMPAIGN_QUERY,
        plan,
        RECENT_CAMPAIGN_SQL,
        semantic_verification=verification,
    )
    reconciled = graph_rag._reconcile_semantic_verification_with_receipts(
        verification, delivery
    )

    assert delivery["semantic_issues"][0]["exempt_reason"] == "applied_default_period_policy"
    assert delivery["semantic_issues"][0]["severity"] == "warning"
    assert "critical_semantic_issue" not in delivery["failure_reasons"]
    assert delivery["is_satisfied"]
    # 판정은 지워지지 않는다 — 관측은 남기고 차단만 푼다(어떤 창이 왜 들어갔는지의 근거).
    assert reconciled["status"] == "review"
    assert reconciled["original_status"] == "fail"


def test_an_unexplained_window_still_blocks() -> None:
    """영수증이 없으면 같은 판정이 그대로 차단이다 — 면제의 근거는 정책 기록뿐이다."""
    verification = _spurious_window_verdict()

    delivery = graph_rag._validate_sql_delivery_contract(
        RECENT_CAMPAIGN_QUERY,
        MEMBER_PLAN,
        RECENT_CAMPAIGN_SQL,
        semantic_verification=verification,
    )

    assert delivery["semantic_issues"][0].get("exempt_reason") is None
    assert delivery["semantic_issues"][0]["severity"] == "critical"
    assert not delivery["is_satisfied"]
    assert "critical_semantic_issue" in delivery["failure_reasons"]


def test_a_dispute_over_another_period_is_not_covered() -> None:
    """정책이 고른 값이 아닌 기간을 다투는 판정은 면제되지 않는다(사용자 명시값 보호)."""
    assert not _covered(
        {
            "type": "spurious",
            "condition": "최근 30일",
            "detail": "원문에 없는 최근 30일 창이 SQL 에 있음.",
        },
        _applied_default_period(),
    )


@pytest.mark.parametrize(
    "detail",
    [
        "원문에 기간이 없는데 SQL 은 ZC.CAMP_SDATE >= '20260802' 로 제한함.",
        "원문에 없는 기간 제한이 SQL 에 있음(3회 이상 임계값은 원문대로 반영됨).",
    ],
    ids=("rendered-date-literal", "unrelated-threshold-number"),
)
def test_numbers_that_are_not_windows_do_not_break_the_receipt(detail: str) -> None:
    """창이 아닌 수(렌더된 날짜 리터럴·임계값)는 창 주장이 아니므로 비교 대상이 아니다."""
    assert _covered(
        {"type": "spurious", "condition": "최근 기간(기간 미지정)", "detail": detail},
        _applied_default_period(),
    )


def test_a_non_period_filter_is_not_covered_by_the_period_receipt() -> None:
    assert not _covered(
        {
            "type": "spurious",
            "condition": "지역 조건",
            "detail": "원문에 없는 B.SIDO = '서울' 필터가 SQL 에 있음.",
        },
        _applied_default_period(),
    )


def test_a_column_named_date_is_not_a_period_dispute() -> None:
    """영문 단서는 단어 경계로 본다 — CAMP_SDATE 가 'date' 로 걸리면 면제가 새어 나간다."""
    assert not _covered(
        {
            "type": "spurious",
            "condition": "발송 상태 조건",
            "detail": "원문에 없는 ZC.SEND_RSLT_CD 필터가 SQL 에 있음(ZC.CAMP_SDATE 기준 집계).",
        },
        _applied_default_period(),
    )


@pytest.mark.parametrize("issue_type", ["dropped", "inverted"])
def test_the_period_receipt_never_excuses_a_missing_or_inverted_condition(
    issue_type: str,
) -> None:
    assert not _covered(
        {
            "type": issue_type,
            "condition": "최근 기간",
            "detail": "최근 5일 창이 SQL 에 반영되지 않았습니다.",
        },
        _applied_default_period(),
    )


def test_a_week_scale_default_is_recognized_in_day_wording() -> None:
    """주 단위 정책값을 검증기가 일수로 부를 수 있다(같은 창이다). 달·해는 환산하지 않는다."""
    issue = {
        "type": "spurious",
        "condition": "최근 기간(기간 미지정)",
        "detail": "원문에 없는 최근 14일 창이 SQL 에 있음.",
    }

    assert _covered(issue, _applied_default_period(2, "week"))
    assert not _covered(issue, _applied_default_period(2, "month"))


def test_only_the_policys_own_receipt_can_excuse_a_window() -> None:
    """면제의 근거는 '정책이 채웠다'는 기록뿐이다 — 다른 출처의 기간 기록은 영수증이 아니다."""
    plan = {**MEMBER_PLAN, default_period_policy.DEFAULT_PERIOD_KEY: _applied_default_period()}
    foreign = {
        **MEMBER_PLAN,
        default_period_policy.DEFAULT_PERIOD_KEY: {**_applied_default_period(), "source": "user"},
    }

    assert default_period_policy.applied_period_receipt(plan) is not None
    assert default_period_policy.applied_period_receipt(foreign) is None
    assert default_period_policy.applied_period_receipt(MEMBER_PLAN) is None
    assert not _covered(_spurious_window_verdict()["issues"][0], None)


def _stub_verifier_call(monkeypatch, captured: dict) -> None:
    """의미 검증 게이트가 실제로 보낸 messages 를 잡아 둔다(LLM 호출은 하지 않는다)."""

    def _capture(_client, **kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content='{"status": "pass", "reason": "ok", "issues": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(graph_rag, "_sql_semantic_verify_enabled", lambda: True)
    monkeypatch.setattr(graph_rag, "_semantic_verify_model", lambda _model: "test-model")
    monkeypatch.setattr(graph_rag, "_openai_chat_create", _capture)
    monkeypatch.setattr(graph_rag, "_write_rag_llm_log", lambda *_args, **_kwargs: None)


def test_the_verifier_is_told_which_window_the_policy_chose(monkeypatch) -> None:
    """면제는 마지막 안전망이다. 검증기에 정책을 알려 애초에 지적하지 않게 한다."""
    captured: dict = {}
    _stub_verifier_call(monkeypatch, captured)

    verdict = graph_rag._verify_sql_semantics(
        RECENT_CAMPAIGN_QUERY,
        RECENT_CAMPAIGN_SQL,
        "test-model",
        None,
        {**MEMBER_PLAN, default_period_policy.DEFAULT_PERIOD_KEY: _applied_default_period()},
    )

    assert verdict["status"] == "pass"
    system_prompt, user_prompt = (message["content"] for message in captured["messages"])
    assert "[적용된 기본 기간 정책]" in user_prompt
    assert '"value": 5' in user_prompt and '"unit": "day"' in user_prompt
    assert "[적용된 기본 기간 정책]" in system_prompt


def test_the_policy_block_is_absent_when_no_default_was_applied(monkeypatch) -> None:
    captured: dict = {}
    _stub_verifier_call(monkeypatch, captured)

    graph_rag._verify_sql_semantics(
        RECENT_CAMPAIGN_QUERY, RECENT_CAMPAIGN_SQL, "test-model", None, MEMBER_PLAN
    )

    _system_prompt, user_prompt = (message["content"] for message in captured["messages"])
    assert "[적용된 기본 기간 정책]" not in user_prompt
