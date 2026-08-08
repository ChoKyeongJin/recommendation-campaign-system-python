"""Resolution Policy — **자동 확정 / 되묻기 / 미지원**을 결정하는 단 하나의 계층.

이 계층 밖에서는 아무도 이 셋을 고르지 않는다(§9).

* Clarification Planner 는 이미 ``ASK_USER`` 로 결정된 결핍을 문장으로 **표현만** 한다.
* Lowering Planner 는 결핍이 없는 요구가 실행 가능한지만 본다.
* Compiler 는 결핍의 존재를 몰라도 된다.

판정 순서가 곧 계약이다.

1. **미지원 계열은 절대 묻지 않는다.** 사용자가 아무리 설명해도 실행 자산이 생기지 않는다(§25).
2. 설정이 "반드시 묻는다"고 선언한 종류는 값이 있어도 묻는다(``require_clarification``).
3. 위험도가 모드의 자동 확정 허용선을 넘으면 묻는다 — HIGH 는 **어느 모드에서도** 넘는다(§34-H).
4. 설정이 자동 확정을 허용하지 않았으면 묻는다.
5. 값을 만들 수 있는 해결기가 없거나 값이 없으면 묻는다.
6. **그 값을 실제로 넣을 자리가 없으면 묻는다.** 이 마지막 관문이 없으면 정책은 채울 수 없는
   값을 약속하고, 고정점 루프는 같은 결핍을 영원히 다시 본다.

값은 이 파일이 만들지 않는다. 기간 기본값의 소유자는 :mod:`default_period_policy` 이고
(배포 env · 표현별 카탈로그), 여기서는 그 소유자에게 **묻기만** 한다 — 같은 사실을 두 곳에
적으면 "구조화기가 채운 창"과 "정책이 인정하는 창"이 갈린다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from resolution import slots
from resolution.config import (
    MODE_AUTO_RISKS,
    ResolutionMode,
    ResolutionPolicyConfig,
    resolved_config,
)
from resolution.issues import (
    MISSING_RECENT_PERIOD,
    IssueFamily,
    ResolutionRisk,
    SemanticIssue,
)
from resolution.request import CanonicalRequest, ResolutionProvenance


class ResolutionAction(StrEnum):
    AUTO_RESOLVE = "auto_resolve"
    ASK_USER = "ask_user"
    UNSUPPORTED = "unsupported"


# ── 사유 코드(닫힌 어휘) ─────────────────────────────────────────────────────────
REASON_UNSUPPORTED_FAMILY = "unsupported_family"
REASON_CLARIFICATION_REQUIRED = "clarification_required_by_policy"
REASON_RISK_ABOVE_MODE = "risk_above_mode"
REASON_AUTO_NOT_ALLOWED = "auto_resolution_not_allowed"
REASON_NO_RESOLVER = "no_auto_resolver"
REASON_VALUE_UNAVAILABLE = "policy_value_unavailable"
REASON_SLOT_NOT_APPLICABLE = "slot_not_applicable"
REASON_POLICY_DEFAULT = "policy_default_applied"


@dataclass(frozen=True, slots=True)
class AutoResolution:
    """정책이 만들어 낸 값 하나."""

    value: Any
    #: 응답 계약의 assumption 코드.
    code: str
    provenance: ResolutionProvenance = ResolutionProvenance.POLICY_DEFAULT
    reason_code: str = REASON_POLICY_DEFAULT


AutoResolver = Callable[
    [CanonicalRequest, SemanticIssue, ResolutionPolicyConfig], AutoResolution | None
]


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    issue_id: str
    action: ResolutionAction
    risk: ResolutionRisk
    kind: str
    value: Any = None
    code: str | None = None
    provenance: ResolutionProvenance | None = None
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "action": self.action.value,
            "risk": self.risk.value,
            "kind": self.kind,
            "code": self.code,
            "provenance": None if self.provenance is None else self.provenance.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ResolutionDecisions:
    """한 라운드의 판정 전부. 결핍과 1:1 이다."""

    decisions: tuple[ResolutionDecision, ...] = ()

    def by_action(self, action: ResolutionAction) -> tuple[ResolutionDecision, ...]:
        return tuple(item for item in self.decisions if item.action is action)

    def get(self, issue_id: str) -> ResolutionDecision | None:
        for item in self.decisions:
            if item.issue_id == issue_id:
                return item
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.decisions)

    def __len__(self) -> int:
        return len(self.decisions)


# ── 값 해결기 ────────────────────────────────────────────────────────────────────

#: 기간 기본값 assumption 코드. 응답 계약이라 문자열을 바꾸지 않는다.
CODE_DEFAULT_RECENT_PERIOD = "DEFAULT_RECENT_PERIOD"


def resolve_recent_period(
    request: CanonicalRequest,
    issue: SemanticIssue,
    config: ResolutionPolicyConfig,
) -> AutoResolution | None:
    """기간 없는 최근성 표현의 기본 창. **값의 소유자에게 묻는다**(여기서 만들지 않는다).

    표현별 카탈로그가 먼저다 — '요즘'과 '최근'이 같은 창이 되면 카탈로그를 둔 이유가 없다.
    카탈로그가 답하지 못하면 배포 설정(env)으로 물러서고, 둘 다 없으면 ``None`` 이다(정책 꺼짐).
    """

    # 지연 import: 값 소유자가 구조화기 패키지를 끌어오므로 순환을 만들지 않는다.
    import default_period_policy
    import qualitative_defaults

    found = None
    if issue.evidence is not None:
        found = qualitative_defaults.resolve_recency_marker(issue.evidence.text)
    if found is not None:
        window = {"type": "rolling", "value": found.value, "unit": found.unit}
        return AutoResolution(value=window, code=CODE_DEFAULT_RECENT_PERIOD)
    period = default_period_policy.resolve_default_period()
    if period is None:
        return None
    return AutoResolution(value=period.window, code=CODE_DEFAULT_RECENT_PERIOD)


#: 종류별 값 해결기. 등록되지 않은 종류는 자동 확정되지 않는다(=되묻기).
AUTO_RESOLVERS: dict[str, AutoResolver] = {
    MISSING_RECENT_PERIOD: resolve_recent_period,
}


# ── 판정 ─────────────────────────────────────────────────────────────────────────


class ResolutionPolicy:
    """설정 + 모드로 결핍을 판정한다. 상태를 갖지 않는다(전역 mutable 없음, §30)."""

    def __init__(
        self,
        config: ResolutionPolicyConfig | None = None,
        *,
        resolvers: Mapping[str, AutoResolver] | None = None,
        applicability: Callable[[CanonicalRequest, SemanticIssue], bool] | None = None,
    ) -> None:
        self._config = config if config is not None else resolved_config()
        self._resolvers = dict(AUTO_RESOLVERS if resolvers is None else resolvers)
        self._applicability = slots.can_apply if applicability is None else applicability

    @property
    def config(self) -> ResolutionPolicyConfig:
        return self._config

    @property
    def mode(self) -> ResolutionMode:
        return self._config.mode

    def _decide(
        self, request: CanonicalRequest, issue: SemanticIssue
    ) -> ResolutionDecision:
        def closed(action: ResolutionAction, reason: str) -> ResolutionDecision:
            return ResolutionDecision(
                issue_id=issue.issue_id,
                action=action,
                risk=issue.risk,
                kind=issue.kind,
                reason_code=reason,
            )

        if issue.family is IssueFamily.UNSUPPORTED:
            return closed(ResolutionAction.UNSUPPORTED, REASON_UNSUPPORTED_FAMILY)
        if self._config.clarification_required(issue.kind):
            return closed(ResolutionAction.ASK_USER, REASON_CLARIFICATION_REQUIRED)
        if issue.risk not in MODE_AUTO_RISKS[self._config.mode]:
            return closed(
                ResolutionAction.ASK_USER, f"{REASON_RISK_ABOVE_MODE}:{issue.risk.value}"
            )
        if not self._config.auto_resolution_allowed(issue.kind):
            return closed(ResolutionAction.ASK_USER, REASON_AUTO_NOT_ALLOWED)
        resolver = self._resolvers.get(issue.kind)
        if resolver is None:
            return closed(ResolutionAction.ASK_USER, REASON_NO_RESOLVER)
        resolution = resolver(request, issue, self._config)
        if resolution is None:
            return closed(ResolutionAction.ASK_USER, REASON_VALUE_UNAVAILABLE)
        if not self._applicability(request, issue):
            # 값은 있는데 넣을 자리가 없다. 약속하지 않는 편이 정직하다.
            return closed(ResolutionAction.ASK_USER, REASON_SLOT_NOT_APPLICABLE)
        return ResolutionDecision(
            issue_id=issue.issue_id,
            action=ResolutionAction.AUTO_RESOLVE,
            risk=issue.risk,
            kind=issue.kind,
            value=resolution.value,
            code=resolution.code,
            provenance=resolution.provenance,
            reason_code=resolution.reason_code,
        )

    def resolve(
        self, request: CanonicalRequest, issues: Sequence[SemanticIssue]
    ) -> ResolutionDecisions:
        return ResolutionDecisions(
            decisions=tuple(self._decide(request, issue) for issue in issues)
        )


__all__ = [
    "AUTO_RESOLVERS",
    "CODE_DEFAULT_RECENT_PERIOD",
    "REASON_AUTO_NOT_ALLOWED",
    "REASON_CLARIFICATION_REQUIRED",
    "REASON_NO_RESOLVER",
    "REASON_POLICY_DEFAULT",
    "REASON_RISK_ABOVE_MODE",
    "REASON_SLOT_NOT_APPLICABLE",
    "REASON_UNSUPPORTED_FAMILY",
    "REASON_VALUE_UNAVAILABLE",
    "AutoResolution",
    "AutoResolver",
    "ResolutionAction",
    "ResolutionDecision",
    "ResolutionDecisions",
    "ResolutionMode",
    "ResolutionPolicy",
    "resolve_recent_period",
]
