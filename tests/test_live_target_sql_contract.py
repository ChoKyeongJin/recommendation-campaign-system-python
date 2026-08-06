"""실제 HTTP `/target-sql` 경로의 종단 계약 — fixture 가 못 보는 사각지대를 잰다.

내부 테스트가 전부 통과하는데 전수 실행은 회귀를 냈다(2026-08-06). 이유는 단순하다 — fixture
테스트는 **플랜을 손으로 만들어** 컴파일러에 넣고, 실행 경로는 구조화기(LLM)·정책·재시도·
검증 게이트를 모두 통과한다. 그 사이의 배선은 fixture 로 덮이지 않는다.

이 파일은 그 배선만 본다. API 가 떠 있지 않으면 **건너뛴다**(없는 서버를 실패로 세지 않는다):

    LIVE_TARGET_SQL_BASE_URL=http://localhost:8000 python -m pytest tests/test_live_target_sql_contract.py -q

메타모픽 검사(조건을 더하거나 빼면 무엇이 달라져야 하는가)는 LLM 방출에 좌우되지 않는 축만
고른다 — 여기서 검증하는 것은 '어떤 SQL 이 나왔나'가 아니라 '의미 계약이 유지되는가'다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE_URL = os.getenv("LIVE_TARGET_SQL_BASE_URL", "").strip()
TIMEOUT = int(os.getenv("LIVE_TARGET_SQL_TIMEOUT", "240"))

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="LIVE_TARGET_SQL_BASE_URL 이 없다 — 종단 계약은 실제 서버가 있을 때만 잰다",
)


def call(prompt: str) -> dict:
    payload = json.dumps(
        {
            "prompt": prompt,
            "query_parser": "auto",
            "execute_sql": False,
            "persist_targeting": False,
            "generate_messages": False,
            "include_debug": True,
            "diagnostic_run_id": "pytest-live-contract",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/target-sql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"API 에 닿지 못했다: {exc}")


def test_every_response_carries_its_policy_version() -> None:
    """정책 버전이 없으면 코퍼스 기대와 런타임 판정을 대조할 수 없다(§4)."""

    response = call("최근 30일 구매한 회원")
    assert response.get("policy_version"), response.get("failure_reason")


def test_a_shipped_sql_never_leaves_an_open_requirement() -> None:
    """부분 SQL 출고 0건 — 미해결 요구가 있으면 SQL 은 나오지 않는다(§1)."""

    response = call("최근 30일 구매한 회원")
    if not response.get("sql"):
        pytest.skip(f"이 실행은 SQL 을 내지 않았다: {response.get('failure_reason')}")
    gate = response.get("coverage_gate") or {}
    assert gate.get("ran") is True
    counts = ((gate.get("ledger") or {}).get("counts")) or {}
    assert counts.get("open", 0) == 0, gate.get("verdict")
    assert (gate.get("verdict") or {}).get("is_shippable") is True


def test_a_count_request_produces_a_scalar_not_a_member_list() -> None:
    """결과 형태는 canonical 계약이다 — 회원 목록은 '회원 수' 질문의 답이 아니다(§2)."""

    response = call("최근 30일 구매한 회원 수를 알려줘")
    shape = response.get("result_shape") or {}
    assert shape.get("kind") == "scalar", response.get("failure_reason")
    sql = response.get("sql") or response.get("blocked_sql") or ""
    assert "COUNT(" in sql.upper(), sql


def test_a_stated_duration_never_reports_a_missing_period() -> None:
    """원문이 기간을 말했으면 그 신고는 원문과 모순이다(§3)."""

    response = call("최근 30일 구매한 회원 수를 알려줘")
    unresolved = (response.get("debug") or {}).get("unresolved_source_conditions") or []
    period_gaps = [
        item
        for item in unresolved
        if item.get("code") == "missing_argument" and "period" in str(item.get("path", ""))
    ]
    assert not period_gaps, period_gaps


def test_a_failed_response_names_its_stage() -> None:
    """설명 없는 no_sql_candidates 0건 — 실패는 단계와 코드를 댄다(§6)."""

    response = call("구매주기가 30일 이하인 회원")
    reason = response.get("failure_reason")
    if reason is None:
        pytest.skip("이 실행은 성공했다")
    assert reason != "no_sql_candidates", response.get("compile_outcomes")
    assert (response.get("failure_stage") or {}).get("code"), reason


def test_repeated_runs_converge_on_one_meaning() -> None:
    """같은 입력·같은 정책·같은 기준 시각이면 의미 지문이 흔들리지 않아야 한다(§9).

    귀결 문자열이 아니라 **의미 지문**을 본다 — 검증 단계가 달라 귀결이 갈려도 뜻이 같으면
    그것은 다른 종류의 문제이고, 뜻이 갈리면 그것이 비결정성이다.
    """

    prompt = "최근 30일 구매한 회원 수를 알려줘"
    fingerprints = {call(prompt).get("semantic_fingerprint") for _ in range(2)}
    fingerprints.discard(None)
    assert len(fingerprints) <= 1, fingerprints


def test_adding_a_count_request_changes_only_the_result_shape() -> None:
    """메타모픽: COUNT 표현을 더하면 필터는 유지되고 형태만 스칼라가 된다(§11)."""

    listed = call("최근 30일 구매한 회원")
    counted = call("최근 30일 구매한 회원 수를 알려줘")
    assert (listed.get("result_shape") or {}).get("kind") == "entity_list"
    assert (counted.get("result_shape") or {}).get("kind") == "scalar"
    listed_sql = listed.get("sql") or listed.get("blocked_sql") or ""
    counted_sql = counted.get("sql") or counted.get("blocked_sql") or ""
    if not listed_sql or not counted_sql:
        pytest.skip("두 실행 중 하나가 SQL 을 내지 않았다")
    # 같은 오디언스 필터가 양쪽에 남아 있어야 한다(형태만 바뀐다).
    assert "EXISTS" in counted_sql.upper()
    assert "COUNT(" not in listed_sql.upper()
