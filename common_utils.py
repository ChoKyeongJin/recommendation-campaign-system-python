"""여러 모듈이 공유하는 순수 유틸리티(리프 모듈 — 프로젝트 내부 모듈을 import 하지 않는다).

여기 있는 함수들은 원래 각 모듈에 바이트 동일하게 복제돼 있었다(예: _elapsed_ms 가 api.py·graph_rag.py 에,
_compact 가 set_expression_engine.py·formula_engine.py 에). 단일 소스로 모아 관례(반올림 자리수·공백 제거
규칙)가 한 곳에서만 정의되게 한다. 소비 모듈은 기존 내부 이름을 보존하려고 alias 로 import 한다
(예: `from common_utils import elapsed_ms as _elapsed_ms`).
"""

from __future__ import annotations

import re
import time


def elapsed_ms(started_at: float) -> float:
    """time.perf_counter() 기준 시각(started_at)부터 지금까지 경과 밀리초(소수 2자리)."""
    return round((time.perf_counter() - started_at) * 1000, 2)


def compact(value: str) -> str:
    """공백 제거 + casefold — 표면형 매칭 전 정규화(한글 문장의 공백 흔들림 흡수)."""
    return re.sub(r"\s+", "", value.casefold())
