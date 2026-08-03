"""이행 상태 저장소 — 판정이 본 값 **전부**를 조건으로 거는 compare-and-swap.

저장 자산(``campaign_target_audiences``)에는 revision 컬럼이 없다(2026-08-03 실측). 그래서 이행
상태는 별도 테이블이 소유하고, 그 테이블이 revision·지문 3축·보존 payload·검증 근거를 함께 든다.
갱신은 **판정 시점에 읽은 값 전부**를 WHERE 로 걸어서만 이뤄진다:

    asset_id · revision · migration_status · source_fingerprint · source_schema_checksum
    · semantic_fingerprint · row_version

갱신 행이 0이면 그 사이 누군가 이 자산을 움직였다는 뜻이므로 **중단**한다(재시도하지 않는다 —
재시도는 우리가 읽지 않은 상태 위에서 판정을 다시 쓰는 일이다).

``row_version`` 이 따로 있는 이유: 지문 여섯 개가 모두 같아도 상태가 한 번 왕복(cut-over →
rollback → cut-over)했다면 우리가 읽은 행은 이미 다른 행이다. 지문은 '내용이 같은가'를 답하고
row_version 은 '그 사이에 아무 일도 없었는가'를 답한다.

이 모듈은 **연결을 만들지 않는다.** 커서를 주입받는다 — 도구가 트랜잭션 하나를 열고 상태 행과
플랜 행을 같은 트랜잭션에서 바꾸는 것이 cut-over 의 원자성이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from audience_authority import MigrationStatus, coerce_status
from audience_cutover import Decision, StoredState

# 이행 상태 테이블(신설). 자산 테이블과 분리한 이유는 위 docstring 참조.
MIGRATION_STATE_TABLE = "campaign_audience_migration"
# 권위가 움직인 사건의 append-only 기록. 상태 행은 '지금'만 말하고, 이 표가 '무엇이 언제 왜'를 답한다.
MIGRATION_LOG_TABLE = "campaign_audience_migration_log"
# 저장 자산 테이블 — 실행 권위(plan.audience_authority)가 실제로 사는 곳.
AUDIENCE_ASSET_TABLE = "campaign_target_audiences"


class MigrationStoreError(RuntimeError):
    """저장 계층이 계약을 지키지 못했다(갱신 행 0 = stale/동시 수정 포함)."""


@dataclass(frozen=True)
class PlanRow:
    """저장된 자산 한 행 — 실행 권위가 사는 곳."""

    row_id: Any
    asset_id: str
    plan: Mapping[str, Any]


@dataclass(frozen=True)
class StateGuard:
    """CAS 의 WHERE 절 — **판정이 실제로 본 값**만 들어간다.

    판정에 쓰지 않은 값을 조건에 넣으면 무관한 변경이 cut-over 를 막고, 판정에 쓴 값을 빼면
    우리가 보지 않은 상태 위에 결과를 쓴다. 그래서 이 dataclass 는 :class:`StoredState` 에서만
    만든다(손으로 조립하지 않는다).
    """

    asset_id: str
    revision: int
    status: MigrationStatus
    source_fingerprint: str
    source_schema_checksum: str
    semantic_fingerprint: str
    row_version: int

    @classmethod
    def from_state(cls, state: StoredState) -> StateGuard:
        return cls(
            asset_id=state.asset_id,
            revision=state.revision,
            status=state.status,
            source_fingerprint=state.source_fingerprint,
            source_schema_checksum=state.source_schema_checksum,
            semantic_fingerprint=state.semantic_fingerprint,
            row_version=state.row_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "revision": self.revision,
            "migration_status": self.status.value,
            "source_fingerprint": self.source_fingerprint,
            "source_schema_checksum": self.source_schema_checksum,
            "semantic_fingerprint": self.semantic_fingerprint,
            "row_version": self.row_version,
        }


@dataclass(frozen=True)
class StateWrite:
    """저장할 값 한 벌. 검증 근거(``verification_digest``/``verified_at``)는 **함께** 움직인다 —
    따로 갱신하면 '검증되지 않은 SHADOW_VERIFIED' 같은 상태가 잠깐 존재할 수 있다."""

    asset_id: str
    revision: int
    status: MigrationStatus
    source_fingerprint: str
    source_schema_checksum: str
    semantic_fingerprint: str = ""
    binding_fingerprint: str = ""
    legacy_payload: Mapping[str, Any] = field(default_factory=dict)
    legacy_payload_checksum: str = ""
    event_expression: Mapping[str, Any] | None = None
    verification_digest: str = ""
    verified_at: str | None = None
    actor: str = ""


@dataclass(frozen=True)
class LogEntry:
    """감사 기록 한 줄. 경고도 싣는다 — 차단하지 않은 신호가 기록에도 없으면 없었던 일이 된다."""

    asset_id: str
    revision: int
    action: str
    from_status: MigrationStatus | None
    to_status: MigrationStatus | None
    authority: str
    actor: str
    evidence_digest: str = ""
    reasons: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    @classmethod
    def from_decision(cls, decision: Decision, *, actor: str, note: str = "") -> LogEntry:
        return cls(
            asset_id=decision.asset_id,
            revision=decision.revision,
            action=decision.action,
            from_status=decision.from_status,
            to_status=decision.to_status,
            authority=decision.authority.value,
            actor=actor,
            evidence_digest=decision.evidence_digest,
            reasons={
                "blockers": [item.to_dict() for item in decision.blockers],
                "warnings": [item.to_dict() for item in decision.warnings],
            },
            note=note,
        )


@dataclass(frozen=True)
class LogRecord:
    """읽어 온 감사 기록 한 줄. :class:`LogEntry` 와 나눠 둔 이유는 방향이 다르기 때문이다 —
    쓰는 쪽은 서버가 채울 값(``log_id``·``occurred_at``)을 모르고, 읽는 쪽은 그 둘이 본체다.

    ``blocked`` 는 파생이다: 차단 사유가 실려 있으면 그 줄은 **시도했지만 막힌 기록**이고,
    사고 후 재구성에서 가장 먼저 세는 값이다(성공한 이관보다 막힌 시도가 많은 것이 정상이다).
    """

    log_id: Any
    asset_id: str
    revision: int
    action: str
    from_status: str | None
    to_status: str | None
    authority: str
    actor: str
    occurred_at: str
    evidence_digest: str = ""
    reasons: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.reasons.get("blockers"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.log_id,
            "asset_id": self.asset_id,
            "revision": self.revision,
            "action": self.action,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "audience_authority": self.authority,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
            "evidence_digest": self.evidence_digest,
            "blocked": self.blocked,
            "reasons": dict(self.reasons),
            "note": self.note,
        }


class MigrationStore(Protocol):
    """도구가 주입받는 저장 인터페이스. 판정 계층은 이것도 모른다."""

    def ensure_schema(self) -> None: ...
    def read_state(self, asset_id: str, revision: int) -> StoredState | None: ...
    def read_plan(self, asset_id: str, *, for_update: bool = False) -> PlanRow | None: ...
    def insert_state(self, write: StateWrite) -> int: ...
    def swap_state(self, guard: StateGuard, write: StateWrite) -> int: ...
    def write_plan(self, row: PlanRow, plan: Mapping[str, Any]) -> int: ...
    def append_log(self, entry: LogEntry) -> None: ...
    def read_log(
        self, *, asset_id: str = "", revision: int | None = None, limit: int = 0
    ) -> list[LogRecord]: ...


# ── DDL ───────────────────────────────────────────────────────────────────────────
# docs/data/metadata_ddl.sql 에도 같은 정의가 있다. 여기 두는 이유는 도구가 처음 도는 환경에서
# 스키마 부재가 "cut-over 실패"로 나타나지 않게 하기 위해서다(둘의 동치는 테스트가 고정한다).

STATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_STATE_TABLE} (
    asset_id                VARCHAR(200) NOT NULL,
    revision                INTEGER      NOT NULL,
    migration_status        VARCHAR(40)  NOT NULL,
    source_fingerprint      VARCHAR(64)  NOT NULL,
    source_schema_checksum  VARCHAR(64)  NOT NULL,
    semantic_fingerprint    VARCHAR(64)  NOT NULL DEFAULT '',
    binding_fingerprint     VARCHAR(64)  NOT NULL DEFAULT '',
    legacy_payload          JSONB        NOT NULL,
    legacy_payload_checksum VARCHAR(64)  NOT NULL,
    event_expression        JSONB,
    verification_digest     VARCHAR(64)  NOT NULL DEFAULT '',
    verified_at             TIMESTAMPTZ,
    row_version             INTEGER      NOT NULL DEFAULT 1,
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by              VARCHAR(120) NOT NULL DEFAULT '',
    PRIMARY KEY (asset_id, revision)
)
"""

LOG_DDL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_LOG_TABLE} (
    log_id           BIGSERIAL PRIMARY KEY,
    asset_id         VARCHAR(200) NOT NULL,
    revision         INTEGER      NOT NULL,
    action           VARCHAR(20)  NOT NULL,
    from_status      VARCHAR(40),
    to_status        VARCHAR(40),
    audience_authority VARCHAR(20) NOT NULL,
    actor            VARCHAR(120) NOT NULL,
    evidence_digest  VARCHAR(64)  NOT NULL DEFAULT '',
    reasons          JSONB        NOT NULL DEFAULT '{{}}'::JSONB,
    note             TEXT         NOT NULL DEFAULT '',
    occurred_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

LOG_INDEX_DDL = f"""
CREATE INDEX IF NOT EXISTS idx_{MIGRATION_LOG_TABLE}_asset
    ON {MIGRATION_LOG_TABLE}(asset_id, revision, occurred_at)
"""

_STATE_COLUMNS = (
    "asset_id", "revision", "migration_status", "source_fingerprint", "source_schema_checksum",
    "semantic_fingerprint", "binding_fingerprint", "legacy_payload", "legacy_payload_checksum",
    "event_expression", "verification_digest", "verified_at", "row_version",
)

SELECT_STATE_SQL = (
    f"SELECT {', '.join(_STATE_COLUMNS)} FROM {MIGRATION_STATE_TABLE}"
    " WHERE asset_id = %s AND revision = %s"
)

INSERT_STATE_SQL = f"""
INSERT INTO {MIGRATION_STATE_TABLE} (
    asset_id, revision, migration_status, source_fingerprint, source_schema_checksum,
    semantic_fingerprint, binding_fingerprint, legacy_payload, legacy_payload_checksum,
    event_expression, verification_digest, verified_at, row_version, updated_by
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s, %s::JSONB, %s, %s, 1, %s)
ON CONFLICT (asset_id, revision) DO NOTHING
"""

# CAS. WHERE 에 있는 일곱 값이 판정의 입력 전부다 — 하나라도 달라졌으면 갱신 행이 0이 되고,
# 그때는 재시도가 아니라 중단이다(우리가 읽지 않은 상태 위에 결과를 쓰지 않는다).
SWAP_STATE_SQL = f"""
UPDATE {MIGRATION_STATE_TABLE}
   SET migration_status = %s,
       source_fingerprint = %s,
       source_schema_checksum = %s,
       semantic_fingerprint = %s,
       binding_fingerprint = %s,
       legacy_payload = %s::JSONB,
       legacy_payload_checksum = %s,
       event_expression = %s::JSONB,
       verification_digest = %s,
       verified_at = %s,
       row_version = row_version + 1,
       updated_at = CURRENT_TIMESTAMP,
       updated_by = %s
 WHERE asset_id = %s
   AND revision = %s
   AND migration_status = %s
   AND source_fingerprint = %s
   AND source_schema_checksum = %s
   AND semantic_fingerprint = %s
   AND row_version = %s
"""

# FOR UPDATE: 플랜 행을 읽은 뒤 같은 트랜잭션에서 쓴다. 읽기와 쓰기 사이에 다른 세션이 플랜을
# 갈아 끼우면 우리가 판정한 것과 다른 플랜에 권위를 얹게 된다.
SELECT_PLAN_SQL = (
    f"SELECT audience_id, audience_key, query_plan FROM {AUDIENCE_ASSET_TABLE}"
    " WHERE audience_key = %s"
)
UPDATE_PLAN_SQL = f"UPDATE {AUDIENCE_ASSET_TABLE} SET query_plan = %s::JSONB WHERE audience_id = %s"

INSERT_LOG_SQL = f"""
INSERT INTO {MIGRATION_LOG_TABLE} (
    asset_id, revision, action, from_status, to_status, audience_authority,
    actor, evidence_digest, reasons, note
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::JSONB, %s)
"""

_LOG_COLUMNS = (
    "log_id", "asset_id", "revision", "action", "from_status", "to_status",
    "audience_authority", "actor", "evidence_digest", "reasons", "note", "occurred_at",
)

# 정렬은 **최신 우선**으로 읽는다. ``--limit`` 이 붙었을 때 잘려 나가야 하는 것은 오래된 쪽이기
# 때문이다(사고 조사는 방금 무슨 일이 있었는지부터 본다). 읽은 뒤 시간순으로 뒤집어서 돌려준다 —
# 목록으로 읽을 때는 앞에서 뒤로 읽는 것이 사건의 순서다.
SELECT_LOG_SQL = (
    f"SELECT {', '.join(_LOG_COLUMNS)} FROM {MIGRATION_LOG_TABLE}"
    " WHERE (%s = '' OR asset_id = %s) AND (%s::INTEGER IS NULL OR revision = %s::INTEGER)"
    " ORDER BY occurred_at DESC, log_id DESC"
)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def state_from_row(row: Mapping[str, Any]) -> StoredState:
    """저장 행 → 판정 계층의 값. 드라이버가 JSONB 를 문자열로 주는 경우도 받는다."""
    payload = row.get("legacy_payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    envelope = row.get("event_expression")
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    return StoredState(
        asset_id=str(row["asset_id"]),
        revision=int(row["revision"]),
        status=coerce_status(row["migration_status"]),
        source_fingerprint=str(row.get("source_fingerprint") or ""),
        source_schema_checksum=str(row.get("source_schema_checksum") or ""),
        semantic_fingerprint=str(row.get("semantic_fingerprint") or ""),
        binding_fingerprint=str(row.get("binding_fingerprint") or ""),
        legacy_payload=payload if isinstance(payload, Mapping) else {},
        legacy_payload_checksum=str(row.get("legacy_payload_checksum") or ""),
        event_expression=envelope if isinstance(envelope, Mapping) else None,
        verification_digest=str(row.get("verification_digest") or ""),
        verified_at=str(row.get("verified_at") or ""),
        row_version=int(row.get("row_version") or 1),
    )


def log_record_from_row(row: Mapping[str, Any]) -> LogRecord:
    """감사 로그 행 → 값. 드라이버가 JSONB 를 문자열로, 시각을 datetime 으로 주는 경우도 받는다."""
    reasons = row.get("reasons")
    if isinstance(reasons, str):
        reasons = json.loads(reasons)
    occurred = row.get("occurred_at")
    return LogRecord(
        log_id=row.get("log_id"),
        asset_id=str(row["asset_id"]),
        revision=int(row["revision"]),
        action=str(row["action"]),
        from_status=str(row["from_status"]) if row.get("from_status") else None,
        to_status=str(row["to_status"]) if row.get("to_status") else None,
        authority=str(row.get("audience_authority") or ""),
        actor=str(row.get("actor") or ""),
        occurred_at=occurred.isoformat() if hasattr(occurred, "isoformat") else str(occurred or ""),
        evidence_digest=str(row.get("evidence_digest") or ""),
        reasons=reasons if isinstance(reasons, Mapping) else {},
        note=str(row.get("note") or ""),
    )


@dataclass
class PostgresMigrationStore:
    """메타데이터 DB(로컬 쓰기 DB) 구현. **커서를 주입받는다** — 연결·트랜잭션은 도구 소유다."""

    cursor: Any

    def ensure_schema(self) -> None:
        for statement in (STATE_DDL, LOG_DDL, LOG_INDEX_DDL):
            self.cursor.execute(statement)

    def read_state(self, asset_id: str, revision: int) -> StoredState | None:
        self.cursor.execute(SELECT_STATE_SQL, (asset_id, revision))
        rows = self.cursor.fetchall()
        if not rows:
            return None
        return state_from_row(_as_mapping(rows[0], _STATE_COLUMNS))

    def read_plan(self, asset_id: str, *, for_update: bool = False) -> PlanRow | None:
        sql = SELECT_PLAN_SQL + (" FOR UPDATE" if for_update else "")
        self.cursor.execute(sql, (asset_id,))
        rows = self.cursor.fetchall()
        if not rows:
            return None
        row = _as_mapping(rows[0], ("audience_id", "audience_key", "query_plan"))
        plan = row.get("query_plan")
        if isinstance(plan, str):
            plan = json.loads(plan)
        return PlanRow(
            row_id=row["audience_id"],
            asset_id=str(row["audience_key"]),
            plan=plan if isinstance(plan, Mapping) else {},
        )

    def insert_state(self, write: StateWrite) -> int:
        self.cursor.execute(INSERT_STATE_SQL, (
            write.asset_id, write.revision, write.status.value,
            write.source_fingerprint, write.source_schema_checksum,
            write.semantic_fingerprint, write.binding_fingerprint,
            _json(dict(write.legacy_payload)), write.legacy_payload_checksum,
            _json(dict(write.event_expression) if write.event_expression else None),
            write.verification_digest, write.verified_at or None, write.actor,
        ))
        return int(self.cursor.rowcount or 0)

    def swap_state(self, guard: StateGuard, write: StateWrite) -> int:
        self.cursor.execute(SWAP_STATE_SQL, (
            write.status.value, write.source_fingerprint, write.source_schema_checksum,
            write.semantic_fingerprint, write.binding_fingerprint,
            _json(dict(write.legacy_payload)), write.legacy_payload_checksum,
            _json(dict(write.event_expression) if write.event_expression else None),
            write.verification_digest, write.verified_at or None, write.actor,
            guard.asset_id, guard.revision, guard.status.value,
            guard.source_fingerprint, guard.source_schema_checksum,
            guard.semantic_fingerprint, guard.row_version,
        ))
        return int(self.cursor.rowcount or 0)

    def write_plan(self, row: PlanRow, plan: Mapping[str, Any]) -> int:
        self.cursor.execute(UPDATE_PLAN_SQL, (_json(dict(plan)), row.row_id))
        return int(self.cursor.rowcount or 0)

    def append_log(self, entry: LogEntry) -> None:
        self.cursor.execute(INSERT_LOG_SQL, (
            entry.asset_id, entry.revision, entry.action,
            entry.from_status.value if entry.from_status else None,
            entry.to_status.value if entry.to_status else None,
            entry.authority, entry.actor, entry.evidence_digest,
            _json(dict(entry.reasons)), entry.note,
        ))

    def read_log(
        self, *, asset_id: str = "", revision: int | None = None, limit: int = 0
    ) -> list[LogRecord]:
        """감사 기록을 시간순으로 읽는다(``limit`` 은 **최신** 쪽을 남긴다).

        한 행 더 읽는 이유는 잘렸다는 사실을 부를 수 있게 하기 위해서다 — 조용한 절단은
        "이게 전부"로 읽힌다(웨이브 3 에서 표본 절단에 같은 규칙을 걸었다).
        """
        sql = SELECT_LOG_SQL + (" LIMIT %s" if limit > 0 else "")
        params: list[Any] = [asset_id, asset_id, revision, revision]
        if limit > 0:
            params.append(limit + 1)
        self.cursor.execute(sql, tuple(params))
        rows = self.cursor.fetchall() or []
        records = [log_record_from_row(_as_mapping(row, _LOG_COLUMNS)) for row in rows]
        return list(reversed(records))


def _as_mapping(row: Any, columns: tuple[str, ...]) -> Mapping[str, Any]:
    """드라이버 차이를 흡수한다. tuple 행은 **선언한 컬럼 순서**로 이름을 붙인다 —
    위치로 집으면 SELECT 목록이 바뀌는 날 값이 조용히 옆 컬럼으로 들어간다."""
    if isinstance(row, Mapping):
        return row
    if isinstance(row, (list, tuple)):
        if len(row) != len(columns):
            raise MigrationStoreError(
                f"결과 행의 컬럼 수가 선언({len(columns)})과 다르다: {len(row)}"
            )
        return dict(zip(columns, row))
    raise MigrationStoreError(f"결과 행의 모양을 알 수 없다: {type(row).__name__}")


def require_swapped(rows: int, *, asset_id: str, revision: int) -> None:
    """CAS 결과 판정. 0이면 **중단**이고, 재시도하지 않는다."""
    if rows == 0:
        raise MigrationStoreError(
            f"이행 상태를 갱신하지 못했다({asset_id}@{revision}): 판정 이후 상태가 바뀌었거나"
            " 다른 명령이 동시에 이 자산을 옮겼다 — 다시 읽고 판정부터 다시 하라"
        )
    if rows > 1:
        raise MigrationStoreError(
            f"이행 상태 갱신이 {rows}행을 건드렸다({asset_id}@{revision}) — 자산 하나여야 한다"
        )


__all__ = [
    "AUDIENCE_ASSET_TABLE",
    "INSERT_LOG_SQL",
    "INSERT_STATE_SQL",
    "LOG_DDL",
    "LOG_INDEX_DDL",
    "LogEntry",
    "LogRecord",
    "MIGRATION_LOG_TABLE",
    "MIGRATION_STATE_TABLE",
    "MigrationStore",
    "MigrationStoreError",
    "PlanRow",
    "PostgresMigrationStore",
    "SELECT_LOG_SQL",
    "SELECT_PLAN_SQL",
    "SELECT_STATE_SQL",
    "STATE_DDL",
    "SWAP_STATE_SQL",
    "StateGuard",
    "StateWrite",
    "UPDATE_PLAN_SQL",
    "log_record_from_row",
    "require_swapped",
    "state_from_row",
]
