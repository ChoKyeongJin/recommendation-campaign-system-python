"""Event IR 컴파일 실패의 **좌표** 기록 — 한 곳에서 같은 모양으로 남긴다.

빌더 안에서 실패하든 진입 가드에서 걸리든 항목 모양이 같아야 한다. 모양이 두 벌이면 진단
좌표가 경로마다 달라지고, 그때 "어디서 막혔는가"는 다시 추측이 된다.

같은 사실을 :mod:`compile_outcome` 원장에도 남긴다 — 후보가 0개일 때 응답이
``no_sql_candidates`` 대신 ``<단계>:<코드>`` 로 답할 수 있는 유일한 경로다(§6).

이 모듈은 graph_rag 를 import 하지 않는다(plain dict in/out).

실행: python -m pytest tests/test_event_ir_unresolved_coordinate.py -q
"""

from __future__ import annotations

from typing import Any

import compile_outcome

EVENT_EXPRESSION_KEY = "event_expression"
UNRESOLVED_KEY = "unresolved_source_conditions"


def record_unresolved(
    query_plan: dict[str, Any],
    *,
    stage: str,
    code: str,
    reason: str,
    unsupported: bool = False,
    requirement_ids: tuple[str, ...] = (),
) -> None:
    """실패 좌표를 플랜에 남긴다(중복 append 는 하지 않는다)."""

    outcome_stage = (
        stage if stage in compile_outcome.STAGES else compile_outcome.STAGE_AUDIENCE_LOWERING
    )
    factory = (
        compile_outcome.ExplicitUnsupported if unsupported else compile_outcome.CompileFailure
    )
    compile_outcome.record(
        query_plan,
        factory(
            stage=outcome_stage,
            code=code,
            requirement_ids=requirement_ids,
            details={"declared_stage": stage},
        ),
    )
    unresolved = query_plan.setdefault(UNRESOLVED_KEY, [])
    item = {
        "path": f"plan.{EVENT_EXPRESSION_KEY}",
        "condition": EVENT_EXPRESSION_KEY,
        "reason": reason,
        # 단계는 파이프라인의 어디인지를, 코드는 무엇이 실패했는지를 말한다. 코드가 없으면
        # plan_validation._marker 가 위 한국어 **문장**을 정규화해 issue code 로 승격한다
        # (닫힌 집합 밖의 코드가 사용자 문구까지 흘러간다).
        "stage": stage,
        "code": code,
        "source": "event_ir",
        "status": "unresolved",
    }
    if item not in unresolved:
        unresolved.append(item)


def record_unsupported(
    query_plan: dict[str, Any], *, stage: str, code: str, message: str, clarification: str
) -> None:
    """'이 뜻을 실행할 자산이 없다' 를 플랜과 귀결 원장에 함께 남긴다."""

    query_plan["unsupported"] = {
        "reason": code,
        "message": message,
        "clarification": clarification,
    }
    compile_outcome.record(
        query_plan,
        compile_outcome.ExplicitUnsupported(stage=stage, code=code),
    )


__all__ = ["EVENT_EXPRESSION_KEY", "UNRESOLVED_KEY", "record_unresolved", "record_unsupported"]
