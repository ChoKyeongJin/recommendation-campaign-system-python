"""SQL 방언 어댑터(sql_dialect.py) 회귀 — DB 이식성 계층.

배경(docs/operations/db_portability_audit.md §4-A): 타겟팅 실DB는 계속 다른 DB로 바뀔 수
있어, 결정론 빌더의 날짜창·캐스트·힌트 등 엔진 문법을 sql_dialect 어댑터로 추상화했다.

고정 내용:
  - tsql 렌더가 리팩터 전 인라인 문자열과 **바이트 단위로 동일**하다(기존 SQL 출력 불변 보증).
  - mysql/postgres 렌더가 각 엔진 문법으로 나온다(이식성 실증).
  - graph_rag._member_dialect() 기본이 tsql 이고, 방언을 갈아끼우면 같은 빌더가 다른 엔진
    문법을 낸다(소스 무수정 DB 스왑의 핵심 시나리오).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_sql_dialect.py -q
"""

import graph_rag as g
from sql_dialect import dialect_for_connection, get_dialect


# ── tsql: 리팩터 전 인라인과 동일 문자열 (회귀 앵커) ─────────────────────────

def test_tsql_renders_match_legacy_inline_strings():
    d = get_dialect("tsql")
    assert d.char8_cutoff(30) == "CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112)"
    assert d.char8_today() == "CONVERT(CHAR(8), GETDATE(), 112)"
    assert d.datetime_cutoff(7) == "DATEADD(DAY, -7, GETDATE())"
    assert d.cast_bigint("R.MBR_NO") == "TRY_CAST(R.MBR_NO AS BIGINT)"
    assert d.concat("R.CAMP_ID", "':'", "R.CAMP_EXEC_NO") == "CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)"
    assert d.char8_valid("B.REG_DT") == "B.REG_DT IS NOT NULL AND LEN(B.REG_DT) = 8"
    assert d.parse_char8("(SELECT MAX(REG_DT) FROM CRM_MB_BASEINFO WHERE LEN(REG_DT) = 8)") == (
        "CONVERT(DATE, (SELECT MAX(REG_DT) FROM CRM_MB_BASEINFO WHERE LEN(REG_DT) = 8), 112)"
    )
    assert d.nolock_hint() == " WITH(NOLOCK)"


def test_tsql_coalesce_uses_isnull_convention():
    # 실CRM 설정(valid_campaign_condition)의 ISNULL 관례와 일치.
    assert get_dialect("tsql").coalesce("ZC.CANCEL_YN", "'N'") == "ISNULL(ZC.CANCEL_YN, 'N')"


# ── mysql/postgres: 같은 논리, 다른 엔진 문법 ───────────────────────────────

def test_mysql_renders_engine_syntax():
    d = get_dialect("mysql")
    assert d.char8_cutoff(30) == "DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 30 DAY), '%Y%m%d')"
    assert d.datetime_cutoff(7) == "DATE_SUB(NOW(), INTERVAL 7 DAY)"
    assert d.cast_bigint("R.MBR_NO") == "CAST(R.MBR_NO AS SIGNED)"
    assert d.str_len("REG_DT") == "CHAR_LENGTH(REG_DT)"
    assert d.nolock_hint() == ""  # NOLOCK 미지원 엔진은 힌트 없음


def test_postgres_renders_engine_syntax():
    d = get_dialect("postgres")
    assert d.char8_cutoff(30) == "TO_CHAR((NOW() - INTERVAL '30 day'), 'YYYYMMDD')"
    assert d.cast_bigint("R.MBR_NO") == "CAST(R.MBR_NO AS BIGINT)"
    assert d.nolock_hint() == ""


def test_dialect_aliases_and_unknown_fallback():
    assert get_dialect("mssql").name == "tsql"
    assert get_dialect("mariadb").name == "mysql"
    assert get_dialect("postgresql").name == "postgres"
    assert get_dialect(None).name == "ansi"
    assert get_dialect("oracle").name == "ansi"  # 미지원 이름은 ANSI 기본으로


def test_dialect_for_connection_mapping():
    assert dialect_for_connection("CRMDW").name == "tsql"
    assert dialect_for_connection("CRMAN").name == "tsql"
    assert dialect_for_connection("quadmax_sdz").name == "mysql"
    assert dialect_for_connection("unknown_db").name == "ansi"


# ── graph_rag 빌더 통합: 방언 스왑 시 소스 무수정으로 출력이 바뀐다 ──────────

def test_member_dialect_defaults_to_tsql_and_builders_unchanged():
    assert g._member_dialect().name == "tsql"
    # 대표 빌더 출력이 리팩터 전과 동일해야 한다(기존 회귀 스위트의 앵커와 같은 문자열).
    assert g._member_activity_predicate(30) == (
        "(B.LAST_LOGIN_DATE IS NOT NULL AND B.LAST_LOGIN_DATE <= "
        "CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112))"
    )


def test_member_builders_follow_swapped_dialect():
    # DB 스왑 시나리오: 방언만 갈아끼우면 같은 빌더가 MySQL 문법을 낸다(소스 무수정).
    original = g._MEMBER_DIALECT
    g._MEMBER_DIALECT = get_dialect("mysql")
    try:
        predicate = g._member_activity_predicate(30)
        assert "DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 30 DAY), '%Y%m%d')" in predicate
        assert "GETDATE" not in predicate and "CONVERT" not in predicate
    finally:
        g._MEMBER_DIALECT = original
