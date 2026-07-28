"""조건 결정 감사 로그(plan["decisions"]).

플랜의 최종 모습만으로는 "이 조건이 왜 SQL 에 없나"에 답할 수 없다 — 파싱이 안 된 것인지, 다른
조건이 소유권을 가져간 것인지, 스테이지가 지운 것인지, 빌더가 표현 못한 것인지가 구분되지 않는다.
여기서 강제하는 계약:

  1. 모든 기록은 (filter, action, slot, reason) 네 필드를 갖는다.
  2. 결정론 필터·정규화 규칙이 슬롯을 채우면 그 사실이 남는다(스테이지별 등록 없이 차이로 잡는다).
  3. 소유권 회수(slot_ownership)와 스테이지 드롭은 사유와 함께 남고, 같은 변화를 두 번 남기지 않는다.
  4. 어느 빌더가 왜 채택/거부됐는지 남는다.
  5. 같은 판정의 중복 적재는 접는다(파이프라인은 결정론 스테이지를 두 번 돈다).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plan_decisions
import slot_ownership
from graph_rag import build_query_plan, build_sql_template_candidate


REQUIRED_FIELDS = ("filter", "action", "slot", "reason")


def _slots(plan: dict, action: str | None = None) -> set[str]:
    return {
        entry["slot"] for entry in plan_decisions.decisions(plan)
        if action is None or entry["action"] == action
    }


def test_every_decision_carries_the_four_audit_fields() -> None:
    """감사 로그의 계약은 네 필드다 — 하나라도 비면 '누가 왜'를 못 읽는다."""
    plan = build_query_plan("30대 여성 고객", parser="rules")
    entries = plan_decisions.decisions(plan)

    assert entries, "결정론 파싱이 아무 결정도 남기지 않았다"
    for entry in entries:
        for field in REQUIRED_FIELDS:
            assert isinstance(entry.get(field), str) and entry[field], f"{field} 누락: {entry}"
        assert entry["action"] in plan_decisions.ACTIONS
        assert entry["seq"] == entries.index(entry)


def test_deterministic_filter_records_the_slot_it_filled() -> None:
    """필터가 채운 슬롯은 필터 이름과 함께 남는다(어느 필터가 이 값을 만들었나)."""
    plan = build_query_plan("30대 여성 고객", parser="rules")

    age = [
        entry for entry in plan_decisions.decisions(plan)
        if entry["slot"] == "target_user.age_min"
    ]
    assert age, "연령 조건의 출처가 기록되지 않았다"
    assert age[0]["filter"] == "filter:age"
    assert age[0]["action"] == plan_decisions.SET
    assert age[0]["value"] == 30


def test_normalization_rule_records_the_text_it_read() -> None:
    """어휘 정규화가 만든 조건은 규칙 id 와 원문 표현을 근거로 남긴다(출처 없는 조건 금지)."""
    plan = build_query_plan("여성 고객에게 쿠폰 발송", parser="rules")

    gender = [
        entry for entry in plan_decisions.decisions(plan)
        if entry["slot"] == "target_user.gender"
    ]
    assert gender, "성별 조건의 출처가 기록되지 않았다"
    assert gender[0]["filter"].startswith("normalization:")
    assert "여성" in gender[0]["evidence"]


def test_reclaimed_condition_is_logged_as_a_claim_with_its_owner() -> None:
    """소유권 회수는 '조용한 삭제'가 아니라 소유자·사유가 붙은 claim 으로 남는다."""
    plan = build_query_plan("2019년 가장 많이 팔린 상품 10개를 구매한 고객", parser="rules")

    claims = [
        entry for entry in plan_decisions.decisions(plan)
        if entry["action"] == plan_decisions.CLAIM and entry["filter"] == "entity_set_condition"
    ]
    assert claims, "순위 절의 소유권 회수가 감사 로그에 없다"
    assert all(entry["reason"] for entry in claims)
    # superseded_conditions 와 같은 판정을 보여야 한다(두 기록이 갈라지면 진단이 서로를 반박한다).
    superseded = {
        record["slot"] for record in slot_ownership.superseded_conditions(plan)
        if record["outcome"] == "removed"
    }
    assert {entry["slot"] for entry in claims} <= superseded


def test_preserved_condition_is_logged_as_keep_not_silence() -> None:
    """다른 절 소유라 보존한 조건도 기록한다 — 회수 시도 자체가 진단 정보다."""
    plan = build_query_plan("2019년 가장 많이 팔린 상품 10개를 2020년 3월에 구매한 고객", parser="rules")

    kept = [
        entry for entry in plan_decisions.decisions(plan)
        if entry["action"] == plan_decisions.KEEP and entry["slot"] == "target_user.purchase_date"
    ]
    assert kept, "보존 판정이 감사 로그에 없다"


def test_builder_selection_is_recorded() -> None:
    """어느 빌더가 SQL 을 만들었는지 남는다 — SQL 문자열 역추적 없이 답하기 위함."""
    plan = dict(build_query_plan("30대 여성 고객", parser="rules"))
    candidate = build_sql_template_candidate(plan)

    assert candidate is not None
    selected = [
        entry for entry in plan_decisions.decisions(plan)
        if entry["action"] == plan_decisions.SELECT and entry["slot"] == "plan.sql"
    ]
    assert selected, "채택된 빌더가 기록되지 않았다"
    assert selected[0]["filter"].startswith("builder:")


def test_snapshot_diff_separates_set_update_and_clear() -> None:
    """빈 슬롯 채움/값 교체/비움은 서로 다른 액션이다 — 선초기화는 결정이 아니다."""
    plan: dict = {"target_user": {"gender": None}, "intent": "find_user_segment"}
    before = plan_decisions.snapshot(plan)
    assert "target_user.gender" not in before  # 빈 값은 스냅샷에 없다

    plan["target_user"]["gender"] = "female"
    plan_decisions.record_changes(plan, before, filter_name="f", reason="r", since=0)
    assert _slots(plan, plan_decisions.SET) == {"target_user.gender"}

    before = plan_decisions.snapshot(plan)
    since = len(plan_decisions.decisions(plan))
    plan["target_user"]["gender"] = "male"
    plan_decisions.record_changes(plan, before, filter_name="f", reason="r", since=since)
    assert _slots(plan, plan_decisions.UPDATE) == {"target_user.gender"}

    before = plan_decisions.snapshot(plan)
    since = len(plan_decisions.decisions(plan))
    plan["target_user"]["gender"] = None
    plan_decisions.record_changes(plan, before, filter_name="f", reason="r", since=since)
    assert _slots(plan, plan_decisions.CLEAR) == {"target_user.gender"}


def test_explicit_drop_is_not_also_logged_as_an_anonymous_clear() -> None:
    """사유 있는 드롭을 사유 없는 차이 기록이 덮어쓰면 로그가 이유를 잃는다."""
    plan: dict = {"target_user": {"gender": "female"}, "intent": "find_user_segment"}
    before = plan_decisions.snapshot(plan)
    since = len(plan_decisions.decisions(plan))

    plan_decisions.drop_slots(
        plan, (("target_user", "gender"),), owner="analytical_contract", mode="clear",
        reason="집계 계약이 소유",
    )
    plan_decisions.record_changes(plan, before, filter_name="stage", reason="스테이지", since=since)

    entries = plan_decisions.decisions(plan)
    assert len(entries) == 1
    assert entries[0]["action"] == plan_decisions.DROP
    assert entries[0]["reason"] == "집계 계약이 소유"


def test_drop_slots_removes_only_the_named_list_value() -> None:
    """리스트 슬롯은 소유한 값만 빼고 나머지 조건은 남긴다(기록도 그 값만)."""
    plan: dict = {"target_user": {"lifecycle": ["vip", "app_user"]}}

    plan_decisions.drop_slots(
        plan, (("target_user", "lifecycle:vip"),), owner="analytical_contract", reason="집계 필터가 소유",
    )

    assert plan["target_user"]["lifecycle"] == ["app_user"]
    assert plan_decisions.decisions(plan)[0]["value"] == "vip"


def test_no_op_drop_is_not_a_decision() -> None:
    """값이 없던 슬롯을 지우는 건 결정이 아니다 — 무동작까지 기록하면 로그가 잡음이 된다."""
    plan: dict = {"target_user": {}}

    plan_decisions.drop_slots(
        plan, (("target_user", "gender"),), owner="analytical_contract", reason="집계 계약이 소유",
    )

    assert plan_decisions.decisions(plan) == []


def test_same_verdict_recorded_twice_collapses() -> None:
    """파이프라인은 결정론 스테이지를 두 번 돈다 — 같은 판정이 두 줄이면 로그를 못 읽는다."""
    plan: dict = {}
    for _ in range(2):
        plan_decisions.record(
            plan, filter_name="filter:age", action=plan_decisions.SET,
            slot="target_user.age_min", reason="매치", value=30,
        )

    assert len(plan_decisions.decisions(plan)) == 1


def test_log_is_capped_and_says_so() -> None:
    """상한에 걸려 잘린 사실을 숨기면 '전부 기록됐다'로 읽힌다."""
    plan: dict = {}
    for index in range(plan_decisions.MAX_DECISIONS + 5):
        plan_decisions.record(
            plan, filter_name="f", action=plan_decisions.SET, slot=f"target_user.s{index}", reason="r",
        )

    assert len(plan_decisions.decisions(plan)) == plan_decisions.MAX_DECISIONS
    assert plan[plan_decisions.TRUNCATED_KEY] is True
