"""잉여 행동 라벨 강등 — 집계 조건이 논리적으로 함의하는 주문 횟수 행동을 회수한다.

배경: LLM 구조화기는 '최근 6개월 주문 건수 5건 이상'을 aggregate_conditions(order_count>=5)로
정확히 구조화하면서, 같은 어구(또는 캠페인명 '재구매 캠페인')를 한 번 더 읽어
behaviors=['repeat_buyer'] 를 덧붙이곤 한다. 두 슬롯은 같은 물리 집계(COUNT(DISTINCT ORDER_ID)
on CRM_SL_ORDERHEADERMALL)를 가리키므로 조건이 늘어난 것이 아니라 **같은 조건의 이중 표현**이다.
그런데 빌더 레지스트리에서 order_count 빌더가 aggregate 빌더보다 먼저라 잉여 행동이 집계 조건을
가로채 dropped 로 남기고 fail-close reject 를 유발했고, 폴백 후보는 behaviors 파생 필수 커버리지
조건(리터럴 문자열 매칭) 때문에 사망했다(2026-08-01 'VIP 전환' 프롬프트 실사고).

강등은 '조건 제거'가 아니라 **소유권 이동**이다 — aggregate_conditions 가 그 의미를 계속 소유한다.
그래서 pop 이 아니라 slot_ownership.claim_slot 으로 회수해 superseded_conditions 와 decisions 에
근거가 남는다. 행동↔지표 등가는 코드가 아니라 설정(member_target_filters.json 의
behaviors.*.metric_id)이 단일 소유한다.

강등 조건(전부 만족해야 한다 — 하나라도 불확실하면 강등하지 않는다. 조건을 남기면 최악이
reject 지만, 잘못 강등하면 조용히 틀린 오디언스가 출고된다):
  1. 행동 선언에 metric_id 등가가 있다. anti_join 행동(no_purchase)은 부재 조건이라 대상이
     아니다 — 집계 임계값으로 오인해 강등하면 극성이 반전된다.
  2. 같은 metric_id 의 집계 조건이 있고 grain 이 회원 단위다(aggregation_scope 부재 또는
     per_member — per_brand 등 다른 grain 의 임계값은 회원 총량을 함의하지 않는다).
  3. 집계의 (operator, threshold) 가 행동의 (operator, count) 를 논리적으로 함의한다.
     '>=' 행동은 기간 창이 함의를 깨지 않는다 — 부분구간 카운트는 평생 카운트 이하이므로
     'N일 내 5건 이상'이면 평생 2건 이상은 반드시 참이다. 반대로 '=' 행동(first_purchase)은
     창이 있으면 함의가 깨진다('창 안에서 1건'이어도 평생 1건이 아닐 수 있다) — 무창일 때만.

순수 모듈 불변식: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

import member_filters_config
import slot_ownership


def _member_grain(condition: dict[str, Any]) -> bool:
    scope = condition.get("aggregation_scope")
    return scope is None or scope == "per_member"


def _windowless(condition: dict[str, Any]) -> bool:
    return not condition.get("window_days") and not condition.get("calendar_period")


def _implies(condition: dict[str, Any], rule_operator: str, rule_count: int) -> bool:
    """집계 조건(operator, threshold)이 행동 규칙(operator, count)을 논리적으로 함의하는가."""
    operator = condition.get("operator")
    threshold = condition.get("threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return False
    if rule_operator == ">=":
        # 하한 함의: 어떤 창에서든 threshold 건 이상이면 평생 rule_count 건 이상이다.
        if operator == ">=":
            return threshold >= rule_count
        if operator == ">":
            return threshold >= rule_count - 1
        if operator == "=":
            return threshold >= rule_count
        return False
    if rule_operator == "=":
        # 정확값 함의는 무창 동일 비교만 성립한다 — 창 안의 '=1'은 평생 '=1'을 보장하지 않는다.
        return operator == "=" and threshold == rule_count and _windowless(condition)
    return False


def _covering_condition(
    aggregates: list[Any], spec: dict[str, Any]
) -> dict[str, Any] | None:
    for condition in aggregates:
        if not isinstance(condition, dict):
            continue
        if condition.get("metric_id") != spec["metric_id"]:
            continue
        if not _member_grain(condition):
            continue
        if _implies(condition, spec["operator"], spec["count"]):
            return condition
    return None


def demote_aggregate_covered_behaviors(
    plan: dict[str, Any], *, source_text: str
) -> list[str]:
    """aggregate_conditions 가 함의하는 주문 횟수 행동을 behaviors 에서 강등한다.

    반환: 강등된 행동 canonical 목록(진단용). 기록은 claim_slot 이
    superseded_conditions / decisions 두 곳에 남긴다.
    """
    target_user = plan.get("target_user")
    if not isinstance(target_user, dict):
        return []
    behaviors = target_user.get("behaviors")
    aggregates = target_user.get("aggregate_conditions")
    if not isinstance(behaviors, list) or not behaviors:
        return []
    if not isinstance(aggregates, list) or not aggregates:
        return []
    equivalents = member_filters_config.behavior_aggregate_equivalents()
    demoted: list[str] = []
    for behavior in list(behaviors):
        spec = equivalents.get(behavior) if isinstance(behavior, str) else None
        if spec is None:
            continue
        covering = _covering_condition(aggregates, spec)
        if covering is None:
            continue
        outcome = slot_ownership.claim_slot(
            plan,
            "target_user",
            "behaviors",
            owner=f"aggregate_conditions:{spec['metric_id']}",
            reason=(
                f"집계 조건 {spec['metric_id']} {covering.get('operator')} {covering.get('threshold')} 가 "
                f"{behavior}({spec['metric_id']} {spec['operator']} {spec['count']})를 논리적으로 함의 — "
                "같은 조건의 이중 표현이라 집계가 소유권을 가진다"
            ),
            source_text=source_text,
            value=behavior,
        )
        if outcome == "removed":
            demoted.append(behavior)
    return demoted


# 장바구니 행동의 표면 근거. 원문에 이 표지가 없는데 cart_abandoner 가 방출됐다면 환각이다 —
# 카트 빌더가 우선순위로 다른 조건(캠페인 반응 등)의 SQL 을 가로채는 실사고(26종 감사 #2/#5)를 낳는다.
_CART_SURFACE_MARKERS = ("장바구니", "카트", "담아", "담은", "담고", "담기")


def demote_unevidenced_cart_behavior(
    plan: dict[str, Any], *, source_text: str
) -> list[str]:
    """원문에 장바구니 표면 근거가 전혀 없는 cart_abandoner 행동을 강등한다.

    fill 근거가 없는 행동은 빌더 라우팅을 가로채므로(카트 빌더 승자독식) 환각으로 보고
    걷는다. 반환: 강등된 행동 목록(진단용)."""
    target_user = plan.get("target_user")
    if not isinstance(target_user, dict):
        return []
    behaviors = target_user.get("behaviors")
    if not isinstance(behaviors, list) or "cart_abandoner" not in behaviors:
        return []
    compact = "".join((source_text or "").split()).casefold()
    if any(marker in compact for marker in _CART_SURFACE_MARKERS):
        return []
    outcome = slot_ownership.claim_slot(
        plan,
        "target_user",
        "behaviors",
        owner="behavior_demotion:unevidenced_cart",
        reason="원문에 장바구니 표면 근거가 없어 cart_abandoner 를 환각으로 판정(빌더 가로채기 방지)",
        source_text=source_text,
        value="cart_abandoner",
    )
    return ["cart_abandoner"] if outcome == "removed" else []


# '최근 N개월 주문은 있었지만 최근 M일 구매가 없는'(이탈 위험) 문형. LLM 은 이를
# 평생 무구매(no_purchase)+미접속(inactivity_period)으로 오배선하곤 한다(26종 감사 #2) —
# 올바른 합성은 구매 존재(purchase_membership, N창) + 미구매 창(purchase_inactivity, M일)이다.
_LAPSED_RE = re.compile(
    r"최근\s*(?P<active>\d+)\s*(?P<unit>개월|주|일)\s*(?:동안|간|내)?\s*(?:주문|구매)"
    r"[은는이가도]?\s*있(?:었|던|는)?지만.{0,24}?"
    r"최근\s*(?P<inactive>\d+)\s*일\s*(?:간|동안|내)?\s*(?:구매|주문)[가는은이]?\s*없"
)
_UNIT_DAYS = {"개월": 30, "주": 7, "일": 1}


def normalize_lapsed_purchase_pattern(
    plan: dict[str, Any], *, source_text: str
) -> list[str]:
    """이탈 위험 문형을 구매 존재+미구매 창 합성으로 정규화하고 오배선 슬롯을 회수한다.

    반환: 적용한 변경 라벨 목록(진단용). 접속/로그인이 원문에 실재하면 inactivity_period 는
    남긴다(다른 절 소유 가능, fail-close)."""
    import plan_decisions

    match = _LAPSED_RE.search(source_text or "")
    if match is None:
        return []
    target_user = plan.get("target_user")
    if not isinstance(target_user, dict):
        target_user = {}
        plan["target_user"] = target_user
    active_days = int(match.group("active")) * _UNIT_DAYS[match.group("unit")]
    inactive_days = int(match.group("inactive"))
    changed: list[str] = []
    if not isinstance(target_user.get("purchase_membership"), dict):
        target_user["purchase_membership"] = {"operator": "exists", "window_days": active_days}
        changed.append(f"purchase_membership:{active_days}d")
    if not isinstance(target_user.get("purchase_inactivity"), dict):
        target_user["purchase_inactivity"] = {
            "value": inactive_days, "unit": "days", "min_days": inactive_days,
        }
        changed.append(f"purchase_inactivity:{inactive_days}d")
    behaviors = target_user.get("behaviors")
    if isinstance(behaviors, list) and "no_purchase" in behaviors:
        # 평생 무구매는 '최근 M일 무구매'와 모순 — 창 조건이 소유권을 가진다.
        target_user["behaviors"] = [item for item in behaviors if item != "no_purchase"]
        changed.append("behaviors:no_purchase 회수")
    compact = "".join((source_text or "").split()).casefold()
    if (
        isinstance(target_user.get("inactivity_period"), dict)
        and not any(marker in compact for marker in ("접속", "로그인"))
    ):
        target_user["inactivity_period"] = None
        changed.append("inactivity_period 회수(구매 부재를 접속 부재로 오배선)")
    for label in changed:
        plan_decisions.record(
            plan,
            filter_name="behavior_demotion:lapsed_purchase",
            action=plan_decisions.SET if label.startswith("purchase_") else plan_decisions.CLAIM,
            slot=label.split(":", 1)[0],
            reason="이탈 위험 문형(주문 있었지만 최근 무구매)의 결정론 합성/오배선 회수",
            value=label,
        )
    return changed


__all__ = [
    "demote_aggregate_covered_behaviors",
    "demote_unevidenced_cart_behavior",
    "normalize_lapsed_purchase_pattern",
]
