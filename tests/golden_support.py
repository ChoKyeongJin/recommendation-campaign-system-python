"""골든 코퍼스 로더 — 테스트와 재생성 스크립트가 같은 규칙으로 케이스를 읽게 한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CASES_PATH = GOLDEN_DIR / "cases.json"
SNAPSHOT_DIR = GOLDEN_DIR / "snapshots"
BASELINE_PATH = GOLDEN_DIR / "method_mix_baseline.json"


def load_corpus() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def load_cases() -> list[dict[str, Any]]:
    return load_corpus()["cases"]


def snapshot_path(case_id: str) -> Path:
    return SNAPSHOT_DIR / f"{case_id}.json"


def load_snapshot(case_id: str) -> dict[str, Any] | None:
    path = snapshot_path(case_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_snapshot(case_id: str, snapshot: dict[str, Any]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path(case_id).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def apply_corpus_env() -> dict[str, str]:
    """코퍼스가 선언한 환경을 적용한다(호출 시점의 프로세스 전역 — 골든 전용 실행에서만 쓴다).

    ``parser="rules"`` 는 이름과 달리 완전한 오프라인 경로가 아니다. 상품명 추출은 LLM 후보
    (``llm_object_fallback``)로 병합되므로, 끄지 않으면 골든이 네트워크와 모델 출력에 의존한다.
    코퍼스는 자기가 어떤 환경에서 유효한지 파일 안에 적어 두고, 테스트와 재생성이 같은 환경을 쓴다.
    """
    import os

    env = load_corpus().get("env") or {}
    for key, value in env.items():
        os.environ[str(key)] = str(value)
    return {str(k): str(v) for k, v in env.items()}


def build_plan(prompt: str, parser: str = "rules") -> dict[str, Any]:
    """골든 경로의 플랜 생성. 코퍼스가 선언한 환경에서 결정론적으로 실행된다."""
    apply_corpus_env()
    import graph_rag

    return graph_rag.build_query_plan(prompt, parser=parser)
