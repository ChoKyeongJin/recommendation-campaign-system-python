"""plan 최상위 키의 분류를 한 곳에서 선언하는 레지스트리.

배경: plan dict 는 이 시스템의 사실상 IR 인데, "어떤 키가 사용자가 말한 조건이고 어떤 키가
파서·검증이 만든 파생물인가"를 최소 네 곳이 각자의 목록으로 들고 있었다 —
ir_snapshot(DERIVED / KNOWN_CONDITION), plan_decisions(NON_CONDITION), semantic_requirements(요구 슬롯).
한 곳만 고치면 같은 키가 곳에 따라 조건이었다가 파생이 되고, 그 결과 조건이 스냅샷·감사·요구
원장에서 조용히 빠진다. 실제로 policy_constraints 가 '의미 슬롯이자 파생'인 모순 상태였고,
코퍼스가 만들지 않는 키 13종은 어느 목록에도 없었다.

이 모듈은 그 분류의 **단일 소유자**다. 소비자는 각자 목록을 들지 않고 여기서 파생한다.

순수 모듈 규약: graph_rag 를 import 하지 않는다. 분류는 도메인 사실이지 실행 지식이 아니다.

분류 기준(경계가 애매할 때 이걸로 판단한다):
  CONDITION  — 사용자가 말한 것. 표면 어구가 바뀌면 값도 바뀐다.
               (브랜드명→코드처럼 카탈로그로 실체화되더라도 촉발이 사용자면 조건이다)
  DERIVED    — 파서·검증·라우팅이 만든 산출물. 사용자가 말한 적 없다.
  NON_CONDITION — 원문·계측·감사 로그처럼 애초에 IR 이 아닌 것.
"""

from __future__ import annotations

from dataclasses import dataclass


CONDITION = "condition"
DERIVED = "derived"
NON_CONDITION = "non_condition"

KINDS = frozenset({CONDITION, DERIVED, NON_CONDITION})


@dataclass(frozen=True)
class PlanKey:
    """plan 최상위 키 하나의 분류 선언."""

    name: str
    kind: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: 알 수 없는 분류 {self.kind!r}")


def _keys(kind: str, entries: dict[str, str]) -> tuple[PlanKey, ...]:
    return tuple(PlanKey(name=name, kind=kind, note=note) for name, note in entries.items())


# 사용자가 말한 조건. 새 조건 유형을 열면 여기 한 줄 추가하는 것이 곧 "이건 파생이 아니라
# 사용자가 말한 조건이다" 라는 선언이다.
_CONDITIONS = _keys(CONDITION, {
    "intent": "질의 의도",
    "condition_evaluations": "조건 판정 grain 이 분리된 무손실 실행 IR",
    "result_limit": "상위 N 건 제한",
    "dimension_filters": "디멘션 값 조건(브랜드명→코드)",
    "compound_dimension_filters": "복합 디멘션 조건",
    "external_conditions": "외부 의존 조건(날씨 등)",
    "computed_metrics": "계산 지표 조건",
    "member_metric_selection": "회원 지표 선택",
    "member_metric_ranking": "회원 지표 랭킹",
    "set_expressions": "집합식",
    "semantic_conditions": "의미 조건",
    "cart_context": "장바구니 문맥",
    "unresolved_source_conditions": "해소되지 않은 원문 조건(fail-close 근거)",
    "logical_expression": "OR-of-conjunctions 논리식",
    "analytical_intent": "분석 의도",
    "metric_trend": "기간 대비 지표 증감",
    "purchase_count_ranking": "구매 건수 랭킹",
    "entity_set": "지정 엔터티 집합",
    "retrieval_scope": "검색 범위",
    "event_expression": "사건 논리식 IR",
    "canonical_targeting_expression": "정규 타겟팅 표현식",
    "union_condition": "합집합 타겟(A 이거나 B)",
    "group_ranking_target": "그룹별 상위 N",
    "region_density_target": "회원 밀집 지역",
    "aggregation_request": "사용자가 요청한 집계('회원수를 세어줘')",
    "policy_constraints": "업무 정책으로 실체화되지만 촉발은 사용자 어구",
})

# 파서·검증·라우팅 산출물. 사용자가 말한 적 없다.
_DERIVED = _keys(DERIVED, {
    "capability_check": "능력 점검 결과",
    "detected_intent": "입력 조건에서 계산된 라우팅 힌트",
    "member_policy": "회원 정책 판정",
    "member_scope": "'전체 회원 대상'인가",
    "output_contract": "출력 계약(컬럼/그레인)",
    "selected_route": "선택된 라우트",
    "semantic_resolutions": "의미 해소 흔적",
    "source_requirements": "소스 요구 봉인 원장(다이제스트와 쌍)",
    "semantic_evidence": "V3 의미 슬롯의 원문 근거",
    "external_condition_results": "Resolver 감사 스냅샷",
    "external_condition_resolution": "외부 의존성 처리 요약",
    "conceptual_resolutions": "상식 grounding 영수증",
    "conceptual_targeting_resolution": "상식 grounding 실행 요약",
    "canonical_projection": "정규 표현식 투영 영수증",
    "canonical_targeting_validation": "소유권 불변식 검증 결과",
    "canonical_targeting_version": "정규 표현식 버전",
    "condition_claims": "조건 소유 선언 목록",
    "event_compiler_capability": "사건 컴파일러 능력 판정",
    "event_semantic_validation": "사건 의미 검증 결과",
    "ownership_reconciliation_complete": "소유권 재조정 완료 표시",
    "aggregation_request_validation": "집계 요청 해석의 검증 영수증",
    "combine_mode": "조건 결합 방식(실행 힌트)",
    "unsupported": "미지원 판정 목록",
    "unmatched_source_conditions": "슬롯에 못 담은 원문 항목",
    "validation_errors": "플랜 검증 오류",
    "semantic_ir": "LLM 소유 의미 연산 계층",
    "semantic_ir_reconciliation": "그 계층과 실행 플랜의 대조 결과",
    "literal_bindings": "원문 값 원자 봉인",
})

ALL: tuple[PlanKey, ...] = _CONDITIONS + _DERIVED

_BY_NAME = {key.name: key for key in ALL}
assert len(_BY_NAME) == len(ALL), "plan_schema 에 중복 키 선언이 있다."


def kind_of(name: str) -> str | None:
    """분류(미선언이면 None)."""
    key = _BY_NAME.get(name)
    return key.kind if key is not None else None


def names(kind: str) -> frozenset[str]:
    """분류별 키 이름 집합 — 소비자는 자기 목록을 들지 말고 이걸 쓴다."""
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 분류: {kind!r}")
    return frozenset(key.name for key in ALL if key.kind == kind)


def is_condition(name: str) -> bool:
    return kind_of(name) == CONDITION


__all__ = [
    "ALL",
    "CONDITION",
    "DERIVED",
    "KINDS",
    "NON_CONDITION",
    "PlanKey",
    "is_condition",
    "kind_of",
    "names",
]
