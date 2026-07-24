"""조인키 타입 가드 회귀.

배경: LLM SQL 폴백이 "최근 생성된 장바구니가 있지만 주문으로 이어지지 않은 회원"에 대해
`EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART C WHERE C.CART_ID = B.MEMBER_NO)` 를 만들어냈다.
CART_ID 는 nvarchar(로그인ID 'al923931'), MEMBER_NO 는 bigint 라 실행하면
"Error converting data type nvarchar to bigint" 로 실패하는데, 기존 sql_guard 는 테이블 허용목록·
SELECT 전용만 봐서 이걸 통과시키고 status=success 로 내보냈다(올바른 짝은 B.MEMBER_ID).
소스 데이터(sql_examples, 지식베이스)에는 그런 조인이 없어 컨텍스트 복사가 아니라 생성된 값이다.

고정 내용(2단):
  (1) 타입군 검사 — 등호 조인 양변의 타입군(schema_catalog 의 type)이 다르면 error.
  (2) 조인키 검사 — 검증된 관계(foreign_keys, confidence=verified)가 있는 컬럼은 그 짝하고만 조인.
      타입은 같지만 엉뚱한 문자열 컬럼에 붙인 조인(CART_ID = 다른 nvarchar)은 (1)로는 못 잡는다.
      실측: CART_ID = MEMBER_ID 는 14,771건 매칭, CART_ID = MEMBER_NO 는 변환 오류.
스키마에 타입/관계 정보가 없으면 둘 다 판정 보류한다(정상 SQL 오탐 방지).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_join_key_guard.py -q
"""

from pathlib import Path

from sql_guard import load_column_types, load_join_key_registry, validate_join_keys

SCHEMA = Path("docs/data/schema_catalog.json")
COLUMN_TYPES = load_column_types(SCHEMA)
JOIN_REGISTRY = load_join_key_registry(SCHEMA)


def _check(sql: str) -> dict:
    return validate_join_keys(sql, COLUMN_TYPES, JOIN_REGISTRY)

BAD_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, 'cart_abandoner' AS segment_label "
    "FROM CRM_MB_BASEINFO B WHERE B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL' "
    "AND EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART C WHERE C.CART_ID = B.MEMBER_NO)"
)
GOOD_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID FROM ODS_MALL_OMS_CART A "
    "INNER JOIN CRM_MB_BASEINFO B ON A.CART_ID = B.MEMBER_ID WHERE A.KEEP_YN = 'Y'"
)


def test_schema_types_are_loaded():
    assert COLUMN_TYPES, "schema_catalog 에서 컬럼 타입을 못 읽으면 가드가 무력화된다"
    assert COLUMN_TYPES["crm_mb_baseinfo"]["member_no"] == "numeric"
    assert COLUMN_TYPES["crm_mb_baseinfo"]["member_id"] == "string"
    assert COLUMN_TYPES["ods_mall_oms_cart"]["cart_id"] == "string"


def test_verified_cart_relationship_is_registered():
    # CART_ID 의 검증된 상대는 CRM_MB_BASEINFO.MEMBER_ID 다(양방향 등록).
    assert JOIN_REGISTRY[("ods_mall_oms_cart", "cart_id", "crm_mb_baseinfo")] == {"member_id"}
    assert JOIN_REGISTRY[("crm_mb_baseinfo", "member_id", "ods_mall_oms_cart")] == {"cart_id"}


def test_type_mismatch_join_is_rejected():
    result = _check(BAD_SQL)
    assert result["is_valid"] is False
    assert result["issues"][0]["code"] == "join_key_type_mismatch"
    assert result["issues"][0]["severity"] == "error"
    assert "CART_ID" in result["issues"][0]["message"] and "MEMBER_NO" in result["issues"][0]["message"]


def test_correct_join_passes():
    assert _check(GOOD_SQL) == {"is_valid": True, "issues": []}


def test_wrong_same_type_join_is_rejected():
    # 타입군이 같아(둘 다 문자열) 타입 검사로는 안 걸리지만, CART_ID 의 상대는 MEMBER_ID 뿐이다.
    sql = (
        "SELECT 1 FROM ODS_MALL_OMS_CART A INNER JOIN CRM_MB_BASEINFO B "
        "ON A.CART_ID = B.MEMBER_STATE_CD"
    )
    result = _check(sql)
    assert result["is_valid"] is False
    assert result["issues"][0]["code"] == "join_key_not_verified"
    assert "MEMBER_ID" in result["issues"][0]["message"]


def test_reversed_join_direction_is_checked():
    sql = (
        "SELECT 1 FROM CRM_MB_BASEINFO B INNER JOIN ODS_MALL_OMS_CART A "
        "ON B.MEMBER_ID = A.PRODUCT_ID"
    )
    assert _check(sql)["is_valid"] is False


def test_unknown_columns_are_not_flagged():
    # 스키마에 없는 테이블/컬럼은 판정 보류 — 가드가 정상 SQL 을 막으면 안 된다.
    sql = "SELECT 1 FROM some_unknown_table X JOIN other_unknown Y ON X.A = Y.B"
    assert _check(sql)["is_valid"] is True


def test_columns_without_verified_relationship_are_not_flagged():
    # 양쪽 다 검증된 관계가 없는 컬럼 조합은 강제하지 않는다(추정으로 정상 SQL 을 막지 않기 위해).
    # 주의: 한쪽만 알려져 있어도 강제된다 — MEMBER_ID 를 CART_ID 아닌 컬럼에 붙이면 잡힌다
    # (test_reversed_join_direction_is_checked 가 그 경우를 고정한다).
    sql = "SELECT 1 FROM ODS_MALL_OMS_CART A JOIN CRM_MB_BASEINFO B ON A.CART_TYPE_CD = B.MEMBER_STATE_CD"
    assert _check(sql)["is_valid"] is True


def test_alias_less_table_reference_resolves():
    sql = (
        "SELECT 1 FROM ODS_MALL_OMS_CART JOIN CRM_MB_BASEINFO "
        "ON ODS_MALL_OMS_CART.CART_ID = CRM_MB_BASEINFO.MEMBER_NO"
    )
    assert _check(sql)["is_valid"] is False


def test_same_family_different_width_is_allowed():
    # bigint vs int 처럼 같은 군이면 통과해야 한다(과잉 차단 방지).
    types = {"t1": {"a": "numeric"}, "t2": {"b": "numeric"}}
    assert validate_join_keys("SELECT 1 FROM t1 X JOIN t2 Y ON X.A = Y.B", types)["is_valid"] is True


# 캠페인 반응 팩트 조인(MBR_NO 문자열 ↔ MEMBER_NO 숫자)은 raw 등호면 타입 불일치로 실행 실패한다.
# 빌더가 TRY_CAST(R.MBR_NO AS BIGINT) 캐스트 조인을 써야 가드를 통과한다(후보 통째 탈락 방지).
_CAMPAIGN_RAW = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "WHERE EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R WHERE R.MBR_NO = B.MEMBER_NO AND R.CNCT_SCS_YN = 'Y')"
)
_CAMPAIGN_CAST = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "WHERE EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R WHERE TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO AND R.CNCT_SCS_YN = 'Y')"
)


def test_campaign_response_raw_join_is_rejected():
    result = _check(_CAMPAIGN_RAW)
    assert result["is_valid"] is False
    assert result["issues"][0]["code"] == "join_key_type_mismatch"


def test_campaign_response_cast_join_passes():
    assert _check(_CAMPAIGN_CAST) == {"is_valid": True, "issues": []}
