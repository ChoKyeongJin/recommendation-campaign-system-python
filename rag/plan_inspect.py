"""실행 IR 검사 술어 — "제안된 것이 실제로 붙었는가"를 plan dict 만 보고 답한다.

graph_rag.py 에서 분리했다. LLM 이나 외부 조건 해석기가 필터를 제안해도, 그것이 실행
plan 에 실제로 반영됐는지는 별개 사실이다. 반영 여부를 확인하지 않으면 "제안했으니 됐다"로
넘어가 조건이 조용히 빠진 SQL 이 나간다 — 이 프로젝트가 가장 경계하는 실패다.

두 계층이 이 술어를 공유한다: 검증 계층(외부조건 차단·의미검증 계약·미해결 조건 판정)과
트레이스 계층(어떤 참조 자산이 실제로 기여했는지 표시). 어느 한쪽에 두면 다른 쪽이
되돌아 import 하게 되므로 아래 리프에 둔다.

판정이 부분 일치가 아니라 **완전 일치**인 것이 계약이다 — id 만 같은 껍데기가 붙은 것을
"반영됨"으로 세면 검증이 무력해진다(docstring 의 'not an ID lookalike').

순수 모듈 불변식: graph_rag 를 import 하지 않는다. plain Mapping 입력, bool 출력.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _generated_filter_is_attached(
    query_plan: Mapping[str, Any], generated: Any
) -> bool:
    """Require the complete generated filter, not an ID lookalike, in execution IR."""

    if not isinstance(generated, Mapping):
        return False
    if generated.get("logic") == "OR":
        return any(
            isinstance(item, Mapping) and dict(item) == dict(generated)
            for item in (query_plan.get("compound_dimension_filters") or [])
        )
    if "dimension_id" in generated:
        return any(
            isinstance(item, Mapping) and dict(item) == dict(generated)
            for item in (query_plan.get("dimension_filters") or [])
        )
    target_values = generated.get("target_user")
    if isinstance(target_values, Mapping):
        target = query_plan.get("target_user")
        return isinstance(target, Mapping) and all(
            target.get(key) == value for key, value in target_values.items()
        )
    numeric_values = generated.get("target_user.balance_conditions")
    if isinstance(numeric_values, list):
        attached = (
            (query_plan.get("target_user") or {}).get("balance_conditions")
            if isinstance(query_plan.get("target_user"), Mapping)
            else None
        )
        return isinstance(attached, list) and all(
            any(
                isinstance(item, Mapping)
                and isinstance(candidate, Mapping)
                and dict(item) == dict(candidate)
                for candidate in attached
            )
            for item in numeric_values
        )
    return False
