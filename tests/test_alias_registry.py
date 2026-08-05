"""alias 레지스트리 테스트 — 사전이 **조용히 비거나 어긋나지 않는지** 검사한다.

이 계층의 고장은 눈에 띄지 않는다. 사전이 빈 채로 로드되면 파서는 예외 없이 돌면서 모든
문장을 fallback 으로 보내고, 그 상태는 '아직 규칙이 부족한 것'과 구분되지 않는다. 그래서
로딩 실패는 반드시 **예외**여야 하고, 그 사실을 테스트가 고정해야 한다.

요구 사양 15절의 항목들(schema validation / 중복 탐지 / 충돌 오류)이 여기서 검증된다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nl_event_ir.aliases import (
    AliasConflictError,
    AliasFileError,
    AliasRegistry,
    AliasSchemaError,
    AliasSection,
    find_alias_matches,
    load_alias_registry,
)
from nl_event_ir.enums import ComparisonOperator, EntityType, EventType, LogicOperator


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ── 기본 조회 ────────────────────────────────────────────────────────────────────


def test_loads_the_shipped_alias_file() -> None:
    registry = load_alias_registry()
    assert registry.lookup(AliasSection.EVENT, "구입") == EventType.PURCHASE.value
    assert registry.lookup(AliasSection.ENTITY, "고객") == EntityType.MEMBER.value
    assert registry.lookup(AliasSection.COMPARISON, "이상") == ComparisonOperator.GTE.value
    assert registry.lookup(AliasSection.LOGIC, "또는") == LogicOperator.OR.value


def test_unknown_surface_returns_none_rather_than_guessing() -> None:
    registry = load_alias_registry()
    assert registry.lookup(AliasSection.EVENT, "텔레포트") is None


def test_every_canonical_value_maps_to_a_real_enum_member() -> None:
    """사전이 enum 에 없는 값을 선언하면 파서가 런타임에 죽는다. 미리 잡는다."""

    registry = load_alias_registry()
    pairs = [
        (AliasSection.EVENT, EventType),
        (AliasSection.ENTITY, EntityType),
        (AliasSection.COMPARISON, ComparisonOperator),
        (AliasSection.LOGIC, LogicOperator),
    ]
    for section, enum_type in pairs:
        for canonical in registry.canonical_values(section):
            enum_type(canonical)  # 값이 없으면 ValueError


# ── 교대 순서(긴 것 우선) ─────────────────────────────────────────────────────────


def test_surfaces_are_ordered_longest_first() -> None:
    """짧은 낱말이 앞에 오면 그것을 접두어로 가진 긴 낱말은 영영 매치되지 않는다."""

    registry = load_alias_registry()
    surfaces = registry.surfaces(AliasSection.EVENT)
    lengths = [len(surface) for surface in surfaces]
    assert lengths == sorted(lengths, reverse=True)


def test_longer_alias_wins_when_one_contains_another() -> None:
    registry = load_alias_registry()
    matches = find_alias_matches("회원가입한 사용자", registry, AliasSection.EVENT)
    assert [match[2] for match in matches] == ["회원가입"]


def test_data_cannot_smuggle_regex_syntax() -> None:
    """사전은 사전이지 코드가 아니다. 정규식 메타문자는 이스케이프되어야 한다."""

    registry = AliasRegistry.from_mapping({"event": {"purchase": ["a.c"]}})
    assert find_alias_matches("abc", registry, AliasSection.EVENT) == ()
    assert len(find_alias_matches("a.c", registry, AliasSection.EVENT)) == 1


# ── 스키마 검증 ──────────────────────────────────────────────────────────────────


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AliasFileError, match="cannot read alias file"):
        load_alias_registry(tmp_path / "nope.json")


def test_malformed_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AliasFileError, match="not valid JSON"):
        load_alias_registry(path)


def test_root_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(AliasSchemaError, match="root must be an object"):
        load_alias_registry(_write(tmp_path, ["event"]))


def test_typo_in_a_section_name_is_rejected(tmp_path: Path) -> None:
    """오타 난 절을 무시하면 그 어휘가 통째로 사라진 채 파서가 '정상' 동작한다."""

    with pytest.raises(AliasSchemaError, match="unknown alias section"):
        load_alias_registry(_write(tmp_path, {"evnet": {"purchase": ["구매"]}}))


def test_metadata_keys_are_allowed(tmp_path: Path) -> None:
    registry = load_alias_registry(
        _write(
            tmp_path,
            {"version": 1, "description": "x", "how_to_add": [], "event": {"purchase": ["구매"]}},
        )
    )
    assert registry.lookup(AliasSection.EVENT, "구매") == "purchase"


def test_section_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(AliasSchemaError, match="must be an object"):
        load_alias_registry(_write(tmp_path, {"event": ["구매"]}))


def test_canonical_value_must_map_to_a_list(tmp_path: Path) -> None:
    with pytest.raises(AliasSchemaError, match="must map to a list"):
        load_alias_registry(_write(tmp_path, {"event": {"purchase": "구매"}}))


@pytest.mark.parametrize("bad", [["", "구매"], [" ", "구매"], [None], [123]])
def test_empty_or_non_string_surface_is_rejected(tmp_path: Path, bad: list) -> None:
    with pytest.raises(AliasSchemaError, match="empty or non-string surface"):
        load_alias_registry(_write(tmp_path, {"event": {"purchase": bad}}))


# ── 중복과 충돌 ──────────────────────────────────────────────────────────────────


def test_duplicate_surface_within_one_value_is_rejected(tmp_path: Path) -> None:
    """같은 값 안의 중복은 데이터 실수다. 삼키면 사전이 계속 자란다."""

    with pytest.raises(AliasSchemaError, match="duplicate surface"):
        load_alias_registry(_write(tmp_path, {"event": {"purchase": ["구매", "구매"]}}))


def test_one_surface_on_two_canonical_values_is_a_conflict(tmp_path: Path) -> None:
    """한 절 안에서 한 표면어가 두 뜻을 가지면 어느 쪽으로 읽힐지 순서에 좌우된다."""

    with pytest.raises(AliasConflictError, match="registered to both"):
        load_alias_registry(
            _write(tmp_path, {"event": {"purchase": ["주문"], "cart": ["주문"]}})
        )


def test_the_shipped_file_has_no_conflicts() -> None:
    """운영 사전 자체가 계약을 지키는지 확인한다(로딩이 곧 검증이다)."""

    assert load_alias_registry().sections


def test_same_surface_in_different_sections_is_allowed(tmp_path: Path) -> None:
    """절이 다르면 축이 다르다. ``제외`` 는 논리 배제이면서 부정 표지일 수 있다."""

    registry = load_alias_registry(
        _write(tmp_path, {"logic": {"not": ["제외"]}, "polarity": {"negative": ["제외"]}})
    )
    assert registry.lookup(AliasSection.LOGIC, "제외") == "not"
    assert registry.lookup(AliasSection.POLARITY, "제외") == "negative"


# ── 사전이 담지 말아야 할 것 ──────────────────────────────────────────────────────


def test_alias_file_holds_no_conjugated_verb_forms() -> None:
    """활용형은 규칙이 처리한다. 사전에 들어가면 예전 구조로 되돌아간다."""

    registry = load_alias_registry()
    surfaces = {
        surface for section in registry.sections.values() for surface in section
    }
    assert not surfaces & {"샀", "산", "사서", "사고", "샀다", "구매한", "구매했"}


def test_alias_file_holds_no_catalog_data() -> None:
    registry = load_alias_registry()
    surfaces = {
        surface for section in registry.sections.values() for surface in section
    }
    assert not surfaces & {"나이키", "아디다스", "뉴발란스", "애플", "진로"}


def test_empty_section_alternation_fails_loudly() -> None:
    registry = AliasRegistry.from_mapping({"event": {"purchase": ["구매"]}})
    with pytest.raises(AliasSchemaError, match="alias section is empty"):
        registry.alternation(AliasSection.SORT)


# ── 불변성 ───────────────────────────────────────────────────────────────────────


def test_registry_sections_are_read_only() -> None:
    """전역 사전이 실행 중에 바뀌면 같은 입력이 다른 결과를 낸다."""

    registry = load_alias_registry()
    with pytest.raises(TypeError):
        registry.sections["event"]["새말"] = "purchase"  # type: ignore[index]
