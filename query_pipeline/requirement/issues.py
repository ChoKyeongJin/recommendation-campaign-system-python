"""요구 결핍의 구조화 — 문자열 목록이 아니라 **해소 주체가 드러나는** 타입.

이 저장소가 반복해서 다친 지점: "왜 안 되는가"가 자유 문장으로만 남으면 그 문장을 읽고
무엇을 할지(사용자에게 되묻기 / 기본값 적용 / 후보 선택 / 재방출 / 거절) 판단하는 코드가
문자열 매칭으로 자란다. issue 에 ``kind``·``severity``·``resolution`` 을 두는 것은 그
판단을 **값**으로 옮기는 일이다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from query_pipeline.base import StrictModel
from query_pipeline.event_query.expressions import LiteralValue, SourceEvidence


class IssueKind(str, Enum):
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    POLICY_DENIED = "policy_denied"
    SCHEMA_MISMATCH = "schema_mismatch"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ResolutionKind(str, Enum):
    ASK_USER = "ask_user"
    USE_DEFAULT = "use_default"
    SELECT_CANDIDATE = "select_candidate"
    REWRITE = "rewrite"
    REJECT = "reject"


class IssueResolution(StrictModel):
    """이 결핍을 **어떻게** 없앨 수 있는가. 없으면 사용자에게 되묻는 것이 기본이다."""

    kind: ResolutionKind
    default_value: LiteralValue | None = None


class RequirementIssue(StrictModel):
    id: str = Field(min_length=1)
    kind: IssueKind
    severity: IssueSeverity
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)
    candidates: tuple[LiteralValue, ...] = ()
    resolution: IssueResolution | None = None
    # 이 결핍이 원문 어디에서 나왔는가. 없으면 '문장 어딘가'가 되고, 그러면 되묻기 문장도
    # 원인 판정(추출된 리터럴과의 대조)도 구간을 잃는다 — 이 저장소가 실측으로 다친 자리다.
    evidence: SourceEvidence | None = None

    @property
    def blocks_execution(self) -> bool:
        """실행을 막는가 — ERROR 이면서 애플리케이션이 스스로 해소할 수단이 없을 때."""
        if self.severity is not IssueSeverity.ERROR:
            return False
        if self.resolution is None:
            return True
        return self.resolution.kind in {ResolutionKind.ASK_USER, ResolutionKind.REJECT}


__all__ = [
    "IssueKind",
    "IssueResolution",
    "IssueSeverity",
    "RequirementIssue",
    "ResolutionKind",
]
