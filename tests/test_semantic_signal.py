"""의미 신호 계층의 계약 — 뜻으로 판정하고, 한 번 구조화하고, 폴백은 보수적이다.

이 스위트가 지키는 것은 여섯이다.

  1. **뜻으로 판정** 활용형·유의어·명사형·간접 표현이 같은 신호가 되고, 목록에 없던 새 말투도
     코드 변경 없이 같은 뜻으로 흐른다(표현형은 여기 테스트 데이터에만 쌓인다).
  2. **뭉치지 않음** 실제 발생·과거 이력·진행·의향·가정·단순 언급·부정이 서로 다른 상태다.
     ``detected`` 는 :func:`semantic_signal.detected_for` 한 곳에서만 계산된다.
  3. **범위 있는 부정** 부정어의 존재가 아니라 부정이 **무엇에 걸리는지**로 판정한다.
     한 문장에서 대상마다 답이 다를 수 있다.
  4. **재작성 보존** 표현만 바뀐 재작성은 같은 뜻이고, 뜻이 사라지거나 없던 뜻이 생기면 잡힌다.
     재작성본 문자열을 같은 키워드로 다시 검사하지 않는다.
  5. **폴백은 우선순위** 검증된 구조화 결과가 있으면 하위 폴백을 보지 않는다. 스키마 위반·호출
     실패는 조용한 false 가 아니라 하위 폴백 또는 unknown 이다.
  6. **메타데이터는 의미가 아니다** 출처·모델·소요시간이 달라도 뜻이 같으면 같다.

표현형 목록은 **구현이 아니라 여기**에 있다. 새 표현이 들어오면 이 파일에 줄을 더하는 것으로
검증하고, 같은 뜻이면 구현은 바뀌지 않아야 한다.
"""

from __future__ import annotations

import pytest

import graph_rag
import purchase_lexicon
import semantic_signal as ss

SIGNAL = purchase_lexicon.SIGNAL
SPEC = ss.spec_for(SIGNAL)


# ── 판정기 스텁 ────────────────────────────────────────────────────────────────────────────
def _payload(status: str, evidence: str, entities: list[tuple[str, str]] | None = None) -> dict:
    """뜻을 읽어낸 추출기가 돌려주는 구조화 응답. 낱말이 아니라 상태를 돌려준다는 점이 핵심이다."""
    return {
        "signal": SIGNAL,
        "status": status,
        "negated": status == ss.DENIED,
        "evidence": evidence,
        "entities": [{"entity": name, "status": item_status} for name, item_status in (entities or [])],
    }


def _extractor(payload):
    """호출 텍스트와 무관하게 준비된 응답을 돌려주는 추출기(계약·정책 검증용)."""
    return lambda _text, _spec: payload


def _judge(text: str, payload) -> ss.SemanticSignal:
    return ss.resolve(text, SIGNAL, extract=_extractor(payload), rules=purchase_lexicon.rule_signal)


# ── A. 실제 발생: 활용형·유의어·명사형·대표 키워드 없는 표현 ────────────────────────────────
# 표면형은 제각각이지만 뜻은 하나다. 사전에 없는 말투('질렀다'·'장만했다'·'들였다')가 섞여 있는 것이
# 요점이다 — 이 목록이 늘어도 구현은 바뀌지 않아야 한다.
OCCURRENCE_CASES = [
    ("노트북을 샀다", "샀다"),
    ("노트북을 구매했다", "구매했다"),
    ("노트북을 구입한 고객", "구입한"),
    ("노트북을 주문한 회원", "주문한"),
    ("지난달에 노트북을 질러버린 고객", "질러버린"),
    ("큰맘 먹고 노트북을 장만한 회원", "장만한"),
    ("노트북을 들인 지 얼마 안 된 고객", "들인"),
    ("결제까지 마친 고객", "결제까지 마친"),
    ("우리 몰에서 노트북 데려간 분들", "데려간"),
]


@pytest.mark.parametrize("text,evidence", OCCURRENCE_CASES, ids=[case[0] for case in OCCURRENCE_CASES])
def test_occurrence_is_detected_whatever_the_wording(text: str, evidence: str) -> None:
    signal = _judge(text, _payload(ss.COMPLETED, evidence))
    assert signal.status == ss.COMPLETED
    assert signal.detected is True
    assert signal.negated is False
    assert signal.source == ss.SOURCE_LLM
    assert ss.signature(signal) == {f"{SIGNAL}:exists"}


# ── B. 과거 이력·경험 ──────────────────────────────────────────────────────────────────────
HISTORY_CASES = [
    ("구매 이력이 있는 고객", "구매 이력이 있는"),
    ("구입 내역이 남아 있는 회원", "구입 내역이 남아 있는"),
    ("주문 기록이 있는 사람", "주문 기록이 있는"),
    ("사 본 경험이 있는 고객", "경험이 있는"),
    ("한 번이라도 결제해 본 적 있는 회원", "적 있는"),
]


@pytest.mark.parametrize("text,evidence", HISTORY_CASES, ids=[case[0] for case in HISTORY_CASES])
def test_history_counts_as_occurrence(text: str, evidence: str) -> None:
    """이력은 발생이다 — 오디언스 타겟팅에서 '산 적 있는'과 '샀다'는 같은 모집단이다."""
    signal = _judge(text, _payload(ss.HISTORY, evidence))
    assert signal.status == ss.HISTORY
    assert signal.detected is True
    assert ss.signature(signal) == {f"{SIGNAL}:exists"}


# ── C. 의향·계획: 발생이 아니다 ───────────────────────────────────────────────────────────
INTENT_CASES = [
    ("노트북을 살까 고민 중인 고객", "살까 고민 중인"),
    ("노트북을 살 예정인 회원", "살 예정인"),
    ("노트북을 사고 싶어하는 고객", "사고 싶어하는"),
    ("구매를 알아보고 있는 사람", "알아보고 있는"),
    ("장바구니만 담아 두고 계획 중인 회원", "계획 중인"),
]


@pytest.mark.parametrize("text,evidence", INTENT_CASES, ids=[case[0] for case in INTENT_CASES])
def test_intent_is_not_an_occurrence(text: str, evidence: str) -> None:
    """의향을 발생과 같은 boolean 으로 뭉치면 '살까 고민 중'이 구매 이력 조건이 된다."""
    signal = _judge(text, _payload(ss.INTENT, evidence))
    assert signal.status == ss.INTENT
    assert signal.detected is False
    assert ss.signature(signal) == frozenset()
    # 그러나 '이 문장이 구매 얘기인가'라는 문맥 질문에는 여전히 참이다(두 정책은 서로 다르다).
    assert signal.status in ss.CONTEXTUAL


# ── D. 부정 ────────────────────────────────────────────────────────────────────────────────
DENIAL_CASES = [
    ("노트북을 사지 않았다", "사지 않았다"),
    ("한 번도 구매한 적이 없는 고객", "적이 없는"),
    ("아직 결제하지 않은 회원", "결제하지 않은"),
    ("앞으로도 사지 않을 고객", "사지 않을"),
    ("사려다 못 산 회원", "못 산"),
    ("미구매 고객", "미구매"),
]


@pytest.mark.parametrize("text,evidence", DENIAL_CASES, ids=[case[0] for case in DENIAL_CASES])
def test_denial_is_absence_not_occurrence(text: str, evidence: str) -> None:
    signal = _judge(text, _payload(ss.DENIED, evidence))
    assert signal.status == ss.DENIED
    assert signal.detected is False
    assert signal.negated is True
    assert ss.signature(signal) == {f"{SIGNAL}:absent"}


# ── E. 단순 언급·정보 탐색·타인의 행위 ────────────────────────────────────────────────────
MENTION_CASES = [
    ("구매 방법을 알려줘", "구매 방법"),
    ("구매란 무엇인지 설명해줘", "구매란"),
    ("신상품을 팔고 싶은 고객에게 발송", "팔고 싶은"),
    ("친구가 대신 사줬다는 회원", "친구가 대신 사줬다"),
    ("샀다는 이야기를 들었다는 고객", "이야기를 들었다"),
]


@pytest.mark.parametrize("text,evidence", MENTION_CASES, ids=[case[0] for case in MENTION_CASES])
def test_mention_is_not_an_occurrence(text: str, evidence: str) -> None:
    """개념이 말로 등장했을 뿐인 문장과 제3자의 행위는 대상 회원의 구매가 아니다."""
    signal = _judge(text, _payload(ss.MENTIONED, evidence))
    assert signal.detected is False
    assert ss.signature(signal) == frozenset()


def test_hypothetical_is_not_an_occurrence() -> None:
    signal = _judge("노트북을 샀다고 가정하면", _payload(ss.HYPOTHETICAL, "가정하면"))
    assert signal.status == ss.HYPOTHETICAL
    assert signal.detected is False


def test_absent_meaning_is_none_not_unknown() -> None:
    """'그 뜻 없음'과 '판정 실패'는 다르다 — 하나로 접으면 실패율을 볼 수 없다."""
    signal = _judge("부산에 사는 30대 여성 고객", _payload(ss.NONE, ""))
    assert signal.status == ss.NONE
    assert signal.detected is False
    assert signal.status not in ss.CONTEXTUAL


# ── F. 동음이의: 같은 표면형, 다른 뜻 ─────────────────────────────────────────────────────
HOMONYM_NON_PURCHASE = [
    "한라산에 갔다",
    "높은 산을 등반했다",
    "산 정상까지 올라간 고객",
    "부산 고객에게 발송",
    "등산을 좋아하는 회원",
    "교통사고 이력이 있는 고객",
    "도서관 사서로 일하는 회원",
]


@pytest.mark.parametrize("text", HOMONYM_NON_PURCHASE, ids=HOMONYM_NON_PURCHASE)
def test_homonyms_are_not_purchase_in_the_rule_tier(text: str) -> None:
    """폴백조차 낱말만 보고 구매로 읽지 않는다 — 문맥 조건이 형태 판정에 들어 있다."""
    assert purchase_lexicon.rule_signal(text).detected is False


def test_same_surface_form_flips_with_context() -> None:
    """substring 검색으로는 절대 구분되지 않는 쌍. 판정을 가르는 것은 낱말이 아니라 문맥이다."""
    assert purchase_lexicon.rule_signal("기저귀 산 고객").detected is True
    assert purchase_lexicon.rule_signal("설악산 고객").detected is False


# ── G. 복합 문장: 대상마다 다른 답 ────────────────────────────────────────────────────────
def test_negation_scope_is_per_entity_not_per_sentence() -> None:
    """"노트북은 사지 않았지만 모니터는 샀다" — 문장 하나의 boolean 으로 접으면 반드시 틀린다."""
    text = "노트북은 사지 않았지만 모니터는 샀다"
    signal = _judge(
        text,
        _payload(ss.COMPLETED, "모니터는 샀다", [("노트북", ss.DENIED), ("모니터", ss.COMPLETED)]),
    )

    assert signal.entities == ("노트북", "모니터")
    assert signal.entity_status("노트북") == ss.DENIED
    assert signal.entity_status("모니터") == ss.COMPLETED
    assert [row.detected for row in signal.claims] == [False, True]
    # 문장 단위 정책: 하나라도 발생이면 문장은 발생이다(그 회원은 구매 이력이 있다).
    assert signal.detected is True
    assert ss.signature(signal) == {f"{SIGNAL}:absent:노트북", f"{SIGNAL}:exists:모니터"}


def test_every_entity_denied_makes_the_sentence_negated() -> None:
    signal = _judge(
        "노트북도 모니터도 사지 않은 고객",
        _payload(ss.DENIED, "사지 않은", [("노트북", ss.DENIED), ("모니터", ss.DENIED)]),
    )
    assert signal.detected is False
    assert signal.negated is True


def test_mixed_polarity_without_entities_still_keeps_both_relations() -> None:
    """대상 이름을 못 얻은 경우(형태 폴백)에도 두 극성이 모두 서명에 남는다."""
    signal = purchase_lexicon.rule_signal("예전에는 구매했지만 최근에는 구매하지 않은 고객")
    assert ss.signature(signal) == {f"{SIGNAL}:exists", f"{SIGNAL}:absent"}
    assert signal.detected is True


# ── H. 재작성 전후: 뜻이 보존되는가 ───────────────────────────────────────────────────────
def test_nominalising_rewrite_preserves_the_meaning() -> None:
    """동사가 명사구로 바뀌어도 같은 뜻이다 — 문자열이 아니라 구조화 값끼리 비교하기 때문이다."""
    original = _judge("7년전 기저귀를 구매한 여자 고객", _payload(ss.COMPLETED, "구매한", [("기저귀", ss.COMPLETED)]))
    rewritten = _judge("여자 고객, 7년전 기저귀 구매 이력", _payload(ss.HISTORY, "구매 이력", [("기저귀", ss.HISTORY)]))

    diff = ss.compare_rewrite(original, rewritten)
    assert diff.preserved, diff
    # status 는 달라도(completed↔history) 관계 서명은 같다 — 둘 다 '샀다'는 모집단이다.
    assert ss.signature(original) == ss.signature(rewritten)


def test_rewrite_that_deletes_the_relation_is_a_loss() -> None:
    original = _judge("기저귀를 구매한 고객", _payload(ss.COMPLETED, "구매한", [("기저귀", ss.COMPLETED)]))
    rewritten = _judge("기저귀에 관심 있는 고객", _payload(ss.NONE, ""))

    diff = ss.compare_rewrite(original, rewritten)
    assert diff.dropped == (f"{SIGNAL}:exists:기저귀",)
    assert diff.added == ()


def test_rewrite_that_invents_a_relation_is_an_addition() -> None:
    """원문에 없던 조건이 SQL 로 나가는 쪽이 조건이 빠지는 것보다 나쁘다 — 따로 잡는다."""
    original = _judge("기저귀에 관심 있는 고객", _payload(ss.NONE, ""))
    rewritten = _judge("기저귀를 구매한 고객", _payload(ss.COMPLETED, "구매한", [("기저귀", ss.COMPLETED)]))

    diff = ss.compare_rewrite(original, rewritten)
    assert diff.dropped == ()
    assert diff.added == (f"{SIGNAL}:exists:기저귀",)
    assert diff.preserved is False


def test_rewrite_that_flips_polarity_is_both_a_loss_and_an_addition() -> None:
    original = _judge("기저귀를 구매한 고객", _payload(ss.COMPLETED, "구매한"))
    rewritten = _judge("기저귀를 구매하지 않은 고객", _payload(ss.DENIED, "구매하지 않은"))

    diff = ss.compare_rewrite(original, rewritten)
    assert diff.dropped == (f"{SIGNAL}:exists",)
    assert diff.added == (f"{SIGNAL}:absent",)


def test_rewrite_that_turns_an_occurrence_into_an_intent_is_a_loss() -> None:
    """재작성이 '샀다'를 '사고 싶어한다'로 바꾸면 모집단이 완전히 달라진다."""
    original = _judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다"))
    rewritten = _judge("노트북을 사고 싶어하는 고객", _payload(ss.INTENT, "사고 싶어하는"))
    assert ss.compare_rewrite(original, rewritten).dropped == (f"{SIGNAL}:exists",)


# ── I. 안정성: 스키마·빈 입력·실패·폴백 ───────────────────────────────────────────────────
def test_schema_violation_falls_back_instead_of_being_adopted() -> None:
    """허용되지 않은 상태 값은 채택하지 않는다(닫힌 집합)."""
    signal = _judge("노트북을 샀다", {"signal": SIGNAL, "status": "bought", "evidence": "샀다"})
    assert signal.status in ss.STATUSES
    assert signal.source == ss.SOURCE_RULES
    assert signal.fallback_used is True
    assert signal.schema_error == "schema_mismatch"


def test_evidence_not_in_the_source_is_rejected() -> None:
    """뜻은 그럴듯한데 원문에 없는 근거는 환각이다 — 채택하면 폴백보다 나쁘다."""
    signal = _judge("노트북을 샀다", _payload(ss.COMPLETED, "구입 이력이 있다"))
    assert signal.source == ss.SOURCE_RULES


def test_hallucinated_entity_is_dropped_but_the_relation_survives() -> None:
    signal = _judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다", [("냉장고", ss.COMPLETED)]))
    assert signal.entities == ()
    assert signal.detected is True


def test_self_contradicting_response_is_rejected() -> None:
    """negated=true 인데 상태가 발생이면 모순이다 — 둘 중 무엇을 믿을지 추측하지 않는다."""
    payload = {**_payload(ss.COMPLETED, "샀다"), "negated": True}
    assert _judge("노트북을 샀다", payload).source == ss.SOURCE_RULES


def test_asserted_status_without_evidence_is_rejected() -> None:
    assert _judge("노트북을 샀다", {"signal": SIGNAL, "status": ss.COMPLETED}).source == ss.SOURCE_RULES


@pytest.mark.parametrize("payload", [None, {}, [], "nope", {"signals": []}, {"signal": "other", "status": "none"}])
def test_malformed_payloads_never_raise(payload) -> None:
    signal = _judge("노트북을 샀다", payload)
    assert signal.signal == SIGNAL
    assert signal.status in ss.STATUSES


@pytest.mark.parametrize("text", ["", "   ", None])
def test_blank_input_is_none_not_a_crash(text) -> None:
    assert ss.resolve(text or "", SIGNAL, rules=purchase_lexicon.rule_signal).status == ss.NONE


def test_very_long_input_is_handled() -> None:
    text = "노트북을 샀다 " * 5000
    signal = ss.resolve(text, SIGNAL, rules=purchase_lexicon.rule_signal)
    assert signal.detected is True


def test_extractor_failure_falls_back_and_records_the_reason() -> None:
    """호출 실패가 파싱을 막으면 안 되고, 조용히 false 가 되어서도 안 된다."""

    def boom(_text, _spec):
        raise TimeoutError("upstream timeout")

    signal = ss.resolve("노트북을 샀다", SIGNAL, extract=boom, rules=purchase_lexicon.rule_signal)
    assert signal.detected is True
    assert signal.source == ss.SOURCE_RULES
    assert signal.fallback_used is True
    assert signal.schema_error == "TimeoutError"


def test_no_judge_at_all_returns_unknown_not_false() -> None:
    """판정기가 하나도 없으면 unknown 이다 — 호출부가 이 상태를 보고 정책을 정할 수 있어야 한다."""
    signal = ss.resolve("노트북을 샀다", SIGNAL, extract=_extractor(None))
    assert signal.status == ss.UNKNOWN
    assert signal.detected is False
    assert signal.fallback_used is True


def test_conservative_tier_never_promotes_to_an_occurrence() -> None:
    """3순위는 문맥만 답한다 — 낱말 하나로 발생을 만들면 그것이 이 작업이 없애려던 결함이다."""
    signal = graph_rag._purchase_conservative_signal("화장품 구매 캠페인")
    assert signal is not None
    assert signal.status == ss.MENTIONED
    assert signal.detected is False


def test_a_silent_upper_tier_does_not_swallow_the_lower_one() -> None:
    """상위 폴백의 '못 읽었다'(none)와 '뜻이 없다'는 다르다 — 섞으면 하위 순위가 영영 안 돈다.

    '화장품 구매 캠페인'은 형태 판정이 침묵하는 문장이다(용언 어미도 기록 명사도 없다).
    그때 보수적 낱말 판정이 문맥만 답할 수 있어야 상품 추출 게이트가 열린다.
    """
    assert purchase_lexicon.rule_signal("화장품 구매 캠페인").status == ss.NONE
    resolved = ss.resolve(
        "화장품 구매 캠페인",
        SIGNAL,
        rules=purchase_lexicon.rule_signal,
        conservative=graph_rag._purchase_conservative_signal,
    )
    assert resolved.status == ss.MENTIONED
    assert resolved.source == ss.SOURCE_CONSERVATIVE
    assert graph_rag._has_purchase_history_signal("화장품 구매 캠페인") is True
    # 모든 순위가 침묵하면 그때가 '뜻 없음'이다.
    assert graph_rag._has_purchase_history_signal("부산에 사는 30대 여성") is False


def test_fallback_is_a_priority_chain_not_an_or() -> None:
    """상위가 '의향'이라 판정하면 하위 폴백이 '구매 낱말이 있다'고 해도 의향이다."""
    text = "노트북을 사려고 고민 중인 고객"
    assert purchase_lexicon.rule_signal(text).detected is False  # 형태는 애초에 침묵
    intent = _judge("노트북을 샀지만 반품할까 고민 중", _payload(ss.INTENT, "고민 중"))
    assert intent.detected is False, "형태 판정이 '샀'을 읽어도 상위 판정을 뒤집지 못한다"


def test_detected_is_computed_by_policy_not_by_the_response() -> None:
    """응답이 무엇을 보내든 detected 는 정책 함수가 다시 계산한다."""
    payload = {**_payload(ss.INTENT, "고민 중"), "detected": True}
    assert _judge("노트북을 살까 고민 중", payload).detected is False
    assert ss.detected_for(SPEC, ss.INTENT, False) is False
    assert ss.detected_for(SPEC, ss.COMPLETED, False) is True
    assert ss.detected_for(SPEC, ss.COMPLETED, True) is False, "부정이면 어떤 상태도 발생이 아니다"


def test_repeated_calls_are_structurally_stable() -> None:
    payload = _payload(ss.COMPLETED, "샀다", [("노트북", ss.COMPLETED)])
    results = [_judge("노트북을 샀다", payload) for _ in range(5)]
    assert all(ss.same_meaning(results[0], other) for other in results[1:])
    assert len({ss.signature(item) for item in results}) == 1


def test_declared_statuses_are_a_closed_set() -> None:
    assert SPEC is not None, "semantic_signals.json 을 못 읽었다"
    assert SPEC.detected_statuses <= ss.STATUSES
    assert set(ss.STATUS_ORDER) == set(ss.STATUSES)
    assert ss.json_schema(SIGNAL)["properties"]["status"]["enum"] == sorted(ss.STATUSES)


def test_signal_declaration_holds_meaning_not_words() -> None:
    """선언은 '뜻'과 '정책'만 갖는다 — 표현형 사전이 되면 이 계층의 존재 이유가 사라진다."""
    for spec in ss.load_specs().values():
        assert len(spec.description) >= 10
        assert spec.detected_statuses, f"{spec.signal}: 발생으로 볼 상태가 선언돼 있지 않다"
        # few-shot 은 소수 예시여야 한다(전수 표현형은 이 테스트 파일이 갖는다).
        assert len(spec.positive_examples) + len(spec.negative_examples) <= 20


# ── J. 의미 비교: 메타데이터를 섞지 않는다 ────────────────────────────────────────────────
def test_metadata_differences_do_not_change_the_meaning() -> None:
    """출처·모델·소요시간이 달라도 뜻이 같으면 같다 — 다르다고 판정하면 정상 결과가 차단된다."""
    from dataclasses import replace

    base = _judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다", [("노트북", ss.COMPLETED)]))
    other = replace(
        base, source=ss.SOURCE_RULES, fallback_used=True, model="another-model",
        prompt_version="99", elapsed_ms=1234.5, schema_error="whatever",
    )

    assert ss.same_meaning(base, other)
    assert ss.canonical_form(base) == ss.canonical_form(other)
    assert "source" not in ss.canonical_form(base)
    assert "model" not in ss.canonical_form(base)


def test_same_source_but_different_status_is_a_different_meaning() -> None:
    """메타데이터가 같다는 이유로 실제 의미가 다른 결과를 같다고 판단하지 않는다."""
    completed = _judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다"))
    intent = _judge("노트북을 살까", _payload(ss.INTENT, "살까"))
    assert completed.source == intent.source
    assert not ss.same_meaning(completed, intent)


def test_entity_order_does_not_change_the_meaning() -> None:
    left = _judge("노트북과 모니터를 샀다", _payload(ss.COMPLETED, "샀다", [("노트북", ss.COMPLETED), ("모니터", ss.COMPLETED)]))
    right = _judge("노트북과 모니터를 샀다", _payload(ss.COMPLETED, "샀다", [("모니터", ss.COMPLETED), ("노트북", ss.COMPLETED)]))
    assert ss.same_meaning(left, right)


def test_different_entities_are_a_different_meaning() -> None:
    left = _judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다", [("노트북", ss.COMPLETED)]))
    right = _judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다", [("노트북", ss.DENIED)]))
    assert not ss.same_meaning(left, right)


def test_observation_omits_the_source_text_by_default() -> None:
    """관측 로그에 원문을 흘리지 않는다(개인정보). 진단에 필요한 것은 상태와 출처다."""
    record = ss.observation(_judge("노트북을 샀다", _payload(ss.COMPLETED, "샀다")))
    assert record["status"] == ss.COMPLETED
    assert record["source"] == ss.SOURCE_LLM
    assert "evidence" not in record
    assert set(record) >= {"signal", "status", "detected", "source", "fallback_used", "schema_error"}


# ── K. 파이프라인 통합: 후속 단계가 같은 구조화 값을 읽는다 ───────────────────────────────
def test_context_gate_and_occurrence_gate_are_different_questions() -> None:
    """같은 status 하나에서 두 정책 함수가 각자 답한다(하나의 boolean 으로 뭉치지 않는다)."""
    assert graph_rag._has_purchase_history_signal("구매하지 않은 고객") is True
    assert _judge("구매하지 않은 고객", _payload(ss.DENIED, "구매하지 않은")).detected is False
    assert graph_rag._has_purchase_history_signal("부산에 사는 고객") is False


def test_scope_split_gate_reads_the_structured_signal() -> None:
    """절 분리 폐기 판정과 재작성 폐기 판정이 같은 값을 읽는다 — 게이트마다 다른 규칙이 아니다."""
    original = "기저귀를 구매한 고객에게 쿠폰 발송"
    assert not (
        graph_rag._audience_polarity_signals(original)
        - graph_rag._audience_polarity_signals("기저귀 구매 이력 고객")
    )
    assert (
        graph_rag._audience_polarity_signals(original)
        - graph_rag._audience_polarity_signals("기저귀에 관심 있는 고객")
    )


def test_rewrite_gate_reports_invented_conditions_too() -> None:
    """재작성이 없던 구매 조건을 지어내면 그 재작성도 폐기 대상이다."""
    invented = graph_rag._rewrite_dropped_signals("기저귀에 관심 있는 고객", "기저귀를 구매한 고객")
    assert any("원문에 없는 구매 조건" in item for item in invented), invented


def test_llm_meaning_suppresses_a_rule_matched_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    """형태는 '샀'을 읽지만 뜻은 의향이다 — 구매 존재 조건으로 승격하지 않는다."""
    query = "노트북을 샀으면 좋겠다고 하는 고객"
    assert purchase_lexicon.rule_signal(query).detected is True, "형태 판정은 이 문장을 발생으로 읽는다"

    monkeypatch.setattr(
        graph_rag,
        "_llm_extract_semantic_signal",
        lambda text, spec, *_a, **_k: _payload(ss.INTENT, "샀으면 좋겠다"),
    )
    plan: dict = {"target_user": {}}
    with graph_rag._semantic_signal_scope(query):
        graph_rag._apply_core_membership_semantics(query, plan)

    assert "purchase_membership" not in plan["target_user"]
    decisions = graph_rag.plan_decisions.decisions(plan)
    assert any("실제 발생이 아니다" in entry.get("reason", "") for entry in decisions), decisions


def test_rule_tier_still_promotes_without_the_llm() -> None:
    """폴백만 도는 오프라인 경로에서는 기존 결정론 동작 그대로다(폴백이 조건을 없애지 않는다)."""
    plan: dict = {"target_user": {}}
    graph_rag._apply_core_membership_semantics("기저귀 구매 이력", plan)
    assert plan["target_user"]["purchase_membership"] == {"domain": "purchase", "operator": "exists"}


def test_scope_is_resolved_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """뜻은 질의당 한 번만 구조화한다 — 후속 단계가 같은 문장을 다시 판정하지 않는다."""
    calls: list[str] = []

    def _extract(text, spec, *_args, **_kwargs):
        calls.append(text)
        return _payload(ss.COMPLETED, "구매한", [("기저귀", ss.COMPLETED)])

    monkeypatch.setattr(graph_rag, "_llm_extract_semantic_signal", _extract)
    query = "기저귀를 구매한 고객에게 쿠폰 발송"
    with graph_rag._semantic_signal_scope(query):
        first = graph_rag._purchase_semantics(query)
        second = graph_rag._purchase_semantics(query)

    assert calls == [query], "질의 하나에 판정도 하나여야 한다"
    assert ss.same_meaning(first, second)
    assert first.source == ss.SOURCE_LLM


def test_scope_projects_onto_a_clause_by_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """상위 판정을 절 조각에 투영한다 — 조각마다 다시 물으면 같은 문장에 다른 답이 나온다."""
    monkeypatch.setattr(
        graph_rag,
        "_llm_extract_semantic_signal",
        lambda *_a, **_k: _payload(ss.COMPLETED, "구매한", [("기저귀", ss.COMPLETED)]),
    )
    query = "기저귀를 구매한 고객에게 쿠폰 발송"
    with graph_rag._semantic_signal_scope(query):
        assert graph_rag._purchase_semantics("기저귀를 구매한 고객").detected is True
        assert graph_rag._purchase_semantics("쿠폰 발송").detected is False


def test_scope_leaves_no_global_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_rag, "_llm_extract_semantic_signal", lambda *_a, **_k: _payload(ss.INTENT, "고민 중")
    )
    query = "노트북을 살까 고민 중인 고객"
    with graph_rag._semantic_signal_scope(query):
        assert graph_rag._purchase_semantics(query).source == ss.SOURCE_LLM
    assert graph_rag._purchase_semantics(query).source != ss.SOURCE_LLM
