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
    # 이 키가 **오디언스(회원 집합)를 정하는 언어**인가. canonical Event IR 이 오디언스를
    # 소유할 때 이런 키가 함께 채워져 있으면 같은 요청에 해석이 둘이라는 뜻이다.
    # DERIVED 키는 정의상 오디언스 언어가 아니므로 None(=해당 없음)으로 둔다.
    audience: bool | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: 알 수 없는 분류 {self.kind!r}")
        if self.kind != CONDITION and self.audience is not None:
            raise ValueError(
                f"{self.name}: audience 는 CONDITION 키에만 선언한다"
                " (파생물은 오디언스 언어가 아니다)"
            )
        if self.kind == CONDITION and self.audience is None:
            raise ValueError(
                f"{self.name}: CONDITION 키는 audience 를 명시해야 한다"
                " — 빠뜨리면 그 키가 오디언스 표면에서 조용히 빠진다"
            )


# 오디언스 조건을 담는 **컨테이너**(최상위 키가 아니라 하위 슬롯 묶음). 키 분류와 축이 다르므로
# 별도 상수다. 원래 legacy_audience_migration 이 소유했고, 이제 그 모듈이 여기서 재수출한다 —
# 두 벌로 적으면 슬롯이 하나 늘어나는 날 한쪽만 갱신되고 그 순간 판정이 갈라진다.
AUDIENCE_CONTAINERS: tuple[str, ...] = ("target_user", "exclude")


def _keys(kind: str, entries: dict[str, str] | dict[str, tuple[str, bool]]) -> tuple[PlanKey, ...]:
    keys: list[PlanKey] = []
    for name, value in entries.items():
        note, audience = value if isinstance(value, tuple) else (value, None)
        keys.append(PlanKey(name=name, kind=kind, note=note, audience=audience))
    return tuple(keys)


# 사용자가 말한 조건. 새 조건 유형을 열면 여기 한 줄 추가하는 것이 곧 "이건 파생이 아니라
# 사용자가 말한 조건이다" 라는 선언이다.
#
# 두 번째 값(audience)의 뜻: **오늘 게이트가 "두 번째 오디언스 언어"로 세는 키인가.**
# 의미상 오디언스에 가까운 키가 더 있지만(`dimension_filters`, `member_metric_ranking`,
# `union_condition`, `entity_set` …) 표면을 넓히는 것은 열린 결정이다
# (docs/plans_event_ir_only.md §6-1) — 넓히는 순간 그 키를 가진 canonical 요청이 fail-close 되고,
# 그 축은 Event IR 로 표현할 수도 없어서 기능이 그대로 줄어든다. 그래서 지금의 True 집합은
# **현행 게이트 표면과 정확히 같다**. 넓히려면 여기 한 줄을 바꾸는 것이 곧 그 결정의 기록이다.
_CONDITIONS = _keys(CONDITION, {
    "intent": ("질의 의도", False),
    "condition_evaluations": ("조건 판정 grain 이 분리된 무손실 실행 IR", True),
    "result_limit": ("상위 N 건 제한", False),
    "dimension_filters": ("디멘션 값 조건(브랜드명→코드)", False),
    "compound_dimension_filters": ("복합 디멘션 조건", True),
    "external_conditions": ("외부 의존 조건(날씨 등)", True),
    "computed_metrics": ("계산 지표 조건", True),
    "member_metric_selection": ("회원 지표 선택", False),
    "member_metric_ranking": ("회원 지표 랭킹", False),
    "set_expressions": ("집합식", True),
    "semantic_conditions": ("의미 조건", False),
    "cart_context": ("장바구니 문맥", False),
    "unresolved_source_conditions": (
        # Event IR 빌더 자신이 쓰는 좌표다. True 로 두면 canonical 실패가 자기참조로
        # 또 하나의 오디언스 언어가 되어 사유가 뒤바뀐다.
        "해소되지 않은 원문 조건(fail-close 근거)",
        False,
    ),
    "analytical_intent": ("분석 의도", False),
    "metric_trend": ("기간 대비 지표 증감", False),
    "purchase_count_ranking": ("구매 건수 랭킹", False),
    "entity_set": ("지정 엔터티 집합", False),
    "retrieval_scope": ("검색 범위", False),
    "audience_requirement": ("근거와 이슈를 포함한 canonical 오디언스 요구 계약", False),
    "event_expression": ("사건 논리식 IR", False),
    "canonical_targeting_expression": ("정규 타겟팅 표현식", False),
    "union_condition": ("합집합 타겟(A 이거나 B)", False),
    "group_ranking_target": ("그룹별 상위 N", False),
    "region_density_target": ("회원 밀집 지역", False),
    "aggregation_request": ("사용자가 요청한 집계('회원수를 세어줘')", False),
    "policy_constraints": ("업무 정책으로 실체화되지만 촉발은 사용자 어구", False),
    # ``relational_operations``(검증된 속성 시점·이력 연산)는 2026-08-05 폐기됐다. 등급/상태
    # 이력·전이 축은 SemanticPlanV2 relation_predicate 노드로만 생산됐고, 그 노드가 사라지면서
    # 생산자도 컴파일러도 남지 않았다. 생산자 없는 조건 키는 거짓 광고다.
    # ``semantic_plan``(SemanticPlanV2 의미 노드 집합)은 2026-08-05 폐기됐다. 오디언스 의미의
    # 노출면은 ``audience_requirement`` 하나이고, 그 노드를 슬롯으로 컴파일하는 실행 경로가
    # 남아 있지 않았다. 생산자가 없는 키를 조건으로 선언해 두면 "두 번째 해석 계층이 아직
    # 계약에 있다"는 거짓 광고가 된다.
})

# 파서·검증·라우팅 산출물. 사용자가 말한 적 없다.
_DERIVED = _keys(DERIVED, {
    # 오디언스를 실행하는 계층. 사용자가 말한 조건이 아니므로 파생이다. 2026-08-07 legacy 레인
    # 폐쇄로 성립하는 값은 ``event_ir`` 하나이고, 이 키를 계속 읽는 이유는 저장된 ``legacy``
    # 표식을 조용히 삼키지 않고 명명된 실패로 드러내기 위해서다(audience_authority 참조).
    "audience_authority": "오디언스 실행 권위(event_ir 하나 — legacy 레인은 폐쇄)",
    "capability_check": "능력 점검 결과(plan 생산자 없음 — 응답 조립이 capability_validation 요약으로 파생)",
    "detected_intent": "입력 조건에서 계산된 라우팅 힌트",
    "member_policy": "회원 정책 판정",
    "member_scope": "'전체 회원 대상'인가",
    "output_contract": "출력 계약(컬럼/그레인)",
    "selected_route": "선택된 라우트",
    "semantic_resolutions": "의미 해소 흔적",
    "source_requirements": "소스 요구 봉인 원장(다이제스트와 쌍)",
    "semantic_evidence": "V4 의미 슬롯의 원문 근거",
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
    "semantic_ir": "의미 판정 투영(애플리케이션 소유 — status/missing/unsupported)",
    # ``semantic_pipeline``(파이프라인 감사 영수증)과 ``requirements``(요구 원장)는 2026-08-05
    # 폐기됐다. 둘 다 SemanticPlan 컴파일 산출물이었고, 그 컴파일 경로가 사라지면서 생산자가
    # 없어졌다. 파생 키로 남겨 두면 스냅샷·감사가 "있다가 비었다"를 계속 구분하려 든다.
    "semantic_ir_reconciliation": "그 계층과 실행 플랜의 대조 결과",
    "literal_bindings": "원문 값 원자 봉인",
    # ── 의미 완전성 계층(2026-08-06) ─────────────────────────────────────────────
    # 넷 다 '해석의 결과'다. 사용자가 말한 적 없고, 조건이 바뀌면 따라 바뀐다.
    "policy_decisions": "정책 결정 영수증(policy_id/version/decision/reason_code)",
    "semantic_requirements_ledger": "의미 요구사항 원장(요구별 terminal disposition)",
    "result_shape": "요청된 결과 형태(entity_list | scalar | grouped_table …)",
    "compile_outcomes": "컴파일 단계별 총체적 귀결(candidate | unsupported | failure)",
    "semantic_fingerprint": "의미 지문(같은 뜻이면 같은 값)",
    "execution_fingerprint": "실행 지문(모델·스키마·정책·기준시각)",
    # ``relational_ir``(속성 이력 조건의 차단 판정)은 2026-08-05 폐기됐다 — 그 리졸버가 사라졌다.
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


def audience_keys() -> frozenset[str]:
    """두 번째 오디언스 언어로 세는 **최상위 키** 집합.

    컨테이너(:data:`AUDIENCE_CONTAINERS`)와 축이 다르므로 합치지 않는다 — 컨테이너는 하위
    슬롯 묶음이고 이쪽은 키 자체가 조건이다. 소비자는 필요한 쪽을 골라 쓰거나 둘 다 쓴다.
    """
    return frozenset(key.name for key in ALL if key.audience)


__all__ = [
    "ALL",
    "AUDIENCE_CONTAINERS",
    "CONDITION",
    "DERIVED",
    "KINDS",
    "NON_CONDITION",
    "PlanKey",
    "audience_keys",
    "is_condition",
    "kind_of",
    "names",
]
