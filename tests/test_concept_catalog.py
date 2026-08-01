"""ConceptCatalog 계약 — 파생(집계/프로필 지표) + 선언(특수 슬롯) 병합, 결정론 검색, 실행 가능성.

관례: ① 레지스트리 비어 있음 실패, ② 선언↔실행자산 배선 검증(compiler 실재),
③ 임의 개념 생성 불가(검색 결과 ⊆ 카탈로그), ④ 이중 소유 충돌 즉시 실패."""

from __future__ import annotations

import json

import pytest

import compiler_registry
from concept_catalog import ConceptCatalog, ConceptCatalogError, ConceptSpec, default_catalog


def test_catalog_is_not_empty_and_contains_all_three_sources() -> None:
    catalog = default_catalog()
    ids = catalog.concept_ids()
    assert ids, "카탈로그가 비었다 — 검사가 공허하다."
    # 파생: 집계 지표(member_target_filters.json aggregate_targets.metrics)
    assert "purchase_amount" in ids and "order_count" in ids
    # 파생: 프로필 지표(metric_registry, targeting.enabled)
    assert "buy_cycle" in ids
    # 선언: 특수 슬롯(docs/data/runtime/semantics/concept_catalog.json)
    assert "purchase_inactivity" in ids and "inactivity_period" in ids


def test_every_executable_concept_has_registered_compiler() -> None:
    """선언된 compiler 가 실제 레지스트리에 없는데 executable 로 보이면 조용한 실행 오류가 된다."""
    catalog = default_catalog()
    registry = compiler_registry.default_registry()
    executable = [cid for cid in catalog.concept_ids() if catalog.is_executable(cid)]
    assert executable, "실행 가능 개념이 하나도 없다 — 배선이 끊겼다."
    for concept_id in executable:
        compiler = catalog.get_compiler(concept_id)
        assert registry.has(compiler), f"{concept_id}: compiler {compiler!r} 가 default_registry 에 없다."


def test_unregistered_synonym_links_to_existing_concept() -> None:
    """문장 전체를 등록하지 않아도 별칭 검색이 기존 개념으로 연결된다(요청 2·8번)."""
    catalog = default_catalog()
    for query, expected in [
        ("석 달째 아무것도 안 산 고객", "purchase_inactivity"),
        ("3개월 동안 구매가 없는 고객", "purchase_inactivity"),
        ("한 분기 동안 미구매 고객", "purchase_inactivity"),
        ("최근 30일 동안 결제한 돈이 10만원 이상인 고객", "purchase_amount"),
        ("지난달 주문 총액이 최소 10만원인 고객", "purchase_amount"),
    ]:
        results = catalog.search_concepts(query, limit=3)
        assert results, f"{query!r} 후보가 비었다"
        assert results[0]["canonical_id"] == expected, f"{query!r} → {results[0]['canonical_id']}"


def test_search_never_returns_ids_outside_catalog() -> None:
    """LLM 이 고를 수 있는 후보는 전부 카탈로그 실재 개념이다 — 임의 canonical_id 생성 불가."""
    catalog = default_catalog()
    for query in ["구매", "금액", "로그인", "장바구니"]:
        for result in catalog.search_concepts(query, limit=10):
            assert result["canonical_id"] in catalog.concept_ids()


def test_unknown_concept_is_not_invented() -> None:
    catalog = default_catalog()
    assert catalog.search_concepts("고객 생애가치가 앞으로 상승할 가능성") == []
    assert catalog.get_concept("predicted_ltv_growth") is None
    assert not catalog.is_executable("predicted_ltv_growth")
    assert catalog.get_compiler("predicted_ltv_growth") is None


def test_search_is_deterministic() -> None:
    catalog = default_catalog()
    first = catalog.search_concepts("구매 금액", limit=5)
    second = catalog.search_concepts("구매 금액", limit=5)
    assert first == second


def test_inactive_concept_is_not_executable() -> None:
    spec = ConceptSpec(concept_id="ghost_metric", label="유령", compiler="purchase_aggregate",
                       normalizer="generic_condition", active=False)
    catalog = ConceptCatalog({"ghost_metric": spec}, compiler_available=lambda name: True)
    assert not catalog.is_executable("ghost_metric")


def test_missing_compiler_downgrades_executability_not_membership() -> None:
    """개념은 있되(catalog_match 가능) 컴파일러가 없으면 실행만 막힌다(요청 3번 축 분리)."""
    spec = ConceptSpec(concept_id="known_metric", label="아는 지표", compiler="not_registered_anywhere",
                       normalizer="generic_condition")
    catalog = ConceptCatalog({"known_metric": spec}, compiler_available=lambda name: False)
    assert catalog.get_concept("known_metric") is not None
    assert not catalog.is_executable("known_metric")


def test_declared_id_colliding_with_derived_source_fails_fast(tmp_path) -> None:
    """파생 소스(설정)와 선언(JSON)이 같은 id 를 가지면 이중 소유다 — 조용한 우선순위 대신 실패."""
    payload = {
        "concepts": {
            "purchase_amount": {
                "label": "중복 선언", "kind": "generic_condition",
                "normalizer": "generic_condition", "compiler": "purchase_aggregate",
            }
        }
    }
    path = tmp_path / "concept_catalog.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConceptCatalogError):
        ConceptCatalog.load(path)


def test_unregistered_normalizer_in_declaration_fails_fast() -> None:
    with pytest.raises(ConceptCatalogError):
        ConceptSpec(concept_id="x", label="x", normalizer="llm_invented_normalizer")


def test_aliases_are_derived_not_duplicated() -> None:
    """공통 지표 동의어의 소스는 member_target_filters.json 이다 — 카탈로그가 사본을 들고 있지 않다."""
    import member_filters_config

    config_synonyms = set(member_filters_config.aggregate_metrics()["purchase_amount"]["synonyms"])
    catalog_aliases = set(default_catalog().get_concept("purchase_amount").aliases)
    assert config_synonyms <= catalog_aliases, "설정 동의어가 카탈로그 파생에서 빠졌다"
