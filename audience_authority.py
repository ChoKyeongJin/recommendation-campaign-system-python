"""오디언스 실행 권위 — "이 플랜의 조건을 **누가** 실행하는가"의 단일 판정자.

**2026-08-07: legacy 실행 레인이 폐쇄됐다.** 그 전까지 이 모듈은 두 값을 가진 상태 기계였다 —
``legacy``(``target_user``/``exclude`` 슬롯을 회원 SQL 빌더들이 실행) 와 ``event_ir``(canonical
Event IR 을 :func:`graph_rag.build_event_expression_sql_candidate` 가 실행). 지금 성립하는 값은
**하나뿐이다**: 오디언스 언어는 canonical Event IR 하나다.

값이 하나인데 왜 모듈이 남는가. 이유는 하나이고, 그것이 이 파일의 존재 이유 전부다.

    **저장된 ``audience_authority: "legacy"`` 를 조용히 삼키지 않기 위해서다.**

이행기에 그 값은 rollback 탈출구였고, 그 표식을 가진 플랜이 저장돼 있을 수 있다. 어휘에서 그
값을 빼면 :func:`coerce_authority` 가 **명명된 실패**(``audience_authority_invalid``)를 내고,
:func:`graph_rag._audience_authority_blocking_sql_result` 가 그것을 응답 경로 첫 게이트에서 잡는다.
반대로 그 값을 "이제 event_ir 과 같은 뜻"으로 접어 읽으면, legacy 로 실행되기를 기대하고 저장된
플랜이 **다른 오디언스**를 조용히 추출한다 — 폐쇄에서 가장 비싼 사고가 정확히 그것이다.

그래서 규약은 셋이다.

1. 실행기는 :func:`requires_event_ir` **하나만** 보고 경로를 고른다. 표현의 존재를 보지 않는다.
2. Event IR 컴파일이 실패해도 두 번째 오디언스 언어로 폴백하지 않는다 — 실패는 실패다.
3. 어휘 밖 값(``legacy`` 포함)은 강등도 승격도 하지 않고 예외다.

순수 모듈 규약: graph_rag 도, event_ir 도, 도메인 모듈도 import 하지 않는다.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, MutableMapping


class AudienceAuthorityError(ValueError):
    """권위 선언이 닫힌 어휘 밖이다(폐쇄된 ``legacy`` 를 포함한다)."""


class AudienceAuthority(str, Enum):
    """오디언스 조건을 실행하는 계층. 성립하는 값은 하나뿐이다.

    ``LEGACY`` 는 2026-08-07 어휘에서 **제거**됐다. 되살리려면 그것을 실행할 두 번째 오디언스
    언어를 함께 되살려야 하고, 그 순간 "무엇이 실행됐는가"의 답이 다시 둘이 된다.
    """

    EVENT_IR = "event_ir"


# 플랜 최상위의 명시적 권위 키. plan_schema 가 DERIVED 로 분류한다(사용자가 말한 조건이 아니다).
# 값이 하나뿐인 지금도 이 키를 계속 **읽는** 이유는 모듈 docstring 에 있다 — 저장된 ``legacy``
# 표식을 명명된 실패로 드러내기 위해서다.
PLAN_AUTHORITY_KEY = "audience_authority"

EVENT_EXPRESSION_KEY = "event_expression"


def coerce_authority(raw: Any) -> AudienceAuthority:
    """닫힌 어휘로 접는다. 모르는 값은 **강등하지 않고** 예외다.

    강등하면 오타 하나로, 또는 폐쇄된 ``legacy`` 표식 하나로 플랜이 조용히 다른 경로를 타게 된다.
    """
    if isinstance(raw, AudienceAuthority):
        return raw
    if isinstance(raw, str):
        try:
            return AudienceAuthority(raw.strip().casefold())
        except ValueError as exc:
            raise AudienceAuthorityError(
                f"알 수 없는 오디언스 실행 권위: {raw!r}"
                f" (허용: {', '.join(item.value for item in AudienceAuthority)};"
                " 'legacy' 레인은 폐쇄됐습니다)"
            ) from exc
    raise AudienceAuthorityError(f"오디언스 실행 권위는 문자열이어야 한다: {raw!r}")


def resolve_authority(plan: Any) -> AudienceAuthority:
    """이 플랜의 오디언스를 실행하는 계층. **실행기가 보는 유일한 값.**

    선언이 있으면 어휘 검사만 하고(어휘 밖이면 예외), 없으면 그대로 Event IR 이다. 선언 부재를
    legacy 로 읽던 기본값은 폐쇄와 함께 사라졌다 — 그 기본값이 rules 레인·표식 없는 저장 플랜을
    두 번째 오디언스 언어로 흘려보내던 마지막 통로였다.
    """
    if isinstance(plan, Mapping):
        declared = plan.get(PLAN_AUTHORITY_KEY)
        if declared is not None:
            return coerce_authority(declared)
    return AudienceAuthority.EVENT_IR


def executes_event_ir(plan: Any) -> bool:
    """항상 참이다(어휘 밖 선언이면 예외). 호출부는 이 이름으로 불변식을 읽는다."""
    return resolve_authority(plan) is AudienceAuthority.EVENT_IR


def requires_event_ir(plan: Any) -> bool:
    """canonical 오디언스는 Event IR 로 해결되거나 fail-close 한다 — 예외 없이 모든 플랜에서.

    폐쇄 전에는 이 술어가 ``audience_requirement`` 계약의 존재로 갈렸고, 계약이 없는 플랜
    (rules 레인·저장 페이로드)은 False 로 남아 legacy 빌더가 실행했다. 지금은 갈래가 없다.
    """
    return executes_event_ir(plan)


def stamp_authority(plan: MutableMapping[str, Any], authority: AudienceAuthority) -> None:
    """명시 권위를 플랜에 남긴다(생산자용). 값은 문자열로 저장한다 — 직렬화 경계를 넘어야 한다.

    값이 하나뿐이므로 이 도장은 라우팅 선택이 아니라 **불변식의 기록**이다. 계속 찍는 이유는
    저장된 플랜을 나중에 읽는 쪽이 "권위가 명시된 적 없는 플랜"과 "폐쇄 전 legacy 플랜"을
    구분할 수 있어야 하기 때문이다.
    """
    plan[PLAN_AUTHORITY_KEY] = coerce_authority(authority).value


__all__ = [
    "AudienceAuthority",
    "AudienceAuthorityError",
    "EVENT_EXPRESSION_KEY",
    "PLAN_AUTHORITY_KEY",
    "coerce_authority",
    "executes_event_ir",
    "requires_event_ir",
    "resolve_authority",
    "stamp_authority",
]
