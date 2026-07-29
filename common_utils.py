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


_DEFAULT_COMPACT_DROP_RE = re.compile(r"\s")


def compact_index_map(value: str, drop: "re.Pattern[str] | None" = None) -> tuple[str, list[int]]:
    """정규형 문자열 + ``compact 좌표 → 원문 좌표`` 대응표.

    표면어 매칭은 정규형(공백·구두점 제거)에서 하지만, "이 판정이 원문의 **어느 구간**을 읽었는가"는
    원문 좌표로만 답할 수 있다(소유권 판정이 그 좌표를 쓴다). 대응표가 그 되돌림이다.

    글자 단위 casefold 가 길이를 바꾸는 문자(라틴 확장 등)는 대응이 깨지므로 원문자를 유지한다 —
    이 프로젝트의 표면어는 한글/ASCII 라 실사용 결과는 :func:`compact` 계열과 동일하다.
    """
    pattern = drop if drop is not None else _DEFAULT_COMPACT_DROP_RE
    chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(str(value)):
        if pattern.match(char):
            continue
        folded = char.casefold()
        chars.append(folded if len(folded) == 1 else char)
        index_map.append(index)
    return "".join(chars), index_map


def raw_span(index_map: list[int], start: int | None, end: int | None) -> list[int] | None:
    """compact 좌표 ``[start, end)`` → 원문 좌표 ``[start, end)``. 범위 밖이거나 빈 구간이면 None."""
    if start is None or end is None or not index_map:
        return None
    start = max(0, min(int(start), len(index_map)))
    end = max(0, min(int(end), len(index_map)))
    if end <= start:
        return None
    return [index_map[start], index_map[end - 1] + 1]
