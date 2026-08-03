"""legacy 오디언스 슬롯 자산의 이행 배치 — **dry-run 이 기본값**이다.

    python -m tools.migrate_legacy_audience --assets <file.json> --output migration-report.json

이 도구가 하는 일과 하지 않는 일:

    한다      자산 읽기 · 변환(:mod:`legacy_audience_migration`) · 분류 · manifest/report 출력
              · offline replay(legacy 슬롯 ↔ Event IR 을 **같은 앵커**로 각각 컴파일해 대조)
    하지 않는다  저장 · 권위 변경 · cut-over · rollback

권위를 바꾸지 않는 것이 이 웨이브의 핵심 계약이다. 실행 권위는 검증(shadow)이 끝난 revision 에만
compare-and-swap 으로 옮기고, 그 단계는 이 도구가 아니라 별도 명령이 소유한다 — dry-run 도구가
쓰기를 할 수 있으면 "확인만 해보려던 실행"이 운영 상태를 바꾼다.

resume: ``--checkpoint`` 파일에 처리 완료 자산 키(``asset_id@revision``)를 누적한다. 같은 파일로
다시 실행하면 그 키는 건너뛴다. 어댑터가 순수 함수라 재실행은 멱등이고, checkpoint 는 **속도**를
위한 것이지 정확성을 위한 것이 아니다(중복 실행해도 결과가 같아야 한다는 계약이 먼저다).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import legacy_audience_migration as migration  # noqa: E402
import migration_fingerprint  # noqa: E402
from audience_authority import AudienceAuthority, MigrationStatus  # noqa: E402

DEFAULT_TIMEZONE = "Asia/Seoul"
# 메타데이터 DB 의 오디언스 자산 테이블. revision 컬럼이 없으므로 payload 가 선언한 값을 쓰고,
# 없으면 1 로 본다 — 없는 컬럼을 있는 척 읽지 않는다.
AUDIENCE_ASSET_TABLE = "campaign_target_audiences"


class AssetSourceError(RuntimeError):
    """자산을 읽지 못했다(경로·형식·연결)."""


def _asset_key(asset_id: str, revision: int) -> str:
    return f"{asset_id}@{revision}"


def _load_file_assets(path: Path) -> list[dict[str, Any]]:
    """JSON 배열 / ``{"assets": [...]}`` / JSONL 을 모두 받는다."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssetSourceError(f"자산 파일을 읽지 못했다: {path}: {exc}") from exc
    text = text.strip()
    if not text:
        return []
    if text[0] in "[{":
        payload = json.loads(text)
        rows = payload.get("assets") if isinstance(payload, Mapping) else payload
        if not isinstance(rows, list):
            raise AssetSourceError(f"자산 파일의 최상위는 배열이거나 {{assets: [...]}} 여야 한다: {path}")
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _load_db_assets(limit: int | None) -> list[dict[str, Any]]:
    import db_connections  # 지연 import — 파일 소스만 쓸 때 DB 의존을 끌어오지 않는다.

    suffix = f" LIMIT {int(limit)}" if isinstance(limit, int) and limit > 0 else ""
    sql = (
        f"SELECT audience_key, query_plan, created_at FROM {AUDIENCE_ASSET_TABLE}"
        f" ORDER BY audience_key{suffix}"
    )
    try:
        rows = db_connections.run_read_query("postgres", sql)
    except Exception as exc:  # noqa: BLE001 — 연결 실패는 사용자에게 그대로 보고한다.
        raise AssetSourceError(f"메타데이터 DB 에서 자산을 읽지 못했다: {exc}") from exc
    assets: list[dict[str, Any]] = []
    for row in rows:
        plan = row.get("query_plan")
        if isinstance(plan, str):
            plan = json.loads(plan)
        if not isinstance(plan, Mapping):
            continue
        assets.append({
            "asset_id": str(row.get("audience_key")),
            "revision": int(plan.get("revision") or 1),
            "payload": dict(plan),
        })
    return assets


def _normalize_asset(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    """자산 하나를 ``{asset_id, revision, payload}`` 로 정규화한다.

    payload 를 감싸지 않고 곧바로 query_plan 을 준 경우도 받는다 — 운영에서 실제로 꺼내는 모양이
    그쪽이고, 감싸기를 강요하면 손으로 만든 중간 파일이 하나 더 생긴다.
    """
    payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
    asset_id = str(raw.get("asset_id") or raw.get("audience_key") or f"asset-{index}")
    revision_raw = raw.get("revision") if raw.get("revision") is not None else payload.get("revision")
    try:
        revision = int(revision_raw) if revision_raw is not None else 1
    except (TypeError, ValueError):
        revision = 1
    return {"asset_id": asset_id, "revision": revision, "payload": dict(payload)}


def load_assets(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.assets:
        raw = _load_file_assets(Path(args.assets))
    elif args.source == "db":
        raw = _load_db_assets(args.limit)
    else:
        raise AssetSourceError("--assets 파일 또는 --source db 중 하나를 지정해야 한다")
    assets = [_normalize_asset(item, index) for index, item in enumerate(raw)]
    if args.asset_id:
        wanted = set(args.asset_id)
        assets = [asset for asset in assets if asset["asset_id"] in wanted]
    if args.slot_type:
        wanted_slots = set(args.slot_type)
        assets = [
            asset for asset in assets
            if wanted_slots & {
                path.rpartition(".")[2] for path in migration.audience_slot_types(asset["payload"])
            }
        ]
    if isinstance(args.limit, int) and args.limit > 0:
        assets = assets[: args.limit]
    return assets


def build_context(args: argparse.Namespace) -> migration.MigrationContext:
    zone = ZoneInfo(args.timezone)
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of)
        as_of = as_of.replace(tzinfo=zone) if as_of.tzinfo is None else as_of
    else:
        as_of = datetime.now(dt_timezone.utc).astimezone(zone)
    catalog = audience_runtime.resolve_audience_catalog()
    config = audience_runtime.load_audience_catalog_config()
    # 카탈로그 '버전'은 선언된 버전 문자열 + 그 선언 전체의 체크섬이다. 선언 버전만 쓰면 아무도
    # 올리지 않은 채 내용이 바뀌고, 체크섬만 쓰면 사람이 읽을 수 없다.
    catalog_version = f"{config.get('version', '0')}+{migration_fingerprint.digest(config)[:12]}"
    return migration.MigrationContext(
        as_of=as_of,
        timezone=args.timezone,
        catalog_version=catalog_version,
        compiler_version=event_compiler.COMPILER_VERSION,
        catalog=catalog,
    )


# ── offline replay: legacy 실행 술어 ↔ Event IR 컴파일 결과 ────────────────────────
# SQL **문자열 완전 일치**를 요구하지 않는다. 같은 뜻을 다른 구조로 적을 수 있기 때문이다.
# 다만 회원키 상관 술어로 곧장 렌더되는 세 계열은 legacy 헬퍼가 순수 함수라 **문자열 대조가 가능**하고,
# 그 대조가 가장 강한 등가 근거이므로 가능한 곳에서는 그것을 쓴다. 나머지 계열(기간 존재·집계)은
# legacy 쪽이 빌더 문맥(조인·별칭·서브쿼리 배치)에 얽혀 있어 이 웨이브에서는 구조 대조만 보고한다.
_ALIAS_TOKEN = re.compile(r"\b(?:EO|O)\b")


def _legacy_predicates(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(문자열 대조 가능한 legacy 술어들, 대조 불가로 남은 슬롯들)."""
    import graph_rag  # 지연 import — 어댑터 계층은 실행기를 모른다.

    container = payload.get("target_user")
    container = container if isinstance(container, Mapping) else {}
    predicates: list[str] = []
    uncomparable: list[str] = []
    for slot in sorted(container):
        value = container.get(slot)
        if migration_fingerprint.is_empty(value) or slot not in migration.CONVERTIBLE_SLOTS:
            continue
        if slot == "purchase_membership" and isinstance(value, Mapping):
            predicates.append(graph_rag._purchase_membership_predicate(value.get("window_days")))
        elif slot == "purchase_inactivity" and isinstance(value, Mapping):
            predicates.append(graph_rag._purchase_inactivity_predicate(int(value["min_days"])))
        elif slot == "behaviors" and "no_purchase" in {str(item) for item in value}:
            predicates.append(graph_rag._no_purchase_anti_join_predicate())
        else:
            uncomparable.append(f"target_user.{slot}")
    return predicates, uncomparable


def _normalize_predicate(sql: str) -> str:
    """별칭·여백만 정규화한다(구조는 건드리지 않는다 — 건드리면 대조가 의미를 잃는다)."""
    return " ".join(_ALIAS_TOKEN.sub("A", sql).split())


def replay_predicates(
    payload: Mapping[str, Any], event_sql: str
) -> dict[str, Any]:
    legacy, uncomparable = _legacy_predicates(payload)
    normalized_event = _normalize_predicate(event_sql)
    normalized_legacy = [_normalize_predicate(item) for item in legacy]
    matched = [item for item in normalized_legacy if item in normalized_event]
    if not legacy:
        verdict = "structural_only"
    elif len(matched) == len(legacy) and not uncomparable:
        verdict = "predicate_identical"
    elif len(matched) == len(legacy):
        verdict = "predicate_identical_partial_coverage"
    else:
        verdict = "predicate_mismatch"
    return {
        "verdict": verdict,
        "legacy_predicates": legacy,
        "uncomparable_slots": uncomparable,
        "unmatched_legacy_predicates": [
            item for item in normalized_legacy if item not in normalized_event
        ],
    }


def _compile_event_sql(expression: Any, context: migration.MigrationContext) -> tuple[str, str]:
    """변환된 IR 을 실제 컴파일해 본다. 반환: (sql, 오류메시지)."""
    try:
        catalog = context.catalog
        compiled = event_compiler.compile_expression(
            expression, context=catalog.compile_context(literals=True, today=context.as_of_date)
        )
        return compiled.sql, ""
    except Exception as exc:  # noqa: BLE001 — 컴파일 실패는 보고 대상이지 배치 중단 사유가 아니다.
        return "", f"{exc.__class__.__name__}: {exc}"


def replay_asset(
    asset: Mapping[str, Any], context: migration.MigrationContext, *, compile_sql: bool
) -> dict[str, Any]:
    """자산 하나의 변환 + (선택) 컴파일 결과. **legacy 실행 경로는 건드리지 않는다.**"""
    payload = asset["payload"]
    result = migration.legacy_slot_to_event_ir(payload, context=context)
    item = migration.manifest_item(asset["asset_id"], asset["revision"], payload, result)
    row: dict[str, Any] = {
        **item.to_dict(),
        "is_executable": result.is_executable,
        # 저장은 가능하지만 실행 권위는 여전히 legacy 다 — 이 배치는 권위를 옮기지 않는다.
        "audience_authority": AudienceAuthority.LEGACY.value,
        "consumed_paths": list(result.consumed_paths),
        "ignored_paths": [entry.to_dict() for entry in result.ignored_paths],
        "receipts": [entry.to_dict() for entry in result.receipts],
        "provenance": [entry.to_dict() for entry in result.provenance],
        "errors": [entry.to_dict() for entry in result.errors],
        "envelope": migration.build_migration_envelope(
            result,
            asset_id=asset["asset_id"],
            asset_revision=asset["revision"],
            context=context,
            converted_at=context.as_of.isoformat(),
        ),
    }
    if compile_sql and result.is_executable and result.expression is not None:
        sql, error = _compile_event_sql(result.expression, context)
        row["event_ir_sql"] = sql
        row["event_ir_compile_error"] = error
        if not error:
            row["replay"] = replay_predicates(payload, sql)
        if error:
            # 컴파일이 안 되면 '변환됨'이라고 보고하지 않는다 — 자동 legacy 폴백이 없는 설계에서
            # 컴파일 불가는 곧 실행 불가이고, 그 사실이 상태에 드러나야 한다.
            row["migration_status"] = MigrationStatus.BLOCKED_CATALOG.value
            row["is_executable"] = False
            row["reason_codes"] = sorted({*row["reason_codes"], "EVENT_IR_COMPILE_FAILED"})
    return row


def _summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    by_status: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    replay_verdicts: dict[str, int] = {}
    for row in rows:
        by_status[row["migration_status"]] = by_status.get(row["migration_status"], 0) + 1
        by_classification[row["classification"]] = by_classification.get(row["classification"], 0) + 1
        for code in row.get("reason_codes") or ():
            reason_counts[code] = reason_counts.get(code, 0) + 1
        verdict = (row.get("replay") or {}).get("verdict")
        if verdict:
            replay_verdicts[verdict] = replay_verdicts.get(verdict, 0) + 1
    return {
        "assets": len(rows),
        "executable": sum(1 for row in rows if row.get("is_executable")),
        "by_migration_status": dict(sorted(by_status.items())),
        "by_classification": dict(sorted(by_classification.items())),
        "reason_codes": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        "replay_verdicts": dict(sorted(replay_verdicts.items())),
    }


def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    columns = [
        "asset_id", "asset_revision", "classification", "migration_status", "is_executable",
        "slot_types", "reason_codes", "unmapped_paths", "invalid_paths",
        "source_fingerprint", "semantic_fingerprint", "source_schema_checksum",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: (
                    ";".join(str(item) for item in row.get(key) or ())
                    if isinstance(row.get(key), list) else row.get(key)
                )
                for key in columns
            })


def _read_checkpoint(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _append_checkpoint(path: Path | None, keys: Iterable[str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        for key in keys:
            handle.write(f"{key}\n")


def _batches(items: list[Any], size: int) -> Iterator[list[Any]]:
    if size <= 0:
        yield items
        return
    for start in range(0, len(items), size):
        yield items[start : start + size]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.migrate_legacy_audience",
        description="legacy 오디언스 슬롯 → Event IR 이행 배치(기본 dry-run)",
    )
    parser.add_argument("--assets", help="자산 파일 경로(JSON 배열 / {assets:[...]} / JSONL)")
    parser.add_argument("--source", choices=("file", "db"), default="file",
                        help="자산 출처. db 는 메타데이터 DB 의 오디언스 테이블을 읽는다(읽기 전용)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="기본값이자 현재 웨이브에서 유일하게 지원하는 모드")
    parser.add_argument("--apply", action="store_true",
                        help="아직 지원하지 않는다 — 저장·권위 이관은 검증 웨이브가 소유한다")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0, help="처리할 자산 수 상한(0=제한 없음)")
    parser.add_argument("--asset-id", action="append", help="특정 자산만 재실행(반복 지정 가능)")
    parser.add_argument("--slot-type", action="append", help="특정 슬롯을 가진 자산만(반복 지정 가능)")
    parser.add_argument("--checkpoint", help="처리 완료 키를 누적할 파일(재실행 시 건너뛴다)")
    parser.add_argument("--output", help="JSON 리포트 경로")
    parser.add_argument("--csv-output", help="CSV manifest 경로")
    parser.add_argument("--as-of", help="기준 시각(ISO8601). 없으면 현재 시각")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--compile-sql", action="store_true",
                        help="변환된 IR 을 실제 컴파일해 보고서에 SQL/오류를 싣는다")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply:
        print(
            "이 도구는 저장·권위 이관을 하지 않는다. cut-over 는 shadow 검증을 통과한 revision 에만"
            " compare-and-swap 으로 수행하며 별도 명령이 소유한다.",
            file=sys.stderr,
        )
        return 2
    try:
        assets = load_assets(args)
        context = build_context(args)
    except (AssetSourceError, migration.MigrationContextError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
    done = _read_checkpoint(checkpoint_path)
    pending = [
        asset for asset in assets
        if _asset_key(asset["asset_id"], asset["revision"]) not in done
    ]

    rows: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for batch in _batches(pending, args.batch_size):
        processed: list[str] = []
        for asset in batch:
            key = _asset_key(asset["asset_id"], asset["revision"])
            try:
                rows.append(replay_asset(asset, context, compile_sql=args.compile_sql))
            except Exception as exc:  # noqa: BLE001 — 자산 하나의 사고가 배치를 멈추지 않는다.
                quarantined.append({
                    "asset_id": asset["asset_id"], "asset_revision": asset["revision"],
                    "migration_status": MigrationStatus.QUARANTINED.value,
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
            processed.append(key)
        _append_checkpoint(checkpoint_path, processed)

    report = {
        "generated_at": datetime.now(dt_timezone.utc).isoformat(),
        "mode": "dry-run",
        "context": context.to_dict(),
        "skipped_by_checkpoint": len(assets) - len(pending),
        "summary": _summary(rows),
        "quarantined": quarantined,
        "assets": rows,
    }
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.csv_output:
        _write_csv(Path(args.csv_output), rows)
    if not args.output and not args.csv_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
