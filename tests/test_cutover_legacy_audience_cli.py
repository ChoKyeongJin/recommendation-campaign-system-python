"""이행 명령의 계약 — **권위는 여기서만 움직이고, 움직였으면 실행이 따라와야 한다.**

이 파일이 지키는 것 여섯:

    ① ``--apply`` 없이는 아무것도 쓰지 않는다(읽기 전용 트랜잭션이 물리적으로 막는다)
    ② 실제 shadow 보고서(⑤⑥ 미실행)로는 승격되지 않는다 — 미실행은 통과가 아니다
    ③ cut-over 는 상태 행과 플랜 행을 **함께** 옮긴다(하나만 남는 커밋이 없다)
    ④ 권위가 옮겨졌다는 말은 실행기가 그렇게 읽는다는 뜻이다(플랜을 실행기에 물어본다)
    ⑤ 권위를 얹는 저장 플랜은 **판정한 그 자산**이어야 한다
    ⑥ 무슨 일이 있었는지는 도구로 읽을 수 있어야 한다 — 막힌 시도까지
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_admission  # noqa: E402
import audience_cutover as cutover  # noqa: E402
import graph_rag  # noqa: E402
import plan_validation  # noqa: E402
from audience_authority import MigrationStatus  # noqa: E402
from audience_migration_store import (  # noqa: E402
    LogEntry, LogRecord, MigrationStoreError, PlanRow, StateGuard, StateWrite, require_swapped,
    state_from_row,
)
from tools import cutover_legacy_audience as cli  # noqa: E402
from tools import verify_legacy_audience_shadow as verify_cli  # noqa: E402

ASSETS = REPO_ROOT / "tests" / "fixtures" / "legacy_audience_assets.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "audience_shadow_fixture.json"
AS_OF = "2026-08-03T00:00:00"
# 여섯 단계를 실측으로 통과한 유일한 자산(웨이브 3 실DB 대조) — 첫 cut-over 대상이다.
ASSET_ID = "aud-convertible-lifetime-absence"


# ── 저장소 대역 ───────────────────────────────────────────────────────────────────
# 실제 저장 계층은 postgres 다. 여기서 재현하는 것은 **계약**뿐이다: CAS 는 판정이 본 값 전부를
# 조건으로 걸고, 트랜잭션은 예외로 빠져나갈 때 전부 되돌리며, 읽기 전용 연결에서는 쓰기가 실패한다.


class ReadOnlyViolation(RuntimeError):
    """읽기 전용 트랜잭션에서 쓰기를 시도했다(서버가 거부하는 자리)."""


class MemoryStore:
    def __init__(self, plans: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.rows: dict[tuple[str, int], dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {
            key: deepcopy(dict(value)) for key, value in (plans or {}).items()
        }
        self.log: list[LogEntry] = []
        self.writable = True
        self.schema_ready = False

    # 읽기 -------------------------------------------------------------------
    def ensure_schema(self) -> None:
        self._assert_writable()
        self.schema_ready = True

    def read_state(self, asset_id: str, revision: int) -> Any:
        row = self.rows.get((asset_id, revision))
        return state_from_row(deepcopy(row)) if row else None

    def read_plan(self, asset_id: str, *, for_update: bool = False) -> PlanRow | None:
        if for_update:
            self._assert_writable()  # SELECT … FOR UPDATE 는 읽기 전용 트랜잭션에서 실패한다
        plan = self.plans.get(asset_id)
        return PlanRow(row_id=asset_id, asset_id=asset_id, plan=deepcopy(plan)) if plan else None

    # 쓰기 -------------------------------------------------------------------
    def insert_state(self, write: StateWrite) -> int:
        self._assert_writable()
        key = (write.asset_id, write.revision)
        if key in self.rows:
            return 0
        self.rows[key] = {**self._row(write), "row_version": 1}
        return 1

    def swap_state(self, guard: StateGuard, write: StateWrite) -> int:
        self._assert_writable()
        row = self.rows.get((guard.asset_id, guard.revision))
        if row is None:
            return 0
        guarded = (
            row["migration_status"], row["source_fingerprint"], row["source_schema_checksum"],
            row["semantic_fingerprint"], row["row_version"],
        )
        expected = (
            guard.status.value, guard.source_fingerprint, guard.source_schema_checksum,
            guard.semantic_fingerprint, guard.row_version,
        )
        if guarded != expected:
            return 0
        self.rows[(guard.asset_id, guard.revision)] = {
            **self._row(write), "row_version": row["row_version"] + 1
        }
        return 1

    def write_plan(self, row: PlanRow, plan: Mapping[str, Any]) -> int:
        self._assert_writable()
        if row.asset_id not in self.plans:
            return 0
        self.plans[row.asset_id] = deepcopy(dict(plan))
        return 1

    def append_log(self, entry: LogEntry) -> None:
        self._assert_writable()
        self.log.append(entry)

    def read_log(
        self, *, asset_id: str = "", revision: int | None = None, limit: int = 0
    ) -> list[LogRecord]:
        """append 순서가 곧 시간순이다.

        실제 저장소는 최신 우선으로 ``limit + 1`` 행을 읽고 시간순으로 뒤집어 돌려준다. 대역이
        재현하는 것은 그 **계약**이다 — limit 은 오래된 쪽을 버리고, 한 행을 더 실어 보내
        호출자가 "잘렸다"를 말할 수 있게 한다.
        """
        records = [
            LogRecord(
                log_id=index + 1,
                asset_id=entry.asset_id,
                revision=entry.revision,
                action=entry.action,
                from_status=entry.from_status.value if entry.from_status else None,
                to_status=entry.to_status.value if entry.to_status else None,
                authority=entry.authority,
                actor=entry.actor,
                occurred_at=f"2026-08-03T00:00:{index:02d}+00:00",
                evidence_digest=entry.evidence_digest,
                reasons=dict(entry.reasons),
                note=entry.note,
            )
            for index, entry in enumerate(self.log)
            if (not asset_id or entry.asset_id == asset_id)
            and (revision is None or entry.revision == revision)
        ]
        return records[-(limit + 1):] if limit > 0 else records

    # 내부 -------------------------------------------------------------------
    def _assert_writable(self) -> None:
        if not self.writable:
            raise ReadOnlyViolation("읽기 전용 트랜잭션에서 쓰기를 시도했다")

    @staticmethod
    def _row(write: StateWrite) -> dict[str, Any]:
        return {
            "asset_id": write.asset_id,
            "revision": write.revision,
            "migration_status": write.status.value,
            "source_fingerprint": write.source_fingerprint,
            "source_schema_checksum": write.source_schema_checksum,
            "semantic_fingerprint": write.semantic_fingerprint,
            "binding_fingerprint": write.binding_fingerprint,
            "legacy_payload": deepcopy(dict(write.legacy_payload)),
            "legacy_payload_checksum": write.legacy_payload_checksum,
            "event_expression": deepcopy(dict(write.event_expression)) if write.event_expression else None,
            "verification_digest": write.verification_digest,
            "verified_at": write.verified_at or "",
        }

    def snapshot(self) -> tuple[Any, ...]:
        return (deepcopy(self.rows), deepcopy(self.plans), list(self.log))

    def restore(self, snapshot: tuple[Any, ...]) -> None:
        self.rows, self.plans, self.log = deepcopy(snapshot[0]), deepcopy(snapshot[1]), list(snapshot[2])


def store_factory(store: MemoryStore) -> Any:
    """트랜잭션 하나를 흉내낸다 — 예외로 빠져나가면 **전부** 되돌린다(부분 적용 없음)."""

    @contextmanager
    def factory(*, writable: bool) -> Iterator[MemoryStore]:
        store.writable = writable
        snapshot = store.snapshot()
        try:
            yield store
        except Exception:
            store.restore(snapshot)
            raise

    return factory


# ── 공통 실행 ─────────────────────────────────────────────────────────────────────


def _payload(asset_id: str) -> dict[str, Any]:
    corpus = json.loads(ASSETS.read_text(encoding="utf-8"))
    asset = next(item for item in corpus["assets"] if item["asset_id"] == asset_id)
    return deepcopy(asset["payload"])


def _seeded_store() -> MemoryStore:
    """저장된 자산이 하나 있는 저장소(권위가 사는 자리 = query_plan)."""
    return MemoryStore(plans={ASSET_ID: _payload(ASSET_ID)})


def _run(store: MemoryStore, tmp_path: Path, *args: str) -> tuple[int, dict[str, Any]]:
    output = tmp_path / f"cutover-{len(list(tmp_path.glob('*.json')))}.json"
    code = cli.main(
        [*args, "--assets", str(ASSETS), "--as-of", AS_OF, "--output", str(output)],
        store_factory=store_factory(store),
    )
    payload = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return code, payload


def _verified_report(store: MemoryStore, tmp_path: Path) -> Path:
    """여섯 단계를 통과한 보고서를 **도구가 보고한 지문으로** 만든다.

    검증 자체를 흉내내는 것이 아니다(그것은 shadow 도구의 일이고, 실DB 없이는 ⑤⑥ 이 돌지 않는다).
    여기서 고정하는 것은 "통과한 보고서가 있을 때 cut-over 경로가 실제로 열리는가"이며, 그래서
    지문은 지어내지 않고 변환 결과에서 읽는다.
    """
    code, report = _run(store, tmp_path, "status", "--asset-id", ASSET_ID)
    assert code == 0
    conversion = report["results"][0]["conversion"]
    payload = {
        "generated_at": "2026-08-03T09:00:00+09:00",
        "assets": [{
            "asset_id": ASSET_ID,
            "asset_revision": 1,
            "cutover_allowed": True,
            "blocking_reasons": [],
            "source_fingerprint": conversion["source_fingerprint"],
            "semantic_fingerprint": conversion["semantic_fingerprint"],
            "stages": [
                {"stage": stage, "status": "pass", "divergences": []}
                for stage in (
                    "path_accounting", "semantic_fingerprint", "sql_structure",
                    "adversarial_fixture", "snapshot_members", "performance",
                )
            ],
        }],
    }
    path = tmp_path / "verified-report.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _chain_to_shadow_verified(store: MemoryStore, tmp_path: Path) -> Path:
    report = _verified_report(store, tmp_path)
    code, _ = _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")
    assert code == 0
    code, _ = _run(
        store, tmp_path, "promote", "--asset-id", ASSET_ID,
        "--shadow-report", str(report), "--apply", "--actor", "kim",
    )
    assert code == 0
    return report


# ── ① dry-run 은 쓰지 않는다 ──────────────────────────────────────────────────────


def test_dry_run_cannot_write_because_the_transaction_is_read_only(tmp_path: Path) -> None:
    store = _seeded_store()

    code, report = _run(store, tmp_path, "record", "--asset-id", ASSET_ID)

    assert code == 0
    assert report["mode"] == "dry-run"
    assert store.rows == {} and store.log == [], "dry-run 이 저장소를 건드렸다"
    assert store.schema_ready is False


def test_apply_without_an_actor_is_refused(tmp_path: Path) -> None:
    store = _seeded_store()

    code = cli.main(
        ["record", "--assets", str(ASSETS), "--as-of", AS_OF, "--apply"],
        store_factory=store_factory(store),
    )

    assert code == 2
    assert store.rows == {}


def test_authority_moves_one_asset_at_a_time(tmp_path: Path) -> None:
    store = _seeded_store()

    code = cli.main(
        ["cutover", "--assets", str(ASSETS), "--as-of", AS_OF],
        store_factory=store_factory(store),
    )

    assert code == 2, "자산을 지정하지 않은 cut-over 는 시작되지 않는다"


# ── 저장(record) ──────────────────────────────────────────────────────────────────


def test_record_stores_the_conversion_and_leaves_execution_on_legacy(tmp_path: Path) -> None:
    store = _seeded_store()

    code, report = _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    assert code == 0
    state = store.read_state(ASSET_ID, 1)
    assert state.status is MigrationStatus.CONVERTED
    assert state.legacy_payload == _payload(ASSET_ID), "보존 payload 가 원본 그대로여야 한다"
    assert state.event_expression is not None
    assert not state.verification_digest, "저장은 검증이 아니다"
    # 권위는 플랜에 사는데, record 는 플랜을 건드리지 않는다.
    assert "audience_authority" not in store.plans[ASSET_ID]
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is False
    assert [entry.action for entry in store.log] == ["record"]
    assert report["results"][0]["applied"] is True


def test_blocked_assets_are_recorded_with_their_reason_not_skipped(tmp_path: Path) -> None:
    store = MemoryStore()

    code, report = _run(store, tmp_path, "record", "--apply", "--actor", "kim")

    assert code == 0
    statuses = {row["asset_id"]: row["to_status"] for row in report["results"]}
    # 막힘을 하나의 'failed' 로 뭉치지 않는 이유가 여기서 보인다 — 해소 주체가 상태로 드러난다.
    # (로그인 계열은 카탈로그 가드 부재와 NULL·경계 업무 결정이 겹치고, 더 근본적인 쪽이 상태다.)
    assert statuses["aud-blocked-login-family"] == MigrationStatus.BLOCKED_DOMAIN_DECISION.value
    assert statuses["aud-blocked-ir-extension"] == MigrationStatus.BLOCKED_IR_EXTENSION.value
    assert statuses[ASSET_ID] == MigrationStatus.CONVERTED.value


# ── ② 승격은 근거를 요구한다 ──────────────────────────────────────────────────────


def test_a_real_shadow_report_does_not_promote_because_stages_are_unrun(tmp_path: Path) -> None:
    """실DB 없이 만든 보고서에는 ⑤⑥ 이 미실행이다 — 그 보고서로는 승격되지 않는다."""
    store = _seeded_store()
    shadow_path = tmp_path / "shadow.json"
    assert verify_cli.main([
        "--assets", str(ASSETS), "--fixture", str(FIXTURE), "--output", str(shadow_path),
        "--as-of", AS_OF,
    ]) == 0
    _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(
        store, tmp_path, "promote", "--asset-id", ASSET_ID,
        "--shadow-report", str(shadow_path), "--apply", "--actor", "kim",
    )

    assert code == 3
    codes = {item["code"] for item in report["results"][0]["blockers"]}
    assert cutover.SHADOW_REPORT_BLOCKED in codes
    assert cutover.SHADOW_REPORT_STAGE_NOT_RUN in codes
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.CONVERTED
    # 차단도 기록으로 남는다 — 시도했다는 사실이 사라지면 감사가 아니다.
    assert [entry.action for entry in store.log][-1] == "promote"


def test_promote_without_a_report_never_starts(tmp_path: Path) -> None:
    store = _seeded_store()

    code = cli.main(
        ["promote", "--assets", str(ASSETS), "--asset-id", ASSET_ID, "--as-of", AS_OF],
        store_factory=store_factory(store),
    )

    assert code == 2


def test_promotion_records_the_evidence_pointer(tmp_path: Path) -> None:
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)

    state = store.read_state(ASSET_ID, 1)
    assert state.status is MigrationStatus.SHADOW_VERIFIED
    assert state.verification_digest, "무엇이 이 승격을 허가했는지 저장돼 있어야 한다"
    assert state.verified_at.startswith("2026-08-03")


# ── ③④ cut-over ─────────────────────────────────────────────────────────────────


def test_cutover_moves_both_the_state_row_and_the_execution_authority(tmp_path: Path) -> None:
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)

    code, report = _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    assert code == 0, report["results"][0]["blockers"]
    state = store.read_state(ASSET_ID, 1)
    assert state.status is MigrationStatus.EVENT_IR_PRIMARY
    plan = store.plans[ASSET_ID]
    # 권위가 옮겨졌다는 말은 **실행기가 그렇게 읽는다**는 뜻이다.
    assert graph_rag._has_canonical_audience_authority(plan) is True
    # 되돌릴 재료(legacy 슬롯)는 그대로 남아 있다.
    assert plan["target_user"] == _payload(ASSET_ID)["target_user"]
    assert [entry.action for entry in store.log][-1] == "cutover"

    # 2026-08-04(Phase 3-4): 이 단언은 **반전됐다.** 예전에는 "보존된 슬롯이 옆에 있어도 플랜
    # 검증이 이 모양을 실행 가능으로 읽는다"였고, 그 근거는 "그렇게 판정하면 cut-over 한 자산이
    # 통째로 실행 불가가 된다"였다. 지금은 정확히 그 상태가 맞다.
    #
    # 뒤집은 이유는 dual-storage 를 금지해서가 아니라, 게이트가 '보존된 같은 조건'과 '두 번째
    # 오디언스 언어'를 **구분할 수단이 없기 때문**이다. 구분 장치가 Phase 3-2 원안의
    # preserved_legacy_audience(실행 위치 → 보존 위치 이사)였는데, §6-3 에서 저장 자산 0행 +
    # 소유자 판단("자산이 legacy 에만 연결돼 있으면 쓰지 않는다")으로 만들지 않기로 했다.
    # 그 결정의 대가가 여기다 — cut-over 산출 플랜은 실행 경로에 들어오지 않는다(3-2′).
    #
    # 결정이 뒤집히면 이 단언이 먼저 red 가 되고, 그때 선택지는 3-2 원안 복원 또는 cut-over 가
    # 표면을 비우게 하는 것이다. **약화가 아니라 반전이므로 계약은 여전히 재고 있다.**
    validation = plan_validation.validate_executable_plan(dict(plan))
    assert validation.status == plan_validation.INTERNAL_INVALID
    assert [issue.code for issue in validation.issues] == [
        audience_admission.LEGACY_AUDIENCE_CONFLICT_CODE
    ]
    assert [issue.path for issue in validation.issues] == ["target_user.behaviors"]
    assert graph_rag.build_event_expression_sql_candidate(dict(plan)) is None


def test_cutover_without_promotion_is_blocked_by_the_state_machine(tmp_path: Path) -> None:
    store = _seeded_store()
    _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    assert code == 3
    codes = {item["code"] for item in report["results"][0]["blockers"]}
    assert cutover.ILLEGAL_TRANSITION in codes
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is False


def test_cutover_is_all_or_nothing_when_the_plan_write_fails(tmp_path: Path) -> None:
    """상태만 옮겨진 채 커밋되면 그 자산은 어느 쪽으로도 설명되지 않는다."""
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    before = store.read_state(ASSET_ID, 1)
    store.write_plan = lambda row, plan: 0  # type: ignore[assignment]

    code = cli.main(
        ["cutover", "--assets", str(ASSETS), "--asset-id", ASSET_ID, "--as-of", AS_OF,
         "--apply", "--actor", "kim"],
        store_factory=store_factory(store),
    )

    assert code == 1
    assert store.read_state(ASSET_ID, 1).row_version == before.row_version
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.SHADOW_VERIFIED
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is False


def test_a_concurrent_state_change_aborts_the_swap(tmp_path: Path) -> None:
    """판정 이후에 상태가 움직였으면 갱신 행이 0이고, 그때는 재시도가 아니라 중단이다."""
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    original = store.swap_state

    def racing_swap(guard: StateGuard, write: StateWrite) -> int:
        store.rows[(guard.asset_id, guard.revision)]["row_version"] += 1  # 다른 명령이 끼어들었다
        return original(guard, write)

    store.swap_state = racing_swap  # type: ignore[assignment]
    code = cli.main(
        ["cutover", "--assets", str(ASSETS), "--asset-id", ASSET_ID, "--as-of", AS_OF,
         "--apply", "--actor", "kim"],
        store_factory=store_factory(store),
    )

    assert code == 1
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.SHADOW_VERIFIED
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is False


def test_record_refuses_to_overwrite_an_asset_that_is_already_cut_over(tmp_path: Path) -> None:
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    assert code == 3
    codes = {item["code"] for item in report["results"][0]["blockers"]}
    assert cutover.AUTHORITY_ALREADY_EVENT_IR in codes


# ── rollback ─────────────────────────────────────────────────────────────────────


def test_rollback_returns_execution_to_legacy_and_keeps_the_stored_expression(tmp_path: Path) -> None:
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(
        store, tmp_path, "rollback", "--asset-id", ASSET_ID, "--apply", "--actor", "kim",
        "--reason", "대상 수가 예상보다 20% 적다",
    )

    assert code == 0, report["results"][0]["blockers"]
    plan = store.plans[ASSET_ID]
    assert graph_rag._has_canonical_audience_authority(plan) is False
    assert plan["event_expression"]["expression"], "rollback 은 표현을 지우는 일이 아니다"
    assert plan["target_user"] == _payload(ASSET_ID)["target_user"]
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.LEGACY_ONLY
    assert store.log[-1].action == "rollback"
    assert store.log[-1].note == "대상 수가 예상보다 20% 적다"


def test_rollback_without_a_reason_never_starts(tmp_path: Path) -> None:
    store = _seeded_store()

    code = cli.main(
        ["rollback", "--assets", str(ASSETS), "--asset-id", ASSET_ID, "--as-of", AS_OF,
         "--apply", "--actor", "kim"],
        store_factory=store_factory(store),
    )

    assert code == 2


def test_rollback_of_an_asset_that_never_moved_is_blocked(tmp_path: Path) -> None:
    store = _seeded_store()
    _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(
        store, tmp_path, "rollback", "--asset-id", ASSET_ID, "--apply", "--actor", "kim",
        "--reason", "확인",
    )

    assert code == 3
    assert cutover.ILLEGAL_TRANSITION in {
        item["code"] for item in report["results"][0]["blockers"]
    }


def test_rollback_can_run_from_the_stored_state_without_the_asset_file(tmp_path: Path) -> None:
    """운영 사고 중에는 자산 파일이 손에 없을 수 있다 — 되돌리기는 저장 상태만으로 성립해야 한다."""
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code = cli.main(
        ["rollback", "--asset-id", ASSET_ID, "--revision", "1", "--as-of", AS_OF,
         "--apply", "--actor", "kim", "--reason", "즉시 복구"],
        store_factory=store_factory(store),
    )

    assert code == 0
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.LEGACY_ONLY


# ── ⑤ 판정한 자산 = 권위를 얹을 자산 ─────────────────────────────────────────────


def _drifted_store() -> MemoryStore:
    """저장 플랜이 코퍼스 자산과 **다른 오디언스**인 저장소.

    실자산 이관에서 실제로 일어날 수 있는 모양이다: 판정은 ``--assets`` 파일로 하고 스탬프는
    ``audience_key`` 로 찾은 저장 행에 하므로, 그 둘이 같은 자산이라는 보장이 어디에도 없었다.
    """
    plan = _payload(ASSET_ID)
    plan["target_user"]["behaviors"] = ["cart_abandoner"]
    return MemoryStore(plans={ASSET_ID: plan})


def test_cutover_refuses_a_stored_plan_that_is_not_the_asset_we_judged(tmp_path: Path) -> None:
    store = _drifted_store()
    _chain_to_shadow_verified(store, tmp_path)

    code, report = _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    assert code == 3
    assert cutover.PLAN_PAYLOAD_MISMATCH in {
        item["code"] for item in report["results"][0]["blockers"]
    }
    # 검증한 IR 이 다른 자산의 실행 권위가 되지 않았다.
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is False
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.SHADOW_VERIFIED


def test_status_shows_the_same_plan_mismatch_that_cutover_blocks_on(tmp_path: Path) -> None:
    """status 가 cut-over 와 다른 재료로 판정하면, 통과라고 말해 놓고 막히는 자산이 생긴다."""
    store = _drifted_store()
    _chain_to_shadow_verified(store, tmp_path)

    code, report = _run(store, tmp_path, "status", "--asset-id", ASSET_ID)

    assert code == 0
    row = next(item for item in report["results"] if item["asset_id"] == ASSET_ID)
    assert cutover.PLAN_PAYLOAD_MISMATCH in {
        item["code"] for item in row["cutover"]["blockers"]
    }


# ── ④ 사후 조건: 쓴 것과 실행기가 읽는 것이 같은가 ────────────────────────────────


def test_cutover_does_not_commit_when_the_stamp_is_not_read_back_as_event_ir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """저장 형식과 권위 판정자가 어긋나는 날을 흉내낸다 — 그날 "옮겼다"는 거짓이 된다.

    지금까지 이 사후 조건을 지키던 것은 코드 한 줄뿐이었다(웨이브 4 노트). 그 줄을 지워도
    아무 테스트도 빨개지지 않으면, 그것은 방어선이 아니라 주석이다.
    """
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    before = store.read_state(ASSET_ID, 1)
    # 스탬프가 사라진 산출물 — 필드를 썼다고 믿지만 판정자는 legacy 라고 답한다.
    monkeypatch.setattr(cli.cutover, "plan_after_cutover", lambda plan, envelope: dict(plan))

    code = cli.main(
        ["cutover", "--assets", str(ASSETS), "--asset-id", ASSET_ID, "--as-of", AS_OF,
         "--apply", "--actor", "kim"],
        store_factory=store_factory(store),
    )

    assert code == 1
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.SHADOW_VERIFIED
    assert store.read_state(ASSET_ID, 1).row_version == before.row_version
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is False


def test_the_stamp_post_condition_is_checked_in_dry_run_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """어긋남은 쓰기 직전이 아니라 판정 직후에 드러나야 한다 — dry-run 이 '통과'라고 말하면
    운영자는 ``--apply`` 를 붙여 다시 돌린다."""
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    monkeypatch.setattr(cli.cutover, "plan_after_cutover", lambda plan, envelope: dict(plan))

    code = cli.main(
        ["cutover", "--assets", str(ASSETS), "--asset-id", ASSET_ID, "--as-of", AS_OF],
        store_factory=store_factory(store),
    )

    assert code == 1


def test_rollback_does_not_commit_when_the_authority_did_not_come_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")
    monkeypatch.setattr(cli.cutover, "plan_after_rollback", lambda plan: dict(plan))

    code = cli.main(
        ["rollback", "--asset-id", ASSET_ID, "--revision", "1", "--as-of", AS_OF,
         "--apply", "--actor", "kim", "--reason", "확인"],
        store_factory=store_factory(store),
    )

    assert code == 1
    assert store.read_state(ASSET_ID, 1).status is MigrationStatus.EVENT_IR_PRIMARY
    assert graph_rag._has_canonical_audience_authority(store.plans[ASSET_ID]) is True


# ── ⑥ 감사 로그 조회 ─────────────────────────────────────────────────────────────


def test_history_reads_back_the_sequence_the_commands_wrote(tmp_path: Path) -> None:
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(store, tmp_path, "history", "--asset-id", ASSET_ID)

    assert code == 0
    assert report["mode"] == "read-only"
    assert [row["action"] for row in report["results"]] == ["record", "promote", "cutover"]
    assert report["results"][-1]["audience_authority"] == "event_ir"
    assert report["summary"]["authority_moves"] == 1
    assert report["summary"]["blocked_attempts"] == 0


def test_history_keeps_blocked_attempts_because_that_is_why_the_table_exists(
    tmp_path: Path
) -> None:
    """사고 후 재구성에서 필요한 것은 대개 '무엇이 막혔나'다 — 성공만 남기면 그 답이 사라진다."""
    store = _seeded_store()
    _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(store, tmp_path, "history", "--asset-id", ASSET_ID)

    assert code == 0, "막힌 시도가 쌓여 있다는 사실은 조회 명령의 정상 출력이다"
    blocked = [row for row in report["results"] if row["blocked"]]
    assert [row["action"] for row in blocked] == ["cutover"]
    assert cutover.ILLEGAL_TRANSITION in {
        item["code"] for item in blocked[0]["reasons"]["blockers"]
    }
    assert report["summary"]["blocked_attempts"] == 1
    assert report["summary"]["authority_moves"] == 0


def test_history_limit_keeps_the_newest_and_never_truncates_silently(tmp_path: Path) -> None:
    """조용한 절단은 '이게 전부'로 읽힌다 — 웨이브 3 이 표본에 건 규칙과 같다."""
    store = _seeded_store()
    _chain_to_shadow_verified(store, tmp_path)
    _run(store, tmp_path, "cutover", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code, report = _run(store, tmp_path, "history", "--asset-id", ASSET_ID, "--limit", "2")

    assert code == 0
    assert [row["action"] for row in report["results"]] == ["promote", "cutover"]
    assert report["results"][0]["truncated_before"] is True
    assert report["summary"]["truncated"] is True


def test_history_never_opens_a_writable_connection_even_with_apply(tmp_path: Path) -> None:
    """조회가 쓰기 연결을 열 수 있으면 "확인만 해보려던 실행"이 다시 상태를 바꿀 수 있게 된다."""
    store = _seeded_store()
    _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")
    before = len(store.log)

    code, report = _run(
        store, tmp_path, "history", "--asset-id", ASSET_ID, "--apply", "--actor", "kim"
    )

    assert code == 0
    assert report["mode"] == "read-only"
    assert store.writable is False
    assert len(store.log) == before, "조회가 기록을 남기면 그 표가 조회로 오염된다"


def test_history_runs_without_the_asset_file(tmp_path: Path) -> None:
    """rollback 과 같은 이유다 — 사고 중에 코퍼스 파일이 손에 없을 수 있다."""
    store = _seeded_store()
    _run(store, tmp_path, "record", "--asset-id", ASSET_ID, "--apply", "--actor", "kim")

    code = cli.main(["history", "--asset-id", ASSET_ID], store_factory=store_factory(store))

    assert code == 0


def test_history_refuses_to_mix_several_assets_into_one_timeline(tmp_path: Path) -> None:
    store = _seeded_store()

    code = cli.main(
        ["history", "--asset-id", ASSET_ID, "--asset-id", "aud-other"],
        store_factory=store_factory(store),
    )

    assert code == 2


# ── 저장 계층 계약 ────────────────────────────────────────────────────────────────


def test_zero_updated_rows_is_an_abort_not_a_retry() -> None:
    with pytest.raises(MigrationStoreError) as error:
        require_swapped(0, asset_id="aud-x", revision=1)

    assert "다시 읽고" in str(error.value)


def test_more_than_one_updated_row_is_also_an_abort() -> None:
    with pytest.raises(MigrationStoreError):
        require_swapped(2, asset_id="aud-x", revision=1)


METADATA_DDL = REPO_ROOT / "docs" / "data" / "metadata_ddl.sql"


def _ddl_columns(text: str, table: str) -> dict[str, str]:
    """CREATE TABLE 본문에서 (컬럼 → 타입+제약) 을 읽는다(주석·키 선언 제외)."""
    match = re.search(
        rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)\n\)", text, re.S | re.IGNORECASE
    )
    assert match, f"{table} 정의를 찾지 못했다"
    columns: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = re.sub(r"--.*$", "", line).strip().rstrip(",").strip()
        if not line or re.match(r"^(PRIMARY KEY|CONSTRAINT|UNIQUE|CHECK|FOREIGN KEY)\b", line, re.I):
            continue
        name, _, rest = line.partition(" ")
        columns[name.lower()] = " ".join(rest.split()).upper()
    return columns


@pytest.mark.parametrize("table", ["campaign_audience_migration", "campaign_audience_migration_log"])
def test_the_two_ddl_declarations_do_not_drift(table: str) -> None:
    """도구가 만드는 표와 문서의 표가 갈라지면, 운영 DB 와 개발 DB 가 조용히 다른 스키마가 된다.

    도구 쪽 DDL 을 지우고 문서만 두지 않은 이유: 이행 명령이 처음 도는 환경에서 스키마 부재가
    "cut-over 실패"로 나타나면, 그 실패의 원인을 자산에서 찾게 된다.
    """
    from audience_migration_store import LOG_DDL, STATE_DDL

    module_ddl = STATE_DDL if table.endswith("migration") else LOG_DDL
    assert _ddl_columns(module_ddl, table) == _ddl_columns(
        METADATA_DDL.read_text(encoding="utf-8"), table
    )
