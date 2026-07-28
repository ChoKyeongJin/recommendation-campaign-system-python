"""어휘형 정규식 래칫 — 새 표현을 또 코드로 받는 것을 막는다.

이행의 방향은 하나다: **표면어 목록은 데이터로, 구조만 코드로.** 그 방향을 지키는 장치가 이 테스트다.
어휘형(리터럴 표면어 교대) 정규식 상수가 기준선을 넘으면 실패한다 — 기준선은 이행이 진행될수록
내려가고, 올리려면 사유와 함께 `tools/regex_inventory.py --set-baseline` 을 명시적으로 돌려야 한다.

관련: :mod:`lexicon_patterns` (어휘의 단일 소스), `docs/regex_inventory.md` (남은 작업 목록).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import regex_inventory

BASELINE_PATH = Path(__file__).resolve().parents[1] / "docs" / "data" / "regex_inventory_baseline.json"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return regex_inventory.scan()


def test_lexical_regex_count_does_not_regress(rows: list[dict]) -> None:
    if not BASELINE_PATH.exists():
        pytest.fail("어휘형 래칫 기준선이 없다. `python tools/regex_inventory.py --set-baseline` 로 생성하라.")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["counts"]
    current = regex_inventory.counts(rows)

    offenders = [f"{r['file']}:{r['line']} {r['name']}" for r in rows if r["class"] == "lexical"]
    assert current["lexical"] <= baseline["lexical"], (
        f"코드에 박힌 어휘형 정규식이 늘었다: {current['lexical']} > 기준선 {baseline['lexical']}.\n"
        f"새 표면어는 docs/data/parser_lexicon.json 의 어휘에 추가하고 lexicon_patterns.pattern(...) 을 쓴다.\n"
        f"현재 어휘형 목록:\n  " + "\n  ".join(offenders)
    )


def test_migrated_patterns_are_gone_from_the_inventory(rows: list[dict]) -> None:
    """이관한 상수가 다시 코드 정규식으로 되살아나지 않았는지."""
    import test_lexicon_patterns

    # 이관된 패턴이 쓰던 낱말 집합 그대로 다시 re.compile 된 상수가 있으면 되돌아간 것이다.
    migrated_term_sets = {
        frozenset(source.split("|")) for source in test_lexicon_patterns.MIGRATED_ORIGINALS.values()
    }
    revived = [
        f"{row['file']}:{row['line']} {row['name']}"
        for row in rows
        if row["pattern"] and frozenset(row["pattern"].split("|")) in migrated_term_sets
    ]
    assert not revived, f"이관된 어휘가 코드 정규식으로 되살아났다: {revived}"


def test_inventory_classes_are_closed(rows: list[dict]) -> None:
    assert {row["class"] for row in rows} <= set(regex_inventory.CLASS_ORDER)


def test_classifier_treats_particle_groups_as_structure() -> None:
    """조사 결합('(?:이|가)?')은 문법이다 — 어휘형으로 잘못 분류하면 통째로 데이터화하려다 깨진다."""
    assert regex_inventory.classify(r"구매|구입|주문")[0] == "lexical"
    assert regex_inventory.classify(r"구매(?:을|를)?")[0] == "domain"
    # 한글이 한 글자라도 섞이면(단위 '원' 등) 어휘 부분이 있다는 뜻이라 domain 이다.
    assert regex_inventory.classify(r"\d[\d,]*\s*원")[0] == "domain"
    assert regex_inventory.classify(r"\d{4}-\d{2}-\d{2}")[0] == "grammar"
