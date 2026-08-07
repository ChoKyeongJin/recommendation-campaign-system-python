"""복합 집계 lowering 의 **실행 결과** 계약(픽스처 DB).

golden SQL 은 '어떤 문자열이 나오는가'만 잰다. 뜻이 맞는지는 그 SQL 을 **돌려 봐야** 안다 —
특히 이 축들의 실패 모드가 전부 '문법은 맞고 값이 다르다'이기 때문이다(행당 평균 vs 캠페인
분모 평균, 최신 스냅샷 vs 전체 월, 구분자 결합 키 충돌).

실행 엔진은 stdlib ``sqlite3`` 이고, 카탈로그는 이 파일이 소유하는 합성 선언이다. 실DB
카탈로그를 쓰지 않는 이유는 그 바인딩이 T-SQL 전용 식(``TRY_CAST``/``ISNULL``)을 담고 있어
어떤 이식 엔진에서도 실행되지 않기 때문이다. 여기서 재는 것은 물리 바인딩이 아니라
**lowering 의 산술·집계·grain 의미**다.
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import member_scalar_metrics  # noqa: E402
import resolved_semantic_catalog  # noqa: E402
import sql_dialect  # noqa: E402

RUNTIME_CONFIG = {
    "sources": {
        "campaign_buy": {
            "table": "RESPONSE",
            "alias": "R",
            "subject_key": "MEMBER_NO",
            "event_subject_key": "MBR_NO",
            "time_column": "ORDER_DATE",
            "time_format": "char8",
            "binding": "fact_table",
            # 취소·환불 제외는 소스 정의의 일부다(조건마다 다시 적지 않는다).
            "extra_predicates": [
                "{alias}.BUY_RSPN_YN = 'Y'",
                "COALESCE({alias}.CANCEL_YN, 'N') = 'N'",
            ],
            "label": "캠페인 구매 반응",
        },
        "member_snapshot": {
            "table": "SNAPSHOT",
            "alias": "MS",
            "subject_key": "MEMBER_NO",
            "event_subject_key": "MEMBER_NO",
            "time_column": "YYYYMM",
            "time_format": "char6",
            "binding": "fact_table",
            "extra_predicates": ["{alias}.YYYYMM = (SELECT MAX(YYYYMM) FROM SNAPSHOT)"],
            "label": "회원 월 스냅샷",
        },
    },
    "fields": {
        "campaign_buy.amount": {
            "source": "campaign_buy",
            "column": "BUY_AMT",
            "data_type": "number",
        },
        "campaign_buy.campaign_id": {
            "source": "campaign_buy",
            "column": "CAMP_ID",
            "data_type": "string",
        },
        "campaign_buy.execution_no": {
            "source": "campaign_buy",
            "column": "CAMP_EXEC_NO",
            "data_type": "string",
        },
        "member_snapshot.value": {
            "source": "member_snapshot",
            "column": "BUY_CYCLE",
            "data_type": "number",
            "nullable": True,
            "unit": "day",
        },
    },
    "metrics": {
        "member_snapshot_buy_cycle": {
            "source": "member_snapshot",
            "kind": "member_scalar",
            "cardinality": "scalar",
            "grain": "subject",
            "expression_field": "member_snapshot.value",
            "value_type": "number",
            "allowed_operators": ["=", "!=", ">", ">=", "<", "<="],
            "null_behavior": "exclude",
            "zero_row_behavior": "exclude",
            "required_capabilities": ["metric.member_scalar"],
            "label": "구매주기",
        }
    },
}

SCHEMA = """
CREATE TABLE MEMBER (MEMBER_NO INTEGER PRIMARY KEY);
CREATE TABLE RESPONSE (
    MBR_NO INTEGER,
    CAMP_ID TEXT,
    CAMP_EXEC_NO TEXT,
    BUY_AMT REAL,
    BUY_RSPN_YN TEXT,
    CANCEL_YN TEXT,
    ORDER_DATE TEXT
);
CREATE TABLE SNAPSHOT (MEMBER_NO INTEGER, YYYYMM TEXT, BUY_CYCLE INTEGER);
"""

# (회원, 캠페인, 실행회차, 금액, 구매반응, 취소, 주문일)
RESPONSES: tuple[tuple[int, str | None, str | None, float, str, str | None, str], ...] = (
    # m2 구매 1회
    (2, "C1", "1", 100.0, "Y", "N", "20260301"),
    # m3 같은 캠페인 실행에 주문 여러 건 → 분모 1
    (3, "C1", "1", 100.0, "Y", "N", "20260301"),
    (3, "C1", "1", 200.0, "Y", "N", "20260302"),
    (3, "C1", "1", 300.0, "Y", "N", "20260302"),
    # m4 서로 다른 캠페인 → 분모 2
    (4, "C1", "1", 100.0, "Y", "N", "20260301"),
    (4, "C2", "1", 200.0, "Y", "N", "20260301"),
    # m5 같은 주문의 중복 행(완전 동일) → 분모 1, 분자는 두 행 모두 더한다
    (5, "C1", "1", 150.0, "Y", "N", "20260301"),
    (5, "C1", "1", 150.0, "Y", "N", "20260301"),
    # m6 같은 캠페인 ID 의 다른 실행 회차 → 분모 2
    (6, "C1", "1", 100.0, "Y", "N", "20260301"),
    (6, "C1", "2", 300.0, "Y", "N", "20260301"),
    # m7 distinct 키 일부가 NULL → NULL 조합도 하나의 키로 센다(분모 2)
    (7, None, "1", 100.0, "Y", "N", "20260301"),
    (7, "C1", "1", 300.0, "Y", "N", "20260301"),
    # m8 취소·환불 행은 소스 정의가 제외한다(남는 건 100 한 건, 분모 1)
    (8, "C1", "1", 100.0, "Y", "N", "20260301"),
    (8, "C2", "1", 900.0, "Y", "Y", "20260301"),
    (8, "C3", "1", 900.0, "N", "N", "20260301"),
    # m9 구매금액 0 인 주문 → 분모에는 들어가고 분자에는 0 을 더한다
    (9, "C1", "1", 200.0, "Y", "N", "20260301"),
    (9, "C2", "1", 0.0, "Y", "N", "20260301"),
    # m10 소수점 금액
    (10, "C1", "1", 100.5, "Y", "N", "20260301"),
    (10, "C2", "1", 99.5, "Y", "N", "20260301"),
    # m11 구분자 결합이면 충돌하는 값('A:B'+'C' 와 'A'+'B:C' 가 둘 다 'A:B:C')
    (11, "A:B", "C", 100.0, "Y", "N", "20260301"),
    (11, "A", "B:C", 300.0, "Y", "N", "20260301"),
    # m12 같은 시각의 여러 구매(같은 캠페인) → 분모 1
    (12, "C1", "1", 50.0, "Y", "N", "20260301"),
    (12, "C1", "1", 50.0, "Y", "N", "20260301"),
    # m13 기간 경계: 시작일·종료일·구간 밖
    (13, "C1", "1", 100.0, "Y", "N", "20260301"),
    (13, "C2", "1", 100.0, "Y", "N", "20260331"),
    (13, "C3", "1", 900.0, "Y", "N", "20260401"),
)

# (회원, YYYYMM, 구매주기)
SNAPSHOTS: tuple[tuple[int, str, int | None], ...] = (
    # 최신 월(202603)에 30 이하 → 포함
    (2, "202602", 90),
    (2, "202603", 30),
    # 최신 월에 30 초과 → 제외(과거 월이 30 이하여도 스냅샷 고정이 그것을 막는다)
    (3, "202602", 10),
    (3, "202603", 45),
    # 최신 월 값이 NULL → 비교가 UNKNOWN 이라 제외
    (4, "202603", None),
    # 최신 월 행 자체가 없음 → EXISTS 거짓이라 제외
    (5, "202602", 5),
    # 경계값 정확히 30 → 포함(<= 이므로)
    (6, "202603", 30),
)

MEMBER_IDS = tuple(range(1, 14))


@pytest.fixture(scope="module")
def catalog() -> resolved_semantic_catalog.ResolvedSemanticCatalog:
    return resolved_semantic_catalog.resolve_semantic_catalog(
        registry={},
        runtime_config=RUNTIME_CONFIG,
        subject=event_compiler.SubjectSpec(
            table="MEMBER", alias="B", key="MEMBER_NO", name="subject"
        ),
    )


@pytest.fixture(scope="module")
def connection() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.executescript(SCHEMA)
    database.executemany(
        "INSERT INTO MEMBER (MEMBER_NO) VALUES (?)",
        [(member_id,) for member_id in MEMBER_IDS],
    )
    database.executemany(
        "INSERT INTO RESPONSE "
        "(MBR_NO, CAMP_ID, CAMP_EXEC_NO, BUY_AMT, BUY_RSPN_YN, CANCEL_YN, ORDER_DATE) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        RESPONSES,
    )
    database.executemany(
        "INSERT INTO SNAPSHOT (MEMBER_NO, YYYYMM, BUY_CYCLE) VALUES (?, ?, ?)",
        SNAPSHOTS,
    )
    database.commit()
    try:
        yield database
    finally:
        database.close()


def _compile(expression: event_ir.Condition, catalog, *, optimize: bool = True) -> str:
    context = catalog.compile_context(
        literals=True,
        dialect=sql_dialect.get_dialect("ansi"),
        optimize_aggregate_membership=optimize,
    )
    return event_compiler.compile_expression(expression, context=context).sql


def _members(connection: sqlite3.Connection, predicate: str) -> list[int]:
    rows = connection.execute(
        f"SELECT B.MEMBER_NO FROM MEMBER B WHERE {predicate} ORDER BY B.MEMBER_NO"
    ).fetchall()
    return [row[0] for row in rows]


def _campaign_average_scalar(window: event_ir.TimeWindow | None = None) -> event_ir.Scalar:
    """분자 SUM * 1.0 / NULLIF(분모 다중 컬럼 COUNT DISTINCT, 0)."""

    relation: event_ir.Relation = event_ir.Source(name="campaign_buy")
    if window is not None:
        relation = event_ir.Filter(
            relation=relation,
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef("campaign_buy.occurred_at"), window=window
            ),
        )
    return event_ir.Arithmetic(
        operator="/",
        left=event_ir.Arithmetic(
            operator="*",
            left=event_ir.Aggregate(
                function="sum",
                relation=relation,
                expression=event_ir.FieldRef("campaign_buy.amount"),
            ),
            right=event_ir.Literal(value=1.0),
        ),
        right=event_ir.NullIf(
            expression=event_ir.Aggregate(
                function="count",
                relation=relation,
                expression=event_ir.Tuple(
                    items=(
                        event_ir.FieldRef("campaign_buy.campaign_id"),
                        event_ir.FieldRef("campaign_buy.execution_no"),
                    )
                ),
                distinct=True,
            ),
            value=event_ir.Literal(value=0),
        ),
    )


def _campaign_average_values(connection, catalog) -> dict[int, float | None]:
    """회원별 캠페인 분모 평균(비교 없이 값 자체)."""

    scalar = _campaign_average_scalar()
    context = catalog.compile_context(
        literals=True, dialect=sql_dialect.get_dialect("ansi")
    )
    sql = event_compiler.compile_scalar(scalar, context)
    rows = connection.execute(
        f"SELECT B.MEMBER_NO, {sql} FROM MEMBER B ORDER BY B.MEMBER_NO"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


# ── 캠페인당 평균 구매금액 ─────────────────────────────────────────────────────────


def test_campaign_average_values_match_the_declared_meaning(connection, catalog) -> None:
    """SUM / 서로 다른 캠페인 실행 수 — 행 수로 나누지 않는다."""

    values = _campaign_average_values(connection, catalog)
    assert values == {
        1: None,           # 반응 행이 없다 → 분모 0 → NULL
        2: 100.0,          # 100 / 1
        3: 600.0,          # (100+200+300) / 1  ← 행당 평균이면 200 이다
        4: 150.0,          # (100+200) / 2
        5: 300.0,          # (150+150) / 1  중복 행도 분자에 더한다
        6: 200.0,          # (100+300) / 2  같은 캠페인 다른 회차
        7: 200.0,          # (100+300) / 2  NULL 조합도 하나의 키
        8: 100.0,          # 취소·환불 제외 후 100 / 1
        9: 100.0,          # (200+0) / 2   금액 0 도 분모에 든다
        10: 100.0,         # (100.5+99.5) / 2  소수 나눗셈
        11: 200.0,         # (100+300) / 2  구분자 충돌 없는 행 값
        12: 100.0,         # (50+50) / 1   같은 시각 여러 구매
        13: 1100.0 / 3,    # (100+100+900) / 3
    }


def test_campaign_average_is_not_a_row_average(connection, catalog) -> None:
    """같은 데이터에서 AVG(BUY_AMT) 와 값이 다르다 — 그것이 이 축이 존재하는 이유다."""

    values = _campaign_average_values(connection, catalog)
    row_averages = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT B.MEMBER_NO, (SELECT AVG(R.BUY_AMT) FROM RESPONSE R "
            "WHERE R.MBR_NO = B.MEMBER_NO AND R.BUY_RSPN_YN = 'Y' "
            "AND COALESCE(R.CANCEL_YN, 'N') = 'N') FROM MEMBER B"
        ).fetchall()
    }
    assert values[3] == 600.0 and row_averages[3] == 200.0
    assert values[5] == 300.0 and row_averages[5] == 150.0
    assert values[12] == 100.0 and row_averages[12] == 50.0


def test_zero_denominator_yields_null_and_excludes_the_member(connection, catalog) -> None:
    """분모 0(반응 없음)은 NULL 이고, NULL 비교는 UNKNOWN 이라 대상에서 빠진다."""

    values = _campaign_average_values(connection, catalog)
    assert values[1] is None
    predicate = _compile(
        event_ir.Comparison(
            operator=">=",
            left=_campaign_average_scalar(),
            right=event_ir.Literal(value=0),
        ),
        catalog,
    )
    assert 1 not in _members(connection, predicate)


def test_decimal_division_is_not_integer_division(connection, catalog) -> None:
    """`* 1.0` 이 정수 나눗셈을 막는다 — 없으면 100/3 이 33 이 된다."""

    values = _campaign_average_values(connection, catalog)
    assert values[13] == pytest.approx(float(Decimal(1100) / Decimal(3)))
    assert values[13] != 366  # 정수 나눗셈이면 366 이다


def test_multi_column_distinct_beats_delimiter_concatenation(connection, catalog) -> None:
    """구분자 결합 키는 서로 다른 키를 같은 문자열로 만든다 — 행 값은 그러지 않는다.

    m11 은 ``('A:B','C')`` 와 ``('A','B:C')`` 다. 결합 문자열은 둘 다 ``'A:B:C'`` 이므로
    분모가 1 이 되어 평균이 두 배로 부푼다. 이것이 기존 표현과 **의도적으로 다른** 유일한
    자리이며, 그 차이가 곧 이 IR 확장의 이유다.
    """

    values = _campaign_average_values(connection, catalog)
    concatenated = connection.execute(
        "SELECT (SELECT SUM(R.BUY_AMT) * 1.0 / "
        "COUNT(DISTINCT COALESCE(R.CAMP_ID,'') || ':' || COALESCE(R.CAMP_EXEC_NO,'')) "
        "FROM RESPONSE R WHERE R.MBR_NO = B.MEMBER_NO AND R.BUY_RSPN_YN = 'Y' "
        "AND COALESCE(R.CANCEL_YN, 'N') = 'N') FROM MEMBER B WHERE B.MEMBER_NO = 11"
    ).fetchone()[0]
    assert values[11] == 200.0   # (100+300) / 2  서로 다른 키 둘
    assert concatenated == 400.0  # (100+300) / 1  충돌해서 하나로 세었다


def test_legacy_concatenated_key_agrees_everywhere_else(connection, catalog) -> None:
    """충돌 사례를 빼면 기존 표현과 결과가 같다(이관이 값을 바꾸지 않았다는 증거)."""

    values = _campaign_average_values(connection, catalog)
    legacy = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT B.MEMBER_NO, (SELECT SUM(R.BUY_AMT) * 1.0 / "
            "COUNT(DISTINCT COALESCE(R.CAMP_ID,'') || ':' || COALESCE(R.CAMP_EXEC_NO,'')) "
            "FROM RESPONSE R WHERE R.MBR_NO = B.MEMBER_NO AND R.BUY_RSPN_YN = 'Y' "
            "AND COALESCE(R.CANCEL_YN, 'N') = 'N') FROM MEMBER B"
        ).fetchall()
    }
    for member_id in MEMBER_IDS:
        if member_id == 11:
            continue
        if values[member_id] is None:
            assert legacy[member_id] is None
        else:
            assert values[member_id] == pytest.approx(legacy[member_id])


def test_threshold_comparison_selects_the_expected_members(connection, catalog) -> None:
    predicate = _compile(
        event_ir.Comparison(
            operator=">=",
            left=_campaign_average_scalar(),
            right=event_ir.Literal(value=200),
        ),
        catalog,
    )
    # 600 · 300 · 200 · 200 · 200 · 366.67 이 임계를 넘고, 100 인 회원들은 빠진다.
    assert _members(connection, predicate) == [3, 5, 6, 7, 11, 13]


def test_period_boundaries_are_half_open(connection, catalog) -> None:
    """[start, end_exclusive) — 시작일 포함, 종료일 다음날 제외.

    m13 은 3월 1일·3월 31일·4월 1일에 각각 반응이 있다. 3월 창은 앞 둘만 담아야 한다
    (분모 2, 분자 200 → 100). 4월 1일 행이 새면 분자가 900 만큼 커진다.
    """

    import datetime

    window = event_ir.AbsoluteInterval(
        start=datetime.date(2026, 3, 1), end_exclusive=datetime.date(2026, 4, 1)
    )
    context = catalog.compile_context(
        literals=True, dialect=sql_dialect.get_dialect("ansi")
    )
    sql = event_compiler.compile_scalar(_campaign_average_scalar(window), context)
    value = connection.execute(
        f"SELECT {sql} FROM MEMBER B WHERE B.MEMBER_NO = 13"
    ).fetchone()[0]
    assert value == 100.0


def test_period_window_does_not_split_numerator_from_denominator(
    connection, catalog
) -> None:
    """기간이 한쪽에만 걸리면 값이 조용히 달라진다 — 같은 관계를 물려받는지 결과로 잰다."""

    import datetime

    window = event_ir.AbsoluteInterval(
        start=datetime.date(2026, 4, 1), end_exclusive=datetime.date(2026, 5, 1)
    )
    context = catalog.compile_context(
        literals=True, dialect=sql_dialect.get_dialect("ansi")
    )
    sql = event_compiler.compile_scalar(_campaign_average_scalar(window), context)
    value = connection.execute(
        f"SELECT {sql} FROM MEMBER B WHERE B.MEMBER_NO = 13"
    ).fetchone()[0]
    assert value == 900.0  # 4월 행 하나만: 900 / 1


# ── 물리 lowering 동치(상관 스칼라 ↔ 2단 집계 semi-join) ──────────────────────────
#
# 낮춘 SQL 은 팩트를 한 번만 읽는다. 그 대가로 두 가지를 증명해야 한다.
#
#   1. 이벤트가 없는 회원 — 상관 모양은 값을 주지만(0/NULL) 집합형에는 그 그룹이 없다.
#   2. SUM 의 NULL — 안쪽 부분합이 NULL 인 분할이 섞여도 바깥 합이 전체 합과 같아야 한다.
#
# 아래는 그 둘을 **같은 데이터에서 두 SQL 을 돌려** 잰다. 문자열 비교로는 잡히지 않는 축이다.


def _campaign_average_condition(operator: str, threshold: float) -> event_ir.Condition:
    return event_ir.Comparison(
        operator=operator,
        left=_campaign_average_scalar(),
        right=event_ir.Literal(value=threshold),
    )


@pytest.mark.parametrize("threshold", [0, 100, 150, 200, 300, 600, 1000])
def test_lowering_selects_exactly_the_same_members(
    threshold: int, connection, catalog
) -> None:
    """임계값을 훑어도 두 물리 계획이 같은 회원 집합을 뜻한다."""

    condition = _campaign_average_condition(">=", threshold)
    lowered = _compile(condition, catalog)
    correlated = _compile(condition, catalog, optimize=False)

    # 정말 다른 두 계획을 재고 있는지부터 확인한다(둘 다 상관이면 이 대조는 공허하다).
    assert " IN (" in lowered and "GROUP BY" in lowered
    assert "R.MBR_NO = B.MEMBER_NO" not in lowered
    assert "R.MBR_NO = B.MEMBER_NO" in correlated

    assert _members(connection, lowered) == _members(connection, correlated)


@pytest.mark.parametrize("operator", [">=", ">", "<=", "<", "=", "!="])
def test_lowering_is_equivalent_for_every_comparison_operator(
    operator: str, connection, catalog
) -> None:
    """0 분모 NULL 가드 덕분에 반응이 없는 회원은 **어느 연산자에서도** 참이 아니다.

    그래서 이 식은 ``<=`` · ``!=`` 처럼 보통은 집합형으로 낮출 수 없는 연산자에서도 낮출 수
    있다. 낮출 수 있다는 판정과 실제 결과가 어긋나지 않는지 결과집합으로 잰다.
    """

    condition = _campaign_average_condition(operator, 200)
    lowered = _compile(condition, catalog)
    assert " IN (" in lowered
    assert _members(connection, lowered) == _members(
        connection, _compile(condition, catalog, optimize=False)
    )
    assert 1 not in _members(connection, lowered)  # 반응 행이 없는 회원


def test_lowering_keeps_the_multi_column_distinct_key_apart(connection, catalog) -> None:
    """m11 의 ``('A:B','C')`` 와 ``('A','B:C')`` 는 낮춘 뒤에도 서로 다른 키다(분모 2).

    안쪽 ``GROUP BY`` 가 구분자 결합으로 바뀌면 분모가 1 이 되어 평균이 200 → 400 으로 부푼다.
    그러면 임계 300 에서 m11 이 조용히 대상에 들어온다.
    """

    assert 11 in _members(connection, _compile(_campaign_average_condition(">=", 200), catalog))
    assert 11 not in _members(connection, _compile(_campaign_average_condition(">=", 300), catalog))


# (회원, 캠페인, 실행회차, 금액) — 금액 NULL 이 섞인 분할을 만든다.
NULL_AMOUNT_RESPONSES: tuple[tuple[int, str, str, float | None], ...] = (
    # m1 한 캠페인의 모든 금액이 NULL → 안쪽 부분합이 NULL 인 분할 하나뿐
    (1, "C1", "1", None),
    (1, "C1", "1", None),
    # m2 한 캠페인은 값이 있고 다른 캠페인은 전부 NULL → 바깥 SUM 이 NULL 분할을 건너뛴다
    (2, "C1", "1", 100.0),
    (2, "C1", "1", None),
    (2, "C2", "1", None),
    # m3 모든 분할이 NULL → COALESCE 가 0 으로 접는다
    (3, "C1", "1", None),
    (3, "C2", "1", None),
    # m4 NULL 없음(대조군)
    (4, "C1", "1", 100.0),
    (4, "C2", "1", 300.0),
)


@pytest.fixture()
def null_amount_connection() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.executescript(SCHEMA)
    database.executemany(
        "INSERT INTO MEMBER (MEMBER_NO) VALUES (?)", [(index,) for index in range(1, 6)]
    )
    database.executemany(
        "INSERT INTO RESPONSE "
        "(MBR_NO, CAMP_ID, CAMP_EXEC_NO, BUY_AMT, BUY_RSPN_YN, CANCEL_YN, ORDER_DATE) "
        "VALUES (?, ?, ?, ?, 'Y', 'N', '20260301')",
        NULL_AMOUNT_RESPONSES,
    )
    database.commit()
    try:
        yield database
    finally:
        database.close()


def test_null_amounts_survive_the_two_level_sum_decomposition(
    null_amount_connection, catalog
) -> None:
    """SUM 분해의 동치 조건 — 부분합이 NULL 인 분할이 섞여도 전체 합이 달라지지 않는다.

    바깥 ``SUM`` 은 NULL 부분합을 건너뛰고, 원래 ``SUM`` 도 NULL 행을 건너뛴다. 두 결과가
    같다는 것이 2단 집계로 낮출 수 있는 이유이며, 여기서 값 자체로 확인한다.
    """

    context = catalog.compile_context(
        literals=True, dialect=sql_dialect.get_dialect("ansi")
    )
    scalar = event_compiler.compile_scalar(_campaign_average_scalar(), context)
    values = {
        row[0]: row[1]
        for row in null_amount_connection.execute(
            f"SELECT B.MEMBER_NO, {scalar} FROM MEMBER B ORDER BY B.MEMBER_NO"
        ).fetchall()
    }
    assert values == {
        1: 0.0,     # 금액이 전부 NULL → COALESCE 0, 분모 1
        2: 50.0,    # 100 / 2  (NULL 분할도 서로 다른 키로 센다)
        3: 0.0,     # 전부 NULL → 0 / 2
        4: 200.0,   # (100+300) / 2
        5: None,    # 반응 행 없음 → 분모 0 → NULL
    }


@pytest.mark.parametrize("threshold", [0, 50, 200])
def test_lowering_matches_the_correlated_shape_when_amounts_are_null(
    threshold: int, null_amount_connection, catalog
) -> None:
    condition = _campaign_average_condition(">=", threshold)
    lowered = _compile(condition, catalog)
    assert " IN (" in lowered
    assert _members(null_amount_connection, lowered) == _members(
        null_amount_connection, _compile(condition, catalog, optimize=False)
    )


# ── 회원별 스칼라 지표 ─────────────────────────────────────────────────────────────


def test_member_scalar_reads_only_the_latest_snapshot_row(connection, catalog) -> None:
    """최신 월 한 행만 읽는다 — 과거 월이 조건을 만족해도 대상이 되지 않는다."""

    predicate = _compile(
        member_scalar_metrics.lower_member_scalar_metric(
            catalog, "member_snapshot_buy_cycle", operator="<=", value=30
        ).expression,
        catalog,
    )
    # m2 최신 월 30 → 포함, m6 경계값 30 → 포함.
    # m3 은 과거 월이 10 이지만 최신 월이 45 라 제외, m4 는 NULL, m5 는 최신 월 행 없음.
    assert _members(connection, predicate) == [2, 6]


def test_member_scalar_null_and_missing_rows_are_both_excluded(connection, catalog) -> None:
    """NULL(값 없음)과 행 없음은 다른 상태지만 결말은 같다 — 둘 다 제외."""

    predicate = _compile(
        member_scalar_metrics.lower_member_scalar_metric(
            catalog, "member_snapshot_buy_cycle", operator=">=", value=0
        ).expression,
        catalog,
    )
    selected = _members(connection, predicate)
    assert 4 not in selected  # 최신 월 값이 NULL
    assert 5 not in selected  # 최신 월 행 자체가 없음
    assert 1 not in selected  # 스냅샷이 하나도 없음
    assert selected == [2, 3, 6]


def test_member_scalar_boundary_value_is_inclusive_for_lte(connection, catalog) -> None:
    inclusive = _compile(
        member_scalar_metrics.lower_member_scalar_metric(
            catalog, "member_snapshot_buy_cycle", operator="<=", value=30
        ).expression,
        catalog,
    )
    exclusive = _compile(
        member_scalar_metrics.lower_member_scalar_metric(
            catalog, "member_snapshot_buy_cycle", operator="<", value=30
        ).expression,
        catalog,
    )
    assert 6 in _members(connection, inclusive)
    assert 6 not in _members(connection, exclusive)
