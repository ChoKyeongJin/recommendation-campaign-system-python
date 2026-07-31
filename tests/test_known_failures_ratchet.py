"""알려진 실패 대장이 부채 숨기는 도구가 되지 않게 지킨다.

대장(tests/known_failures.json)은 빨간 테스트를 strict xfail 로 통과시키는 장치다. 편리한 만큼
위험하다 — 사유 없이 한 줄 늘리면 그만이고, 그러면 CI 는 초록인데 부채는 계속 쌓인다.
그래서 대장에 세 가지를 강제한다: (1) 필수 필드, (2) 상한(하향 전용 래칫), (3) 살아 있는 nodeid.

(3)의 실시간 강제는 tests/conftest.py 의 수집 훅이 한다(대장 nodeid 가 수집되지 않으면 세션 중단).
여기서는 파일 자체의 형식과 개수만 본다 — 훅이 꺼지거나 바뀌어도 이 테스트는 남는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

LEDGER_PATH = Path(__file__).resolve().parent / "known_failures.json"

# 상한은 하향 전용이다. 올리려면 이 숫자와 함께 사유를 커밋 메시지에 남겨라.
# 2026-07-31 기준 1건: 80fe0a4 에서 red 로 커밋된 conceptual targeting 테스트.
MAX_KNOWN_FAILURES = 1

REQUIRED_FIELDS = ("nodeid", "reason", "found_at", "owner")


def _entries() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return raw.get("failures", []) if isinstance(raw, dict) else raw


def test_ledger_is_valid_json_with_a_failures_list() -> None:
    assert LEDGER_PATH.exists(), "대장 파일이 없다 — conftest 훅이 조용히 무동작이 된다."
    raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and isinstance(raw.get("failures"), list)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_entry_has_required_fields(field: str) -> None:
    """사유·발견일·담당이 없으면 대장은 무덤이 된다."""

    missing = [
        entry.get("nodeid", f"#{index}")
        for index, entry in enumerate(_entries())
        if not str(entry.get(field) or "").strip()
    ]
    assert not missing, f"{field} 누락: {missing}"


def test_reasons_are_substantive() -> None:
    """'TODO', '나중에' 같은 한 단어 사유를 막는다 — 다음 사람이 판단할 수 있어야 한다."""

    thin = [
        entry["nodeid"]
        for entry in _entries()
        if len(str(entry.get("reason", "")).strip()) < 40
    ]
    assert not thin, f"사유가 너무 짧다(40자 미만): {thin}"


def test_no_duplicate_nodeids() -> None:
    nodeids = [entry.get("nodeid") for entry in _entries()]
    assert len(nodeids) == len(set(nodeids)), "같은 nodeid 가 두 번 실렸다."


def test_count_does_not_regress() -> None:
    """대장은 늘어나는 방향으로 쓰라고 있는 게 아니다."""

    count = len(_entries())
    assert count <= MAX_KNOWN_FAILURES, (
        f"알려진 실패가 {count}건으로 상한 {MAX_KNOWN_FAILURES} 을 넘었다. "
        "실패를 대장에 밀어 넣는 대신 고쳐라. 정말 올려야 한다면 "
        "MAX_KNOWN_FAILURES 를 사유와 함께 올려라."
    )


def test_golden_snapshots_are_not_in_the_ledger() -> None:
    """골든 실패의 정답은 재생성이지 대장 등재가 아니다(등재하면 회귀 감지를 통째로 끈다)."""

    golden = [
        entry["nodeid"]
        for entry in _entries()
        if "test_ir_golden_corpus" in str(entry.get("nodeid", ""))
    ]
    assert not golden, (
        f"골든 스냅샷 실패가 대장에 있다: {golden}. "
        "tools/regen_ir_goldens.py 로 재생성하고 diff 를 검토해 커밋하라."
    )
