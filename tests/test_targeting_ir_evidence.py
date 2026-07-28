"""타겟팅 IR 경로의 GraphRAG 근거 주입 계약.

근거는 **제안자**다 — 원문 표현을 닫힌 어휘로 옮기고 값 표기를 맞추는 데만 쓴다. 검색이 결정자가
되면(문장에 없는 개수·기간을 채우거나 방향을 뒤집으면) 그건 환각이므로, 프롬프트가 그 경계를
명시하는지와 물리·SQL 노드가 근거에서 배제되는지를 고정한다.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import graph_rag as g


def _node(node_id: str, node_type: str, text: str, *, nested: bool = True) -> dict:
    """expand_context 가 돌려주는 payload 중첩 모양(기본)과 평면 모양을 모두 만든다."""
    node = {"id": node_id, "type": node_type, "title": node_id, "score": 0.9}
    if nested:
        node["payload"] = {"text_for_embedding": text}
    else:
        node["text"] = text
    return node


def test_evidence_keeps_lexical_nodes_and_drops_physical_ones() -> None:
    nodes = [
        _node("dv:brand:alo", "dimension_value", "브랜드 알로&루"),
        _node("tbl:CRM_SL_ORDERDETAILMALL", "schema_table", "주문상세 테이블"),
        _node("sql:top_products", "sql_example", "SELECT TOP 10 ..."),
        _node("term:best", "business_term", "베스트 = 판매수량 상위"),
        _node("norm:alo", "normalization_rule", "알로루 -> 알로&루"),
    ]

    evidence = g._targeting_ir_evidence(nodes)

    assert [item["id"] for item in evidence] == ["dv:brand:alo", "term:best", "norm:alo"]
    assert all(item["text"] for item in evidence)


def test_evidence_reads_both_nested_and_flat_node_shapes() -> None:
    nested = g._targeting_ir_evidence([_node("dv:a", "dimension_value", "중첩 텍스트")])
    flat = g._targeting_ir_evidence([_node("dv:a", "dimension_value", "평면 텍스트", nested=False)])

    assert nested[0]["text"] == "중첩 텍스트"
    assert flat[0]["text"] == "평면 텍스트"


def test_evidence_is_capped_and_ignores_empty_nodes() -> None:
    nodes = [_node(f"dv:{index}", "dimension_value", f"값 {index}") for index in range(30)]
    nodes.append({"id": "dv:empty", "type": "dimension_value", "payload": {}})

    evidence = g._targeting_ir_evidence(nodes)

    assert len(evidence) == g._TARGETING_IR_EVIDENCE_LIMIT
    assert "dv:empty" not in {item["id"] for item in evidence}


def test_missing_context_yields_no_evidence() -> None:
    assert g._targeting_ir_evidence(None) == []
    assert g._targeting_ir_evidence([]) == []
    assert g._targeting_ir_evidence(["not-a-node"]) == []


def _stub_openai(monkeypatch: pytest.MonkeyPatch, captured: dict, payload: dict) -> None:
    """`from openai import OpenAI` 를 가로채 프롬프트를 포착하고 고정 IR 을 돌려준다."""

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class _FakeOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(g, "_write_rag_llm_log", lambda *args, **kwargs: None)


_EXPRESSION = {
    "relation": {
        "name": "purchase",
        "exists": True,
        "entitySet": {"entity": "product", "measure": "sales_quantity", "direction": "top", "limit": 5},
    }
}


def test_context_nodes_reach_the_ir_prompt_with_interpretation_only_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _stub_openai(monkeypatch, captured, {"expression": _EXPRESSION})

    candidate = g._build_llm_targeting_ir_candidate(
        "제일 잘 나가는 상품 5개 산 사람",
        {"intent": "find_user_segment"},
        "gpt-5-mini",
        context_nodes=[_node("dv:brand:alo", "dimension_value", "브랜드 알로&루")],
    )

    system_prompt = captured["messages"][0]["content"]
    assert "브랜드 알로&루" in system_prompt
    # 근거가 넘을 수 없는 선이 프롬프트에 남아 있어야 한다.
    assert "원문에 없는 개수(limit)·기간을 근거로 채우지 않는다" in system_prompt
    assert "방향(top/bottom)과 부재(exists=false)는 원문 문장이 정한다" in system_prompt
    assert candidate is not None
    assert [item["id"] for item in candidate["targeting_ir_evidence"]] == ["dv:brand:alo"]


def test_ir_prompt_without_context_stays_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """근거가 없으면 근거 절 자체를 넣지 않는다 — 빈 근거를 보여주는 프롬프트는 오해를 만든다."""
    captured: dict = {}
    _stub_openai(monkeypatch, captured, {"expression": _EXPRESSION})

    candidate = g._build_llm_targeting_ir_candidate(
        "제일 잘 나가는 상품 5개 산 사람", {"intent": "find_user_segment"}, "gpt-5-mini"
    )

    system_prompt = captured["messages"][0]["content"]
    assert "검색 근거(해석용):" not in system_prompt
    assert candidate is not None
    assert "targeting_ir_evidence" not in candidate


def test_evidence_cannot_widen_the_closed_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """근거에 보이는 표현이라도 enum 밖이면 후보가 되지 못한다(검증이 최종 관문)."""
    captured: dict = {}
    invalid = {
        "relation": {
            "name": "review",  # 레지스트리에 없는 관계
            "exists": True,
        }
    }
    _stub_openai(monkeypatch, captured, {"expression": invalid})

    candidate = g._build_llm_targeting_ir_candidate(
        "리뷰 많이 쓴 사람",
        {"intent": "find_user_segment"},
        "gpt-5-mini",
        context_nodes=[_node("term:review", "business_term", "리뷰 = 상품평")],
    )

    assert candidate is None
