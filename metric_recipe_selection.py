"""같은 자리를 주장하는 recipe 후보 중 **하나를 결정론적으로** 고른다.

같은 표면어에 recipe 가 둘 걸리는 일은 결함이 아니다. `구매주기` 에는 모집단 집계 계약
(`buy_cycle`, aggregate)과 회원별 스칼라 계약(`member_scalar_buy_cycle`, member_scalar)이
함께 걸려 있고 **둘 다 컴파일된다**(조사 문서 §구현 기록의 "실측으로 뒤집힌 조사 전제").
뜻이 다른 별개의 SQL 이므로 어느 쪽도 지울 수 없다 — 지우면 표현할 수 있던 요청이 사라진다.

그래서 이 모듈은 후보를 **줄이지 않고** 고른다. 고르는 근거는 선언 데이터와 매칭 결과뿐이고,
지표 이름으로 분기하지 않는다. 같은 입력·같은 선언이면 후보를 어떤 순서로 넘겨도, 어떤 순서로
만들어졌어도 같은 답이 나온다(:data:`SELECTION_CRITERIA` 가 전순서이고 마지막 기준이
``recipe_id`` 라서 동률이 남지 않는다).

이 모듈은 카탈로그도 원문도 모른다. 후보를 만드는 쪽이 **선언에서 읽어** 채워 넣는다:

* :attr:`RecipeCandidate.priority` — 선언이 명시한 우선순위(없으면 0)
* :attr:`RecipeCandidate.span` — 원문에서 이 후보가 주장하는 구간(없으면 ``None``)
* :attr:`RecipeCandidate.constraint_count` — 그 kind 의 recipe 가 요구하는 선언 중 실제로 채워진 수

`왜 이것이 골랐는가`도 문구를 심지 않고 :data:`SELECTION_CRITERIA` 의 이름과 **실제 계산된
값**에서 만든다(:func:`describe_selection`). 기준이 늘거나 바뀌면 설명도 함께 바뀐다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    """한 입력 자리를 주장하는 recipe 하나.

    ``recipe_id`` 는 후보를 만드는 쪽의 식별자다(카탈로그 지표 id, 레지스트리 metric_id 등).
    같은 후보 집합 안에서 유일하기만 하면 되고, 최종 동률에서 이 값이 순서를 정한다.
    """

    recipe_id: str
    kind: str
    priority: int = 0
    span: Span | None = None
    surface: str | None = None
    constraint_count: int = 0
    fallback: bool = False
    declaration: Any = None

    @property
    def span_length(self) -> int:
        """매칭 구간의 길이(구간이 없으면 0)."""
        if self.span is None:
            return 0
        start, end = self.span
        return max(0, end - start)

    @property
    def explicit_token_count(self) -> int:
        """매칭된 표면어가 몇 개의 명시 토큰으로 이뤄졌는가(표면어가 없으면 0).

        '평균 구매금액'(2)은 '구매금액'(1)보다 원문을 더 많이 지목한다. 길이와 방향이 대체로
        같지만 공백 표기가 다른 동의어('총구매금액' vs '총 구매금액')에서 갈리므로 별개 기준이다.
        """
        if not self.surface:
            return 0
        return len(self.surface.split())


# 선택 기준 — **순서가 곧 우선순위**다. 각 항목은 (이름, 점수 함수)이고 점수가 **큰 쪽이 이긴다**.
# 마지막 기준만 문자열이며 **사전순 작은 쪽**이 이긴다(동률을 남기지 않기 위한 최종 기준).
SELECTION_CRITERIA: tuple[tuple[str, Callable[[RecipeCandidate], int]], ...] = (
    # 1. 선언이 직접 말한 우선순위. 데이터가 말하면 계산된 근거보다 앞선다.
    ("priority", lambda candidate: candidate.priority),
    # 2. fallback 후보는 다른 후보가 하나라도 있으면 지는 자리다.
    ("not_fallback", lambda candidate: 0 if candidate.fallback else 1),
    # 3. 원문을 더 넓게 지목한 후보. 짧은 표면어가 긴 표면어 안에 든 관계를 이 기준이 가른다.
    ("span_length", lambda candidate: candidate.span_length),
    # 4. 명시 토큰 수. 같은 길이라면 더 많은 낱말을 지목한 쪽이 더 구체적이다.
    ("explicit_tokens", lambda candidate: candidate.explicit_token_count),
    # 5. recipe 제약 선언 수. 매칭이 가르지 못하면 **채워야 할 자리를 더 많이 선언한** recipe 를
    #    고른다 — 자리표시자를 채울 근거가 선언에 있다는 뜻이다.
    ("constraints", lambda candidate: candidate.constraint_count),
)

# 기준 이름 → 설명에 쓸 한국어 표현. 문구는 기준마다 하나뿐이고 입력별로 갈라지지 않는다.
CRITERION_LABELS: dict[str, str] = {
    "priority": "선언된 priority",
    "not_fallback": "fallback 여부",
    "span_length": "매칭 구간 길이",
    "explicit_tokens": "명시 토큰 수",
    "constraints": "recipe 제약 선언 수",
    "recipe_id": "recipe id 사전순",
}

FINAL_CRITERION = "recipe_id"

_CRITERION_SCORES: dict[str, Callable[[RecipeCandidate], int]] = dict(SELECTION_CRITERIA)


@dataclass(frozen=True, slots=True)
class RecipeSelection:
    """고른 후보 + 무엇이 갈랐는가."""

    selected: RecipeCandidate
    criterion: str
    ranked: tuple[RecipeCandidate, ...] = field(default_factory=tuple)

    @property
    def runner_up(self) -> RecipeCandidate | None:
        return self.ranked[1] if len(self.ranked) > 1 else None


def selection_key(candidate: RecipeCandidate) -> tuple[Any, ...]:
    """전순서 정렬 키. 앞선 기준일수록 앞자리이고, 마지막은 항상 ``recipe_id`` 다.

    점수는 큰 쪽이 이기므로 부호를 뒤집어 오름차순 정렬에 얹는다. 입력 순서를 읽지 않으므로
    후보를 어떤 순서로 넘겨도 같은 결과가 나온다.
    """
    return (*(-score(candidate) for _name, score in SELECTION_CRITERIA), candidate.recipe_id)


def _deciding_criterion(winner: RecipeCandidate, other: RecipeCandidate) -> str:
    for name, score in SELECTION_CRITERIA:
        if score(winner) != score(other):
            return name
    return FINAL_CRITERION


def select_recipe(candidates: Iterable[RecipeCandidate]) -> RecipeSelection | None:
    """후보 중 하나를 고른다. 후보가 없으면 ``None``.

    후보를 지우지 않는다 — 진 후보도 :attr:`RecipeSelection.ranked` 에 순서대로 남는다.
    """
    ranked = tuple(sorted(candidates, key=selection_key))
    if not ranked:
        return None
    winner = ranked[0]
    return RecipeSelection(
        selected=winner,
        criterion=(
            _deciding_criterion(winner, ranked[1]) if len(ranked) > 1 else FINAL_CRITERION
        ),
        ranked=ranked,
    )


def describe_selection(selection: RecipeSelection) -> str:
    """왜 이것이 골라졌는가 — 기준 이름과 **실제 값**에서 만든다.

    후보가 하나뿐이면 비교 대상이 없으므로 그 사실을 말한다. 문구를 입력별로 심지 않는 것이
    요점이다: 기준이 늘면 이 설명도 저절로 늘어난다.
    """
    runner_up = selection.runner_up
    label = CRITERION_LABELS.get(selection.criterion, selection.criterion)
    if runner_up is None:
        return f"{selection.selected.recipe_id}(단독 후보)"
    if selection.criterion == FINAL_CRITERION:
        return (
            f"{selection.selected.recipe_id}({label}: "
            f"{selection.selected.recipe_id} < {runner_up.recipe_id})"
        )
    score = _CRITERION_SCORES[selection.criterion]
    return (
        f"{selection.selected.recipe_id}({label} "
        f"{score(selection.selected)} > {score(runner_up)})"
    )


def resolve_overlapping_candidates(
    candidates: Sequence[RecipeCandidate],
) -> tuple[RecipeCandidate, ...]:
    """**겹치는 구간을 주장하는** 후보끼리만 겨루게 하고 각 무리에서 하나씩 남긴다.

    구간이 겹치지 않는 후보는 서로 경쟁 관계가 아니다 — 한 문장이 조건 둘을 말한 것이므로
    여기서 하나로 줄이면 나머지 조건이 조용히 사라진다. 구간이 없는 후보(``span is None``)도
    자리를 주장하지 않으므로 그대로 남긴다.

    겹침은 전이적으로 묶는다: A-B 가 겹치고 B-C 가 겹치면 셋이 한 무리다. 그러지 않으면 어느
    쌍부터 보느냐에 따라 답이 달라진다.
    """
    positioned = [
        (candidate.span, candidate)
        for candidate in candidates
        if candidate.span is not None
    ]
    unpositioned = [candidate for candidate in candidates if candidate.span is None]
    ordered = sorted(positioned, key=lambda item: (item[0], item[1].recipe_id))

    groups: list[list[RecipeCandidate]] = []
    group_end = -1
    for (start, end), candidate in ordered:
        if groups and start < group_end:
            groups[-1].append(candidate)
            group_end = max(group_end, end)
            continue
        groups.append([candidate])
        group_end = end

    resolved: list[RecipeCandidate] = []
    for group in groups:
        if len(group) == 1:
            resolved.extend(group)
            continue
        selection = select_recipe(group)
        if selection is not None:
            resolved.append(selection.selected)
    return tuple(sorted(resolved + unpositioned, key=selection_key))


__all__ = [
    "CRITERION_LABELS",
    "FINAL_CRITERION",
    "SELECTION_CRITERIA",
    "RecipeCandidate",
    "RecipeSelection",
    "Span",
    "describe_selection",
    "resolve_overlapping_candidates",
    "select_recipe",
    "selection_key",
]
