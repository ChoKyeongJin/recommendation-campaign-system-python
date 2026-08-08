"""존재/부재 한정어의 **표면 어휘 계약** — 어형은 늘어도 뜻은 하나다.

이 파일이 재는 것은 넷이다.

1. **정규화** — 같은 뜻의 여러 어형이 같은 canonical 연산자로 간다. 어형마다 정규식을 하나씩
   덧붙이는 방식을 막으려면, 새 어형이 표 한 줄로 들어오고 그 사실이 여기서 고정돼야 한다.
2. **충돌 격리** — `적어도`·`최소`·`이상` 은 이미 **수치 비교 축의 소유어**다. 표면 문자열
   하나로 뜻을 정하면 '적어도 3번 구매'가 시간 존재 한정으로 오탐된다.
3. **소유권** — '골드 이상'의 `이상` 은 등급 비교가 소유하고, 같은 문장의 '10만원 이상'은
   금액의 것으로 남는다. 인접성이 그 판정의 유일한 근거다.
4. **전수 가드** — 선언된 시간 연산자마다 표면형이 있거나, 없다는 사실이 사유와 함께
   명시돼 있다. 새 연산자를 선언만 하고 감지기를 잊으면 여기서 걸린다.

의도적으로 SQL 을 재지 않는다 — 종단 계약은 :mod:`tests.test_temporal_claims_wiring` 이
소유한다. 여기서 재는 것은 그 앞 단계(원문 → 마커 → 청구)뿐이다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_frame  # noqa: E402
import audience_runtime  # noqa: E402
import targeting_domain  # noqa: E402
import temporal_claims  # noqa: E402
import temporal_ir  # noqa: E402
import temporal_semantics  # noqa: E402
from query_structurer import semantic_ir as query_semantic_ir  # noqa: E402

TODAY = date(2026, 8, 5)

# 표면형이 없는 연산자 — **없다는 사실을 여기서 선언**한다. 목록이 비어 있지 않은 것 자체는
# 결함이 아니지만, 사유 없이 늘어나면 "감지기를 잊었다"와 구별되지 않는다.
SURFACELESS_OPERATORS: dict[str, str] = {
    # 이 데이터 계약에서 '구간 안 어느 시점에서든'은 '구간 중 최소 한 관측'과 같은 뜻이라
    # (스냅샷 한 행 = 한 칸), 도메인이 그 어형을 AT_LEAST_ONCE_IN_INTERVAL 로 모은다.
    # 두 연산자에 같은 낱말을 나눠 주면 같은 문장이 회차마다 다른 이름으로 끝난다.
    temporal_semantics.WITHIN_INTERVAL: (
        "AT_LEAST_ONCE_IN_INTERVAL 과 같은 뜻이므로 어형을 그쪽 하나가 소유한다"
    ),
}


@pytest.fixture(scope="module")
def lexicon() -> temporal_semantics.TemporalLexicon:
    return targeting_domain.temporal_lexicon()


@pytest.fixture(scope="module")
def snapshot():
    return audience_runtime.catalog_snapshot()


@pytest.fixture(scope="module")
def semantic_catalog():
    return audience_runtime.resolve_audience_catalog()


@pytest.fixture(scope="module")
def runtime(semantic_catalog) -> temporal_ir.TemporalRuntime:
    return temporal_ir.create_temporal_runtime(semantic_catalog)


def _operators(lexicon: temporal_semantics.TemporalLexicon, query: str) -> list[str]:
    return [marker.operator for marker in lexicon.detect(query)]


# ── 1. 정규화: 어형이 달라도 연산자는 하나 ──────────────────────────────────────

AT_LEAST_ONCE_SURFACES: tuple[str, ...] = (
    "최근 6개월 동안 한 번이라도 골드였던 회원",
    "최근 6개월 동안 한번이라도 골드였던 회원",
    "최근 6개월 동안 한 차례라도 골드였던 회원",
    "최근 6개월 중 한 달이라도 골드였던 회원",
    "최근 6개월 중 적어도 한 달은 골드였던 회원",
    "최근 6개월 중 적어도 한 번 골드였던 회원",
    "최근 6개월 중 최소 한 번 골드였던 회원",
    "최근 6개월 중 최소한 한 번 골드였던 회원",
    "최근 6개월 중 한 번 이상 골드였던 회원",
    "최근 6개월 중 골드였던 적이 있는 회원",
)

NEVER_SURFACES: tuple[str, ...] = (
    "최근 6개월 동안 한 번도 골드가 아니었던 회원",
    "최근 6개월 동안 한 번도 골드이지 않았던 회원",
    "최근 6개월 동안 한 번도 골드였던 적이 없는 회원",
    "최근 6개월 동안 한 차례도 골드였던 적이 없는 회원",
    "최근 6개월 중 골드였던 적이 없는 회원",
)


@pytest.mark.parametrize("query", AT_LEAST_ONCE_SURFACES)
def test_every_at_least_once_surface_reaches_one_operator(
    lexicon: temporal_semantics.TemporalLexicon, query: str
) -> None:
    """'최소 하나'의 어형 전부가 같은 연산자로 정규화된다."""

    assert temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL in _operators(lexicon, query)


@pytest.mark.parametrize("query", NEVER_SURFACES)
def test_every_never_surface_reaches_one_operator(
    lexicon: temporal_semantics.TemporalLexicon, query: str
) -> None:
    """'한 번도 …없/않/아니' 의 어형 전부가 부재 연산자로 정규화된다.

    부정 꼬리에 '없'이 없던 동안 '한 번도 VIP였던 적이 없는'은 마커를 0개 냈고, 그래서
    미지원 사유조차 남지 않았다(2026-08-08 실측).
    """

    assert temporal_semantics.NEVER_IN_INTERVAL in _operators(lexicon, query)


def test_the_two_polarities_never_fire_together(
    lexicon: temporal_semantics.TemporalLexicon,
) -> None:
    """존재와 부재가 같은 구절에서 함께 잡히면 극성이 뒤집힌 SQL 이 나갈 수 있다."""

    for query in (*AT_LEAST_ONCE_SURFACES, *NEVER_SURFACES):
        operators = set(_operators(lexicon, query))
        assert not {
            temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL,
            temporal_semantics.NEVER_IN_INTERVAL,
        } <= operators, query


# ── 2. 충돌 격리: 수치 비교 축의 낱말을 훔치지 않는다 ────────────────────────────

NUMERIC_QUANTIFIER_QUERIES: tuple[str, ...] = (
    # 수가 1이 아니다 — '적어도/최소'가 있어도 시간 존재 한정이 아니다.
    "최근 6개월 동안 적어도 3번 구매한 회원",
    "최소 2회 방문한 회원",
    "최소 2개월은 VIP였던 회원",
    # 비교 대상이 금액·기간이다.
    "10만원 이상 구매한 회원",
    "3개월 이상 유지한 회원",
    # 머리가 속성 값이 아니다('한 번' 은 맞지만 뒤에 오는 것이 사건이다).
    "적어도 한 번 구매한 회원",
    "한 번이라도 로그인한 회원",
    # 맨 '한 달'은 창이다 — 최소성 표지가 없다.
    "최근 한 달 골드였던 회원",
)


@pytest.mark.parametrize("query", NUMERIC_QUANTIFIER_QUERIES)
def test_numeric_quantifier_words_do_not_become_temporal_markers(
    lexicon: temporal_semantics.TemporalLexicon, query: str
) -> None:
    """'적어도·최소·이상'은 수가 1이고 머리가 속성 값일 때만 시간 존재 한정이다."""

    fired = set(_operators(lexicon, query)) & {
        temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL,
        temporal_semantics.NEVER_IN_INTERVAL,
    }
    assert not fired, f"{query!r} 에서 오탐: {sorted(fired)}"


def test_a_scope_marker_does_not_lend_its_tail_to_another_clause(
    lexicon: temporal_semantics.TemporalLexicon,
) -> None:
    """'골드 회원 중 구매한 적이 없는'의 '적이 없는'은 등급의 꼬리가 아니다.

    경험형 꼬리는 절 경계 어휘를 건너뛰지 못한다 — 그 어휘의 소유자는 lexicon_patterns 다.
    """

    assert temporal_semantics.NEVER_IN_INTERVAL not in _operators(
        lexicon, "골드 회원 중 구매한 적이 없는 회원"
    )


# ── 3. 절 경계: '중'은 혼자서 절을 가르지 않는다(R1) ────────────────────────────


def test_scope_marker_alone_is_not_a_clause_boundary() -> None:
    """'6개월 중 …'의 기간과 조건은 한 절이다 — '동안'과 같아야 한다."""

    scoped = "최근 6개월 중 한 번이라도 골드였던 회원"
    during = "최근 6개월 동안 한 번이라도 골드였던 회원"

    assert audience_frame.in_same_clause(scoped, (3, 6), (9, 18)) is True
    assert audience_frame.in_same_clause(during, (3, 6), (10, 19)) is True


def test_a_member_noun_with_a_scope_marker_still_is_a_clause_boundary() -> None:
    """'회원 중'은 여전히 경계다 — 결합형이 경계라는 계약을 지운 것이 아니다."""

    query = "장바구니에 담은 회원 중 구매 이력이 없는"

    assert audience_frame.in_same_clause(query, (0, 4), (13, 17)) is False
    assert any(
        term.startswith("회원") for term in audience_frame.scoped_boundary_terms()
    )


# ── 4. 기간인가 세는 수인가(R2) ─────────────────────────────────────────────────

PERIOD_CASES: tuple[tuple[str, tuple[tuple[int, str], ...]], ...] = (
    # 조사가 붙은 단어형 기간 명사는 창이 아니라 세는 수다(calendar_window 가 선언한 계약).
    ("적어도 한 달은 골드 이상이었던 회원", ()),
    # 숫자형·독립 단어형은 창이다.
    ("최근 6개월 중 한 번이라도 골드였던 회원", ((6, "months"),)),
    ("지난 3개월 동안 VIP였던 회원", ((3, "months"),)),
    ("최근 한 달 동안 골드였던 회원", ((1, "months"),)),
)


@pytest.mark.parametrize(("query", "expected"), PERIOD_CASES)
def test_duration_guard_runs_on_the_claim_path(
    query: str, expected: tuple[tuple[int, str], ...]
) -> None:
    """단어형 기간 낱말 경계 가드가 **실제로 돈다**.

    가드는 원문과 좌표 대응표를 받아야 판정할 수 있는데, 청구 경로가 압축 텍스트만 넘기던
    동안 한 번도 돌지 않았다 — 선언된 가드가 배선되지 않은 전형이다(2026-08-08 실측).
    """

    found = tuple(
        (int(window["value"]), str(window["unit"]))
        for _span, window, _kind in temporal_claims._period_candidates(query, TODAY)
    )
    assert found == expected


# ── 5. 서열 비교의 소유권(R3) ───────────────────────────────────────────────────


def _state_comparison(query: str, snapshot, semantic_catalog, runtime):
    requests = temporal_claims.detect_temporal_claims(
        query, snapshot=snapshot, catalog=semantic_catalog, runtime=runtime, today=TODAY
    )
    assert isinstance(requests, tuple) and requests, requests
    predicate = requests[0].condition.predicate
    return predicate.comparison, requests[0].spans


def test_an_adjacent_ordered_operator_is_consumed_by_the_state_claim(
    snapshot, semantic_catalog, runtime
) -> None:
    """'골드 이상'의 '이상'은 등급 비교가 소유하고, 그 구간을 신고한다.

    연산자만 바꾸고 구간을 신고하지 않으면 리터럴 정산 게이트가 미소비로 문장을 막는다.
    """

    query = "최근 6개월 중 한 번이라도 골드 이상이었던 회원"
    comparison, spans = _state_comparison(query, snapshot, semantic_catalog, runtime)

    assert comparison.operator == ">="
    assert comparison.value == "gold_grade"
    start = query.index("이상")
    assert (start, start + 2) in spans


def test_a_distant_ordered_operator_is_left_to_its_own_clause(
    snapshot, semantic_catalog, runtime
) -> None:
    """'골드였고 10만원 이상'의 '이상'은 금액의 것이다 — 등급은 등호를 유지한다."""

    query = "최근 6개월 중 한 번이라도 골드였고 10만원 이상 구매한 회원"
    comparison, spans = _state_comparison(query, snapshot, semantic_catalog, runtime)

    assert comparison.operator == "="
    start = query.index("이상")
    assert all(not (s <= start < e) for s, e in spans)


def test_an_unordered_domain_never_gets_an_inequality(
    snapshot, semantic_catalog, runtime
) -> None:
    """순서가 선언되지 않은 축('정상/휴면')에는 부등호를 만들지 않는다.

    사전식 비교라는 다른 뜻이 조용히 들어오는 자리다. 도메인 선언이 유일한 근거다.
    """

    state_domain = (snapshot.get("value_domains") or {}).get("member_state") or {}
    assert state_domain.get("ordered") is not True

    outcome = temporal_claims.detect_temporal_claims(
        "최근 6개월 동안 한 번이라도 휴면 이상이었던 회원",
        snapshot=snapshot,
        catalog=semantic_catalog,
        runtime=runtime,
        today=TODAY,
    )
    if isinstance(outcome, tuple):
        for request in outcome:
            comparison = getattr(request.condition.predicate, "comparison", None)
            assert comparison is None or comparison.operator == "="


def test_the_owned_comparison_span_matches_the_literal_ledger() -> None:
    """소유 신고 구간이 리터럴 원장의 구간과 **글자 단위로** 같다.

    두 곳이 같은 표(:func:`condition_normalizers.comparison_literal_operators`)를 읽지만
    정규식을 각자 만든다. 구성 방식이 갈리면 소유 신고가 원장의 리터럴을 덮지 못해
    미소비로 막히므로, 동일성을 여기서 고정한다.
    """

    for query in (
        "최근 6개월 중 한 번이라도 골드 이상이었던 회원",
        "최근 6개월 중 한 번이라도 골드였고 10만원 이상 구매한 회원",
        "등급이 3회 이상 변경된 회원",
    ):
        ledger = {
            (binding["start"], binding["end"], binding["normalized"])
            for binding in query_semantic_ir.extract_literal_bindings(query)
            if binding.get("kind") == "comparison_operator"
        }
        scanned = {
            (
                match.start(),
                match.end(),
                temporal_claims.condition_normalizers.comparison_literal_operators()[
                    match.group(0)
                ],
            )
            for match in temporal_claims._COMPARISON_OPERATOR_RE.finditer(query)
        }
        assert scanned == ledger, query


# ── 6. 전수 가드: 선언된 연산자마다 표면형이 있거나, 없다는 선언이 있다 ──────────


def test_every_declared_temporal_operator_has_a_surface_or_a_reason(
    lexicon: temporal_semantics.TemporalLexicon,
) -> None:
    """감지기를 잊은 연산자와 **일부러 표면형을 두지 않은** 연산자를 구별한다.

    이 가드가 없으면 새 연산자가 선언만 된 채 남고, 그 의미를 말한 문장은 어디에서도
    잡히지 않아 '미지원'조차 되지 못한다(청구 0 → 사유 0).
    """

    declared = set(temporal_semantics.OPERATORS)
    with_surface = set(lexicon.operators)

    assert with_surface <= declared, sorted(with_surface - declared)
    assert set(SURFACELESS_OPERATORS) <= declared, sorted(
        set(SURFACELESS_OPERATORS) - declared
    )
    # 표면형이 있으면서 '없다'고 선언된 연산자는 선언이 낡은 것이다.
    assert not (with_surface & set(SURFACELESS_OPERATORS)), sorted(
        with_surface & set(SURFACELESS_OPERATORS)
    )
    missing = declared - with_surface - set(SURFACELESS_OPERATORS)
    assert not missing, (
        f"표면형도 사유도 없는 시간 연산자: {sorted(missing)}. "
        "targeting_domain 에 마커 템플릿을 더하거나 SURFACELESS_OPERATORS 에 사유를 적어라."
    )


def test_every_surface_bearing_operator_has_an_ir_plan(
    lexicon: temporal_semantics.TemporalLexicon,
) -> None:
    """마커가 잡히는 연산자는 IR 조합 선언도 있어야 한다.

    한쪽만 있으면 감지는 되는데 청구가 서지 않아 ``temporal_operator_plan_missing`` 으로
    끝난다 — 표면 어휘를 늘릴 때 함께 빠뜨리기 쉬운 짝이다.
    """

    missing = sorted(set(lexicon.operators) - set(temporal_claims._OPERATOR_PLANS))
    assert not missing, missing


def test_quantifier_surface_tables_are_declarative() -> None:
    """어형은 표에 있고 정규식에는 없다 — 새 어형이 표 한 줄로 들어오는지의 계약.

    표에서 낱말을 빼면 그 어형이 즉시 잡히지 않아야 한다. 정규식에 낱말이 이중으로
    적혀 있으면 이 검사가 통과하지 못한다.
    """

    assert "차례" in targeting_domain._OCCURRENCE_UNITS
    assert "적어도" in targeting_domain._AT_LEAST_PREFIXES
    assert "없" in targeting_domain._EXISTENCE_NEGATION_TAILS

    for table in (
        targeting_domain._OCCURRENCE_UNITS,
        targeting_domain._AT_LEAST_PREFIXES,
        targeting_domain._AT_LEAST_SUFFIXES,
        targeting_domain._NEVER_SUFFIXES,
        targeting_domain._EXISTENCE_NEGATION_TAILS,
        targeting_domain._EXISTENCE_AFFIRMATION_TAILS,
        targeting_domain._EXPERIENCE_NOUNS,
    ):
        assert table and all(isinstance(term, str) and term for term in table)

    # 존재/부재 두 연산자의 템플릿에는 낱말이 하나도 없어야 한다 — 전부 치환자다.
    # (다른 연산자의 템플릿은 이 계약의 대상이 아니다: 예를 들어 CHANGE_COUNT 는 '3회 이상
    #  변경'이라는 **다른 구조**를 읽으며 그 '이상'은 횟수 임계의 것이다. 그 축을 같은 표로
    #  모으는 것은 별개 작업이다.)
    owned = "".join(
        template
        for operator, template in targeting_domain._TEMPORAL_MARKER_TEMPLATES
        if operator
        in {
            temporal_semantics.AT_LEAST_ONCE_IN_INTERVAL,
            temporal_semantics.NEVER_IN_INTERVAL,
        }
    )
    for term in (
        *targeting_domain._OCCURRENCE_UNITS,
        *targeting_domain._AT_LEAST_PREFIXES,
        *targeting_domain._AT_LEAST_SUFFIXES,
        *targeting_domain._NEVER_SUFFIXES,
        *targeting_domain._EXISTENCE_NEGATION_TAILS,
        *targeting_domain._EXISTENCE_AFFIRMATION_TAILS,
        *targeting_domain._EXPERIENCE_NOUNS,
    ):
        assert term not in owned, f"{term!r} 이 템플릿에 직접 적혀 있다(이중 소유)"
