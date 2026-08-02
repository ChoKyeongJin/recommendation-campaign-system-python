"""캠페인 슬롯 라우팅·스칼라 슬롯 소유·슬롯 소유 실패 소거의 회귀 고정(2026-08-02).

대상 프롬프트: "최근 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로
재반응 유도 캠페인을 만들어줘."

같은 프롬프트가 런마다 두 가지로 실패했고, 원인은 서로 다른 세 곳이었다:

  F1 캠페인 집계의 슬롯 분기가 **값의 타입**이었다. strict function calling 은 수량 객체의
     키를 전부 채우므로 '3회'가 `{"amount": 3, "currency": null, "value": 3, "unit": "회"}` 로
     오고, 금액 판별이 `amount` 키 존재를 보는 순간 횟수가 Money 가 된다 → 반응 횟수 조건이
     금액 슬롯으로 갔다. 분기 축을 **지표**로 옮긴다.
  F2 스칼라 슬롯을 두 노드가 쓰면 뒤 노드가 앞 노드를 **조용히 덮었다**. 원장은 그때도
     `compiled: 2` 로 보고했다(가짜 성공) → 덮어쓰기를 충돌 오류로.
  F3 슬롯 계층이 이미 소유한 구간의 `validation_errors` 에는 소거 경로가 없어서, 정확히
     컴파일된 조건의 중복 방출 하나가 요청 전체를 `semantic_registry_gap` 으로 막았다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import legacy_plan_compiler  # noqa: E402
import requirement_ledger  # noqa: E402
import semantic_pipeline  # noqa: E402
import semantic_plan  # noqa: E402
import semantic_plan_bridge  # noqa: E402
import graph_rag  # noqa: E402
from compile_contract import CompileResult  # noqa: E402

_PROMPT = "최근 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원을 대상으로 재반응 유도 캠페인을 만들어줘."

# LLM 이 실제로 낸 형태 — strict 스키마라 amount/value 가 **둘 다** 채워져서 온다(실측).
_COUNT_VALUE = {"amount": 3, "currency": None, "value": 3, "unit": "회"}


def _context():
    return graph_rag._semantic_compile_context()


def _campaign_node(node_id: str, metric: str, operator: str, value, *, span: str, **extra):
    node = {
        "id": node_id, "type": "aggregate_predicate", "scope": "campaign",
        "metric": metric, "operator": operator, "value": value, "source_span": span,
    }
    if span and span in _PROMPT:
        node["source_start"] = _PROMPT.index(span)
        node["source_end"] = node["source_start"] + len(span)
    node.update(extra)
    return node


def _compile(nodes: list[dict], query: str = _PROMPT):
    plan = semantic_plan.plan_from_dict({"nodes": nodes}, source_query=query)
    evaluation = semantic_pipeline.evaluate(
        query, plan, context=_context(),
        compiler=legacy_plan_compiler.LegacyQueryPlanCompiler(),
        slot_catalog=legacy_plan_compiler.NODE_SLOT_MAP,
    )
    return plan, evaluation


# ── F1. 캠페인 집계는 지표로 갈린다 ────────────────────────────────────────────────
def test_money_shaped_count_still_routes_to_the_frequency_slot() -> None:
    """'발송 성공 3회'의 값이 Money 로 읽혀도 지표가 반응 이벤트면 횟수 슬롯이다.

    이 단언이 깨지면 조건이 실패하는 게 아니라 **다른 조건이 된다**(횟수 3 → 금액 3원).
    """
    _plan, evaluation = _compile([
        _campaign_node("r1", "campaign_contact", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상", aggregation="count"),
    ])
    assert evaluation.compiled.failures == []
    slot = evaluation.compiled.target_user.get(legacy_plan_compiler.SLOT_CAMPAIGN_FREQUENCY)
    assert slot is not None, evaluation.compiled.target_user
    assert slot["event"] == "campaign_contact" and slot["count"] == 3 and slot["operator"] == ">="
    assert legacy_plan_compiler.SLOT_CAMPAIGN_BUY_AMOUNT not in evaluation.compiled.target_user


def test_amount_metric_still_routes_to_the_amount_slot() -> None:
    """반대 방향 — 금액 지표는 값 표기가 무엇이든 금액 슬롯이다(집계함수 보존 포함)."""
    _plan, evaluation = _compile(
        [_campaign_node("r1", "campaign_buy_amount", ">=", "10만 원",
                        span="", aggregation="avg")],
        query="캠페인별 구매반응 금액이 평균 10만 원 이상인 회원",
    )
    slot = evaluation.compiled.target_user.get(legacy_plan_compiler.SLOT_CAMPAIGN_BUY_AMOUNT)
    assert slot is not None and slot["amount"] == 100000 and slot["agg"] == "AVG"
    assert legacy_plan_compiler.SLOT_CAMPAIGN_FREQUENCY not in evaluation.compiled.target_user


def test_unknown_campaign_metric_fails_instead_of_becoming_an_amount() -> None:
    """어휘 밖 지표를 금액으로 짐작하면 조용히 다른 조건이 된다 — 사유를 대며 막는다."""
    _plan, evaluation = _compile([
        _campaign_node("r1", "campaign_click_rate", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상"),
    ])
    assert evaluation.compiled.target_user == {}
    assert any("실행 어휘에 없다" in str(f.get("reason")) for f in evaluation.compiled.failures)


# ── F2. 스칼라 슬롯은 조건 하나만 담는다 ───────────────────────────────────────────
def test_second_node_on_a_scalar_slot_conflicts_instead_of_overwriting() -> None:
    """덮어쓰면 노드 순서가 곧 대상이 되고, 사라진 조건의 근거가 남지 않는다.

    실측 사고 그대로: '발송 성공 3회 이상'과 '구매반응 없음' 두 노드가 한 슬롯을 요구한다.
    """
    _plan, evaluation = _compile([
        _campaign_node("r1", "campaign_contact", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상"),
        _campaign_node("r2", "buy_response", "<", {"amount": None, "value": 1, "unit": "건"},
                       span="구매반응이 없는 회원"),
    ])
    slot = evaluation.compiled.target_user[legacy_plan_compiler.SLOT_CAMPAIGN_FREQUENCY]
    # 먼저 온 조건이 그대로 남는다 — 두 번째가 덮지 않는다.
    assert slot["event"] == "campaign_contact" and slot["count"] == 3
    conflicts = [
        failure for failure in evaluation.compiled.failures
        if "조건 하나만" in str(failure.get("reason"))
    ]
    assert len(conflicts) == 1 and conflicts[0]["node_id"] == "r2"
    assert conflicts[0]["failure_code"] == semantic_plan.VALIDATION_MISMATCH
    # 가짜 성공 차단: 밀려난 조건이 원장에서 compiled 로 세어지지 않는다.
    assert not evaluation.ledger.is_complete()


def test_identical_duplicate_on_a_scalar_slot_is_not_a_conflict() -> None:
    """같은 의미의 중복 방출은 충돌이 아니다 — 값이 같으면 멱등하게 통과시킨다."""
    _plan, evaluation = _compile([
        _campaign_node("r1", "campaign_contact", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상"),
        _campaign_node("r2", "campaign_contact", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상"),
    ])
    assert evaluation.compiled.failures == []
    assert evaluation.compiled.target_user[legacy_plan_compiler.SLOT_CAMPAIGN_FREQUENCY]["count"] == 3


# ── F3. 슬롯이 소유한 구간의 검증 실패는 요청을 막지 못한다 ────────────────────────
_OWNED_QUERY = "구매반응이 없는 회원"


def _node_plan():
    return semantic_plan.plan_from_dict(
        {"nodes": [{
            "id": "n", "type": "aggregate_predicate", "scope": "campaign",
            "metric": "campaign_responses", "operator": "=", "value": "no_buy_response",
            "source_span": _OWNED_QUERY, "source_start": 0, "source_end": len(_OWNED_QUERY),
        }]},
        source_query=_OWNED_QUERY,
    )


def test_slot_owned_span_supersedes_a_validation_error() -> None:
    """슬롯이 이미 컴파일한 구간의 정규화 실패는 중복 방출이지 유실된 의미가 아니다.

    이것이 없어서, `campaign_responses` 가 정확히 채워진 요청이 같은 구절을 다시 방출한
    노드 하나 때문에 "실행 설정이 준비되지 않았습니다"로 막혔다(설정은 멀쩡했다).
    """
    # 이 구절에는 셀 수 있는 값 원자가 없다 — 그래서 '원자가 전부 청구됐다'가 공허참이 되고,
    # 소유 판정은 **구간 포함**으로만 증명된다. 이 전제가 깨지면 아래 소거는 의미가 없다.
    import semantic_coverage
    assert semantic_coverage.source_anchors(_OWNED_QUERY) == []

    plan = _node_plan()
    plan.validation_errors.append({
        "node_id": "n", "field": "value",
        "failure_code": semantic_plan.VALIDATION_MISMATCH,
        "reason": "수량을 읽지 못했다: 'no_buy_response'", "received": "no_buy_response",
    })
    compiled = CompileResult(failures=[{
        "node_id": "n", "failure_code": semantic_plan.VALIDATION_MISMATCH,
        "reason": "값 정규화 실패(invalid_number)",
    }])
    assert plan.status() == semantic_plan.STATUS_INVALID

    superseded = semantic_plan_bridge.supersede_slot_owned_failures(
        plan, query=_OWNED_QUERY, claimed=[(0, len(_OWNED_QUERY))], compiled=compiled,
    )

    assert [item["action"] for item in superseded] == ["validation_error"]
    assert superseded[0]["superseded_by"] == "slot_claim"
    assert plan.validation_errors == []
    # 원본(컴파일 실패 목록)까지 함께 걷어야 원장이 같은 실패를 다시 말하지 않는다.
    assert compiled.failures == []
    assert plan.status() != semantic_plan.STATUS_INVALID


def test_unclaimed_validation_error_still_blocks() -> None:
    """반대 방향 — 슬롯이 안 덮은 구간의 검증 실패는 그대로 막는다(소거가 우회로가 되면 안 된다)."""
    plan = _node_plan()
    error = {
        "node_id": "n", "field": "value",
        "failure_code": semantic_plan.VALIDATION_MISMATCH,
        "reason": "수량을 읽지 못했다: 'no_buy_response'",
    }
    plan.validation_errors.append(error)
    assert semantic_plan_bridge.supersede_slot_owned_failures(
        plan, query=_OWNED_QUERY, claimed=[]
    ) == []
    assert plan.validation_errors == [error]
    assert plan.status() == semantic_plan.STATUS_INVALID


def test_node_scoped_validation_errors_only() -> None:
    """노드를 가리키지 않는 내부 사고(추출 실패·레지스트리 파손)는 어떤 청구로도 걷히지 않는다."""
    plan = _node_plan()
    error = {"failure_code": semantic_plan.INTERNAL_FAULT, "reason": "capability 선언을 읽지 못했습니다"}
    plan.validation_errors.append(error)
    assert semantic_plan_bridge.supersede_slot_owned_failures(
        plan, query=_OWNED_QUERY, claimed=[(0, len(_OWNED_QUERY))]
    ) == []
    assert plan.validation_errors == [error]


def test_a_scalar_slot_conflict_is_never_superseded() -> None:
    """충돌은 '중복 방출'이 아니다 — 슬롯을 다른 조건이 갖고 있어서 이 조건이 표현되지 못했다.

    소거하면 조용히 좁아진 오디언스가 그대로 나간다. 두 수리(F2·F3)가 만나는 지점이라
    여기서 명시적으로 고정한다.
    """
    plan = _node_plan()
    conflict = {
        "node_id": "n", "failure_code": semantic_plan.VALIDATION_MISMATCH,
        "reason": "'target_user.campaign_response_frequency' 슬롯을 두 조건이 동시에 요구한다",
        "supersedable": False,
    }
    plan.validation_errors.append(conflict)
    compiled = CompileResult(failures=[dict(conflict)])
    assert semantic_plan_bridge.supersede_slot_owned_failures(
        plan, query=_OWNED_QUERY, claimed=[(0, len(_OWNED_QUERY))], compiled=compiled,
    ) == []
    assert plan.validation_errors == [conflict] and len(compiled.failures) == 1
    assert plan.status() == semantic_plan.STATUS_INVALID


def test_slot_conflict_failures_declare_themselves_unsupersedable() -> None:
    """생산자와 소비자의 결속 — 컴파일러가 그 표시를 실제로 붙이는가(가드가 공허해지지 않게)."""
    _plan, evaluation = _compile([
        _campaign_node("r1", "campaign_contact", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상"),
        _campaign_node("r2", "buy_response", "<", {"amount": None, "value": 1, "unit": "건"},
                       span="구매반응이 없는 회원"),
    ])
    conflicts = [f for f in evaluation.compiled.failures if "조건 하나만" in str(f.get("reason"))]
    assert conflicts and all(f.get("supersedable") is False for f in conflicts)


def test_target_prompt_reaches_the_contact_success_sql() -> None:
    """종단 — 발송 성공 절만 노드로 오고 구매반응 절은 슬롯이 소유한 방출에서 SQL 이 나온다.

    변경 전 같은 입력의 실측: `campaign_buy_amount = {amount: 3}`(3원!)이 채워지고 반응 횟수
    슬롯은 비어 `query_plan_required_conditions_missing` 으로 막혔다. 단계별 단언이 다 맞아도
    배선이 어긋나면 사용자에겐 여전히 실패이므로 여기서 SQL 까지 본다.
    """
    import networkx as nx

    plan = graph_rag.build_query_plan(_PROMPT, parser="rules")
    plan["intent"] = "find_user_segment"
    owned = "구매반응이 없는 회원"
    plan["target_user"]["campaign_responses"] = [
        {"canonical": "no_buy_response", "negated": True, "predicate": "R.BUY_RSPN_YN = 'Y'"}
    ]
    plan["semantic_evidence"] = [{
        "path": "target_user.campaign_responses", "text": owned,
        "start": _PROMPT.index(owned), "end": _PROMPT.index(owned) + len(owned), "confidence": 1,
    }]
    plan["semantic_plan"] = {"nodes": [
        _campaign_node("req-1", "campaign_contact", ">=", _COUNT_VALUE,
                       span="최근 캠페인 발송 성공 횟수가 3회 이상", aggregation="count"),
    ]}
    result = graph_rag.build_sql_result(
        nx.Graph(), _PROMPT, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=_PROMPT,
    )
    assert result["is_success"] is True, result.get("failure_reason")
    assert plan["target_user"]["campaign_response_frequency"]["event"] == "campaign_contact"
    assert plan["target_user"].get("campaign_buy_amount") is None
    sql = result["sql"]
    # 발송 성공 횟수는 접촉 팩트의 회원별 HAVING, 구매반응 부재는 반응 팩트 anti-join.
    assert "M.CONTAC_SUCC_YN = 'Y'" in sql and ">= 3" in sql
    assert "NOT EXISTS" in sql and "R.BUY_RSPN_YN = 'Y'" in sql


def test_superseded_requirements_leave_the_ledger() -> None:
    """소거된 중복 방출은 원장에서도 빠진다 — 요청은 통과하는데 원장만 실패를 말하면 모순이다."""
    ledger = requirement_ledger.RequirementLedger(requirements=[
        requirement_ledger.Requirement(requirement_id="n", label="중복 방출"),
        requirement_ledger.Requirement(
            requirement_id="m", label="진짜 조건",
            validation={"outcome": requirement_ledger.COMPILED},
        ),
    ])
    semantic_plan_bridge._drop_superseded_requirements(
        ledger, [{"node_id": "n", "superseded_by": "slot_claim"}]
    )
    assert [item.requirement_id for item in ledger.requirements] == ["m"]
    assert ledger.is_complete()
