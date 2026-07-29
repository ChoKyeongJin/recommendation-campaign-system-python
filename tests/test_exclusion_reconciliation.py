from __future__ import annotations

from typing import Any

import pytest

import graph_rag


def _gender_plan(
    *, include: Any = "female", exclude: Any = None
) -> dict[str, Any]:
    return {
        "target_user": {"gender": include},
        "exclude": {"gender": ["male"] if exclude is None else exclude},
    }


def _region_rule(*, clear_value: Any = None) -> graph_rag.ExclusionReconciliationRule:
    return graph_rag.ExclusionReconciliationRule(
        include_path=("target_user", "region"),
        exclude_path=("exclude", "region"),
        signature_key="regions",
        allowed_values=frozenset({"seoul", "busan", "daegu"}),
        filter_name="deterministic_region_exclusion_reconciliation",
        reason="원문에 없는 포함 지역을 제거하고 명시된 지역 제외 조건을 우선",
        clear_value=clear_value,
        include_mode="collection",
    )


def test_excluded_gender_clears_ungrounded_complement_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(graph_rag.plan_decisions, "record", lambda *_a, **kw: calls.append(kw))
    plan = _gender_plan()

    graph_rag._reconcile_deterministic_member_exclusions("남성을 빼줘", plan)

    assert plan["target_user"]["gender"] is None
    assert plan["exclude"]["gender"] == ["male"]
    assert len(calls) == 1


def test_explicit_positive_gender_is_preserved() -> None:
    plan = _gender_plan()

    graph_rag._reconcile_deterministic_member_exclusions(
        "여성만 대상으로 하고 남성은 빼줘", plan
    )

    assert plan["target_user"]["gender"] == "female"


def test_same_gender_include_is_cleared_when_source_is_exclude_only() -> None:
    plan = _gender_plan(include="female", exclude=["female"])

    graph_rag._reconcile_deterministic_member_exclusions("여자만 빼줘", plan)

    assert plan["target_user"]["gender"] is None
    assert plan["exclude"]["gender"] == ["female"]


def test_same_gender_real_include_and_exclude_remains_a_conflict() -> None:
    plan = _gender_plan(include="female", exclude=["female"])

    graph_rag._reconcile_deterministic_member_exclusions(
        "여성은 포함하고 여성은 제외해줘", plan
    )

    assert plan["target_user"]["gender"] == "female"
    assert plan["exclude"]["gender"] == ["female"]


def test_unmentioned_excluded_value_does_not_clear_include() -> None:
    plan = _gender_plan()

    graph_rag._reconcile_deterministic_member_exclusions("여성 고객을 추출해줘", plan)

    assert plan["target_user"]["gender"] == "female"


def test_disallowed_include_value_is_unchanged() -> None:
    plan = _gender_plan(include="unknown-value")

    graph_rag._reconcile_deterministic_member_exclusions("남성을 빼줘", plan)

    assert plan["target_user"]["gender"] == "unknown-value"


def test_disallowed_exclude_values_do_not_clear_include() -> None:
    plan = _gender_plan(exclude=["unknown-value"])

    graph_rag._reconcile_deterministic_member_exclusions("남성을 빼줘", plan)

    assert plan["target_user"]["gender"] == "female"


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"target_user": None, "exclude": {"gender": ["male"]}},
        {"target_user": {"gender": "female"}, "exclude": None},
        {"target_user": {"gender": "female"}, "exclude": {"gender": 7}},
    ],
)
def test_missing_or_malformed_nested_paths_exit_safely(plan: dict[str, Any]) -> None:
    graph_rag._reconcile_deterministic_member_exclusions("남성을 빼줘", plan)


def test_signature_is_extracted_once_for_multiple_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def signature(_query: str) -> dict[str, set[str]]:
        nonlocal calls
        calls += 1
        return {"genders": {"male"}, "regions": {"busan"}}

    monkeypatch.setattr(graph_rag, "_prompt_signal_signature", signature)
    monkeypatch.setattr(
        graph_rag,
        "EXCLUSION_RECONCILIATION_RULES",
        (graph_rag.EXCLUSION_RECONCILIATION_RULES[0], _region_rule(clear_value=[])),
    )
    plan = {
        "target_user": {"gender": "female", "region": ["daegu"]},
        "exclude": {"gender": ["male"], "region": ["busan"]},
    }

    graph_rag._reconcile_deterministic_member_exclusions("복합 제외 요청", plan)

    assert calls == 1
    assert plan["target_user"] == {"gender": None, "region": []}


def test_decision_log_uses_rule_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(graph_rag.plan_decisions, "record", lambda *_a, **kw: calls.append(kw))
    rule = graph_rag.EXCLUSION_RECONCILIATION_RULES[0]
    plan = _gender_plan()
    query = "남성을 빼줘"

    graph_rag._reconcile_grounded_exclusion(
        query, plan, rule, signature={"genders": {"male"}}
    )

    assert calls == [
        {
            "filter_name": rule.filter_name,
            "action": graph_rag.plan_decisions.CLEAR,
            "slot": "target_user.gender",
            "reason": rule.reason,
            "value": None,
            "previous": "female",
            "evidence": query,
        }
    ]


def test_collection_mode_removes_only_ungrounded_includes() -> None:
    plan = {
        "target_user": {"region": ["seoul", "daegu"]},
        "exclude": {"region": ["busan"]},
    }

    graph_rag._reconcile_grounded_exclusion(
        "서울 사용자를 대상으로 하고 부산은 빼줘",
        plan,
        _region_rule(clear_value=[]),
        signature={"regions": {"seoul", "busan"}},
    )

    assert plan["target_user"]["region"] == ["seoul"]
    assert plan["exclude"]["region"] == ["busan"]


@pytest.mark.parametrize(
    ("included", "expected"),
    [
        (["seoul", "daegu"], []),
        (("seoul", "daegu"), None),
        ({"seoul", "daegu"}, frozenset()),
    ],
)
def test_collection_mode_applies_clear_value_when_all_are_removed(
    included: Any, expected: Any
) -> None:
    clear_value = expected
    plan = {
        "target_user": {"region": included},
        "exclude": {"region": ["busan"]},
    }

    graph_rag._reconcile_grounded_exclusion(
        "부산은 빼줘",
        plan,
        _region_rule(clear_value=clear_value),
        signature={"regions": {"busan"}},
    )

    assert plan["target_user"]["region"] == expected


def test_collection_mode_preserves_original_container_for_partial_removal() -> None:
    rule = _region_rule(clear_value=None)
    for included, expected_type in (
        (("seoul", "daegu"), tuple),
        ({"seoul", "daegu"}, set),
        (frozenset({"seoul", "daegu"}), frozenset),
    ):
        plan = {
            "target_user": {"region": included},
            "exclude": {"region": ["busan"]},
        }

        graph_rag._reconcile_grounded_exclusion(
            "서울 사용자를 대상으로 하고 부산은 빼줘",
            plan,
            rule,
            signature={"regions": {"seoul", "busan"}},
        )

        assert isinstance(plan["target_user"]["region"], expected_type)
        assert set(plan["target_user"]["region"]) == {"seoul"}


def test_nested_helpers_do_not_create_or_replace_intermediate_objects() -> None:
    plan: dict[str, Any] = {"target_user": {"gender": "female"}}

    assert graph_rag._get_nested(plan, ("target_user", "gender")) == "female"
    assert graph_rag._get_nested(plan, ("exclude", "gender")) is None
    assert graph_rag._set_nested(plan, ("exclude", "gender"), []) is False
    assert graph_rag._set_nested(plan, ("target_user", "missing"), None) is False
    assert plan == {"target_user": {"gender": "female"}}
