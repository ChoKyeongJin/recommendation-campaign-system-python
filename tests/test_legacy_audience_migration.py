"""legacy 슬롯 → Event IR 어댑터의 계약(1차 웨이브).

검증 축 여섯:

    ① 골든 변환      A 그룹 슬롯이 정확히 어떤 IR 이 되는가(그리고 그 IR 이 실제 컴파일되는가)
    ② 멱등·지문      같은 입력 → 같은 지문, 근거가 달라도 같은 의미면 같은 의미 지문
    ③ 경로 회계      컨테이너의 모든 키가 정확히 한 버킷에 들어간다(조용한 통과 0건)
    ④ fail-close     부분 변환·미지원 연산자·미해결 심볼은 실행 가능이 아니다
    ⑤ 시간 경계      경계 하루 전/당일/다음날, 월말, 윤년, 자정, timezone
    ⑥ 의미 동등성    legacy 실행 술어와 Event IR 컴파일 결과가 같은 집합인가(A 그룹의 근거)
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import legacy_audience_migration as migration  # noqa: E402
import migration_fingerprint  # noqa: E402
from audience_authority import AudienceAuthority, MigrationStatus  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2026, 8, 3, 0, 0, tzinfo=SEOUL)


@pytest.fixture(scope="module")
def context() -> migration.MigrationContext:
    return migration.MigrationContext(
        as_of=AS_OF,
        timezone="Asia/Seoul",
        catalog_version="test-catalog",
        compiler_version=event_compiler.COMPILER_VERSION,
        catalog=audience_runtime.resolve_audience_catalog(),
    )


def _plan(**slots: Any) -> dict[str, Any]:
    return {"intent": "find_user_segment", "target_user": dict(slots)}


def _compile(expression: event_ir.Condition, context: migration.MigrationContext) -> str:
    return event_compiler.compile_expression(
        expression, context=context.catalog.compile_context(literals=True, today=context.as_of_date)
    ).sql


# ── ① 골든 변환 ───────────────────────────────────────────────────────────────────


def test_purchase_date_becomes_a_half_open_interval_existence(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_date={"from": "20190301", "to": "20190331", "label": "2019년 3월"}),
        context=context,
    )

    assert result.is_executable
    assert result.status is MigrationStatus.CONVERTED
    assert result.expression.to_dict() == event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source("purchase"),
            where=event_ir.TimeFilter(
                field=event_ir.FieldRef("purchase.occurred_at"),
                window=event_ir.AbsoluteInterval(
                    start=__import__("datetime").date(2019, 3, 1),
                    end_exclusive=__import__("datetime").date(2019, 4, 1),
                ),
            ),
        )
    ).to_dict()
    sql = _compile(result.expression, context)
    assert "EO.ORDER_DATE >= '20190301'" in sql and "EO.ORDER_DATE < '20190401'" in sql


def test_multi_window_purchase_date_becomes_a_disjunction(context) -> None:
    """나열형 기간이 한 구간으로 뭉개지면 사이 기간이 조용히 딸려 들어온다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_date={
            "from": "20180101", "to": "20191231",
            "windows": [
                {"from": "20190101", "to": "20191231"},
                {"from": "20180101", "to": "20181231"},
            ],
        }),
        context=context,
    )

    assert result.is_executable
    assert isinstance(result.expression, event_ir.Or)
    windows = event_ir.time_windows(result.expression)
    assert [window.to_calendar_window() for window in windows] == [
        {"from": "20180101", "to": "20181231"},
        {"from": "20190101", "to": "20191231"},
    ]


def test_purchase_membership_and_inactivity_are_mirror_images(context) -> None:
    membership = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 90}), context=context
    )
    inactivity = migration.legacy_slot_to_event_ir(
        _plan(purchase_inactivity={"value": 90, "unit": "days", "min_days": 90}), context=context
    )

    assert membership.is_executable and inactivity.is_executable
    assert isinstance(inactivity.expression, event_ir.Not)
    assert inactivity.expression.operand.to_dict() == membership.expression.to_dict()
    assert _compile(inactivity.expression, context).startswith("NOT EXISTS (")


def test_no_purchase_behavior_becomes_lifetime_absence(context) -> None:
    result = migration.legacy_slot_to_event_ir(_plan(behaviors=["no_purchase"]), context=context)

    assert result.is_executable
    assert event_ir.covers_existence(result.expression, "purchase", negated=True)
    assert not event_ir.time_windows(result.expression)


def test_aggregate_condition_reads_its_recipe_from_the_catalog(context) -> None:
    """집계 함수·표현 필드·distinct 를 어댑터가 정하지 않는다는 계약."""

    result = migration.legacy_slot_to_event_ir(
        _plan(aggregate_conditions=[
            {"metric_id": "order_count", "operator": ">=", "threshold": 3, "window_days": 90},
        ]),
        context=context,
    )
    metric = context.catalog.metric("purchase_count")

    assert result.is_executable
    aggregate = result.expression.left
    assert isinstance(aggregate, event_ir.Aggregate)
    assert aggregate.function == metric.aggregate_function
    assert aggregate.expression.name == metric.expression_field
    assert aggregate.distinct is metric.distinct
    assert "COUNT(DISTINCT EO.ORDER_ID)" in _compile(result.expression, context)


def test_several_slots_compose_into_one_conjunction(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(
            purchase_date={"from": "20190301", "to": "20190331"},
            aggregate_conditions=[{"metric_id": "order_count", "operator": ">=", "threshold": 3}],
        ),
        context=context,
    )

    assert result.is_executable
    assert isinstance(result.expression, event_ir.And)
    assert len(list(event_ir.iter_atoms(result.expression))) == 2


# ── ② 멱등성과 지문 ───────────────────────────────────────────────────────────────


def test_repeated_conversion_is_idempotent(context) -> None:
    plan = _plan(
        purchase_date={"from": "20190301", "to": "20190331", "label": "3월"},
        aggregate_conditions=[{"metric_id": "purchase_amount", "operator": ">", "threshold": 50000}],
    )

    first = migration.legacy_slot_to_event_ir(plan, context=context)
    second = migration.legacy_slot_to_event_ir(copy.deepcopy(plan), context=context)

    assert first.fingerprint == second.fingerprint
    assert first.source_fingerprint == second.source_fingerprint
    assert first.binding_fingerprint == second.binding_fingerprint
    assert first.expression.to_dict() == second.expression.to_dict()


def test_source_fingerprint_ignores_key_order_and_display_labels(context) -> None:
    bare = _plan(purchase_membership={"operator": "exists", "window_days": 30})
    decorated = {
        "intent": "find_user_segment",
        "target_user": {"purchase_membership": {"window_days": 30, "operator": "exists"}, "label": "표시용"},
    }

    assert (
        migration.legacy_slot_to_event_ir(bare, context=context).source_fingerprint
        == migration.legacy_slot_to_event_ir(decorated, context=context).source_fingerprint
    )


def test_source_fingerprint_changes_when_a_condition_value_changes(context) -> None:
    thirty = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 30}), context=context
    )
    sixty = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 60}), context=context
    )

    assert thirty.source_fingerprint != sixty.source_fingerprint
    assert thirty.fingerprint != sixty.fingerprint
    assert migration.stale_against(thirty.source_fingerprint, sixty)


def test_semantic_fingerprint_ignores_evidence_but_not_the_event_source() -> None:
    """범용 provenance 정규형을 쓰면 ``event_reference.source`` 까지 지워져 의미가 뭉개진다."""

    evidence = event_ir.Evidence(text="3월에 구매", start=0, end=6)
    without = event_ir.Exists(relation=event_ir.Source("purchase"))
    with_evidence = event_ir.Exists(relation=event_ir.Source("purchase"), evidence=evidence)
    other_source = event_ir.Exists(relation=event_ir.Source("login"))

    assert (
        migration_fingerprint.compute_semantic_fingerprint(without.to_dict())
        == migration_fingerprint.compute_semantic_fingerprint(with_evidence.to_dict())
    )
    assert (
        migration_fingerprint.compute_semantic_fingerprint(without.to_dict())
        != migration_fingerprint.compute_semantic_fingerprint(other_source.to_dict())
    )

    purchase_relation = event_ir.TemporalRelation(
        operator="within_after",
        left=event_ir.EventReference(source="purchase"),
        right=event_ir.EventReference(source="purchase"),
        duration=event_ir.Duration(value=30, unit="day"),
    )
    login_relation = event_ir.TemporalRelation(
        operator="within_after",
        left=event_ir.EventReference(source="login"),
        right=event_ir.EventReference(source="purchase"),
        duration=event_ir.Duration(value=30, unit="day"),
    )
    assert (
        migration_fingerprint.compute_semantic_fingerprint(purchase_relation.to_dict())
        != migration_fingerprint.compute_semantic_fingerprint(login_relation.to_dict())
    )


def test_binding_fingerprint_moves_with_the_physical_binding_while_meaning_holds(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 30}), context=context
    )
    renamed = migration_fingerprint.compute_binding_fingerprint(
        result.expression.to_dict(),
        catalog_version=context.catalog_version,
        compiler_version=context.compiler_version,
        bindings={**result.bindings, "purchase": {**result.bindings["purchase"], "table": "OTHER_TABLE"}},
    )
    upgraded = migration_fingerprint.compute_binding_fingerprint(
        result.expression.to_dict(),
        catalog_version=context.catalog_version,
        compiler_version="9.9.9",
        bindings=result.bindings,
    )

    assert result.binding_fingerprint not in (renamed, upgraded)
    assert result.fingerprint == migration_fingerprint.compute_semantic_fingerprint(
        result.expression.to_dict()
    )


def test_schema_checksum_detects_a_new_key_even_with_the_same_conditions(context) -> None:
    before = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 30}), context=context
    )
    after = migration.legacy_slot_to_event_ir(
        _plan(
            purchase_membership={"operator": "exists", "window_days": 30},
            unknown_future_slot={"value": 1},
        ),
        context=context,
    )

    assert before.source_schema_checksum != after.source_schema_checksum


# ── ③ 경로 회계 ───────────────────────────────────────────────────────────────────


def test_every_container_key_lands_in_exactly_one_bucket(context) -> None:
    plan = _plan(
        purchase_date={"from": "20190301", "to": "20190331", "label": "3월"},
        purchase_membership={"operator": "exists", "window_days": None},
        recent_login={"value": 30, "unit": "days", "min_days": 30},
        gender="female",
        interests=[],
        behaviors=["no_purchase", "first_purchase"],
    )

    result = migration.legacy_slot_to_event_ir(plan, context=context)
    accounted = (
        set(result.consumed_paths)
        | {item.path for item in result.ignored_paths}
        | set(result.unmapped_paths)
        | set(result.invalid_paths)
    )

    for key in plan["target_user"]:
        path = f"target_user.{key}"
        covering = [
            entry for entry in accounted
            if entry == path or entry.startswith(f"{path}.") or entry.startswith(f"{path}[")
        ]
        assert covering, f"{path} 가 어느 버킷에도 없다 — 조용히 지나간 경로다"
    assert not any(entry.startswith("exclude.") for entry in accounted)  # 이 자산에는 exclude 가 없다


def test_unknown_slot_is_reported_not_skipped(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(
            purchase_membership={"operator": "exists", "window_days": None},
            some_new_slot_nobody_declared={"threshold": 3},
        ),
        context=context,
    )

    assert not result.is_executable
    assert "target_user.some_new_slot_nobody_declared" in result.unmapped_paths
    assert {error.code for error in result.errors} == {"LEGACY_PATH_UNCLASSIFIED"}
    assert result.status is MigrationStatus.BLOCKED_DOMAIN_DECISION


def test_only_non_semantic_paths_may_be_ignored(context) -> None:
    """의미 필드(연산자·기간·값·극성)가 ignored 로 새면 조용히 다른 조건이 된다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(
            purchase_date={"from": "20190301", "to": "20190331", "label": "3월"},
            purchase_inactivity={"value": 90, "unit": "days", "min_days": 90, "sql_interval": "90 days"},
            aggregate_conditions=[
                {"metric_id": "order_count", "operator": ">=", "threshold": 3, "label": "3회 이상"},
            ],
        ),
        context=context,
    )

    semantic_leaves = {"from", "to", "operator", "threshold", "min_days", "value", "unit",
                       "window_days", "metric_id", "windows", "negated"}
    for item in result.ignored_paths:
        leaf = item.path.rsplit(".", 1)[-1]
        assert leaf not in semantic_leaves, f"의미 필드가 무시됐다: {item.path}"
        assert item.reason and item.category, "사유 없는 무시는 허용하지 않는다"
    assert {item.category for item in result.ignored_paths} <= {
        "ui_label", "render_derivative", "empty_slot", "non_semantic_metadata",
    }


def test_provenance_maps_one_slot_to_several_nodes(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_date={
            "from": "20180101", "to": "20191231",
            "windows": [{"from": "20180101", "to": "20181231"}, {"from": "20190101", "to": "20191231"}],
        }),
        context=context,
    )

    edges = [edge for edge in result.provenance if edge.transformation == "purchase_date"]
    assert sum(len(edge.target_node_ids) for edge in edges) == 2
    assert {path for edge in edges for path in edge.source_paths} == {
        "target_user.purchase_date.windows[0]", "target_user.purchase_date.windows[1]",
    }


# ── ④ fail-close ──────────────────────────────────────────────────────────────────


def test_partial_conversion_is_never_executable(context) -> None:
    """A 그룹 하나가 변환되어도 옆에 미매핑이 있으면 실행 가능이 아니다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(
            purchase_membership={"operator": "exists", "window_days": 30},
            member_metric_ranking={"metric_id": "balance", "direction": "high", "top_n": 100},
        ),
        context=context,
    )

    assert result.expression is not None  # 변환된 조각은 관찰 가능하지만
    assert result.is_executable is False  # 실행 가능은 아니다
    assert result.status is MigrationStatus.BLOCKED_IR_EXTENSION


def test_unknown_operator_fails_closed(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(aggregate_conditions=[
            {"metric_id": "order_count", "operator": "approximately", "threshold": 3},
        ]),
        context=context,
    )

    assert not result.is_executable
    assert result.status is MigrationStatus.INVALID_LEGACY_ASSET
    assert {error.code for error in result.errors} == {"UNKNOWN_OPERATOR"}


def test_zero_event_member_semantics_block_the_at_most_direction(context) -> None:
    """legacy 집계는 INNER JOIN 이라 무주문 회원을 평가하지 않는다 — '이하'는 같은 집합이 아니다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(aggregate_conditions=[
            {"metric_id": "order_count", "operator": "<=", "threshold": 3},
        ]),
        context=context,
    )

    assert not result.is_executable
    assert result.status is MigrationStatus.BLOCKED_DOMAIN_DECISION
    assert {error.code for error in result.errors} == {"AGGREGATE_ZERO_EVENT_MEMBER_SEMANTICS"}


def test_unresolvable_catalog_metric_is_a_catalog_block(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(aggregate_conditions=[
            {"metric_id": "distinct_brand_count", "operator": ">=", "threshold": 3},
        ]),
        context=context,
    )

    assert not result.is_executable
    assert result.status is MigrationStatus.BLOCKED_CATALOG


def test_time_of_day_bounds_are_an_ir_extension_not_a_silent_drop(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_date={
            "from": "20190301", "to": "20190331", "from_time": "090000", "to_time": "180000",
        }),
        context=context,
    )

    assert not result.is_executable
    assert result.status is MigrationStatus.BLOCKED_IR_EXTENSION
    assert "target_user.purchase_date.from_time" in result.unmapped_paths


def test_non_executable_legacy_date_token_is_a_domain_decision(context) -> None:
    """legacy 는 8자리가 아닌 토큰의 술어를 만들지 않는다 — 변환은 조건을 **추가**하는 셈이다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_date={"from": "2019", "to": "2019"}), context=context
    )

    assert not result.is_executable
    assert result.status is MigrationStatus.BLOCKED_DOMAIN_DECISION
    assert {error.code for error in result.errors} == {"DATE_TOKEN_NOT_EXECUTABLE"}


def test_inconsistent_window_surface_is_an_invalid_asset(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_inactivity={"value": 3, "unit": "months", "min_days": 45}), context=context
    )

    assert not result.is_executable
    assert result.status is MigrationStatus.INVALID_LEGACY_ASSET


def test_an_audience_without_conditions_is_a_domain_decision(context) -> None:
    """조건 0개를 '변환 완료'로 부르면 조건 없는 표현이 전체 회원을 뜻하게 된다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(gender=None, behaviors=[], aggregate_conditions=[]), context=context
    )

    assert result.expression is None
    assert not result.is_executable
    assert result.status is MigrationStatus.BLOCKED_DOMAIN_DECISION
    assert {error.code for error in result.errors} == {"NO_AUDIENCE_CONDITION"}


def test_exclude_container_is_out_of_this_wave(context) -> None:
    plan = _plan(purchase_membership={"operator": "exists", "window_days": None})
    plan["exclude"] = {"gender": ["male"], "interests": []}

    result = migration.legacy_slot_to_event_ir(plan, context=context)

    assert not result.is_executable
    assert "exclude.gender" in result.unmapped_paths
    assert any(item.path == "exclude.interests" for item in result.ignored_paths)


def test_aggregate_without_catalog_is_blocked_rather_than_guessed() -> None:
    bare = migration.MigrationContext(
        as_of=AS_OF, timezone="Asia/Seoul", catalog_version="none",
        compiler_version=event_compiler.COMPILER_VERSION, catalog=None,
    )

    result = migration.legacy_slot_to_event_ir(
        _plan(aggregate_conditions=[{"metric_id": "order_count", "operator": ">=", "threshold": 3}]),
        context=bare,
    )

    assert not result.is_executable
    assert {error.code for error in result.errors} == {"CATALOG_NOT_INJECTED"}


# ── ⑤ 시간 계약과 경계 ────────────────────────────────────────────────────────────


def test_migration_context_refuses_an_implicit_timezone() -> None:
    with pytest.raises(migration.MigrationContextError):
        migration.MigrationContext(
            as_of=datetime(2026, 8, 3, 0, 0), timezone="Asia/Seoul",
            catalog_version="v", compiler_version="v",
        )
    with pytest.raises(migration.MigrationContextError):
        migration.MigrationContext(
            as_of=AS_OF, timezone="", catalog_version="v", compiler_version="v",
        )
    with pytest.raises(migration.MigrationContextError):
        migration.MigrationContext(
            as_of=AS_OF, timezone="Mars/Olympus", catalog_version="v", compiler_version="v",
        )


def test_as_of_date_follows_the_declared_zone_not_the_process_clock() -> None:
    midnight_utc = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)  # = 2026-08-03 00:00 KST
    seoul = migration.MigrationContext(
        as_of=midnight_utc, timezone="Asia/Seoul", catalog_version="v", compiler_version="v")
    utc = migration.MigrationContext(
        as_of=midnight_utc, timezone="UTC", catalog_version="v", compiler_version="v")

    assert seoul.as_of_date.isoformat() == "2026-08-03"
    assert utc.as_of_date.isoformat() == "2026-08-02"


@pytest.mark.parametrize(
    ("start", "end", "expected_end_exclusive"),
    [
        ("20190301", "20190331", "2019-04-01"),   # 월말
        ("20200201", "20200229", "2020-03-01"),   # 윤년 2월
        ("20190101", "20191231", "2020-01-01"),   # 연말
        ("20190301", "20190301", "2019-03-02"),   # 하루짜리 구간
    ],
)
def test_absolute_interval_boundaries_are_half_open(context, start, end, expected_end_exclusive) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_date={"from": start, "to": end}), context=context
    )
    window = event_ir.time_windows(result.expression)[0]

    assert window.start.strftime("%Y%m%d") == start
    assert window.end_exclusive.isoformat() == expected_end_exclusive
    assert window.inclusive_end.strftime("%Y%m%d") == end


def test_rolling_windows_are_normalized_to_days_like_the_legacy_predicate(context) -> None:
    """legacy 술어는 min_days 만 쓴다 — '3개월'과 '90일'이 이미 같은 조건이다."""

    months = migration.legacy_slot_to_event_ir(
        _plan(purchase_inactivity={"value": 3, "unit": "months", "min_days": 90}), context=context
    )
    days = migration.legacy_slot_to_event_ir(
        _plan(purchase_inactivity={"value": 90, "unit": "days", "min_days": 90}), context=context
    )

    assert months.fingerprint == days.fingerprint
    assert event_ir.time_windows(months.expression)[0].days == 90


def test_rolling_cutoff_is_rendered_at_execution_time_not_frozen(context) -> None:
    """계획 시점 날짜로 굳으면 '최근 90일'이 자산이 만들어진 날에 고정된다."""

    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_inactivity={"value": 90, "unit": "days", "min_days": 90}), context=context
    )
    sql = _compile(result.expression, context)

    assert "GETDATE()" in sql and AS_OF.strftime("%Y%m%d") not in sql


def test_boundary_day_membership_matches_the_legacy_predicate(context) -> None:
    """경계 하루 전/당일/다음날이 같은 컷오프 식으로 판정되는지(문자열 동일)."""

    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 30}), context=context
    )
    sql = _compile(result.expression, context)

    assert graph_rag._member_dialect().char8_cutoff(30) in sql


# ── ⑥ legacy 실행 술어와의 의미 동등성(A 그룹의 근거) ─────────────────────────────


def test_purchase_membership_compiles_to_the_same_predicate_shape(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 30}), context=context
    )
    legacy_sql = graph_rag._purchase_membership_predicate(30)
    event_sql = _compile(result.expression, context)

    assert event_sql.replace(" EO", " O").replace("EO.", "O.") == legacy_sql
    assert "EXISTS" in event_sql and "NOT EXISTS" not in event_sql
    assert graph_rag._member_dialect().char8_cutoff(30) in event_sql


def test_purchase_inactivity_compiles_to_the_same_anti_join(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_inactivity={"value": 90, "unit": "days", "min_days": 90}), context=context
    )
    legacy_sql = graph_rag._purchase_inactivity_predicate(90)
    event_sql = _compile(result.expression, context)

    # 별칭만 다르고(legacy 는 O, Event IR 은 카탈로그 선언 별칭) 나머지는 문자열 동일이다.
    # 이 등식이 '무손실 변환'의 가장 강한 근거이므로 여기서 고정한다.
    assert event_sql.replace(" EO", " O").replace("EO.", "O.") == legacy_sql
    assert "NOT EXISTS" in legacy_sql
    assert graph_rag._member_dialect().char8_cutoff(90) in event_sql


def test_purchase_date_covers_the_same_days_as_the_legacy_between(context) -> None:
    slot = {"from": "20190301", "to": "20190331"}
    legacy_sql = graph_rag._purchase_date_predicate(slot, alias="EO")
    result = migration.legacy_slot_to_event_ir(_plan(purchase_date=slot), context=context)
    event_sql = _compile(result.expression, context)

    assert legacy_sql == "EO.ORDER_DATE BETWEEN '20190301' AND '20190331'"
    assert "EO.ORDER_DATE >= '20190301'" in event_sql
    assert "EO.ORDER_DATE < '20190401'" in event_sql
    # 일 단위 char8 컬럼에서 [20190301, 20190401) 과 BETWEEN 20190301..20190331 은 같은 날짜 집합이다.
    day = timedelta(days=1)
    window = event_ir.time_windows(result.expression)[0]
    assert window.inclusive_end + day == window.end_exclusive


# ── manifest / envelope ───────────────────────────────────────────────────────────


def test_envelope_keeps_the_existing_event_expression_shape(context) -> None:
    result = migration.legacy_slot_to_event_ir(
        _plan(purchase_membership={"operator": "exists", "window_days": 30}), context=context
    )
    envelope = migration.build_migration_envelope(
        result, asset_id="aud-1", asset_revision=2, context=context,
        converted_at=AS_OF.isoformat(),
    )

    # 기존 소비자가 읽는 세 키가 같은 자리에 있어야 역직렬화가 깨지지 않는다.
    assert set(envelope) >= {"expression", "source", "receipts"}
    assert envelope["expression"] == result.expression.to_dict()
    assert envelope["migration"]["source_revision"] == 2
    assert envelope["migration"]["semantic_fingerprint"] == result.fingerprint
    assert envelope["migration"]["validated_at"] is None
    # 저장은 됐지만 실행 권위는 여전히 legacy 다.
    from audience_authority import resolve_authority
    assert resolve_authority({"event_expression": envelope}) is AudienceAuthority.LEGACY


def test_manifest_item_lists_slots_and_reason_codes(context) -> None:
    plan = _plan(
        purchase_membership={"operator": "exists", "window_days": 30},
        metric_trend={"metric_id": "purchase_amount", "direction": "increase"},
    )
    result = migration.legacy_slot_to_event_ir(plan, context=context)
    item = migration.manifest_item("aud-9", 3, plan, result)

    assert item.asset_id == "aud-9" and item.asset_revision == 3
    assert item.slot_types == ("target_user.metric_trend", "target_user.purchase_membership")
    assert item.migration_status == MigrationStatus.BLOCKED_IR_EXTENSION.value
    assert "PERIOD_OVER_PERIOD_NOT_EXPRESSIBLE" in item.reason_codes


def test_adapter_never_mutates_the_legacy_payload(context) -> None:
    """원본 legacy payload 유실 0건 — 어댑터가 입력을 건드리지 않는다."""

    plan = _plan(
        purchase_date={"from": "20190301", "to": "20190331", "label": "3월"},
        aggregate_conditions=[{"metric_id": "order_count", "operator": ">=", "threshold": 3}],
    )
    snapshot = copy.deepcopy(plan)

    migration.legacy_slot_to_event_ir(plan, context=context)

    assert plan == snapshot


def test_declared_blocked_slots_cover_the_plan_skeleton(context) -> None:
    """플랜 스켈레톤의 오디언스 슬롯은 전부 '변환' 또는 '선언된 막힘' 중 하나여야 한다.

    빠진 슬롯은 LEGACY_PATH_UNCLASSIFIED 로 떨어져도 안전하지만, 그것은 '아직 아무도 보지 않았다'는
    뜻이다 — 스켈레톤에 있는 슬롯만큼은 이유를 말할 수 있어야 한다.
    """

    skeleton = graph_rag._empty_query_plan("q")["target_user"]
    undeclared = sorted(
        key for key in skeleton
        if key not in migration.CONVERTIBLE_SLOTS
        and key not in migration.BLOCKED_SLOTS
        and key not in migration.NON_SEMANTIC_KEYS
    )

    assert not undeclared, f"이행 분류가 선언되지 않은 스켈레톤 슬롯: {undeclared}"
