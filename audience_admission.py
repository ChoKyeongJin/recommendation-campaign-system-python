"""오디언스 실행 입장 판정 — "이 플랜에 실행 언어가 둘인가"의 단일 술어.

배경. 권위(`audience_authority`)는 "누가 실행하는가"를 답하고, 표면(`plan_schema`)은 "무엇이
오디언스 언어인가"를 답한다. 둘을 **합쳐서** "권위가 Event IR 인데 legacy 언어가 함께 채워져
있는가"를 판정하는 자리가 없어서, 그 판정이 `plan_validation` 안에 `event_expression.source ∈
{audience_requirement, semantic_plan}` 이라는 **리터럴 집합**으로 박혀 있었다. 그 형태의 문제는
둘이다.

  ① 표식이 없는 페이로드(cut-over 산출물 등)에서는 권위가 Event IR 이어도 걸리지 않는다.
  ② 같은 사실을 판정하는 리터럴이 저장소에 하나 더 생긴다.

이 모듈은 그 판정만 소유한다. **순수 모듈 규약**: `plan_schema` + `audience_authority` + stdlib
만 import 한다. 도메인 모듈도, `graph_rag` 도, `plan_validation` 도 부르지 않는다 — 입장 판정이
실행 지식을 알기 시작하면 그 순간 순환이 생기고, `plan_validation` 이 이 모듈을 import 하는
방향이 막힌다.

**표면은 두 컨테이너뿐이다**(`plan_schema.AUDIENCE_CONTAINERS`). `plan_schema.audience_keys()`
의 최상위 키들은 **일부러 보지 않는다** — 넓히면 그 키를 가진 canonical 요청이 fail-close 되는데
그 축은 Event IR 로 표현할 수도 없어 기능이 그대로 줄어든다(docs/plans_event_ir_only.md §6-1
미결정). 넓히는 편집이 곧 그 결정의 기록이며, `tests/test_audience_admission.py` 가 지금의 범위를
얼려 둔다.

**`canonical_event_ir_grounding.has_empty_legacy_audience_surface` 와 다른 술어다.** 그쪽은
컨테이너 2개 + `audience_keys()` 를 보고 "복구된 플랜이 두 번째 언어를
세웠는가"를 답한다. 이쪽은 컨테이너 2개만 보고 "지금 실행하면 해석이 둘인가"를 답한다. 한쪽을
다른 쪽에 위임하면 둘 중 하나가 조용히 바뀐다 — 그래서 위임하지 않고, 같은 이름의 함수도 두지
않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import audience_authority
import plan_schema

# 사유코드. `plan_validation` 이 issue 로 옮겨 실을 때 이름을 다시 적지 않도록 여기서 소유한다.
LEGACY_AUDIENCE_CONFLICT_CODE = "canonical_legacy_audience_conflict"

# canonical 오디언스 계약 키. `plan_schema` 는 이 키를 CONDITION 으로 분류하지만 오디언스
# **컨테이너**는 아니라서 `AUDIENCE_CONTAINERS` 에 없다 — 여기서 이름으로 읽는다.
CANONICAL_AUDIENCE_CONTRACT_KEY = "audience_requirement"


@dataclass(frozen=True, slots=True)
class ExecutionConflict:
    """실행 언어가 둘이 되는 지점 하나. `code` + `path` 만 갖는다.

    메시지를 담지 않는 이유: 소비자인 `PlanValidationIssue` 가 message 를 의도적으로 갖지 않고,
    사용자 문구는 `failure_messages` 가 코드와 경로에서 만든다. 여기서 문장을 만들면 문구
    소유자가 둘이 된다.
    """

    code: str
    path: str


def _is_populated(value: Any) -> bool:
    """값이 '채워져 있는가'. `plan_validation` 의 지역 판정과 **같은 정책**이다.

    `0` 과 `False` 는 **빈 값이 아니다** — 사용자가 말한 조건일 수 있기 때문이다(주문 0건, 동의
    거부). 이 규칙이 흔들리면 조건 하나가 판정에서 조용히 사라진다. 빈 dict/list, `None`,
    빈 문자열만 빈 값이다.
    """
    if isinstance(value, Mapping):
        return any(_is_populated(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_is_populated(item) for item in value)
    return value not in (None, "")


def legacy_audience_paths(plan: Any) -> tuple[str, ...]:
    """채워져 있는 legacy 오디언스 표면의 경로들.

    표기는 ``컨테이너.슬롯`` 으로, 저장소의 기존 관용(`plan_decisions._slot_key`)과 같다.
    순서는 컨테이너 선언 순서 → 컨테이너 안에서는 슬롯 이름 정렬이다. dict 삽입 순서에 기대면
    같은 플랜이 경로 순서만 다르게 나와 진단이 재현되지 않는다.

    컨테이너 자리에 Mapping 이 아닌 값이 채워져 있으면 **컨테이너 이름 하나**를 낸다. 이 경우를
    빠뜨리면 리스트로 채운 표면이 판정에서 통째로 빠진다.
    """
    if not isinstance(plan, Mapping):
        return ()

    paths: list[str] = []
    for container in plan_schema.AUDIENCE_CONTAINERS:
        value = plan.get(container)
        if not _is_populated(value):
            continue
        if not isinstance(value, Mapping):
            paths.append(container)
            continue
        paths.extend(
            f"{container}.{slot}"
            for slot in sorted(value)
            if _is_populated(value[slot])
        )
    return tuple(paths)


def declares_audience(plan: Any) -> bool:
    """이 플랜이 **오디언스를 말하는가** — 어떤 언어로 말했든.

    legacy 레인 폐쇄(2026-08-07)가 만든 술어다. 폐쇄 전에는 "Event IR 표현이 있어야 한다"의
    범위를 canonical 계약(``audience_requirement``)의 존재가 정했고, 계약이 없는 플랜은 legacy
    레인이 받아 갔다. 지금 그 레인이 없으므로 범위를 다시 정해야 하는데, 여기서 **모든 플랜**을
    범위에 넣으면 오디언스를 아예 말하지 않는 요청(집계·분석처럼 회원 조건이 0개인 질의)까지
    "Event IR 표현이 없다"로 죽는다. 그것은 legacy 레인과 아무 상관이 없는 기능 손실이다.

    그래서 범위는 **오디언스를 말한 플랜**이다. 말했다는 증거는 둘 중 하나로 충분하다.

      · canonical 계약이 있다(``audience_requirement``) — 실행 가능한 표현이 아직 없어도.
      · legacy 오디언스 표면이 채워져 있다 — 그 표면이 곧 두 번째 언어로 말한 증거다.

    두 번째 항이 폐쇄의 실질이다. 그 표면을 가진 플랜은 표현이 없으면 SQL 을 못 낸다.
    """
    if not isinstance(plan, Mapping):
        return False
    if isinstance(plan.get(CANONICAL_AUDIENCE_CONTRACT_KEY), Mapping):
        return True
    return bool(legacy_audience_paths(plan))


def execution_conflicts(plan: Any) -> tuple[ExecutionConflict, ...]:
    """이 플랜을 지금 실행하면 성립하는 '해석이 둘' 지점들(없으면 빈 튜플).

    권위를 읽는 술어는 :func:`audience_authority.requires_event_ir` 하나다.
    :func:`~audience_authority.executes_event_ir` 가 아니라 이쪽인 이유: canonical 계약으로
    들어온 요청은 **실행 가능한 표현이 아직 없어도** 두 번째 언어로 새면 안 되고, 명시 ``legacy``
    스탬프(rollback 탈출구)는 두 술어 모두에서 빠져나간다.
    """
    if not audience_authority.requires_event_ir(plan):
        return ()
    return tuple(
        ExecutionConflict(code=LEGACY_AUDIENCE_CONFLICT_CODE, path=path)
        for path in legacy_audience_paths(plan)
    )


__all__ = [
    "CANONICAL_AUDIENCE_CONTRACT_KEY",
    "LEGACY_AUDIENCE_CONFLICT_CODE",
    "ExecutionConflict",
    "declares_audience",
    "execution_conflicts",
    "legacy_audience_paths",
]
