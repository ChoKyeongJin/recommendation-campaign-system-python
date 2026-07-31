"""파이프라인 패스의 멱등성을 측정한다 — 구조 이동의 안전성 주장을 수치로 바꾸는 계층.

이 저장소의 정확성은 "모든 결정론 패스는 멱등이다"라는 **강제되지 않는 암묵 계약** 위에 서 있다.
같은 패스 묶음이 파이프라인에서 최대 3회 재실행되기 때문이다(플랜 확정 / 원문 권위 재확정 /
SQL 생성 진입부). 멱등이 아닌 패스가 하나 끼면 재실행 지점에서 조건이 중복 컴파일되거나
값이 누적된다.

여기서는 세 축을 잰다:
  (a) 결정론 필터 하나하나 — 같은 plan 에 두 번 돌려 결과가 같은가
  (b) 소스 권위 재확정 번들 — 통째로 다시 돌려 IR 이 변하지 않는가
  (c) build_sql_result — 자기 입력 plan 을 변형하는가(순수성)

측정이지 강제가 아니다. 위반이 발견되면 허용목록(tests/golden/idempotence_exceptions.json)에
사유와 함께 올리고 개수를 래칫으로 묶는다 — 지금 전부 고치는 것이 목적이 아니라, 구조를 옮길 때
'무엇이 안전한지'를 사람 판단이 아니라 측정으로 답하는 것이 목적이다.

주의: 코퍼스 19케이스 표본 위의 주장이다. 전 프롬프트에 대한 증명이 아니다.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import golden_support  # noqa: E402
import graph_rag  # noqa: E402
import ir_snapshot  # noqa: E402
import semantic_fields  # noqa: E402

EXCEPTIONS_PATH = REPO_ROOT / "tests" / "golden" / "idempotence_exceptions.json"


def _exceptions() -> dict:
    if not EXCEPTIONS_PATH.exists():
        return {}
    return json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    golden_support.apply_corpus_env()
    return golden_support.load_cases()


def _semantic_view(plan: dict) -> dict:
    """의미만 남긴 비교용 뷰 — 출처(스팬)와 감사 로그 차이는 멱등성 판정에서 제외한다."""
    return semantic_fields.strip_provenance(ir_snapshot.snapshot(plan))


# ── (a) 결정론 필터 단위 ─────────────────────────────────────────────────────────────


def test_every_deterministic_filter_is_idempotent(cases: list[dict]) -> None:
    """필터를 한 번 더 돌렸을 때 plan 이 달라지면, 재실행 지점에서 조건이 중복·누적된다."""

    allowed = set(_exceptions().get("filters") or {})
    offenders: dict[str, list[str]] = {}

    registry = graph_rag._deterministic_filter_registry()
    for case in cases[:6]:  # 표본 — 전 케이스 × 전 필터는 비싸다
        prompt = case["prompt"]
        base = golden_support.build_plan(prompt)
        for name, spec in registry.items():
            runner = getattr(spec, "impl", None) or getattr(spec, "run", None)
            if runner is None:
                continue
            plan = copy.deepcopy(base)
            try:
                runner(prompt, plan)
                once = _semantic_view(plan)
                runner(prompt, plan)
                twice = _semantic_view(plan)
            except Exception:  # noqa: BLE001 — 필터 단독 실행이 불가한 경우는 측정 대상 밖
                continue
            if once != twice and name not in allowed:
                offenders.setdefault(name, []).append(case["id"])

    assert not offenders, (
        "두 번 돌리면 결과가 달라지는 필터:\n  "
        + "\n  ".join(f"{name}: {ids}" for name, ids in offenders.items())
        + f"\n\n고치거나 {EXCEPTIONS_PATH.name} 에 사유와 함께 등재하라."
    )


# ── (b) 소스 권위 재확정 번들 ────────────────────────────────────────────────────────


def test_source_authoritative_bundle_rerun_is_a_noop(cases: list[dict]) -> None:
    """원문 권위 재확정을 한 번 더 돌렸을 때 의미가 바뀌면 순서 의존이 실재한다는 뜻이다."""

    allowed = set(_exceptions().get("source_authoritative") or {})
    offenders: list[str] = []

    for case in cases:
        prompt = case["prompt"]
        plan = golden_support.build_plan(prompt)
        before = _semantic_view(plan)
        graph_rag._apply_source_authoritative_stages(
            prompt,
            plan,
            sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
            normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
        )
        after = _semantic_view(plan)
        if before != after and case["id"] not in allowed:
            offenders.append(case["id"])

    assert not offenders, (
        f"재확정 번들 재실행으로 의미가 바뀌는 케이스: {offenders}. "
        f"고치거나 {EXCEPTIONS_PATH.name} 에 등재하라."
    )


# ── (c) build_sql_result 의 입력 순수성 ──────────────────────────────────────────────


def test_build_sql_result_does_not_mutate_the_semantic_plan(cases: list[dict]) -> None:
    """SQL 빌더가 자기 입력을 재변형하면 '생성'과 '소비'의 경계가 사라진다.

    지금은 실제로 변형한다(플래너 패스 여럿을 내부에서 다시 돌린다). 그래서 이 테스트는
    '변형하지 않는다'가 아니라 **변형하는 케이스 수를 고정**한다 — 구조를 옮길 때 이 숫자가
    0 으로 내려가는 것이 성공 판정이 된다.
    """

    mutating: list[str] = []
    for case in cases:
        prompt = case["prompt"]
        plan = golden_support.build_plan(prompt)
        before = _semantic_view(plan)
        try:
            graph_rag.build_sql_result(
                graph_rag.nx.Graph(),
                prompt,
                plan,
                [],
                graph_rag.DEFAULT_SCHEMA_PATH,
                default_limit=100,
            )
        except Exception:  # noqa: BLE001 — 빌더 실패는 이 측정의 관심사가 아니다
            pass
        if _semantic_view(plan) != before:
            mutating.append(case["id"])

    baseline = _exceptions().get("build_sql_result_mutating_cases")
    if baseline is None:
        pytest.skip("기준선 미기록 — tests/golden/idempotence_exceptions.json 에 현재 값을 적어라.")
    assert len(mutating) <= len(baseline), (
        f"build_sql_result 가 입력을 변형하는 케이스가 늘었다: {sorted(mutating)} "
        f"(기준선 {sorted(baseline)})"
    )


def test_exception_ledger_entries_have_reasons() -> None:
    """허용목록이 사유 없는 면제 창구가 되지 않게 한다."""

    data = _exceptions()
    for section in ("filters", "source_authoritative"):
        for name, reason in (data.get(section) or {}).items():
            assert str(reason).strip(), f"{section}.{name} 에 사유가 없다."
