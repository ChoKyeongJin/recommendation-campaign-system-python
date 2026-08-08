"""실패의 **원인**을 타입으로 말하고, 그 타입 하나만 사용자 귀결을 정한다.

왜 이 계층이 필요한가(실측 2026-08-08, 라이브 감사 40~86)
--------------------------------------------------------
``여성이면서 정상에서 휴면으로 바뀐 회원`` 이 다음 문구로 닫혔다::

    이 시간 조건은 선언된 값 2개를 요구하지만 문장에서 확인된 값은 1개입니다

**귀결(unsupported)은 옳고 이름이 틀렸다.** ``정상`` 과 ``휴면`` 은 둘 다 문장에 있다. 실제
원인은 ``MEMBER_STATE_CD`` 의 **상태 이력 소스가 실재하지 않는다**는 것이고, 그것은 문장을
고쳐서 열 수 있는 결핍이 아니다. 운영자가 저 문구를 읽으면 사용자에게 "문장을 고쳐 달라"고
안내하게 되는데, 문장으로는 영원히 열리지 않는다.

같은 결함의 다른 얼굴이 ``semantic_registry_gap`` 이다 — 원인이 다른 실패 여럿이 이름 하나로
뭉개져서, 사용자는 "담당자에게 문의"를, 담당자는 "어디를 볼지"를 모른다.

그래서 이 모듈은 둘을 **분리**한다.

===============  ==========================================================
진단(Diagnostic)  *왜* 실패했나 — 원인을 만든 계층이 선언한다
귀결(Outcome)     사용자가 *무엇을 할 수 있나* — 이 모듈이 진단에서 파생한다
===============  ==========================================================

파생이 표 하나(:data:`_OUTCOME`)인 것이 요점이다. 다른 계층이 ``status="unsupported"`` 를 직접
적으면 그 표는 거짓이 되고, 같은 원인이 경로마다 다른 귀결로 끝난다.

**진단을 먼저 고치고 귀결을 중앙화한다.** 순서를 뒤집으면(귀결만 존중하면) 위 ``#73`` 은
``clarification`` 으로 뒤집혀, 고칠 수 없는 것을 고치라고 안내하게 된다.

실행: python -m pytest tests/test_semantic_diagnostics.py -q
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    """사용자 귀결. **이 모듈만** 이 값을 고른다."""

    CLARIFICATION = "clarification"  # 사용자가 문장을 고쳐 열 수 있다
    UNSUPPORTED = "unsupported"      # 지금 실행 자산으로는 답할 수 없다
    INTERNAL_FAILURE = "internal_failure"  # 배선·불변식 결함(사용자 요청의 의미적 결과가 아니다)


class Recoverability(StrEnum):
    """이 실패를 **누가** 고칠 수 있는가. 귀결과 1:1 이 아니다(운영 안내가 갈린다)."""

    USER = "user"                # 문장을 고치면 열린다
    DEPLOYMENT = "deployment"    # 선언·적재를 늘리면 열린다
    ENGINEERING = "engineering"  # 코드를 고쳐야 한다


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """실패 하나 — 코드·근거·복구 가능성 + 사용자와 개발자에게 각각 줄 문장.

    ``user_action`` 은 사용자가 읽을 문장이고 ``developer_detail`` 은 로그·운영 화면용이다.
    둘을 한 문자열로 합치면 사용자에게 심볼 이름이 나가거나, 운영자가 원인을 못 찾는다.

    ``clause_id`` 는 :mod:`clause_semantics` 가 만든 절 식별자다 — 한 문장에 절이 여럿일 때
    "어느 절이 막혔는지"를 말할 수 있어야 사용자가 고칠 자리를 찾는다.
    """

    code: str
    user_action: str
    developer_detail: str
    recoverability: Recoverability
    clause_id: str | None = None
    symbol: str | None = None
    required_capability: str | None = None
    available_capability: tuple[str, ...] = ()
    evidence: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> Outcome:
        """이 진단이 뜻하는 사용자 귀결. 파생이지 선언이 아니다."""
        return outcome_for(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "outcome": str(self.outcome),
            "recoverability": str(self.recoverability),
            "clause_id": self.clause_id,
            "symbol": self.symbol,
            "required_capability": self.required_capability,
            "available_capability": list(self.available_capability),
            "evidence": self.evidence,
            "user_action": self.user_action,
            "developer_detail": self.developer_detail,
            "detail": dict(self.detail),
        }


# ── 진단 코드(닫힌 집합) ────────────────────────────────────────────────────────────
# 자유 텍스트가 아니라 상수인 이유는 귀결 표의 키이기 때문이다. 새 코드를 추가하면 표에도
# 넣어야 하고, 안 넣으면 :func:`outcome_for` 가 이름을 대며 실패한다(조용한 기본값 금지).

MISSING_VALUE = "missing_value"
AMBIGUOUS_MEANING = "ambiguous_meaning"
MISSING_FIELD = "missing_field"
MISSING_CAPABILITY = "missing_capability"
MISSING_HISTORY_SOURCE = "missing_history_source"
UNSUPPORTED_SUBJECT = "unsupported_subject"
UNSUPPORTED_TEMPORAL_PRECISION = "unsupported_temporal_precision"
UNLOWERABLE_TEMPORAL_CONSTRAINT = "unlowerable_temporal_constraint"
COMPILER_INVARIANT_VIOLATION = "compiler_invariant_violation"

_OUTCOME: dict[str, Outcome] = {
    # 사용자가 말하지 않았거나 애매한 것 — 문장을 고치면 열린다.
    MISSING_VALUE: Outcome.CLARIFICATION,
    AMBIGUOUS_MEANING: Outcome.CLARIFICATION,
    # 지금 실행 자산의 한계 — 문장을 고쳐도 열리지 않는다.
    MISSING_FIELD: Outcome.UNSUPPORTED,
    MISSING_CAPABILITY: Outcome.UNSUPPORTED,
    MISSING_HISTORY_SOURCE: Outcome.UNSUPPORTED,
    UNSUPPORTED_SUBJECT: Outcome.UNSUPPORTED,
    UNSUPPORTED_TEMPORAL_PRECISION: Outcome.UNSUPPORTED,
    UNLOWERABLE_TEMPORAL_CONSTRAINT: Outcome.UNSUPPORTED,
    # 배선 결함 — 사용자 요청의 의미적 결과가 아니다.
    COMPILER_INVARIANT_VIOLATION: Outcome.INTERNAL_FAILURE,
}


class UnknownDiagnosticCode(KeyError):
    """귀결 표에 없는 진단 코드. **조용한 기본값을 주지 않는다.**"""


def outcome_for(diagnostic: Diagnostic) -> Outcome:
    """진단 → 사용자 귀결. 표에 없는 코드는 이름을 대며 실패한다.

    기본값을 두지 않는 이유: 표에 없는 코드에 ``unsupported`` 를 주면 새 진단이 조용히 없는
    한계를 광고하고, ``clarification`` 을 주면 고칠 수 없는 것을 고치라고 안내한다. 둘 다
    틀린 답이므로 답하지 않는다.
    """
    try:
        return _OUTCOME[diagnostic.code]
    except KeyError as exc:
        raise UnknownDiagnosticCode(
            f"진단 코드 {diagnostic.code!r} 의 사용자 귀결이 선언되지 않았습니다"
            f" (선언된 코드: {sorted(_OUTCOME)})"
        ) from exc


def declared_codes() -> frozenset[str]:
    """귀결이 선언된 진단 코드 전부(드리프트 가드가 읽는다)."""
    return frozenset(_OUTCOME)


# ── 생성자(원인을 만든 계층이 부른다) ───────────────────────────────────────────────


def missing_capability(
    *,
    capability: str,
    symbol: str,
    clause_id: str | None = None,
    evidence: str | None = None,
    available: tuple[str, ...] = (),
    user_action: str,
    developer_detail: str,
) -> Diagnostic:
    """요청한 의미가 요구하는 관측 능력을 어떤 선언도 갖고 있지 않다.

    '부재(never)'가 대표 사례다. 마지막 값만 아는 표현(``latest_only``)에 부재를 물으면
    ``!= 'APP'`` 같은 **다른 뜻**의 SQL 이 나온다 — 어제 앱으로 로그인한 회원이 대상에 들어간다.
    근사 대신 이 진단으로 닫는다.
    """
    return Diagnostic(
        code=MISSING_CAPABILITY,
        user_action=user_action,
        developer_detail=developer_detail,
        recoverability=Recoverability.DEPLOYMENT,
        clause_id=clause_id,
        symbol=symbol,
        required_capability=capability,
        available_capability=available,
        evidence=evidence,
    )


def missing_history_source(
    *,
    symbol: str,
    clause_id: str | None = None,
    evidence: str | None = None,
    user_action: str,
    developer_detail: str,
) -> Diagnostic:
    """그 속성의 **이력**을 담은 소스가 없다(현재값만 있다).

    ``정상에서 휴면으로 바뀐`` 이 이 진단의 자리다. 값이 부족한 것이 아니라 두 값을 비교할
    시점이 하나뿐이다.
    """
    return Diagnostic(
        code=MISSING_HISTORY_SOURCE,
        user_action=user_action,
        developer_detail=developer_detail,
        recoverability=Recoverability.DEPLOYMENT,
        clause_id=clause_id,
        symbol=symbol,
        evidence=evidence,
    )


def unsupported_subject(
    *,
    requested: str,
    supported: tuple[str, ...],
    evidence: str | None = None,
) -> Diagnostic:
    """답의 **주체**가 이 시스템이 낼 수 있는 행이 아니다.

    ``구매 회원이 100명 이상인 브랜드를 알려줘`` 는 회원 세그먼트가 아니라 브랜드 목록을
    요청한다. 이것은 배선 결함(``failure``)이 아니라 정직한 미지원이다 — 사용자에게 줄 다음
    행동이 있다("회원을 대상으로 다시 요청하십시오").
    """
    names = ", ".join(supported)
    return Diagnostic(
        code=UNSUPPORTED_SUBJECT,
        user_action=(
            f"현재 타겟 주체는 {names} 뿐입니다. "
            f"'{requested}' 을(를) 행으로 내는 질의는 지원하지 않습니다."
        ),
        developer_detail=(
            f"requested_subject={requested!r} supported_subjects={list(supported)!r}"
        ),
        recoverability=Recoverability.DEPLOYMENT,
        symbol=requested,
        evidence=evidence,
        detail={"requested_subject": requested, "supported_subjects": list(supported)},
    )


def unsupported_temporal_precision(
    *,
    requested: str,
    supported: str,
    clause_id: str | None = None,
    evidence: str | None = None,
) -> Diagnostic:
    """요청한 시간 해상도를 소스가 담지 못한다.

    ``최근 24시간`` 이 대표다. 사용자는 기간을 **말했으므로** '기간 값이 없습니다'로 되물으면
    안 된다 — 이해는 했고 담을 수 없다는 것이 정확한 사실이다.
    """
    return Diagnostic(
        code=UNSUPPORTED_TEMPORAL_PRECISION,
        user_action=(
            f"요청한 시간 해상도({requested})를 이 데이터가 담고 있지 않습니다"
            f"(가능한 최소 단위: {supported})."
        ),
        developer_detail=f"requested_precision={requested!r} supported_precision={supported!r}",
        recoverability=Recoverability.DEPLOYMENT,
        clause_id=clause_id,
        evidence=evidence,
        detail={"requested": requested, "supported": supported},
    )


def compiler_invariant_violation(
    *, symbol: str, developer_detail: str, clause_id: str | None = None
) -> Diagnostic:
    """컴파일러·배선의 불변식이 깨졌다. **사용자 요청의 의미적 결과가 아니다.**"""
    return Diagnostic(
        code=COMPILER_INVARIANT_VIOLATION,
        user_action="요청을 처리하는 중 내부 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        developer_detail=developer_detail,
        recoverability=Recoverability.ENGINEERING,
        clause_id=clause_id,
        symbol=symbol,
    )


__all__ = [
    "AMBIGUOUS_MEANING",
    "COMPILER_INVARIANT_VIOLATION",
    "MISSING_CAPABILITY",
    "MISSING_FIELD",
    "MISSING_HISTORY_SOURCE",
    "MISSING_VALUE",
    "UNLOWERABLE_TEMPORAL_CONSTRAINT",
    "UNSUPPORTED_SUBJECT",
    "UNSUPPORTED_TEMPORAL_PRECISION",
    "Diagnostic",
    "Outcome",
    "Recoverability",
    "UnknownDiagnosticCode",
    "compiler_invariant_violation",
    "declared_codes",
    "missing_capability",
    "missing_history_source",
    "outcome_for",
    "unsupported_subject",
    "unsupported_temporal_precision",
]
