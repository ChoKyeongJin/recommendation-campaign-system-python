"""조건 슬롯 LLM 보완의 경계 계약.

어휘 사전은 표면 표현을 한 줄씩 쌓는 구조라 처음 보는 말투에는 조용히 침묵한다. 그 빈칸을 LLM 이
메우되, 메우는 범위가 계약으로 묶여 있다.

  * **닫힌 집합에서 고르기만.** canonical 은 attribute_token_groups 가 선언한 것 중 실제 컴파일 가능한
    것뿐이고, 연산자는 segment_semantics 의 연산자 집합뿐이다. 목록 밖은 버린다.
  * **근거가 있어야 한다.** 원문에 그대로 있고, 규칙이 이미 읽은 조각과 겹치지 않고, 회원을 가리키는
    말을 포함해야 한다. ('최근 90일 이내 가입한 신규 회원' 을 멤버십 가입으로 읽던 오탐이 실제로 있었다.)
  * **빈칸만.** 규칙이 채운 슬롯은 덮지 않는다.
  * **지원 여부는 여전히 접지가 판정한다.** LLM 은 연산자·값만 정하고 capability 게이트는 JSON 이 쥔다.

같은 계약이 **회원 지표 선택**(member_metric_ranking)에도 적용된다 — '돈이 많아 보이는 고객'처럼
지표를 에둘러 말한 표현은 동의어 목록으로 닫을 수 없어 판정을 LLM 으로 옮겼지만, 고를 수 있는 지표는
member_metrics.json 이 소유하고 개수·퍼센트는 여전히 문장에서 결정론으로 읽는다(파일 하단 절 참조).

LLM 호출은 스텁으로 갈아끼운다 — 이 테스트는 네트워크를 타지 않는다.
"""

from __future__ import annotations

from typing import Any

import graph_rag as g
import lexicon_llm
import pytest
import segment_semantics as ss


@pytest.fixture
def llm_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")


def _stub_extractor(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None) -> list[str]:
    """추출기를 고정 응답으로 갈아끼우고, 실제로 호출된 질의를 기록해 돌려준다."""
    calls: list[str] = []

    def _fake(query: str, canonicals: tuple[str, ...], llm_model: str, prompt_dir: Any) -> dict[str, Any] | None:
        calls.append(query)
        return payload

    monkeypatch.setattr(g, "_llm_extract_condition_slots", _fake)
    return calls


def _plan(query: str) -> dict[str, Any]:
    return g._build_rule_query_plan(query)


# ── 회원 속성: 닫힌 집합 + 근거 ───────────────────────────────────────────────────────────
def test_novel_phrasing_gets_filled_from_closed_set(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """사전에 없는 말투라도 canonical 이 목록 안이고 근거가 원문에 있으면 채운다."""
    _stub_extractor(monkeypatch, {
        "member_flags": [{"canonical": "blacklisted", "polarity": "exclude", "evidence": "차단 처리된 회원"}],
        "coupon_usage": None,
    })
    plan = _plan("차단 처리된 회원은 빼고 발송할 대상 뽑아줘")
    assert "blacklisted" in (plan.get("exclude") or {}).get("lifecycle", [])


def test_canonical_outside_the_closed_set_is_dropped(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_extractor(monkeypatch, {
        "member_flags": [{"canonical": "platinum_whale_member", "polarity": "include", "evidence": "충성 회원"}],
        "coupon_usage": None,
    })
    plan = _plan("충성 회원 대상으로 캠페인 보내줘")
    assert "platinum_whale_member" not in (plan.get("target_user") or {}).get("lifecycle", [])


def test_evidence_absent_from_the_query_is_rejected(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_extractor(monkeypatch, {
        "member_flags": [{"canonical": "employee", "polarity": "include", "evidence": "임직원 회원"}],
        "coupon_usage": None,
    })
    plan = _plan("이번 달 생일인 고객에게 보낼 대상")
    assert "employee" not in (plan.get("target_user") or {}).get("lifecycle", [])


def test_evidence_already_read_by_rules_is_rejected(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """규칙이 '신규'로 읽은 텍스트를 LLM 이 멤버십으로 다시 읽어 조건을 하나 더 만들지 못한다.

    골든 코퍼스 signup_recent_window 에서 실제로 관측된 오탐이다."""
    _stub_extractor(monkeypatch, {
        "member_flags": [{"canonical": "membership_member", "polarity": "include", "evidence": "신규 회원"}],
        "coupon_usage": None,
    })
    plan = _plan("최근 90일 이내 가입한 신규 회원")
    assert plan["target_user"]["lifecycle"] == ["new_user"]


def test_evidence_without_a_member_noun_is_rejected(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """'가입한' 같은 행위 표현만으로 신분 속성을 만들어내지 못한다."""
    _stub_extractor(monkeypatch, {
        "member_flags": [{"canonical": "membership_member", "polarity": "include", "evidence": "가입한"}],
        "coupon_usage": None,
    })
    plan = _plan("최근 90일 이내 가입한 신규 회원")
    assert "membership_member" not in plan["target_user"].get("lifecycle", [])


def test_rules_result_is_not_overwritten(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """규칙이 이미 플래그를 올렸으면 LLM 을 부르지도 않는다(빈칸만 메운다)."""
    calls = _stub_extractor(monkeypatch, {"member_flags": [], "coupon_usage": None})
    plan = _plan("블랙리스트 회원 제외하고 뽑아줘")
    assert "blacklisted" in (plan.get("exclude") or {}).get("lifecycle", [])
    assert calls == []


# ── 쿠폰 임계: 어휘가 못 읽은 것만, 지원 여부는 접지가 판정 ───────────────────────────────
def test_korean_numeral_threshold_is_filled(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """'세 번 넘게'는 숫자+단위가 아니라 어휘 스캐너가 못 읽는다 — LLM 이 operator/value 로 정규화한다."""
    _stub_extractor(monkeypatch, {"member_flags": [], "coupon_usage": {"operator": "gt", "value": 3}})
    plan = _plan("쿠폰을 세 번 넘게 사용한 고객")
    assert plan["target_user"]["coupon_usage_thresholds"] == [{"operator": "gt", "value": 3.0}]


def test_lexicon_read_threshold_is_not_replaced(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """숫자+단위가 있으면 임계는 어휘가 소유한다 — LLM 이 다른 값을 줘도 채택되지 않는다."""
    _stub_extractor(monkeypatch, {"member_flags": [], "coupon_usage": {"operator": "lt", "value": 99}})
    plan = _plan("쿠폰 3개 이상 사용한 고객")
    assert plan["target_user"]["coupon_usage_thresholds"] == [{"operator": "gte", "value": 3.0}]
    assert g._SEGMENT_SLOT_KEY not in plan


def test_invalid_operator_is_dropped(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_extractor(monkeypatch, {"member_flags": [], "coupon_usage": {"operator": "approximately", "value": 3}})
    plan = _plan("쿠폰을 세 번 넘게 사용한 고객")
    assert "coupon_usage_thresholds" not in plan.get("target_user", {})


def test_slot_source_is_recorded_on_the_condition() -> None:
    """슬롯 출처가 의미 노드에 남아야 감사가 가능하다."""
    registry = ss.SegmentSemanticsRegistry.load()
    interp = ss.interpret("쿠폰을 세 번 넘게 사용한 고객", registry, slots={"operator": "gt", "value": 3})
    assert interp is not None
    assert interp.condition.slot_source == "llm"
    assert (interp.condition.operator, interp.condition.value) == ("gt", 3.0)


def test_grounding_still_gates_llm_filled_slots() -> None:
    """LLM 이 채운 임계라도 지원 여부는 접지(capability)가 판정한다 — LLM 이 지원을 만들어내지 못한다."""
    registry = ss.SegmentSemanticsRegistry.load()
    coupon = registry.metrics["coupon_usage_count"]
    narrowed = {**registry.metrics, "coupon_usage_count": ss.replace(
        coupon,
        capabilities={**coupon.capabilities, "filter": ss.Capability(
            operation="filter", supported=False, supported_operators=()
        )},
    )}
    gated = ss.SegmentSemanticsRegistry(metrics=narrowed, operators=registry.operators)
    interp = ss.interpret("쿠폰을 세 번 넘게 사용한 고객", gated, slots={"operator": "gt", "value": 3})
    assert interp is not None
    assert not interp.capability.supported
    assert interp.capability.code == "coupon_usage_count_filter_unsupported"


# ── 폴백 없이도 동작 ──────────────────────────────────────────────────────────────────────
def test_disabled_flag_keeps_rules_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    calls = _stub_extractor(monkeypatch, {
        "member_flags": [{"canonical": "blacklisted", "polarity": "exclude", "evidence": "차단 처리된 회원"}],
        "coupon_usage": None,
    })
    plan = _plan("차단 처리된 회원은 빼고 발송할 대상 뽑아줘")
    assert calls == []
    assert "blacklisted" not in (plan.get("exclude") or {}).get("lifecycle", [])


# ═══════════════════════════════════════════════════════════════════════════════════════
# 회원 지표 선택: '돈이 많아 보이는 고객'
#
# 지표를 에둘러 말하는 표현은 끝이 없다(큰손·여유 있는·씀씀이 큰 …). 동의어를 한 줄씩 더하는 대신
# 판정을 LLM 으로 옮겼고, 옮긴 것은 표면어뿐이다 — 지표 목록·개수·방향의 소유권은 그대로다.
# ═══════════════════════════════════════════════════════════════════════════════════════
VAGUE_QUERY = "돈이 많아 보이는 고객 100명 뽑아줘"


def _stub_metric_chooser(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None
) -> list[str]:
    """지표 선택기를 고정 응답으로 갈아끼우고, 실제로 호출된 질의를 기록해 돌려준다."""
    calls: list[str] = []

    def _fake(query: str, metrics: tuple[dict[str, Any], ...], llm_model: str, prompt_dir: Any) -> Any:
        calls.append(query)
        return payload

    monkeypatch.setattr(g, "_llm_choose_member_metric", _fake)
    return calls


def _metric_plan(query: str, *, concept_fires: bool = True) -> dict[str, Any]:
    """표면 개념 신호를 고정한 채 플랜을 만든다(개념 판정 자체는 별도 계층의 계약이다)."""
    signals = {g._MEMBER_METRIC_CONCEPT_ID: (lexicon_llm.compact(query),)} if concept_fires else {}
    with lexicon_llm.signal_scope(query, lambda _text: signals):
        return g._build_rule_query_plan(query)


def test_vague_metric_phrase_is_resolved_from_the_closed_set(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """사전에 없는 '돈이 많아 보이는'을 닫힌 지표 집합의 한 항목으로 해석한다."""
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "돈이 많아 보이는 고객",
    })
    ranking = _metric_plan(VAGUE_QUERY)["member_metric_ranking"]
    assert ranking["metric_id"] == "total_buy_amt"
    assert ranking["direction"] == "high"
    # 사전이 아니라 뜻으로 읽혔다는 표시가 남아야 감사가 가능하다.
    assert ranking["resolution_source"] == "llm"


def test_vague_low_metric_phrase_accepts_person_as_member_granularity(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'사람'도 회원 단위 표현이며, 모호한 저방향 지표만 LLM이 빈 슬롯에 채운다."""
    query = "돈없을 것 같은 사람 추출해줘"
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "low", "evidence": "돈없을 것 같은 사람",
    })

    ranking = _metric_plan(query)["member_metric_ranking"]

    assert ranking["metric_id"] == "total_buy_amt"
    assert ranking["direction"] == "low"
    assert ranking["resolution_source"] == "llm"


def test_precomputed_surface_signal_survives_targeting_subquery(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전체 문장에서 얻은 evidence를 짧아진 planning query가 그대로 재사용한다."""
    query = "돈없을 것 같은 사람"
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "low", "evidence": query,
    })
    signals = {g._MEMBER_METRIC_CONCEPT_ID: (lexicon_llm.compact(query),)}

    plan = g.build_query_plan(
        query,
        parser="rules",
        precomputed_surface_signals=signals,
    )

    assert plan["member_metric_ranking"]["metric_id"] == "total_buy_amt"
    assert plan["member_metric_ranking"]["direction"] == "low"


def test_member_metric_prompt_declares_financial_metaphor_proxy_policy() -> None:
    prompt = g._member_metric_choice_system_prompt(g._member_metric_catalog())

    assert "total_buy_amt/low" in prompt
    assert "실제 재산·소득을 추정" in prompt


def test_short_metric_evidence_recovers_validated_surface_span(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """지표 선택 응답이 회원 명사를 잘라도 앞 단계의 검증된 원문 evidence로 복원한다."""
    query = "돈없을 것 같은 사람"
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "low", "evidence": "돈없을 것 같은",
    })
    signals = {g._MEMBER_METRIC_CONCEPT_ID: (lexicon_llm.compact(query),)}

    plan = g.build_query_plan(
        query,
        parser="rules",
        precomputed_surface_signals=signals,
    )

    assert plan["member_metric_ranking"]["matched_text"] == query
    assert plan["member_metric_ranking"]["direction"] == "low"


def test_metric_outside_the_registry_is_dropped(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """레지스트리에 없는 지표 id 는 버린다 — LLM 이 컬럼을 지어내지 못한다."""
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "estimated_net_worth", "direction": "high", "evidence": "돈이 많아 보이는 고객",
    })
    assert _metric_plan(VAGUE_QUERY).get("member_metric_ranking") is None


def test_metric_evidence_absent_from_the_query_is_rejected(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """근거가 원문에 글자 그대로 없으면 채택하지 않는다(번역·유추 금지)."""
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "부유한 고객",
    })
    assert _metric_plan(VAGUE_QUERY).get("member_metric_ranking") is None


def test_metric_evidence_without_a_related_member_surface_span_is_rejected(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """회원 단위 표현이 없는 근거는 거절한다 — 지역·상품 순위가 회원 랭킹으로 새는 것을 막는다."""
    query = "돈이 많아 보이는 지역의 고객"
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "돈이 많아 보이는 지역",
    })
    signals = {g._MEMBER_METRIC_CONCEPT_ID: (lexicon_llm.compact("지역의 고객"),)}

    with lexicon_llm.signal_scope(query, lambda _text: signals):
        assert g._build_rule_query_plan(query).get("member_metric_ranking") is None


def test_invalid_direction_is_rejected(llm_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "descending", "evidence": "돈이 많아 보이는 고객",
    })
    assert _metric_plan(VAGUE_QUERY).get("member_metric_ranking") is None


def test_lexicon_resolved_metric_never_calls_the_chooser(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """지표어를 그대로 말한 문장은 사전이 소유한다 — 빈칸이 아니므로 LLM 을 부르지도 않는다."""
    calls = _stub_metric_chooser(monkeypatch, {
        "metric_id": "mean_buy_amt", "direction": "low", "evidence": "매출이 높은 고객",
    })
    ranking = _metric_plan("매출이 높은 고객 100명")["member_metric_ranking"]
    assert ranking["metric_id"] == "total_buy_amt"
    assert "resolution_source" not in ranking
    assert calls == []


def test_chooser_is_not_called_without_the_surface_concept(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """개념 신호가 없으면 지표 선택 호출 자체가 없다 — 평범한 질의의 추가 비용이 0 이라는 계약."""
    calls = _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "돈이 많아 보이는 고객",
    })
    assert _metric_plan(VAGUE_QUERY, concept_fires=False).get("member_metric_ranking") is None
    assert calls == []


def test_counts_come_from_the_sentence_not_the_model(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """개수·퍼센트는 LLM 이 정하지 않는다 — 문장의 숫자를 결정론으로 읽고, 없으면 레지스트리 기본값."""
    _stub_metric_chooser(monkeypatch, {
        # 모델이 top_n 을 우겨 넣어도 무시돼야 한다(스키마에 없는 키).
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "돈이 많아 보이는 고객", "top_n": 7,
    })
    assert _metric_plan(VAGUE_QUERY)["member_metric_ranking"]["top_n"] == 100

    default_top_n = int(g._member_metric_ranking_config()["default_top_n"])
    plain = _metric_plan("돈이 많아 보이는 고객 뽑아줘")["member_metric_ranking"]
    assert plain["top_n"] == default_top_n
    assert plain["limit_type"] == "count"


def test_percent_directive_still_wins_over_count(
    llm_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'상위 5%'는 상위 5명이 아니다 — LLM 경로도 같은 퍼센트 문법을 쓴다."""
    _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "돈이 많아 보이는 고객",
    })
    ranking = _metric_plan("돈이 많아 보이는 고객 상위 5%")["member_metric_ranking"]
    assert (ranking["limit_type"], ranking["percent"]) == ("percent", 5.0)


def test_metric_disabled_flag_keeps_lexicon_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """플래그를 끄면 사전 매칭만 남는다(이관 전 결정론 동작)."""
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    calls = _stub_metric_chooser(monkeypatch, {
        "metric_id": "total_buy_amt", "direction": "high", "evidence": "돈이 많아 보이는 고객",
    })
    assert _metric_plan(VAGUE_QUERY).get("member_metric_ranking") is None
    assert calls == []


def test_every_catalog_metric_declares_its_meaning() -> None:
    """지표 설명은 LLM 프롬프트에 그대로 들어간다 — 비면 모델이 형제 지표와의 경계를 알 수 없다."""
    metrics = g._member_metric_catalog()
    assert metrics, "지표 레지스트리를 못 읽었다(파일 부재/파손이면 LLM 지표 해석이 통째로 비활성)"
    for metric in metrics:
        assert len(str(metric.get("description") or "")) >= 20, (
            f"{metric['metric_id']}: description 이 없거나 너무 짧다"
        )
