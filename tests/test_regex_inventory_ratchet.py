"""코드 규칙 래칫 — 새 표현을 또 코드로 받는 것을 막는다.

이행의 방향은 하나다: **표면어 목록은 데이터로, 구조만 코드로.** 그 방향을 지키는 장치가 이 테스트다.
래칫 대상 분류(``lexical``·``wordlist``·``domain``)의 개수가 기준선을 넘으면 실패한다 — 기준선은
이행이 진행될수록 내려가고, 올리려면 **사유와 함께** `tools/regex_inventory.py --set-baseline
--reason "..."` 을 명시적으로 돌려야 한다(사유 없는 상향은 `write_baseline` 이 거부한다).

이 파일이 지키는 것이 하나 더 있다: **스캔 범위**. 래칫은 세는 것만 막을 수 있어서, 규칙이 세지지
않는 형태로 들어오면(튜플에 담긴 정규식·이름 없는 인라인 정규식·한글 낱말집합 상수) 상한이 있어도
조용히 늘어난다. 실제로 그 구멍에 규칙이 쌓였다 — 아래 커버리지 테스트가 구멍이 다시 열리는 것을
막는다.

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


# ── 1. 래칫 ────────────────────────────────────────────────────────────────────────────────
def test_ratcheted_rule_counts_do_not_regress(rows: list[dict]) -> None:
    """어휘형·낱말집합·업무의미형이 기준선을 넘지 않는다(문법형은 구조라 상한 없음)."""
    if not BASELINE_PATH.exists():
        pytest.fail("래칫 기준선이 없다. `python tools/regex_inventory.py --set-baseline` 로 생성하라.")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["counts"]
    current = regex_inventory.counts(rows)
    breaches = regex_inventory.regressions(current, baseline)
    if not breaches:
        return

    detail = []
    for kind in regex_inventory.RATCHETED_CLASSES:
        if any(breach.startswith(f"{kind}:") for breach in breaches):
            detail.append(f"[{kind}]")
            detail.extend(
                f"  {row['file']}:{row['line']} {row['name']} ({row.get('source')})"
                for row in rows
                if row["class"] == kind
            )
    pytest.fail(
        "코드에 박힌 규칙이 늘었다: " + "; ".join(breaches) + ".\n"
        "새 표면어는 docs/data/parser_lexicon.json 의 어휘에 추가하고 lexicon_patterns.pattern(...) 을 쓴다.\n"
        "상한을 올려야 하는 정당한 사유가 있으면 tools/regex_inventory.py --set-baseline --reason \"...\" 로 남긴다.\n"
        + "\n".join(detail)
    )


def test_baseline_records_its_ratcheted_classes() -> None:
    """기준선 파일이 어떤 분류를 막고 있는지 스스로 밝힌다(래칫 대상이 조용히 줄어드는 것 방지)."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert set(baseline.get("ratcheted", [])) == set(regex_inventory.RATCHETED_CLASSES)
    for kind in regex_inventory.RATCHETED_CLASSES:
        assert kind in baseline["counts"], f"기준선에 {kind} 상한이 없다"


def test_raising_the_baseline_requires_a_reason(tmp_path: Path) -> None:
    """사유 없는 상향은 거부된다 — '올리려면 사유가 필요하다'를 문서가 아니라 코드로 만든다."""
    path = tmp_path / "baseline.json"
    regex_inventory.write_baseline({"lexical": 1, "wordlist": 1, "domain": 1, "grammar": 9}, path=path)

    raised = {"lexical": 2, "wordlist": 1, "domain": 1, "grammar": 9}
    with pytest.raises(ValueError, match="사유가 필요하다"):
        regex_inventory.write_baseline(raised, path=path)

    payload = regex_inventory.write_baseline(raised, reason="스캔 범위 확대", path=path)
    assert payload["counts"]["lexical"] == 2
    assert payload["raise_reason"] == "스캔 범위 확대"

    # 내리는 것은 사유 없이 자유(이행이 진행된 결과다).
    lowered = regex_inventory.write_baseline({"lexical": 1, "wordlist": 1, "domain": 1, "grammar": 9}, path=path)
    assert lowered["counts"]["lexical"] == 1
    # 문법형은 래칫 대상이 아니라 올라도 사유를 요구하지 않는다.
    regex_inventory.write_baseline({"lexical": 1, "wordlist": 1, "domain": 1, "grammar": 99}, path=path)


# ── 2. 스캔 범위(사각지대) ───────────────────────────────────────────────────────────────
def _scan_source(tmp_path: Path, source: str) -> list[dict]:
    (tmp_path / "sample_module.py").write_text(source, encoding="utf-8")
    return regex_inventory.scan(root=tmp_path)


def test_scanner_sees_patterns_hidden_in_collections(tmp_path: Path) -> None:
    """튜플·딕트에 담긴 정규식도 센다 — 상수 하나를 튜플로 감싸 래칫을 피하지 못하게."""
    rows = _scan_source(tmp_path, 'import re\n_RULES = (re.compile("같은상품"), re.compile("동일상품"))\n')
    names = {row["name"] for row in rows}
    assert names == {"_RULES[0]", "_RULES[1]"}
    assert {row["source"] for row in rows} == {"collection"}


def test_scanner_sees_inline_korean_regexes(tmp_path: Path) -> None:
    """이름 없이 호출되는 한글 정규식도 센다. 한글 없는 구조 정규식은 세지 않는다."""
    rows = _scan_source(
        tmp_path,
        'import re\n'
        'def f(text):\n'
        '    if re.search("정기배송|픽업", text):\n'
        '        return re.sub(r"\\s+", "", text)\n'
        '    return None\n',
    )
    assert [(row["name"], row["class"]) for row in rows] == [("<inline re.search>", "lexical")]


def test_scanner_sees_korean_wordlist_constants(tmp_path: Path) -> None:
    """한글 낱말집합 상수도 센다(정규식이 아니어도 같은 두더지다). 딕트는 그룹별로 센다."""
    rows = _scan_source(
        tmp_path,
        'import re\n'
        '_GENERIC_NOUNS = frozenset({"상품", "제품", "품목"})\n'
        '_LEXICON = {"cart_terms": ["장바구니", "담은"], "n": 1}\n'
        '_ONE = {"상품"}\n',
    )
    by_name = {row["name"]: row for row in rows}
    assert by_name["_GENERIC_NOUNS"]["class"] == "wordlist"
    assert by_name["_GENERIC_NOUNS"]["alternatives"] == 3
    assert '_LEXICON["cart_terms"]' in by_name
    assert "_ONE" not in by_name, "낱말 1개는 목록이 아니다"


def test_lexicon_composed_patterns_are_not_counted_as_domain(tmp_path: Path) -> None:
    """사전 어휘로 조립한 구조는 이행 **완료** 형태다 — domain 으로 세면 이관이 지표상 손해가 된다."""
    rows = _scan_source(
        tmp_path,
        'import re\n'
        'import lexicon_patterns\n'
        '_SAME = lexicon_patterns.alternation("identity_same")\n'
        '_COMPOSED = re.compile(rf"(?:{_SAME})\\s*(?:상품)")\n'
        '_HANDWRITTEN = re.compile(r"동일\\s*상품|같은\\s*상품")\n',
    )
    by_name = {row["name"]: row["class"] for row in rows}
    assert by_name["_COMPOSED"] == "composed"
    assert by_name["_HANDWRITTEN"] == "domain"
    assert "composed" not in regex_inventory.RATCHETED_CLASSES


def test_scanner_ignores_prompt_text_lists(tmp_path: Path) -> None:
    """LLM 프롬프트 문구를 조립하는 지역 리스트는 규칙이 아니다 — 문구 수정이 래칫을 깨면 안 된다."""
    rows = _scan_source(
        tmp_path,
        'def prompt():\n'
        '    action_terms = ["발송해줘", "보내줘"]\n'
        '    prompt_lines = ["다음 조건을 만족하는", "회원을 찾아라"]\n'
        '    return action_terms + prompt_lines\n',
    )
    assert rows == []


def test_wordlist_rows_are_not_double_counted_with_patterns(tmp_path: Path) -> None:
    """정규식 묶음으로 센 상수를 낱말집합으로 또 세지 않는다."""
    rows = _scan_source(tmp_path, 'import re\n_RULES = (re.compile("상품"), "제품")\n')
    assert {row["class"] for row in rows} == {"lexical"}
    assert all(row["source"] == "collection" for row in rows)


# ── 3. 분류 계약 ────────────────────────────────────────────────────────────────────────
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


def test_every_row_declares_how_the_rule_is_embedded(rows: list[dict]) -> None:
    """``source`` 는 규칙이 코드에 담긴 형태다 — 사각지대 회귀를 사람이 읽을 수 있게 한다."""
    assert {row["source"] for row in rows} <= {"constant", "collection", "inline", "wordlist"}


def test_classifier_treats_particle_groups_as_structure() -> None:
    """조사 결합('(?:이|가)?')은 문법이다 — 어휘형으로 잘못 분류하면 통째로 데이터화하려다 깨진다."""
    assert regex_inventory.classify(r"구매|구입|주문")[0] == "lexical"
    assert regex_inventory.classify(r"구매(?:을|를)?")[0] == "domain"
    # 한글이 한 글자라도 섞이면(단위 '원' 등) 어휘 부분이 있다는 뜻이라 domain 이다.
    assert regex_inventory.classify(r"\d[\d,]*\s*원")[0] == "domain"
    assert regex_inventory.classify(r"\d{4}-\d{2}-\d{2}")[0] == "grammar"
