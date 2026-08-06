"""설정 선언과 소비 코드가 조용히 끊긴 채로 늘어나지 않게 한다.

이 저장소의 반복 결함은 "선언은 맞았는데 배선이 없다"이다(NOTES 2026-08-03 §"관통하는 발견").
로더가 파일 전체를 읽어도 그 키를 **이름으로 꺼내는 코드**가 없으면 선언은 죽어 있고, 증상은
예외가 아니라 침묵이라 영영 드러나지 않는다 — 설정 한 줄을 고친 사람은 아무 일도 일어나지 않는
것을 보게 된다.

여기서 강제하는 것은 셋이다.

1. **새 사문 선언 금지** — 기준선에 없는 미소비 키가 생기면 실패한다.
2. **하향 전용** — 배선했거나 지운 키가 기준선에 남아 있으면 실패한다(기준선은 줄기만 한다).
3. **사정 기록 의무** — 모든 항목은 ``kind`` 와 사람이 읽는 ``reason`` 을 갖는다.

``kind=generic`` 은 부모를 순회·조회해 소비하는 데이터 키라 결함이 아니고,
``kind=unbuilt`` 는 선언만 있고 구현이 없는 축이다. 후자만 총량 래칫에 걸린다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import config_consumption_inventory as inventory  # noqa: E402

BASELINE_PATH = REPO_ROOT / "docs" / "data" / "test_baselines" / "config_consumption_baseline.json"
VALID_KINDS = {"generic", "unbuilt"}


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_baseline_is_non_trivial() -> None:
    """기준선이 비면 래칫이 아무것도 지키지 않는다."""

    baseline = _baseline()
    assert baseline["entries"], "기준선이 비었다 — 래칫이 공허하게 통과한다."
    assert baseline["unbuilt_total"] == sum(
        1 for entry in baseline["entries"].values() if entry["kind"] == "unbuilt"
    ), "unbuilt_total 이 entries 와 어긋난다(기준선을 손으로 고칠 때 함께 갱신할 것)."


def test_every_entry_names_its_reason() -> None:
    """사정 없는 예외는 다음 사람에게 '왜 죽어 있는지 모르는 선언'으로 남는다."""

    broken = [
        path
        for path, entry in _baseline()["entries"].items()
        if entry.get("kind") not in VALID_KINDS or not str(entry.get("reason") or "").strip()
    ]
    assert not broken, f"kind/reason 이 빠진 기준선 항목: {broken}"


def test_no_new_unconsumed_declaration() -> None:
    """설정에 키를 더하면서 읽는 코드를 안 붙이면 여기서 걸린다."""

    baseline = _baseline()
    current = set(inventory.scan()["unconsumed"])
    added = sorted(current - set(baseline["entries"]))
    assert not added, (
        "설정에 선언됐는데 읽는 코드가 없는 키가 새로 생겼다:\n  "
        + "\n  ".join(added)
        + "\n선언만 넣으면 동작하지 않는다 — 소비 코드를 붙이거나, 의도적 미구현이면 "
        f"{BASELINE_PATH.name} 에 사유와 함께 등록할 것."
    )


def test_baseline_only_shrinks() -> None:
    """배선했거나 지운 키가 기준선에 남으면 다음 회귀 판정이 그만큼 흐려진다."""

    baseline = _baseline()
    current = set(inventory.scan()["unconsumed"])
    stale = sorted(set(baseline["entries"]) - current)
    assert not stale, (
        "이제 소비되거나 사라진 키가 기준선에 남아 있다:\n  "
        + "\n  ".join(stale)
        + f"\n{BASELINE_PATH.name} 에서 지우고 unbuilt_total/generic_total 을 함께 낮출 것."
    )


def test_unbuilt_total_does_not_grow() -> None:
    """구현 없는 선언의 총량은 늘 수 없다."""

    baseline = _baseline()
    current = set(inventory.scan()["unconsumed"])
    unbuilt_now = sum(
        1
        for path, entry in baseline["entries"].items()
        if entry["kind"] == "unbuilt" and path in current
    )
    assert unbuilt_now <= baseline["unbuilt_total"], (
        f"구현 없는 선언이 {baseline['unbuilt_total']} → {unbuilt_now} 로 늘었다."
    )


def test_region_density_defaults_are_read_from_config() -> None:
    """이 래칫이 실제로 배선을 구분하는지 보이는 회귀 사례.

    ``default_top_n``·``max_top_n``·``exclude_null_or_empty`` 는 코드에 5 가 박혀 있고 설정은
    죽어 있었다. 배선한 뒤에는 미소비 목록에서 빠져야 한다 — 빠지지 않으면 인벤토리가 배선을
    못 보는 것이고, 그러면 이 래칫 전체가 무의미해진다.
    """

    unconsumed = set(inventory.scan()["unconsumed"])
    wired = [
        "docs/data/runtime/sql/member_target_filters.json::region_density.default_top_n",
        "docs/data/runtime/sql/member_target_filters.json::region_density.max_top_n",
        "docs/data/runtime/sql/member_target_filters.json::region_density.exclude_null_or_empty",
    ]
    assert not [path for path in wired if path in unconsumed], (
        "배선된 region_density 기본값이 여전히 미소비로 잡힌다 — 인벤토리 판정이 틀렸다."
    )
