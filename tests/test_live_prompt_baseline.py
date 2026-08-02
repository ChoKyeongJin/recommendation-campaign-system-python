"""라이브 회귀 러너의 분류 계약.

러너 자체는 API 를 호출하지만 **판정 로직은 순수 함수**다. 여기서 그 부분만 고정한다 —
숫자를 재는 도구가 조용히 다르게 세기 시작하면 기준선 분쟁이 다시 열린다(14/26·0/26·12/26
세 숫자가 공존했던 이유가 정확히 '도구가 저장소 밖에 있었다'였다).

지키는 것:
  1. 코퍼스는 형식이 온전하다(id 유일, expectation 은 닫힌 집합).
  2. 분류는 닫힌 5종이고 우선순위가 있다(SQL > 되묻기 > 미지원 > 실패).
  3. '정직한 미지원'은 회귀가 아니다 — 이 편향이 도구의 존재 이유다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import live_prompt_baseline as runner  # noqa: E402

CORPUS_PATH = REPO_ROOT / "docs" / "data" / "test_baselines" / "live_prompts.json"


def _corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def test_corpus_is_well_formed() -> None:
    corpus = _corpus()
    prompts = corpus["prompts"]
    assert len(prompts) >= 20, "코퍼스가 너무 작으면 회귀 신호가 되지 않는다."

    ids = [entry["id"] for entry in prompts]
    assert len(ids) == len(set(ids)), f"중복 id: {[i for i in ids if ids.count(i) > 1]}"

    allowed = set(corpus["expectations"])
    for entry in prompts:
        assert entry["text"].strip(), f"빈 프롬프트: id={entry['id']}"
        assert entry["expectation"] in allowed, (
            f"id={entry['id']} 의 expectation={entry['expectation']!r} 이 선언되지 않았다. "
            f"허용: {sorted(allowed)}"
        )


def test_expectations_are_documented() -> None:
    """expectation 값마다 '무엇을 뜻하는가'가 파일 안에 적혀 있어야 한다."""
    corpus = _corpus()
    for name, description in corpus["expectations"].items():
        assert description.strip(), f"expectation {name!r} 에 설명이 없다."


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"sql": "SELECT 1", "status": "success"}, "sql"),
        # SQL 이 있으면 다른 신호가 붙어 있어도 출고다.
        ({"sql": "SELECT 1", "status": "success", "unsupported_conditions": ["x"]}, "sql"),
        ({"status": "needs_clarification", "clarification_questions": ["브랜드를 지정해 주세요"]}, "clarification"),
        ({"status": "unsupported"}, "unsupported"),
        # 실측된 함정: 미지원 응답도 같은 문구를 clarification_questions 에 싣는다.
        # status 를 권위로 보지 않으면 정직한 미지원이 전부 '되묻기'로 오집계된다.
        (
            {
                "status": "unsupported",
                "failure_reason": "semantic_ir_unsupported",
                "clarification_questions": ["3개월 내내 VIP 는 표현할 수 없습니다"],
            },
            "unsupported",
        ),
        ({"status": "no_verified_sql", "failure_reason": "sql_guard_failed"}, "failure"),
        ({"status": "sql_validation_failed", "failure_reason": "boom"}, "failure"),
        # status 가 없는 응답만 보조 필드로 판정한다.
        ({"clarification_questions": ["q"]}, "clarification"),
        ({"unsupported_reason": "data_unavailable"}, "unsupported"),
        ({}, "failure"),
    ],
)
def test_classification_is_closed_and_ordered(response: dict, expected: str) -> None:
    assert runner.classify(response) == expected
    assert runner.classify(response) in runner.OUTCOMES


@pytest.mark.parametrize(
    ("expectation", "outcome", "verdict"),
    [
        ("sql", "sql", "match"),
        ("sql", "failure", "regression"),
        ("sql", "unsupported", "regression"),
        # 정직한 미지원이 기대치면 그대로 나오는 것이 정답이다.
        ("unsupported", "unsupported", "match"),
        ("unsupported", "failure", "regression"),
        # 미지원이던 것이 컴파일되기 시작하면 회귀가 아니라 개선이다(기대치 갱신 후보).
        ("unsupported", "sql", "improvement"),
        ("clarification", "clarification", "match"),
        ("clarification", "sql", "improvement"),
        ("unknown", "failure", "unknown"),
        ("sql", "error", "regression"),
    ],
)
def test_verdict_matrix(expectation: str, outcome: str, verdict: str) -> None:
    assert runner._verdict(expectation, outcome) == verdict


def test_unstable_emission_counts_as_the_worst_outcome() -> None:
    """3번 중 1번만 나오는 SQL 은 출고가 아니다 — 편차는 낙관이 아니라 비관으로 센다."""
    outcomes = ["sql", "failure", "sql"]
    worst = max(outcomes, key=runner.OUTCOMES.index)
    assert worst == "failure"


def test_summary_counts_every_row_exactly_once() -> None:
    rows = [
        {"id": 1, "outcome": "sql", "verdict": "match", "unstable": False},
        {"id": 2, "outcome": "unsupported", "verdict": "match", "unstable": False},
        {"id": 3, "outcome": "failure", "verdict": "regression", "unstable": True},
    ]
    summary = runner.summarize(rows)
    assert sum(summary["outcomes"].values()) == len(rows)
    assert sum(summary["verdicts"].values()) == len(rows)
    assert summary["regressions"] == [3]
    assert summary["unstable"] == [3]
