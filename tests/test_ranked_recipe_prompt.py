"""랭킹 형상 규칙의 **단일 렌더러** — 최초 요청과 재시도가 같은 계약을 본다.

예전에는 이 규칙이 재시도 프롬프트에만 있었다. 그런데 모델이 1차에서 미지원을 선언하면
재시도 자체가 걸리지 않으므로, 그 안내는 **필요한 경우에 정확히 도달하지 않았다**.

규칙의 값(개수·임계·엔터티 필드·측정)은 의무에서 읽는다. 그래서 이 파일은 문구가 아니라
**값이 의무에서 왔는가**를 잰다 — 특정 질의의 숫자를 프롬프트에 적어 두면 그 질의만 맞는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from query_structurer.prompt import (  # noqa: E402
    build_campaign_query_plan_v4_retry_prompt,
    build_campaign_query_plan_v4_user_prompt,
)
from query_structurer.types import QueryStructuringInput, StructuringContext  # noqa: E402

RECIPE_HEADER = "[Ranked Entity Set Recipe]"
CURRENT_DATE = "2026-08-07"
RANKED_QUERY = "작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
OTHER_RANKED_QUERY = "매출 하위 3개 상품을 구매한 회원"


def _first_prompt(query: str) -> str:
    return build_campaign_query_plan_v4_user_prompt(
        QueryStructuringInput(
            query=query, context=StructuringContext(current_date=CURRENT_DATE)
        )
    )


def _recipe(prompt: str) -> str:
    assert RECIPE_HEADER in prompt, "랭킹 레시피가 프롬프트에 없다"
    return prompt[prompt.index(RECIPE_HEADER):]


def test_the_first_attempt_prompt_carries_the_ranked_recipe() -> None:
    assert RECIPE_HEADER in _first_prompt(RANKED_QUERY)


def test_the_retry_prompt_uses_the_same_renderer() -> None:
    first = _recipe(_first_prompt(RANKED_QUERY))
    retry = _recipe(
        build_campaign_query_plan_v4_retry_prompt("{}", "some error", RANKED_QUERY)
    )

    # 같은 렌더러이므로 계약 문장이 글자 그대로 같다(복사본이 아니라 하나의 소유자).
    assert first.split("Required contract per obligation:")[0] == (
        retry.split("Required contract per obligation:")[0]
    )


def test_the_recipe_states_the_cardinality_shape_and_forbids_exists() -> None:
    recipe = _recipe(_first_prompt(RANKED_QUERY))

    assert "distinct=true" in recipe
    assert "'at least one'" in recipe
    assert "count" in recipe


def test_recipe_values_come_from_the_obligation_not_from_constants() -> None:
    """다른 랭킹 요청은 다른 값을 낸다 — 숫자·필드가 상수였다면 같은 값이 나온다."""
    ranked = _recipe(_first_prompt(RANKED_QUERY))
    other = _recipe(_first_prompt(OTHER_RANKED_QUERY))

    assert '"limit": 5' in ranked and '"cardinality"' in ranked
    assert '"direction": "top"' in ranked
    assert '"limit": 3' in other and '"cardinality"' not in other
    assert '"direction": "bottom"' in other
    # 측정 지표도 요청마다 다르다(판매수량 vs 매출).
    assert '"measure_field": "purchase_line.quantity"' in ranked
    assert '"measure_field": "purchase_line.amount"' in other


def test_no_ranked_obligation_means_no_recipe_section() -> None:
    """규칙의 대상이 없으면 프롬프트를 늘리지 않는다."""
    assert RECIPE_HEADER not in _first_prompt("여성 회원을 찾아줘")
    assert RECIPE_HEADER not in build_campaign_query_plan_v4_retry_prompt("{}", "error")
