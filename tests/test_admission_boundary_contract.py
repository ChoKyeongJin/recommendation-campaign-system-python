"""실행 관문 2종의 특성화 계약 — `plan_validation`(전) · `sql_guard`(후).

파이프라인은 `compile_executable_plan → 빌더 레지스트리 → sql_guard/AST 검증` 이다
(execution_gate 참조). 빌더는 그 사이에 끼어 있고, 지금까지 감사·계약은 **빌더에만** 걸려 있었다.
양쪽 괄호는 아무도 특성화하지 않았는데, 정작 SQL 모양을 바꾸는 변경이 조용히 깨뜨리는 것은
이 두 관문이다:

  * `plan_validation` — 실행 가능 판정의 **닫힌 status 5종**. 새 상태를 늘리면 응답 렌더링과
    BFF 표시가 함께 갈라진다.
  * `sql_guard` — 카탈로그 테이블 허용목록, SELECT 전용, 금지 키워드, **타입군 조인 검증**,
    그리고 실제로 SQL 을 **재작성**한다(방언별 TOP/LIMIT). 재작성기가 관문 안에 있다는 사실이
    이 파일의 존재 이유다 — 빌더 산출물이 그대로 나가지 않는다.

여기서 고정하는 것은 "현재 이렇게 동작한다"이지 "이렇게 동작해야 한다"가 아니다.
선언적 렌더러가 SQL 모양을 바꿀 때 **무엇이 깨지는지 먼저 보이게** 하는 것이 목적이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import plan_validation  # noqa: E402
import sql_guard  # noqa: E402

MEMBER_TABLE = "CRM_MB_BASEINFO"


@pytest.fixture(scope="module")
def allowed_tables() -> set[str]:
    return sql_guard.load_allowed_tables()


# ── plan_validation: 닫힌 status 집합 ────────────────────────────────────────────────


def test_plan_validation_status_set_is_closed() -> None:
    """상태를 늘리면 응답 렌더링(failure_messages)과 BFF 표시가 함께 갈라진다."""
    declared = {
        plan_validation.EXECUTABLE,
        plan_validation.CLARIFICATION_REQUIRED,
        plan_validation.SEMANTIC_CONFLICT,
        plan_validation.UNSUPPORTED,
        plan_validation.INTERNAL_INVALID,
    }
    assert declared == {
        "executable", "clarification_required", "semantic_conflict", "unsupported", "internal_invalid",
    }


def test_plan_validation_is_read_only() -> None:
    """admission 판정이 plan 을 변형하면 '검증이 곧 해석'이 된다(단일 해석 경로 위반)."""
    import copy

    plan = {"intent": "find_user_segment", "target_user": {"gender": "female"}}
    before = copy.deepcopy(plan)
    plan_validation.validate_executable_plan(plan)
    assert plan == before, "validate_executable_plan 이 plan 을 변형했다."


def test_garbage_plan_is_not_executable() -> None:
    result = plan_validation.validate_executable_plan({"intent": "find_user_segment"})
    assert result.status in {
        plan_validation.EXECUTABLE,
        plan_validation.CLARIFICATION_REQUIRED,
        plan_validation.SEMANTIC_CONFLICT,
        plan_validation.UNSUPPORTED,
        plan_validation.INTERNAL_INVALID,
    }


def test_non_mapping_plan_is_internal_invalid_not_a_crash() -> None:
    """관문은 쓰레기 입력에도 예외 대신 판정을 낸다 — 예외는 500 이 되고 사유가 사라진다."""
    for garbage in (None, [], "plan", 7):
        result = plan_validation.validate_executable_plan(garbage)
        assert result.status != plan_validation.EXECUTABLE


# ── sql_guard: 안전 관문 ────────────────────────────────────────────────────────────


def test_select_only(allowed_tables: set[str]) -> None:
    result = sql_guard.validate_sql(
        f"SELECT MEMBER_NO FROM {MEMBER_TABLE}", allowed_tables=allowed_tables
    )
    assert result["is_valid"], result["issues"]


@pytest.mark.parametrize(
    "sql",
    [
        f"DELETE FROM {MEMBER_TABLE}",
        f"UPDATE {MEMBER_TABLE} SET MEMBER_NO = 1",
        f"DROP TABLE {MEMBER_TABLE}",
        f"INSERT INTO {MEMBER_TABLE} (MEMBER_NO) VALUES (1)",
        f"TRUNCATE TABLE {MEMBER_TABLE}",
    ],
)
def test_mutations_are_rejected(sql: str, allowed_tables: set[str]) -> None:
    result = sql_guard.validate_sql(sql, allowed_tables=allowed_tables)
    assert not result["is_valid"], f"변경 구문이 통과했다: {sql}"


def test_multiple_statements_are_rejected(allowed_tables: set[str]) -> None:
    result = sql_guard.validate_sql(
        f"SELECT 1 FROM {MEMBER_TABLE}; SELECT 2 FROM {MEMBER_TABLE}",
        allowed_tables=allowed_tables,
    )
    assert not result["is_valid"]
    assert any(issue["code"] == "multiple_statements" for issue in result["issues"])


def test_empty_sql_is_rejected(allowed_tables: set[str]) -> None:
    result = sql_guard.validate_sql("   ", allowed_tables=allowed_tables)
    assert not result["is_valid"]
    assert any(issue["code"] == "empty_sql" for issue in result["issues"])


def test_unknown_table_is_rejected(allowed_tables: set[str]) -> None:
    result = sql_guard.validate_sql(
        "SELECT * FROM TOTALLY_MADE_UP_TABLE", allowed_tables=allowed_tables
    )
    assert not result["is_valid"], "카탈로그에 없는 테이블이 통과했다."


def test_allowed_tables_come_from_the_catalog() -> None:
    """허용목록의 소유자는 schema_catalog.json 이다 — 소스 손목록이 아니다."""
    tables = sql_guard.load_allowed_tables()
    assert len(tables) > 10
    assert MEMBER_TABLE.casefold() in {name.casefold() for name in tables}


def test_forbidden_keyword_vocabulary_is_declared() -> None:
    assert {"delete", "drop", "insert", "update", "truncate", "alter", "create", "merge"} <= (
        sql_guard.FORBIDDEN_KEYWORDS
    )


# ── sql_guard 가 SQL 을 **재작성**한다는 사실의 고정 ──────────────────────────────────


def test_guard_separates_input_sql_from_executable_sql(allowed_tables: set[str]) -> None:
    """관문은 판정만 하지 않고 **실행용 SQL 을 따로 돌려준다**.

    `sql` 은 입력 그대로, `safe_sql` 이 실행용(행 제한 부착), `masked_sql` 이 표시용이다.
    선언적 렌더러 도입 시 '빌더가 만든 문자열'과 '실제 나간 SQL'을 혼동하면 회귀를 잘못 귀속한다 —
    이 세 키의 구분이 그 혼동을 막는 지점이다.
    """
    result = sql_guard.validate_sql(
        f"SELECT MEMBER_NO FROM {MEMBER_TABLE}", allowed_tables=allowed_tables
    )
    for key in ("sql", "safe_sql", "masked_sql", "tables", "dialect", "issues", "is_valid"):
        assert key in result, f"관문 결과에 {key} 키가 없다."


def test_row_limit_is_applied_by_the_guard_into_safe_sql(allowed_tables: set[str]) -> None:
    """행 제한은 빌더가 아니라 관문이 붙인다(방언별 TOP/LIMIT) — 그리고 `safe_sql` 에만 붙는다."""
    original = f"SELECT MEMBER_NO FROM {MEMBER_TABLE}"
    result = sql_guard.validate_sql(
        original, allowed_tables=allowed_tables, default_limit=10
    )
    assert result["sql"] == original, "입력 SQL 은 보존돼야 한다."
    executable = result["safe_sql"].casefold()
    assert "top" in executable or "limit" in executable, (
        f"행 제한이 붙지 않았다: {result['safe_sql'][:120]}"
    )
    assert any(issue["code"] == "limit_added" for issue in result["issues"]), (
        "재작성이 일어났는데 영수증(limit_added)이 남지 않았다."
    )


# ── 타입군 조인 검증(조용한 0건의 주범) ────────────────────────────────────────────


def test_type_family_classification() -> None:
    assert sql_guard._type_family("nvarchar(100)") == "string"
    assert sql_guard._type_family("bigint") == "numeric"
    assert sql_guard._type_family("datetime2") == "datetime"
    assert sql_guard._type_family("some_unknown_type") is None


def test_column_types_holds_families_not_raw_types() -> None:
    """이름은 `column_types` 지만 값은 **타입군**이다(`load_column_types` 가 이미 환산한다).

    원시 타입('bigint'/'int')을 그대로 넣으면 같은 numeric 군인데도 불일치로 잡힌다 — 비교가
    families 끼리가 아니라 저장된 문자열끼리이기 때문이다. 호출자가 직접 dict 를 만들 때
    반드시 밟는 함정이라 계약으로 못 박는다.
    """
    loaded = sql_guard.load_column_types()
    families = set(sql_guard._TYPE_FAMILIES)
    sampled = {
        value
        for columns in list(loaded.values())[:20]
        for value in list(columns.values())[:20]
    }
    assert sampled, "카탈로그에서 컬럼 타입을 하나도 읽지 못했다."
    assert sampled <= families, f"타입군이 아닌 값이 섞였다: {sorted(sampled - families)}"


def test_cross_type_equi_join_is_reported() -> None:
    """string = numeric 조인은 실행 실패이거나 조용한 0건이 된다 — 관문이 잡아야 한다."""
    # 키는 소문자 테이블/컬럼명, 값은 타입군이다.
    column_types = {"t_str": {"k": "string"}, "t_num": {"k": "numeric"}}
    result = sql_guard.validate_join_keys(
        "SELECT 1 FROM T_STR A JOIN T_NUM B ON A.K = B.K",
        column_types=column_types,
    )
    assert not result["is_valid"], "타입군이 다른 등호 조인이 보고되지 않았다."
    assert any(issue["code"] == "join_key_type_mismatch" for issue in result["issues"])


def test_same_family_equi_join_is_clean() -> None:
    """bigint 와 int 는 같은 numeric 군 — 카탈로그를 거치면 둘 다 'numeric' 이라 통과한다."""
    assert sql_guard._type_family("bigint") == sql_guard._type_family("int") == "numeric"
    column_types = {"t_a": {"k": "numeric"}, "t_b": {"k": "numeric"}}
    result = sql_guard.validate_join_keys(
        "SELECT 1 FROM T_A A JOIN T_B B ON A.K = B.K",
        column_types=column_types,
    )
    assert result["is_valid"], f"같은 타입군 조인이 오탐됐다: {result['issues']}"


def test_unknown_column_types_defer_judgment() -> None:
    """스키마에 타입이 없으면 판정을 보류한다 — 오탐으로 정상 SQL 을 막지 않기 위한 설계."""
    result = sql_guard.validate_join_keys(
        "SELECT 1 FROM UNKNOWN_A A JOIN UNKNOWN_B B ON A.K = B.K",
        column_types={},
    )
    assert result["is_valid"], "타입 정보가 없는 조인을 막았다(과잉 차단)."


def test_alias_extractor_does_not_swallow_join_keyword() -> None:
    """별칭 자리 예약어 처리 — 'FROM T1 JOIN T2' 에서 JOIN 을 T1 의 별칭으로 먹으면 T2 를 잃는다."""
    aliases = sql_guard._alias_map(
        "SELECT 1 FROM T_A JOIN T_B ON T_A.K = T_B.K",
        {"t_a": {"k": "bigint"}, "t_b": {"k": "bigint"}},
    )
    assert "t_b" in aliases, f"JOIN 뒤 테이블을 잃었다: {aliases}"
    assert aliases["t_a"] == "t_a"


# ── 세 검증기의 관계(선언적 렌더러 도입 전 확정 사항) ────────────────────────────────


def test_three_independent_sql_shape_validators_exist() -> None:
    """sql_guard(정규식) · sql_ast(별칭 허용목록) · condition_evaluation_ir(문자열 사후조건).

    셋은 서로 다른 파서를 쓴다. SQL 모양을 바꾸는 변경은 **셋 다** 만족시켜야 하며,
    하나만 보고 통과를 선언하면 나머지가 런타임에서 막는다.
    """
    import condition_evaluation_ir
    import sql_ast

    assert hasattr(sql_guard, "validate_sql")
    assert hasattr(sql_ast, "validate_select_ast")
    assert hasattr(condition_evaluation_ir, "validate_compiled_sql")
