"""후보 → 타입드 노드 사이의 **결정론 정규화 계층**(범용 코어).

왜 필요했나: LLM 의 원시 출력이 곧바로 정식 IR 로 취급됐다. 그래서 타입 선택 실수 하나가
그대로 `missing_fields` 가 되고, 그것이 사용자 확인 요청으로 변환됐다. 실측(2026-08-02):

    {"type": "entity_set_membership",           ← 제안된 타입
     "subject": "회원", "metric": "order_count",  ← 채워진 것은 집계 술어의 payload
     "operator": ">=", "value": {"amount": 2},
     "member_entity": null, "ranked_set": null, "relation": null}

`entity_set_membership` 의 필수 payload 는 0/3, `aggregate_predicate` 의 필수 payload 는 완비다.
사람에게 물을 것이 없다 — **코드가 정할 수 있다.**

이 모듈의 규약:

  - **타입은 제안이다.** 최종 타입은 payload 시그니처와 닫힌 어휘 정합으로 계산한다.
  - **케이스 분기를 만들지 않는다.** "metric 이 order_count 면 aggregate_predicate" 같은 규칙은
    다음 지표에서 다시 깨진다. 여기 있는 것은 `NODE_CLASSES` 선언과 도메인 어휘 결속뿐이다.
  - **정보 손실이 있으면 재분류하지 않는다.** 버려지는 의미 필드가 있으면 재방출로 넘긴다.
  - **모호하면 조용히 고르지 않는다.** 호환 타입이 둘 이상이고 우열이 없으면 conflict 다.

범용 코어 규약: graph_rag 를 import 하지 않는다. 도메인 낱말을 모른다 — 어휘 결속과 값 목록은
`semantic_domain_binding` 과 주입된 `vocabularies` 에서 온다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import semantic_domain_binding
import semantic_plan
from semantic_plan import NODE_CLASSES, SemanticNode

# 정규화 판정(닫힌 집합) — 이 값이 그대로 결핍 원인 분류(Phase 3)의 입력이 된다.
ACTION_KEPT = "kept"
ACTION_REPAIRED = "repaired"
ACTION_RETYPED = "retyped"
ACTION_AMBIGUOUS = "ambiguous"
ACTION_UNRESOLVED = "unresolved"

# 노드 공통 필드 — 타입 판정의 입력이 아니다(어느 타입이든 갖는다).
_COMMON_KEYS: frozenset[str] = frozenset(
    {"id", "type", "source_span", "source_start", "source_end", "confidence", "producer"}
)


def _declared_field_names(cls: type[SemanticNode]) -> frozenset[str]:
    return frozenset(spec.name for spec in cls.FIELDS)


def _all_declared_field_names() -> frozenset[str]:
    names: set[str] = set()
    for cls in NODE_CLASSES:
        names |= _declared_field_names(cls)
    return frozenset(names)


def _populated(payload: Mapping[str, Any], name: str) -> bool:
    return not semantic_plan._empty(payload.get(name))  # noqa: SLF001 — 결핍 정의는 하나뿐이다


@dataclass
class TypeFit:
    """한 노드 타입이 이 payload 를 받을 수 있는가에 대한 판정."""

    node_type: str
    fits: bool
    repairs: dict[str, Any] = field(default_factory=dict)
    consumed: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def rank_key(self) -> tuple[int, int]:
        """소비한 의미 필드가 많고 버리는 것이 적을수록 앞선다."""
        return (len(self.consumed), -len(self.dropped))


@dataclass
class RetypeOutcome:
    """노드 하나의 정규화 결과 — 응답 진단으로 그대로 나간다."""

    node_id: str
    source_span: str
    proposed_type: str
    resolved_type: str | None
    action: str
    source_start: int | None = None
    source_end: int | None = None
    compatible_types: tuple[str, ...] = ()
    repairs: dict[str, Any] = field(default_factory=dict)
    dropped_fields: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "source_span": self.source_span,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "proposed_type": self.proposed_type,
            "resolved_type": self.resolved_type,
            "action": self.action,
            "compatible_types": list(self.compatible_types),
            "repairs": dict(self.repairs),
            "dropped_fields": list(self.dropped_fields),
            "reason": self.reason,
        }


# ── 닫힌 어휘 정합 (discriminator 역인덱스 포함) ─────────────────────────────────
def _forms(value: str, resolve: Any) -> set[str]:
    """이 표면 표현이 가리킬 수 있는 canonical 후보들.

    노드 값은 원문 표현 그대로여도 된다는 것이 LLM 계약이다('누적 구매금액', '발송 성공').
    그래서 어휘 정합은 **문자열 일치**가 아니라 **해소 후 일치**로 판정해야 한다 —
    문자열로만 보면 정상 표현이 전부 어휘 위반이 된다.
    """
    forms = {value}
    if resolve is None:
        return forms
    try:
        forms |= {item for item in resolve(value) if isinstance(item, str) and item}
    except Exception:  # noqa: BLE001 — 해소기 사고가 타입 판정을 죽이지 않는다.
        pass
    return forms


def _vocabulary_verdict(
    node_type: str,
    field_name: str,
    payload: Mapping[str, Any],
    *,
    bindings: Mapping[str, Any],
    vocabularies: Mapping[str, Sequence[str]],
    resolve: Any = None,
) -> tuple[bool, dict[str, Any], str]:
    """`node_type.field` 의 값이 닫힌 어휘에 맞는가. 맞출 수 있으면 discriminator 를 고쳐 준다.

    반환: (합격 여부, 고칠 필드, 사유). 선언이나 값 목록이 없으면 제약 없음(합격).

    discriminator 역인덱스가 여기 있는 이유: 선언이 이미
    `metric = f(scope)` 를 갖고 있으므로, 그 역함수 `scope = f⁻¹(metric)` 은 **새 지식이 아니라
    같은 선언의 다른 방향**이다. 새 어휘가 늘어도 JSON 한 줄이면 자동으로 따라온다.
    """
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        return True, {}, ""
    binding = bindings.get(f"{node_type}.{field_name}")
    if not isinstance(binding, Mapping):
        return True, {}, ""

    forms = _forms(value, resolve)
    default_key = binding.get("default")
    if isinstance(default_key, str) and default_key:
        allowed = vocabularies.get(default_key)
        if not allowed:
            return True, {}, ""
        if forms & set(allowed):
            return True, {}, ""
        return False, {}, f"{field_name}={value!r} 는 {default_key} 어휘에 없다"

    discriminator = binding.get("discriminator")
    by_value = binding.get("by_value")
    if not (isinstance(discriminator, str) and isinstance(by_value, Mapping)):
        return True, {}, ""

    current = payload.get(discriminator)
    known_keys = [str(key) for key in by_value.values() if vocabularies.get(str(key))]
    if not known_keys:
        return True, {}, ""
    if isinstance(current, str) and current and str(current) not in by_value:
        # 선언에 없는 분기는 **제약 없음**이다 — 그 관계에서 이 필드는 다른 뜻으로 쓰인다
        # (예: relation='co_purchase' 의 attribute 는 이력 속성이 아니라 엔터티다).
        return True, {}, ""

    # 이 값이 속하는 discriminator 값들(역인덱스).
    owners = [
        str(disc)
        for disc, key in by_value.items()
        if vocabularies.get(str(key)) and forms & set(vocabularies[str(key)])
    ]
    current_key = by_value.get(str(current)) if isinstance(current, str) else None

    if isinstance(current, str) and current_key and vocabularies.get(str(current_key)):
        if forms & set(vocabularies[str(current_key)]):
            return True, {}, ""
        if len(owners) == 1:
            return True, {discriminator: owners[0]}, (
                f"{field_name}={value!r} 에 맞춰 {discriminator} 를 {owners[0]!r} 로 정정"
            )
        return False, {}, (
            f"{field_name}={value!r} 는 {discriminator}={current!r} 의 어휘에 없다"
        )

    # discriminator 가 없거나 알 수 없는 값 — 값에서 되찾는다.
    if len(owners) == 1:
        return True, {discriminator: owners[0]}, (
            f"{field_name}={value!r} 에서 {discriminator}={owners[0]!r} 를 확정"
        )
    if not owners:
        return False, {}, f"{field_name}={value!r} 는 어떤 {discriminator} 어휘에도 없다"
    return False, {}, (
        f"{field_name}={value!r} 가 여러 {discriminator}({', '.join(sorted(owners))})에 속한다"
    )


def _meaning_losing(dropped: Sequence[str], payload: Mapping[str, Any]) -> list[str]:
    """이 필드를 버리면 **의미가 바뀌는가**.

    무엇이 의미인지는 도메인이 선언한다(`identity_fields`) — 코어가 필드 이름을 알지 못한다.
    참인 flag 도 손실로 본다: 부정(`negated=True`)을 조용히 버리면 조건이 뒤집힌다.
    거짓 flag 와 도메인이 의미로 선언하지 않은 필드(주어 표현 등)는 손실이 아니다.
    """
    identity = frozenset(semantic_domain_binding.identity_fields())
    lost: list[str] = []
    for name in dropped:
        value = payload.get(name)
        if value is True or name in identity:
            lost.append(name)
    return lost


def evaluate_fit(
    cls: type[SemanticNode],
    payload: Mapping[str, Any],
    *,
    bindings: Mapping[str, Any],
    vocabularies: Mapping[str, Sequence[str]],
    all_declared: frozenset[str],
    resolve: Any = None,
) -> TypeFit:
    """이 payload 를 `cls` 타입으로 받을 수 있는가."""
    declared = _declared_field_names(cls)
    reasons: list[str] = []
    repairs: dict[str, Any] = {}
    fits = True

    # **어휘 정합이 결핍 판정보다 먼저다.** 역인덱스 수리는 discriminator 를 채우는데, 그
    # discriminator 가 곧 필수 필드인 타입이 있다(집계 술어의 scope). 결핍을 먼저 재면 수리가
    # 채워 줄 자리를 '없음'으로 확정해 버려서, 코드가 확정할 수 있는 타입을 사람에게 되묻게 된다
    # (실측 2026-08-02: metric='order_count' 하나로 scope='order' 가 확정되는데도 필수 결핍 처리).
    working = dict(payload)
    for spec in cls.FIELDS:
        ok, repair, reason = _vocabulary_verdict(
            cls.TYPE, spec.name, working,
            bindings=bindings, vocabularies=vocabularies, resolve=resolve,
        )
        if repair:
            repairs.update(repair)
            working.update(repair)
        if not ok:
            fits = False
            if reason:
                reasons.append(reason)

    missing = sorted(
        name for name in cls.required_field_names() if not _populated(working, name)
    )
    if missing:
        fits = False
        reasons.append("필수 payload 없음: " + ", ".join(missing))

    populated = {
        name for name in payload
        if name not in _COMMON_KEYS and _populated(payload, name)
    }
    consumed = tuple(sorted(populated & declared))
    # 버려지는 것은 '다른 노드 타입이 의미로 선언한 필드'뿐이다 — 선언되지 않은 잡음은 손실이 아니다.
    dropped = tuple(sorted((populated - declared) & all_declared))
    lost = _meaning_losing(dropped, payload)
    if lost:
        fits = False
        reasons.append("의미가 사라진다: " + ", ".join(lost))
    return TypeFit(
        node_type=cls.TYPE,
        fits=fits,
        repairs=repairs,
        consumed=consumed,
        dropped=dropped,
        reasons=tuple(reasons),
    )


# ── 정규화 ───────────────────────────────────────────────────────────────────────
def normalize_node_payload(
    payload: Mapping[str, Any],
    *,
    bindings: Mapping[str, Any],
    vocabularies: Mapping[str, Sequence[str]],
    all_declared: frozenset[str],
    resolve: Any = None,
) -> tuple[dict[str, Any] | None, RetypeOutcome]:
    """노드 payload 하나를 정규화한다. 실패하면 (None, 사유) — 조용히 버리지 않는다."""
    working = dict(payload)
    proposed = str(working.get("type") or "")
    node_id = str(working.get("id") or "")
    span = str(working.get("source_span") or "")
    bounds = {
        "source_start": working.get("source_start") if isinstance(working.get("source_start"), int) else None,
        "source_end": working.get("source_end") if isinstance(working.get("source_end"), int) else None,
    }

    fits = [
        evaluate_fit(
            cls, working, bindings=bindings, vocabularies=vocabularies,
            all_declared=all_declared, resolve=resolve,
        )
        for cls in NODE_CLASSES
    ]
    compatible = [fit for fit in fits if fit.fits]
    compatible_types = tuple(fit.node_type for fit in compatible)

    def _apply(fit: TypeFit, action: str, reason: str) -> tuple[dict[str, Any], RetypeOutcome]:
        result = dict(working)
        result["type"] = fit.node_type
        result.update(fit.repairs)
        for name in fit.dropped:
            result.pop(name, None)
        return result, RetypeOutcome(
            node_id=node_id,
            source_span=span,
            **bounds,
            proposed_type=proposed,
            resolved_type=fit.node_type,
            action=action,
            compatible_types=compatible_types,
            repairs=dict(fit.repairs),
            dropped_fields=fit.dropped,
            reason=reason,
        )

    proposed_fit = next((fit for fit in compatible if fit.node_type == proposed), None)
    if proposed_fit is not None:
        if proposed_fit.repairs:
            reason = "; ".join(
                f"{key}={value!r} 로 정정" for key, value in sorted(proposed_fit.repairs.items())
            )
            return _apply(proposed_fit, ACTION_REPAIRED, reason)
        return _apply(proposed_fit, ACTION_KEPT, "")

    if not compatible:
        failed = next((fit for fit in fits if fit.node_type == proposed), None)
        reasons = failed.reasons if failed else ()
        return None, RetypeOutcome(
            node_id=node_id,
            source_span=span,
            **bounds,
            proposed_type=proposed,
            resolved_type=None,
            action=ACTION_UNRESOLVED,
            compatible_types=(),
            reason="; ".join(reasons) or "어떤 노드 타입의 payload 도 완성되지 않았다",
        )

    ranked = sorted(compatible, key=lambda fit: fit.rank_key, reverse=True)
    if len(ranked) > 1 and ranked[0].rank_key == ranked[1].rank_key:
        return None, RetypeOutcome(
            node_id=node_id,
            source_span=span,
            **bounds,
            proposed_type=proposed,
            resolved_type=None,
            action=ACTION_AMBIGUOUS,
            compatible_types=compatible_types,
            reason=(
                "호환 타입이 여럿이고 우열이 없다: "
                + ", ".join(sorted(compatible_types))
            ),
        )
    winner = ranked[0]
    return _apply(
        winner,
        ACTION_RETYPED,
        f"제안 {proposed!r} 의 필수 payload 가 없고 {winner.node_type!r} 만 완성됐다",
    )


def normalize_payloads(
    payloads: Iterable[Any],
    *,
    vocabularies: Mapping[str, Sequence[str]] | None = None,
    bindings: Mapping[str, Any] | None = None,
    resolve: Any = None,
) -> tuple[list[dict[str, Any]], list[RetypeOutcome]]:
    """노드 payload 목록을 정규화한다(하위 노드 재귀 포함).

    정규화에 실패한 노드는 **결과에서 빠지고** outcome 으로 남는다 — 그 사유가 재방출 요청과
    실패 원인 분류의 입력이다. 조용히 통과시키면 타입 오염이 canonical IR 에 들어간다.
    """
    resolved_bindings = bindings if bindings is not None else semantic_domain_binding.node_field_bindings()
    resolved_vocabularies = {
        str(key): tuple(str(item) for item in values or ())
        for key, values in (vocabularies or {}).items()
        if isinstance(values, (list, tuple))
    }
    all_declared = _all_declared_field_names()
    kept: list[dict[str, Any]] = []
    outcomes: list[RetypeOutcome] = []
    for payload in payloads or []:
        if not isinstance(payload, Mapping):
            continue
        working = dict(payload)
        # 하위 노드 먼저 — 부모의 필수 payload 판정이 자식 존재 여부에 달려 있다.
        for spec_name in _node_valued_fields(working):
            children, child_outcomes = normalize_payloads(
                working.get(spec_name) or [],
                vocabularies=resolved_vocabularies,
                bindings=resolved_bindings,
                resolve=resolve,
            )
            outcomes.extend(child_outcomes)
            if children:
                working[spec_name] = children
            else:
                working.pop(spec_name, None)
        normalized, outcome = normalize_node_payload(
            working,
            bindings=resolved_bindings,
            vocabularies=resolved_vocabularies,
            all_declared=all_declared,
            resolve=resolve,
        )
        outcomes.append(outcome)
        if normalized is not None:
            kept.append(normalized)
    return kept, outcomes


def _node_valued_fields(payload: Mapping[str, Any]) -> list[str]:
    """이 payload 에서 하위 노드 목록을 담고 있는 필드 이름들."""
    names: list[str] = []
    for cls in NODE_CLASSES:
        for spec in cls.FIELDS:
            if spec.kind != "nodes" or spec.name in names:
                continue
            raw = payload.get(spec.name)
            if isinstance(raw, list) and raw:
                names.append(spec.name)
    return names


def unresolved_outcomes(outcomes: Sequence[RetypeOutcome]) -> list[RetypeOutcome]:
    """재방출 대상 — 결정론으로 복구하지 못한 것만."""
    return [
        outcome for outcome in outcomes
        if outcome.action in {ACTION_UNRESOLVED, ACTION_AMBIGUOUS}
    ]


def repaired_outcomes(outcomes: Sequence[RetypeOutcome]) -> list[RetypeOutcome]:
    return [
        outcome for outcome in outcomes
        if outcome.action in {ACTION_RETYPED, ACTION_REPAIRED}
    ]


def to_report(outcomes: Sequence[RetypeOutcome]) -> dict[str, Any]:
    """진단 기록 — '왜 이 타입이 됐는지'가 응답에서 보여야 회귀를 관측할 수 있다."""
    return {
        "outcomes": [outcome.to_dict() for outcome in outcomes],
        "retyped": len(repaired_outcomes(outcomes)),
        "unresolved": len(unresolved_outcomes(outcomes)),
    }


__all__ = [
    "ACTION_AMBIGUOUS",
    "ACTION_KEPT",
    "ACTION_REPAIRED",
    "ACTION_RETYPED",
    "ACTION_UNRESOLVED",
    "RetypeOutcome",
    "TypeFit",
    "evaluate_fit",
    "normalize_node_payload",
    "normalize_payloads",
    "repaired_outcomes",
    "to_report",
    "unresolved_outcomes",
]
