"""[Qualitative defaults] 손으로 적은 카탈로그가 **스스로 선언한 규약을 지키는가**.

`docs/data/runtime/policies/qualitative_defaults.sample.json` 은 '최근', '대부분', '자주'처럼
수치가 없는 표현의 기본 해석값을 200여 항목 기록한 참조 자산이다. 읽는 실행 코드가 없으므로
잘못 적혀도 아무 것도 깨지지 않는다 — 그래서 조용히 썩는다. 이 파일이 막는 것은 그 부패다.

고정하는 불변식은 파일이 `conventions` 에 **스스로 적어 둔 것**들이다. 규약을 밖에서 새로
만들지 않고, 선언과 데이터가 어긋나는 순간만 잡는다:

  - 크기에는 단위가 붙는다(CLAUDE.md §13). value/min/max 가 있으면 unit 이 있어야 한다.
  - 소수는 float 가 아니라 문자열이다(§14). JSON 실수 리터럴이 들어오면 실패한다.
  - kind·comparator·base 는 conventions/bases 에 선언된 것만 쓴다(§34 — 오타가 조용히 통과하면
    나중에 이 파일을 읽는 쪽이 알 수 없는 이름을 만난다).
  - rank 항목은 percent 만으로 완결되지 않는다(§7·§17·§18). 모집단·동점자·null·최소 표본을
    requires_definition 에 남겨, '상위 20%'가 숫자 비교로 환원되는 것을 문서 단에서 막는다.

**주의 — 이 테스트는 기본값이 옳은지 재지 않는다.** '최근 = 5일'이 업종에 맞는 값인지는
데이터로만 정해진다. 여기서 재는 것은 표기가 규약을 지키는가 하나다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "data"
    / "runtime"
    / "policies"
    / "qualitative_defaults.sample.json"
)

# rank 표현이 숫자 비교로 환원되지 않게 하려면 최소한 이 정책들이 미정으로 남아 있어야 한다.
_RANK_REQUIRED_DEFINITION_TOKENS = ("population", "tie_policy")


@pytest.fixture(scope="module")
def catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _entries(catalog: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (group["group_id"], entry)
        for group in catalog["groups"]
        for entry in group["entries"]
    ]


def _atoms(default: dict[str, Any]) -> list[dict[str, Any]]:
    return [atom for atom in default.get("criteria", []) if isinstance(atom, dict)]


def test_every_entry_preserves_the_original_wording(catalog: dict[str, Any]) -> None:
    """표현과 원문 기본값 표기는 손실 없이 남는다(§11 — 구조화가 원본을 대체하지 않는다)."""
    for group_id, entry in _entries(catalog):
        assert entry.get("expression"), f"{group_id}: expression 이 비었다"
        assert entry.get("default_text"), f"{group_id}/{entry.get('expression')}: default_text 가 비었다"
        assert isinstance(entry.get("default"), dict), (
            f"{group_id}/{entry['expression']}: default 는 객체여야 한다"
        )


def test_canonical_ids_are_unique(catalog: dict[str, Any]) -> None:
    """canonical 은 이 카탈로그의 키다. 중복되면 참조하는 쪽이 어느 항목인지 알 수 없다."""
    seen: dict[str, str] = {}
    for group_id, entry in _entries(catalog):
        canonical = entry["canonical"]
        assert canonical not in seen, (
            f"canonical 중복: {canonical} ({seen.get(canonical)} vs {group_id})"
        )
        seen[canonical] = group_id


def test_kinds_and_comparators_are_declared(catalog: dict[str, Any]) -> None:
    """kind·comparator 는 conventions 에 선언된 이름만 쓴다."""
    declared_kinds = set(catalog["conventions"]["value_kinds"])
    declared_comparators = set(catalog["conventions"]["comparators"])

    for group_id, entry in _entries(catalog):
        default = entry["default"]
        assert default["kind"] in declared_kinds, (
            f"{group_id}/{entry['canonical']}: 선언되지 않은 kind {default['kind']!r}"
        )
        comparators = [default["comparator"]] if "comparator" in default else []
        comparators += [atom["comparator"] for atom in _atoms(default) if "comparator" in atom]
        for comparator in comparators:
            assert comparator in declared_comparators, (
                f"{group_id}/{entry['canonical']}: 선언되지 않은 comparator {comparator!r}"
            )


def test_every_magnitude_carries_a_unit(catalog: dict[str, Any]) -> None:
    """크기에는 단위가 붙는다(§13). 숫자만 남기면 읽는 쪽이 단위를 추측하게 된다."""
    for group_id, entry in _entries(catalog):
        for atom in _atoms(entry["default"]):
            has_magnitude = any(key in atom for key in ("value", "min", "max"))
            if not has_magnitude:
                continue
            assert atom.get("unit"), (
                f"{group_id}/{entry['canonical']}: 단위 없는 크기 {atom}"
            )


def test_bases_resolve_to_declarations(catalog: dict[str, Any]) -> None:
    """비율의 분모는 bases 에 정의된 이름만 쓴다(§62 — 모집단 범위를 기본값에 맡기지 않는다)."""
    declared_bases = set(catalog["bases"])
    for group_id, entry in _entries(catalog):
        for atom in _atoms(entry["default"]):
            base = atom.get("base")
            if base is None:
                continue
            assert base in declared_bases, (
                f"{group_id}/{entry['canonical']}: 정의되지 않은 base {base!r}"
            )


def test_no_float_literals_in_the_catalog() -> None:
    """소수는 문자열로 적는다(§14). JSON 실수 리터럴은 부동소수 오차를 데이터에 심는다."""
    found: list[str] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, float):
            found.append(f"{path} = {node!r}")
        elif isinstance(node, dict):
            for key, value in node.items():
                visit(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]")

    visit(json.loads(CATALOG_PATH.read_text(encoding="utf-8")), "$")
    assert not found, f"실수 리터럴은 문자열로 적는다: {found}"


def test_rank_entries_leave_ranking_policy_undecided(catalog: dict[str, Any]) -> None:
    """상위 N% 는 percent 만으로 정의되지 않는다(§7·§17·§18).

    모집단과 동점자 정책이 requires_definition 에 남아 있어야, 이 파일을 읽는 쪽이
    '상위 20%'를 값 비교로 환원하지 않는다.
    """
    rank_entries = [
        (group_id, entry)
        for group_id, entry in _entries(catalog)
        if entry["default"]["kind"] == "rank"
    ]
    assert rank_entries, "rank 항목이 사라졌다면 이 래칫이 아무 것도 재지 않는다"

    for group_id, entry in rank_entries:
        pending = " ".join(entry["default"].get("requires_definition", []))
        for token in _RANK_REQUIRED_DEFINITION_TOKENS:
            assert token in pending, (
                f"{group_id}/{entry['canonical']}: requires_definition 에 {token} 이 없다"
            )


def test_every_group_declares_whether_it_reaches_sql(catalog: dict[str, Any]) -> None:
    """모든 그룹이 '실행 경로에 닿는가'를 정확히 한 번 선언한다.

    '카탈로그에 적혀 있다'와 'SQL 을 바꾼다'는 다르다. 선언이 빠진 그룹이 생기면 읽는 사람은
    236항목 전부가 동작한다고 오해하고, 두 번 선언되면 어느 쪽이 참인지 알 수 없다.
    """
    consumption = catalog["consumption"]
    declared: list[str] = [entry["group_id"] for entry in consumption["wired"]]
    for entry in consumption["reference_only"]:
        declared.extend(entry["group_ids"])

    assert len(declared) == len(set(declared)), f"중복 선언된 그룹: {declared}"
    assert set(declared) == {group["group_id"] for group in catalog["groups"]}


def test_wired_groups_name_their_consumer_and_switch(catalog: dict[str, Any]) -> None:
    """배선된 그룹은 소비자와 스위치를 밝힌다 — 기본값이 어디서 켜지는지 추적 가능해야 한다."""
    wired = catalog["consumption"]["wired"]
    assert wired, "배선 선언이 비면 이 래칫이 아무 것도 재지 않는다"
    for entry in wired:
        assert entry.get("consumer"), f"{entry['group_id']}: consumer 미기재"
        assert entry.get("stage"), f"{entry['group_id']}: stage 미기재"
        assert "AUDIENCE_QUALITATIVE_DEFAULTS" in entry.get("enabled_by", ""), (
            f"{entry['group_id']}: 스위치 이름이 선언과 다르다"
        )
