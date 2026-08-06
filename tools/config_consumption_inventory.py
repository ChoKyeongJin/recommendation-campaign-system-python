"""설정에 선언됐는데 코드가 읽지 않는 키를 기계로 센다(로드 ≠ 소비).

배경
----
이 저장소의 반복 결함은 "선언은 맞았는데 배선이 없다"이다. 로더가 파일 전체를 읽어도 그 키를
**이름으로 꺼내는 코드**가 없으면 선언은 죽어 있고, 증상은 예외가 아니라 침묵이다 — 설정 한 줄을
고친 사람은 아무 일도 일어나지 않는 것을 보게 된다. 카탈로그 별칭 3종이 통째로 소비 불가였던
사례(NOTES 2026-08-03 §"관통하는 발견")가 같은 모양이었다.

무엇을 세나
----------
대상은 **키 이름이 구조(스키마)인 설정** 둘로 한정한다:

* ``docs/data/runtime/sql/member_target_filters.json``
* ``docs/data/runtime/sql/metrics/*.json``

카탈로그류(``audience_catalog.json`` 등)는 키가 곧 데이터 id 라 "이름으로 안 불린다"가 결함이
아니므로 범위 밖이다. 같은 이유로 아래는 후보에서 뺀다:

* ``_``/``$`` 로 시작하는 주석·메타 키
* 값이 전부 dict 인 부모의 키(레지스트리 항목 id — 부모를 순회해 소비한다)
* 비ASCII 키(한국어 표면어는 데이터다 — 코드가 이름으로 부를 수 없다)

판정은 "프로덕션 소스(.py, tests 제외)에 그 키가 **따옴표로 묶여** 등장하는가" 하나다. 간접 참조
(``message_key`` 처럼 다른 설정이 이름을 들고 있는 경우)는 이 판정을 통과하지 못하므로, 기준선의
``reason`` 이 그 사정을 적는다. 도구는 사정을 판단하지 않고 사실만 센다.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SQL = REPO_ROOT / "docs" / "data" / "runtime" / "sql"
MEMBER_TARGET_FILTERS = RUNTIME_SQL / "member_target_filters.json"
METRIC_SPEC_DIR = RUNTIME_SQL / "metrics"

_SKIP_SOURCE_DIRS = {
    ".git", "artifacts", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "tests",
}


@functools.lru_cache(maxsize=1)
def _production_sources() -> str:
    """프로덕션 소스 전문(한 프로세스 안에서 재사용 — 400여 파일 재독을 반복하지 않는다)."""
    chunks: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if parts & _SKIP_SOURCE_DIRS:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def _is_registry(node: dict[str, Any]) -> bool:
    """값이 전부 dict 인 매핑은 '항목 id → 스펙' 레지스트리로 본다(키가 데이터 id)."""
    values = [value for key, value in node.items() if not str(key).startswith(("_", "$"))]
    return len(values) >= 2 and all(isinstance(value, dict) for value in values)


def _structural_keys(node: Any, prefix: str, out: list[str]) -> None:
    if isinstance(node, dict):
        registry = _is_registry(node)
        for key, value in node.items():
            text = str(key)
            child = f"{prefix}.{text}" if prefix else text
            skip = (
                text.startswith(("_", "$"))
                or registry
                or not text.isascii()
            )
            if not skip:
                out.append(child)
            _structural_keys(value, child, out)
    elif isinstance(node, list):
        for item in node:
            # 리스트 항목은 같은 스키마의 반복이므로 경로에 인덱스를 넣지 않는다(결정론 유지).
            _structural_keys(item, prefix, out)


def _config_files() -> list[Path]:
    files = [MEMBER_TARGET_FILTERS]
    files.extend(sorted(METRIC_SPEC_DIR.glob("*.json")))
    return [path for path in files if path.exists()]


def scan() -> dict[str, Any]:
    """{"unconsumed": [키 경로…], "total": n} — 프로덕션 소스가 이름으로 읽지 않는 키."""
    unconsumed = _scan_cached()
    return {"unconsumed": list(unconsumed), "total": len(unconsumed)}


@functools.lru_cache(maxsize=1)
def _scan_cached() -> tuple[str, ...]:
    sources = _production_sources()
    referenced: dict[str, bool] = {}

    def is_referenced(key: str) -> bool:
        if key not in referenced:
            referenced[key] = bool(re.search(rf"""["']{re.escape(key)}["']""", sources))
        return referenced[key]

    unconsumed: set[str] = set()
    for path in _config_files():
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys: list[str] = []
        _structural_keys(payload, "", keys)
        relative = path.relative_to(REPO_ROOT).as_posix()
        for key_path in keys:
            leaf = key_path.rsplit(".", 1)[-1]
            if not is_referenced(leaf):
                unconsumed.add(f"{relative}::{key_path}")

    return tuple(sorted(unconsumed))


def main() -> None:
    print(json.dumps(scan(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
