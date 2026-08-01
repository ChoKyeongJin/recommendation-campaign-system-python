"""graph_rag.py 가 다시 커지지 않게 한다 — 하향 전용 래칫.

분할이 되돌아가는 방식은 "다시 합치자"가 아니다. 새 기능을 넣을 자리가 애매할 때
graph_rag.py 가 여전히 가장 만만한 곳이라 한 함수씩 다시 쌓이는 것이다. 그렇게 몇 달이
지나면 분할 전으로 돌아가 있고, 아무도 그 순간을 못 짚는다.

여기서는 두 가지만 강제한다:
  (1) graph_rag.py 줄 수가 기준선을 넘지 않는다.
  (2) rag/ 하위 모듈도 개별 상한을 넘지 않는다 — 한 모듈이 다시 만물상이 되는 것을 막는다.

기준선을 올리려면 `docs/data/test_baselines/module_size_baseline.json` 을 직접 고치고 커밋 메시지에
사유를 남겨야 한다. 자동 재생성 도구를 일부러 두지 않았다 — 한 줄 고치는 마찰이
"일단 여기 넣자"를 한 번 더 생각하게 만드는 것이 이 가드의 목적이다.

분할이 진행되면 기준선은 내려간다. 내려가는 것은 언제든 환영이고(현재값이 기준선보다
많이 작으면 그 사실을 알려 준다), 올라가는 것만 사유를 요구한다.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "docs" / "data" / "test_baselines" / "module_size_baseline.json"

# 기준선보다 이만큼 넘게 줄어 있으면 "기준선을 내려라"라고 알린다.
# 래칫이 실질을 잃지 않게 하는 장치 — 상한이 현재값에서 너무 멀면 아무것도 안 지킨다.
SLACK_BEFORE_TIGHTENING = 400


def _baseline() -> dict[str, int]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["max_lines"]


def _line_count(relative_path: str) -> int:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"기준선이 존재하지 않는 파일을 가리킨다: {relative_path}"
    return len(path.read_text(encoding="utf-8").splitlines())


def test_baseline_covers_every_rag_module() -> None:
    """새 모듈이 상한 없이 자라는 것을 막는다 — 목록에서 빠지면 무제한이 된다."""

    baseline = _baseline()
    tracked = set(baseline)
    actual = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "rag").rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "__init__.py"
    }
    missing = sorted(actual - tracked)
    assert not missing, (
        f"rag/ 에 상한이 없는 모듈이 있다: {missing}\n"
        f"{BASELINE_PATH.name} 의 max_lines 에 추가하라."
    )


def test_no_module_exceeds_its_ceiling() -> None:
    """상한 초과 = 분할이 되돌아가고 있다는 신호."""

    baseline = _baseline()
    over = {
        name: (ceiling, _line_count(name))
        for name, ceiling in baseline.items()
        if _line_count(name) > ceiling
    }
    assert not over, (
        "모듈이 상한을 넘었다(상한, 현재): "
        + json.dumps(over, ensure_ascii=False)
        + f"\n\n코드를 적절한 rag/ 하위 모듈로 옮기거나, 정말 필요하면 "
        f"{BASELINE_PATH.name} 의 상한을 올리고 커밋 메시지에 사유를 남겨라."
    )


def test_baseline_is_tight_enough_to_mean_something() -> None:
    """현재값이 상한보다 크게 작으면 기준선을 내려 이득을 고정한다."""

    baseline = _baseline()
    slack = {
        name: (ceiling, _line_count(name))
        for name, ceiling in baseline.items()
        if ceiling - _line_count(name) > SLACK_BEFORE_TIGHTENING
    }
    assert not slack, (
        "상한이 현재값보다 너무 헐겁다(상한, 현재): "
        + json.dumps(slack, ensure_ascii=False)
        + f"\n\n분할로 줄인 만큼 {BASELINE_PATH.name} 의 상한을 내려서 되돌아감을 막아라."
    )
