"""설정 파일끼리 참조하는 지점이 조용히 끊기지 않는지 지킨다.

이 저장소의 설정은 서로를 문자열로 참조한다 — 한쪽이 다른 쪽의 섹션 경로·키 이름·개념 id 를
이름으로만 가리킨다. 그 이름이 어긋나면 예외가 아니라 **침묵**이 발생한다: 인덱스가 빈 채로
만들어지거나(continue), 닫힌 집합 검증에서 신호가 버려지거나, 지표가 미등록으로 폴백된다.
증상이 '조금 다른 답'이라 눈에 띄지 않으므로 가드가 없으면 드리프트를 영영 모른다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
DATA = REPO_ROOT / "docs" / "data"

import aggregate_parser_config  # noqa: E402
import aggregate_spans  # noqa: E402
import graph_rag  # noqa: E402


def _json(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# ── aggregate_parser_rules.json → member_target_filters.json 섹션 경로 ────────────────


def test_every_attribute_source_section_resolves() -> None:
    """규칙 JSON 이 가리키는 섹션 경로가 안 풀리면 build_attribute_index 가 조용히 건너뛴다."""

    filters = graph_rag._load_member_target_filters()
    rules = aggregate_parser_config.rules()
    broken: list[str] = []
    for source in rules.supported_attribute_sources:
        node = filters
        for part in source.section.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if not node:
            broken.append(source.section)
    assert not broken, (
        f"member_target_filters.json 에서 풀리지 않는 섹션 경로: {broken}. "
        "설정 섹션을 개명·재구조화하면 임계값 속성 결합이 통째로 침묵한다."
    )


def test_attribute_index_is_not_empty() -> None:
    """경로가 풀려도 인덱스가 비면 결과는 같다 — 공허한 통과를 막는다."""

    index = aggregate_spans.build_attribute_index(
        graph_rag._load_member_target_filters(), aggregate_parser_config.rules()
    )
    assert len(index.supported) > 20, f"지원 속성 별칭이 너무 적다: {len(index.supported)}"


# ── surface_concepts.json ↔ 코드 상수(objective 이름) ────────────────────────────────


def test_every_campaign_objective_has_a_surface_concept() -> None:
    """코드가 f'objective_{objective}' 로 조회하므로 짝이 없으면 신호가 조용히 버려진다."""

    concept_ids = {concept["concept_id"] for concept in _json("surface_concepts.json")["concepts"]}
    missing = sorted(
        objective
        for objective in graph_rag.CAMPAIGN_OBJECTIVES
        if f"objective_{objective}" not in concept_ids
    )
    assert not missing, (
        f"surface_concepts.json 에 짝이 없는 캠페인 목적: {missing}. "
        "닫힌 집합 검증에서 그 목적 신호가 조용히 버려진다."
    )


def test_no_orphan_objective_concepts() -> None:
    """반대 방향 — 코드가 모르는 objective 개념이 남아 있으면 죽은 선언이다."""

    concept_ids = {concept["concept_id"] for concept in _json("surface_concepts.json")["concepts"]}
    declared = {f"objective_{objective}" for objective in graph_rag.CAMPAIGN_OBJECTIVES}
    orphans = sorted(
        cid for cid in concept_ids if cid.startswith("objective_") and cid not in declared
    )
    assert not orphans, f"CAMPAIGN_OBJECTIVES 에 없는 objective 개념: {orphans}"


# ── metrics/*.json ↔ member_target_filters.numeric_filters ──────────────────────────


def test_metric_spec_sources_exist_in_the_schema_catalog() -> None:
    """지표 스펙이 선언한 물리 테이블·컬럼이 카탈로그에 실재해야 한다.

    지표 정의는 코드에서 통합 스펙 레지스트리로 이관 중이라, 스펙마다 자기 소스를 선언한다
    (numeric_filters 와 조인되지 않는 것이 정상이다). 대신 그 선언이 실제 스키마와 맞는지는
    아무도 안 보고 있었다 — 어긋나면 DB 스왑 때 '성공하는데 0명'이 된다.
    """

    import db_swap_preflight

    catalog = db_swap_preflight._load_json(DATA / "schema_catalog.json")
    columns_by_table, _ = db_swap_preflight._catalog_index(catalog)

    registry = graph_rag._METRIC_REGISTRY
    assert registry is not None and registry.specs, "지표 레지스트리가 비었다."

    problems: list[str] = []
    for spec in registry.specs:
        metric_id = getattr(spec, "metric_id", None) or getattr(spec, "id", None)
        source = getattr(spec, "source", None)
        if source is None:
            continue
        table = getattr(source, "table", None)
        column = getattr(source, "column", None)
        if not table or not column:
            continue
        present = columns_by_table.get(table)
        if present is None:
            problems.append(f"{metric_id}: 테이블 {table} 이 카탈로그에 없음")
        elif column not in present:
            problems.append(f"{metric_id}: {table}.{column} 이 카탈로그에 없음")
    assert not problems, "지표 스펙의 물리 소스가 카탈로그와 어긋남:\n  " + "\n  ".join(problems)


# ── clarification 메시지 키 ─────────────────────────────────────────────────────────


def test_unsupported_message_keys_resolve() -> None:
    """미지원 힌트가 가리키는 안내 문구 키가 없으면 사용자에게 빈 안내가 나간다."""

    rules = aggregate_parser_config.rules()
    messages = _json("clarification_messages.ko.json")
    known = set(messages.get("messages", messages).keys())
    referenced = {
        hint.message_key
        for hint in getattr(rules, "unsupported_attribute_hints", ())
        if getattr(hint, "message_key", None)
    }
    missing = sorted(referenced - known)
    assert not missing, f"clarification_messages.ko.json 에 없는 message_key: {missing}"
