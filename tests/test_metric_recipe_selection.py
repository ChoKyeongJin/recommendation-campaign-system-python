"""kind 별 recipe builder registry + 경쟁 recipe 의 결정론 선택.

두 가지를 고정한다.

1. **모든 kind 가 registry 를 지난다.** 회원 행 지표 14종(member_scalar 9 + field 3 +
   transition 2)이 kind 별 예외 분기가 아니라 :data:`audience_runtime.METRIC_RECIPE_BUILDERS`
   한 곳에서 모양을 받는다. 등록되지 않은 kind 는 '가장 비슷한 모양'으로 대신 채우지 않는다.
2. **경쟁 recipe 를 지우지 않고 고른다.** 같은 표면어에 aggregate 계약과 member_scalar 계약이
   함께 걸려 있고 **둘 다 컴파일된다**. 어느 쪽도 지울 수 없으므로 남는 문제는 "겹칠 때 무엇을
   고르는가"이고, 그 답이 후보를 넘긴 순서·등록 순서·완료 순서에 흔들리지 않아야 한다.

선택 기준은 :data:`metric_recipe_selection.SELECTION_CRITERIA` 하나가 소유하므로 여기서 기준을
다시 적지 않는다 — 기준의 **순서**와 **동률이 남지 않는다**는 성질만 잰다.
"""

from __future__ import annotations

import itertools
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import member_scalar_metrics  # noqa: E402
import metric_recipe_selection  # noqa: E402

# 회원당 0..1 행을 읽는 kind 와 그 하한. 조사 문서가 완료 조건으로 못 박은 분포다.
MEMBER_ROW_KIND_FLOOR: dict[str, int] = {
    member_scalar_metrics.MEMBER_SCALAR_KIND: 9,
    "field": 3,
    "transition": 2,
}
MEMBER_ROW_METRIC_FLOOR = sum(MEMBER_ROW_KIND_FLOOR.values())

_METRICS: Mapping[str, Any] = audience_runtime.catalog_snapshot().get("metrics") or {}
_GUIDANCE_LINES: tuple[str, ...] = tuple(audience_runtime.audience_catalog_guidance().splitlines())


def _declarations_of_kind(kind: str) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    return tuple(
        (metric_id, declaration)
        for metric_id, declaration in sorted(_METRICS.items())
        if isinstance(declaration, Mapping)
        and audience_runtime.metric_recipe_builder_kind(declaration) == kind
    )


MEMBER_ROW_METRIC_IDS: tuple[str, ...] = tuple(
    metric_id
    for kind in sorted(MEMBER_ROW_KIND_FLOOR)
    for metric_id, _declaration in _declarations_of_kind(kind)
)


def _mentions_symbol(line: str, symbol: str) -> bool:
    """식별자를 **토큰 단위**로 찾는다.

    부분문자열로 세면 ``member_scalar_activity_month_cnt`` 한 줄이 ``activity_month_cnt`` 안내로도
    계산돼, 선택 규칙이 통째로 사라져도 초록이 된다.
    """
    return re.search(rf"(?<![0-9A-Za-z_]){re.escape(symbol)}(?![0-9A-Za-z_])", line) is not None


def _competing_labels() -> dict[str, dict[str, str]]:
    """같은 label 을 서로 다른 builder kind 가 나눠 가진 쌍(카탈로그 파생)."""
    by_label: dict[str, dict[str, str]] = {}
    for metric_id, declaration in sorted(_METRICS.items()):
        if not isinstance(declaration, Mapping):
            continue
        kind = audience_runtime.metric_recipe_builder_kind(declaration)
        if not kind:
            continue
        by_label.setdefault(str(declaration.get("label") or metric_id), {})[kind] = metric_id
    return {label: owners for label, owners in by_label.items() if len(owners) > 1}


COMPETING_LABELS: dict[str, dict[str, str]] = _competing_labels()


def _candidate(
    recipe_id: str,
    *,
    priority: int = 0,
    span: tuple[int, int] | None = None,
    surface: str | None = None,
    constraint_count: int = 0,
    fallback: bool = False,
) -> metric_recipe_selection.RecipeCandidate:
    return metric_recipe_selection.RecipeCandidate(
        recipe_id=recipe_id,
        kind="probe",
        priority=priority,
        span=span,
        surface=surface,
        constraint_count=constraint_count,
        fallback=fallback,
    )


def _selected_ids_over_every_order(
    candidates: list[metric_recipe_selection.RecipeCandidate],
) -> set[str]:
    """후보 순열 전부에 대해 고른 recipe id 들(하나여야 순서 독립이다)."""
    return {
        selection.selected.recipe_id
        for order in itertools.permutations(candidates)
        if (selection := metric_recipe_selection.select_recipe(order)) is not None
    }


# ── 1. kind 별 builder registry ────────────────────────────────────────────────────


def test_the_registry_covers_every_kind_the_catalog_declares() -> None:
    """카탈로그가 쓰는 kind 중 registry 에 없는 것이 있으면 그 지표는 조용히 사라진다."""

    declared = {
        audience_runtime.metric_recipe_builder_kind(declaration)
        for declaration in _METRICS.values()
        if isinstance(declaration, Mapping)
    }
    assert declared, "카탈로그에 지표 선언이 하나도 없다."
    assert declared <= set(audience_runtime.METRIC_RECIPE_BUILDERS), (
        f"registry 에 builder 가 없는 kind: {sorted(declared - set(audience_runtime.METRIC_RECIPE_BUILDERS))}"
    )


@pytest.mark.parametrize(("kind", "floor"), sorted(MEMBER_ROW_KIND_FLOOR.items()))
def test_every_member_row_kind_builds_its_recipes_through_the_registry(
    kind: str, floor: int
) -> None:
    """member_scalar 9 / field 3 / transition 2 — kind 별 예외가 아니라 같은 registry 를 지난다."""

    declarations = _declarations_of_kind(kind)
    assert len(declarations) >= floor, (
        f"{kind} 선언이 {len(declarations)}개뿐이다 — 하한 {floor} 보다 적다. "
        "선언이 사라지면 이 래칫도 함께 사라진다."
    )
    builder = audience_runtime.METRIC_RECIPE_BUILDERS[kind]
    for metric_id, declaration in declarations:
        wire = audience_runtime.metric_recipe_wire(declaration)
        assert wire is not None, f"{metric_id} 의 recipe 가 없다."
        assert wire == builder.build(declaration), (
            f"{metric_id} 의 recipe 가 registry 의 {kind} builder 산출과 다르다 — "
            "어딘가에 kind 별 예외 분기가 남아 있다."
        )
        assert wire.get("type") == "exists", (
            f"{metric_id} recipe 가 {wire.get('type')!r} 다 — 회원 행 지표는 관계다."
        )


def test_the_registry_builds_the_fourteen_member_row_recipes() -> None:
    """총 14종. 분포가 아니라 **합계**도 함께 못 박는다."""

    assert len(MEMBER_ROW_METRIC_IDS) >= MEMBER_ROW_METRIC_FLOOR
    assert len({metric_id for metric_id in MEMBER_ROW_METRIC_IDS}) == len(MEMBER_ROW_METRIC_IDS)
    assert all(
        audience_runtime.metric_recipe_wire(_METRICS[metric_id]) is not None
        for metric_id in MEMBER_ROW_METRIC_IDS
    )


def test_an_unregistered_kind_gets_no_recipe_instead_of_the_closest_shape() -> None:
    """등록되지 않은 kind 를 가장 비슷한 builder 로 대신 채우지 않는다(규칙 11)."""

    assert "존재하지 않는 kind" not in audience_runtime.METRIC_RECIPE_BUILDERS
    assert (
        audience_runtime.metric_recipe_wire(
            {"kind": "존재하지 않는 kind", "source": "s", "expression_field": "s.f"}
        )
        is None
    )


def test_the_aggregate_function_declaration_still_routes_to_the_aggregate_builder() -> None:
    """집계 함수 선언은 **kind 추론 규칙**이다 — registry 이전의 분기 순서를 그대로 유지한다."""

    declaration = {"kind": "field", "function": "sum", "source": "s", "expression_field": "s.f"}
    assert audience_runtime.metric_recipe_builder_kind(declaration) == "aggregate"
    wire = audience_runtime.metric_recipe_wire(declaration)
    assert wire is not None and wire["type"] == "aggregate"


def test_the_incompleteness_guard_stays_inside_the_member_row_builders() -> None:
    """값 필드 요구를 공통 전제로 끌어올리면 집계·존재 recipe 가 조용히 사라진다."""

    counting = {"kind": "aggregate", "function": "count", "source": "purchase"}
    existence = {"kind": "existence", "source": "purchase"}
    assert audience_runtime.metric_recipe_wire(counting) is not None
    assert audience_runtime.metric_recipe_wire(existence) is not None
    assert (
        audience_runtime.metric_recipe_wire(
            {"kind": member_scalar_metrics.MEMBER_SCALAR_KIND, "source": "s"}
        )
        is None
    )


def test_every_builder_constraint_key_is_a_key_some_declaration_carries() -> None:
    """제약 키가 아무 선언에도 없으면 그 kind 의 구체성 점수는 항상 0 이다(죽은 기준)."""

    declared_keys = {
        key
        for declaration in _METRICS.values()
        if isinstance(declaration, Mapping)
        for key in declaration
    }
    for kind, builder in audience_runtime.METRIC_RECIPE_BUILDERS.items():
        unknown = [key for key in builder.constraint_keys if key not in declared_keys]
        assert not unknown, f"{kind} builder 의 제약 키가 선언에 없다: {unknown}"


# ── 2. 경쟁 recipe: 둘 다 후보로 남고, 하나가 결정론적으로 뽑힌다 ──────────────────


def test_both_competing_recipes_stay_candidates_and_both_compile_to_a_shape() -> None:
    """경쟁 recipe 중 하나를 지우거나 비활성화하지 않았다 — 둘 다 모양이 있다."""

    assert COMPETING_LABELS, (
        "같은 label 을 나눠 가진 지표 쌍이 하나도 없다 — 카탈로그가 바뀌었다면 전제를 다시 확인하라."
    )
    for label, owners in sorted(COMPETING_LABELS.items()):
        assert len(owners) >= 2, label
        for metric_id in owners.values():
            assert audience_runtime.metric_recipe_wire(_METRICS[metric_id]) is not None, (
                f"{label!r} 의 경쟁 후보 {metric_id} 가 recipe 없음으로 죽었다."
            )


@pytest.mark.parametrize("label", sorted(COMPETING_LABELS))
def test_a_competing_pair_resolves_to_one_recipe_whatever_the_order(label: str) -> None:
    """후보를 어떤 순서로 넘겨도 같은 recipe 가 뽑힌다(등록 순서·완료 순서 독립)."""

    candidates = [
        audience_runtime.metric_recipe_candidate(metric_id, _METRICS[metric_id])
        for metric_id in sorted(COMPETING_LABELS[label].values())
    ]
    assert len(_selected_ids_over_every_order(candidates)) == 1


@pytest.mark.parametrize("label", sorted(COMPETING_LABELS))
def test_the_guidance_names_both_competitors_and_the_computed_reason(label: str) -> None:
    """안내의 선택 이유는 문구가 아니라 **계산된 기준과 값**에서 나온다."""

    owners = COMPETING_LABELS[label]
    matching = [
        line
        for line in _GUIDANCE_LINES
        if label in line and all(_mentions_symbol(line, metric_id) for metric_id in owners.values())
    ]
    assert matching, f"{label!r} 쌍을 함께 말하는 안내 줄이 없다."
    line = matching[0]
    selection = metric_recipe_selection.select_recipe(
        audience_runtime.metric_recipe_candidate(metric_id, _METRICS[metric_id])
        for metric_id in sorted(owners.values())
    )
    assert selection is not None
    assert metric_recipe_selection.describe_selection(selection) in line
    assert metric_recipe_selection.CRITERION_LABELS[selection.criterion] in line


# ── 3. 선택 기준: 순서·전순서·최종 동률 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("winner", "loser", "criterion"),
    [
        (
            _candidate("z_low_everything_else", priority=5),
            _candidate("a_high_everything_else", span=(0, 20), constraint_count=9),
            "priority",
        ),
        (
            _candidate("z_real", span=(0, 2)),
            _candidate("a_fallback", span=(0, 20), constraint_count=9, fallback=True),
            "not_fallback",
        ),
        (
            _candidate("z_long", span=(0, 7), surface="평균 구매금액"),
            _candidate("a_short", span=(3, 7), surface="구매금액", constraint_count=9),
            "span_length",
        ),
        (
            _candidate("z_two_tokens", span=(0, 7), surface="총 구매금액"),
            _candidate("a_one_token", span=(0, 7), surface="총구매금액", constraint_count=9),
            "explicit_tokens",
        ),
        (
            _candidate("z_specific", span=(0, 4), surface="구매주기", constraint_count=2),
            _candidate("a_generic", span=(0, 4), surface="구매주기", constraint_count=1),
            "constraints",
        ),
        (
            _candidate("a_alphabetically_first", span=(0, 4), surface="구매주기"),
            _candidate("b_alphabetically_second", span=(0, 4), surface="구매주기"),
            "recipe_id",
        ),
    ],
    ids=["priority", "not_fallback", "span_length", "explicit_tokens", "constraints", "recipe_id"],
)
def test_each_criterion_decides_in_its_declared_order(
    winner: metric_recipe_selection.RecipeCandidate,
    loser: metric_recipe_selection.RecipeCandidate,
    criterion: str,
) -> None:
    """앞선 기준이 이긴다 — 뒤 기준을 아무리 크게 줘도 뒤집히지 않는다."""

    for order in ([winner, loser], [loser, winner]):
        selection = metric_recipe_selection.select_recipe(order)
        assert selection is not None
        assert selection.selected.recipe_id == winner.recipe_id
        assert selection.criterion == criterion
        assert metric_recipe_selection.CRITERION_LABELS[criterion] in (
            metric_recipe_selection.describe_selection(selection)
        )


def test_identical_scores_still_select_deterministically_by_recipe_id() -> None:
    """점수가 완전히 같아도 최종 기준이 남는다 — 무승부로 닫히지 않는다."""

    twins = [
        _candidate("b_twin", span=(0, 4), surface="구매주기", constraint_count=2),
        _candidate("a_twin", span=(0, 4), surface="구매주기", constraint_count=2),
        _candidate("c_twin", span=(0, 4), surface="구매주기", constraint_count=2),
    ]
    assert _selected_ids_over_every_order(twins) == {"a_twin"}


def test_selection_key_is_a_total_order_and_reads_no_insertion_order() -> None:
    """정렬 키가 전순서다 — 서로 다른 후보의 키가 같으면 순서가 삽입 순서로 떨어진다."""

    candidates = [
        audience_runtime.metric_recipe_candidate(metric_id, declaration)
        for metric_id, declaration in sorted(_METRICS.items())
        if isinstance(declaration, Mapping)
    ]
    keys = [metric_recipe_selection.selection_key(candidate) for candidate in candidates]
    assert len(set(keys)) == len(keys)


def test_no_candidates_selects_nothing() -> None:
    assert metric_recipe_selection.select_recipe([]) is None
    assert metric_recipe_selection.resolve_overlapping_candidates([]) == ()


def test_a_single_candidate_reports_that_nothing_competed() -> None:
    selection = metric_recipe_selection.select_recipe([_candidate("only", span=(0, 4))])
    assert selection is not None
    assert selection.runner_up is None
    assert "only" in metric_recipe_selection.describe_selection(selection)


# ── 4. 겹치는 구간만 겨룬다 ────────────────────────────────────────────────────────


def test_candidates_that_do_not_overlap_are_all_kept() -> None:
    """겹치지 않는 후보는 서로 다른 자리를 말한다 — 하나로 줄이면 다른 조건이 사라진다."""

    left = _candidate("left", span=(0, 4), surface="구매주기")
    right = _candidate("right", span=(10, 17), surface="누적 구매금액")
    resolved = metric_recipe_selection.resolve_overlapping_candidates([left, right])
    assert {candidate.recipe_id for candidate in resolved} == {"left", "right"}
    assert metric_recipe_selection.resolve_overlapping_candidates([right, left]) == resolved


def test_a_contained_candidate_loses_to_the_longer_one() -> None:
    """포함 관계는 겹침의 한 경우다 — 긴 쪽이 이겨 종전 동작과 같은 답이 나온다."""

    resolved = metric_recipe_selection.resolve_overlapping_candidates([
        _candidate("short", span=(3, 7), surface="구매금액"),
        _candidate("long", span=(0, 7), surface="평균 구매금액"),
    ])
    assert [candidate.recipe_id for candidate in resolved] == ["long"]


def test_partially_overlapping_candidates_resolve_instead_of_closing() -> None:
    """길이까지 같은 부분 겹침도 후보를 지우지 않고 결정론적으로 하나를 고른다."""

    candidates = [
        _candidate("b_right", span=(2, 6), surface="BCDE"),
        _candidate("a_left", span=(0, 4), surface="ABCD"),
    ]
    for order in (candidates, list(reversed(candidates))):
        resolved = metric_recipe_selection.resolve_overlapping_candidates(order)
        assert [candidate.recipe_id for candidate in resolved] == ["a_left"]


def test_overlap_grouping_is_transitive_so_pair_order_cannot_change_the_answer() -> None:
    """A-B 가 겹치고 B-C 가 겹치면 셋이 한 무리다 — 어느 쌍부터 보느냐로 답이 갈리지 않는다."""

    chain = [
        _candidate("a", span=(0, 5), surface="AAAAA"),
        _candidate("b", span=(4, 9), surface="BBBBB"),
        _candidate("c", span=(8, 13), surface="CCCCC"),
    ]
    for order in itertools.permutations(chain):
        resolved = metric_recipe_selection.resolve_overlapping_candidates(list(order))
        assert [candidate.recipe_id for candidate in resolved] == ["a"]


def test_candidates_without_a_span_claim_no_position_and_are_kept() -> None:
    """구간이 없는 후보는 자리를 주장하지 않는다 — 겹침 판정에서 남을 지우지 않는다."""

    resolved = metric_recipe_selection.resolve_overlapping_candidates([
        _candidate("positioned", span=(0, 4), surface="구매주기"),
        _candidate("unpositioned"),
    ])
    assert {candidate.recipe_id for candidate in resolved} == {"positioned", "unpositioned"}
