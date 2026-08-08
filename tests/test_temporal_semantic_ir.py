"""상위 시간 IR 의 고정 계약 — 닫힌 어휘, 조합 검증, 직렬화 round-trip, 연산자 이름 파생.

이 파일이 재는 것은 **의미 표현**이지 SQL 이 아니다. 데이터베이스에 연결하지 않고 카탈로그도
읽지 않는다 — 상위 IR 이 실행 계층을 모른다는 것이 이 계층의 존재 이유이므로, 그 사실이
테스트의 의존 관계에도 그대로 드러나야 한다.
"""

from __future__ import annotations

import ast
import json
import sys
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from temporal_ir import operators as tops  # noqa: E402
from temporal_ir import registry as treg  # noqa: E402
from temporal_ir import semantic_ir as sir  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
EVIDENCE = sir.Evidence(text="지난달 말 기준 VIP", start=6, end=18)


def _state(value: str = "vip") -> sir.StatePredicate:
    return sir.StatePredicate(comparison=sir.Comparison(operator="=", value=value))


def _condition(**overrides) -> sir.TemporalCondition:
    payload = {
        "metric": "member.grade",
        "binding": "member.grade.monthly_snapshot",
        "selector": sir.AsOfSelector(
            anchor=sir.RelativeAnchor(offset=-1, unit="month", boundary="end"),
            strategy=sir.ExactBucket(),
        ),
        "quantifier": sir.ExistsQuantifier(),
        "predicate": _state(),
        "evidence": EVIDENCE,
    }
    payload.update(overrides)
    return sir.TemporalCondition(**payload)


# ── 닫힌 어휘와 조합 검증 ─────────────────────────────────────────────────────────


def test_unknown_vocabulary_values_are_rejected_at_construction() -> None:
    with pytest.raises(sir.TemporalIrError):
        sir.RelativeAnchor(offset=-1, unit="fortnight", boundary="end")
    with pytest.raises(sir.TemporalIrError):
        sir.StatePredicate(comparison=sir.Comparison(operator="=", value="vip"), null_policy="maybe")
    with pytest.raises(sir.TemporalIrError):
        sir.Comparison(operator="approximately", value="vip")


def test_naive_datetimes_are_rejected_everywhere() -> None:
    naive = datetime(2026, 8, 5, 9, 0)
    with pytest.raises(sir.TemporalIrError):
        sir.AbsoluteAnchor(instant=naive)
    with pytest.raises(sir.TemporalIrError):
        sir.TemporalRequestContext(now=naive)
    with pytest.raises(sir.TemporalIrError):
        sir.AbsoluteWindow(start=naive, end_exclusive=datetime(2026, 9, 1, tzinfo=SEOUL))


def test_rolling_window_cannot_exclude_the_current_bucket() -> None:
    """롤링은 칸이 아니라 길이다 — '현재 칸 제외'라는 개념 자체가 없다."""
    with pytest.raises(sir.TemporalIrError):
        sir.RelativeWindow(amount=30, unit="day", mode="rolling", include_current=False)


def test_calendar_window_requires_an_explicit_current_bucket_decision() -> None:
    """'최근 3개월'의 세 가지 뜻 중 하나를 기본값으로 고르지 않는다."""
    with pytest.raises(TypeError):
        sir.RelativeWindow(amount=3, unit="month", mode="calendar")  # type: ignore[call-arg]


def test_change_needs_two_different_values() -> None:
    with pytest.raises(sir.TemporalIrError):
        sir.TransitionPredicate(
            from_value="vip", to_value="vip", transition_mode="direct_observed_transition"
        )
    with pytest.raises(sir.TemporalIrError):
        sir.ValueChange(from_value="vip", to_value="vip")


def test_self_relation_requires_an_explicit_anchor_occurrence() -> None:
    """'구매 후 30일 이내 재구매'는 첫 구매인지 마지막 구매인지 정해야 집합이 확정된다."""
    with pytest.raises(sir.TemporalIrError):
        sir.TemporalRelationPredicate(
            left_binding="member.purchase.events",
            right_binding="member.purchase.events",
            relation="within_after",
            duration=sir.Duration(amount=30, unit="day"),
        )


# ── 직렬화 round-trip ────────────────────────────────────────────────────────────

ROUND_TRIP_CASES = {
    "as_of_snapshot": _condition(),
    "as_of_reference": _condition(
        binding="member.grade.current",
        selector=sir.AsOfSelector(anchor=sir.ReferenceAnchor(), strategy=sir.ExactBucket()),
    ),
    "as_of_absolute_anchor": _condition(
        selector=sir.AsOfSelector(
            anchor=sir.AbsoluteAnchor(instant=datetime(2026, 7, 31, 23, 59, tzinfo=SEOUL)),
            strategy=sir.LatestAtOrBefore(),
            missing_policy=sir.MissingPolicy.ERROR,
        )
    ),
    "window_absolute": _condition(
        selector=sir.WindowSelector(
            window=sir.AbsoluteWindow(
                start=datetime(2026, 1, 1, tzinfo=SEOUL),
                end_exclusive=datetime(2026, 7, 1, tzinfo=SEOUL),
            )
        ),
        quantifier=sir.NoneQuantifier(),
        predicate=sir.OccurrencePredicate(),
    ),
    "window_relative_calendar": _condition(
        selector=sir.WindowSelector(
            window=sir.RelativeWindow(
                amount=3, unit="month", mode="calendar", include_current=False
            ),
            bucket=sir.TimeUnit.MONTH,
        ),
        quantifier=sir.EveryBucketQuantifier(),
    ),
    "window_lifetime": _condition(
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        predicate=sir.OccurrencePredicate(),
    ),
    "window_open_ended": _condition(
        selector=sir.WindowSelector(
            window=sir.OpenEndedWindow(
                anchor=sir.RelativeAnchor(offset=0, unit="month", boundary="start"),
                direction=sir.Direction.BEFORE,
            )
        )
    ),
    "previous_distinct_value": _condition(
        selector=sir.PreviousSelector(
            anchor=sir.ReferenceAnchor(), previous_kind=sir.PreviousKind.DISTINCT_VALUE
        )
    ),
    "transition": _condition(
        predicate=sir.TransitionPredicate(
            from_value="gold_grade", to_value="vip", transition_mode="direct_observed_transition"
        )
    ),
    "change_count": _condition(
        selector=sir.WindowSelector(
            window=sir.RelativeWindow(amount=3, unit="month", mode="calendar", include_current=True)
        ),
        predicate=sir.ChangeCountPredicate(
            transition=sir.AnyValueChange(),
            comparison=sir.NumericComparison(operator=">=", value=Decimal("2")),
        ),
    ),
    "unchanged": _condition(
        selector=sir.WindowSelector(
            window=sir.RelativeWindow(amount=3, unit="month", mode="calendar", include_current=True)
        ),
        quantifier=sir.AllObservationsQuantifier(),
        predicate=sir.UnchangedPredicate(observation_semantics="observed_values_equal"),
    ),
    "temporal_relation": _condition(
        metric="member.purchase",
        binding="member.purchase.events",
        selector=sir.WindowSelector(window=sir.AllAvailableDataWindow()),
        predicate=sir.TemporalRelationPredicate(
            left_binding="member.purchase.events",
            right_binding="member.signup.event",
            relation="within_after",
            duration=sir.Duration(amount=7, unit="day"),
        ),
    ),
}


@pytest.mark.parametrize("name", sorted(ROUND_TRIP_CASES))
def test_semantic_ir_json_round_trip_preserves_meaning(name: str) -> None:
    """JSON 을 거쳐도 같은 조건이어야 한다 — 저장한 오디언스가 다른 집합이 되지 않도록."""
    condition = ROUND_TRIP_CASES[name]
    wire = json.loads(json.dumps(condition.to_dict(), ensure_ascii=False))
    restored = sir.condition_from_dict(wire)
    assert restored == condition
    assert restored.to_dict() == condition.to_dict()


def test_round_trip_preserves_decimal_precision() -> None:
    predicate = sir.ChangeCountPredicate(
        transition=sir.AnyValueChange(),
        comparison=sir.NumericComparison(operator=">=", value=Decimal("2.50")),
    )
    restored = sir.predicate_from_dict(json.loads(json.dumps(predicate.to_dict())))
    assert isinstance(restored, sir.ChangeCountPredicate)
    assert restored.comparison.value == Decimal("2.50")
    assert str(restored.comparison.value) == "2.50"


def test_round_trip_preserves_timezone_offset() -> None:
    window = sir.AbsoluteWindow(
        start=datetime(2026, 7, 1, tzinfo=SEOUL), end_exclusive=datetime(2026, 8, 1, tzinfo=SEOUL)
    )
    restored = sir.window_from_dict(json.loads(json.dumps(window.to_dict())))
    assert isinstance(restored, sir.AbsoluteWindow)
    assert restored.start.utcoffset() == window.start.utcoffset()
    assert restored == window


# ── 구조 → 연산자 이름 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("as_of_snapshot", treg.AS_OF),
        ("window_absolute", treg.NONE_IN_WINDOW),
        ("window_relative_calendar", treg.EVERY_BUCKET),
        ("window_lifetime", treg.IN_WINDOW),
        ("window_open_ended", treg.BEFORE),
        ("previous_distinct_value", treg.PREVIOUS_DISTINCT_VALUE),
        ("transition", treg.DIRECT_TRANSITION),
        ("change_count", treg.CHANGE_COUNT),
        ("unchanged", treg.UNCHANGED_OBSERVATIONS),
        ("temporal_relation", treg.WITHIN_AFTER),
    ],
)
def test_operator_name_is_derived_from_the_composition(case: str, expected: str) -> None:
    """생산자가 연산자 이름을 짓지 않는다 — 조합이 이름을 정한다."""
    assert treg.resolve_operator_name(ROUND_TRIP_CASES[case]) == expected


def test_previous_kinds_resolve_to_three_distinct_operators() -> None:
    names = {
        kind: treg.resolve_operator_name(
            _condition(
                selector=sir.PreviousSelector(anchor=sir.ReferenceAnchor(), previous_kind=kind)
            )
        )
        for kind in sir.PreviousKind
    }
    assert len(set(names.values())) == 3


# ── 별칭과 레지스트리 ────────────────────────────────────────────────────────────


def test_legacy_uppercase_qualifiers_are_input_aliases_only() -> None:
    """기존 대문자 표기는 입력에서만 인정하고, 정본 이름은 namespace 표기 하나다."""
    registry = tops.create_default_temporal_operator_registry()
    assert treg.canonical_operator_name("AS_OF") == treg.AS_OF
    assert treg.canonical_operator_name("NEVER_IN_INTERVAL") == treg.NONE_IN_WINDOW
    assert treg.canonical_operator_name("temporal.any") == treg.IN_WINDOW
    assert treg.canonical_operator_name("정말없는연산") is None
    assert registry.get("CHANGE_BETWEEN").name == treg.DIRECT_TRANSITION
    assert all(name.startswith("temporal.") for name in registry.names())


def test_registry_rejects_duplicate_and_unknown_operators() -> None:
    registry = tops.create_default_temporal_operator_registry()
    with pytest.raises(treg.TemporalRegistryError):
        registry.register(registry.get(treg.AS_OF))
    with pytest.raises(treg.TemporalRegistryError):
        registry.get("temporal.made_up")


def test_every_registered_operator_either_lowers_or_explains_itself() -> None:
    """lowerer 가 없는 연산자는 **사유를 선언한다** — 오타와 미구현은 다른 결말이어야 한다."""
    registry = tops.create_default_temporal_operator_registry()
    for name, entry in registry.documentation().items():
        assert entry["lowerable"] or entry["unsupported_reason"], name


def test_operator_contracts_reference_only_declared_capabilities() -> None:
    from temporal_ir import catalog as tcat

    registry = tops.create_default_temporal_operator_registry()
    for name, entry in registry.documentation().items():
        assert set(entry["required_capabilities"]) <= set(tcat.FLAG_NAMES), name


# ── 계층 경계(§19-4·5·6) ─────────────────────────────────────────────────────────

FORBIDDEN_SUBSTRINGS = (
    # 물리 컬럼 — 이 이름을 아는 순간 새 속성마다 코드가 바뀐다.
    "ZTS_GRADE", "MEMBER_NO", "EMART_GRADE_CD", "YYYYMM", "CRM_MB", "PREV_",
    # 도메인 값 — canonical 도 물리 코드도 프레임워크의 어휘가 아니다.
    "MEM_GRADE_CD", "gold_grade", "vip",
)


def _code_strings(path: Path) -> list[str]:
    """docstring 을 제외한 코드 안의 문자열 상수(주석은 AST 에 없으므로 자동 제외)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


FRAMEWORK_MODULES = sorted(path.name for path in (REPO_ROOT / "temporal_ir").glob("*.py"))


@pytest.mark.parametrize("module", FRAMEWORK_MODULES)
def test_framework_code_has_no_physical_or_domain_value_literals(module: str) -> None:
    """컬럼명·값 코드가 코드 상수로 들어오면 그 순간 새 속성마다 Python 이 바뀐다."""
    path = REPO_ROOT / "temporal_ir" / module
    offenders = [
        (text, token)
        for text in _code_strings(path)
        for token in FORBIDDEN_SUBSTRINGS
        if token in text
    ]
    assert not offenders, offenders


def _has_hangul(text: str) -> bool:
    return any("가" <= char <= "힣" for char in text)


def _phrase_branches(path: Path) -> list[str]:
    """한국어 **표면어로 분기**하는 지점(비교·매칭·표 조회). 메시지 문자열은 분기가 아니다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            offenders.extend(
                item.value
                for item in operands
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and _has_hangul(item.value)
            )
        elif isinstance(node, ast.Dict):
            offenders.extend(
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and _has_hangul(key.value)
            )
        elif isinstance(node, ast.MatchValue):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                if _has_hangul(node.value.value):
                    offenders.append(node.value.value)
    return offenders


@pytest.mark.parametrize("module", FRAMEWORK_MODULES)
def test_framework_never_branches_on_korean_surface_phrases(module: str) -> None:
    """표면어의 소유자는 도메인 계층이다 — 프레임워크는 그 낱말을 알지 못한다."""
    assert _phrase_branches(REPO_ROOT / "temporal_ir" / module) == []
