"""결정론 구제가 fail-close 게이트보다 **앞에서** 도는지 고정한다.

배경(2026-08-02 실측): `behavior_demotion.normalize_lapsed_purchase_pattern` 은 정확히
"최근 N개월 주문은 있었지만 최근 M일 구매가 없는" 문형을 위해 존재했고, 정규식도 그 문장에
매치됐고, 만들어 내는 합성도 정확했다. 그런데 호출 위치가 `_semantic_ir_blocking_sql_result`
게이트보다 **40줄 뒤**였다. 게이트가 먼저 return 하므로 구제는 한 번도 실행되지 않았다.

이런 결함은 동작이 아니라 **순서**에 있어서, 어떤 단위 테스트도 잡지 못한다. 그래서 순서 자체를
불변식으로 고정한다:

  1. 원문만 읽어 슬롯을 채우는 결정론 정규화기는 게이트보다 먼저 호출된다.
  2. 그 정규화기는 자기가 읽은 원문 구간을 남긴다(소유 판정의 입력 — 남기지 않으면 같은 어구를
     다시 방출한 노드가 '유실된 의미'로 보고돼 요청 전체가 막힌다).
  3. 컴파일 결과에 의존하는 강등기는 반대로 파이프라인 **뒤**에 남는다(입력이 아직 없으므로).
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import behavior_demotion  # noqa: E402
import graph_rag  # noqa: E402
import slot_ownership  # noqa: E402

_LAPSED_PROMPT = "최근 3개월 주문은 있었지만 최근 30일간 구매가 없는 회원을 추출해서 이탈방지 캠페인을 만들어줘."


def _build_sql_result_source() -> str:
    return inspect.getsource(graph_rag.build_sql_result)


def _line_of(pattern: str, source: str) -> int:
    match = re.search(pattern, source)
    assert match is not None, f"build_sql_result 에서 {pattern!r} 를 찾지 못했다"
    return source[: match.start()].count("\n")


def test_source_only_rescue_runs_before_the_fail_close_gate() -> None:
    """원문만 읽는 결정론 구제는 semantic_ir 게이트보다 먼저 돌아야 한다."""
    source = _build_sql_result_source()
    rescue = _line_of(r"normalize_lapsed_purchase_pattern\(", source)
    gate = _line_of(r"_semantic_ir_blocking_sql_result\(", source)
    assert rescue < gate, (
        "결정론 구제가 fail-close 게이트 뒤에 있다 — 게이트가 먼저 return 하므로 구제는 "
        f"영영 실행되지 않는다(구제 {rescue}행, 게이트 {gate}행)."
    )


def test_rescue_runs_before_the_semantic_pipeline_so_its_claim_is_an_input() -> None:
    """구제의 슬롯 청구는 파이프라인 소유 판정의 **입력**이어야 한다(뒤에 두면 판정이 못 본다)."""
    source = _build_sql_result_source()
    rescue = _line_of(r"normalize_lapsed_purchase_pattern\(", source)
    pipeline = _line_of(r"_apply_semantic_plan_pipeline\(", source)
    assert rescue < pipeline, (
        "구제가 의미 파이프라인 뒤에 있다 — 그러면 구제가 채운 슬롯이 소유 판정에 보이지 않아, "
        "같은 어구를 다시 방출한 노드가 '유실된 의미'로 보고돼 요청 전체가 막힌다."
    )


def test_compiler_dependent_demotions_stay_after_the_pipeline() -> None:
    """집계 강등은 컴파일러가 만든 aggregate_conditions 를 읽는다 — 앞으로 옮기면 무증상 no-op 이 된다."""
    source = _build_sql_result_source()
    pipeline = _line_of(r"_apply_semantic_plan_pipeline\(", source)
    demotion = _line_of(r"demote_aggregate_covered_behaviors\(", source)
    assert demotion > pipeline, (
        "집계 강등이 파이프라인보다 앞에 있다 — 읽을 aggregate_conditions 가 아직 없어 "
        "조용히 아무 일도 하지 않는다."
    )


def test_rescue_records_the_source_span_it_read() -> None:
    """구제는 자기가 읽은 구간을 남긴다 — 소유 판정이 '누가 이 어구를 읽었나'를 물을 곳."""
    plan: dict = {"target_user": {"behaviors": ["no_purchase", "cart_abandoner"]}}
    changed = behavior_demotion.normalize_lapsed_purchase_pattern(plan, source_text=_LAPSED_PROMPT)
    assert changed, "이탈 문형이 매치되지 않았다"
    assert plan["target_user"]["purchase_membership"] == {"operator": "exists", "window_days": 90}
    assert plan["target_user"]["purchase_inactivity"]["min_days"] == 30
    assert plan["target_user"]["behaviors"] == ["cart_abandoner"], "평생 무구매는 창 조건과 모순이다"

    for slot in ("purchase_membership", "purchase_inactivity"):
        span = slot_ownership.slot_span(plan, slot)
        assert span is not None, f"{slot} 의 출처 구간이 기록되지 않았다"
        assert span["source"] == _LAPSED_PROMPT
        assert "주문" in span["text"] and "구매가 없" in span["text"], span["text"]


def test_rescue_does_not_overwrite_values_the_structurer_already_produced() -> None:
    """fill-if-empty — 구조화기가 확정한 창을 정규식 근사치로 덮지 않는다."""
    plan: dict = {
        "target_user": {
            "purchase_inactivity": {"value": 45, "unit": "days", "min_days": 45},
        }
    }
    behavior_demotion.normalize_lapsed_purchase_pattern(plan, source_text=_LAPSED_PROMPT)
    assert plan["target_user"]["purchase_inactivity"]["min_days"] == 45
    assert plan["target_user"]["purchase_membership"] == {"operator": "exists", "window_days": 90}
