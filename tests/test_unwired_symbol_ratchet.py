"""정의만 있고 부르는 곳이 없는 공개 심볼이 늘지 않게 한다.

설정 키와 같은 결함이 코드에도 있다: 함수·클래스·상수를 만들고 **배선을 안 붙인다.** 예외가
아니라 침묵이라 다음 사람은 그 심볼을 살아 있는 경로로 믿고 고친다. 실제로 이 저장소에서는
조건 재조정 파이프라인 한 벌(4개 진입점)과 외부조건 전역 싱글턴이 그 상태였다.

여기서 강제하는 것은 셋이다.

1. **새 사문 심볼 금지** — 기준선에 없는 미배선 심볼이 생기면 실패한다.
2. **하향 전용** — 배선했거나 지운 심볼이 기준선에 남아 있으면 실패한다.
3. **사유 기록 의무** — 모든 항목이 ``kind`` 와 사람이 읽는 ``reason`` 을 갖는다.

``kind=dead`` 는 테스트조차 부르지 않는 것, ``kind=test_only`` 는 테스트만 부르는 것이다.
후자는 "계약 테스트가 지키는 조회 표면"일 수도 있으므로 사유로 구분한다 — 다만 프로덕션
호출자가 없다는 사실 자체는 둘 다 같으므로 총량은 함께 래칫에 건다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import unwired_symbol_inventory as inventory  # noqa: E402

BASELINE_PATH = REPO_ROOT / "docs" / "data" / "test_baselines" / "unwired_symbol_baseline.json"
VALID_KINDS = {"dead", "test_only"}


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _current() -> dict[str, str]:
    result = inventory.scan()
    return {symbol: "dead" for symbol in result["dead"]} | {
        symbol: "test_only" for symbol in result["test_only"]
    }


def test_baseline_is_non_trivial() -> None:
    baseline = _baseline()
    assert baseline["entries"], "기준선이 비면 래칫이 공허하게 통과한다."
    assert baseline["dead_total"] == sum(
        1 for entry in baseline["entries"].values() if entry["kind"] == "dead"
    ), "dead_total 이 entries 와 어긋난다."


def test_every_entry_names_its_reason() -> None:
    broken = [
        symbol
        for symbol, entry in _baseline()["entries"].items()
        if entry.get("kind") not in VALID_KINDS or not str(entry.get("reason") or "").strip()
    ]
    assert not broken, f"kind/reason 이 빠진 기준선 항목: {broken}"


def test_no_new_unwired_symbol() -> None:
    """공개 심볼을 만들면서 부르는 곳을 안 붙이면 여기서 걸린다."""

    baseline = _baseline()
    added = sorted(set(_current()) - set(baseline["entries"]))
    assert not added, (
        "정의만 있고 프로덕션이 부르지 않는 공개 심볼이 새로 생겼다:\n  "
        + "\n  ".join(added)
        + f"\n호출부를 붙이거나, 의도된 미배선이면 {BASELINE_PATH.name} 에 사유와 함께 등록할 것."
    )


def test_baseline_only_shrinks() -> None:
    """배선했거나 지운 심볼이 기준선에 남으면 다음 회귀 판정이 흐려진다."""

    baseline = _baseline()
    stale = sorted(set(baseline["entries"]) - set(_current()))
    assert not stale, (
        "이제 배선됐거나 사라진 심볼이 기준선에 남아 있다:\n  "
        + "\n  ".join(stale)
        + f"\n{BASELINE_PATH.name} 에서 지우고 dead_total/test_only_total 을 함께 낮출 것."
    )


def test_totals_do_not_grow() -> None:
    baseline = _baseline()
    current = _current()
    assert len(current) <= baseline["dead_total"] + baseline["test_only_total"], (
        "미배선 공개 심볼 총량이 기준선을 넘었다."
    )


def test_deleted_external_condition_singleton_stays_deleted() -> None:
    """이 래칫이 실제로 배선을 구분하는지 보이는 회귀 사례.

    ``get_default_service``/``reset_default_service`` 는 호출자가 없는 전역 mutable 싱글턴이라
    삭제했다. 되살아나면 기준선에 없는 새 사문 심볼로 잡혀야 한다 — 안 잡히면 인벤토리가
    사문을 못 보는 것이고, 그러면 이 래칫 전체가 무의미해진다.
    """

    import external_conditions.service as service

    assert not hasattr(service, "get_default_service")
    assert not hasattr(service, "reset_default_service")
    assert hasattr(service, "build_default_service"), (
        "주입 지점(build_default_service)까지 지우면 외부 조건 계층에 들어갈 문이 없어진다."
    )
