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
