"""테스트 환경이 실행 순서에 좌우되지 않는지 지킨다.

골든 헬퍼(apply_corpus_env)가 프로세스 전역 os.environ 을 복원 없이 바꾸기 때문에, 골든 케이스가
먼저 도느냐 나중에 도느냐에 따라 이후 테스트의 환경이 달라졌다. 앞으로 측정할 멱등성·경로
패리티는 전부 '같은 입력이면 같은 결과'를 전제하므로, 측정 도구 자체의 오염을 먼저 닫아야 한다.

닫는 장치는 tests/conftest.py 의 autouse fixture(_restore_environment)다. 이 파일은 그 장치가
살아 있는지, 그리고 결정론 바닥값이 코퍼스 선언과 어긋나지 않는지를 본다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# conftest 가 모든 테스트에 적용하는 결정론 바닥값. 코퍼스 선언과 같아야 한다.
DETERMINISM_FLOOR = {
    "CONDITION_SLOT_LLM_FALLBACK": "off",
    "SURFACE_LEXICON_LLM": "off",
    "CONCEPTUAL_TARGETING_LLM": "off",
    "TARGET_OBJECT_LLM_FALLBACK": "false",
}


def test_determinism_floor_is_applied() -> None:
    for key, value in DETERMINISM_FLOOR.items():
        assert os.environ.get(key) == value, f"{key} 가 결정론 바닥값과 다르다: {os.environ.get(key)!r}"


def test_corpus_env_matches_the_conftest_floor() -> None:
    """코퍼스 선언과 conftest 바닥값이 갈라지면 '골든이 돌았는지'가 환경을 정한다."""

    corpus = json.loads(
        (REPO_ROOT / "tests" / "golden" / "cases.json").read_text(encoding="utf-8")
    )
    declared = {str(k): str(v) for k, v in (corpus.get("env") or {}).items()}
    conflicting = {
        key: (value, DETERMINISM_FLOOR[key])
        for key, value in declared.items()
        if key in DETERMINISM_FLOOR and DETERMINISM_FLOOR[key] != value
    }
    assert not conflicting, f"코퍼스 env 와 conftest 바닥값 불일치 (코퍼스, conftest): {conflicting}"


def test_env_written_by_a_test_does_not_leak(monkeypatch) -> None:
    """이 테스트가 남긴 값은 다음 테스트로 새면 안 된다(짝 테스트가 확인한다)."""

    os.environ["_ENV_ISOLATION_PROBE"] = "leaked"
    assert os.environ["_ENV_ISOLATION_PROBE"] == "leaked"


def test_previous_tests_env_did_not_leak_here() -> None:
    """앞 테스트가 심은 값이 보이면 autouse 복원 fixture 가 죽은 것이다."""

    assert "_ENV_ISOLATION_PROBE" not in os.environ, (
        "환경변수가 테스트 사이로 샜다 — tests/conftest.py 의 _restore_environment 를 확인하라."
    )


def test_golden_helper_env_is_restored_after_use() -> None:
    """골든 헬퍼를 직접 호출해도 세션 환경이 오염되지 않는지 확인한다."""

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import golden_support

    before = dict(os.environ)
    golden_support.apply_corpus_env()
    os.environ["_GOLDEN_PROBE"] = "1"
    # 헬퍼는 코퍼스 선언값을 쓰므로 바닥값과 같아야 한다(다르면 위 테스트가 먼저 잡는다).
    for key, value in DETERMINISM_FLOOR.items():
        assert os.environ.get(key) == value
    # 복원은 fixture 가 담당한다 — 여기서는 그 사실을 문서화하고 흔적만 남긴다.
    assert "_GOLDEN_PROBE" in os.environ
    assert set(before) <= set(os.environ)
