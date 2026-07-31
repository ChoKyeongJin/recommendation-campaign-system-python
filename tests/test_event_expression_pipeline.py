"""사건 IR 파이프라인 통합 — 자연어에서 실행 SQL 까지 의미가 보존되는지 끝단에서 고정한다.

단위 계약은 :mod:`tests.test_event_ir` 가 본다. 여기서는 그 IR 이 실제 실행 경로(build_query_plan →
build_sql_result)에서 **소유권을 제대로 가져가고, 가져가지 않아야 할 때는 물러나는지**를 본다.

이 파일이 잡는 회귀는 구체적이다. 이전 파서는 '하반기 구매 기록이 없는 고객'에 대해 하반기에 구매한
회원을 뽑는 SQL 을 냈다 — 부정이 통째로 사라지고 기간만 살아남았기 때문이다. 조용한 의미 반전이라
SQL 을 읽어야만 알 수 있었다.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("CONDITION_SLOT_LLM_FALLBACK", "off")
os.environ.setdefault("SURFACE_LEXICON_LLM", "off")
os.environ.setdefault("TARGET_OBJECT_LLM_FALLBACK", "false")

import event_ir
import graph_rag

YEAR = date.today().year


@pytest.fixture(autouse=True)
def _offline_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 보완 계층을 끈다 — 결정론 경로의 의미 보존만 고정한다(기존 테스트와 같은 관례)."""
    monkeypatch.setattr(graph_rag, "_apply_llm_condition_slot_fallback", lambda *_a, **_kw: None)
    monkeypatch.setenv("TARGET_OBJECT_LLM_FALLBACK", "false")
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "off")
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "off")


def plan_for(query: str) -> dict:
    return graph_rag.build_query_plan(query, parser="rules")


def sql_for(query: str) -> str | None:
    plan = plan_for(query)
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 1000, original_query=query
    )
    return result.get("sql")


REFERENCE = "올해 상반기 구매 기록이 있는 고객 중 하반기 구매 기록이 없는 고객"


def test_reference_sentence_builds_the_event_expression() -> None:
    plan = plan_for(REFERENCE)
    expression = event_ir.condition_from_dict(plan["event_expression"]["expression"])
    assert [
        (view.negated, view.window.to_calendar_window())
        for view in event_ir.existence_views(expression)
    ] == [
        (False, {"from": f"{YEAR}0101", "to": f"{YEAR}0630"}),
        (True, {"from": f"{YEAR}0701", "to": f"{YEAR}1231"}),
    ]


def test_reference_sentence_releases_the_legacy_slots_it_replaced() -> None:
    """같은 어구가 두 번 컴파일되지 않도록 IR 이 슬롯을 회수한다."""
    target_user = plan_for(REFERENCE)["target_user"]
    assert target_user.get("purchase_date") is None
    assert target_user.get("purchase_inactivity") is None
    assert "no_purchase" not in (target_user.get("behaviors") or [])


def test_reference_sentence_compiles_to_exists_and_not_exists() -> None:
    sql = sql_for(REFERENCE)
    assert sql is not None, "실행 SQL 이 생성돼야 한다"
    assert f"EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL EO" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL EO" in sql
    assert f">= '{YEAR}0101'" in sql and f"< '{YEAR}0701'" in sql
    assert f">= '{YEAR}0701'" in sql and f"< '{YEAR+1}0101'" in sql


def test_windowed_absence_alone_is_not_inverted() -> None:
    """회귀 고정: 예전에는 이 문장이 '하반기에 구매한 회원'을 뽑았다(부정 소실)."""
    sql = sql_for("하반기 구매 기록이 없는 고객")
    assert sql is not None
    assert "NOT EXISTS" in sql
    assert f"BETWEEN '{YEAR}0701'" not in sql, "부정이 사라지고 기간 필터만 남으면 안 된다"


def test_windowed_absence_is_not_downgraded_to_lifetime_absence() -> None:
    plan = plan_for("2026년 7월부터 12월까지 구매가 없는 고객")
    assert "no_purchase" not in (plan["target_user"].get("behaviors") or [])
    expression = event_ir.condition_from_dict(plan["event_expression"]["expression"])
    view = event_ir.existence_views(expression)[0]
    assert view.negated is True
    assert view.window.to_calendar_window() == {"from": "20260701", "to": "20261231"}


@pytest.mark.parametrize(
    "query",
    [
        "올해 상반기 구매 이력이 있는 고객 중 하반기 구매 이력이 없는 고객",
        "올해 상반기 구매 내역이 있는 고객 중 하반기 구매 내역이 없는 고객",
        "올해 상반기에 구매한 적이 있고 하반기에는 구매하지 않은 고객",
    ],
)
def test_synonyms_reach_the_same_sql(query: str) -> None:
    assert sql_for(query) == sql_for(REFERENCE)


# ── 호환 계층: 기존 슬롯이 담을 수 있으면 IR 은 물러난다 ───────────────────────────

@pytest.mark.parametrize(
    "query, slot",
    [
        ("최근 30일 구매하지 않은 고객", "purchase_inactivity"),
        ("2026년 상반기에 구매한 고객", "purchase_date"),
        ("최근 30일 동안 로그인한 고객", "recent_login"),
    ],
)
def test_legacy_expressible_conditions_keep_the_existing_path(query: str, slot: str) -> None:
    plan = plan_for(query)
    assert plan.get("event_expression") is None, "슬롯이 담을 수 있으면 IR 이 실행 모델이 되면 안 된다"
    assert plan["target_user"].get(slot), f"기존 슬롯 {slot} 이 그대로 소유해야 한다"


def test_lifetime_absence_keeps_the_behavior_slot() -> None:
    plan = plan_for("구매 이력이 없는 고객")
    assert plan.get("event_expression") is None
    assert "no_purchase" in plan["target_user"]["behaviors"]


def test_brand_scope_is_preserved_when_legacy_product_extraction_has_no_owner() -> None:
    """브랜드 scope를 버리고 일반 구매로 축소하지 않으며, 물리 미지원이면 차단한다."""
    plan = plan_for("알로루 브랜드를 구매한 고객")
    expression = plan["event_expression"]["expression"]
    assert "purchase.brand" in str(expression)
    assert "알로루" in str(expression)
    assert plan["event_compiler_capability"]["status"] == "unsupported"
    assert graph_rag.build_sql_template_candidate(plan) is None


# ── 회원 속성 결합 ────────────────────────────────────────────────────────────────

def test_member_attributes_are_and_combined() -> None:
    sql = sql_for("올해 상반기 구매 기록이 있고 하반기 구매 기록이 없는 30대 여성 고객")
    assert sql is not None
    assert "NOT EXISTS" in sql and "EXISTS" in sql
    assert "B.GENDER" in sql.upper() or "GENDER" in sql.upper()


def test_temporal_relation_reaches_sql_through_the_pipeline() -> None:
    """새 문장 유형('첫 구매 후 30일 이내 재구매')이 IR 타입 추가 없이 실행 SQL 까지 간다."""
    sql = sql_for("첫 구매 후 30일 이내 재구매한 고객")
    assert sql is not None
    assert "MIN(EO1.ORDER_DATE)" in sql
    assert "DATEADD(DAY, 30," in sql


def test_aggregate_sentences_still_belong_to_the_registry_backed_builder() -> None:
    """집계 임계는 지표 레지스트리를 아는 기존 빌더가 계속 소유한다(IR 은 물러난다)."""
    plan = plan_for("최근 30일 동안 3회 이상 구매한 고객")
    assert plan.get("event_expression") is None
    assert plan["target_user"]["aggregate_conditions"]


def test_uncompilable_atom_fails_closed_instead_of_raising() -> None:
    """컴파일 못 하는 원자(미등록 필드)는 검증 토큰을 못 만들고 SQL 이 막힌다 — 예외로 500 이 되면 안 된다.

    구조화 계층이 스키마에 없는 필드를 만들어 낼 수 있으므로(예: 'purchase.product_scope'), 컴파일
    불가는 정상 경로의 한 갈래다. 사용자에게는 오류가 아니라 사유가 나가야 한다.
    """
    query = "노트북을 구매한 고객"
    plan = plan_for(query)
    plan["event_expression"] = {
        "expression": {
            "type": "exists",
            "relation": {
                "type": "filter",
                "relation": {"type": "source", "name": "purchase"},
                "where": {
                    "type": "comparison",
                    "operator": "=",
                    "left": {"type": "field", "name": "purchase.not_a_registered_field"},
                    "right": {"type": "literal", "value": "노트북"},
                    "evidence": {"text": "노트북", "start": 0, "end": 3},
                },
            },
            "evidence": {"text": "노트북을 구매", "start": 0, "end": 7},
        },
        "source_text": query,
        "evidence_span": [0, 7],
    }

    assert graph_rag.build_verified_condition_tokens(plan) is not None
    assert all(
        not token["path"].startswith("plan.event_expression")
        for token in graph_rag.build_verified_condition_tokens(plan)
    ), "컴파일 불가 원자는 검증 토큰을 만들지 않는다(만들면 미지원 SQL 이 통과한다)"
