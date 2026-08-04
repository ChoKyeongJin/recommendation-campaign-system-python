"""좌표가 **응답·debug·실패로그까지** 간다 — 파생만 하고 배선을 안 하면 fixture 위의 그린이다.

Phase 2-2 는 좌표를 만들었고 이 파일은 그것이 실제로 나가는지를 잰다. 셋 다 서로 다른 소비자다.

| 표면 | 무엇이 실리나 | 왜 거기인가 |
|---|---|---|
| `api_response.audience_diagnosis` | 요약 좌표 1개 | `include_debug` 없이도 보여야 화면·BFF 가 읽는다 |
| `debug.unresolved_source_conditions` | 원본 항목 전량 | 내부 경로·코드가 들어 있어 debug 밖으로 못 낸다 |
| 실패로그 `context_metadata.audience_diagnosis` | 요약 좌표 1개 | **DDL 없이** JSONB 로 집계된다(새 컬럼도 새 로그파일도 안 만든다) |

새 로그 파일을 만들지 않는 것이 판정이다 — 요청별 흔적은 `logs/rag_llm/<날짜>/` 가 이미 갖고
있고, 소비자 없는 세 번째 로그가 생기면 운영자가 볼 곳이 다시 흩어진다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import api
import audience_failure
import graph_rag

REPO_ROOT = Path(__file__).resolve().parents[1]


def _failing_sql_result() -> dict:
    return {
        "sql": None,
        "is_success": False,
        "failure_reason": "audience_authority_invalid",
        "interpretation_status": "needs_clarification",
        "missing_input_conditions": [],
        "clarification_questions": [],
    }


def _response(query_plan: dict, sql_result: dict) -> dict:
    return graph_rag.build_recommendation_api_response(
        "지난달 구매한 회원", query_plan, sql_result, {"content": None}
    )


def test_failed_response_carries_the_coordinate_without_debug() -> None:
    """`include_debug` 없이도 실린다 — 디버그 플래그 뒤에 숨으면 운영에서 안 보인다."""
    plan = {"intent": "find_user_segment", "audience_authority": "event-ir"}
    diagnosis = _response(plan, _failing_sql_result())["audience_diagnosis"]

    assert diagnosis is not None
    assert diagnosis["stage"] == "audience_authority"
    assert diagnosis["code"] == "audience_authority_invalid"


def test_successful_response_carries_no_coordinate() -> None:
    """성공에 좌표가 붙으면 소비자가 '실패했나?'를 다시 판정하게 된다."""
    response = _response(
        {"intent": "find_user_segment"},
        {"sql": "SELECT 1", "is_success": True, "confidence": None},
    )
    assert response["audience_diagnosis"] is None


def test_diagnosis_axis_is_not_the_stepper_axis() -> None:
    """두 축이 한 응답에 함께 나가고, 서로 다른 어휘를 쓴다(합치면 한쪽이 거짓말한다)."""
    plan = {"intent": "find_user_segment", "audience_authority": "event-ir"}
    response = _response(plan, _failing_sql_result())

    stage_codes = {row["stage"] for row in audience_failure.LANE_STAGES}
    stepper = response["failure_stage"]
    assert stepper is not None, "이 사유는 스텝퍼 단계를 갖는다(2-1 이 매핑했다)."
    # 소유자 축(레인)과 사용자 축(스텝퍼)이 같은 요청에서 서로 다른 값을 낸다.
    assert response["audience_diagnosis"]["stage"] == "audience_authority"
    assert stepper["code"] == "condition_recognition"
    assert stepper["code"] not in stage_codes, (
        "스텝퍼 단계 코드가 레인 어휘와 겹친다 — 두 축을 구별할 수 없게 된다."
    )


def test_failure_log_metadata_carries_the_coordinate_for_jsonb_queries() -> None:
    """DDL 을 늘리지 않고 조회되려면 context_metadata(JSONB) 안에 들어가야 한다."""
    metadata = api._capability_failure_context_metadata(
        {"context_assembly": {"metadata": {"chunks": 3}}},
        {"audience_diagnosis": {"stage": "event_ir_compile", "code": "event_ir_compile_failed"}},
    )
    assert metadata["chunks"] == 3, "기존 메타데이터를 덮지 않는다."
    assert metadata["audience_diagnosis"]["stage"] == "event_ir_compile"


def test_failure_log_metadata_omits_the_key_when_there_is_no_coordinate() -> None:
    """성공 응답의 메타데이터에 None 좌표를 심으면 집계가 그것을 한 갈래로 센다."""
    metadata = api._capability_failure_context_metadata(
        {"context_assembly": {"metadata": {}}}, {"audience_diagnosis": None}
    )
    assert "audience_diagnosis" not in metadata


def _debug_block_keys() -> set[str]:
    """api.py 의 debug 블록이 싣는 키를 AST 로 읽는다(문자열 검색은 다른 자리도 센다)."""
    tree = ast.parse((REPO_ROOT / "api.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "debug"
        ):
            return {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("api.py 에서 debug 블록을 찾지 못했다 — 이 계약이 공허해진다.")


def test_debug_block_exposes_the_raw_coordinates() -> None:
    """요약 좌표는 항목 하나만 든다 — 여럿일 때 전량은 debug 에서만 보인다."""
    assert "unresolved_source_conditions" in _debug_block_keys()


def test_this_document_lists_exactly_the_declared_lanes() -> None:
    """운영 문서의 레인 표가 코드와 갈리면, 운영자는 없는 레인의 대응을 찾는다.

    문서는 "다음 행동"을 소유하고 코드는 "왜 이 레인이 따로 있는가"를 소유한다 — 표를 두 벌로
    적으면 갈라지므로, 이름의 일치만 기계로 잰다.
    """
    doc = (REPO_ROOT / "docs" / "operations" / "failure_diagnosis.md").read_text(encoding="utf-8")
    declared = {row["stage"] for row in audience_failure.LANE_STAGES}
    documented = {lane for lane in declared if f"`{lane}`" in doc}
    assert documented == declared, (
        f"문서에 없는 레인: {sorted(declared - documented)}"
    )


def test_no_new_failure_log_file_was_introduced() -> None:
    """세 번째 로그 파일을 만들지 않는 것이 판정이다(운영자가 볼 곳이 흩어진다)."""
    source = (REPO_ROOT / "api.py").read_text(encoding="utf-8") + (
        REPO_ROOT / "audience_failure.py"
    ).read_text(encoding="utf-8")
    for phantom in ("logs/unresolved.jsonl", "logs/diagnosis.jsonl", "UNRESOLVED_LOG"):
        assert phantom not in source, f"실재하지 않는 로그 장치가 다시 들어왔다: {phantom}"
