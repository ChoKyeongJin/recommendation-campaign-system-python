"""조건 IR 정규 스냅샷 — 플랜에서 '무엇을 조건으로 이해했는가'만 결정론적으로 뽑는다.

배경: 지금까지 파이프라인 회귀를 잡는 유일한 수단은 최종 SQL 문자열 대조였다. SQL 은 방언·별칭·
조인 순서·포맷이 조금만 달라져도 바뀌므로, 컴파일러를 손대는 리팩터에서는 전부 깨진다 — 정작
확인하고 싶은 것("이 프롬프트에서 어떤 조건을 읽어냈나")은 그 아래 층에 있는데도.

이 모듈은 그 층을 고정한다. :func:`snapshot` 은 query_plan 을 받아 **조건 슬롯만** 남긴 정규형을
돌려준다 — 키 순서가 정렬되고, 빈 슬롯과 휘발성 항목(원문·다이제스트·검색 계측·라우팅 결과)은
빠진다. 골든 테스트는 이 정규형을 비교하므로, SQL 생성 방식이 바뀌어도 '해석 결과'가 같으면 그린이다.

키 분류의 단일 소스는 이 모듈이다.

  * 컨테이너 슬롯(target_user / exclude / campaign_constraints)은 전부 조건이다.
  * plan 최상위는 셋 중 하나로 **반드시** 분류된다:
      - :data:`DERIVED_PLAN_KEYS`      파생 결과(라우팅·계약·정책 판정). 스냅샷에서 뺀다.
      - :mod:`plan_decisions` 의 ``NON_CONDITION_PLAN_KEYS``  원문·계측·로그. 스냅샷에서 뺀다.
      - 그 외                          조건으로 본다. 스냅샷에 들어간다.
    새 키가 생기면 자동으로 '조건'이 되어 골든이 시끄럽게 깨진다. 조용히 새는 것보다 낫다 —
    깨지면 위 두 목록 중 하나에 분류하거나, 진짜 조건으로 받아들이고 골든을 갱신한다.
    :func:`unclassified_plan_keys` 로 계약 테스트가 이 분류를 강제한다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다(plain dict 입력).
"""

from __future__ import annotations

from typing import Any

import plan_decisions

# 스냅샷 스키마 버전. 정규형의 모양(키 구성·정렬 규칙)이 바뀌면 올린다 — 골든 파일이 어느 규칙으로
# 만들어졌는지 파일 자체가 답하게 하려는 것이다. 값 변화(파서 개선)로는 올리지 않는다.
SCHEMA_VERSION = 1

# 조건이 사는 컨테이너(plan_decisions 감사 대상과 같은 목록을 재사용한다 — 두 곳이 갈라지면
# '감사에는 잡히는데 스냅샷에는 없는' 슬롯이 생긴다).
CONTAINERS = plan_decisions.AUDITED_CONTAINERS

# plan 최상위 중 **파생 결과**(입력 해석이 아니라 해석의 결과로 계산된 것). 라우팅·출력 계약·정책
# 판정·능력 점검은 조건이 바뀌면 따라 바뀌므로, 스냅샷에 넣으면 같은 사실을 두 번 비교하게 된다.
DERIVED_PLAN_KEYS = frozenset({
    "capability_check",       # 능력 점검 결과
    "detected_intent",        # 입력 조건에서 계산된 분석 라우팅 힌트
    "member_policy",          # 회원 정책 판정
    "output_contract",        # 출력 계약(컬럼/그레인)
    "selected_route",         # 선택된 라우트
    "policy_constraints",     # 업무 정책에서 파생된 제약
    "semantic_resolutions",   # 의미 해소 흔적
    "source_requirements",    # 소스 요구(다이제스트와 쌍)
    "parser_shadow",          # 파서 shadow 비교 계측(parser_shadow) — 조건이 아니라 관찰이다
    "semantic_evidence",      # V3 의미 슬롯의 원문 근거 — 조건값이 아니라 provenance다
    "slot_policy",            # 슬롯 소유권 판정 흔적(slot_policy)
})

# 조건이 아니라고 이미 선언된 키(원문·계측·감사 로그). plan_decisions 가 소유한다.
_NON_CONDITION = plan_decisions.NON_CONDITION_PLAN_KEYS


def _is_empty(value: Any) -> bool:
    """빈 슬롯 판정(plan_decisions 와 같은 기준 — 선초기화된 빈 컨테이너는 조건이 아니다)."""
    return value is None or value == [] or value == {} or value == "" or value is False


def _canonical(value: Any) -> Any:
    """비교 가능한 정규형으로 접는다. dict 는 키 정렬, set 은 정렬 리스트, 그 외는 그대로."""
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value, key=str) if not str(key).startswith("_")}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        # 리스트는 순서를 보존한다 — behaviors 처럼 순서가 의미를 갖는 슬롯이 있다.
        return [_canonical(item) for item in value]
    return value


def is_condition_plan_key(key: str) -> bool:
    """plan 최상위 키가 조건인가(파생/비조건 목록 어디에도 없으면 조건)."""
    return not (key.startswith("_") or key in DERIVED_PLAN_KEYS or key in _NON_CONDITION or key in CONTAINERS)


def unclassified_plan_keys(plan: dict[str, Any]) -> list[str]:
    """어느 목록에도 선언되지 않아 '조건'으로 흘러든 plan 최상위 키.

    계약 테스트가 쓴다: 새 키가 조용히 스냅샷에 들어오는 것을 막고, 개발자가 조건/파생 중 하나로
    분류하도록 강제한다. 진짜 조건이면 :data:`KNOWN_CONDITION_PLAN_KEYS` 에 등록하면 통과한다.
    """
    return sorted(
        key for key, value in plan.items()
        if isinstance(key, str) and is_condition_plan_key(key) and not _is_empty(value)
        and key not in KNOWN_CONDITION_PLAN_KEYS
    )


# 조건으로 확정 선언된 plan 최상위 키. 새 조건 유형을 열면 여기 한 줄 추가한다(그 행위가 곧
# "이건 파생이 아니라 사용자가 말한 조건이다" 라는 선언이다).
KNOWN_CONDITION_PLAN_KEYS = frozenset({
    "intent",
    "condition_evaluations",
    "result_limit",
    "dimension_filters",
    "computed_metrics",
    "member_metric_selection",
    "member_metric_ranking",
    "set_expressions",
    "semantic_conditions",
    "cart_context",
    "unresolved_source_conditions",
    "logical_expression",
    "analytical_intent",
    "metric_trend",
    "purchase_count_ranking",
    "entity_set",
    "retrieval_scope",
})


def snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    """query_plan → 조건 IR 정규 스냅샷.

    반환 형태::

        {
          "schema_version": 1,
          "plan": {조건 성격의 plan 최상위 슬롯},
          "target_user": {...}, "exclude": {...}, "campaign_constraints": {...},
        }

    빈 컨테이너는 키 자체가 빠진다 — "조건 없음"과 "빈 dict 로 선초기화됨"을 구분하지 않는 것이
    골든 비교에서 잡음을 만들기 때문이다.
    """
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}

    conditions = {
        key: _canonical(value)
        for key, value in sorted(plan.items(), key=lambda item: str(item[0]))
        if isinstance(key, str) and is_condition_plan_key(key) and not _is_empty(value)
    }
    if conditions:
        out["plan"] = conditions

    for container in CONTAINERS:
        holder = plan.get(container)
        if not isinstance(holder, dict):
            continue
        slots = {
            key: _canonical(value)
            for key, value in sorted(holder.items(), key=lambda item: str(item[0]))
            if isinstance(key, str) and not key.startswith("_") and not _is_empty(value)
        }
        if slots:
            out[container] = slots
    return out


def condition_slots(plan: dict[str, Any]) -> set[str]:
    """스냅샷에 남은 조건 슬롯 키 집합(``"target_user.gender"`` 형태). 커버리지 단언용."""
    snap = snapshot(plan)
    keys: set[str] = set()
    for container, slots in snap.items():
        if container == "schema_version" or not isinstance(slots, dict):
            continue
        keys.update(f"{container}.{slot}" for slot in slots)
    return keys
