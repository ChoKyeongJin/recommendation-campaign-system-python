"""카디널리티 주장의 근거 계약 — **집합 술어는 공유 근거로 증명된다**.

감사 #47(`이메일, 문자, 앱푸시 중 정확히 두 개 채널에 동의한 회원`)은 기준선에서 SQL 이
나오다가 되묻기로 퇴행했다. 조사 결과 검증기는 범인이 아니었고, 실제 차단은 둘이었다.

    GATE 6  모델이 ``"consent_count"`` 라는 문자열을 정확히 써야 구제가 열린다
    GATE 7  문장의 **모든** 리터럴이 수량자 매치 안에 있어야 한다

둘 다 우연(모델 어휘·문장에 다른 절이 있는지)에 기능이 달려 있었다. 이 파일은 그 우연이
다시 들어오지 못하게 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import consent_cardinality  # noqa: E402
from query_structurer.semantic_ir import extract_literal_bindings  # noqa: E402

BARE = "이메일, 문자, 앱푸시 중 정확히 두 개 채널에 동의한 회원을 찾아줘."
# 무관한 리터럴이 하나 더 있는 같은 요청. GATE 7 이 죽인 모양이다.
WITH_OTHER_CLAUSE = (
    "최근 30일 동안 구매하고 이메일, 문자, 앱푸시 중 정확히 두 개 채널에 동의한 회원을 찾아줘."
)


def snapshot():
    return audience_runtime.catalog_snapshot()


def bindings(query: str) -> list:
    return list(extract_literal_bindings(query, current_date="2026-08-08"))


# ── typed 주장(T12) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", [BARE, WITH_OTHER_CLAUSE])
def test_the_claim_is_typed_semantics_not_a_model_string(query: str) -> None:
    """주장은 **원문**에서 읽힌다 — 모델이 표현을 내지 못해도 그대로 있다."""

    claim = consent_cardinality.detect_cardinality_claim(query, snapshot())
    assert claim is not None
    assert claim.domain == consent_cardinality.CONSENT_CHANNEL_DOMAIN
    assert claim.operator == "exact"
    assert claim.count == 2
    assert len(claim.members) == 3


def test_the_claim_owns_all_three_semantic_members() -> None:
    """GATE 7 이 물어야 하는 질문. 멤버마다 스팬이 아니라 **집합이 멤버를 다 갖는가**다."""

    claim = consent_cardinality.detect_cardinality_claim(BARE, snapshot())
    assert claim is not None
    fields = snapshot()["fields"]
    for member in claim.members:
        assert fields[member]["value_domain"] == "consent_flag"
    assert len(set(claim.members)) == 3


def test_the_quantifier_phrase_is_one_shared_evidence_span() -> None:
    """카디널리티는 필드 **집합**에 걸리는 술어다 — 세 멤버가 각자 근거를 가질 필요가 없다."""

    claim = consent_cardinality.detect_cardinality_claim(BARE, snapshot())
    assert claim is not None
    start, end = claim.quantifier_span
    assert BARE[start:end].strip()
    # 공유 근거 하나가 세 멤버 전부를 증명한다.
    assert claim.footprint[0] <= start and end <= claim.footprint[1]


# ── 무관한 절이 구제를 죽이지 않는다(T14) ────────────────────────────────────────


def test_an_unrelated_literal_no_longer_kills_the_rescue() -> None:
    """``최근 30일`` 은 **다른 절**의 리터럴이다. 이 주장이 책임질 것이 아니다."""

    rows = bindings(WITH_OTHER_CLAUSE)
    assert rows, "무관한 리터럴이 실제로 추출돼야 이 테스트가 의미를 갖는다"
    expression = consent_cardinality.synthesize_exact_consent_cardinality(
        WITH_OTHER_CLAUSE, rows, snapshot()
    )
    assert expression is not None
    validation = consent_cardinality.validate_consent_cardinality(
        WITH_OTHER_CLAUSE, expression, rows, snapshot()
    )
    assert validation is not None and validation.equivalent


def test_the_claim_does_not_own_another_clause_literal() -> None:
    """사정거리는 수량자 + 멤버 표면어뿐이다 — 다른 절까지 삼키면 그 절이 조용히 사라진다."""

    claim = consent_cardinality.detect_cardinality_claim(WITH_OTHER_CLAUSE, snapshot())
    assert claim is not None
    period = WITH_OTHER_CLAUSE.index("최근 30일")
    assert not claim.owns(period, period + len("최근 30일"))


# ── evidence 는 능력 판정을 덮지 않는다(T15) ─────────────────────────────────────


def test_evidence_metadata_does_not_change_executability() -> None:
    """근거 스팬 metadata 가 달라도 **같은 표현**이 나온다.

    evidence 는 provenance·디버깅·추적·모호성 탐지용이다. semantic IR 과 능력이 같은데
    substring 이 부족하다는 이유만으로 실행 가능 여부가 바뀌면, 근거 회계가 능력 판정을
    덮어쓴 것이다.
    """

    rows = bindings(BARE)
    first = consent_cardinality.synthesize_exact_consent_cardinality(BARE, rows, snapshot())
    # 리터럴 원장을 비워도(= 근거 metadata 를 잃어도) 같은 표현이 나온다.
    second = consent_cardinality.synthesize_exact_consent_cardinality(BARE, [], snapshot())
    assert first is not None and second is not None
    assert first.to_dict() == second.to_dict()


def test_a_truth_table_mismatch_still_blocks() -> None:
    """완화한 것은 **엉뚱한 절의 책임**뿐이다 — 뜻이 다른 표현은 그대로 막힌다."""

    import event_ir

    wrong = event_ir.Or((
        event_ir.Comparison(
            "=",
            event_ir.FieldRef("subject.email_consent"),
            event_ir.Literal("agreed"),
            evidence=event_ir.Evidence(BARE, 0, len(BARE)),
        ),
    ))
    validation = consent_cardinality.validate_consent_cardinality(
        BARE, wrong, bindings(BARE), snapshot()
    )
    assert validation is not None
    assert not validation.equivalent
    assert validation.reason
