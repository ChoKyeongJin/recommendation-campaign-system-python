"""요청 범위의 되묻기 답변 — 경계에서 한 번 넣고, 깊은 계층이 꺼내 쓴다.

답변은 HTTP 요청의 사실이지 구조화기 계약의 일부가 아니다. 그래서 V4 플랜 스키마를 열지
않고, API 경계(:mod:`api`)가 :func:`clarification_scope` 로 한 번 감싸면 오디언스 확정
계층이 :func:`current_answers` 로 읽는다. 저장소가 이미 실행 기준일에 쓰고 있는 방식과 같다
(``graph_rag._EXECUTION_REFERENCE_DATE``).

:class:`~contextvars.ContextVar` 이므로 요청·태스크마다 격리된다 — 전역 mutable state 가
아니고(§30), 스코프를 벗어나면 반드시 원래 값으로 되돌아간다.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from resolution.clarification import ClarificationAnswer

_ANSWERS: ContextVar[tuple[ClarificationAnswer, ...]] = ContextVar(
    "resolution_clarification_answers", default=()
)


@contextmanager
def clarification_scope(
    answers: Sequence[ClarificationAnswer],
) -> Iterator[tuple[ClarificationAnswer, ...]]:
    """이 스코프 안에서 유효한 답변 목록을 설정한다."""

    resolved = tuple(answers)
    token = _ANSWERS.set(resolved)
    try:
        yield resolved
    finally:
        _ANSWERS.reset(token)


def current_answers() -> tuple[ClarificationAnswer, ...]:
    """지금 요청에 실려 온 답변들(없으면 빈 튜플)."""

    return _ANSWERS.get()


__all__ = ["clarification_scope", "current_answers"]
