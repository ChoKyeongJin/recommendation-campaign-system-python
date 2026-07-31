"""선언과 실행 자산이 갈라지지 않게 하는 가드 모음.

이 저장소에는 "선언(레지스트리·설정)"과 "실행(빌더·컴파일러·카탈로그)"을 잇는 배선이 여럿 있는데,
그중 몇 개는 소스 주석이 "테스트가 강제한다"고 적어 놓고도 실제 테스트가 없었다. 가드가 없으면
드리프트는 조용히 일어나고, 증상은 SQL 이 '성공하는데 0명'이거나 조건이 무성 삭제되는 형태로 나온다.

여기 모은 불변식은 전부 지금 green 이다(도입 시점에 red 인 것은 없다). 즉 이 파일은 부채를
드러내는 게 아니라 이미 맞춰져 있는 상태를 고정한다.

경로 주의: 설정 로더 여럿이 상대경로 기본값을 쓴다. pytest.ini 가 rootdir 을 고정하지만, cwd 까지
의존하는 검사는 명시적으로 절대경로를 넘긴다(그러지 않으면 CI 워킹디렉터리가 바뀌는 순간
'가드는 green 인데 프로덕션만 강등'이라는 최악의 조합이 된다).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import capability_registry  # noqa: E402
import compiler_strategies  # noqa: E402
import graph_rag  # noqa: E402
import join_paths  # noqa: E402
import semantic_requirements  # noqa: E402
import targeting_ir  # noqa: E402


# ── capability 선언 ↔ 실행 자산 ──────────────────────────────────────────────────────


def test_capability_declarations_resolve_to_real_execution_assets() -> None:
    """supported=true 인 capability 는 compiler/join/field 가 실재해야 한다.

    validate_capabilities() 는 이걸 검사하라고 만들어졌는데 호출부가 한 곳도 없었다.
    선언만 바꾸고 실행 자산을 안 만들면 조용히 미지원으로 강등되는 창구가 열려 있던 셈이다.
    """

    # 레지스트리를 명시 주입한다 — 모듈 기본 경로가 상대경로라 cwd 가 레포 밖이면 로더가 터지거나
    # 조용히 빈 상태가 된다(가드는 green 인데 프로덕션만 강등되는 조합을 피한다).
    violations = capability_registry.validate_capabilities(
        registry=capability_registry.CapabilityRegistry.load(
            REPO_ROOT / "docs" / "data" / "requirement_capabilities.json"
        ),
        schema_fields=capability_registry.SchemaFields.load(
            REPO_ROOT / "docs" / "data" / "schema_catalog.json"
        ),
    )
    assert violations == [], "capability 선언과 실행 자산이 어긋남:\n  " + "\n  ".join(map(str, violations))


def test_capability_validation_is_not_vacuous() -> None:
    """검사 대상이 0건이면 green 은 아무 의미가 없다."""

    registry = capability_registry.CapabilityRegistry.load(
        REPO_ROOT / "docs" / "data" / "requirement_capabilities.json"
    )
    assert len(registry.capabilities) >= 8, f"capability 수가 비정상적으로 적다: {len(registry.capabilities)}"


# ── fact_join 조건 ↔ 소유 빌더 ───────────────────────────────────────────────────────

# fact_join=False 인데 전용 빌더가 소유하는 의도된 예외. 캠페인 반응 EXISTS(>=1회)는 회원키
# EXISTS 술어라 어느 빌더에나 AND 결합이 가능해서 fact_join 이 아니다(targeting_ir.py 주석 참조).
_INTENTIONAL_NON_FACT_JOIN_OWNERS = frozenset({"campaign_responses"})


def _owner_map() -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for builder, kinds in graph_rag._sql_target_builder_registry():
        for kind in kinds:
            owners.setdefault(kind, []).append(getattr(builder, "__name__", str(builder)))
    return owners


def test_every_fact_join_kind_has_an_owning_builder() -> None:
    """소유자 없는 조건은 EXISTS-류 범용 빌더가 양보한 채 아무도 안 만들어 조용히 사라진다.

    실제로 났던 사고와 같은 형태다 — '캠페인 반응 횟수'가 주문 집계로 오배정된 건.
    """

    ownerless = sorted(set(targeting_ir.fact_join_kinds()) - set(_owner_map()))
    assert not ownerless, f"fact_join 인데 소유 빌더가 없다: {ownerless}"


def test_no_kind_is_owned_by_two_builders() -> None:
    """두 빌더가 같은 조건을 소유하면 후보 순서가 곧 의미가 된다."""

    duplicated = {kind: names for kind, names in _owner_map().items() if len(names) > 1}
    assert not duplicated, f"중복 소유: {duplicated}"


def test_builder_registry_kinds_are_declared_conditions() -> None:
    """레지스트리가 존재하지 않는 kind 를 소유한다고 선언하면(오탈자) 그 배선은 죽은 채로 남는다."""

    declared = {spec.kind for spec in targeting_ir.CONDITION_SPECS}
    unknown = sorted(set(_owner_map()) - declared)
    assert not unknown, f"CONDITION_SPECS 에 없는 kind 를 빌더가 소유한다: {unknown}"


def test_non_fact_join_owners_are_the_declared_exceptions() -> None:
    """의도된 예외 목록이 조용히 늘어나지 않게 고정한다."""

    owned_but_not_fact_join = sorted(set(_owner_map()) - set(targeting_ir.fact_join_kinds()))
    assert set(owned_but_not_fact_join) == _INTENTIONAL_NON_FACT_JOIN_OWNERS, (
        f"fact_join=False 인데 전용 빌더가 소유하는 kind 가 바뀌었다: {owned_but_not_fact_join}. "
        "의도된 설계라면 _INTENTIONAL_NON_FACT_JOIN_OWNERS 에 사유와 함께 추가하라."
    )


# ── 조인 경로 선언 ↔ 카탈로그 ────────────────────────────────────────────────────────


def test_declared_join_paths_exist_in_the_schema_catalog() -> None:
    """조인 경로가 실재하지 않는 테이블·컬럼을 가리키면 DB 스왑 때 조용히 깨진다."""

    import db_swap_preflight

    catalog = db_swap_preflight._load_json(REPO_ROOT / "docs" / "data" / "schema_catalog.json")
    columns_by_table, _ = db_swap_preflight._catalog_index(catalog)

    missing: list[str] = []
    for name, path in join_paths.JOIN_PATHS.items():
        for table in (path.source_table, path.target_table):
            if table not in columns_by_table:
                missing.append(f"{name}: 테이블 {table}")
    assert not missing, f"카탈로그에 없는 조인 대상: {missing}"


# ── SQL 리터럴 미러 패리티 ───────────────────────────────────────────────────────────

_QUOTE_INPUTS = ["abc", "O'Brien", "a''b", "it''s a 'test'", "신세계백화점", "", 5, 0, 3.14, None]


@pytest.mark.parametrize("value", _QUOTE_INPUTS)
def test_sql_quote_mirrors_are_byte_identical(value: object) -> None:
    """같은 WHERE 리스트에 섞이는 리터럴이라 한 글자만 달라도 다른 SQL 이 된다.

    이 계약은 지금까지 주석으로만 있었고 검증하는 테스트는 삭제돼 있었다. 실제로 갈라져 있었다 —
    graph_rag 쪽만 str() 캐스팅이 없어 비문자열에서 AttributeError 를 던졌다.
    """

    import event_compiler

    assert graph_rag._sql_quote(value) == compiler_strategies._quote(value)
    assert graph_rag._sql_quote(value) == event_compiler._sql_quote(value)


@pytest.mark.parametrize("term", _QUOTE_INPUTS)
def test_nlike_contains_mirrors_are_byte_identical(term: object) -> None:
    column = "P.BRAND_NAME"
    assert graph_rag._sql_nlike_contains(column, term) == compiler_strategies._nlike_contains(column, term)


def test_sql_literals_have_exactly_one_implementation() -> None:
    """복제가 다시 생기지 않게 소스에서 직접 조립하는 지점을 막는다.

    패리티 테스트만으로는 부족하다 — 새 복제가 '지금은 같은 출력'을 내며 들어오면 통과하고,
    나중에 갈라진다. 조립 자체를 한 곳으로 묶어야 구조적으로 재발이 막힌다.
    """

    import re

    pattern = re.compile(r"""\.replace\(\s*(?:"'"\s*,\s*"''"|chr\(39\)\s*,\s*chr\(39\)\s*\*\s*2)""")
    offenders: list[str] = []
    for path in REPO_ROOT.glob("*.py"):
        if path.name == "sql_dialect.py":
            continue  # 단일 소유자
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{index}")
    assert not offenders, (
        "sql_dialect 밖에서 SQL 리터럴 이스케이프를 직접 조립한다: " + ", ".join(offenders)
        + ". sql_dialect.quote_literal / nlike_contains 를 쓰라."
    )


@pytest.mark.parametrize(
    "value",
    ["알로&루", "A-BC ", "ALLO LU", "신세계  백화점", "스타벅스(리저브)", "", "  ", "Nike#1", None],
)
def test_value_normalization_mirrors_agree(value: object) -> None:
    """정규화가 갈리면 canonical 보정된 정상 SQL 이 '미반영'으로 오탐돼 출고가 부당 차단된다."""

    assert graph_rag._normalize_product_term(value) == semantic_requirements._normalize_value(value)


# ── 설정 레지스트리 건전성 ───────────────────────────────────────────────────────────


def test_three_registries_load_non_empty() -> None:
    """로더가 실패를 삼키고 빈 레지스트리로 강등되면 단위·의미가 조용히 사라진다.

    강등은 예외가 아니라 '조금 다른 답'으로 나타나기 때문에 눈으로는 잡히지 않는다.
    """

    assert graph_rag._METRIC_REGISTRY is not None, "지표 레지스트리가 강등됐다(빈 상태)."
    assert graph_rag._METRIC_REGISTRY.specs, "지표 스펙이 0건 — docs/data/metrics 를 못 읽었다."
    assert graph_rag._SEGMENT_SEMANTICS is not None, "세그먼트 의미 레지스트리가 강등됐다."
    assert graph_rag._REQUIREMENT_REGISTRY is not None, (
        "requirement capability 레지스트리가 강등됐다 — 회계 계층 전체가 무동작이 된다."
    )


def test_no_registry_reports_a_degradation() -> None:
    """로더가 실패를 삼켰는지 자체를 본다 — 삼킨 사실이 어디에도 안 남으면 아무도 모른다."""

    degraded = {name: reason for name, reason in graph_rag.REGISTRY_HEALTH.items() if reason}
    assert not degraded, f"강등된 레지스트리: {degraded}"


def test_registry_defaults_are_absolute_paths() -> None:
    """상대경로 기본값은 cwd 가 바뀌는 순간 '가드는 green 인데 프로덕션만 강등'을 만든다."""

    import metric_registry
    import segment_semantics

    relative = [
        f"{module.__name__}.{name}"
        for module in (capability_registry, metric_registry, segment_semantics, semantic_requirements)
        for name in dir(module)
        if name.startswith("DEFAULT_") and isinstance(getattr(module, name), Path)
        and not getattr(module, name).is_absolute()
    ]
    assert not relative, f"상대경로 기본값이 남아 있다: {relative}"
