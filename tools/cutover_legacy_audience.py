"""이행 명령 — 실행 권위를 옮기는 **유일한** 도구(기본은 여전히 쓰지 않는다).

    python -m tools.cutover_legacy_audience status   --assets assets.json
    python -m tools.cutover_legacy_audience history  --asset-id aud-x --limit 50
    python -m tools.cutover_legacy_audience record   --assets assets.json --apply --actor me
    python -m tools.cutover_legacy_audience promote  --assets assets.json --asset-id aud-x \
                                                     --shadow-report shadow.json --apply --actor me
    python -m tools.cutover_legacy_audience cutover  --assets assets.json --asset-id aud-x --apply --actor me
    python -m tools.cutover_legacy_audience rollback --asset-id aud-x --revision 1 --apply --actor me \
                                                     --reason "..."

``rollback`` 과 ``history`` 는 자산 소스 없이 돈다. 운영 사고 중에 코퍼스 파일이 손에 없을 수 있고,
되돌리기의 재료와 무슨 일이 있었는지의 기록은 저장된 상태 안에 전부 있어야 하기 때문이다.

명령 하나 = 상태 전이 하나다. 붙여서 자동으로 진행하지 않는다 — ``record`` 하면서 검증까지
통과시키는 명령이 있으면 그것이 기본 경로가 되고, 검증은 다시 형식이 된다.

세 가지가 이 도구의 계약이다:

1. **``--apply`` 없이는 쓰지 않는다.** 기본 실행은 읽기 전용 연결로 열려서 물리적으로 쓸 수 없다.
2. **권위는 자산 하나씩 옮긴다.** ``record`` 만 배치이고 promote/cutover/rollback 은 ``--asset-id``
   하나를 요구한다. 여러 자산의 권위가 한 명령으로 움직이면 사고의 반경이 명령 한 줄이 된다.
3. **한 트랜잭션 안에서 상태 행과 플랜 행이 함께 바뀐다.** 둘 중 하나만 바뀌면 "상태는 Event IR
   인데 실행은 legacy"(또는 그 반대)가 되고, 그 자산은 어느 쪽으로도 설명되지 않는다.

CAS 는 :mod:`audience_migration_store` 가, 판정은 :mod:`audience_cutover` 가 소유한다. 여기 있는
것은 배선뿐이다 — 연결을 만들고, 재료를 모아 판정에 넣고, 허용된 것만 트랜잭션으로 적용한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import audience_cutover as cutover  # noqa: E402
import legacy_audience_migration as migration  # noqa: E402
import migration_fingerprint  # noqa: E402
from audience_authority import AudienceAuthority, MigrationStatus  # noqa: E402
from audience_migration_store import (  # noqa: E402
    MIGRATION_LOG_TABLE, MIGRATION_STATE_TABLE, LogEntry, MigrationStoreError,
    PostgresMigrationStore, StateGuard, StateWrite, require_swapped,
)
from tools.migrate_legacy_audience import (  # noqa: E402
    DEFAULT_TIMEZONE, AssetSourceError, build_context, load_assets,
)

# 보고 명령 — 아무것도 쓰지 않고, 막혀 있다는 사실이 그 명령의 **정상 출력**이다(종료 코드 0).
COMMAND_STATUS = "status"
COMMAND_HISTORY = "history"
REPORT_COMMANDS: frozenset[str] = frozenset({COMMAND_STATUS, COMMAND_HISTORY})

# 배치가 허용되는 명령. 나머지는 자산 하나를 명시해야 한다(위 계약 2).
BATCH_COMMANDS: frozenset[str] = frozenset({cutover.ACTION_RECORD, *REPORT_COMMANDS})


class CommandError(RuntimeError):
    """명령을 시작할 수 없다(재료 부재·인자 모순). 판정 결과의 '차단'과 구분한다."""


# ── legacy 컴파일러가 지금 실행할 수 있는 슬롯·행동 ────────────────────────────────
# rollback 판정의 재료다. 목록을 여기 적지 않고 **선언 레지스트리에서 파생**한다 — 적으면
# 소유자가 하나 더 생기고, 슬롯이 사라지는 날 이 도구만 "지원한다"고 답한다.


def legacy_supported_slots() -> frozenset[str]:
    import member_filters_config
    import targeting_ir

    slots = {name for name, shape in targeting_ir.SLOT_SHAPES.items() if shape.container == "target_user"}
    slots |= {
        str(entry.get("category")) for entry in member_filters_config.eq_filters()
        if entry.get("category")
    }
    # 행동은 값 어휘가 따로 있으므로 슬롯 이름으로만 통과시키고 값은 아래에서 본다.
    slots.add("behaviors")
    return frozenset(slots)


def legacy_supported_behaviors() -> frozenset[str]:
    import member_filters_config
    import targeting_ir

    # 이 둘 밖의 행동은 targeting_ir 이 'unclassified_behavior' 로 뽑아 빌더가 실행하지 않는다.
    return frozenset(member_filters_config.order_count_behaviors()) | {targeting_ir.CART_BEHAVIOR}


# ── 연결 ──────────────────────────────────────────────────────────────────────────


@contextmanager
def postgres_store(*, writable: bool) -> Iterator[Any]:
    """메타데이터 DB 연결 하나 = 트랜잭션 하나. ``writable=False`` 면 **서버가** 쓰기를 거부한다.

    dry-run 을 애플리케이션 규칙으로만 지키지 않는 이유: 규칙은 코드 한 줄로 우회되지만 read-only
    트랜잭션은 우회되지 않는다.
    """
    import db_connections

    with db_connections.postgres_connection(read_only=not writable) as connection:
        with connection.cursor() as cursor:
            yield PostgresMigrationStore(cursor)


# ── 재료 모으기 ───────────────────────────────────────────────────────────────────


def _convert(asset: Mapping[str, Any], context: Any) -> tuple[Any, cutover.ConversionFacts]:
    result = migration.legacy_slot_to_event_ir(asset["payload"], context=context)
    return result, cutover.ConversionFacts.from_result(result)


def _envelope(asset: Mapping[str, Any], result: Any, context: Any) -> dict[str, Any]:
    return migration.build_migration_envelope(
        result,
        asset_id=asset["asset_id"],
        asset_revision=asset["revision"],
        context=context,
        converted_at=context.as_of.isoformat(),
    )


def _payload_checksum(payload: Mapping[str, Any]) -> str:
    """보존본의 **저장 무결성** 체크섬 — 정규형이 아니라 payload 를 그대로 해싱한다.

    의미 지문(source_fingerprint)과 다른 질문에 답한다: 저 값이 저장 중에 바뀌었는가.
    정규형으로 재면 비의미 키가 사라진 채 계산되어, 그 키가 지워져도 '무결'로 나온다.
    """
    return migration_fingerprint.digest(payload)


def _find_asset(assets: list[dict[str, Any]], asset_id: str) -> dict[str, Any]:
    for asset in assets:
        if asset["asset_id"] == asset_id:
            return asset
    raise CommandError(f"자산 소스에 그 자산이 없다: {asset_id}")


def _live_source_fingerprint(plan: Mapping[str, Any], context: Any) -> str:
    """저장된 플랜(지금 실행 중인 것)의 오디언스 원본 지문.

    두 판정이 이 값을 쓰는데 **결과가 반대다**. cut-over 에서는 판정 대상과 스탬프 대상이 같은
    자산인지 보는 차단 재료이고(읽지 못하면 차단), rollback 에서는 표류 경고 재료다(읽지 못하면
    경고를 만들지 않을 뿐 되돌리기를 막지 않는다). 그래서 여기서는 빈 문자열을 돌려주고,
    그것을 어떻게 읽을지는 각 판정이 정한다.

    지문 대상은 오디언스 컨테이너뿐이므로(``_audience_scope``) 저장 플랜에 붙어 있는 다른 키
    (intent·campaign_constraints·cut-over 가 얹은 event_expression)는 이 값을 움직이지 않는다.
    """
    try:
        result = migration.legacy_slot_to_event_ir(plan, context=context)
    except Exception:  # noqa: BLE001 — 읽지 못했다는 사실은 각 판정이 자기 규칙으로 해석한다.
        return ""
    return str(result.source_fingerprint or "")


# ── 명령 ──────────────────────────────────────────────────────────────────────────


def run_status(assets: list[dict[str, Any]], context: Any, store: Any) -> list[dict[str, Any]]:
    """지금 각 자산이 어디에 있고, cut-over 가 왜 막혀 있는가(쓰지 않는다)."""
    rows: list[dict[str, Any]] = []
    for asset in assets:
        result, facts = _convert(asset, context)
        state = store.read_state(asset["asset_id"], asset["revision"])
        plan_row = store.read_plan(asset["asset_id"])
        decision = cutover.evaluate_cutover(
            state, facts, asset_id=asset["asset_id"], revision=asset["revision"],
            plan_present=plan_row is not None,
            # status 가 cut-over 와 **같은 재료**로 판정해야 "왜 막혀 있나"의 답이 같아진다.
            # 여기서만 빼면 status 는 통과라고 말하는데 cut-over 는 막히는 자산이 생긴다.
            plan_source_fingerprint=(
                _live_source_fingerprint(plan_row.plan, context) if plan_row else None
            ),
        )
        rows.append({
            "asset_id": asset["asset_id"],
            "asset_revision": asset["revision"],
            "stored_state": state.to_dict() if state else None,
            "plan_stored": plan_row is not None,
            "executed_authority": (
                cutover.executed_authority(plan_row.plan).value if plan_row else None
            ),
            "conversion": facts.to_dict(),
            "cutover": decision.to_dict(),
        })
    return rows


def run_history(
    store: Any, *, asset_id: str = "", revision: int | None = None, limit: int = 0
) -> list[dict[str, Any]]:
    """감사 기록을 시간순으로 읽는다(쓰지 않는다).

    이 명령이 없던 동안 ``campaign_audience_migration_log`` 는 raw SQL 로만 읽혔다. 그 표가
    존재하는 이유가 사고 후 재구성인데, 읽는 경로가 도구에 없으면 그 이유가 성립하지 않는다.

    **차단된 시도도 기록이다** — 오히려 그쪽이 더 자주 쌓인다(막힌 이관이 성공한 이관보다 많은
    것이 이 이행에서는 정상이다). 그래서 목록은 성공만 거르지 않고 전부 싣고, ``blocked`` 로
    구분만 해 둔다.
    """
    records = store.read_log(asset_id=asset_id, revision=revision, limit=limit)
    truncated = bool(limit > 0 and len(records) > limit)
    # 한 행 더 읽어 왔으므로 여기서 **오래된 쪽**을 버린다(시간순 정렬이라 앞쪽이 오래된 것이다).
    if truncated:
        records = records[len(records) - limit:]
    rows = [record.to_dict() for record in records]
    if truncated and rows:
        # 잘렸다는 사실을 목록 안에 싣는다. 요약에만 적으면 목록만 복사해 가는 순간 사라진다.
        rows[0] = {**rows[0], "truncated_before": True}
    return rows


def run_record(
    assets: list[dict[str, Any]], context: Any, store: Any, *, apply: bool, actor: str
) -> list[dict[str, Any]]:
    """변환 결과를 저장한다 — **권위는 움직이지 않는다.**"""
    rows: list[dict[str, Any]] = []
    for asset in assets:
        result, facts = _convert(asset, context)
        state = store.read_state(asset["asset_id"], asset["revision"])
        decision = cutover.evaluate_record(
            state, facts, asset_id=asset["asset_id"], revision=asset["revision"]
        )
        write = StateWrite(
            asset_id=asset["asset_id"],
            revision=asset["revision"],
            status=facts.status,
            source_fingerprint=facts.source_fingerprint,
            source_schema_checksum=facts.source_schema_checksum,
            semantic_fingerprint=facts.semantic_fingerprint,
            binding_fingerprint=facts.binding_fingerprint,
            legacy_payload=asset["payload"],
            legacy_payload_checksum=_payload_checksum(asset["payload"]),
            event_expression=_envelope(asset, result, context) if facts.expression else None,
            # 원본이 바뀐 자산을 다시 변환하면 기존 검증 근거는 **다른 원본**의 것이다.
            # 그것을 남겨 두면 다음 cut-over 가 그 근거로 통과한다 → 여기서 지운다.
            verification_digest="",
            verified_at=None,
            actor=actor,
        )
        applied = _apply_state(store, decision, state, write, apply=apply, actor=actor)
        rows.append({**decision.to_dict(), "applied": applied})
    return rows


def run_promote(
    asset: Mapping[str, Any], context: Any, store: Any, *,
    report: Mapping[str, Any], apply: bool, actor: str,
) -> dict[str, Any]:
    """shadow 보고서를 근거로 ``CONVERTED`` → ``SHADOW_VERIFIED``."""
    result, facts = _convert(asset, context)
    state = store.read_state(asset["asset_id"], asset["revision"])
    verdict = cutover.shadow_verdict_from_report(
        report, asset_id=asset["asset_id"], revision=asset["revision"]
    )
    decision = cutover.evaluate_promotion(state, verdict, facts)
    applied = False
    if decision.allowed and state is not None:
        write = StateWrite(
            asset_id=state.asset_id, revision=state.revision,
            status=MigrationStatus.SHADOW_VERIFIED,
            source_fingerprint=state.source_fingerprint,
            source_schema_checksum=state.source_schema_checksum,
            semantic_fingerprint=state.semantic_fingerprint,
            binding_fingerprint=state.binding_fingerprint,
            legacy_payload=state.legacy_payload,
            legacy_payload_checksum=state.legacy_payload_checksum,
            event_expression=state.event_expression,
            verification_digest=verdict.digest,
            verified_at=verdict.generated_at,
            actor=actor,
        )
        applied = _apply_state(store, decision, state, write, apply=apply, actor=actor)
    elif apply:
        _log(store, decision, actor=actor, note="차단되어 적용하지 않았다")
    return {**decision.to_dict(), "applied": applied}


def run_cutover(
    asset: Mapping[str, Any], context: Any, store: Any, *,
    report: Mapping[str, Any] | None, apply: bool, actor: str,
) -> dict[str, Any]:
    """``SHADOW_VERIFIED`` → ``EVENT_IR_PRIMARY``. 상태 행과 플랜 행이 **같은 트랜잭션**에서 바뀐다."""
    _, facts = _convert(asset, context)
    state = store.read_state(asset["asset_id"], asset["revision"])
    plan_row = store.read_plan(asset["asset_id"], for_update=apply)
    verdict = (
        cutover.shadow_verdict_from_report(
            report, asset_id=asset["asset_id"], revision=asset["revision"]
        )
        if report is not None else None
    )
    decision = cutover.evaluate_cutover(
        state, facts, asset_id=asset["asset_id"], revision=asset["revision"],
        plan_present=plan_row is not None,
        # 판정 대상(읽어 온 payload)과 스탬프 대상(저장 플랜 행)이 같은 자산인가(§9-12).
        plan_source_fingerprint=(
            _live_source_fingerprint(plan_row.plan, context) if plan_row else None
        ),
        verdict=verdict,
    )
    applied = False
    if decision.allowed and state is not None and plan_row is not None:
        updated_plan = cutover.plan_after_cutover(plan_row.plan, state.event_expression or {})
        if cutover.executed_authority(updated_plan) is not AudienceAuthority.EVENT_IR:
            # 사후 조건: 필드를 썼는데 권위 판정자가 여전히 legacy 라고 답하면, 우리가 쓴 것과
            # 실행기가 읽는 것이 다르다는 뜻이다 — 그 상태로 커밋하면 "옮겼다"가 거짓이 된다.
            raise CommandError(
                "권위 스탬프 후에도 실행 권위가 legacy 다 — 저장 형식과 권위 판정자가 어긋났다"
            )
        if apply:
            write = StateWrite(
                asset_id=state.asset_id, revision=state.revision,
                status=MigrationStatus.EVENT_IR_PRIMARY,
                source_fingerprint=state.source_fingerprint,
                source_schema_checksum=state.source_schema_checksum,
                semantic_fingerprint=state.semantic_fingerprint,
                binding_fingerprint=state.binding_fingerprint,
                legacy_payload=state.legacy_payload,
                legacy_payload_checksum=state.legacy_payload_checksum,
                event_expression=state.event_expression,
                verification_digest=state.verification_digest,
                verified_at=state.verified_at or None,
                actor=actor,
            )
            require_swapped(
                store.swap_state(StateGuard.from_state(state), write),
                asset_id=state.asset_id, revision=state.revision,
            )
            if store.write_plan(plan_row, updated_plan) != 1:
                raise MigrationStoreError(
                    f"플랜 행을 갱신하지 못했다({asset['asset_id']}) — 상태만 옮겨진 채 커밋되지 않게 중단한다"
                )
            _log(store, decision, actor=actor)
            applied = True
    elif apply:
        _log(store, decision, actor=actor, note="차단되어 적용하지 않았다")
    return {**decision.to_dict(), "applied": applied}


def run_rollback(
    asset_id: str, revision: int, context: Any, store: Any, *,
    apply: bool, actor: str, reason: str,
) -> dict[str, Any]:
    """실행 권위를 legacy 로 되돌린다 — 보존 payload 로, 역변환 없이."""
    state = store.read_state(asset_id, revision)
    plan_row = store.read_plan(asset_id, for_update=apply)
    unsupported = (
        cutover.unsupported_legacy_paths(
            state.legacy_payload,
            containers=migration.AUDIENCE_CONTAINERS,
            supported_slots=legacy_supported_slots(),
            supported_behaviors=legacy_supported_behaviors(),
        )
        if state is not None and state.legacy_payload else ()
    )
    decision = cutover.evaluate_rollback(
        state, asset_id=asset_id, revision=revision,
        plan_present=plan_row is not None,
        unsupported_paths=unsupported,
        live_source_fingerprint=(
            _live_source_fingerprint(plan_row.plan, context) if plan_row else ""
        ),
    )
    applied = False
    if decision.allowed and state is not None and plan_row is not None and apply:
        updated_plan = cutover.plan_after_rollback(plan_row.plan)
        if cutover.executed_authority(updated_plan) is not AudienceAuthority.LEGACY:
            raise CommandError("권위를 되돌렸는데 판정자가 여전히 Event IR 이라고 답한다")
        write = StateWrite(
            asset_id=state.asset_id, revision=state.revision,
            status=MigrationStatus.LEGACY_ONLY,
            source_fingerprint=state.source_fingerprint,
            source_schema_checksum=state.source_schema_checksum,
            semantic_fingerprint=state.semantic_fingerprint,
            binding_fingerprint=state.binding_fingerprint,
            legacy_payload=state.legacy_payload,
            legacy_payload_checksum=state.legacy_payload_checksum,
            event_expression=state.event_expression,  # 표현은 남는다(저장 ≠ 실행)
            verification_digest=state.verification_digest,
            verified_at=state.verified_at or None,
            actor=actor,
        )
        require_swapped(
            store.swap_state(StateGuard.from_state(state), write),
            asset_id=state.asset_id, revision=state.revision,
        )
        if store.write_plan(plan_row, updated_plan) != 1:
            raise MigrationStoreError(
                f"플랜 행을 갱신하지 못했다({asset_id}) — 상태만 되돌린 채 커밋되지 않게 중단한다"
            )
        _log(store, decision, actor=actor, note=reason)
        applied = True
    elif apply:
        _log(store, decision, actor=actor, note=f"차단되어 적용하지 않았다: {reason}")
    return {**decision.to_dict(), "applied": applied}


def _apply_state(
    store: Any, decision: cutover.Decision, state: Any, write: StateWrite, *,
    apply: bool, actor: str,
) -> bool:
    if not (apply and decision.allowed):
        if apply:
            _log(store, decision, actor=actor, note="차단되어 적용하지 않았다")
        return False
    if state is None:
        if store.insert_state(write) != 1:
            raise MigrationStoreError(
                f"이행 상태 행을 만들지 못했다({write.asset_id}@{write.revision}) — 그 사이 다른 명령이 만들었다"
            )
    else:
        require_swapped(
            store.swap_state(StateGuard.from_state(state), write),
            asset_id=write.asset_id, revision=write.revision,
        )
    _log(store, decision, actor=actor)
    return True


def _log(store: Any, decision: cutover.Decision, *, actor: str, note: str = "") -> None:
    store.append_log(LogEntry.from_decision(decision, actor=actor, note=note))


# ── CLI ───────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.cutover_legacy_audience",
        description="legacy ↔ Event IR 실행 권위 이관(cut-over)과 명시 rollback",
    )
    parser.add_argument("command", choices=(*sorted(REPORT_COMMANDS), *cutover.ACTIONS))
    parser.add_argument("--assets", help="자산 파일 경로(rollback·history 는 저장된 상태만 있으면 된다)")
    parser.add_argument("--source", choices=("file", "db"), default="file")
    parser.add_argument("--asset-id", action="append",
                        help="대상 자산. record/status/history 외의 명령은 정확히 하나여야 한다")
    parser.add_argument("--revision", type=int,
                        help="rollback 대상 revision(자산 소스 없이 되돌릴 때) · history 필터")
    parser.add_argument("--slot-type", action="append")
    parser.add_argument("--limit", type=int, default=0,
                        help="자산 수 상한(history 에서는 **최신** 기록 개수)")
    parser.add_argument("--shadow-report", help="promote 의 근거가 되는 shadow 보고서(JSON)")
    parser.add_argument("--apply", action="store_true",
                        help="실제로 저장한다. 없으면 읽기 전용 연결로 열려 물리적으로 쓸 수 없다")
    parser.add_argument("--actor", default="", help="집행자(감사 기록에 남는다). 쓰기에는 필수")
    parser.add_argument("--reason", default="", help="rollback 사유(감사 기록). rollback 에는 필수")
    parser.add_argument("--output", help="JSON 리포트 경로")
    parser.add_argument("--as-of", help="변환 기준 시각(ISO8601)")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    return parser


def _validate(args: argparse.Namespace) -> None:
    if args.apply and not args.actor.strip():
        raise CommandError("--apply 에는 --actor 가 필요하다 — 권위를 옮긴 사람이 기록에 없으면 감사가 아니다")
    if args.command not in BATCH_COMMANDS:
        if not args.asset_id or len(args.asset_id) != 1:
            raise CommandError(f"{args.command} 는 --asset-id 하나를 요구한다(권위는 자산 하나씩 옮긴다)")
    if args.command == COMMAND_HISTORY and args.asset_id and len(args.asset_id) > 1:
        # 필터가 여러 자산을 받으면 시간순 목록이 자산 사이를 오가고, 그때 그 목록은 사건의
        # 순서가 아니라 그냥 섞인 줄이 된다. 자산별로 따로 읽어라.
        raise CommandError("history 의 --asset-id 는 하나까지다(여러 자산을 섞으면 시간순이 뜻을 잃는다)")
    if args.command == cutover.ACTION_PROMOTE and not args.shadow_report:
        raise CommandError("promote 에는 --shadow-report 가 필요하다 — 근거 없는 승격은 없다")
    if args.command == cutover.ACTION_ROLLBACK and not args.reason.strip():
        raise CommandError("rollback 에는 --reason 이 필요하다 — 무엇을 보고 되돌렸는지가 기록의 본체다")


def _report_payload(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CommandError(f"shadow 보고서의 최상위가 객체가 아니다: {path}")
    return payload


def main(argv: list[str] | None = None, *, store_factory: Any = None) -> int:
    args = build_parser().parse_args(argv)
    factory = store_factory or postgres_store
    try:
        _validate(args)
        report = _report_payload(args.shadow_report)
        needs_assets = args.command not in (cutover.ACTION_ROLLBACK, COMMAND_HISTORY)
        assets = load_assets(args) if (needs_assets or args.assets) else []
        if needs_assets and args.command not in BATCH_COMMANDS:
            # 재료가 없다는 것은 판정의 '차단'이 아니라 명령을 시작할 수 없다는 뜻이다(종료 코드가 다르다).
            _find_asset(assets, args.asset_id[0])
        context = build_context(args)
    except (
        CommandError, AssetSourceError, OSError, ValueError, migration.MigrationContextError
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 보고 명령은 ``--apply`` 가 붙어도 읽기 전용으로 연다. 조회가 쓰기 연결을 열 수 있으면
    # "확인만 해보려던 실행"이 다시 운영 상태를 바꿀 수 있는 자리가 된다(dry-run 규칙과 같은 이유).
    writable = bool(args.apply) and args.command not in REPORT_COMMANDS
    try:
        with factory(writable=writable) as store:
            if writable:
                store.ensure_schema()
            result = _dispatch(args, assets, context, store, report=report)
    except (CommandError, MigrationStoreError, cutover.CutoverError) as exc:
        # 트랜잭션은 컨텍스트를 예외로 빠져나가며 롤백된다 — 부분 적용은 남지 않는다.
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — 연결·드라이버 실패도 '적용되지 않았다'로 끝나야 한다.
        hint = (
            f"\n(이행 상태 테이블이 아직 없다면 --apply 로 실행할 때"
            f" {MIGRATION_STATE_TABLE}/{MIGRATION_LOG_TABLE} 가 생성된다)"
            if "does not exist" in str(exc) else ""
        )
        print(f"{exc.__class__.__name__}: {exc}{hint}", file=sys.stderr)
        return 1

    payload = {
        "command": args.command,
        "mode": "apply" if writable else ("read-only" if args.command in REPORT_COMMANDS else "dry-run"),
        "actor": args.actor,
        "context": context.to_dict(),
        "results": result,
        "summary": _summary(result, args.command),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    else:
        print(text)
    # 차단된 것이 있으면 0 이 아니다 — 배치 스크립트가 '조용히 아무것도 안 함'을 성공으로 읽지 않게.
    # 보고 명령(status·history)은 예외다: 막혀 있다는 사실·막힌 시도가 쌓여 있다는 사실이 그
    # 명령의 정상 출력이므로, 그것을 실패 코드로 내면 조회를 파이프라인에 넣을 수 없게 된다.
    if args.command in REPORT_COMMANDS:
        return 0
    return 0 if payload["summary"]["blocked"] == 0 else 3


def _dispatch(
    args: argparse.Namespace, assets: list[dict[str, Any]], context: Any, store: Any, *,
    report: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    command = args.command
    if command == COMMAND_STATUS:
        return run_status(assets, context, store)
    if command == COMMAND_HISTORY:
        return run_history(
            store,
            asset_id=(args.asset_id[0] if args.asset_id else ""),
            revision=args.revision,
            limit=max(0, args.limit),
        )
    if command == cutover.ACTION_RECORD:
        return run_record(assets, context, store, apply=args.apply, actor=args.actor)

    asset_id = args.asset_id[0]
    if command == cutover.ACTION_ROLLBACK:
        revision = args.revision
        if revision is None:
            revision = _find_asset(assets, asset_id)["revision"] if assets else None
        if revision is None:
            raise CommandError("rollback 대상 revision 을 알 수 없다(--revision 또는 --assets 가 필요하다)")
        return [run_rollback(
            asset_id, int(revision), context, store,
            apply=args.apply, actor=args.actor, reason=args.reason,
        )]

    asset = _find_asset(assets, asset_id)
    if command == cutover.ACTION_PROMOTE:
        if report is None:
            raise CommandError("promote 에는 shadow 보고서가 필요하다")
        return [run_promote(
            asset, context, store, report=report, apply=args.apply, actor=args.actor
        )]
    return [run_cutover(
        asset, context, store, report=report, apply=args.apply, actor=args.actor
    )]


def _history_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """조회 요약 — **막힌 시도를 따로 센다.**

    성공한 이관만 세면 "권위가 두 번 움직였다"는 문장은 나오지만 "그 사이에 열두 번 막혔다"는
    문장은 안 나온다. 사고 후 재구성에서 필요한 것은 대개 뒤쪽이다.
    """
    actions: dict[str, int] = {}
    for row in rows:
        name = str(row.get("action"))
        actions[name] = actions.get(name, 0) + 1
    return {
        "entries": len(rows),
        "assets": len({str(row.get("asset_id")) for row in rows}),
        "blocked_attempts": sum(1 for row in rows if row.get("blocked")),
        "authority_moves": sum(
            1 for row in rows
            if not row.get("blocked")
            and str(row.get("action")) in (cutover.ACTION_CUTOVER, cutover.ACTION_ROLLBACK)
        ),
        "actions": dict(sorted(actions.items())),
        "truncated": any(row.get("truncated_before") for row in rows),
        "range": (
            [rows[0].get("occurred_at"), rows[-1].get("occurred_at")] if rows else []
        ),
    }


def _summary(rows: list[dict[str, Any]], command: str = "") -> dict[str, Any]:
    if command == COMMAND_HISTORY:
        return _history_summary(rows)
    blocker_codes: dict[str, int] = {}
    blocked = 0
    for row in rows:
        decision = row.get("cutover") if "cutover" in row else row
        if decision.get("allowed"):
            continue
        blocked += 1
        for item in decision.get("blockers") or ():
            code = str(item.get("code"))
            blocker_codes[code] = blocker_codes.get(code, 0) + 1
    return {
        "assets": len(rows),
        "allowed": len(rows) - blocked,
        "blocked": blocked,
        "applied": sum(1 for row in rows if row.get("applied")),
        "blocker_codes": dict(sorted(blocker_codes.items(), key=lambda item: (-item[1], item[0]))),
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
