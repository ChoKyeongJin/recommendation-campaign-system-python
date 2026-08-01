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
import graph_rag
import metric_registry  # noqa: E402


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


# 소비자가 없어도 남겨 두기로 한 개념 — 이유를 여기 적지 않으면 남길 수 없다.
_CONCEPTS_WITHOUT_A_CONSUMER: dict[str, str] = {}


def test_every_surface_concept_has_a_consumer() -> None:
    """소비자 없는 개념은 프롬프트 토큰만 쓰고 판정에 쓰이지 않는다.

    2026-08-01 에 그런 개념이 25개 중 16개였다(intent_*/objective_*/awareness_* 등 — 소비 계층이
    커밋 8ba50b6 에서 사라졌는데 선언만 남아 있었다). 개념 목록은 '무엇을 판정할 수 있는가'의
    선언이므로, 아무도 조회하지 않는 항목이 섞여 있으면 그 선언 자체가 거짓이 된다.

    ``objective_*`` 를 CAMPAIGN_OBJECTIVES 와 대조하던 이전 두 테스트를 대체한다 — 그 테스트는
    "코드가 f'objective_{objective}' 로 조회한다"를 전제했는데 그 조회부가 이미 없었다.
    """

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(REPO_ROOT.glob("*.py")) + sorted((REPO_ROOT / "rag").glob("*.py"))
    )
    orphans = []
    for concept in _json("surface_concepts.json")["concepts"]:
        concept_id = concept["concept_id"]
        if concept_id in _CONCEPTS_WITHOUT_A_CONSUMER:
            continue
        quoted = f'"{concept_id}"'
        # aggregate_* 는 analytical_intent 가 f"aggregate_{fn}" 로 만든다(정적 문자열이 없다).
        dynamic = concept_id.startswith("aggregate_") and 'f"aggregate_{' in sources
        if quoted not in sources and not dynamic:
            orphans.append(concept_id)
    assert not orphans, (
        f"조회하는 코드가 없는 표면 개념: {orphans}. 소비자를 붙이거나 개념을 지워라 — "
        "남겨야 한다면 _CONCEPTS_WITHOUT_A_CONSUMER 에 이유와 함께 등록한다."
    )


def test_retained_orphan_concepts_still_exist() -> None:
    """허용 목록이 유령을 가리키면(개념은 지웠는데 예외만 남으면) 다음 사람이 오해한다."""

    concept_ids = {concept["concept_id"] for concept in _json("surface_concepts.json")["concepts"]}
    stale = sorted(set(_CONCEPTS_WITHOUT_A_CONSUMER) - concept_ids)
    assert not stale, f"이미 삭제된 개념이 허용 목록에 남아 있다: {stale}"


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

    # 지표 카탈로그는 metric_registry 가 단일 소유자다(graph_rag 재수출은 규칙 계층과 함께 사라졌다).
    registry = metric_registry.MetricRegistry.load()
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
