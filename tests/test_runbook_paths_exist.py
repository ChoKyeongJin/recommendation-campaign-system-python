"""운영 문서가 **없는 것**을 현재형으로 안내하지 못하게 한다.

이 저장소의 알려진 재발 사고 모드다. 실측(2026-08-04) `docs/migration_runbook.md` 는 아래를
**주간 루틴의 첫 명령**으로 안내하고 있었다. 전부 저장소에 없다 — 삭제됐거나 만들어진 적이 없다.

- 도구 셋: `tools/weekly_triage.py` · `tools/regen_ir_goldens.py` · `tools/regex_inventory.py` (전부 삭제)
- 파일 넷: `logs/unresolved.jsonl` · `docs/weekly_triage.md` · `docs/data/slot_policy.json` ·
  `docs/data/regex_inventory_baseline.json` — 넷 다 없다(삭제됨)
- env 여섯: `UNRESOLVED_LOG` · `PARSER_SHADOW_MODE` · `PARSER_SHADOW_LOG` · `SLOT_POLICY_PATH` ·
  `TARGET_OBJECT_LLM_FALLBACK` · `CONDITION_SLOT_LLM_FALLBACK` — 읽는 코드가 없다

운영자가 그 명령을 붙여넣으면 아무 일도 안 나고, 그러면 루프 자체가 돌지 않는데 문서는
"돌고 있다"고 말한다.

산문은 사람만 읽는다. 이 파일이 그 산문을 기계가 읽게 만든다.

**삭제된 장치를 언급하는 것 자체는 막지 않는다** — "무엇이 없어졌는지"는 남겨야 할 정보다.
막는 것은 없는 것을 **경로/명령으로** 적는 것이다. 그래서 스캐너는 백틱 안의 경로 모양만 본다.
"철거된 장치" 절은 백틱 안에 확장자 없는 이름만 두는 관례로 이 검사를 통과한다.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 이 검사를 받는 문서. 늘리는 것은 언제나 옳다.
AUDITED_DOCS: tuple[str, ...] = (
    "docs/migration_runbook.md",
    "docs/operations/failure_diagnosis.md",
)

# 백틱 안에서 "저장소 경로"로 읽히는 것: 슬래시가 있고, 파일 확장자로 끝나거나 슬래시로 끝난다.
_PATH_IN_BACKTICKS = re.compile(
    r"`([A-Za-z0-9_][A-Za-z0-9_./\-]*(?:/[A-Za-z0-9_.\-]*)+)`"
)
_FILE_SUFFIXES = (".py", ".json", ".md", ".txt", ".yml", ".yaml", ".jsonl", ".sql")

# 경로 모양이지만 저장소 파일이 아닌 것들. 사유 없이 늘리지 마라 — 이 목록이 곧 가드의 구멍이다.
_NOT_REPO_PATHS = frozenset({
    "app/api/targeting/route.ts",  # 형제 레포(BFF)
})


def _candidate_paths(text: str) -> set[str]:
    found: set[str] = set()
    for raw in _PATH_IN_BACKTICKS.findall(text):
        if any(char in raw for char in "<>*?"):
            continue  # logs/rag_llm/<날짜>/ 같은 자리표시자
        if raw in _NOT_REPO_PATHS:
            continue
        if raw.endswith("/") or raw.endswith(_FILE_SUFFIXES):
            found.add(raw.rstrip("/") if raw.endswith("/") else raw)
    return found


@pytest.mark.parametrize("doc", AUDITED_DOCS)
def test_every_path_the_document_names_actually_exists(doc: str) -> None:
    path = REPO_ROOT / doc
    assert path.exists(), f"감사 대상 문서 자체가 없다: {doc}"

    candidates = _candidate_paths(path.read_text(encoding="utf-8"))
    assert candidates, f"{doc} 에서 경로를 하나도 못 찾았다 — 스캐너가 공허하다."

    missing = sorted(name for name in candidates if not (REPO_ROOT / name).exists())
    assert not missing, (
        f"{doc} 가 실재하지 않는 경로를 안내한다:\n  " + "\n  ".join(missing)
        + "\n\n지워졌다면 문서에서 빼거나, 확장자 없는 이름으로 '철거됨' 절에 적어라 — "
        "명령으로 붙여넣을 수 있는 모양이면 운영자가 실행하고 아무 일도 안 난다."
    )


# 실측으로 확인한 죽은 스위치. 되살아나면(코드가 다시 읽으면) 이 목록에서 지운다.
DEAD_ENV_SWITCHES: tuple[str, ...] = (
    "UNRESOLVED_LOG",
    "PARSER_SHADOW_MODE",
    "PARSER_SHADOW_LOG",
    "SLOT_POLICY_PATH",
    "TARGET_OBJECT_LLM_FALLBACK",
    "CONDITION_SLOT_LLM_FALLBACK",
)


@functools.lru_cache(maxsize=1)
def _runtime_sources() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in REPO_ROOT.rglob("*.py")
        if ".venv" not in p.parts and "tests" not in p.parts and "__pycache__" not in p.parts
    )


@pytest.mark.parametrize("name", DEAD_ENV_SWITCHES)
def test_dead_switches_are_still_dead_or_the_list_is_wrong(name: str) -> None:
    """목록이 스테일이 되는 방향도 막는다 — 되살아난 스위치를 '죽었다'고 적으면 같은 거짓말이다."""
    assert name not in _runtime_sources(), (
        f"{name} 을 코드가 다시 읽는다 — DEAD_ENV_SWITCHES 에서 빼고 runbook 표에 되돌려라."
    )


def test_runbook_does_not_advertise_a_dead_switch_as_a_control() -> None:
    """끄고 켜도 아무 일이 안 생기는 스위치는 안내가 아니라 함정이다.

    언급 자체는 허용한다(무엇이 죽었는지는 남겨야 할 정보다). 막는 것은 **표의 행**으로 적는
    것이다 — 표는 "이 값을 바꾸면 동작이 바뀐다"는 뜻이다.
    """
    text = (REPO_ROOT / "docs" / "migration_runbook.md").read_text(encoding="utf-8")
    offenders = [
        name for name in DEAD_ENV_SWITCHES if re.search(rf"^\|\s*`{name}`\s*\|", text, re.M)
    ]
    assert not offenders, (
        "환경 변수 표에 죽은 스위치가 있다:\n  " + "\n  ".join(offenders)
    )
