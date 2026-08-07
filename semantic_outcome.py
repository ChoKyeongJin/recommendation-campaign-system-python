"""결핍의 **원인**과 실패의 **성격** — 실패 진단 계층이 공유하는 닫힌 어휘.

원래 SemanticPlanV2(`semantic_plan`)가 소유했다. 그 중간표현은 2026-08-05 폐기됐지만 이
어휘는 canonical Event IR 레인이 그대로 쓴다: "왜 이 조건이 비었는가"(원인)와 "그래서
누구에게 무엇을 물어야 하는가"(성격)는 오디언스를 어떤 IR 로 적든 필요한 구분이기 때문이다.

원인을 구분하지 않으면 결핍이 전부 사용자 확인 요청이 된다. 사용자는 `member_entity` 를
정해 줄 수 없다 — 그건 우리 쪽 사고이지 사용자의 정보 누락이 아니다.

이 파일은 리터럴 선언만 갖는다(stdlib 조차 import 하지 않는다). 실패 진단은 응답 조립·
트레이스·실패로그에서 무조건 호출되므로 어떤 스키마 로딩에도 묶이면 안 된다
(`tests/test_module_layering.LEAF_DIAGNOSTIC_MODULES` 가 이것을 계약으로 잰다).

**문자열 값은 바꾸지 않는다** — `rag/failure_stage.py` 와 `audience_failure.py` 가 이 값을
키로 들고 있고, 저장된 실패로그도 이 값으로 조회된다.
"""

from __future__ import annotations


# ── 결핍의 원인(닫힌 집합) ──────────────────────────────────────────────────────
CAUSE_USER_OMISSION = "user_omission"      # 원문에 정말 없다 → 물어봐야 한다
CAUSE_MODEL_OMISSION = "model_omission"    # 구간은 청구해 놓고 못 채웠다 → 재방출
CAUSE_TYPE_MISMATCH = "type_mismatch"      # 타입/필드가 어긋났다 → 재분류 또는 재방출
CAUSE_REGISTRY_GAP = "registry_gap"        # 실행 어휘가 비어 있다 → 설정/컴파일러 결함

MISSING_CAUSES: frozenset[str] = frozenset({
    CAUSE_USER_OMISSION,
    CAUSE_MODEL_OMISSION,
    CAUSE_TYPE_MISMATCH,
    CAUSE_REGISTRY_GAP,
})


# ── 원인 → 실패의 성격 ──────────────────────────────────────────────────────────
# 사용자 노출 문구와 failure_reason 이 이 값으로 갈린다.
FAILURE_KIND_USER = "user_clarification"
FAILURE_KIND_STRUCTURER = "structurer_failure"
FAILURE_KIND_SYSTEM = "system_failure"
FAILURE_KIND_UNSUPPORTED = "unsupported"

FAILURE_KIND_BY_CAUSE: dict[str, str] = {
    CAUSE_USER_OMISSION: FAILURE_KIND_USER,
    CAUSE_MODEL_OMISSION: FAILURE_KIND_STRUCTURER,
    CAUSE_TYPE_MISMATCH: FAILURE_KIND_STRUCTURER,
    CAUSE_REGISTRY_GAP: FAILURE_KIND_SYSTEM,
}


# ── system_failure 안의 실패 사유(닫힌 집합) ────────────────────────────────────
# 한 kind 안에 고칠 곳이 서로 다른 실패가 셋 들어 있다. 이 구별은 **판정한 계층만** 알 수
# 있으므로 사유는 선언되는 값이지 파생되는 값이 아니다.
#
# 예전에는 사유를 적지 않은 system_failure 를 전부 registry gap 으로 파생했고, 그래서 실제로
# 낮출 수 있는 요구까지 '실행 설정이 준비되지 않았다'로 종결됐다(실측 2026-08-07). 레지스트리
# 불일치는 자산 목록을 대조해 본 경로만 주장할 수 있는 사실이다.
FAILURE_REASON_REGISTRY_GAP = "semantic_registry_gap"    # 실행 자산이 그 축을 못 낸다 → 설정
FAILURE_REASON_EMISSION = "semantic_emission_failure"    # 낼 수 있는데 표현이 안 나왔다 → 방출
FAILURE_REASON_SYSTEM = "semantic_system_failure"        # 그 외 내부 실패(선언 없음의 중립 귀결)

SYSTEM_FAILURE_REASONS: frozenset[str] = frozenset({
    FAILURE_REASON_REGISTRY_GAP,
    FAILURE_REASON_EMISSION,
    FAILURE_REASON_SYSTEM,
})


__all__ = [
    "CAUSE_MODEL_OMISSION",
    "CAUSE_REGISTRY_GAP",
    "CAUSE_TYPE_MISMATCH",
    "CAUSE_USER_OMISSION",
    "FAILURE_KIND_BY_CAUSE",
    "FAILURE_KIND_STRUCTURER",
    "FAILURE_KIND_SYSTEM",
    "FAILURE_KIND_UNSUPPORTED",
    "FAILURE_KIND_USER",
    "FAILURE_REASON_EMISSION",
    "FAILURE_REASON_REGISTRY_GAP",
    "FAILURE_REASON_SYSTEM",
    "MISSING_CAUSES",
    "SYSTEM_FAILURE_REASONS",
]
