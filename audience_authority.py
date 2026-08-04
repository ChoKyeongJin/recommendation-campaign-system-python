"""오디언스 실행 권위 — "이 플랜의 조건을 **누가** 실행하는가"의 명시적 단일 판정자.

배경: 지금까지 실행 경로 선택은 `plan.event_expression` 이라는 **자산의 존재**로 결정됐다
(`graph_rag._has_canonical_audience_authority`). 존재는 권위가 아니다 — 이행기에는 같은 자산이
"변환은 됐지만 아직 검증 전"인 상태로 저장되어야 하고, 그때 저장이 곧 실행이 되면
① 검증되지 않은 IR 이 사용자 요청을 실행하고 ② rollback 이 "표현을 지우는 것"이 되어
원본 legacy payload 를 건드리게 된다. 그래서 **저장 여부와 실행 권위를 분리**한다:

    저장   plan.event_expression        (있을 수 있다 — dual-storage)
    권위   plan.audience_authority      (하나뿐이다 — dual-execution 금지)

이 모듈의 계약 셋:

1. 실행기는 :func:`resolve_authority` **하나만** 보고 경로를 고른다. 표현의 존재를 보지 않는다.
2. Event IR 컴파일이 실패해도 legacy 로 자동 폴백하지 않는다 — 권위는 값이지 결과가 아니다.
3. rollback 은 IR 을 슬롯으로 역변환하지 않는다. 보존된 legacy payload 를 그대로 두고
   **권위 값만** 되돌린다(:data:`ROLLBACK_TARGET`).

순수 모듈 규약: graph_rag 도, event_ir 도, 도메인 모듈도 import 하지 않는다. 권위는 상태 기계이고
그 상태 기계는 어떤 조건이 실행되는지 알 필요가 없다.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, MutableMapping


class AudienceAuthorityError(ValueError):
    """권위 선언이 닫힌 어휘 밖이거나, 허용되지 않은 상태 전이를 요구했다."""


class AudienceAuthority(str, Enum):
    """오디언스 조건을 실행하는 계층. 한 실행에서 **정확히 하나**만 성립한다."""

    LEGACY = "legacy"
    EVENT_IR = "event_ir"


class MigrationStatus(str, Enum):
    """자산 하나(``asset_id`` + ``revision``)의 이행 상태.

    앞의 여섯은 정상 진행 경로이고, 뒤의 여섯은 **진행이 막힌 이유**다. 막힘을 하나의
    'failed' 로 뭉치지 않는 이유는 해소 주체가 다르기 때문이다 — 카탈로그 선언(BLOCKED_CATALOG),
    IR 대수(BLOCKED_IR_EXTENSION), 업무 결정(BLOCKED_DOMAIN_DECISION)은 서로 다른 사람이
    서로 다른 작업으로 푼다.
    """

    LEGACY_ONLY = "LEGACY_ONLY"
    CONVERTED = "CONVERTED"
    SHADOW_VERIFIED = "SHADOW_VERIFIED"
    EVENT_IR_PRIMARY = "EVENT_IR_PRIMARY"
    ROLLBACK_ELIGIBLE = "ROLLBACK_ELIGIBLE"
    RETIRED = "RETIRED"

    QUARANTINED = "QUARANTINED"
    STALE = "STALE"
    BLOCKED_CATALOG = "BLOCKED_CATALOG"
    BLOCKED_IR_EXTENSION = "BLOCKED_IR_EXTENSION"
    BLOCKED_DOMAIN_DECISION = "BLOCKED_DOMAIN_DECISION"
    INVALID_LEGACY_ASSET = "INVALID_LEGACY_ASSET"


# 플랜 최상위의 명시적 권위 키. plan_schema 가 DERIVED 로 분류한다(사용자가 말한 조건이 아니라
# 이행 라우팅 판정이다).
PLAN_AUTHORITY_KEY = "audience_authority"

# 이행 전 ingress 가 canonical 표현을 세울 때 쓰던 표식. 명시 권위 필드가 없는 **기존 저장 페이로드**
# 를 읽기 위한 호환 인코딩이며, 새 생산자는 :func:`stamp_authority` 로 명시 필드를 남긴다.
# 이 집합을 넓히는 것은 "이 표식이 곧 실행 권위다"라는 선언이므로 사유 없이 늘리지 않는다.
CANONICAL_EVENT_EXPRESSION_SOURCES: frozenset[str] = frozenset({"audience_requirement", "semantic_plan"})
EVENT_EXPRESSION_KEY = "event_expression"

# 상태 → 그 상태에서 성립하는 실행 권위. **파생이다** — 상태와 권위를 따로 저장하면 둘이 갈라지고,
# 갈라진 순간 "무엇이 실행됐는가"의 답이 둘이 된다.
_EVENT_IR_STATUSES: frozenset[MigrationStatus] = frozenset({
    MigrationStatus.EVENT_IR_PRIMARY,
    MigrationStatus.ROLLBACK_ELIGIBLE,
    MigrationStatus.RETIRED,
})

# 실행 권위를 Event IR 로 올릴 수 있는 **유일한** 선행 상태. 이 표를 넓히면 검증(shadow) 단계를
# 건너뛴 cut-over 가 가능해진다 — 그것이 이 이행에서 가장 비싼 사고다.
CUTOVER_PREDECESSOR: MigrationStatus = MigrationStatus.SHADOW_VERIFIED

# rollback 이 도달하는 상태. IR → 슬롯 역변환이 아니라 **보존된 legacy payload 로 권위를 되돌리는 것**
# 이므로 목적지는 항상 LEGACY_ONLY 다.
ROLLBACK_TARGET: MigrationStatus = MigrationStatus.LEGACY_ONLY

_BLOCKED: frozenset[MigrationStatus] = frozenset({
    MigrationStatus.BLOCKED_CATALOG,
    MigrationStatus.BLOCKED_IR_EXTENSION,
    MigrationStatus.BLOCKED_DOMAIN_DECISION,
    MigrationStatus.INVALID_LEGACY_ASSET,
})

# 허용 전이표. 여기 없는 전이는 예외다 — "일단 상태만 바꿔 두자"가 검증 단계를 건너뛰는 방식이었다.
ALLOWED_TRANSITIONS: dict[MigrationStatus, frozenset[MigrationStatus]] = {
    MigrationStatus.LEGACY_ONLY: frozenset({
        MigrationStatus.CONVERTED, MigrationStatus.QUARANTINED, *_BLOCKED,
    }),
    MigrationStatus.CONVERTED: frozenset({
        MigrationStatus.SHADOW_VERIFIED, MigrationStatus.STALE,
        MigrationStatus.QUARANTINED, MigrationStatus.LEGACY_ONLY, *_BLOCKED,
    }),
    MigrationStatus.SHADOW_VERIFIED: frozenset({
        MigrationStatus.EVENT_IR_PRIMARY, MigrationStatus.STALE,
        MigrationStatus.QUARANTINED, MigrationStatus.CONVERTED, *_BLOCKED,
    }),
    MigrationStatus.EVENT_IR_PRIMARY: frozenset({
        MigrationStatus.ROLLBACK_ELIGIBLE, ROLLBACK_TARGET, MigrationStatus.QUARANTINED,
    }),
    MigrationStatus.ROLLBACK_ELIGIBLE: frozenset({
        MigrationStatus.RETIRED, ROLLBACK_TARGET, MigrationStatus.QUARANTINED,
    }),
    # 종착지. legacy payload 가 폐기된 뒤이므로 되돌릴 대상이 없다 — 되돌리려면 자산을 새로 만든다.
    MigrationStatus.RETIRED: frozenset(),
    MigrationStatus.QUARANTINED: frozenset({MigrationStatus.LEGACY_ONLY, MigrationStatus.CONVERTED}),
    # 원본이 바뀐 자산은 **다시 변환**해서만 진행한다. SHADOW_VERIFIED 로 직행하는 길은 없다.
    MigrationStatus.STALE: frozenset({
        MigrationStatus.CONVERTED, MigrationStatus.LEGACY_ONLY, MigrationStatus.QUARANTINED,
    }),
    **{
        status: frozenset({
            MigrationStatus.CONVERTED, MigrationStatus.LEGACY_ONLY, MigrationStatus.QUARANTINED,
        })
        for status in _BLOCKED
    },
}


def authority_for_status(status: MigrationStatus) -> AudienceAuthority:
    """상태가 함의하는 실행 권위(파생 — 저장하지 않는다)."""
    return AudienceAuthority.EVENT_IR if status in _EVENT_IR_STATUSES else AudienceAuthority.LEGACY


def is_blocked(status: MigrationStatus) -> bool:
    return status in _BLOCKED


def coerce_authority(raw: Any) -> AudienceAuthority:
    """닫힌 어휘로 접는다. 모르는 값은 **legacy 로 강등하지 않고** 예외다.

    강등하면 Event IR 권위 자산이 오타 하나로 조용히 legacy 로 실행된다 — 두 경로의 결과가 다를 수
    있는 이행기에 그것은 '조용히 다른 오디언스'다.
    """
    if isinstance(raw, AudienceAuthority):
        return raw
    if isinstance(raw, str):
        try:
            return AudienceAuthority(raw.strip().casefold())
        except ValueError as exc:
            raise AudienceAuthorityError(
                f"알 수 없는 오디언스 실행 권위: {raw!r}"
                f" (허용: {', '.join(item.value for item in AudienceAuthority)})"
            ) from exc
    raise AudienceAuthorityError(f"오디언스 실행 권위는 문자열이어야 한다: {raw!r}")


def coerce_status(raw: Any) -> MigrationStatus:
    if isinstance(raw, MigrationStatus):
        return raw
    if isinstance(raw, str):
        try:
            return MigrationStatus(raw.strip().upper())
        except ValueError as exc:
            raise AudienceAuthorityError(f"알 수 없는 이행 상태: {raw!r}") from exc
    raise AudienceAuthorityError(f"이행 상태는 문자열이어야 한다: {raw!r}")


def can_transition(current: MigrationStatus, target: MigrationStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(current: MigrationStatus, target: MigrationStatus) -> MigrationStatus:
    """허용된 전이만 통과시킨다(아니면 예외). 상태를 바꾸는 유일한 통로."""
    if not can_transition(current, target):
        raise AudienceAuthorityError(
            f"허용되지 않은 이행 전이: {current.value} → {target.value}"
            f" (허용: {sorted(item.value for item in ALLOWED_TRANSITIONS.get(current, frozenset()))})"
        )
    return target


def _compatibility_authority(plan: Mapping[str, Any]) -> AudienceAuthority:
    """명시 권위 필드가 없는 **기존 저장 페이로드**의 권위를 읽는다.

    이행 전 ingress 는 canonical 표현을 세울 때 ``event_expression.source`` 에 생산자 이름을 적었고,
    실행기는 그 표식으로 경로를 골랐다. 그 페이로드는 이미 저장돼 있으므로, 표식을 **명시 필드와
    같은 뜻으로 읽어 주는 것**이 유일한 무손실 호환이다. 표현이 그냥 '있다'는 것은 여전히 권위가
    아니다 — 이행 어댑터가 세우는 표현은 이 표식을 달지 않고, 그래서 legacy 로 남는다.
    """
    payload = plan.get(EVENT_EXPRESSION_KEY)
    if (
        isinstance(payload, Mapping)
        and isinstance(payload.get("expression"), Mapping)
        and payload.get("source") in CANONICAL_EVENT_EXPRESSION_SOURCES
    ):
        return AudienceAuthority.EVENT_IR
    return AudienceAuthority.LEGACY


def resolve_authority(plan: Any) -> AudienceAuthority:
    """이 플랜의 오디언스를 실행하는 계층. **실행기가 보는 유일한 값.**"""
    if not isinstance(plan, Mapping):
        return AudienceAuthority.LEGACY
    declared = plan.get(PLAN_AUTHORITY_KEY)
    if declared is not None:
        return coerce_authority(declared)
    return _compatibility_authority(plan)


def executes_event_ir(plan: Any) -> bool:
    return resolve_authority(plan) is AudienceAuthority.EVENT_IR


def declares_canonical_audience(plan: Any) -> bool:
    """Whether the request entered through the canonical audience contract.

    This is intentionally different from :func:`executes_event_ir`: a failed
    canonical request has no executable expression yet, but it still must not
    fall through to a second audience language.  An explicitly stamped legacy
    authority is the only rollback escape hatch for stored migration assets.
    """

    if not isinstance(plan, Mapping):
        return False
    requirement = plan.get("audience_requirement")
    if not isinstance(requirement, Mapping):
        return False
    semantic_plan = plan.get("semantic_plan")
    semantic_nodes = (
        semantic_plan.get("nodes") if isinstance(semantic_plan, Mapping) else None
    )
    # CampaignPlan V4 carries an empty audience placeholder even when a
    # non-audience SemanticPlan node owns the request (metric/history flows).
    # That placeholder is not an audience language.  A real expression, an
    # explicit issue, or an otherwise empty semantic plan is canonical ingress.
    return bool(
        isinstance(requirement.get("expression"), Mapping)
        or requirement.get("issues")
        or not semantic_nodes
    )


def requires_event_ir(plan: Any) -> bool:
    """True when canonical ingress must resolve to Event IR or fail closed."""

    if not isinstance(plan, Mapping):
        return False
    # An explicit authority is application-owned migration state.  In
    # particular, ``legacy`` is the only rollback escape hatch; a model must
    # not be able to infer or manufacture it.
    if plan.get(PLAN_AUTHORITY_KEY) is not None:
        return resolve_authority(plan) is AudienceAuthority.EVENT_IR
    # Fresh canonical ingress can fail before an executable expression exists,
    # while cut-over/stored payloads can contain only the authority-compatible
    # expression marker.  Both cases must stay on the one Event IR route.
    return declares_canonical_audience(plan) or executes_event_ir(plan)


def stamp_authority(plan: MutableMapping[str, Any], authority: AudienceAuthority) -> None:
    """명시 권위를 플랜에 남긴다(생산자용). 값은 문자열로 저장한다 — 직렬화 경계를 넘어야 한다."""
    plan[PLAN_AUTHORITY_KEY] = coerce_authority(authority).value


__all__ = [
    "ALLOWED_TRANSITIONS",
    "AudienceAuthority",
    "AudienceAuthorityError",
    "CANONICAL_EVENT_EXPRESSION_SOURCES",
    "CUTOVER_PREDECESSOR",
    "EVENT_EXPRESSION_KEY",
    "MigrationStatus",
    "PLAN_AUTHORITY_KEY",
    "ROLLBACK_TARGET",
    "authority_for_status",
    "can_transition",
    "coerce_authority",
    "coerce_status",
    "declares_canonical_audience",
    "executes_event_ir",
    "is_blocked",
    "resolve_authority",
    "requires_event_ir",
    "stamp_authority",
    "transition",
]
