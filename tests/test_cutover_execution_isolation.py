"""cut-over 자산은 실행 경로에 들어오지 않는다 — 결정(2026-08-04)을 계약으로 고정한다.

배경. `docs/plans_event_ir_only.md` 의 Phase 3-2 원안은 legacy 표면을 실행 위치에서 보존
위치(`preserved_legacy_audience`)로 **이사**시키는 L 단계였다. 그 전제는 "이미 cut-over 된
저장 플랜이 있다"였는데 실측이 다르게 나왔다:

  - `campaign_audience_migration` 0행(2026-08-04, campaign_db)
  - 소유자 판단: "자산이 legacy 에만 연결돼 있으면 쓰지 않는다"

그래서 이사 대신 이 계약을 둔다. 계약이 **산문이 아니라 테스트**여야 하는 이유는, 다음 사람이
0행을 "결정"이 아니라 "아직 안 썼을 뿐"으로 읽기 때문이다. 결정이 뒤집혀 legacy 에만 있는
자산을 실행해야 하는 날, 이 파일이 먼저 red 가 되어 원안(L) 으로 돌아갈지 cut-over 가 표면을
비우게 할지를 **그때 정하게** 만든다.

여기서 재지 않는 것: "권위가 event_ir 인데 legacy 표면이 남은 플랜이 차단되는가"는 Phase 3-4
(`plan_validation` 의 리터럴 → `execution_conflicts`)가 소유한다. 이 파일은 그 플랜이 애초에
실행 경로로 **들어오지 않는다**만 잰다 — 두 계약을 한 파일에 섞으면 3-4 가 red 일 때 이 결정이
깨진 것인지 게이트가 덜 된 것인지 갈리지 않는다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_cutover  # noqa: E402

# cut-over 자산을 읽거나 쓰는 모듈. 실행 경로가 이 중 하나라도 import 하면, 저장된 legacy
# 자산이 요청 처리에 참여할 길이 열린 것이다.
CUTOVER_MODULES: frozenset[str] = frozenset({
    "audience_cutover",
    "audience_migration_store",
    "legacy_audience_migration",
})

# 요청을 처리하는 모듈들. tools/ 는 제외한다 — CLI 는 정의상 실행 경로 밖이고, cut-over 를
# 수행하는 주체가 바로 그쪽이다.
EXECUTION_PATH: tuple[str, ...] = (
    "api.py",
    "graph_rag.py",
    "plan_validation.py",
    "canonical_event_ir_grounding.py",
    "query_structurer/audience_execution.py",
    # 2026-08-04(Phase 3-4): plan_validation 이 import 하는 순간 실행 경로가 됐다.
    "audience_admission.py",
    # 사용자 문구를 만드는 자리도 실행 경로다 — 여기서 cut-over 저장소를 읽으면 실패 사유가
    # 저장 자산에 의존하게 된다.
    "failure_messages.py",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def test_execution_path_files_exist() -> None:
    """공허한 통과 방지 — 파일이 사라졌는데 초록이면 이 계약은 아무것도 안 지킨다."""

    missing = [name for name in EXECUTION_PATH if not (REPO_ROOT / name).is_file()]
    assert not missing, f"실행 경로 목록이 실재하지 않는 파일을 가리킨다: {missing}"


def test_execution_path_never_imports_cutover_assets() -> None:
    """실행 경로는 cut-over 저장소를 읽지 않는다."""

    offenders: list[str] = []
    for name in EXECUTION_PATH:
        imported = _imported_modules(REPO_ROOT / name) & CUTOVER_MODULES
        offenders.extend(f"{name} -> {module}" for module in sorted(imported))

    assert not offenders, (
        "실행 경로가 cut-over 자산 모듈을 import 한다: "
        + ", ".join(offenders)
        + ". 저장된 legacy 자산을 실행하기로 결정을 바꾼 것이라면, "
        "docs/plans_event_ir_only.md §6-3 을 먼저 갱신하고 Phase 3-2 원안(표면 이사)을 되살려라."
    )


def test_cutover_still_leaves_the_legacy_surface_in_place() -> None:
    """이사하지 않기로 한 결정의 다른 쪽 면.

    `plan_after_cutover` 는 슬롯을 지우지 않는다(그것이 rollback 을 '권위 되돌리기'로 유지하는
    조건이다). 이 사실이 바뀌면 계약의 전제가 바뀐 것이므로 함께 red 가 되어야 한다 —
    표면을 비우기 시작했다면 그때는 3-2 원안 쪽이 맞는 설계다.
    """

    plan = {
        "target_user": {"grades": ["VIP"]},
        "audience_authority": "legacy",
    }
    envelope = {"expression": {"kind": "exists"}, "source": "legacy_migration"}

    after = audience_cutover.plan_after_cutover(plan, envelope)

    assert after["target_user"] == {"grades": ["VIP"]}, (
        "cut-over 가 legacy 표면을 비우기 시작했다 — rollback 이 역변환을 요구하게 된다."
    )
    assert after["audience_authority"] == "event_ir"
    # rollback 은 표현을 지우지 않고 권위만 되돌린다(dual-storage ≠ dual-execution).
    back = audience_cutover.plan_after_rollback(after)
    assert back["target_user"] == plan["target_user"]
    assert back["audience_authority"] == "legacy"
    assert back["event_expression"] == envelope
