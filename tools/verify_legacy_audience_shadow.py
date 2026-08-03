"""shadow 검증 실행기 — legacy 실행 권위를 **그대로 둔 채** 두 산출물을 대조한다.

    python -m tools.verify_legacy_audience_shadow \
      --assets tests/fixtures/legacy_audience_assets.json \
      --fixture tests/fixtures/audience_shadow_fixture.json \
      --snapshot-db CRMDW \
      --output shadow-report.json

이 도구도 쓰지 않는다: 저장·권위 이관·cut-over 는 전부 밖에 있다. 여기서 나오는 것은
"이 자산을 SHADOW_VERIFIED 로 올려도 되는가"에 대한 **근거와 차단 사유**뿐이다.

legacy 쪽 산출물은 어댑터를 거치지 않고 :mod:`graph_rag` 의 실행 헬퍼로 직접 렌더한다 —
같은 코드에서 나온 두 값을 비교하면 검증이 동어반복이 되기 때문이다.

``--snapshot-db`` 를 주면 ⑤(실데이터 회원 집합 대조)가 그 **읽기 전용** 연결에서 돌아간다.
주지 않으면 ⑤ 는 사유와 함께 미실행이고, 필수 단계이므로 cut-over 는 계속 막힌다 — 접근이
없다는 사실이 검증을 통과시키는 길이 되어서는 안 된다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import audience_shadow as shadow  # noqa: E402
import event_compiler  # noqa: E402
import legacy_audience_migration as migration  # noqa: E402
import sql_dialect  # noqa: E402
from tools.migrate_legacy_audience import (  # noqa: E402
    DEFAULT_TIMEZONE, build_context, load_assets,
)

DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "audience_shadow_fixture.json"
DEFAULT_SAMPLE_LIMIT = 20
# 비용 퇴행 판정 기본값. 둘 다 넘어야 퇴행이다(비율만 보면 2ms→4ms 가 '2배 퇴행'이 되고,
# 절대 차이만 보면 느린 질의의 배수 퇴행을 놓친다).
DEFAULT_COST_RATIO = 1.5
DEFAULT_COST_NOISE_FLOOR_MS = 50.0

# legacy 쪽 산출물을 렌더할 수 있는 슬롯. 여기 없는 슬롯이 자산에 있으면 ③④ 는 **대조 불가**이고,
# 그 사실이 미실행 사유로 보고서에 남는다(모르는 것을 '같다'로 세지 않는다).
RENDERABLE_SLOTS: frozenset[str] = frozenset({
    "purchase_membership", "purchase_inactivity", "behaviors", "aggregate_conditions",
})


def _legacy_member_sql(payload: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """자산의 legacy 실행 산출물(회원 술어/문장)을 렌더한다. 반환: (SQL, 대조 불가 슬롯)."""
    import graph_rag

    container = payload.get("target_user")
    container = container if isinstance(container, Mapping) else {}
    predicates: list[str] = []
    statements: list[str] = []
    uncomparable: list[str] = []

    for slot in sorted(container):
        value = container.get(slot)
        if not value or slot not in migration.CONVERTIBLE_SLOTS:
            continue
        if slot not in RENDERABLE_SLOTS:
            uncomparable.append(f"target_user.{slot}")
            continue
        if slot == "purchase_membership":
            predicates.append(graph_rag._purchase_membership_predicate(value.get("window_days")))
        elif slot == "purchase_inactivity":
            predicates.append(graph_rag._purchase_inactivity_predicate(int(value["min_days"])))
        elif slot == "behaviors":
            if "no_purchase" in {str(item) for item in value}:
                predicates.append(graph_rag._no_purchase_anti_join_predicate())
        elif slot == "aggregate_conditions":
            statement = _legacy_aggregate_statement(value)
            if statement is None:
                uncomparable.append(f"target_user.{slot}")
            else:
                statements.append(statement)

    if uncomparable:
        # **부분 렌더는 대조하지 않는다.** legacy 쪽 조건 하나가 빠진 채 비교하면 반드시 차이가 나고,
        # 그 차이는 자산의 성질이 아니라 이 렌더러의 한계다 — 그것을 divergence 로 보고하면
        # 보고서가 잡음으로 채워지고 진짜 차이가 묻힌다.
        return "", tuple(sorted(uncomparable))
    if statements and predicates:
        # 집계는 조인 문장이고 나머지는 술어다. 둘을 한 문장으로 합치려면 legacy 빌더의 조립 규칙을
        # 여기서 흉내내야 하는데, 흉내낸 것과 대조하면 검증이 아니라 자기확인이 된다.
        return "", ("target_user.aggregate_conditions",)
    if statements:
        return (statements[0], ()) if len(statements) == 1 else ("", ("target_user.aggregate_conditions",))
    if not predicates:
        return "", ()
    return " AND ".join(f"({item})" for item in predicates), ()


def _legacy_aggregate_statement(conditions: Any) -> str | None:
    """집계 조건의 legacy 회원 조회 문장(INNER JOIN + GROUP BY … HAVING)."""
    import graph_rag

    if not isinstance(conditions, list) or len(conditions) != 1:
        return None
    condition = conditions[0]
    if not isinstance(condition, Mapping):
        return None
    config = graph_rag._aggregate_targets_config()
    metric = (config.get("metrics") or {}).get(str(condition.get("metric_id")))
    if not isinstance(metric, Mapping):
        return None
    subquery = graph_rag._aggregate_member_subquery(
        dict(config), dict(metric), str(condition.get("operator")), condition.get("threshold"),
        condition.get("window_days"), "A",
    )
    if subquery is None:
        return None
    join_column = config.get("join_column")
    # 주체 테이블·키는 카탈로그가 소유한다 — 검증기가 그 이름을 자기 소스에 적으면 물리 바인딩
    # 소유자가 하나 더 생기고, 이름이 바뀌는 날 검증기만 옛 이름으로 대조한다.
    table, alias, key = shadow.subject_binding()
    return (
        f"SELECT {alias}.{key} FROM {table} {alias} "
        f"INNER JOIN {subquery} ON A.{join_column} = {alias}.{key}"
    )


def _event_ir_sql(result: Any, context: migration.MigrationContext) -> str:
    compiled = event_compiler.compile_expression(
        result.expression,
        context=context.catalog.compile_context(literals=True, today=context.as_of_date),
    )
    return compiled.sql


# ── ⑤ 스냅샷 대조 실행기 ──────────────────────────────────────────────────────────
# 연결은 **여기서만** 만든다. 검증 계층(audience_shadow)은 실행자를 주입받을 뿐이고, 그래야
# "어느 DB 에 붙는가"가 검증기 안에서 정해지지 않는다.


@dataclass(frozen=True)
class SnapshotRunner:
    connection: str
    execute: Any
    dialect: Any
    sample_limit: int = DEFAULT_SAMPLE_LIMIT
    pin_clock: bool = True
    # ⑥ 비용 측정은 자산마다 질의를 (반복+1)×2 번 돌린다. 실DB 부하를 만드는 유일한 자리라
    # 기본을 끄고, 켤 때 반복 횟수를 명시하게 한다.
    cost_repetitions: int = 0
    cost_ratio_threshold: float = DEFAULT_COST_RATIO
    cost_noise_floor_ms: float = DEFAULT_COST_NOISE_FLOOR_MS

    @property
    def measures_cost(self) -> bool:
        return self.cost_repetitions > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "dialect": self.dialect.name,
            "sample_limit": self.sample_limit,
            "pin_clock": self.pin_clock,
            "cost_repetitions": self.cost_repetitions,
            "cost_ratio_threshold": self.cost_ratio_threshold,
            "cost_noise_floor_ms": self.cost_noise_floor_ms,
        }


def build_snapshot_runner(args: argparse.Namespace) -> SnapshotRunner | None:
    """``--snapshot-db`` 가 있을 때만 실행기를 만든다(없으면 ⑤ 는 사유와 함께 미실행)."""
    if not args.snapshot_db:
        return None
    import db_connections  # 지연 import — 스냅샷을 쓰지 않는 실행에 DB 의존을 끌어오지 않는다.

    connection = str(args.snapshot_db)
    if connection not in db_connections.ALL_DBS:
        raise shadow.ShadowVerificationError(
            f"알 수 없는 스냅샷 연결: {connection!r} (사용 가능: {', '.join(db_connections.ALL_DBS)})"
        )

    def execute(sql: str) -> Any:
        # run_read_query 가 SELECT 강제(sql_guard)·읽기 전용 트랜잭션을 소유한다 — 이 도구는
        # 그 경로 밖으로 질의를 내보내지 않는다.
        return db_connections.run_read_query(connection, sql)

    return SnapshotRunner(
        connection=connection,
        execute=execute,
        dialect=sql_dialect.dialect_for_connection(connection, default="tsql"),
        sample_limit=max(0, int(args.snapshot_sample_limit)),
        pin_clock=not args.snapshot_unpinned_clock,
        cost_repetitions=max(0, int(args.cost_repetitions)),
        cost_ratio_threshold=float(args.cost_ratio_threshold),
        cost_noise_floor_ms=float(args.cost_noise_floor_ms),
    )


def snapshot_stage_for(
    runner: SnapshotRunner | None, *, legacy_sql: str, event_ir_sql: str, unavailable: str
) -> shadow.StageResult:
    """⑤ 한 자산분. 실행할 수 없는 모든 사유는 **미실행(사유 기록)** 으로 떨어진다 — 통과가 아니다."""
    if runner is None:
        return shadow.not_run(
            shadow.STAGE_SNAPSHOT_MEMBERS,
            "실데이터 스냅샷 대조가 요청되지 않았다(--snapshot-db 미지정)",
        )
    if unavailable:
        return shadow.not_run(shadow.STAGE_SNAPSHOT_MEMBERS, unavailable)
    try:
        comparison = shadow.run_snapshot_comparison(
            legacy_sql=legacy_sql, event_ir_sql=event_ir_sql,
            execute=runner.execute, dialect=runner.dialect,
            sample_limit=runner.sample_limit, pin_clock=runner.pin_clock,
        )
    except shadow.ShadowVerificationError as exc:
        return shadow.not_run(shadow.STAGE_SNAPSHOT_MEMBERS, str(exc))
    except Exception as exc:  # noqa: BLE001 — 연결·드라이버·타임아웃 실패는 '같다'가 아니라 미실행이다.
        return shadow.not_run(
            shadow.STAGE_SNAPSHOT_MEMBERS,
            f"스냅샷 질의를 실행하지 못했다({runner.connection}): {exc.__class__.__name__}: {exc}",
        )
    return shadow.snapshot_stage(comparison)


def performance_stage_for(
    runner: SnapshotRunner | None, *, legacy_sql: str, event_ir_sql: str, unavailable: str
) -> shadow.StageResult:
    """⑥ 한 자산분. 비용 측정은 **명시적으로 켤 때만** 돈다(실DB 에 부하를 만드는 유일한 자리)."""
    if runner is None or not runner.measures_cost:
        return shadow.not_run(
            shadow.STAGE_PERFORMANCE,
            "실행 비용 대조가 요청되지 않았다(--snapshot-db 와 --cost-repetitions 필요)",
        )
    if unavailable:
        return shadow.not_run(shadow.STAGE_PERFORMANCE, unavailable)
    try:
        comparison = shadow.run_performance_comparison(
            legacy_sql=legacy_sql, event_ir_sql=event_ir_sql,
            execute=runner.execute, dialect=runner.dialect,
            repetitions=runner.cost_repetitions,
            ratio_threshold=runner.cost_ratio_threshold,
            noise_floor_ms=runner.cost_noise_floor_ms,
            pin_clock=runner.pin_clock,
        )
    except shadow.ShadowVerificationError as exc:
        return shadow.not_run(shadow.STAGE_PERFORMANCE, str(exc))
    except Exception as exc:  # noqa: BLE001
        return shadow.not_run(
            shadow.STAGE_PERFORMANCE,
            f"비용 질의를 실행하지 못했다({runner.connection}): {exc.__class__.__name__}: {exc}",
        )
    return shadow.performance_stage(comparison)


def verify_asset(
    asset: Mapping[str, Any],
    context: migration.MigrationContext,
    *,
    fixture_spec: Mapping[str, Any],
    anchor: date,
    snapshot: SnapshotRunner | None = None,
) -> shadow.ShadowReport:
    payload = asset["payload"]
    result = migration.legacy_slot_to_event_ir(payload, context=context)
    stages: list[shadow.StageResult] = [shadow.path_accounting_stage(result)]

    if not result.is_executable or result.expression is None:
        reason = "변환이 실행 가능 상태가 아니라 대조할 Event IR 산출물이 없다"
        stages.extend([
            shadow.not_run(shadow.STAGE_SEMANTIC_FINGERPRINT, reason),
            shadow.not_run(shadow.STAGE_SQL_STRUCTURE, reason),
            shadow.not_run(shadow.STAGE_ADVERSARIAL_FIXTURE, reason),
            shadow.not_run(shadow.STAGE_SNAPSHOT_MEMBERS, reason),
            shadow.not_run(shadow.STAGE_PERFORMANCE, reason),
        ])
        return shadow.ShadowReport(
            asset_id=asset["asset_id"], asset_revision=asset["revision"], stages=tuple(stages),
            semantic_fingerprint=result.fingerprint, source_fingerprint=result.source_fingerprint,
        )

    repeat = migration.legacy_slot_to_event_ir(payload, context=context)
    stages.append(shadow.fingerprint_stage(result, repeat))

    event_sql = _event_ir_sql(result, context)
    legacy_sql, uncomparable = _legacy_member_sql(payload)
    unrenderable = ""
    if not legacy_sql:
        unrenderable = (
            "legacy 실행 산출물을 독립적으로 렌더할 수 없는 슬롯이 있다"
            + (f"({', '.join(uncomparable)})" if uncomparable else "")
            + " — 흉내낸 legacy 와 대조하면 검증이 자기확인이 된다"
        )
        stages.extend([
            shadow.not_run(shadow.STAGE_SQL_STRUCTURE, unrenderable),
            shadow.not_run(shadow.STAGE_ADVERSARIAL_FIXTURE, unrenderable),
        ])
    else:
        stages.append(shadow.sql_structure_stage(legacy_sql, event_sql))
        try:
            comparison = shadow.run_fixture_comparison(
                legacy_sql=legacy_sql, event_ir_sql=event_sql,
                tables=shadow.build_fixture_tables(fixture_spec, anchor=anchor),
            )
            stages.append(shadow.fixture_stage(comparison))
        except shadow.ShadowVerificationError as exc:
            stages.append(shadow.not_run(shadow.STAGE_ADVERSARIAL_FIXTURE, str(exc)))

    stages.append(snapshot_stage_for(
        snapshot, legacy_sql=legacy_sql, event_ir_sql=event_sql, unavailable=unrenderable,
    ))
    stages.append(performance_stage_for(
        snapshot, legacy_sql=legacy_sql, event_ir_sql=event_sql, unavailable=unrenderable,
    ))
    return shadow.ShadowReport(
        asset_id=asset["asset_id"], asset_revision=asset["revision"], stages=tuple(stages),
        semantic_fingerprint=result.fingerprint, source_fingerprint=result.source_fingerprint,
    )


def _summary(reports: list[shadow.ShadowReport]) -> dict[str, Any]:
    stage_counts: dict[str, dict[str, int]] = {}
    kinds: dict[str, int] = {}
    for report in reports:
        for stage in report.stages:
            bucket = stage_counts.setdefault(stage.stage, {})
            bucket[stage.status] = bucket.get(stage.status, 0) + 1
        for divergence in report.divergences:
            kinds[divergence.kind] = kinds.get(divergence.kind, 0) + 1
    return {
        "assets": len(reports),
        "cutover_allowed": sum(1 for report in reports if report.cutover_allowed),
        "blocked": sum(1 for report in reports if not report.cutover_allowed),
        "by_stage": {name: dict(sorted(counts.items())) for name, counts in sorted(stage_counts.items())},
        "divergence_kinds": dict(sorted(kinds.items())),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.verify_legacy_audience_shadow",
        description="legacy ↔ Event IR shadow 대조(실행 권위 변경 없음)",
    )
    parser.add_argument("--assets", help="자산 파일 경로")
    parser.add_argument("--source", choices=("file", "db"), default="file")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="경계 fixture 명세 경로")
    parser.add_argument("--approvals", help="승인된 legacy 버그 수정 기록(JSON 배열)")
    parser.add_argument("--asset-id", action="append")
    parser.add_argument("--slot-type", action="append")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", help="JSON 리포트 경로")
    parser.add_argument("--as-of", help="기준 시각(ISO8601)")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--snapshot-db",
        help="⑤ 실데이터 회원 집합 대조를 돌릴 읽기 전용 연결(예: CRMDW). 없으면 ⑤ 는 사유와 함께 미실행",
    )
    parser.add_argument(
        "--snapshot-sample-limit", type=int, default=DEFAULT_SAMPLE_LIMIT,
        help="차집합 회원 표본 상한(진단용). 잘린 사실은 보고서에 함께 실린다",
    )
    parser.add_argument(
        "--snapshot-unpinned-clock", action="store_true",
        help="기준 시각을 고정하지 않고 돌린다(측정용 진단 모드). 시계 함수가 남아 있으면 대조는 미실행이다",
    )
    parser.add_argument(
        "--cost-repetitions", type=int, default=0,
        help="⑥ 실행 비용 대조 반복 횟수(0=미실행). 자산마다 (반복+1)×2 회 질의가 실DB 에서 돈다",
    )
    parser.add_argument(
        "--cost-ratio-threshold", type=float, default=DEFAULT_COST_RATIO,
        help="p50 비용 비율이 이 값을 넘고 절대 차이도 바닥값을 넘으면 퇴행으로 본다",
    )
    parser.add_argument(
        "--cost-noise-floor-ms", type=float, default=DEFAULT_COST_NOISE_FLOOR_MS,
        help="비율만으로 퇴행이라 부르지 않기 위한 절대 차이 바닥값(ms)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        assets = load_assets(args)
        context = build_context(args)
        fixture_spec = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        approvals = (
            shadow.approvals_from_dicts(json.loads(Path(args.approvals).read_text(encoding="utf-8")))
            if args.approvals else {}
        )
        snapshot = build_snapshot_runner(args)
    except (OSError, ValueError, shadow.ShadowVerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # fixture 행의 기준일은 **검증 엔진의 오늘**이다(선언 as_of 가 아니다 — 사유는 engine_anchor_date).
    anchor = shadow.engine_anchor_date()
    reports = [
        shadow.apply_approvals(
            verify_asset(asset, context, fixture_spec=fixture_spec, anchor=anchor, snapshot=snapshot),
            approvals,
        )
        for asset in assets
    ]
    report = {
        "generated_at": datetime.now(dt_timezone.utc).isoformat(),
        "mode": "shadow",
        "context": context.to_dict(),
        "fixture_anchor_date": anchor.isoformat(),
        "snapshot": snapshot.to_dict() if snapshot else None,
        "summary": _summary(reports),
        "assets": [item.to_dict() for item in reports],
    }
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
