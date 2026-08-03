"""⑤ 스냅샷 회원 집합 대조의 계약.

④(경계 fixture)와 ⑤(스냅샷)는 **다른 것을 본다**: ④ 는 만든 경계를, ⑤ 는 있는 데이터를 본다.
그래서 여기서 고정하는 것도 다르다.

    **teeth**   합성한 카운트 문장이 차이·중복을 실제로 세는가(항상 0을 세는 비교기는 장식이다)
    **clock**   두 산출물이 **같은 기준 시각**에 평가되는가(고정하지 못하면 대조하지 않는다)
    **gate**    움직이는 원천 위에서 판정하지 않는가(불안정 = 미실행이고, 미실행은 통과가 아니다)

실행 엔진은 ④와 같은 sqlite 인메모리다. 실DB 접근 없이도 **합성한 SQL 이 실제로 도는지**와
게이트가 닫히는지를 고정할 수 있고, 그것이 이 파일이 지키는 범위다(실데이터 분포는 운영 실행이 본다).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import audience_shadow as shadow  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import sql_dialect  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "audience_shadow_fixture.json"


@pytest.fixture(scope="module")
def anchor() -> date:
    return shadow.engine_anchor_date()


@pytest.fixture(scope="module")
def tables(anchor: date):
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return shadow.build_fixture_tables(spec, anchor=anchor)


class _Engine:
    """sqlite 인메모리 스냅샷 실행자. 실행된 문장을 그대로 들고 있어, 우리가 **무엇을 보냈는지**를
    검증할 수 있게 한다(합성물의 모양은 계약이다 — 시계가 고정됐는지도 여기서 보인다)."""

    def __init__(self, tables) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.statements: list[str] = []
        self.on_execute = None
        for table in tables:
            columns = ", ".join(f'"{column}"' for column in table.columns)
            placeholders = ", ".join("?" for _ in table.columns)
            self.connection.execute(f'CREATE TABLE "{table.name}" ({columns})')
            self.connection.executemany(
                f'INSERT INTO "{table.name}" ({columns}) VALUES ({placeholders})', table.rows)

    def __call__(self, sql: str) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        rows = self.connection.execute(shadow._sqlite_sql(sql, dialect="tsql")).fetchall()
        if self.on_execute is not None:
            self.on_execute(self, sql)
        return rows

    def close(self) -> None:
        self.connection.close()


@pytest.fixture
def engine(tables):
    engine = _Engine(tables)
    try:
        yield engine
    finally:
        engine.close()


DIALECT = shadow.SqliteVerificationDialect()


def _event_sql(expression: event_ir.Condition, anchor: date) -> str:
    catalog = audience_runtime.resolve_audience_catalog()
    return event_compiler.compile_expression(
        expression, context=catalog.compile_context(literals=True, today=anchor)
    ).sql


def _login_absence_sql(anchor: date) -> str:
    return _event_sql(
        event_ir.Not(operand=event_ir.Exists(
            relation=event_ir.event_relation("login", event_ir.RollingWindow(value=30, unit="day")))),
        anchor,
    )


def _purchase_absence_sql(anchor: date) -> str:
    return _event_sql(
        event_ir.Not(operand=event_ir.Exists(
            relation=event_ir.event_relation("purchase", event_ir.RollingWindow(value=90, unit="day")))),
        anchor,
    )


def _compare(engine: _Engine, legacy_sql: str, event_ir_sql: str, **overrides: Any):
    return shadow.run_snapshot_comparison(
        legacy_sql=legacy_sql, event_ir_sql=event_ir_sql,
        execute=engine, dialect=DIALECT, **overrides,
    )


# ── teeth: 합성한 카운트 문장이 실제로 센다 ───────────────────────────────────────


def test_snapshot_finds_the_same_divergence_the_fixture_stage_found(engine, anchor) -> None:
    """④가 잡은 미접속 조건의 차이를 ⑤도 **수치와 표본으로** 잡는다.

    같은 사실을 두 층에서 확인하는 것이 중복이 아닌 이유: ④는 파이썬이 집합을 비교하고, ⑤는
    엔진이 차집합을 센다. 합성한 카운트 문장이 틀리면 ⑤만 조용히 '차이 없음'을 보고하게 된다.
    """
    comparison = _compare(engine, graph_rag._member_activity_predicate(30), _login_absence_sql(anchor))

    assert comparison.counts.only_in_legacy == 1   # 마지막 접속이 컷오프 정확히 그날(1009)
    assert comparison.counts.only_in_event_ir == 1  # 한 번도 접속하지 않은 회원(1004)
    assert 1009 in comparison.sample_only_in_legacy
    assert 1004 in comparison.sample_only_in_event_ir

    stage = shadow.snapshot_stage(comparison)
    assert stage.status == shadow.FAIL
    assert stage.divergences[0].kind == shadow.UNEXPLAINED_DIVERGENCE
    assert stage.divergences[0].blocks_cutover


def test_snapshot_confirms_the_purchase_absence_equivalence(engine, anchor) -> None:
    """A 그룹의 반대편 — 같은 뜻이면 차집합이 양쪽 다 0이고 단계는 통과한다."""
    comparison = _compare(
        engine, graph_rag._purchase_inactivity_predicate(90), _purchase_absence_sql(anchor))

    assert comparison.counts.matches
    assert comparison.counts.legacy_members > 0, "0명끼리 같으면 아무것도 검증하지 않은 것이다"
    assert not comparison.counts.has_duplicates
    assert shadow.snapshot_stage(comparison).status == shadow.PASS


def test_row_and_member_counts_are_counted_separately(engine) -> None:
    """조인 증폭은 집합이 같아도 잡아야 한다 — 행 수와 회원 수를 따로 세는 이유."""
    amplified = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        "INNER JOIN CRM_SL_ORDERHEADERMALL O ON O.MEMBER_NO = B.MEMBER_NO"
    )
    deduped = (
        "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        "INNER JOIN CRM_SL_ORDERHEADERMALL O ON O.MEMBER_NO = B.MEMBER_NO"
    )

    comparison = _compare(engine, amplified, deduped)

    assert comparison.counts.matches               # 집합은 같지만
    assert comparison.counts.legacy_rows > comparison.counts.legacy_members  # 행은 증폭됐다
    assert comparison.counts.has_duplicates
    stage = shadow.snapshot_stage(comparison)
    assert stage.status == shadow.FAIL
    assert "중복" in stage.divergences[0].summary


def test_all_six_counts_come_from_one_statement(engine, anchor) -> None:
    """여섯 수치가 같은 스냅샷에서 나와야 서로 비교할 수 있다 — 나눠 실행하면 그 사이의 변동이
    '의미 차이'로 둔갑한다."""
    _compare(engine, graph_rag._purchase_inactivity_predicate(90), _purchase_absence_sql(anchor))

    count_statements = [item for item in engine.statements if "only_in_event_ir" in item]
    assert len(count_statements) == 1
    for bucket in shadow.COUNT_BUCKETS:
        assert f"'{bucket}'" in count_statements[0]


def test_the_member_projection_must_be_the_subject_key(engine) -> None:
    """회원 조회가 주체 키를 내지 않으면 **엔진이 실패해야 한다** — 위치로 집으면 엉뚱한 값이
    조용히 회원 id 자리에 들어온다(그리고 그 대조는 통과한다)."""
    flag_query = "SELECT 1 AS FLAG FROM CRM_MB_BASEINFO B"

    with pytest.raises(sqlite3.OperationalError, match="MEMBER_NO"):
        _compare(engine, flag_query, flag_query)


# ── clock: 두 산출물이 같은 기준 시각에 평가되는가 ────────────────────────────────


def test_both_sides_are_pinned_to_one_anchor(engine, anchor) -> None:
    legacy = graph_rag._purchase_inactivity_predicate(90)
    comparison = _compare(engine, legacy, _purchase_absence_sql(anchor))

    assert comparison.clock_pinned
    assert comparison.anchor, "고정에 쓴 기준 시각이 보고되지 않으면 재현할 수 없다"
    assert "GETDATE" in legacy and "GETDATE" not in comparison.legacy_sql
    assert "GETDATE" not in comparison.event_ir_sql
    pinned = DIALECT.datetime_anchor(comparison.anchor)
    assert pinned in comparison.legacy_sql and pinned in comparison.event_ir_sql
    # 실행자에게 나간 문장에도 시계가 남아 있으면 안 된다(합성 과정에서 되살아나는 길 차단).
    assert not any("GETDATE" in item for item in engine.statements if "only_in_legacy" in item)


def test_the_anchor_is_read_from_the_engine_not_the_process(engine) -> None:
    """앵커는 파이썬이 아니라 **질의를 실행할 엔진**의 시계에서 읽는다(경계가 통째로 어긋난다)."""
    read = shadow.engine_clock_anchor(engine, dialect=DIALECT)

    assert engine.statements[0].strip().upper().startswith("SELECT GETDATE()")
    assert read.startswith(shadow.engine_anchor_date().isoformat())


def test_an_unpinnable_clock_stops_the_comparison(engine) -> None:
    """방언 기본 시계가 아닌 시계가 남으면 앵커가 계속 움직인다 — '같다'도 '다르다'도 말할 수 없다."""
    with pytest.raises(shadow.ShadowVerificationError, match="고정하지 못한"):
        _compare(
            engine,
            "B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), SYSDATETIME(), 112)",
            "B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), GETDATE(), 112)",
        )


def test_refusing_to_pin_refuses_to_compare(engine, anchor) -> None:
    """고정할 수 있는데 고정하지 않고 얻은 '같다'는 우연일 수 있다 — 그것을 통과로 적지 않는다."""
    with pytest.raises(shadow.ShadowVerificationError, match="기준 시각"):
        _compare(
            engine, graph_rag._purchase_inactivity_predicate(90), _purchase_absence_sql(anchor),
            pin_clock=False,
        )


def test_absolute_windows_need_no_pin(engine) -> None:
    """시계가 없는 산출물은 고정할 것이 없다 — 없는 위험을 이유로 막지 않는다."""
    comparison = _compare(
        engine,
        "B.MEMBER_NO IN (SELECT O.MEMBER_NO FROM CRM_SL_ORDERHEADERMALL O "
        "WHERE O.ORDER_DATE >= '20190301' AND O.ORDER_DATE < '20190401')",
        "B.MEMBER_NO IN (SELECT O.MEMBER_NO FROM CRM_SL_ORDERHEADERMALL O "
        "WHERE O.ORDER_DATE >= '20190301' AND O.ORDER_DATE < '20190401')",
    )

    assert comparison.clock_functions == ()
    assert comparison.clock_pinned is False
    assert comparison.anchor == ""
    assert comparison.counts.legacy_members == 1, "20190301~0331 구간 회원(1010)을 세지 못했다"
    assert shadow.snapshot_stage(comparison).status == shadow.PASS


def test_the_pin_replaces_every_occurrence_and_reports_how_many() -> None:
    sql = "A >= CONVERT(CHAR(8), GETDATE(), 112) AND B < getdate()"

    pinned, hits = shadow.pin_execution_clock(sql, clock_token="GETDATE()", anchor_sql="@ANCHOR")

    assert hits == 2
    assert "GETDATE" not in pinned.upper()
    # 컬럼 이름에 우연히 섞인 토큰은 시계가 아니다.
    assert shadow.pin_execution_clock(
        "B.LAST_GETDATE_FLAG = 1", clock_token="GETDATE()", anchor_sql="@A")[1] == 0


def test_the_clock_vocabulary_is_owned_by_the_dialect() -> None:
    """검증기가 시계 이름을 자기 소스에 적으면 방언이 바뀌는 날 검증기만 옛 어휘로 센다."""
    tsql = sql_dialect.get_dialect("tsql")

    assert tsql.now() in tsql.clock_functions()
    assert shadow.clock_functions_in("X >= GETDATE()", dialect=tsql) == ("GETDATE()",)
    assert shadow.clock_functions_in("X >= NOW()", dialect=tsql) == ()
    assert shadow.clock_functions_in("X >= NOW()", dialect=sql_dialect.get_dialect("mysql")) == ("NOW()",)


def test_a_foreign_dialect_clock_is_not_counted_as_no_clock(engine) -> None:
    """대조 연결의 방언 어휘로만 세면, 다른 방언이 렌더한 시계가 '없다'로 통과한다.

    그 SQL 은 실행하면 문법 오류로 죽지만 그 전에 "고정할 시계가 없으니 고정 없이 대조해도 된다"는
    **잘못된 판정**을 통과한다 — 그래서 잔여 검사는 아는 어휘 전부로 한다.
    """
    tsql = sql_dialect.get_dialect("tsql")
    mysql_shaped = "B.LAST_LOGIN_DATE >= DATE_FORMAT(NOW(), '%Y%m%d')"

    assert shadow.clock_functions_in(mysql_shaped, dialect=tsql) == ()
    assert shadow.residual_clock_functions(mysql_shaped) == ("NOW()",)
    with pytest.raises(shadow.ShadowVerificationError, match="고정하지 못한"):
        _compare(engine, mysql_shaped, "B.MEMBER_NO > 0")


def test_the_anchor_literal_is_typed_not_a_bare_string() -> None:
    """``to_char8(now())`` 자리에 문자열을 끼우면 오류가 아니라 **조용히 틀린 컷오프**가 된다."""
    tsql = sql_dialect.get_dialect("tsql")

    assert tsql.datetime_anchor("2026-08-03T10:00:00") == "CAST('2026-08-03T10:00:00' AS DATETIME)"


# ── gate: 움직이는 원천 위에서는 판정하지 않는다 ──────────────────────────────────


def test_a_moving_snapshot_is_not_run_rather_than_divergent(engine, anchor) -> None:
    """대조 도중 원천이 움직이면 그 차이는 뜻의 차이가 아닐 수 있다 — 판정하지 않는다.

    두 번째 probe 직전에 회원을 하나 넣어 실제로 움직이게 만든다(관측이 아니라 실행으로 고정).
    """
    def mutate(engine: _Engine, sql: str) -> None:
        if "only_in_event_ir" in sql:
            engine.connection.execute(
                'INSERT INTO "CRM_MB_BASEINFO" (MEMBER_NO, LAST_LOGIN_DATE, MEMBER_STATE_CD)'
                " VALUES (9001, NULL, 'MEMBER_STATE_CD.NORMAL')")
            engine.on_execute = None

    engine.on_execute = mutate
    comparison = _compare(engine, graph_rag._member_activity_predicate(30), _login_absence_sql(anchor))

    assert comparison.probes, "차이가 있으면 안정성 probe 를 돌려야 한다"
    assert not comparison.stable
    stage = shadow.snapshot_stage(comparison)
    assert stage.status == shadow.NOT_RUN
    assert stage.detail.strip()
    assert stage.divergences[0].kind == shadow.NONDETERMINISTIC_SOURCE


def test_a_stable_comparison_does_not_pay_for_a_second_scan(engine, anchor) -> None:
    """차이가 없을 때 재실행이 답하는 질문은 없다 — 실데이터 전수 스캔을 이유 없이 두 번 돌리지 않는다."""
    comparison = _compare(
        engine, graph_rag._purchase_inactivity_predicate(90), _purchase_absence_sql(anchor))

    assert comparison.probes == ()
    assert comparison.stable
    assert len([item for item in engine.statements if "only_in_event_ir" in item]) == 1


def test_instability_kind_names_only_what_it_can_prove() -> None:
    """고정된 시계 위에서 움직였는가 아닌가로 분류가 갈린다 — 가릴 수 없는 것은 말하지 않는다."""
    first = shadow.SnapshotCounts(10, 10, 10, 10, 0, 0)
    moved = shadow.SnapshotCounts(11, 11, 10, 10, 1, 0)

    pinned = shadow.snapshot_stage(shadow.SnapshotComparison(
        counts=first, probes=(moved,), clock_pinned=True, anchor="2026-08-03T00:00:00",
        clock_functions=("GETDATE()",)))
    unpinned = shadow.snapshot_stage(shadow.SnapshotComparison(
        counts=first, probes=(moved,), clock_pinned=False, anchor="", clock_functions=()))

    assert pinned.status == unpinned.status == shadow.NOT_RUN
    assert pinned.divergences[0].kind == shadow.NONDETERMINISTIC_SOURCE
    assert unpinned.divergences[0].kind == shadow.DATA_VOLATILITY


def test_an_unrun_snapshot_still_blocks_cutover() -> None:
    """분류가 '차단하지 않는 종류'여도 미실행은 미실행이다(둘을 헷갈리면 게이트가 열린다)."""
    unstable = shadow.snapshot_stage(shadow.SnapshotComparison(
        counts=shadow.SnapshotCounts(10, 10, 10, 10, 0, 0),
        probes=(shadow.SnapshotCounts(11, 11, 10, 10, 1, 0),),
        clock_pinned=True, anchor="2026-08-03T00:00:00", clock_functions=("GETDATE()",)))
    others = [
        shadow.StageResult(stage=name, status=shadow.PASS)
        for name in shadow.REQUIRED_STAGES if name != shadow.STAGE_SNAPSHOT_MEMBERS
    ]

    report = shadow.ShadowReport(asset_id="a", asset_revision=1, stages=(unstable, *others))

    assert not any(item.blocks_cutover for item in report.divergences)
    assert report.missing_stages == (shadow.STAGE_SNAPSHOT_MEMBERS,)
    assert report.cutover_allowed is False


def test_an_empty_match_is_not_evidence(engine) -> None:
    """0명끼리의 일치는 통과가 아니다 — 뜻이 완전히 다른 두 조건도 대상이 없으면 똑같이 0명이다.

    실DB 첫 실행에서 잡힌 구멍이다: '최근 90일 미구매 + 최근 365일 구매 존재' 자산이 (데이터가
    2019년까지라) 양쪽 0명으로 **통과**했다. 데이터가 비어 있는 자산일수록 검증을 쉽게 통과하는
    것은 정확히 거꾸로다.
    """
    nobody = "B.MEMBER_NO < 0"
    also_nobody = "B.MEMBER_NO IS NULL"

    comparison = _compare(engine, nobody, also_nobody)
    stage = shadow.snapshot_stage(comparison)

    assert comparison.counts.matches
    assert stage.status == shadow.NOT_RUN
    assert "0명" in stage.detail


def test_an_empty_fixture_match_is_not_evidence_either(tables) -> None:
    """④에도 같은 규칙이 선다 — 경계를 넣어 만든 코퍼스라도 그 경계가 조건에 걸리지 않으면
    0명끼리 같아진다(코퍼스에 경계를 더하라는 신호이지 통과가 아니다)."""
    result = shadow.run_fixture_comparison(
        legacy_sql="B.MEMBER_NO < 0", event_ir_sql="B.MEMBER_NO IS NULL", tables=tables)

    stage = shadow.fixture_stage(result)

    assert result.matches
    assert stage.status == shadow.NOT_RUN
    assert "0명" in stage.detail


def test_a_truncated_sample_says_so(engine, anchor) -> None:
    """표본이 잘렸다는 사실을 싣지 않으면 보고서가 '이게 전부'라고 말하게 된다."""
    comparison = _compare(
        engine, graph_rag._member_activity_predicate(30), _login_absence_sql(anchor),
        sample_limit=0,
    )

    assert comparison.counts.only_in_legacy > 0
    assert comparison.sample_only_in_legacy == ()
    assert comparison.sample_truncated
    detail = shadow.snapshot_stage(comparison).divergences[0].detail
    assert detail["sample_truncated"] is True


# ── ⑥ 실행 비용 ──────────────────────────────────────────────────────────────────


class _Clock:
    """호출마다 정해진 만큼 흐르는 가짜 시계 — 측정 로직을 실제 서버 변동 없이 고정한다."""

    def __init__(self, steps: list[float]) -> None:
        self.steps = list(steps)
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += self.steps.pop(0) if self.steps else 0.0
        return current


def _timings(legacy_ms: list[float], event_ms: list[float]) -> list[float]:
    """(시작, 끝) 쌍으로 흐르는 시계 스텝. 교대 실행 순서(legacy, event_ir)를 그대로 따른다."""
    steps: list[float] = []
    for legacy, event in zip(legacy_ms, event_ms):
        steps.extend([legacy / 1000.0, 0.0, event / 1000.0, 0.0])
    return steps


def test_cost_probe_measures_the_predicate_not_the_transfer(engine, anchor) -> None:
    """비용 질의는 회원 수를 센다 — id 를 전부 실어 나르면 술어 비용이 아니라 네트워크를 잰다."""
    comparison = shadow.run_performance_comparison(
        legacy_sql=graph_rag._purchase_inactivity_predicate(90),
        event_ir_sql=_purchase_absence_sql(anchor),
        execute=engine, dialect=DIALECT, repetitions=2,
    )

    assert all("COUNT(*)" in item for item in comparison.measured_sql)
    assert comparison.legacy.usable and comparison.event_ir.usable
    # (반복+1) × 2 회 + 앵커 조회 1회.
    assert len([item for item in engine.statements if "member_count" in item]) == 6


def test_the_first_run_is_excluded_from_the_samples(engine, anchor) -> None:
    """첫 실행에는 계획 컴파일·캐시 적재가 들어 있다 — 빼지 않으면 먼저 돈 쪽이 항상 느려 보인다."""
    clock = _Clock(_timings([500.0, 10.0, 10.0], [500.0, 10.0, 10.0]))

    comparison = shadow.run_performance_comparison(
        legacy_sql=graph_rag._purchase_inactivity_predicate(90),
        event_ir_sql=_purchase_absence_sql(anchor),
        execute=engine, dialect=DIALECT, repetitions=2, timer=clock,
    )

    assert comparison.legacy.first_ms == pytest.approx(500.0)
    assert comparison.legacy.samples == pytest.approx((10.0, 10.0))
    assert comparison.ratio == pytest.approx(1.0)
    assert shadow.performance_stage(comparison).status == shadow.PASS


def test_a_cost_regression_blocks(engine, anchor) -> None:
    clock = _Clock(_timings([10.0] * 4, [10.0, 400.0, 400.0, 400.0]))

    comparison = shadow.run_performance_comparison(
        legacy_sql=graph_rag._purchase_inactivity_predicate(90),
        event_ir_sql=_purchase_absence_sql(anchor),
        execute=engine, dialect=DIALECT, repetitions=3, timer=clock,
    )
    stage = shadow.performance_stage(comparison)

    assert comparison.regressed
    assert stage.status == shadow.FAIL
    assert stage.divergences[0].blocks_cutover, "비용 퇴행에는 승인 경로가 없다 — 그대로 막는다"


def test_a_ratio_without_an_absolute_difference_is_noise(engine, anchor) -> None:
    """2ms → 4ms 는 '2배 퇴행'이 아니다 — 비율만 보면 보고서가 잡음으로 찬다."""
    clock = _Clock(_timings([2.0] * 4, [4.0] * 4))

    comparison = shadow.run_performance_comparison(
        legacy_sql=graph_rag._purchase_inactivity_predicate(90),
        event_ir_sql=_purchase_absence_sql(anchor),
        execute=engine, dialect=DIALECT, repetitions=3, timer=clock,
    )

    assert comparison.ratio == pytest.approx(2.0)
    assert not comparison.regressed
    assert shadow.performance_stage(comparison).status == shadow.PASS


def test_one_sided_failure_is_the_regression(engine, anchor) -> None:
    """Event IR 쪽만 못 돌면 그것이 곧 비용 신호다 — 미실행이 아니라 차이로 보고한다."""
    comparison = shadow.PerformanceComparison(
        legacy=shadow.Timing(20.0, (20.0, 21.0)),
        event_ir=shadow.Timing(0.0, (), failures=3, error="Timeout expired"),
        repetitions=3, ratio_threshold=1.5, noise_floor_ms=50.0,
    )
    stage = shadow.performance_stage(comparison)

    assert stage.status == shadow.FAIL
    assert stage.divergences[0].detail["side"] == "event_ir"
    assert stage.divergences[0].blocks_cutover


def test_measuring_neither_side_is_not_a_pass() -> None:
    comparison = shadow.PerformanceComparison(
        legacy=shadow.Timing(0.0, (), failures=3, error="Timeout expired"),
        event_ir=shadow.Timing(0.0, (), failures=3, error="Timeout expired"),
        repetitions=3, ratio_threshold=1.5, noise_floor_ms=50.0,
    )
    stage = shadow.performance_stage(comparison)

    assert stage.status == shadow.NOT_RUN
    assert "Timeout" in stage.detail


def test_the_unmeasured_cost_axes_are_named(engine, anchor) -> None:
    """이름을 적어 두지 않으면 보고서가 '성능이 모든 축에서 같다'로 읽힌다."""
    comparison = shadow.run_performance_comparison(
        legacy_sql=graph_rag._purchase_inactivity_predicate(90),
        event_ir_sql=_purchase_absence_sql(anchor),
        execute=engine, dialect=DIALECT, repetitions=1,
    )

    assert shadow.UNMEASURED_COST_AXES
    assert comparison.to_dict()["unmeasured_cost_axes"] == list(shadow.UNMEASURED_COST_AXES)
    assert any("STATISTICS IO" in item for item in shadow.UNMEASURED_COST_AXES)


def test_cost_measurement_pins_the_same_clock_as_the_snapshot(engine, anchor) -> None:
    """⑤와 ⑥이 다른 시점의 SQL 을 재면 '같은 뜻인데 비용만 다르다'는 결론이 무의미해진다."""
    comparison = shadow.run_performance_comparison(
        legacy_sql=graph_rag._purchase_inactivity_predicate(90),
        event_ir_sql=_purchase_absence_sql(anchor),
        execute=engine, dialect=DIALECT, repetitions=1,
    )

    assert comparison.clock_pinned
    assert all("GETDATE" not in item for item in comparison.measured_sql)


# ── 읽기 전용: 대조는 아무것도 바꾸지 않는다 ──────────────────────────────────────


@pytest.mark.parametrize("hostile", [
    "B.MEMBER_NO = 1; DROP TABLE CRM_MB_BASEINFO",
    "B.MEMBER_NO IN (SELECT 1) UNION ALL SELECT 1 FROM (DELETE FROM CRM_MB_BASEINFO) X",
])
def test_the_comparison_refuses_write_shaped_input(engine, hostile: str) -> None:
    """합성 전에 막는다 — 조각을 한 문장으로 잇는 순간 우리가 읽은 것과 다른 문장이 된다."""
    with pytest.raises(shadow.ShadowVerificationError):
        _compare(engine, hostile, "B.MEMBER_NO = 1")
    with pytest.raises(shadow.ShadowVerificationError):
        _compare(engine, "B.MEMBER_NO = 1", hostile)


def test_an_empty_side_is_refused_instead_of_compared(engine) -> None:
    with pytest.raises(shadow.ShadowVerificationError):
        _compare(engine, "   ", "B.MEMBER_NO = 1")
