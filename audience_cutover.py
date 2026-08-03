"""cut-over / rollback 판정 — 실행 권위를 **옮겨도 되는가**를 사유와 함께 답한다(순수 모듈).

웨이브 1~3 은 변환하고 검증했을 뿐 권위를 옮기지 않았다. 옮기는 일이 여기이고, 이 모듈이
소유하는 것은 **판정 하나**다:

    지금 이 자산의 실행 권위를 legacy → event_ir 로 옮겨도 되는가 (그리고 되돌려도 되는가)

저장·연결·시계는 전부 밖에 있다(:mod:`audience_migration_store` 와 도구). 판정이 I/O 를 알기
시작하면 "확인만 해보려던 실행"이 어느 DB 에 무엇을 쓸지 판정 안에서 정해진다 — shadow 계층에
실행자를 주입한 것과 같은 이유다.

판정 규칙 다섯(전부 fail-close):

1. **검증은 이 자산의 이 상태에 대한 것이어야 한다.** 보고서가 말한 지문과 지금 다시 변환해서
   얻은 지문이 다르면, 그 보고서는 다른 것을 검증한 것이다 → 차단.
2. **저장된 변환도 지금 원본과 같아야 한다.** 저장 지문 ≠ 재계산 지문이면 stale 이다 → 차단.
3. **cut-over 의 선행 상태는 ``SHADOW_VERIFIED`` 하나다**(상태 기계가 강제한다). 여기서 그것을
   다시 판정하지 않고 :func:`audience_authority.transition` 에 위임한다.
4. **rollback 은 역변환이 아니다.** 보존된 legacy payload 가 있고, 그 payload 의 슬롯을 legacy
   컴파일러가 지금도 지원할 때에만 권위를 되돌린다. 지원하지 않는 슬롯이 있으면 되돌린 결과가
   '조건 하나가 조용히 사라진 오디언스'이므로 실패보다 나쁘다 → 차단.
5. **판정한 자산과 권위를 얹을 자산은 같아야 한다.** 판정은 읽어 온 payload 로 하고 스탬프는
   저장 플랜 행에 한다 — 둘이 다르면 검증한 IR 이 **다른 자산**의 실행 권위가 된다 → 차단.

경고(warning)와 차단(blocker)을 나눈 자리가 하나 있다: rollback 시점에 원본이 움직여 있는 경우다.
cut-over 에서 그것은 차단이지만(검증하지 않은 것을 실행하게 된다), rollback 에서는 **기록**이다 —
잘못된 cut-over 에서 빠져나오는 유일한 길을 무관한 편집 하나가 막으면 안 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Sequence

import migration_fingerprint
from audience_authority import (
    EVENT_EXPRESSION_KEY,
    PLAN_AUTHORITY_KEY,
    AudienceAuthority,
    AudienceAuthorityError,
    ROLLBACK_TARGET,
    MigrationStatus,
    authority_for_status,
    can_transition,
    resolve_authority,
    stamp_authority,
)


class CutoverError(RuntimeError):
    """판정 재료를 읽지 못했다(보고서 모양·필수 값 부재). '막혔다'와 구분한다 —
    막힘은 판정 결과이고, 이것은 판정을 시작하지 못한 것이다."""


# 행동 이름. 감사 로그의 닫힌 어휘이자 도구 명령 이름이다.
ACTION_RECORD = "record"
ACTION_PROMOTE = "promote"
ACTION_CUTOVER = "cutover"
ACTION_ROLLBACK = "rollback"
ACTIONS: tuple[str, ...] = (ACTION_RECORD, ACTION_PROMOTE, ACTION_CUTOVER, ACTION_ROLLBACK)

# 차단 사유 코드. 이름을 닫아 두는 이유는 보고서를 기계로 세기 위해서다 — 자유 문장만 남기면
# "무엇이 몇 건 막혔나"를 사람이 눈으로 세게 되고, 그때 목록은 읽히지 않는다.
STATE_ROW_MISSING = "STATE_ROW_MISSING"
ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
CONVERSION_NOT_EXECUTABLE = "CONVERSION_NOT_EXECUTABLE"
SOURCE_FINGERPRINT_MOVED = "SOURCE_FINGERPRINT_MOVED"
SOURCE_SCHEMA_CHECKSUM_MOVED = "SOURCE_SCHEMA_CHECKSUM_MOVED"
SEMANTIC_FINGERPRINT_MOVED = "SEMANTIC_FINGERPRINT_MOVED"
BINDING_FINGERPRINT_MOVED = "BINDING_FINGERPRINT_MOVED"
SHADOW_REPORT_BLOCKED = "SHADOW_REPORT_BLOCKED"
SHADOW_REPORT_STAGE_NOT_RUN = "SHADOW_REPORT_STAGE_NOT_RUN"
SHADOW_REPORT_FINGERPRINT_MISMATCH = "SHADOW_REPORT_FINGERPRINT_MISMATCH"
SHADOW_VERIFICATION_MISSING = "SHADOW_VERIFICATION_MISSING"
SHADOW_EVIDENCE_CHANGED = "SHADOW_EVIDENCE_CHANGED"
PRESERVED_PAYLOAD_MISSING = "PRESERVED_PAYLOAD_MISSING"
PRESERVED_PAYLOAD_CORRUPT = "PRESERVED_PAYLOAD_CORRUPT"
PLAN_ROW_MISSING = "PLAN_ROW_MISSING"
PLAN_PAYLOAD_MISMATCH = "PLAN_PAYLOAD_MISMATCH"
PLAN_PAYLOAD_UNVERIFIED = "PLAN_PAYLOAD_UNVERIFIED"
EXPRESSION_MISSING = "EXPRESSION_MISSING"
AUTHORITY_ALREADY_EVENT_IR = "AUTHORITY_ALREADY_EVENT_IR"
LEGACY_SLOT_UNSUPPORTED = "LEGACY_SLOT_UNSUPPORTED"
REVISION_MISMATCH = "REVISION_MISMATCH"

# 차단이 아니라 **기록**으로 남는 것. 목록이 비면 "아무 특이사항 없었다"로 읽히므로 이름을 남긴다.
LEGACY_PAYLOAD_DRIFTED = "LEGACY_PAYLOAD_DRIFTED"
VERIFICATION_EVIDENCE_DISCARDED = "VERIFICATION_EVIDENCE_DISCARDED"


@dataclass(frozen=True)
class Reason:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ConversionFacts:
    """**지금** 원본을 다시 변환해서 얻은 사실. 저장값·보고서와 대조하는 기준선이다.

    저장된 값을 기준으로 삼지 않는 이유: 저장값끼리 비교하면 둘 다 낡았을 때 일치한다.
    """

    source_fingerprint: str
    source_schema_checksum: str
    semantic_fingerprint: str
    binding_fingerprint: str
    is_executable: bool
    status: MigrationStatus
    expression: Mapping[str, Any] | None = None

    @classmethod
    def from_result(cls, result: Any) -> ConversionFacts:
        """어댑터 산출물(:class:`legacy_audience_migration.MigrationResult`)에서 읽는다.

        타입을 import 하지 않고 속성으로 읽는 이유는 shadow 계층과 같다 — 판정 계층이 어댑터
        타입을 알면 어댑터가 바뀔 때마다 판정 규칙이 따라 바뀐다.
        """
        expression = result.expression
        return cls(
            source_fingerprint=str(result.source_fingerprint or ""),
            source_schema_checksum=str(result.source_schema_checksum or ""),
            semantic_fingerprint=str(result.fingerprint or ""),
            binding_fingerprint=str(result.binding_fingerprint or ""),
            is_executable=bool(result.is_executable),
            status=result.status,
            expression=expression.to_dict() if expression is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_fingerprint": self.source_fingerprint,
            "source_schema_checksum": self.source_schema_checksum,
            "semantic_fingerprint": self.semantic_fingerprint,
            "binding_fingerprint": self.binding_fingerprint,
            "is_executable": self.is_executable,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class StoredState:
    """이행 상태 저장 행 하나(:mod:`audience_migration_store` 가 만든다).

    ``row_version`` 이 compare-and-swap 의 마지막 축이다. 지문 여섯 개가 모두 같아도 그 사이에
    다른 명령이 상태를 한 번 왕복시켰다면 우리가 읽은 행은 이미 다른 행이다.
    """

    asset_id: str
    revision: int
    status: MigrationStatus
    source_fingerprint: str
    source_schema_checksum: str
    semantic_fingerprint: str
    binding_fingerprint: str
    legacy_payload: Mapping[str, Any]
    legacy_payload_checksum: str
    # 검증된 산출물 그 자체(이행 envelope). cut-over 는 이것을 **그대로** 플랜에 얹는다 —
    # 다시 만들면 '검증한 것'과 '실행하는 것'이 미세하게 갈릴 수 있다(최소한 converted_at 이 다르다).
    event_expression: Mapping[str, Any] | None = None
    verification_digest: str = ""
    verified_at: str = ""
    row_version: int = 1

    @property
    def authority(self) -> AudienceAuthority:
        """상태에서 **파생**한다 — 따로 저장하면 둘이 갈라진다."""
        return authority_for_status(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "revision": self.revision,
            "migration_status": self.status.value,
            "audience_authority": self.authority.value,
            "source_fingerprint": self.source_fingerprint,
            "source_schema_checksum": self.source_schema_checksum,
            "semantic_fingerprint": self.semantic_fingerprint,
            "binding_fingerprint": self.binding_fingerprint,
            "legacy_payload_checksum": self.legacy_payload_checksum,
            "has_event_expression": bool(self.event_expression),
            "verification_digest": self.verification_digest,
            "verified_at": self.verified_at,
            "row_version": self.row_version,
        }


@dataclass(frozen=True)
class ShadowVerdict:
    """shadow 보고서에서 **판정에 쓰는 것만** 뽑은 값.

    보고서 전체를 들고 다니지 않는 이유: 그러면 판정이 보고서 스키마에 묶이고, 보고서에 필드가
    하나 늘 때마다 cut-over 판정이 따라 흔들린다. 여기 있는 값이 곧 "cut-over 가 근거로 삼는 것"의
    전부이고, 그 목록이 짧아야 무엇을 근거로 권위를 옮겼는지 한 문장으로 말할 수 있다.
    """

    asset_id: str
    revision: int
    cutover_allowed: bool
    blocking_reasons: tuple[str, ...]
    not_run_stages: tuple[str, ...]
    source_fingerprint: str
    semantic_fingerprint: str
    generated_at: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_revision": self.revision,
            "cutover_allowed": self.cutover_allowed,
            "blocking_reasons": list(self.blocking_reasons),
            "not_run_stages": list(self.not_run_stages),
            "source_fingerprint": self.source_fingerprint,
            "semantic_fingerprint": self.semantic_fingerprint,
            "generated_at": self.generated_at,
            "digest": self.digest,
        }


def shadow_verdict_from_report(
    report: Mapping[str, Any], *, asset_id: str, revision: int
) -> ShadowVerdict:
    """shadow 보고서(JSON)에서 이 자산의 판정을 읽는다. 읽지 못하면 **예외**다.

    없는 항목을 '통과 아님'으로 접지 않는 이유: 그렇게 하면 보고서를 잘못 준 것과 검증이 막힌 것이
    같은 결과로 보이고, 운영자는 "왜 막혔지"를 자산에서 찾게 된다.
    """
    entries = report.get("assets")
    if not isinstance(entries, Sequence):
        raise CutoverError("shadow 보고서에 assets 배열이 없다")
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("asset_id")) != asset_id:
            continue
        if int(entry.get("asset_revision") or 0) != revision:
            continue
        stages = entry.get("stages")
        if not isinstance(stages, Sequence) or not stages:
            raise CutoverError(f"보고서 항목에 단계 결과가 없다: {asset_id}@{revision}")
        generated_at = str(report.get("generated_at") or "").strip()
        if not generated_at:
            raise CutoverError("보고서에 생성 시각이 없다 — 언제의 검증인지 말할 수 없다")
        return ShadowVerdict(
            asset_id=asset_id,
            revision=revision,
            cutover_allowed=bool(entry.get("cutover_allowed")),
            blocking_reasons=tuple(str(item) for item in entry.get("blocking_reasons") or ()),
            not_run_stages=tuple(
                str(stage.get("stage"))
                for stage in stages
                if isinstance(stage, Mapping) and str(stage.get("status")) == "not_run"
            ),
            source_fingerprint=str(entry.get("source_fingerprint") or ""),
            semantic_fingerprint=str(entry.get("semantic_fingerprint") or ""),
            generated_at=generated_at,
            # 근거 포인터. 이 값이 저장돼 있어야 "어느 검증이 이 cut-over 를 허가했는가"에
            # 보고서 파일 이름이 아니라 **내용**으로 답할 수 있다.
            digest=migration_fingerprint.digest(entry),
        )
    raise CutoverError(f"shadow 보고서에 이 자산의 항목이 없다: {asset_id}@{revision}")


@dataclass(frozen=True)
class Decision:
    """한 명령의 판정 결과. **허용/차단이 아니라 사유가 본체다.**"""

    action: str
    asset_id: str
    revision: int
    from_status: MigrationStatus | None
    to_status: MigrationStatus | None
    blockers: tuple[Reason, ...] = ()
    warnings: tuple[Reason, ...] = ()
    facts: ConversionFacts | None = None
    evidence_digest: str = ""
    verified_at: str = ""
    notes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return not self.blockers and self.to_status is not None

    @property
    def authority(self) -> AudienceAuthority:
        """이 판정이 성립하면 자산이 갖게 되는 실행 권위."""
        return authority_for_status(self.to_status) if self.to_status else AudienceAuthority.LEGACY

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "asset_id": self.asset_id,
            "asset_revision": self.revision,
            "allowed": self.allowed,
            "from_status": self.from_status.value if self.from_status else None,
            "to_status": self.to_status.value if self.to_status else None,
            "audience_authority": self.authority.value,
            "blockers": [item.to_dict() for item in self.blockers],
            "warnings": [item.to_dict() for item in self.warnings],
            "evidence_digest": self.evidence_digest,
            "verified_at": self.verified_at,
            "facts": self.facts.to_dict() if self.facts else None,
            "notes": dict(self.notes),
        }


def _transition_blocker(current: MigrationStatus, target: MigrationStatus) -> Reason | None:
    if can_transition(current, target):
        return None
    return Reason(
        ILLEGAL_TRANSITION,
        f"허용되지 않은 이행 전이: {current.value} → {target.value}",
    )


def _fingerprint_blockers(state: StoredState, facts: ConversionFacts) -> list[Reason]:
    """저장된 변환이 **지금 원본**과 같은가. 다르면 저장된 검증도 그 원본의 것이 아니다."""
    blockers: list[Reason] = []
    if state.source_fingerprint != facts.source_fingerprint:
        blockers.append(Reason(
            SOURCE_FINGERPRINT_MOVED,
            "저장된 변환 이후 원본 legacy payload 가 바뀌었다 — 다시 변환(record)하고 다시 검증해야 한다"
            f" (저장 {state.source_fingerprint[:12]} ≠ 현재 {facts.source_fingerprint[:12]})",
        ))
    if state.source_schema_checksum != facts.source_schema_checksum:
        blockers.append(Reason(
            SOURCE_SCHEMA_CHECKSUM_MOVED,
            "원본 저장 스키마의 **모양**이 바뀌었다 — 경로 회계가 더 이상 완전하지 않다"
            f" (저장 {state.source_schema_checksum[:12]} ≠ 현재 {facts.source_schema_checksum[:12]})",
        ))
    if state.semantic_fingerprint != facts.semantic_fingerprint:
        blockers.append(Reason(
            SEMANTIC_FINGERPRINT_MOVED,
            "저장된 변환의 의미 지문이 지금 변환과 다르다 — 같은 원본에서 다른 뜻이 나왔다",
        ))
    if state.binding_fingerprint != facts.binding_fingerprint:
        # 세 축을 나눈 이유가 여기서 값을 한다: 뜻은 그대로인데 카탈로그·컴파일러가 바뀌어
        # **같은 뜻이 다른 SQL** 이 된 경우다. 검증은 그 SQL 을 재지 않았다.
        blockers.append(Reason(
            BINDING_FINGERPRINT_MOVED,
            "의미는 같지만 물리 바인딩·컴파일러 버전이 바뀌었다 — 검증이 잰 SQL 과 지금 나올 SQL 이 다르다"
            " (다시 검증하면 해소된다)",
        ))
    return blockers


def _stored_expression_blockers(state: StoredState, facts: ConversionFacts) -> list[Reason]:
    """플랜에 얹을 **저장된 산출물**이 지금 변환과 같은 뜻인가.

    cut-over 가 저장본을 얹는 이유는 그것이 검증을 통과한 바로 그 산출물이기 때문이다. 그렇다면
    저장본이 지금 변환과 같은 뜻이라는 사실도 여기서 확인해야 한다 — 아니면 '검증된 것을 얹는다'가
    '언젠가 저장된 것을 얹는다'가 된다.
    """
    envelope = state.event_expression
    expression = envelope.get("expression") if isinstance(envelope, Mapping) else None
    if not isinstance(expression, Mapping):
        return [Reason(
            EXPRESSION_MISSING,
            "저장된 이행 산출물(event_expression)이 없다 — 검증한 산출물을 그대로 얹을 수 없다",
        )]
    stored_fingerprint = migration_fingerprint.compute_semantic_fingerprint(expression)
    if stored_fingerprint != facts.semantic_fingerprint:
        return [Reason(
            SEMANTIC_FINGERPRINT_MOVED,
            "저장된 산출물의 의미 지문이 지금 변환과 다르다 — 검증된 표현이 아니다"
            f" (저장 {stored_fingerprint[:12]} ≠ 현재 {facts.semantic_fingerprint[:12]})",
        )]
    return []


def _plan_row_blockers(
    plan_source_fingerprint: str | None, facts: ConversionFacts
) -> list[Reason]:
    """판정한 payload 와 **권위를 얹을 저장 플랜**이 같은 오디언스인가.

    판정은 ``--assets``/``--source`` 로 읽은 payload 를 지금 다시 변환해서 하고, 스탬프는
    ``audience_key`` 로 찾은 저장 행에 한다. 그 둘이 다르면 **파일 코퍼스에서 검증한 IR 이 저장
    플랜의 실행 권위가 된다** — 검증한 자산과 실행할 자산이 다르다.

    같은 계열의 대조가 rollback 에도 있지만(:data:`LEGACY_PAYLOAD_DRIFTED`) 거기서는 경고다.
    방향이 반대이기 때문이다: 되돌아가는 길은 무관한 편집 하나로 잠기면 안 되고, 옮기는 길은
    검증한 바로 그 자산에 대해서만 열려야 한다.

    대조 재료가 없으면 통과가 아니라 차단이다 — 이 이행의 규칙("대조할 수 없으면 '다르다'가 아니라
    '대조하지 않았다'")이 여기서는 차단으로 나타난다. 인자를 넘기지 않은 호출도 같은 취급이다.
    그러지 않으면 대조를 **빼먹은** 호출이 조용히 통과하는데, 그것이 §9-12 가 적어 둔 결함이다.
    """
    if plan_source_fingerprint is None:
        return [Reason(
            PLAN_PAYLOAD_UNVERIFIED,
            "저장 플랜의 원본 지문이 판정에 들어오지 않았다 — 무엇에 권위를 얹는지 확인하지 않았다",
        )]
    if not plan_source_fingerprint:
        return [Reason(
            PLAN_PAYLOAD_UNVERIFIED,
            "저장 플랜에서 원본 지문을 읽지 못했다 — 대조하지 못한 것은 '같다'가 아니다",
        )]
    if plan_source_fingerprint != facts.source_fingerprint:
        return [Reason(
            PLAN_PAYLOAD_MISMATCH,
            "판정한 payload 와 저장 플랜의 오디언스가 다르다 — 검증한 자산이 아닌 곳에 권위를 얹게 된다"
            f" (플랜 {plan_source_fingerprint[:12]} ≠ 판정 {facts.source_fingerprint[:12]})",
        )]
    return []


def evaluate_record(
    state: StoredState | None, facts: ConversionFacts, *, asset_id: str, revision: int
) -> Decision:
    """변환 결과 저장 판정(권위는 움직이지 않는다).

    이미 Event IR 권위인 자산의 변환을 덮어쓰지 않는 것이 이 판정의 유일한 차단이다 — 덮어쓰면
    실행 중인 표현과 저장된 근거가 갈라지고, 그 상태에서 rollback 은 무엇으로 되돌릴지 모른다.
    """
    blockers: list[Reason] = []
    warnings: list[Reason] = []
    target = facts.status
    if state is None:
        return Decision(
            action=ACTION_RECORD, asset_id=asset_id, revision=revision,
            from_status=MigrationStatus.LEGACY_ONLY, to_status=target, facts=facts,
        )
    if state.authority is AudienceAuthority.EVENT_IR:
        blockers.append(Reason(
            AUTHORITY_ALREADY_EVENT_IR,
            f"실행 권위가 이미 Event IR 인 자산({state.status.value})의 변환을 덮어쓰지 않는다 — rollback 이 먼저다",
        ))
    elif state.status != target:
        blocker = _transition_blocker(state.status, target)
        if blocker:
            blockers.append(blocker)
    if state.source_fingerprint and state.source_fingerprint != facts.source_fingerprint:
        # 차단이 아니다: 원본이 바뀐 자산을 **다시 변환해 저장하는 것**이 정확히 record 의 일이다.
        # 다만 그때 기존 검증 근거는 다른 원본의 것이므로 저장 계층이 지운다(그 사실을 남긴다).
        warnings.append(Reason(
            SOURCE_FINGERPRINT_MOVED,
            "원본이 바뀌어 다시 변환한다 — 기존 shadow 검증 근거는 그 원본의 것이므로 폐기된다",
        ))
    if state.verification_digest:
        # 다시 변환하면 검증 근거는 무조건 폐기된다(원본이 그대로여도 그렇다 — 근거는 '그때 그 산출물'
        # 에 붙어 있다). 조용히 지우면 운영자는 승격이 왜 필요해졌는지 모른 채 다시 승격을 돌린다.
        warnings.append(Reason(
            VERIFICATION_EVIDENCE_DISCARDED,
            f"저장돼 있던 shadow 검증 근거를 폐기하고 {MigrationStatus.CONVERTED.value} 로 되돌린다"
            " — 다시 promote 해야 cut-over 할 수 있다",
        ))
    return Decision(
        action=ACTION_RECORD, asset_id=asset_id, revision=revision,
        from_status=state.status, to_status=target, facts=facts,
        blockers=tuple(blockers), warnings=tuple(warnings),
    )


def evaluate_promotion(
    state: StoredState | None, verdict: ShadowVerdict, facts: ConversionFacts
) -> Decision:
    """``CONVERTED`` → ``SHADOW_VERIFIED`` 판정 — 보고서가 **이 자산의 이 상태**를 검증했는가."""
    blockers: list[Reason] = []
    target = MigrationStatus.SHADOW_VERIFIED
    if state is None:
        return Decision(
            action=ACTION_PROMOTE, asset_id=verdict.asset_id, revision=verdict.revision,
            from_status=None, to_status=None, facts=facts,
            blockers=(Reason(STATE_ROW_MISSING, "저장된 변환이 없다 — record 가 먼저다"),),
        )

    blocker = _transition_blocker(state.status, target)
    if blocker:
        blockers.append(blocker)
    blockers.extend(_fingerprint_blockers(state, facts))
    if not facts.is_executable:
        blockers.append(Reason(
            CONVERSION_NOT_EXECUTABLE,
            "변환이 실행 가능 상태가 아니다 — 반쯤 변환된 오디언스는 실패보다 나쁘다",
        ))
    if not verdict.cutover_allowed:
        blockers.append(Reason(
            SHADOW_REPORT_BLOCKED,
            "shadow 보고서가 차단 상태다: " + (
                " · ".join(verdict.blocking_reasons) or "사유가 보고서에 없다"
            ),
        ))
    if verdict.not_run_stages:
        # cutover_allowed 가 이미 필수 단계 미실행을 막지만, 그 판정은 **보고서를 만든 쪽**의 것이다.
        # 여기서 한 번 더 세는 이유는 보고서 스키마가 바뀌어도 "미실행은 통과가 아니다"가 살아남게
        # 하기 위해서다(그 규칙이 이 이행에서 가장 자주 우회될 규칙이다).
        blockers.append(Reason(
            SHADOW_REPORT_STAGE_NOT_RUN,
            f"미실행 검증 단계가 있다: {', '.join(verdict.not_run_stages)} — 미실행은 통과가 아니다",
        ))
    if verdict.source_fingerprint != facts.source_fingerprint:
        blockers.append(Reason(
            SHADOW_REPORT_FINGERPRINT_MISMATCH,
            "보고서가 검증한 원본과 지금 원본이 다르다 — 그 보고서는 다른 것을 검증했다",
        ))
    if verdict.semantic_fingerprint and verdict.semantic_fingerprint != facts.semantic_fingerprint:
        blockers.append(Reason(
            SHADOW_REPORT_FINGERPRINT_MISMATCH,
            "보고서가 검증한 의미와 지금 변환의 의미가 다르다",
        ))
    return Decision(
        action=ACTION_PROMOTE, asset_id=verdict.asset_id, revision=verdict.revision,
        from_status=state.status, to_status=target if not blockers else None,
        blockers=tuple(blockers), facts=facts,
        evidence_digest=verdict.digest, verified_at=verdict.generated_at,
        notes={"shadow": verdict.to_dict()},
    )


def evaluate_cutover(
    state: StoredState | None,
    facts: ConversionFacts,
    *,
    asset_id: str,
    revision: int,
    plan_present: bool,
    plan_source_fingerprint: str | None = None,
    verdict: ShadowVerdict | None = None,
) -> Decision:
    """``SHADOW_VERIFIED`` → ``EVENT_IR_PRIMARY`` 판정 — **권위가 실제로 움직이는 유일한 자리**.

    ``plan_source_fingerprint`` 는 권위를 얹을 **저장 플랜**의 오디언스를 지금 다시 변환해서 얻은
    원본 지문이다. 판정 대상(payload)과 스탬프 대상(플랜 행)이 같은 자산인지 여기서 대조한다 —
    넘기지 않으면 차단된다(:func:`_plan_row_blockers`).

    ``verdict`` 를 다시 줄 수 있다(선택). 주면 승격 때 저장한 근거 지문과 같은지 대조한다 —
    다른 보고서로 승격해 두고 또 다른 보고서를 들고 와서 cut-over 하는 경로를 막는다.
    """
    blockers: list[Reason] = []
    target = MigrationStatus.EVENT_IR_PRIMARY
    if state is None:
        return Decision(
            action=ACTION_CUTOVER, asset_id=asset_id, revision=revision,
            from_status=None, to_status=None, facts=facts,
            blockers=(Reason(STATE_ROW_MISSING, "저장된 이행 상태가 없다 — record → promote 가 먼저다"),),
        )

    blocker = _transition_blocker(state.status, target)
    if blocker:
        blockers.append(blocker)
    blockers.extend(_fingerprint_blockers(state, facts))
    if not facts.is_executable or facts.expression is None:
        blockers.append(Reason(
            CONVERSION_NOT_EXECUTABLE,
            "변환이 실행 가능 상태가 아니거나 표현이 없다",
        ))
    if not (state.verification_digest and state.verified_at):
        blockers.append(Reason(
            SHADOW_VERIFICATION_MISSING,
            "검증 근거(보고서 지문·검증 시각)가 저장돼 있지 않다 — 무엇이 이 cut-over 를 허가했는지 말할 수 없다",
        ))
    if verdict is not None and verdict.digest != state.verification_digest:
        blockers.append(Reason(
            SHADOW_EVIDENCE_CHANGED,
            "제시한 보고서가 승격 근거와 다르다 — 같은 보고서로 승격하고 cut-over 해야 한다",
        ))
    if not state.legacy_payload:
        # rollback 의 재료를 확보하지 못한 채 권위를 옮기면, 되돌리는 길이 '역변환'밖에 남지 않는다.
        blockers.append(Reason(
            PRESERVED_PAYLOAD_MISSING,
            "보존된 legacy payload 가 없다 — 되돌릴 재료 없이 권위를 옮기지 않는다",
        ))
    blockers.extend(_stored_expression_blockers(state, facts))
    if not plan_present:
        blockers.append(Reason(
            PLAN_ROW_MISSING,
            "저장된 자산(query_plan)이 없다 — 파일 코퍼스는 실행 경로가 아니므로 권위를 옮길 대상이 없다",
        ))
    else:
        blockers.extend(_plan_row_blockers(plan_source_fingerprint, facts))
    return Decision(
        action=ACTION_CUTOVER, asset_id=asset_id, revision=revision,
        from_status=state.status, to_status=target if not blockers else None,
        blockers=tuple(blockers), facts=facts,
        evidence_digest=state.verification_digest, verified_at=state.verified_at,
    )


def evaluate_rollback(
    state: StoredState | None,
    *,
    asset_id: str,
    revision: int,
    plan_present: bool,
    unsupported_paths: Sequence[str] = (),
    live_source_fingerprint: str = "",
) -> Decision:
    """실행 권위를 legacy 로 되돌리는 판정.

    되돌리는 방법은 **보존된 payload 로 권위 값을 되돌리는 것**이지 IR → 슬롯 역변환이 아니다.
    그래서 이 판정이 보는 것은 IR 이 아니라 ① 보존 payload 가 있는가 ② 그 payload 가 저장 중에
    깨지지 않았는가 ③ 그 슬롯을 legacy 컴파일러가 지금도 실행할 수 있는가 셋이다.
    """
    blockers: list[Reason] = []
    warnings: list[Reason] = []
    # 목적지는 항상 LEGACY_ONLY 다(:data:`ROLLBACK_TARGET`). 다른 목적지를 인자로 열어 두지 않는
    # 이유는 그 순간 rollback 이 '아무 상태로나 가는 명령'이 되기 때문이다.
    destination = ROLLBACK_TARGET
    if state is None:
        return Decision(
            action=ACTION_ROLLBACK, asset_id=asset_id, revision=revision,
            from_status=None, to_status=None,
            blockers=(Reason(STATE_ROW_MISSING, "저장된 이행 상태가 없다 — 되돌릴 대상이 없다"),),
        )
    if state.revision != revision:
        blockers.append(Reason(
            REVISION_MISMATCH,
            f"보존 상태의 revision({state.revision})이 요청한 revision({revision})과 다르다",
        ))
    if state.authority is not AudienceAuthority.EVENT_IR:
        # 전이표는 CONVERTED → LEGACY_ONLY 를 허용하지만 그것은 '변환 취소'이지 rollback 이 아니다.
        # rollback 으로 적으면 감사 기록이 "권위를 되돌렸다"고 말하는데 움직인 권위가 없다.
        blockers.append(Reason(
            ILLEGAL_TRANSITION,
            f"실행 권위가 이미 legacy 다({state.status.value}) — 되돌릴 권위가 없다",
        ))
    blocker = _transition_blocker(state.status, destination)
    if blocker:
        blockers.append(blocker)
    if not state.legacy_payload:
        blockers.append(Reason(
            PRESERVED_PAYLOAD_MISSING,
            "보존된 legacy payload 가 없다 — 역변환으로 만들어 내지 않는다",
        ))
    elif migration_fingerprint.digest(state.legacy_payload) != state.legacy_payload_checksum:
        blockers.append(Reason(
            PRESERVED_PAYLOAD_CORRUPT,
            "보존 payload 의 체크섬이 맞지 않는다 — 저장 중에 바뀐 것을 되돌리지 않는다",
        ))
    if unsupported_paths:
        blockers.append(Reason(
            LEGACY_SLOT_UNSUPPORTED,
            "legacy 컴파일러가 더 이상 지원하지 않는 슬롯이 보존 payload 에 있다"
            f"({', '.join(unsupported_paths)}) — 되돌리면 그 조건이 조용히 사라진다",
        ))
    if not plan_present:
        blockers.append(Reason(
            PLAN_ROW_MISSING, "저장된 자산(query_plan)이 없다 — 권위를 되돌릴 대상이 없다",
        ))
    if (
        live_source_fingerprint
        and state.source_fingerprint
        and live_source_fingerprint != state.source_fingerprint
    ):
        # **경고이지 차단이 아니다.** cut-over 이후 원본이 편집됐다면 되돌린 legacy 는 편집된 원본을
        # 실행한다 — 그것은 애초에 cut-over 하지 않았을 때와 같은 상태다. 여기서 막으면 잘못된
        # cut-over 에서 빠져나오는 유일한 길을 무관한 편집 하나가 잠근다.
        warnings.append(Reason(
            LEGACY_PAYLOAD_DRIFTED,
            "cut-over 이후 원본이 바뀌었다 — 되돌린 legacy 는 보존본이 아니라 **현재 원본**을 실행한다",
        ))
    return Decision(
        action=ACTION_ROLLBACK, asset_id=asset_id, revision=revision,
        from_status=state.status, to_status=destination if not blockers else None,
        blockers=tuple(blockers), warnings=tuple(warnings),
        evidence_digest=state.verification_digest, verified_at=state.verified_at,
    )


# ── legacy 슬롯 지원 여부 ─────────────────────────────────────────────────────────


def unsupported_legacy_paths(
    payload: Mapping[str, Any],
    *,
    containers: Collection[str],
    supported_slots: Collection[str],
    supported_behaviors: Collection[str],
) -> tuple[str, ...]:
    """보존 payload 에서 **legacy 컴파일러가 더 이상 실행하지 못하는** 경로들.

    어휘(슬롯 이름·행동 이름)는 주입받는다 — 이 모듈이 슬롯 목록을 자기 소스에 적으면 소유자가
    하나 더 생기고, 슬롯이 사라지는 날 이 판정만 옛 목록으로 "지원한다"고 답한다.
    """
    slots = {str(name) for name in supported_slots}
    behaviors = {str(name) for name in supported_behaviors}
    missing: list[str] = []
    for container in containers:
        values = payload.get(container)
        if not isinstance(values, Mapping):
            continue
        for key, value in values.items():
            if migration_fingerprint.is_empty(value):
                continue
            name = str(key)
            if name == "behaviors":
                unknown = sorted({str(item) for item in value} - behaviors)
                missing.extend(f"{container}.behaviors:{item}" for item in unknown)
                continue
            if name not in slots:
                missing.append(f"{container}.{name}")
    return tuple(sorted(missing))


# ── 플랜 페이로드 변환 ────────────────────────────────────────────────────────────


def plan_after_cutover(plan: Mapping[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
    """cut-over 후의 플랜 — **legacy 슬롯은 그대로 두고** 표현을 얹고 권위를 선언한다.

    슬롯을 지우지 않는 것이 rollback 을 '권위 되돌리기'로 유지하는 조건이다(지우면 되돌릴 때
    역변환이 필요해진다). 실행 경로에서 두 해석이 동시에 도는 일은 권위 판정이 막는다 —
    Event IR 권위에서는 회원 속성 컴파일러가 호출되지 않는다.
    """
    if not isinstance(envelope.get("expression"), Mapping):
        raise CutoverError("cut-over envelope 에 표현(expression)이 없다")
    updated = dict(plan)
    updated[EVENT_EXPRESSION_KEY] = dict(envelope)
    stamp_authority(updated, AudienceAuthority.EVENT_IR)
    return updated


def plan_after_rollback(plan: Mapping[str, Any]) -> dict[str, Any]:
    """rollback 후의 플랜 — 권위만 되돌린다. **표현은 지우지 않는다.**

    지우면 rollback 이 '표현을 삭제하는 일'이 되고, 그러면 되돌린 뒤 무엇을 검증했었는지가
    저장소에서 사라진다. 저장은 남고 실행만 legacy 로 돌아간다(dual-storage ≠ dual-execution).
    """
    updated = dict(plan)
    stamp_authority(updated, AudienceAuthority.LEGACY)
    return updated


def executed_authority(plan: Mapping[str, Any]) -> AudienceAuthority:
    """이 플랜을 지금 실행하면 어느 계층이 오디언스를 소유하는가.

    스탬프 직후 이것을 다시 묻는 것이 cut-over 의 사후 조건이다 — 우리가 **쓴 것**과 실행기가
    **읽는 것**이 어긋나면 "권위를 옮겼다"가 거짓이 되는데, 그 어긋남은 저장 형식이 바뀌는 날
    조용히 생긴다.
    """
    try:
        return resolve_authority(plan)
    except AudienceAuthorityError:
        return AudienceAuthority.LEGACY


__all__ = [
    "ACTIONS",
    "ACTION_CUTOVER",
    "ACTION_PROMOTE",
    "ACTION_RECORD",
    "ACTION_ROLLBACK",
    "AUTHORITY_ALREADY_EVENT_IR",
    "BINDING_FINGERPRINT_MOVED",
    "CONVERSION_NOT_EXECUTABLE",
    "ConversionFacts",
    "CutoverError",
    "Decision",
    "EXPRESSION_MISSING",
    "ILLEGAL_TRANSITION",
    "LEGACY_PAYLOAD_DRIFTED",
    "LEGACY_SLOT_UNSUPPORTED",
    "PLAN_AUTHORITY_KEY",
    "PLAN_PAYLOAD_MISMATCH",
    "PLAN_PAYLOAD_UNVERIFIED",
    "PLAN_ROW_MISSING",
    "PRESERVED_PAYLOAD_CORRUPT",
    "PRESERVED_PAYLOAD_MISSING",
    "REVISION_MISMATCH",
    "Reason",
    "SEMANTIC_FINGERPRINT_MOVED",
    "SHADOW_EVIDENCE_CHANGED",
    "SHADOW_REPORT_BLOCKED",
    "SHADOW_REPORT_FINGERPRINT_MISMATCH",
    "SHADOW_REPORT_STAGE_NOT_RUN",
    "SHADOW_VERIFICATION_MISSING",
    "SOURCE_FINGERPRINT_MOVED",
    "SOURCE_SCHEMA_CHECKSUM_MOVED",
    "STATE_ROW_MISSING",
    "VERIFICATION_EVIDENCE_DISCARDED",
    "ShadowVerdict",
    "StoredState",
    "evaluate_cutover",
    "evaluate_promotion",
    "evaluate_record",
    "evaluate_rollback",
    "executed_authority",
    "plan_after_cutover",
    "plan_after_rollback",
    "shadow_verdict_from_report",
    "unsupported_legacy_paths",
]
