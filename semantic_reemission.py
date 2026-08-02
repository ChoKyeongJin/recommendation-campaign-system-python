"""재방출 — **미해결 요구사항만** 고치는 패치. 전체 재생성이 아니다.

이전 구조의 재시도는 두 가지였고 둘 다 위험했다:
  ① 스키마 실패 시 **전문 재생성** — 이미 맞았던 조건이 다음 런에서 사라질 수 있다.
  ② uncovered 구간 재추출 후 **무조건 병합** — 구간 밖 노드가 들어오면 조용한 재해석이 된다.

여기서는 재방출을 패치로 좁힌다:

    1. 원장에서 미해결 요구사항만 고른다(pending/uncovered — 미지원·내부 사고는 제외)
    2. 그 요구사항의 원문 구간만 담은 **패치 요청**을 만든다
    3. 돌아온 노드 중 요청 구간에 근거를 둔 것만 받아들인다
    4. **보호 집합**(이미 compiled 인 요구사항의 노드)은 절대 교체·삭제하지 않는다
    5. 라운드 수는 정책값이다(기본 1, 환경변수/인자로 조정)

그리고 매 라운드가 끝나면 원장을 다시 만들어 **회귀 여부를 결정론으로 확인**한다.
회귀가 생기면 그 라운드를 통째로 되돌린다 — "고치려다 망가뜨렸다"를 구조적으로 막는다.

범용 코어 규약: graph_rag 를 import 하지 않는다. 도메인 이름을 모른다.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import requirement_ledger
import semantic_plan
from requirement_ledger import RequirementLedger
from semantic_plan import SemanticPlanV2

MAX_ROUNDS_ENV = "SEMANTIC_REEMISSION_MAX_ROUNDS"
DEFAULT_MAX_ROUNDS = 1


@dataclass(frozen=True)
class ReemissionPolicy:
    """재방출 정책. 케이스별 예외가 아니라 정책값 하나로 조절한다."""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    # 미해결 요구사항이 없어도 라운드를 돌릴 것인가(항상 False — 돌 이유가 없다).
    reemit_when_complete: bool = False
    # 한 라운드에 요청할 구간 수 상한(프롬프트 폭주 방지). 0 이면 제한 없음.
    max_spans_per_round: int = 8

    @classmethod
    def from_env(cls, **overrides: Any) -> "ReemissionPolicy":
        raw = os.getenv(MAX_ROUNDS_ENV)
        max_rounds = DEFAULT_MAX_ROUNDS
        if raw is not None:
            try:
                max_rounds = max(0, int(raw))
            except ValueError:
                max_rounds = DEFAULT_MAX_ROUNDS
        return cls(max_rounds=max_rounds, **overrides)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "max_spans_per_round": self.max_spans_per_round,
        }


@dataclass
class PatchRequest:
    """무엇을 다시 읽어야 하는지 — 원문 구절과 그 이유만 담는다(슬롯 이름 없음)."""

    spans: tuple[str, ...] = ()
    targets: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"spans": list(self.spans), "targets": [dict(item) for item in self.targets]}


@dataclass
class RoundRecord:
    round: int
    requested_spans: list[str] = field(default_factory=list)
    accepted_node_ids: list[str] = field(default_factory=list)
    rejected_node_ids: list[str] = field(default_factory=list)
    reverted: bool = False
    revert_reason: str | None = None
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "requested_spans": list(self.requested_spans),
            "accepted_node_ids": list(self.accepted_node_ids),
            "rejected_node_ids": list(self.rejected_node_ids),
            "reverted": self.reverted,
            "revert_reason": self.revert_reason,
            "before": dict(self.before),
            "after": dict(self.after),
        }


@dataclass
class ReemissionOutcome:
    plan: SemanticPlanV2
    ledger: RequirementLedger
    rounds: list[RoundRecord] = field(default_factory=list)

    @property
    def patched(self) -> bool:
        return any(record.accepted_node_ids and not record.reverted for record in self.rounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": [record.to_dict() for record in self.rounds],
            "patched": self.patched,
            "summary": self.ledger.summary(),
        }


def build_patch_request(
    ledger: RequirementLedger,
    *,
    policy: ReemissionPolicy,
    structurer_issues: Sequence[Mapping[str, Any]] = (),
) -> PatchRequest:
    """미해결 요구사항 + **정규화 실패 후보** → 패치 요청.

    미지원·내부 사고는 재방출로 고쳐지지 않으므로 제외한다. 반대로 "노드는 있는데 타입이
    어긋났다"는 재방출로 고쳐질 수 있는데도 예전에는 트리거가 아니었다 — coverage 상 그
    구간은 노드가 청구하고 있어 uncovered 가 아니기 때문이다(실측: 최대 실패 군집이 여기였다).
    """
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(target: dict[str, Any]) -> bool:
        span = str(target.get("source_span") or "").strip()
        if not span or span in seen:
            return True
        seen.add(span)
        targets.append({**target, "source_span": span})
        return not (policy.max_spans_per_round and len(targets) >= policy.max_spans_per_round)

    for issue in structurer_issues or ():
        if str(issue.get("reemission") or "") == "skip":
            continue
        # 검증기의 구체적 피드백을 그대로 실어 보낸다 — "다시 해봐"보다 "이 타입 payload 가
        # 비었고 저 타입은 완성됐다"가 훨씬 잘 고쳐진다.
        if not _add({
            "requirement_id": issue.get("node_id"),
            "label": issue.get("proposed_type"),
            "source_span": issue.get("source_span"),
            "reason": issue.get("reason"),
            "missing_fields": [],
            "candidate_types": list(issue.get("compatible_types") or []),
            "kind": "type_mismatch",
        }):
            return PatchRequest(
                spans=tuple(item["source_span"] for item in targets), targets=tuple(targets)
            )

    for item in ledger.unresolved():
        if not _add({
            "requirement_id": item.requirement_id,
            "label": item.label,
            "source_span": item.source_span or "",
            "reason": item.validation.get("reason"),
            "missing_fields": list(item.validation.get("missing_fields") or []),
            "kind": "unresolved",
        }):
            break
    return PatchRequest(spans=tuple(item["source_span"] for item in targets), targets=tuple(targets))


def _compact(text: str) -> str:
    return "".join((text or "").split())


def apply_patch(
    plan: SemanticPlanV2,
    patch: SemanticPlanV2,
    *,
    request: PatchRequest,
    protected_ids: frozenset[str],
) -> tuple[list[str], list[str]]:
    """패치 노드를 병합한다. 보호 집합은 건드리지 않는다.

    반환: (받아들인 노드 id, 거절한 노드 id)

    받아들이는 조건:
      - 요청한 구간 안(또는 그 구간을 포함)에 근거를 둔 노드일 것
      - 보호된 요구사항의 노드를 교체하지 않을 것

    같은 요구사항 id 로 온 노드는 그 요구사항이 **미해결일 때만** 교체한다. 그래야
    "이미 통과한 조건이 재방출로 훼손되지 않는다"가 코드로 보장된다.
    """
    allowed = [_compact(span) for span in request.spans if span and span.strip()]
    accepted: list[str] = []
    rejected: list[str] = []
    existing = {node.id: node for node in plan.walk()}
    counter = 0

    for node in list(patch.nodes):
        compact = _compact(node.source_span)
        if not compact or not any(compact in span or span in compact for span in allowed):
            rejected.append(node.id)
            continue
        if node.id in protected_ids:
            rejected.append(node.id)
            continue
        node.producer = f"{node.producer}:patch"
        if node.id in existing:
            replaced = existing[node.id]
            if replaced in plan.nodes:
                plan.nodes[plan.nodes.index(replaced)] = node
                accepted.append(node.id)
                continue
            counter += 1
            node.id = f"{node.id}-p{counter}"
        plan.nodes.append(node)
        accepted.append(node.id)
    return accepted, rejected


def run(
    plan: SemanticPlanV2,
    ledger: RequirementLedger,
    *,
    request_patch: Callable[[PatchRequest], SemanticPlanV2 | None] | None,
    rebuild: Callable[[SemanticPlanV2], RequirementLedger],
    policy: ReemissionPolicy | None = None,
) -> ReemissionOutcome:
    """미해결 요구사항이 남아 있는 동안 정책 횟수만큼 패치를 시도한다.

    `rebuild` 는 '수정된 플랜 → 새 원장'을 만드는 함수다(정규화·capability·컴파일 재실행).
    회귀가 감지되면 그 라운드를 되돌리고 즉시 멈춘다.
    """
    active_policy = policy or ReemissionPolicy.from_env()
    outcome = ReemissionOutcome(plan=plan, ledger=ledger)
    if request_patch is None or active_policy.max_rounds <= 0:
        return outcome

    current_plan, current_ledger = plan, ledger
    for round_index in range(1, active_policy.max_rounds + 1):
        pending_issues = list(getattr(current_plan, "structurer_issues", []) or [])
        if (
            current_ledger.is_complete()
            and not pending_issues
            and not active_policy.reemit_when_complete
        ):
            break
        request = build_patch_request(
            current_ledger, policy=active_policy, structurer_issues=pending_issues
        )
        if not request.spans:
            break
        record = RoundRecord(
            round=round_index,
            requested_spans=list(request.spans),
            before=current_ledger.summary(),
        )
        snapshot = copy.deepcopy(current_plan)
        try:
            patch = request_patch(request)
        except Exception as exc:  # noqa: BLE001 — 패치 실패는 원래의 정직한 결핍 보고로 귀결된다.
            record.revert_reason = f"패치 요청 실패: {exc.__class__.__name__}: {exc}"
            record.after = record.before
            outcome.rounds.append(record)
            break
        if patch is None or not patch.nodes:
            record.after = record.before
            outcome.rounds.append(record)
            break

        accepted, rejected = apply_patch(
            current_plan,
            patch,
            request=request,
            protected_ids=current_ledger.protected_ids(),
        )
        record.accepted_node_ids, record.rejected_node_ids = accepted, rejected
        if not accepted:
            record.after = record.before
            outcome.rounds.append(record)
            break

        new_ledger = rebuild(current_plan)
        delta = requirement_ledger.merge_outcomes(current_ledger, new_ledger)
        record.after = new_ledger.summary()
        if delta["regressed"]:
            # 되돌린다 — 통과했던 조건을 잃느니 원래의 미해결 보고가 낫다.
            record.reverted = True
            record.revert_reason = (
                "재방출이 이미 통과한 요구사항을 훼손했다: " + ", ".join(delta["regressed"])
            )
            current_plan = snapshot
            outcome.rounds.append(record)
            break
        outcome.rounds.append(record)
        current_plan, current_ledger = current_plan, new_ledger

    outcome.plan, outcome.ledger = current_plan, current_ledger
    return outcome


def patch_prompt_targets(
    request: PatchRequest | None = None,
    *,
    spans: Sequence[str] = (),
    targets: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """패치 요청을 사람이 읽는 줄로(프롬프트 조립은 호출자 몫 — 여기선 문구만 만든다).

    검증기의 구체적 피드백(왜 거절했는지 + 어떤 타입이 가능한지)을 함께 싣는다. 구간만
    다시 보내면 같은 실수가 반복된다 — 타입 오배선은 재시도해도 같은 타입이 나온다.
    """
    listed = list(request.targets) if request is not None else list(targets)
    sequence = list(request.spans) if request is not None else list(spans)
    by_span = {
        str(target.get("source_span") or "").strip(): target
        for target in listed
        if isinstance(target, Mapping)
    }
    lines: list[str] = []
    for span in sequence or list(by_span):
        span = str(span).strip()
        target = by_span.get(span)
        if not target:
            lines.append(f"- {span}")
            continue
        reason = str(target.get("reason") or "").strip()
        candidates = [str(item) for item in (target.get("candidate_types") or []) if item]
        detail = f" (검증: {reason})" if reason else ""
        if candidates:
            detail += f" · 가능한 노드 타입: {', '.join(sorted(candidates))}"
        lines.append(f"- {span}{detail}")
    return lines


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "MAX_ROUNDS_ENV",
    "PatchRequest",
    "ReemissionOutcome",
    "ReemissionPolicy",
    "RoundRecord",
    "apply_patch",
    "build_patch_request",
    "patch_prompt_targets",
    "run",
]
