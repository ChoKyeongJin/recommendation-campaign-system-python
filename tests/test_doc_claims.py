"""소스가 존재하지 않는 테스트·설정 파일을 계약 근거로 인용하지 못하게 한다.

2026-07-28~29 에 테스트 113개가 의도적으로 삭제됐는데(213741e, ce39f68) 소스 주석은 그대로
남아 "테스트가 강제한다", "tests/test_x.py 가 parity 검증" 같은 문장으로 없는 안전망을 광고했다.
그 결과 다음 사람은 지켜지지 않는 불변식을 지켜지는 것으로 읽는다 — 주석이 거짓말을 하면
없는 것보다 나쁘다.

같은 사고가 설정 JSON 에서도 두 번 재발했다: ac924ff 가 condition_ownership_policy.json 을
지웠는데 docstring·운영 문서가 계속 그 파일을 정책 권위로 인용했다. 그래서 docs/data/**.json
인용도 같은 규칙으로 검사한다(두 번째 검사 축). 단 이 축은 **.py 소스만** 스캔한다 —
운영·가이드 md 의 스테일 참조는 정리하지 않기로 한 기존 결정(운영문서는 사용 근거가 아니다)을
따르고, 코드가 존재하지 않는 파일을 런타임 기본 경로나 계약 근거로 갖는 것만 막는다.

**정책**: 삭제된 파일을 *역사로* 언급하는 것은 허용한다("213741e 에서 삭제 — 현재 가드 없음").
금지되는 것은 *현재형 주장*이다("테스트가 강제한다"). 그래서 인용된 파일이 실재하지 않으면
같은 줄 주변에 부재를 밝히는 표지(없음/삭제/부재/TODO/미복원)가 있어야 통과시킨다.

plans_*.md 계획서는 앞으로 만들 테스트·설정을 설계하므로 제외한다(미래형 서술).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

CITATION_RE = re.compile(r"tests/(test_[a-z0-9_]+\.py)")

# 부재를 밝히는 표지. 인용 줄 자체 또는 앞뒤 2줄 안에 하나라도 있으면 '역사 언급'으로 본다.
ABSENCE_MARKERS = ("없음", "없다", "삭제", "부재", "TODO", "미복원", "복원하지")

# 계획·설계 문서는 아직 없는 테스트를 설계하므로 제외한다.
EXCLUDED_FILES = {"plans_ir_decoupling.md", "plans_canonical_ir_capability.md"}

SCAN_SUFFIXES = (".py", ".md")
SKIP_DIR_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def _live_test_files() -> set[str]:
    return {path.name for path in TESTS_DIR.glob("test_*.py")}


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if path.suffix not in SCAN_SUFFIXES or not path.is_file():
            continue
        if SKIP_DIR_PARTS & set(path.parts):
            continue
        if path.name in EXCLUDED_FILES:
            continue
        files.append(path)
    return files


def _false_claims() -> list[str]:
    live = _live_test_files()
    offenders: list[str] = []
    for path in _scanned_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for index, line in enumerate(lines):
            for match in CITATION_RE.finditer(line):
                cited = match.group(1)
                if cited in live:
                    continue
                context = "\n".join(lines[max(0, index - 2) : index + 3])
                if any(marker in context for marker in ABSENCE_MARKERS):
                    continue  # 부재를 밝힌 역사 언급 — 허용
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{index + 1} -> tests/{cited}"
                )
    return offenders


# docs/data 설정 인용. 글롭('*')은 문자 클래스 밖이라 매칭되지 않는다(docs/data/metrics/*.json 허용).
JSON_CITATION_RE = re.compile(r"docs/data/[A-Za-z0-9_\-/.]+?\.json")


def _false_json_claims() -> list[str]:
    offenders: list[str] = []
    for path in _scanned_files():
        if path.suffix != ".py":
            continue  # 운영·가이드 md 의 스테일 참조는 이 축의 대상이 아니다(docstring 참조).
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for index, line in enumerate(lines):
            for match in JSON_CITATION_RE.finditer(line):
                cited = match.group(0)
                if (REPO_ROOT / cited).is_file():
                    continue
                context = "\n".join(lines[max(0, index - 2) : index + 3])
                if any(marker in context for marker in ABSENCE_MARKERS):
                    continue  # 부재를 밝힌 역사 언급 — 허용
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{index + 1} -> {cited}"
                )
    return offenders


def test_no_source_claims_a_missing_test_as_a_guard() -> None:
    """없는 테스트를 근거로 든 문장은 거짓 안전감이다."""

    offenders = _false_claims()
    assert not offenders, (
        "존재하지 않는 테스트를 계약 근거로 인용하는 지점:\n  "
        + "\n  ".join(offenders)
        + "\n\n테스트를 만들거나, 인용을 지우거나, 부재를 밝히는 표지"
        + f"({'/'.join(ABSENCE_MARKERS)})를 함께 적어라."
    )


def test_no_source_claims_a_missing_config_json_as_authority() -> None:
    """삭제된 설정 JSON 을 계속 권위로 인용하는 문장은 거짓 안전감이다(ac924ff 재발 방지)."""

    offenders = _false_json_claims()
    assert not offenders, (
        "존재하지 않는 docs/data JSON 을 인용하는 지점:\n  "
        + "\n  ".join(offenders)
        + "\n\n파일을 만들거나, 인용을 지우거나, 부재를 밝히는 표지"
        + f"({'/'.join(ABSENCE_MARKERS)})를 함께 적어라."
    )


def test_json_scanner_detects_a_planted_false_claim() -> None:
    """JSON 인용 탐지력 자체를 검증한다."""

    missing = "docs/data/" + "definitely_not_a_real_config.json"
    line = "# 정책은 " + missing + " 이 소유한다."

    assert not (REPO_ROOT / missing).is_file()
    assert JSON_CITATION_RE.search(line), "인용 패턴이 이 형태를 잡지 못한다."
    glob_line = "docs/data/metrics/*.json 레지스트리"
    matched = JSON_CITATION_RE.search(glob_line)
    assert matched is None or "*" not in matched.group(0), "글롭 표기가 오탐된다."


def test_scanner_actually_scans_something() -> None:
    """공허한 통과 방지 — 스캔 대상이 0건이면 green 은 의미가 없다."""

    files = _scanned_files()
    assert len(files) > 100, f"스캔 파일이 너무 적다: {len(files)}"
    assert _live_test_files(), "tests/ 에서 테스트 파일을 하나도 찾지 못했다."


def test_scanner_detects_a_planted_false_claim(tmp_path: Path) -> None:
    """탐지력 자체를 검증한다(이 가드가 조용히 무력해지는 것을 막는다)."""

    # 인용 문자열을 소스에 리터럴로 두면 스캐너가 이 파일 자체를 위반으로 잡는다(실제로 밟았다).
    # 조립해서 만든다 — 그래야 가드가 자기 자신을 물지 않는다.
    missing_name = "test_" + "definitely_not_a_real_file.py"
    line = "# 이 불변식은 " + "tests/" + missing_name + " 가 강제한다."

    assert missing_name not in _live_test_files()
    assert CITATION_RE.search(line), "인용 패턴이 이 형태를 잡지 못한다."
    assert not any(marker in line for marker in ABSENCE_MARKERS), (
        "부재 표지 없는 현재형 주장인데 예외로 통과된다."
    )
