"""슬롯 소유권 정책 — 어느 슬롯을 어느 경로가 만드는가, 그리고 언제 넘길 수 있는가.

이행을 "정규식 전부 → LLM 전부"로 한 번에 뒤집으면 사고가 한꺼번에 온다. 실제로 필요한 것은
**슬롯 하나씩** 넘기는 것이고, 그 순서를 정하는 기준은 두 축이다.

    소유(owner)   rule | llm    — 지금 이 슬롯의 값을 확정하는 경로
    위험(risk)    low|medium|high — 이 슬롯이 틀렸을 때 결과가 얼마나 나빠지는가

위험 등급은 "틀리면 무슨 일이 나는가"로 매긴다.

    low     틀려도 후보가 조금 넓어지거나 좁아지는 정도(정렬 방향, 결과 개수 제한 등)
    medium  대상 집합이 눈에 띄게 달라진다(연령·성별·등급 같은 회원 속성)
    high    조용히 0명이 되거나 전혀 다른 모집단이 된다(기간 창, 팩트 조인 조건, 집계 임계)

승격(rule → llm)은 감이 아니라 :mod:`parser_shadow` 누적 관찰로만 한다. :func:`promotion_report` 가
각 슬롯이 자기 위험 등급의 문턱(:data:`PROMOTION_GATES`)을 넘었는지 계산한다 — 문턱은 관찰 횟수와
일치율 둘 다이며, 위험한 판정(baseline 만 잡은 조건·값 불일치)이 하나라도 있으면 승격하지 않는다.
소실은 넓어지는 것보다 훨씬 나쁘기 때문이다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import parser_shadow

RULE = "rule"
LLM = "llm"
OWNERS = (RULE, LLM)

LOW, MEDIUM, HIGH = "low", "medium", "high"
RISKS = (LOW, MEDIUM, HIGH)

# ── 백스톱(7단계): LLM 이 소유한 슬롯이 LLM 없이 어떻게 되는가 ──────────────────
# 정규식을 없애는 것이 목표가 아니라 **역할을 줄이는 것**이 목표다. 1차 파서 자리에서 내려온
# 정규식은 백스톱으로 남아 "LLM 이 못 쓸 때 최소한을 건진다". 백스톱조차 없다면 그 슬롯은
# 반드시 fail-close 여야 한다 — 조건이 조용히 사라지는 것(fail-open)이 이 프로그램이 고치려는
# 결함 그 자체이기 때문이다.
BACKSTOP_RULE = "rule"            # 정규식/렉시콘 백스톱이 있다(좁지만 값을 만든다)
BACKSTOP_FAIL_CLOSE = "fail_close"  # 백스톱이 없고, 못 만들면 미해석으로 표시한다
BACKSTOP_NONE = "none"            # 백스톱도 표시도 없다 — 조용한 소실. 금지(결함으로만 존재)
BACKSTOPS = (BACKSTOP_RULE, BACKSTOP_FAIL_CLOSE, BACKSTOP_NONE)

DEFAULT_POLICY_PATH = Path(
    os.getenv("SLOT_POLICY_PATH", str(Path(__file__).resolve().parent / "docs" / "data" / "slot_policy.json"))
)


@dataclass(frozen=True)
class PromotionGate:
    """승격 문턱: 최소 관찰 횟수와 최소 일치율. 위험할수록 둘 다 높다."""

    min_observations: int
    min_agreement: float


# 위험 등급별 승격 문턱. high 슬롯은 사실상 사람 검토 없이는 못 넘어간다(의도한 것이다 —
# 기간 창이나 팩트 조인이 조용히 틀리면 0명 SQL 이 되고, 그건 아무도 오류로 인지하지 못한다).
PROMOTION_GATES: dict[str, PromotionGate] = {
    LOW: PromotionGate(min_observations=20, min_agreement=0.90),
    MEDIUM: PromotionGate(min_observations=50, min_agreement=0.97),
    HIGH: PromotionGate(min_observations=200, min_agreement=0.995),
}

# 코드 폴백(파일 부재/파손 시). 슬롯 이름은 ir_snapshot 정규형 키와 같다.
_CODE_FALLBACK: dict[str, dict[str, Any]] = {
    # ── 낮은 위험: 틀려도 후보 범위가 조금 흔들리는 정도 ──
    "plan.result_limit": {"owner": RULE, "risk": LOW, "note": "개수 제한. 틀리면 결과가 잘리거나 늘 뿐이다."},
    "plan.intent": {"owner": RULE, "risk": LOW, "note": "라우팅 의도. 틀리면 다른 트랙으로 가지만 결과가 조용히 비지는 않는다."},
    "campaign_constraints.objective": {"owner": RULE, "risk": LOW},
    "campaign_constraints.offer_type": {"owner": RULE, "risk": LOW},
    "campaign_constraints.channels": {"owner": RULE, "risk": LOW},
    # ── 중간 위험: 대상 집합이 눈에 띄게 달라진다 ──
    "target_user.gender": {"owner": RULE, "risk": MEDIUM},
    "target_user.age_min": {"owner": RULE, "risk": MEDIUM},
    "target_user.age_max": {"owner": RULE, "risk": MEDIUM},
    "target_user.lifecycle": {"owner": RULE, "risk": MEDIUM},
    "target_user.interests": {"owner": RULE, "risk": MEDIUM},
    "target_user.preferred_channels": {"owner": RULE, "risk": MEDIUM},
    "plan.dimension_filters": {"owner": RULE, "risk": MEDIUM},
    "target_user.purchase_object": {
        "owner": LLM, "risk": MEDIUM, "backstop": BACKSTOP_NONE,
        "gap": "결정론 상품명 추출기가 없어 TARGET_OBJECT_LLM_FALLBACK=false 면 조건이 조용히 사라진다(fail-open). "
               "최소한 fail_close 표시가 필요하고, 이상적으로는 상품 마스터 기반 백스톱을 둔다.",
    },
    "target_user.purchase_object_kind": {
        "owner": LLM, "risk": MEDIUM, "backstop": BACKSTOP_NONE,
        "gap": "purchase_object 와 같은 결함(값의 종속 슬롯).",
    },
    # ── 높은 위험: 조용히 0명이 되거나 모집단이 통째로 바뀐다 ──
    "target_user.purchase_date": {"owner": RULE, "risk": HIGH, "note": "절대 달력 창. 틀리면 0명."},
    "target_user.recent_login": {"owner": RULE, "risk": HIGH},
    "target_user.purchase_inactivity": {"owner": RULE, "risk": HIGH},
    "target_user.birthday_target": {"owner": RULE, "risk": HIGH, "note": "MMDD 비교. 연도가 섞이면 0명."},
    "target_user.signup_target": {"owner": RULE, "risk": HIGH},
    "target_user.cart_retention": {"owner": RULE, "risk": HIGH},
    "target_user.cart_type": {"owner": RULE, "risk": HIGH},
    "target_user.aggregate_conditions": {"owner": RULE, "risk": HIGH, "note": "집계 임계. 방향·값이 틀리면 전혀 다른 모집단."},
    "target_user.behaviors": {"owner": RULE, "risk": HIGH},
    "target_user.campaign_response_frequency": {"owner": RULE, "risk": HIGH},
    "target_user.purchase_membership": {"owner": RULE, "risk": HIGH},
    "plan.member_metric_ranking": {"owner": RULE, "risk": HIGH},
    "plan.semantic_conditions": {"owner": RULE, "risk": HIGH},
    "plan.set_expressions": {"owner": RULE, "risk": HIGH},
    "plan.logical_expression": {"owner": RULE, "risk": HIGH},
}

# 정책에 없는 슬롯의 기본값. 모르는 슬롯은 **가장 위험하게** 다룬다 — 등록을 잊었을 때 조용히
# LLM 으로 넘어가는 것보다, 규칙이 소유한 채 승격 대상에서 빠지는 쪽이 안전하다.
DEFAULT_ENTRY: dict[str, Any] = {
    "owner": RULE, "risk": HIGH, "backstop": BACKSTOP_RULE,
    "note": "정책 미등록 — 기본값(규칙 소유/최고 위험)",
}


def _load() -> dict[str, dict[str, Any]]:
    if not DEFAULT_POLICY_PATH.exists():
        return _CODE_FALLBACK
    try:
        payload = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _CODE_FALLBACK
    slots = payload.get("slots") if isinstance(payload, dict) else None
    return slots if isinstance(slots, dict) and slots else _CODE_FALLBACK


def policy_for(slot: str) -> dict[str, Any]:
    """슬롯 하나의 정책(미등록이면 안전한 기본값)."""
    entry = _load().get(slot)
    if not isinstance(entry, dict):
        return dict(DEFAULT_ENTRY)
    owner = entry.get("owner") if entry.get("owner") in OWNERS else DEFAULT_ENTRY["owner"]
    risk = entry.get("risk") if entry.get("risk") in RISKS else DEFAULT_ENTRY["risk"]
    backstop = entry.get("backstop") if entry.get("backstop") in BACKSTOPS else (
        # 규칙이 소유한 슬롯은 그 규칙 자체가 백스톱이다. LLM 소유인데 선언이 없으면 알 수 없으므로
        # 결함(none)으로 본다 — 모르는 것을 안전하다고 가정하지 않는다.
        BACKSTOP_RULE if owner == RULE else BACKSTOP_NONE
    )
    return {"owner": owner, "risk": risk, "backstop": backstop,
            "note": entry.get("note"), "gap": entry.get("gap")}


def owner_of(slot: str) -> str:
    return policy_for(slot)["owner"]


def risk_of(slot: str) -> str:
    return policy_for(slot)["risk"]


def llm_owned_slots() -> tuple[str, ...]:
    """LLM 이 확정하는 슬롯들(enforce 모드에서 후보 값을 채택하는 대상)."""
    return tuple(sorted(slot for slot in _load() if owner_of(slot) == LLM))


def registered_slots() -> tuple[str, ...]:
    return tuple(sorted(_load()))


def backstop_of(slot: str) -> str:
    return policy_for(slot)["backstop"]


def silent_loss_slots() -> dict[str, str]:
    """백스톱도 fail-close 표시도 없는 슬롯 → 사유. **조용한 소실**이 남아 있는 자리다.

    이 목록이 비는 것이 7단계의 종료 조건이다. 계약 테스트는 (a) 모든 항목에 사유가 적혀 있고
    (b) 개수가 늘지 않는다는 두 가지를 강제한다 — 결함을 없애기 전에 최소한 **세어지게** 만든다.
    """
    out: dict[str, str] = {}
    for slot in registered_slots():
        policy = policy_for(slot)
        if policy["backstop"] == BACKSTOP_NONE:
            out[slot] = policy.get("gap") or ""
    return out


def promotion_report(agreement: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """슬롯별 승격 가능 여부. 입력은 :func:`parser_shadow.agreement_by_slot` 결과다.

    ``eligible`` 은 세 조건을 모두 만족할 때만 참이다: 관찰 횟수 충족, 일치율 충족, 위험 판정 0건.
    ``blocked_by`` 가 못 넘은 이유를 남긴다 — "아직 안 된다"만으로는 무엇을 더 모아야 할지 모른다.
    """
    report: list[dict[str, Any]] = []
    for slot in sorted(set(agreement) | set(registered_slots())):
        stats = agreement.get(slot) or {"observed": 0, "agree": 0, "rate": 0.0, "risky": 0}
        policy = policy_for(slot)
        gate = PROMOTION_GATES[policy["risk"]]

        blocked: list[str] = []
        if policy["owner"] == LLM:
            blocked.append("already_llm_owned")
        if stats["observed"] < gate.min_observations:
            blocked.append(f"observations {stats['observed']}/{gate.min_observations}")
        if stats["rate"] < gate.min_agreement:
            blocked.append(f"agreement {stats['rate']:.3f}/{gate.min_agreement}")
        if stats.get("risky"):
            blocked.append(f"risky_verdicts {stats['risky']}")

        report.append({
            "slot": slot,
            "owner": policy["owner"],
            "risk": policy["risk"],
            "observed": stats["observed"],
            "agreement": stats["rate"],
            "risky": stats.get("risky", 0),
            "eligible": not blocked,
            "blocked_by": blocked,
        })
    return report


def resolve_enforced_slots(comparison: dict[str, Any]) -> dict[str, Any]:
    """enforce 모드에서 후보 값을 채택할 슬롯과 그 값.

    LLM 소유로 선언된 슬롯만 대상이며, 후보가 값을 만들지 않았으면 채택하지 않는다(빈 값으로
    덮어써서 조건을 지우는 사고 방지 — 이행 중에는 소실이 가장 위험한 실패다).
    """
    if parser_shadow.mode() != parser_shadow.MODE_ENFORCE:
        return {}
    owned = set(llm_owned_slots())
    adopted: dict[str, Any] = {}
    for slot, entry in (comparison.get("slots") or {}).items():
        if slot not in owned or "candidate" not in entry:
            continue
        if entry.get("verdict") in {parser_shadow.ONLY_CANDIDATE, parser_shadow.VALUE_DIFFERS}:
            adopted[slot] = entry["candidate"]
    return adopted
