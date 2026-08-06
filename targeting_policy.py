"""정책 결정의 **단일 소유자**와 그 영수증(:class:`PolicyDecision`).

배경(실측 2026-08-06)
---------------------
같은 코퍼스 항목에서 세 개의 진실이 서로를 반박했다:

    #5   코퍼스 expectation 은 ``sql`` 인데 다른 코퍼스 설명은 "기간 없는 최근 = 되묻기"라고 적었다.
    #16  코퍼스는 ``unsupported`` 를 기대하는데 런타임 ``data_availability_policy=advise`` 는 SQL 을 낸다.
    #26  같은 입력이 실행에 따라 SQL 또는 clarification 으로 갈렸다.

이 셋의 공통 원인은 정책이 **여러 곳에 흩어져 있고 버전이 없다**는 것이다. 프롬프트별
예외를 넣으면 그 순간 네 번째 진실이 생긴다. 그래서 이 모듈이 생겼다:

  1. 정책 **우선순위**를 코드로 명시한다(:data:`PRECEDENCE`).
  2. 모든 결정에 :class:`PolicyDecision` 영수증을 남긴다 — 입력 사실 · 결정 · 사유 코드 · 버전.
  3. 정책 **버전**(:data:`POLICY_VERSION`)을 응답과 코퍼스가 함께 들고 대조할 수 있게 한다.

이 모듈은 SQL 도, 슬롯도 만들지 않는다. "무엇으로 귀결시킬 것인가"만 답하고 그 근거를 남긴다.

실행: python -m pytest tests/test_targeting_policy.py -q
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import temporal_clause

# 정책 묶음의 버전. 정책의 **의미**가 바뀌면 올린다(어휘 추가는 올리지 않는다).
# 코퍼스 항목의 ``policy_version`` 과 응답의 ``policy_decisions[].policy_version`` 이 이 값이다.
POLICY_VERSION = "targeting-policy-v2"

# 플랜/응답에 결정 영수증을 싣는 키.
POLICY_DECISIONS_KEY = "policy_decisions"

# 우선순위(작은 수가 먼저). 이 표가 "무엇이 무엇을 이기는가"의 단일 선언이다.
PRECEDENCE: dict[str, int] = {
    "safety": 10,
    "data_availability": 20,
    "ambiguity": 30,
    "default_binding": 40,
    "catalog_capability": 50,
    "compilation": 60,
}

# 결정 어휘(닫힌 집합).
DECISION_ALLOW = "allow"                      # 계속 진행한다
DECISION_CLARIFY = "user_clarification"       # 사용자에게 되묻는다
DECISION_UNSUPPORTED = "explicit_unsupported"  # 정직한 미지원
DECISION_REJECT = "policy_rejected"           # 정책이 막았다
DECISIONS = frozenset({DECISION_ALLOW, DECISION_CLARIFY, DECISION_UNSUPPORTED, DECISION_REJECT})


class PolicyContractError(ValueError):
    """정책 선언 자체가 어긋났다(알 수 없는 단계·결정 어휘)."""


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """정책 하나가 내린 결정의 영수증.

    ``input_facts`` 는 **결정을 좌우한 사실**만 담는다. 원문 전체나 개인정보를 넣지 않는다
    (CLAUDE.md §48) — 사실은 "period_stated=True" 같은 판정 결과다.
    """

    policy_id: str
    decision: str
    reason_code: str
    stage: str
    input_facts: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stage not in PRECEDENCE:
            raise PolicyContractError(f"unknown policy stage: {self.stage!r}")
        if self.decision not in DECISIONS:
            raise PolicyContractError(f"unknown policy decision: {self.decision!r}")

    @property
    def precedence(self) -> int:
        return PRECEDENCE[self.stage]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "stage": self.stage,
            "precedence": self.precedence,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "input_facts": list(self.input_facts),
            "detail": dict(self.detail),
        }


def record_decision(plan: dict[str, Any] | None, decision: PolicyDecision) -> PolicyDecision:
    """결정을 플랜의 영수증 목록에 남긴다(플랜이 없으면 기록만 건너뛴다)."""
    if isinstance(plan, dict):
        ledger = plan.setdefault(POLICY_DECISIONS_KEY, [])
        if isinstance(ledger, list):
            ledger.append(decision.to_dict())
    return decision


def decisions(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """플랜에 남은 결정 영수증(우선순위 → 정책 id 순)."""
    if not isinstance(plan, Mapping):
        return []
    raw = plan.get(POLICY_DECISIONS_KEY)
    if not isinstance(raw, list):
        return []
    items = [dict(item) for item in raw if isinstance(item, Mapping)]
    return sorted(items, key=lambda item: (item.get("precedence", 999), str(item.get("policy_id"))))


def digest(plan: Mapping[str, Any] | None) -> str:
    """이 요청이 적용한 정책 결정의 요약 해시(실행 지문 재료)."""
    import hashlib

    payload = json.dumps(
        [
            {key: item.get(key) for key in ("policy_id", "policy_version", "decision", "reason_code")}
            for item in decisions(plan)
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── 기간 결핍 정책(ambiguity → default_binding) ───────────────────────────────────
#
# 두 갈래가 하나의 정책이다:
#   (a) 원문이 이미 기간을 말했는데 결핍으로 신고됐다  → 신고가 거짓이다(교정한다).
#   (b) 원문이 정말 기간을 말하지 않았다             → 배포 설정이 정한다(기본값 or 되묻기).
#
# (a)를 (b)와 같은 자리에서 처리하는 것이 핵심이다. 예전에는 (a)를 다루는 곳이 아예 없어서
# `최근 30일` 이 `최근` 하나 때문에 되묻기로 닫혔다(#14 · #78 실측).

PERIOD_POLICY_ID = "period_binding"
REASON_PERIOD_STATED = "stated_period_contradicts_missing_argument"
REASON_PERIOD_REPAIR_FAILED = "stated_period_repair_rejected"
# 교정이 '표현 불가'를 이름 대며 선언한 경우. 거부와 구분해야 영수증이 원인을 말한다 —
# 하나로 묶으면 "모델이 아무것도 못 만들었다"와 "만들 수 없는 이유를 정확히 댔다"가 같아 보인다.
REASON_PERIOD_REPAIR_UNSUPPORTED = "stated_period_repair_unsupported"
REASON_PERIOD_ABSENT = "bare_recency_without_duration"


def _stated_period_instruction(
    clauses: Sequence[tuple[Mapping[str, Any], temporal_clause.TemporalClause]],
) -> str:
    """구조화기에 넘길 **애플리케이션 소유** 교정 지시문.

    모델에게 새 의미를 주는 것이 아니라, **원문이 이미 말한 값**을 어느 구간이 소유하는지
    알려 준다. 값의 출처는 원문 리터럴이므로 이 경로에는 기본값 표식이 붙지 않는다.
    """

    lines = [
        "[Application-owned Stated Period Correction]",
        (
            "A deterministic scan of original_query found an explicit duration bound to the "
            "recency marker you reported as missing. The period is stated, not missing."
        ),
    ]
    for issue, clause in clauses:
        evidence = issue.get("evidence") if isinstance(issue, Mapping) else None
        marker = ""
        if isinstance(evidence, Mapping) and isinstance(evidence.get("text"), str):
            marker = evidence["text"]
        window = {"type": "rolling", "value": clause.amount, "unit": clause.unit}
        spans = ", ".join(f"'{span.text}'" for span in clause.source_spans)
        lines.append(
            f"- The marker '{marker}' is quantified by {spans}. Put {json.dumps(window)} in the "
            f"TimeFilter that owns that clause."
        )
    lines.append(
        "Rebuild the same audience meaning with those TimeFilters and do NOT report "
        "missing_argument(period) for those spans. Every other stated period keeps its own value, "
        "and no unstated value may be inferred."
    )
    return "\n".join(lines)


def _rolling_windows(payload: Any) -> set[tuple[int, str]]:
    """표현 트리가 실제로 들고 있는 롤링 창들(값·단위)."""
    import event_ir

    found: set[tuple[int, str]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == "rolling":
                unit = event_ir.canonical_unit(value.get("unit"))
                raw = value.get("value")
                if unit is not None and isinstance(raw, int) and not isinstance(raw, bool):
                    found.add((raw, unit))
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child)

    visit(payload)
    return found


def resolve_stated_period(
    plan: Any,
    *,
    query: str,
    current_date: Any,
    restructure: Callable[[str], Any],
    missing_period_issues: Callable[[Any], list[dict[str, Any]]],
    requirement_key: str,
    write_log: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    """원문이 말한 기간으로 거짓 결핍 신고를 교정한다(교정 실패 시 원본 유지).

    **반려보다 교정이 먼저다.** 신고를 계약 위반으로 반려하면 같은 모델이 같은 실수를
    반복할 뿐이고 재시도 예산만 태운다. 여기서는 애플리케이션이 답을 알고 있으므로 그 답을
    지시문으로 넘겨 한 번만 다시 세운다. 채택은 **원문이 말한 창이 결과에 들어 있을 때만**
    한다 — 그 검사가 없으면 모델이 다른 창을 지어내도 통과한다.
    """

    def log(event: str, payload: dict[str, Any]) -> None:
        if write_log is not None:
            write_log(event, {"query": query, **payload})

    issues = missing_period_issues(plan)
    if not issues:
        return plan
    # 리터럴 추출은 앱이 소유한 결정론 단계다. 호출자가 넘기게 두면 같은 원문에서 두 개의
    # 리터럴 목록이 생길 수 있고, 그러면 이 판정과 하류 소비가 다른 사실을 본다.
    from query_structurer.semantic_ir import extract_literal_bindings

    literal_bindings = extract_literal_bindings(query, current_date=current_date)
    refuted = [
        (issue, clause)
        for issue in issues
        if (clause := temporal_clause.stated_period_for_issue(query, issue, literal_bindings))
        is not None
    ]
    if not refuted:
        # 신고가 유효하다. 기본값/되묻기 판정은 default_binding 단계가 이어받는다.
        record_decision(
            plan if isinstance(plan, dict) else None,
            PolicyDecision(
                policy_id=PERIOD_POLICY_ID,
                stage="ambiguity",
                decision=DECISION_ALLOW,
                reason_code=REASON_PERIOD_ABSENT,
                input_facts=("period_stated=false", f"missing_period_issues={len(issues)}"),
            ),
        )
        return plan

    # 단위 표기를 IR 어휘로 접는다. 리터럴 추출기는 복수형('days')을, IR 은 단수형('day')을 쓴다 —
    # 접지 않으면 정확히 옳은 재구조화가 ``candidate_dropped_stated_period`` 로 거부된다(실측).
    import event_ir

    stated = {
        (clause.amount, event_ir.canonical_unit(clause.unit))
        for _issue, clause in refuted
        if event_ir.canonical_unit(clause.unit) is not None
    }
    if not stated:
        record_decision(
            plan if isinstance(plan, dict) else None,
            PolicyDecision(
                policy_id=PERIOD_POLICY_ID,
                stage="ambiguity",
                decision=DECISION_ALLOW,
                reason_code=REASON_PERIOD_ABSENT,
                input_facts=("period_stated=true", "unit_not_expressible_in_ir"),
            ),
        )
        return plan
    candidate = restructure(_stated_period_instruction(refuted))
    rejected = _admit_stated_period(
        candidate,
        stated=stated,
        requirement_key=requirement_key,
        missing_period_issues=missing_period_issues,
    )
    log(
        "stated_period_correction",
        {
            "applied": rejected is None,
            "reason": rejected,
            "stated": sorted((f"{value}", unit) for value, unit in stated),
        },
    )
    if rejected == "candidate_declared_unsupported":
        # 교정이 '표현 불가'를 이름 대며 선언했다. 그 판정이 원본의 거짓 결핍(기간이 없다)보다
        # 정직하므로 채택한다 — 정책은 미지원을 되묻기로 바꾸는 자리가 아니다.
        record_decision(
            candidate if isinstance(candidate, dict) else None,
            PolicyDecision(
                policy_id=PERIOD_POLICY_ID,
                stage="ambiguity",
                decision=DECISION_UNSUPPORTED,
                reason_code=REASON_PERIOD_REPAIR_UNSUPPORTED,
                input_facts=("period_stated=true", "repair=declared_unsupported"),
            ),
        )
        return candidate
    if rejected is not None:
        record_decision(
            plan if isinstance(plan, dict) else None,
            PolicyDecision(
                policy_id=PERIOD_POLICY_ID,
                stage="ambiguity",
                decision=DECISION_CLARIFY,
                reason_code=REASON_PERIOD_REPAIR_FAILED,
                input_facts=("period_stated=true", f"rejection={rejected}"),
            ),
        )
        return plan
    record_decision(
        candidate if isinstance(candidate, dict) else None,
        PolicyDecision(
            policy_id=PERIOD_POLICY_ID,
            stage="ambiguity",
            decision=DECISION_ALLOW,
            reason_code=REASON_PERIOD_STATED,
            input_facts=("period_stated=true", "repair=accepted"),
            detail={
                "windows": sorted(
                    [{"value": value, "unit": unit} for value, unit in stated],
                    key=lambda item: (item["unit"], item["value"]),
                )
            },
        ),
    )
    return candidate


def declares_unsupported(candidate: Any) -> bool:
    """이 후보가 '이 뜻은 표현할 수 없다'를 **이름을 대며** 선언했는가.

    표현이 비었다는 사실만 보면 두 상태가 뭉친다 — "아무것도 못 만들었다"와 "만들 수 없는
    이유를 정확히 말했다". 뒤쪽은 거짓 결핍(기간이 없다)보다 **정직한 답**이므로 버리면 안 된다.
    """
    if not isinstance(candidate, Mapping):
        return False
    semantic_ir = candidate.get("semantic_ir")
    if not isinstance(semantic_ir, Mapping):
        return False
    return semantic_ir.get("status") == "unsupported"


def _admit_stated_period(
    candidate: Any,
    *,
    stated: set[tuple[int | None, str | None]],
    requirement_key: str,
    missing_period_issues: Callable[[Any], list[dict[str, Any]]],
) -> str | None:
    """채택 가능하면 ``None``, 아니면 거부 사유."""
    if not isinstance(candidate, Mapping):
        return "candidate_not_a_plan"
    requirement = candidate.get(requirement_key)
    if not isinstance(requirement, Mapping) or requirement.get("expression") is None:
        # 재구조화가 '표현 불가'를 이름 대며 선언했다면 그것이 이 요청의 정직한 귀결이다.
        # 버리고 원본을 돌려주면 사용자는 답이 이미 있는 기간을 되묻는 화면을 본다(실측 #25).
        if declares_unsupported(candidate):
            return "candidate_declared_unsupported"
        return "candidate_has_no_expression"
    if missing_period_issues(candidate):
        return "candidate_still_missing_period"
    windows = _rolling_windows(requirement.get("expression"))
    missing = sorted(
        f"{value}{unit}" for value, unit in stated if (value, unit) not in windows
    )
    if missing:
        return f"candidate_dropped_stated_period:{missing}"
    return None


# ── 데이터 적재 범위 정책(data_availability) ──────────────────────────────────────

DATA_AVAILABILITY_POLICY_ID = "data_availability"


def data_availability_mode(catalog: Mapping[str, Any] | None = None) -> str:
    """``advise``(고지만) 또는 ``block``(정직한 미지원). 선언 파일이 단일 소유자다."""
    if isinstance(catalog, Mapping):
        declared = catalog.get("data_availability_policy")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
    override = os.getenv("DATA_AVAILABILITY_POLICY", "").strip()
    return override or "advise"


def decide_data_availability(
    plan: dict[str, Any] | None,
    *,
    has_coverage_gap: bool,
    catalog: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """적재 범위 밖 조건을 막을지 고지만 할지 — 한 곳에서 정한다."""
    mode = data_availability_mode(catalog)
    if not has_coverage_gap:
        decision, reason = DECISION_ALLOW, "no_coverage_gap"
    elif mode == "block":
        decision, reason = DECISION_UNSUPPORTED, "data_coverage_gap_blocked"
    else:
        decision, reason = DECISION_ALLOW, "data_coverage_gap_advised"
    return record_decision(
        plan,
        PolicyDecision(
            policy_id=DATA_AVAILABILITY_POLICY_ID,
            stage="data_availability",
            decision=decision,
            reason_code=reason,
            input_facts=(f"mode={mode}", f"coverage_gap={str(has_coverage_gap).casefold()}"),
        ),
    )


__all__ = [
    "DATA_AVAILABILITY_POLICY_ID",
    "DECISIONS",
    "DECISION_ALLOW",
    "DECISION_CLARIFY",
    "DECISION_REJECT",
    "DECISION_UNSUPPORTED",
    "PERIOD_POLICY_ID",
    "POLICY_DECISIONS_KEY",
    "POLICY_VERSION",
    "PRECEDENCE",
    "REASON_PERIOD_ABSENT",
    "REASON_PERIOD_REPAIR_FAILED",
    "REASON_PERIOD_REPAIR_UNSUPPORTED",
    "REASON_PERIOD_STATED",
    "declares_unsupported",
    "PolicyContractError",
    "PolicyDecision",
    "data_availability_mode",
    "decide_data_availability",
    "decisions",
    "digest",
    "record_decision",
    "resolve_stated_period",
]
