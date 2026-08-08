"""'최근 / 현재 / 최신 / 직전 / 이전' — 낱말이 아니라 **머리**가 뜻을 정한다.

이 파일이 고정하는 것은 여섯 불변식이다.

    I1  선택자 낱말의 뜻은 그 낱말이 수식하는 머리(semantic head)가 정한다.
    I2  속성 축에 결합된 CURRENT/LATEST/PREVIOUS 는 기간이 아니라 관측 선택자다.
    I3  선택자의 낮춤은 달력 기본값이 아니라 **축의 데이터 계약**이 정한다.
    I4  선택자로 소비된 낱말에는 기간 결핍이 **만들어지지 않는다**(만든 뒤 취소가 아니다).
    I5  전이는 문형으로 탐지하지 않는다 — 같은 축의 CURRENT/LATEST + PREVIOUS 를 정규화해 만든다.
    I6  인식한 축·값·선택자는 조용히 사라지지 않는다. 못 내면 이름을 대며 미지원으로 남는다.

재는 것은 결과 행 수가 아니라 **의미**다. 등급 축 전이는 적재된 달(201701)에서 0건이므로
행으로는 아무것도 증명되지 않고, 증명할 수 있는 것은 어느 컬럼을 어느 창에서 비교하는가다.

실행: python -m pytest tests/test_observation_selector_claims.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_issue_contract  # noqa: E402
import audience_runtime  # noqa: E402
import canonical_audience_claims  # noqa: E402
import event_compiler  # noqa: E402
import sql_dialect  # noqa: E402
import targeting_domain  # noqa: E402
import temporal_claims  # noqa: E402
import temporal_ir  # noqa: E402
import temporal_semantics  # noqa: E402
from temporal_ir import registry as treg  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

TODAY = date(2026, 8, 8)
CURRENT_DATE = "2026-08-08"

# 이 배포의 등급 전이 계약(선언에서 온 물리 좌표). 문자열을 여기 적는 이유는 이 시험이
# 재는 대상이 바로 '어느 컬럼을 읽는가'이기 때문이다.
CURRENT_GRADE = "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'"
PREVIOUS_GRADE = "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'"
ANCHOR_BUCKET = "MS.YYYYMM >= '202608'"


@pytest.fixture(scope="module")
def semantic_catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def runtime(semantic_catalog) -> temporal_ir.TemporalRuntime:
    return temporal_ir.create_temporal_runtime(semantic_catalog)


@pytest.fixture(scope="module")
def snapshot():
    return audience_runtime.catalog_snapshot()


def _claim(query: str, semantic_catalog, runtime, snapshot):
    return temporal_claims.synthesize_temporal_claim(
        query,
        snapshot=snapshot,
        catalog=semantic_catalog,
        runtime=runtime,
        context=temporal_claims.request_context_for(CURRENT_DATE),
        today=TODAY,
    )


def _sql(query: str, semantic_catalog, runtime, snapshot) -> str:
    outcome = _claim(query, semantic_catalog, runtime, snapshot)
    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis), outcome
    context = semantic_catalog.compile_context(
        dialect=sql_dialect.get_dialect("tsql"), literals=True
    )
    return event_compiler.compile_expression(outcome.expression, context=context).sql


# ── I1·I2. 낱말이 아니라 머리가 뜻을 정한다 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("최근 상태가 VIP인 회원", targeting_domain.HEAD_ATTRIBUTE),
        ("현재 등급이 VIP인 회원", targeting_domain.HEAD_ATTRIBUTE),
        ("직전 등급이 골드인 회원", targeting_domain.HEAD_ATTRIBUTE),
        ("지금은 VIP인 회원", targeting_domain.HEAD_ATTRIBUTE),
    ],
)
def test_a_selector_word_on_an_attribute_head_is_an_observation_selector(
    query: str, expected: str
) -> None:
    tokens = targeting_domain.observation_selector_tokens(query)
    assert [token.head_kind for token in tokens] == [expected]
    assert all(token.observation for token in tokens)


@pytest.mark.parametrize(
    "query",
    [
        "최근 30일 구매한 회원",
        "최근 구매한 VIP 회원",
        "최근 3개월 동안 로그인한 회원",
        "지난달 상태가 VIP인 회원",
    ],
)
def test_a_selector_word_that_does_not_qualify_an_attribute_stays_a_period_word(
    query: str,
) -> None:
    """머리가 기간·사건이면 관측 선택자가 아니다 — 기존 기간 문법이 그대로 소유한다."""

    assert targeting_domain.observation_selector_tokens(query) == ()
    assert audience_issue_contract.observation_selector_spans(query) == ()


# ── I3·I5. 같은 뜻은 문형이 달라도 같은 canonical semantics 로 ──────────────────

SAME_MEANING = (
    "최근 상태가 VIP이고 직전 상태는 골드였던 회원",
    "현재 등급이 VIP이고 이전 등급이 GOLD",
    "직전에는 GOLD였는데 지금은 VIP",
    "최근 등급은 VIP, 이전 등급은 GOLD",
    # 문형 감지기가 이미 있던 갈래. 정규화 후에도 **같은 조건**이어야 한다.
    "골드에서 VIP로 승급한 회원",
)


@pytest.mark.parametrize("query", SAME_MEANING, ids=SAME_MEANING)
def test_every_phrasing_of_one_transition_lowers_to_one_row_comparison(
    query: str, semantic_catalog, runtime, snapshot
) -> None:
    """다섯 문형이 같은 canonical semantics 로 들어간다(I5).

    두 비교가 **한 EXISTS 안**에 있는 것이 전이의 존재 이유다 — 쪼개면 서로 다른 관측 행에서
    만족돼도 통과한다.
    """

    sql = _sql(query, semantic_catalog, runtime, snapshot)
    assert sql.count("EXISTS") == 1
    assert CURRENT_GRADE in sql
    assert PREVIOUS_GRADE in sql


def test_the_pair_becomes_one_transition_claim_not_two_state_claims(
    semantic_catalog, runtime, snapshot
) -> None:
    """정규화의 결과는 조건 두 개가 아니라 **전이 하나**다."""

    outcome = _claim(
        "최근 상태가 VIP이고 직전 상태는 골드였던 회원", semantic_catalog, runtime, snapshot
    )
    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis)
    assert [request.operator for request in outcome.requests] == [
        temporal_semantics.CHANGE_BETWEEN
    ]
    predicate = outcome.requests[0].condition.predicate
    assert isinstance(predicate, sir.TransitionPredicate)
    assert (predicate.from_value, predicate.to_value) == ("gold_grade", "vip")
    # 두 절의 근거가 모두 남는다 — 어느 쪽도 조용히 사라지지 않는다(I6).
    assert min(span[0] for span in outcome.spans) == 0
    assert max(span[1] for span in outcome.spans) >= len("최근 상태가 VIP이고 직전 상태는 골드")


# ── 짝이 없는 절은 각각 독립적으로 지원된다 ────────────────────────────────────


def test_a_previous_value_alone_reads_the_declared_previous_column(
    semantic_catalog, runtime, snapshot
) -> None:
    sql = _sql("직전 등급이 GOLD인 회원", semantic_catalog, runtime, snapshot)
    assert PREVIOUS_GRADE in sql
    assert CURRENT_GRADE not in sql
    # 직전 관측은 **기준 관측 행**이 들고 있는 값이다(앞 칸으로 옮기지 않는다).
    assert ANCHOR_BUCKET in sql


def test_a_current_value_alone_stays_with_the_current_value_asset(
    semantic_catalog, runtime, snapshot
) -> None:
    """'현재 등급이 VIP' 는 이력 조건이 아니다 — 이 계층은 소유하지 않는다.

    소유하지 않는 것과 **잃는 것**은 다르다(I6). 값과 축은 카탈로그 값 청구로 그대로 남아
    현재값 자산이 답하고, '현재'라는 낱말은 그 자산 위에서 동어반복이다.
    """

    query = "현재 등급이 VIP인 회원"
    assert _claim(query, semantic_catalog, runtime, snapshot) is None
    claims = canonical_audience_claims.catalog_value_claims(query, snapshot)
    assert any(claim.get("canonical") == "vip" for claim in claims)


def test_a_current_value_selector_records_no_history_obligation() -> None:
    """요구 원장은 '이력 관측이 필요하다'만 기록한다 — 현재값 조건은 그 요구가 아니다.

    기록하면 아무도 방면할 수 없는 의무가 남아, 이력이 필요 없는 문장이 미귀결로 막힌다.
    반대로 **이력이 정말 필요한** 절의 의무는 그대로 있어야 한다(둘을 함께 고정한다).
    """

    import semantic_requirements

    def kinds(query: str) -> list[str]:
        return [
            semantic_requirements.obligation_kind(item)
            for item in semantic_requirements.capture_source_semantic_obligations(query)
        ]

    assert kinds("현재 등급이 VIP인 회원") == []
    assert kinds("직전 등급이 GOLD인 회원") == [
        semantic_requirements.TEMPORAL_QUALIFIER_KIND
    ]
    # 달력 시점의 as_of 는 과거 관측을 골라야 하므로 여전히 이력 의무다.
    assert kinds("지난달 말 기준 VIP였던 회원") == [
        semantic_requirements.TEMPORAL_QUALIFIER_KIND
    ]


# ── I4. 선택자로 소비된 낱말에는 기간 결핍이 만들어지지 않는다 ─────────────────


def _period_issue(query: str, marker: str) -> dict[str, object]:
    start = query.index(marker)
    return {
        "code": "missing_argument",
        "argument": "period",
        "message": "기간이 필요합니다.",
        "evidence": {"text": marker, "start": start, "end": start + len(marker)},
    }


def test_a_selector_word_never_becomes_a_period_gap() -> None:
    query = "최근 상태가 VIP이고 직전 상태는 골드였던 회원"
    span = (query.index("최근"), query.index("최근") + 2)

    assert audience_issue_contract.period_span_is_observation_selector(query, span)
    assert audience_issue_contract.period_issue_is_observation_selector(
        query, _period_issue(query, "최근")
    )


def test_a_real_period_gap_is_still_reported() -> None:
    """기간을 요구하는 '최근'은 그대로 결핍이다 — 선택자 판정이 그 문을 열지 않는다."""

    query = "최근 구매한 VIP 회원"
    span = (query.index("최근"), query.index("최근") + 2)

    assert not audience_issue_contract.period_span_is_observation_selector(query, span)
    assert not audience_issue_contract.period_issue_is_observation_selector(
        query, _period_issue(query, "최근")
    )


def test_the_guard_no_longer_uses_the_word_under_judgment_as_its_own_evidence(
    snapshot,
) -> None:
    """자기 참조 제거: '최근 상태'의 '최근'이 스스로를 반증 근거로 삼지 못한다.

    같은 함수가 **진짜** 기간 한정어는 계속 세는지도 함께 고정한다 — 한쪽만 보면 이 수정은
    가드를 끄는 것과 구별되지 않는다.
    """

    owned = "최근 상태가 VIP인 회원"
    assert audience_issue_contract.fabricated_period_issue_for_current_catalog_value(
        owned, _period_issue(owned, "최근"), snapshot
    )

    qualified = "최근 30일 동안 VIP였던 회원"
    assert not audience_issue_contract.fabricated_period_issue_for_current_catalog_value(
        qualified, _period_issue(qualified, "최근"), snapshot
    )


def test_the_deterministic_validator_skips_selector_words() -> None:
    """호스트 검증기도 같은 판정을 본다(신고 생산자가 둘이면 답이 갈린다)."""

    import audience_validators

    query = "최근 상태가 VIP이고 직전 상태는 골드였던 회원"
    matches = list(audience_validators._INCOMPLETE_RECENCY_RE.finditer(query))
    assert matches, "이 문장에는 값 없는 '최근'이 있다(전제 확인)"
    assert all(
        audience_issue_contract.period_span_is_observation_selector(
            query, (match.start(), match.end())
        )
        for match in matches
    )


# ── 오탐 방지 ───────────────────────────────────────────────────────────────────


def test_two_different_axes_are_never_merged_into_a_transition(
    semantic_catalog, runtime, snapshot
) -> None:
    """'현재 등급이 VIP이고 직전 구매 상품은 골드 패키지' 는 전이가 아니다.

    '직전 구매'의 머리는 사건이므로 관측 선택자가 아니고, 따라서 짝이 되지 않는다. 결합
    기준이 절 위치나 문장 안 값의 존재였다면 여기서 등급 전이가 만들어졌을 것이다.
    """

    query = "현재 등급이 VIP이고 직전 구매 상품은 골드 패키지"
    assert [
        marker.operator for marker in targeting_domain.temporal_lexicon().detect(query)
    ] == [temporal_semantics.AS_OF]
    assert _claim(query, semantic_catalog, runtime, snapshot) is None


def test_an_axis_without_a_declared_previous_value_is_explicitly_unsupported(
    semantic_catalog, runtime, snapshot
) -> None:
    """직전 값을 선언하지 않은 축의 '직전 …'은 **조건 삭제가 아니라** 미지원이다(I6)."""

    outcome = _claim("직전 상태는 휴면이었던 회원", semantic_catalog, runtime, snapshot)
    assert isinstance(outcome, temporal_claims.TemporalClaimRejection)
    assert outcome.code == temporal_claims.METRIC_NOT_DECLARED
    assert "state" in outcome.message


def test_the_lowering_names_the_missing_declaration(semantic_catalog, runtime) -> None:
    """선언이 없으면 낮춤이 **어느 선언이 없는지** 말한다(조용한 미지원 금지)."""

    import dataclasses

    from temporal_ir import operators as top

    binding = runtime.temporal_catalog.binding("member.grade.monthly_snapshot")
    without_previous = dataclasses.replace(binding, prev_value_field=None)
    condition = sir.TemporalCondition(
        metric="member.grade",
        binding=binding.id,
        selector=sir.PreviousSelector(
            anchor=sir.ReferenceAnchor(), previous_kind=sir.PreviousKind.OBSERVATION
        ),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.StatePredicate(comparison=sir.Comparison(operator="=", value="vip")),
        evidence=sir.Evidence(text="직전 등급", start=0, end=5),
    )
    codes = [issue.code for issue in top._previous_observation_issues(condition, without_previous)]
    assert "temporal_previous_value_unavailable" in codes
    # 선언이 있으면 같은 조건이 통과한다(가드가 항상 닫혀 있는 것이 아니다).
    assert top._previous_observation_issues(condition, binding) == ()


# ── I3. 저장소 조회 방식은 축의 데이터 계약이 정한다 ────────────────────────────


def test_the_logical_selector_and_the_storage_lookup_stay_separate(
    runtime, semantic_catalog
) -> None:
    """같은 논리 선택자라도 어느 컬럼을 읽는지는 선언이 정한다."""

    binding = runtime.temporal_catalog.binding("member.grade.monthly_snapshot")
    previous = sir.TemporalCondition(
        metric="member.grade",
        binding=binding.id,
        selector=sir.PreviousSelector(
            anchor=sir.ReferenceAnchor(), previous_kind=sir.PreviousKind.OBSERVATION
        ),
        quantifier=sir.ExistsQuantifier(),
        predicate=sir.StatePredicate(comparison=sir.Comparison(operator="=", value="vip")),
        evidence=sir.Evidence(text="직전 등급", start=0, end=5),
    )
    assert treg.state_value_field(previous, binding) == "member_month_snapshot.prev_grade"

    bucket = sir.TemporalCondition(
        metric=previous.metric,
        binding=binding.id,
        selector=sir.PreviousSelector(
            anchor=sir.ReferenceAnchor(), previous_kind=sir.PreviousKind.BUCKET
        ),
        quantifier=previous.quantifier,
        predicate=previous.predicate,
        evidence=previous.evidence,
    )
    assert treg.state_value_field(bucket, binding) == "member_month_snapshot.grade"


def test_the_value_axis_decides_the_columns_not_the_axis_word(
    semantic_catalog, runtime, snapshot
) -> None:
    """축 낱말('상태')이 아니라 **값의 도메인**이 컬럼을 정한다 — 가치등급은 다른 컬럼이다."""

    sql = _sql("가치등급이 골드에서 VIP로 바뀐 회원", semantic_catalog, runtime, snapshot)
    assert "MS.WORTH_GRADE = 'VIP'" in sql
    assert "MS.PREV_WORTH_GRADE = 'GOLD'" in sql


# ── 적재 범위는 의미를 바꾸지 않는다 ────────────────────────────────────────────


def test_out_of_coverage_is_a_warning_not_a_reinterpretation(
    semantic_catalog, runtime, snapshot
) -> None:
    """적재 월이 앵커와 달라도 창을 옮기지 않는다 — 사실은 경고로 드러낸다."""

    outcome = _claim(
        "최근 상태가 VIP이고 직전 상태는 골드였던 회원", semantic_catalog, runtime, snapshot
    )
    assert isinstance(outcome, temporal_claims.TemporalClaimSynthesis)
    assert "out_of_coverage" in outcome.warnings
    sql = _sql(
        "최근 상태가 VIP이고 직전 상태는 골드였던 회원", semantic_catalog, runtime, snapshot
    )
    # 요청한 시점은 기준일의 칸이다. 적재된 달(201701)로 조용히 옮기지 않는다.
    assert ANCHOR_BUCKET in sql
    assert "201701" not in sql


# ── 기존 문법 회귀 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    ["최근 30일 구매한 회원", "지난달 상태가 VIP인 회원"],
)
def test_period_and_calendar_queries_stay_outside_this_layer(
    query: str, semantic_catalog, runtime, snapshot
) -> None:
    """기간·달력 질의는 이 변경의 대상이 아니다 — 선택자 판정이 그 자리를 가져가지 않는다."""

    assert _claim(query, semantic_catalog, runtime, snapshot) is None


def test_a_period_bound_transition_keeps_its_window(
    semantic_catalog, runtime, snapshot
) -> None:
    """기간이 붙은 전이는 그 구간을 그대로 읽는다(관측 선택자로 접히지 않는다)."""

    sql = _sql(
        "최근 6개월 동안 골드에서 VIP로 승급한 회원", semantic_catalog, runtime, snapshot
    )
    assert "MS.YYYYMM >= '202603'" in sql
    assert "MS.YYYYMM < '202609'" in sql
