"""타겟 조건 도메인 IR(중간 표현) + 중앙 조건 레지스트리.

배경: 조건 유형을 하나 추가할 때마다 신호 감지(_has_member_target_signal 한 줄), 빌더 defer 목록,
confidence 조건 수집/라벨까지 서로 다른 곳에 손배선해야 했다 — Builder 가 도메인 의미·정책·실행 계획의
책임까지 떠안고 있었기 때문이다. 이 모듈은 그 도메인 지식을 Builder 앞의 별도 계층으로 올린다:

  ConditionSpec  — 조건 유형 하나의 도메인 선언(어디서 추출하나, 어떤 팩트가 필요한가, 신호로 치나,
                   confidence 에 어떻게 보이나). CONDITION_SPECS 가 단일 소스.
  TargetCondition — query_plan 에서 추출된 조건 인스턴스(IR 노드). extract_target_conditions 가 생성.

파생 규칙(소비자는 이 IR 만 보면 된다):
  - 신호 감지: spec.signals_target 인 조건이 하나라도 추출되면 실추출 가능 신호
    (graph_rag._has_member_target_signal 이 회원속성 컴파일 신호와 OR).
  - 빌더 defer: spec.fact_join 인 조건은 전용 팩트조인 빌더가 소유한다 — EXISTS-류 범용 빌더는
    이런 조건이 있으면 양보한다(graph_rag 캠페인 반응 빌더 등).
  - 빌더 소유권: fact_join 조건 kind ↔ 빌더 매핑은 graph_rag._sql_target_builder_registry 가 선언하고,
    테스트가 '모든 fact_join kind 는 정확히 하나의 빌더가 소유한다'를 강제한다(죽은 레지스트리 방지).
  - confidence: spec.confidence 메타로 조건 수집/한글 라벨이 자동 파생된다(confidence._extract_conditions).

순환 방지: 이 모듈은 graph_rag/confidence 를 import 하지 않는다(plain dict 입력). 설정 의존 값
(주문횟수 행동 집합)은 호출자가 order_count_behaviors 로 주입한다 — 기본값은 코드 상수와 동일.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 주문 횟수 행동(집계 기준은 member_target_filters.json 의 order_count_targets.behaviors 가 소유).
# graph_rag 는 로드된 설정 키 집합을 주입하고, 이 기본값은 설정 부재 시 폴백이다.
DEFAULT_ORDER_COUNT_BEHAVIORS = frozenset({"first_purchase", "repeat_buyer", "no_purchase"})
CART_BEHAVIOR = "cart_abandoner"

# 행동 한글 라벨(도메인 어휘 — confidence 가 재사용).
BEHAVIOR_KO = {
    "no_purchase": "구매 이력 없음(무구매)",
    "first_purchase": "첫 구매",
    "repeat_buyer": "재구매(2회 이상)",
    "cart_abandoner": "장바구니 이탈",
}


@dataclass(frozen=True)
class ConfidenceMeta:
    """조건이 confidence 리포트에 어떻게 나타나는지(수집 키/값/한글 라벨/채점 kind)."""

    kind: str  # confidence._score_condition 의 채점 분기 kind
    category: str
    key: Callable[[dict[str, Any]], str]
    value: Callable[[dict[str, Any]], Any]
    ko: Callable[[dict[str, Any]], str]
    # 파라미터가 채점 가능한 꼴인지(현행 confidence 의 isinstance 가드 보존). False 면 수집하지 않는다.
    applies: Callable[[dict[str, Any]], bool] = lambda params: True


@dataclass(frozen=True)
class ConditionSpec:
    """조건 유형 하나의 도메인 선언(단일 소스). 추출/팩트/신호/confidence 파생이 전부 여기서 나온다."""

    kind: str
    fact: str  # 필요한 팩트 계열: member | order | cart | campaign | region
    fact_join: bool  # 전용 팩트조인 빌더가 소유(EXISTS-류 범용 빌더는 양보)
    signals_target: bool  # 실추출 가능 타겟 신호로 인정(intent 승격/필수조건 검증)
    extract: Callable[[dict[str, Any], frozenset[str]], list[dict[str, Any]]]
    confidence: ConfidenceMeta | None = None


@dataclass(frozen=True)
class TargetCondition:
    """query_plan 에서 추출된 조건 인스턴스(IR 노드)."""

    kind: str
    params: dict[str, Any]
    spec: ConditionSpec


def _tu(plan: dict[str, Any]) -> dict[str, Any]:
    target_user = plan.get("target_user")
    return target_user if isinstance(target_user, dict) else {}


def _tu_dict(name: str) -> Callable[[dict[str, Any], frozenset[str]], list[dict[str, Any]]]:
    """target_user.<name> 이 dict 면 그것을 파라미터로 추출(현행 isinstance dict 신호 판정과 동일)."""

    def extract(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
        value = _tu(plan).get(name)
        return [value] if isinstance(value, dict) else []

    return extract


def _plan_dict(name: str) -> Callable[[dict[str, Any], frozenset[str]], list[dict[str, Any]]]:
    """plan 최상위 <name> 이 dict 면 추출(밀집지역/랭킹류)."""

    def extract(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
        value = plan.get(name)
        return [value] if isinstance(value, dict) else []

    return extract


def _extract_signup(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    tu = _tu(plan)
    signup = tu.get("signup_target")
    if isinstance(signup, dict):
        return [{"days": signup.get("days")}]
    if "new_user" in (tu.get("lifecycle") or []):
        return [{"days": None}]
    return []


def _extract_order_count_behaviors(plan: dict[str, Any], behaviors_ctx: frozenset[str]) -> list[dict[str, Any]]:
    return [{"behavior": b} for b in _tu(plan).get("behaviors", []) if b in behaviors_ctx]


def _extract_cart_abandoner(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    return [{"behavior": CART_BEHAVIOR}] if CART_BEHAVIOR in _tu(plan).get("behaviors", []) else []


def _extract_unclassified_behaviors(plan: dict[str, Any], behaviors_ctx: frozenset[str]) -> list[dict[str, Any]]:
    """지원 집합 밖의 행동(예: office_worker). 신호로 치지 않지만(현행 intent 미승격 보존)
    팩트조인 필요 조건으로는 남겨 EXISTS-류 빌더가 조용히 삼키지 않게 한다(현행 defer 보존)."""
    return [
        {"behavior": b}
        for b in _tu(plan).get("behaviors", [])
        if b not in behaviors_ctx and b != CART_BEHAVIOR
    ]


def _extract_purchase_object(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    tu = _tu(plan)
    value = tu.get("purchase_object")
    if isinstance(value, str) and value:
        return [{"value": value, "object_kind": tu.get("purchase_object_kind")}]
    return []


def _extract_campaign_responses(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    responses = _tu(plan).get("campaign_responses")
    return [{"responses": responses}] if isinstance(responses, list) and responses else []


def _extract_aggregate_conditions(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    conditions = _tu(plan).get("aggregate_conditions")
    return [{"conditions": conditions}] if isinstance(conditions, list) and conditions else []


def _cart_retention_ko(params: dict[str, Any]) -> str:
    days = params.get("min_days") or params.get("max_days")
    direction = "이상" if params.get("min_days") else "이내"
    return params.get("label") or f"장바구니 보관 {days}일 {direction}"


# 조건 레지스트리(단일 소스). 순서는 confidence 조건 목록 표시 순서를 겸한다(기존 수작업 순서 보존:
# 가입 → 생일 → 최근로그인 → 미구매창 → 캠페인반응횟수 → 카트보관/유형 → 행동 → 상품/날짜 구매이력).
CONDITION_SPECS: tuple[ConditionSpec, ...] = (
    # ── 회원 술어 계열(compile_member_target_conditions 가 컴파일; 신호도 그쪽 has_signal 이 담당) ──
    ConditionSpec(
        kind="signup_target", fact="member", fact_join=False, signals_target=False,
        extract=_extract_signup,
        confidence=ConfidenceMeta(
            kind="signup", category="date",
            key=lambda p: "signup_target", value=lambda p: p.get("days"),
            ko=lambda p: f"최근 {p.get('days') or 90}일 이내 가입",
        ),
    ),
    ConditionSpec(
        kind="birthday_target", fact="member", fact_join=False, signals_target=True,
        extract=_tu_dict("birthday_target"),
        confidence=ConfidenceMeta(
            kind="birthday", category="date",
            key=lambda p: "birthday_target",
            value=lambda p: "이달" if p.get("granularity") == "month" else "오늘",
            ko=lambda p: f"생일 {'이달' if p.get('granularity') == 'month' else '오늘'}",
        ),
    ),
    ConditionSpec(
        kind="recent_login", fact="member", fact_join=False, signals_target=False,
        extract=_tu_dict("recent_login"),
        confidence=ConfidenceMeta(
            kind="recent_login", category="date",
            key=lambda p: "recent_login", value=lambda p: p.get("min_days"),
            ko=lambda p: f"최근 {p.get('min_days')}일 이내 로그인",
            applies=lambda p: isinstance(p.get("min_days"), int),
        ),
    ),
    # ── 주문 팩트 계열 ──
    ConditionSpec(
        kind="purchase_inactivity", fact="order", fact_join=True, signals_target=True,
        extract=_tu_dict("purchase_inactivity"),
        confidence=ConfidenceMeta(
            kind="order_window", category="date",
            key=lambda p: "purchase_inactivity", value=lambda p: p.get("min_days"),
            ko=lambda p: f"최근 {p.get('min_days')}일 미구매",
            applies=lambda p: isinstance(p.get("min_days"), int),
        ),
    ),
    # ── 캠페인 반응 팩트 계열 ──
    ConditionSpec(
        kind="campaign_response_frequency", fact="campaign", fact_join=True, signals_target=True,
        extract=_tu_dict("campaign_response_frequency"),
        confidence=ConfidenceMeta(
            kind="campaign_response", category="behavior",
            key=lambda p: "campaign_response_frequency", value=lambda p: p.get("count"),
            ko=lambda p: p.get("label") or f"캠페인 {p.get('count')}회 이상 반응",
            applies=lambda p: isinstance(p.get("count"), int),
        ),
    ),
    # 캠페인 반응(EXISTS ≥1회)은 회원키 EXISTS 술어로 어느 빌더에나 AND 결합 가능 → fact_join 아님.
    ConditionSpec(
        kind="campaign_responses", fact="campaign", fact_join=False, signals_target=True,
        extract=_extract_campaign_responses,
    ),
    # ── 장바구니 팩트 계열 ──
    ConditionSpec(
        kind="cart_retention", fact="cart", fact_join=True, signals_target=True,
        extract=_tu_dict("cart_retention"),
        confidence=ConfidenceMeta(
            kind="cart", category="date",
            key=lambda p: "cart_retention",
            value=lambda p: p.get("min_days") or p.get("max_days"),
            ko=_cart_retention_ko,
            applies=lambda p: bool(p.get("min_days") or p.get("max_days")),
        ),
    ),
    ConditionSpec(
        kind="cart_type", fact="cart", fact_join=True, signals_target=True,
        extract=_tu_dict("cart_type"),
        confidence=ConfidenceMeta(
            kind="cart", category="cart_type",
            key=lambda p: "cart_type", value=lambda p: p.get("value"),
            ko=lambda p: p.get("label") or p.get("value"),
            applies=lambda p: bool(p.get("value")),
        ),
    ),
    ConditionSpec(
        kind="cart_aggregate", fact="cart", fact_join=True, signals_target=True,
        extract=_tu_dict("cart_aggregate"),
    ),
    # ── 행동(behaviors 리스트에서 분류) ──
    ConditionSpec(
        kind="order_count_behavior", fact="order", fact_join=True, signals_target=True,
        extract=_extract_order_count_behaviors,
        confidence=ConfidenceMeta(
            kind="order_count", category="behavior",
            key=lambda p: p["behavior"], value=lambda p: p["behavior"],
            ko=lambda p: BEHAVIOR_KO.get(p["behavior"], p["behavior"]),
        ),
    ),
    ConditionSpec(
        kind="cart_abandoner", fact="cart", fact_join=True, signals_target=True,
        extract=_extract_cart_abandoner,
        confidence=ConfidenceMeta(
            kind="cart", category="behavior",
            key=lambda p: CART_BEHAVIOR, value=lambda p: CART_BEHAVIOR,
            ko=lambda p: BEHAVIOR_KO[CART_BEHAVIOR],
        ),
    ),
    ConditionSpec(
        kind="unclassified_behavior", fact="order", fact_join=True, signals_target=False,
        extract=_extract_unclassified_behaviors,
    ),
    # ── 구매 이력(상품/날짜) — purchase_history 빌더 소유 ──
    ConditionSpec(
        kind="purchase_object", fact="order", fact_join=True, signals_target=True,
        extract=_extract_purchase_object,
        confidence=ConfidenceMeta(
            kind="free_text", category="purchase",
            key=lambda p: "purchase_object", value=lambda p: p["value"],
            ko=lambda p: ("브랜드 구매 이력" if p.get("object_kind") == "brand" else "상품 구매 이력")
            + f": '{p['value']}'",
        ),
    ),
    ConditionSpec(
        kind="purchase_date", fact="order", fact_join=True, signals_target=True,
        extract=_tu_dict("purchase_date"),
        confidence=ConfidenceMeta(
            kind="order_window", category="date",
            key=lambda p: "purchase_date", value=lambda p: f"{p.get('from')}~{p.get('to')}",
            ko=lambda p: p.get("label") or f"구매 날짜 {p.get('from')}~{p.get('to')}",
            applies=lambda p: bool(p.get("from") and p.get("to")),
        ),
    ),
    # ── 집계/랭킹/지역(plan 최상위) ──
    ConditionSpec(
        kind="aggregate_conditions", fact="order", fact_join=True, signals_target=True,
        extract=_extract_aggregate_conditions,
    ),
    ConditionSpec(
        kind="purchase_count_ranking", fact="order", fact_join=True, signals_target=True,
        extract=_plan_dict("purchase_count_ranking"),
    ),
    ConditionSpec(
        kind="member_metric_ranking", fact="order", fact_join=True, signals_target=True,
        extract=_plan_dict("member_metric_ranking"),
    ),
    ConditionSpec(
        kind="region_density_target", fact="region", fact_join=True, signals_target=True,
        extract=_plan_dict("region_density_target"),
    ),
)

_SPECS_BY_KIND = {spec.kind: spec for spec in CONDITION_SPECS}
assert len(_SPECS_BY_KIND) == len(CONDITION_SPECS), "CONDITION_SPECS kind 중복"


def extract_target_conditions(
    query_plan: dict[str, Any],
    *,
    order_count_behaviors: frozenset[str] = DEFAULT_ORDER_COUNT_BEHAVIORS,
) -> list[TargetCondition]:
    """query_plan → 타겟 조건 IR 목록. 레지스트리 선언 순서대로 추출한다.

    order_count_behaviors: 주문횟수 집계로 컴파일되는 행동 집합(설정 소유 — graph_rag 가
    order_count_targets.behaviors 키 집합을 주입). 이 집합/cart_abandoner 밖의 행동은
    unclassified_behavior 로 분류된다."""
    conditions: list[TargetCondition] = []
    for spec in CONDITION_SPECS:
        for params in spec.extract(query_plan, order_count_behaviors):
            conditions.append(TargetCondition(kind=spec.kind, params=params, spec=spec))
    return conditions


def fact_join_kinds() -> frozenset[str]:
    """전용 팩트조인 빌더 소유가 필요한 조건 kind 집합(소유권 불변식 검증용)."""
    return frozenset(spec.kind for spec in CONDITION_SPECS if spec.fact_join)
