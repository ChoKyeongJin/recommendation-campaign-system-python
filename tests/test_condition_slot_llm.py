"""조건 슬롯 LLM 보완의 경계 계약.

어휘 사전은 표면 표현을 한 줄씩 쌓는 구조라 처음 보는 말투에는 조용히 침묵한다. 그 빈칸을 LLM 이
메우되, 메우는 범위가 계약으로 묶여 있다.

  * **닫힌 집합에서 고르기만.** canonical 은 attribute_token_groups 가 선언한 것 중 실제 컴파일 가능한
    것뿐이고, 연산자는 segment_semantics 의 연산자 집합뿐이다. 목록 밖은 버린다.
  * **근거가 있어야 한다.** 원문에 그대로 있고, 규칙이 이미 읽은 조각과 겹치지 않고, 회원을 가리키는
    말을 포함해야 한다. ('최근 90일 이내 가입한 신규 회원' 을 멤버십 가입으로 읽던 오탐이 실제로 있었다.)
  * **빈칸만.** 규칙이 채운 슬롯은 덮지 않는다.
  * **지원 여부는 여전히 접지가 판정한다.** LLM 은 연산자·값만 정하고 capability 게이트는 JSON 이 쥔다.

LLM 호출은 스텁으로 갈아끼운다 — 이 테스트는 네트워크를 타지 않는다.
"""

from __future__ import annotations

from typing import Any

import graph_rag as g
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
