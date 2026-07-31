"""shadow 비교 계약 — 관찰이 프로덕션 경로를 바꾸지 않는다.

shadow 의 유일한 약속은 "결과를 바꾸지 않는다"이다. 그 약속이 깨지면 관찰이 곧 사고가 되므로,
여기서 강제한다: 비교는 두 플랜 중 어느 것도 변경하지 않고, 계측 키는 조건 IR 에 섞이지 않는다.
"""

from __future__ import annotations

import copy
import json

import ir_snapshot
import parser_shadow


def _plan(**target_user) -> dict:
    return {"intent": "find_user_segment", "target_user": dict(target_user)}


def test_identical_plans_agree() -> None:
    plan = _plan(gender="female", age_min=30)
    result = parser_shadow.compare(plan, copy.deepcopy(plan))
    assert result["agreed"] is True
    # gender / age_min / intent — intent 도 조건 IR 슬롯이다.
    assert result["counts"][parser_shadow.AGREE] == 3
    assert parser_shadow.divergent_slots(result) == []


def test_verdicts_cover_the_four_relations() -> None:
    baseline = _plan(gender="female", age_min=30, purchase_date={"from": "20190301"})
    candidate = _plan(gender="male", age_min=30, birthday_target={"mmdd": "0301"})
    result = parser_shadow.compare(baseline, candidate)
    slots = result["slots"]

    assert slots["target_user.age_min"]["verdict"] == parser_shadow.AGREE
    assert slots["target_user.gender"]["verdict"] == parser_shadow.VALUE_DIFFERS
    assert slots["target_user.purchase_date"]["verdict"] == parser_shadow.ONLY_BASELINE
    assert slots["target_user.birthday_target"]["verdict"] == parser_shadow.ONLY_CANDIDATE
    # 불일치한 칸만 값을 싣는다(일치한 칸까지 실으면 로그가 플랜 복사본이 된다).
    assert "baseline" not in slots["target_user.age_min"]
    assert slots["target_user.gender"] == {
        "verdict": parser_shadow.VALUE_DIFFERS, "baseline": "female", "candidate": "male",
    }


def test_compare_does_not_mutate_either_plan() -> None:
    baseline, candidate = _plan(gender="female"), _plan(age_min=20)
    before = (copy.deepcopy(baseline), copy.deepcopy(candidate))
    parser_shadow.compare(baseline, candidate)
    assert (baseline, candidate) == before


def test_shadow_measurement_is_not_a_condition() -> None:
    """계측 키가 조건 IR 로 새면 골든이 관찰 때문에 깨진다."""
    plan = _plan(gender="female")
    snapshot_before = ir_snapshot.snapshot(plan)
    parser_shadow.attach(plan, parser_shadow.compare(plan, plan))
    assert ir_snapshot.snapshot(plan) == snapshot_before
    assert parser_shadow.SHADOW_KEY in ir_snapshot.DERIVED_PLAN_KEYS
    assert ir_snapshot.unclassified_plan_keys(plan) == []


def test_mode_falls_back_to_off_on_typos(monkeypatch) -> None:
    monkeypatch.setenv("PARSER_SHADOW_MODE", "shaddow")
    assert parser_shadow.mode() == parser_shadow.MODE_OFF
    assert parser_shadow.enabled() is False
    monkeypatch.setenv("PARSER_SHADOW_MODE", "shadow")
    assert parser_shadow.enabled() is True


def test_record_is_a_noop_without_a_log_path(monkeypatch) -> None:
    monkeypatch.delenv(parser_shadow.LOG_PATH_ENV, raising=False)
    assert parser_shadow.record(parser_shadow.compare(_plan(gender="f"), _plan(gender="f"))) is False


def test_observations_round_trip_and_aggregate(tmp_path) -> None:
    log = tmp_path / "shadow.jsonl"
    for _ in range(3):
        parser_shadow.record(parser_shadow.compare(_plan(gender="female"), _plan(gender="female")), path=log)
    parser_shadow.record(parser_shadow.compare(_plan(gender="female"), _plan(gender="male")), path=log)

    rates = parser_shadow.agreement_by_slot(parser_shadow.load_observations(log))
    stats = rates["target_user.gender"]
    assert stats["observed"] == 4
    assert stats["agree"] == 3
    assert stats["rate"] == 0.75
    assert stats["risky"] == 1  # value_differs 는 위험 판정


def test_corrupt_log_lines_are_skipped(tmp_path) -> None:
    log = tmp_path / "shadow.jsonl"
    log.write_text('{"slots": {"a.b": "agree"}}\n{ broken\n\n', encoding="utf-8")
    assert len(parser_shadow.load_observations(log)) == 1


def test_record_survives_unwritable_paths(tmp_path) -> None:
    """관찰 실패가 요청을 깨뜨리면 안 된다."""
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert parser_shadow.record(parser_shadow.compare(_plan(gender="f"), _plan()), path=blocker / "nested.jsonl") is False


def test_comparison_is_json_serializable() -> None:
    result = parser_shadow.compare(_plan(gender="female"), _plan(age_min=20))
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result


# ── 의미 동일성: 출처가 달라도 뜻이 같으면 같다 ────────────────────────────────────────────
# 두 경로는 애초에 서로 다른 입력 문자열(원문 / 재작성·절 분리본)을 본다. 그래서 근거 텍스트·좌표는
# **항상** 다르다. 그것을 불일치로 세면 의미가 동일한 해석까지 위험 칸이 되고, LLM-first 경로에서는
# 그 한 칸 때문에 SQL 생성이 통째로 막힌다(llm_legacy_semantic_disagreement).


def _purchase_plan(product: str, source_text: str) -> dict:
    """같은 뜻을 서로 다른 입력에서 읽은 조건 IR(출처 필드만 다르다)."""
    return {
        "intent": "purchase_history",
        "event_expression": {
            "expression": {
                "type": "exists",
                "relation": {
                    "type": "filter",
                    "relation": {"type": "source", "name": "purchase"},
                    "where": {
                        "type": "comparison",
                        "operator": "=",
                        "left": {"type": "field", "name": "purchase.product"},
                        "right": {"type": "literal", "value": product},
                        "evidence": {"text": product, "start": 0, "end": len(product)},
                    },
                },
                "evidence": {"text": f"{product} 구매", "start": 0, "end": len(product) + 3},
            },
            "source_text": source_text,
            "evidence_span": [0, len(source_text)],
        },
    }


def test_same_meaning_from_different_sources_is_not_a_divergence() -> None:
    """source 만 다른 두 해석은 게이트를 통과해야 한다(순수 오탐 차단)."""
    baseline = _purchase_plan("노트북", "노트북을 구매한 고객 뽑아줘")
    candidate = _purchase_plan("노트북", "노트북을 구매한 고객")

    result = parser_shadow.compare(baseline, candidate)

    assert result["agreed"] is True
    assert parser_shadow.divergent_slots(result) == []


def test_different_meaning_is_a_divergence_even_with_the_same_source() -> None:
    """반대 방향도 지켜야 한다 — 출처가 같아도 의미가 다르면 같다고 판정하면 안 된다."""
    source = "노트북을 구매한 고객"
    baseline = _purchase_plan("노트북", source)
    candidate = _purchase_plan("모니터", source)

    result = parser_shadow.compare(baseline, candidate)

    assert result["agreed"] is False
    assert parser_shadow.divergent_slots(result) == ["plan.event_expression"]


def test_semantic_form_drops_provenance_but_keeps_conditions() -> None:
    """무엇이 출처인지는 semantic_fields 가 소유한다 — 여기서는 그 경계를 계약으로 못 박는다."""
    value = {
        "operator": "exists",
        "value": "노트북",
        "source": "llm",
        "confidence": 0.97,
        "evidence": {"text": "노트북", "start": 0, "end": 3},
        "nested": [{"operator": "=", "source_text": "원문"}],
    }

    assert parser_shadow.semantic_form(value) == {
        "operator": "exists",
        "value": "노트북",
        "nested": [{"operator": "="}],
    }


def test_product_order_is_meaningful_and_stays_compared() -> None:
    """상품 순서는 의미를 갖는다(첫 상품이 단수 슬롯·라벨로 투영된다) — 집합으로 접지 않는다."""
    baseline = _plan(purchase_objects=[{"value": "노트북"}, {"value": "모니터"}])
    candidate = _plan(purchase_objects=[{"value": "모니터"}, {"value": "노트북"}])

    assert parser_shadow.divergent_slots(parser_shadow.compare(baseline, candidate)) == [
        "target_user.purchase_objects"
    ]
