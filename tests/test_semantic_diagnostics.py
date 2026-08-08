"""진단 → 귀결 파생의 계약. **표 하나가 유일한 파생 경로**임을 고정한다."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import semantic_diagnostics as sd  # noqa: E402


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (sd.MISSING_VALUE, sd.Outcome.CLARIFICATION),
        (sd.AMBIGUOUS_MEANING, sd.Outcome.CLARIFICATION),
        (sd.MISSING_FIELD, sd.Outcome.UNSUPPORTED),
        (sd.MISSING_CAPABILITY, sd.Outcome.UNSUPPORTED),
        (sd.MISSING_HISTORY_SOURCE, sd.Outcome.UNSUPPORTED),
        (sd.UNSUPPORTED_SUBJECT, sd.Outcome.UNSUPPORTED),
        (sd.UNSUPPORTED_TEMPORAL_PRECISION, sd.Outcome.UNSUPPORTED),
        (sd.UNLOWERABLE_TEMPORAL_CONSTRAINT, sd.Outcome.UNSUPPORTED),
        (sd.COMPILER_INVARIANT_VIOLATION, sd.Outcome.INTERNAL_FAILURE),
    ],
)
def test_every_code_declares_its_outcome(code: str, expected: sd.Outcome) -> None:
    diagnostic = sd.Diagnostic(
        code=code,
        user_action="…",
        developer_detail="…",
        recoverability=sd.Recoverability.USER,
    )
    assert sd.outcome_for(diagnostic) is expected
    assert diagnostic.outcome is expected


def test_unknown_code_fails_loudly_instead_of_defaulting() -> None:
    """조용한 기본값을 주면 새 진단이 없는 한계를 광고하거나 고칠 수 없는 것을 되묻는다."""

    unknown = sd.Diagnostic(
        code="not_declared",
        user_action="…",
        developer_detail="…",
        recoverability=sd.Recoverability.ENGINEERING,
    )
    with pytest.raises(sd.UnknownDiagnosticCode) as caught:
        sd.outcome_for(unknown)
    assert "not_declared" in str(caught.value)


def test_declared_codes_and_module_constants_do_not_drift() -> None:
    constants = {
        value
        for name, value in vars(sd).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("_")
    }
    assert sd.declared_codes() <= constants


# ── 비-SQL 귀결의 완결성 ────────────────────────────────────────────────────────


def test_every_diagnostic_carries_a_user_sentence_and_a_developer_detail() -> None:
    """모든 비-SQL 결과에는 사용자 문장과 개발자 상세가 **둘 다** 있어야 한다.

    ``clarification_questions = []`` · ``unsupported_reason = null`` · 내부 문자열만 남는
    ``failure_reason`` 이 이 계약이 없던 자리의 실제 모습이었다(감사 #44).
    """

    built = [
        sd.missing_capability(
            capability="supports_all_occurrences",
            symbol="login",
            user_action="사용자 문장",
            developer_detail="개발자 상세",
        ),
        sd.missing_history_source(
            symbol="MEMBER_STATE_CD",
            user_action="사용자 문장",
            developer_detail="개발자 상세",
        ),
        sd.unsupported_subject(requested="brand", supported=("member",)),
        sd.unsupported_temporal_precision(requested="hour", supported="day"),
        sd.compiler_invariant_violation(symbol="x", developer_detail="상세"),
    ]
    for diagnostic in built:
        assert diagnostic.user_action.strip(), diagnostic.code
        assert diagnostic.developer_detail.strip(), diagnostic.code
        assert diagnostic.outcome in set(sd.Outcome)


def test_unsupported_subject_is_not_an_internal_failure() -> None:
    """감사 #44. 주체가 다른 요청은 배선 결함이 아니라 정직한 미지원이다."""

    diagnostic = sd.unsupported_subject(requested="brand", supported=("member",))
    assert diagnostic.outcome is sd.Outcome.UNSUPPORTED
    assert diagnostic.outcome is not sd.Outcome.INTERNAL_FAILURE
    assert "brand" in diagnostic.user_action


def test_temporal_precision_does_not_ask_for_a_period_the_user_gave() -> None:
    """감사 #82. ``최근 24시간`` 은 기간을 **말했다** — 되묻는 것이 아니라 해상도를 말한다."""

    diagnostic = sd.unsupported_temporal_precision(requested="hour", supported="day")
    assert diagnostic.outcome is sd.Outcome.UNSUPPORTED
    assert "기간 값이 없" not in diagnostic.user_action


def test_diagnostic_round_trips_to_a_transportable_dict() -> None:
    diagnostic = sd.missing_capability(
        capability="supports_all_occurrences",
        symbol="login",
        clause_id="clause@4-7",
        evidence="로그인",
        available=("member.login.latest",),
        user_action="사용자 문장",
        developer_detail="개발자 상세",
    )
    payload = diagnostic.to_dict()
    assert payload["code"] == sd.MISSING_CAPABILITY
    assert payload["outcome"] == "unsupported"
    assert payload["clause_id"] == "clause@4-7"
    assert payload["available_capability"] == ["member.login.latest"]
