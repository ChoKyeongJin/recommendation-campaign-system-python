"""시간 축의 낮춤 계약 — 시각 경계·자정 넘김·해상도·두 경계·미래 방향.

이 축의 위험은 전부 **조용한 오답**이다. 시각이 사라지면 조건이 하루 전체로 넓어지고, 경계일을
틀리면 가운데 날이 잘리고, 자정 넘김에서 AND/OR 를 뒤집으면 정반대 집합이 나온다. 그래서
여기서 고정하는 것은 "SQL 이 나왔다"가 아니라 **어느 술어가 어느 날에 걸리는가**다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import calendar_window  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import lowering_planner as planner  # noqa: E402
import semantic_diagnostics as sd  # noqa: E402
import sql_dialect  # noqa: E402

FIXED_TODAY = date(2026, 8, 8)
EVIDENCE = event_ir.Evidence("x", 0, 1)


@pytest.fixture(scope="module")
def context():
    return audience_runtime.resolve_audience_catalog().compile_context(
        dialect=sql_dialect.get_dialect("tsql"), literals=True
    )


def purchase_sql(context, window: event_ir.TimeWindow) -> str:
    condition = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="purchase"),
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="purchase.occurred_at"), window=window
            ),
        ),
        evidence=EVIDENCE,
    )
    return event_compiler.compile_condition(condition, context).sql


def interval(**kwargs) -> event_ir.AbsoluteInterval:
    return event_ir.AbsoluteInterval(**kwargs)


# ── T16. 절대 날짜+시각 경계가 SQL 에서 유실되지 않는다 ──────────────────────────


def test_end_time_only_keeps_the_second(context) -> None:
    """감사 #69. ``23시 59분 59초까지`` 가 '그날 하루 전체'로 넓어지면 조용한 오답이다."""

    sql = purchase_sql(
        context,
        interval(start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2), end_time="235959"),
    )
    assert "EO.ORDER_DATE = '20260701'" in sql
    assert "EO.ORDER_TIME <= '235959'" in sql


def test_start_time_only_keeps_the_second(context) -> None:
    """감사 #79. ``23시 59분 30초 이후``."""

    sql = purchase_sql(
        context,
        interval(start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2), start_time="235930"),
    )
    assert "EO.ORDER_TIME >= '235930'" in sql


def test_same_day_range_is_one_conjunction(context) -> None:
    sql = purchase_sql(
        context,
        interval(
            start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2),
            start_time="090000", end_time="180000",
        ),
    )
    assert "EO.ORDER_DATE = '20260701'" in sql
    assert "EO.ORDER_TIME >= '090000'" in sql
    assert "EO.ORDER_TIME <= '180000'" in sql
    assert " OR " not in sql, sql


def test_a_source_without_a_declared_time_column_still_fails_closed(context) -> None:
    """선언이 없으면 근사하지 않는다 — fail-close 방벽을 먼저 제거하지 않는다."""

    condition = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="signup"),
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef(name="signup.occurred_at"),
                window=interval(
                    start=date(2026, 7, 1), end_exclusive=date(2026, 7, 2), end_time="235959"
                ),
            ),
        ),
        evidence=EVIDENCE,
    )
    with pytest.raises(event_compiler.SqlCompileError) as caught:
        event_compiler.compile_condition(condition, context)
    assert "시각 경계" in str(caught.value)


# ── T17. 다일 범위에서 시각은 **경계일에만** 걸린다 ─────────────────────────────


def test_middle_days_are_whole_days(context) -> None:
    """감사 #80 의 조용한 오답. 양 끝에 시각을 한꺼번에 걸면 7월 1일 18:30 이후가 사라진다."""

    sql = purchase_sql(
        context,
        interval(
            start=date(2026, 7, 1), end_exclusive=date(2026, 7, 4),
            start_time="090000", end_time="183000",
        ),
    )
    # 첫날은 시작 시각 이후, 끝날은 끝 시각 이전, 가운데 날(7/2)은 하루 전체.
    assert "(EO.ORDER_DATE = '20260701' AND EO.ORDER_TIME >= '090000')" in sql
    assert "(EO.ORDER_DATE >= '20260702' AND EO.ORDER_DATE <= '20260702')" in sql
    assert "(EO.ORDER_DATE = '20260703' AND EO.ORDER_TIME <= '183000')" in sql
    # 가운데 날에 시각 술어가 붙지 않는다.
    assert "EO.ORDER_DATE >= '20260702' AND EO.ORDER_DATE <= '20260702' AND EO.ORDER_TIME" not in sql


def test_two_day_range_has_no_middle_branch(context) -> None:
    sql = purchase_sql(
        context,
        interval(
            start=date(2026, 7, 1), end_exclusive=date(2026, 7, 3),
            start_time="090000", end_time="183000",
        ),
    )
    assert sql.count("EO.ORDER_DATE") == 2, sql


def test_one_sided_time_leaves_the_other_boundary_whole(context) -> None:
    sql = purchase_sql(
        context,
        interval(start=date(2026, 7, 1), end_exclusive=date(2026, 7, 5), start_time="090000"),
    )
    assert "(EO.ORDER_DATE = '20260701' AND EO.ORDER_TIME >= '090000')" in sql
    assert "(EO.ORDER_DATE >= '20260702' AND EO.ORDER_DATE <= '20260704')" in sql


def test_a_date_only_interval_is_byte_identical_to_the_old_shape(context) -> None:
    """시각이 없으면 기존 SQL 이 **바이트 동일**하다 — 의미 없는 형식 변경을 만들지 않는다."""

    sql = purchase_sql(context, interval(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)))
    assert "EO.ORDER_DATE >= '20260701' AND EO.ORDER_DATE < '20260801'" in sql
    assert " OR " not in sql


# ── T18. 자정 넘김은 AND/OR 가 반대다(짝 테스트) ────────────────────────────────


def _time_of_day(context, start: str, end: str, *, crosses: bool) -> str:
    def compare(operator: str, value: str) -> event_ir.Condition:
        return event_ir.Comparison(
            operator=operator,
            left=event_ir.FieldRef(name="purchase.order_time"),
            right=event_ir.Literal(value=value),
            evidence=EVIDENCE,
        )

    combined = (
        event_ir.Or(operands=(compare(">=", start), compare("<=", end)))
        if crosses
        else event_ir.And(operands=(compare(">=", start), compare("<=", end)))
    )
    condition = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="purchase"), where=combined
        ),
        evidence=EVIDENCE,
    )
    return event_compiler.compile_condition(condition, context).sql


def test_the_surface_grammar_decides_whether_midnight_is_crossed() -> None:
    """자정 넘김 판정의 소유자는 달력 문법 하나다 — 코드가 시각을 다시 비교하지 않는다."""

    day = calendar_window.parse_time_of_day_window("오전 9시부터 오후 6시 사이")
    night = calendar_window.parse_time_of_day_window("밤 11시부터 다음 날 새벽 2시 사이")
    assert day is not None and night is not None
    assert not calendar_window.time_of_day_crosses_midnight(day)
    assert calendar_window.time_of_day_crosses_midnight(night)


def test_same_day_time_range_is_and(context) -> None:
    sql = _time_of_day(context, "090000", "180000", crosses=False)
    assert "EO.ORDER_TIME >= '090000'" in sql
    assert "EO.ORDER_TIME <= '180000'" in sql
    assert " OR " not in sql


def test_crossing_midnight_is_a_precedence_safe_or(context) -> None:
    """Phase 1 의 값어치가 드러나는 자리. 괄호가 없으면 상관 술어가 분기 밖으로 샌다."""

    sql = _time_of_day(context, "230000", "020000", crosses=True)
    assert " OR " in sql
    # 상관 술어가 OR 분기 **밖**에 남아야 한다(괄호로 묶인 분기 안에는 없다).
    assert "((EO.ORDER_TIME >= '230000') OR (EO.ORDER_TIME <= '020000'))" in sql
    assert not event_compiler.has_top_level_disjunction(
        sql[sql.index("WHERE") + 5:]
    ), sql


# ── T19. 최근 24시간은 '최근 1일'이 아니다 ──────────────────────────────────────


def test_recent_24_hours_is_a_precision_diagnostic_not_a_period_question() -> None:
    """감사 #82. 사용자는 기간을 **말했다** — '기간 값이 없습니다'는 사실이 아니다."""

    resolution = planner.resolve_executable("최근 24시간 동안 주문한 회원", today=FIXED_TODAY)
    assert isinstance(resolution, planner.MissingCapability)
    diagnostic = resolution.diagnostic
    assert diagnostic.code == sd.UNSUPPORTED_TEMPORAL_PRECISION
    assert diagnostic.outcome is sd.Outcome.UNSUPPORTED
    assert "기간 값이 없" not in diagnostic.user_action
    assert diagnostic.detail["supported"] == "day"


def test_a_day_window_is_not_mistaken_for_sub_day() -> None:
    resolution = planner.resolve_executable("최근 30일 동안 주문한 회원", today=FIXED_TODAY)
    assert isinstance(resolution, planner.Executable)


def test_subday_surface_is_owned_by_the_calendar_grammar() -> None:
    assert calendar_window.subday_duration_spans("최근 24시간 동안") == [(3, 7, 24, "hour")]
    assert calendar_window.subday_duration_spans("최근 30일 동안") == []


# ── T20. 두 상대 경계는 하나의 구간이다 ─────────────────────────────────────────


def test_two_boundaries_compose_into_one_interval() -> None:
    """감사 #85. 반쪽만 읽으면 요청하지 않은 기간이 들어온다."""

    resolution = planner.resolve_executable(
        "3개월 전부터 1개월 전까지 주문한 회원", today=FIXED_TODAY
    )
    assert isinstance(resolution, planner.Executable)
    sql = next(plan.sql for plan in resolution.plans)
    assert "EO.ORDER_DATE >= '20260508'" in sql
    assert "EO.ORDER_DATE < '20260709'" in sql


def test_a_reversed_boundary_pair_is_not_invented() -> None:
    """``1개월 전부터 3개월 전까지`` 는 뜻이 성립하지 않는다 — 지어내지 않는다."""

    compact = "1개월전부터3개월전까지주문한회원"
    candidates = calendar_window.duration_window_candidates(compact)
    assert calendar_window.compose_boundary_interval(candidates, today=FIXED_TODAY) is None


def test_a_single_boundary_is_not_composed() -> None:
    compact = "3개월전부터주문한회원"
    candidates = calendar_window.duration_window_candidates(compact)
    assert calendar_window.compose_boundary_interval(candidates, today=FIXED_TODAY) is None


def test_the_negated_two_boundary_clause_keeps_its_window() -> None:
    """부재 조건에서 창이 사라지면 대상이 통째로 달라진다(하드 의미 제약 보존)."""

    resolution = planner.resolve_executable(
        "30일 전부터 7일 전까지 구매하지 않은 회원", today=FIXED_TODAY
    )
    assert isinstance(resolution, planner.Executable)
    sql = next(plan.sql for plan in resolution.plans)
    assert sql.startswith("NOT EXISTS")
    assert "EO.ORDER_DATE >=" in sql, "창이 사라진 NOT EXISTS 는 조용한 오답이다"


# ── T21. 미래 창은 미래를 담는 필드에만 ─────────────────────────────────────────


def test_future_window_is_allowed_on_a_future_capable_field() -> None:
    resolution = planner.resolve_executable(
        "향후 7일 안에 구매예정일이 도래하는 회원", today=FIXED_TODAY
    )
    assert isinstance(resolution, planner.Executable)
    plan = next(
        item
        for item in resolution.plans
        if isinstance(item.obligation, planner.FutureWindowObligation)
    )
    assert "BUY_DUE_DATE >= '20260808'" in plan.sql
    assert "BUY_DUE_DATE < '20260815'" in plan.sql


def test_future_window_is_refused_on_a_past_event_column() -> None:
    """``ORDER_DATE`` 에 미래 창을 걸면 경고 없이 항상 0건이다."""

    resolution = planner.resolve_executable("향후 7일 안에 주문할 회원", today=FIXED_TODAY)
    assert not isinstance(resolution, planner.Executable)


def test_a_future_marker_never_becomes_a_past_window() -> None:
    """구현 중 실측된 조용한 의미 반전. ``향후 7일`` 이 ``>= 7일 전`` 이 됐다."""

    resolution = planner.resolve_executable(
        "향후 7일 안에 구매예정일이 도래하는 회원", today=FIXED_TODAY
    )
    assert isinstance(resolution, planner.Executable)
    for plan in resolution.plans:
        assert "DATEADD(DAY, -7" not in plan.sql, plan.sql


def test_future_capability_is_declared_not_hardcoded() -> None:
    """어느 컬럼이 미래를 담는지는 카탈로그가 안다 — 코드에 컬럼 이름이 없다."""

    fields = planner.future_capable_fields()
    assert fields, "미래 능력 선언이 하나도 없으면 이 축은 열려 있지 않다"
    snapshot = audience_runtime.catalog_snapshot()
    for field_id in fields:
        assert snapshot["fields"][field_id][planner.FUTURE_VALUES_CAPABILITY] is True
