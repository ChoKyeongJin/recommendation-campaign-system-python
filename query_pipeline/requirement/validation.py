"""표현 검증의 **주입 계약** — "이 표현이 유효한가"는 요구 계층이 답할 수 없다.

이 계층은 심볼 카탈로그도, 컴파일러 능력도, 원문 리터럴 색인도 모른다. 그래서 검증을
여기에 구현하면 범용 계층이 도메인 계층이 된다 — 그 순간 `requirement` 는 특정 배포의
스키마 없이는 import 조차 되지 않는다.

대신 **모양만** 선언하고 구현은 도메인이 준다(:mod:`audience_validators`). 계층 가드
(``tests/test_query_pipeline_layering.py``)는 패키지 내부 방향만 보므로 이 경계는 가드가
아니라 **시그니처**가 지킨다: 검증기는 표현·원문·리터럴만 받고, 그 밖의 것을 받으려면
자기 생성자에서 받아 와야 한다.

issue 의 ``code``(LLM 계약 어휘)와 ``kind``(애플리케이션 처리 분기)를 잇는 표도 여기 있다.
표와 그 역함수를 한 곳에 두는 이유는 하나다 — 두 방향이 갈리면 같은 결핍이 들어올 때와
나갈 때 다른 이름을 갖는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from query_pipeline.event_query.expressions import EventExpression, SourceEvidence
from query_pipeline.requirement.issues import (
    IssueKind,
    IssueSeverity,
    RequirementIssue,
)

# 기존 구조화기 issue code → 구조화된 결핍 종류. code 는 LLM 계약의 어휘이고 kind 는
# 애플리케이션의 처리 분기다. 둘을 같은 값으로 쓰면 계약을 바꿀 때마다 처리 분기가 흔들린다.
ISSUE_CODE_KINDS: dict[str, IssueKind] = {
    "missing_argument": IssueKind.MISSING,
    "ambiguous_requirement": IssueKind.AMBIGUOUS,
    "unsupported_semantics": IssueKind.UNSUPPORTED,
    "validation_mismatch": IssueKind.INVALID,
}

# 역방향. 위 표가 단사(injective)라 파생으로 만든다 — 손으로 적으면 한쪽만 고쳐진다.
ISSUE_KIND_CODES: dict[IssueKind, str] = {
    kind: code for code, kind in ISSUE_CODE_KINDS.items()
}
assert len(ISSUE_KIND_CODES) == len(ISSUE_CODE_KINDS), "issue code ↔ kind 표가 단사가 아니다"

# 표에 없는 kind 가 나왔을 때 부를 이름. 계약 밖의 판정을 조용히 성공으로 만들지 않고
# '검증 불일치'로 내보낸다 — 사라지는 것보다 이름이 어색한 편이 낫다.
_FALLBACK_CODE = "validation_mismatch"


@runtime_checkable
class ExpressionValidator(Protocol):
    """표현 하나를 보고 결핍을 보고한다. **고치지 않는다**(정규화는 다른 단계의 일이다)."""

    def validate(
        self,
        expression: EventExpression,
        *,
        query: str,
        literals: Sequence[JsonValue],
    ) -> tuple[RequirementIssue, ...]: ...


def issue_from_report(
    *,
    code: str,
    argument: str,
    message: str,
    evidence: SourceEvidence | None = None,
) -> RequirementIssue:
    """``{code, argument, message, evidence}`` 보고 → 구조화된 결핍.

    ``id``/``path`` 를 규약으로 조립해 :func:`report_from_issue` 가 되돌릴 수 있게 한다.
    """
    return RequirementIssue(
        id=f"{code}:{argument}",
        kind=ISSUE_CODE_KINDS.get(code, IssueKind.INVALID),
        severity=IssueSeverity.ERROR,
        path=f"$.{argument}",
        message=message,
        evidence=evidence,
    )


def report_from_issue(issue: RequirementIssue) -> tuple[str, str]:
    """구조화된 결핍 → ``(code, argument)``. :func:`issue_from_report` 의 역이다."""
    code = ISSUE_KIND_CODES.get(issue.kind, _FALLBACK_CODE)
    argument = issue.path[2:] if issue.path.startswith("$.") else issue.path
    return code, argument or "audience_expression"


__all__ = [
    "ISSUE_CODE_KINDS",
    "ISSUE_KIND_CODES",
    "ExpressionValidator",
    "issue_from_report",
    "report_from_issue",
]
