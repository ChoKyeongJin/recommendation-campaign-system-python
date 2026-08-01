"""골든 코퍼스 로더 — 테스트와 재생성 스크립트가 같은 규칙으로 케이스를 읽게 한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
CASES_PATH = GOLDEN_DIR / "cases.json"


def load_corpus() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def load_cases() -> list[dict[str, Any]]:
    return load_corpus()["cases"]






def build_plan(prompt: str, parser: str = "rules") -> dict[str, Any]:
    """골든 경로의 플랜 생성. 코퍼스가 선언한 환경에서 결정론적으로 실행된다."""
    import graph_rag

    return graph_rag.build_query_plan(prompt, parser=parser)
