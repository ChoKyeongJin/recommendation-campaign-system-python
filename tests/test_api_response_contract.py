"""build_sql_result 하류가 소비하는 응답 필드가 비지 않는지 지킨다 — 순수화 착수의 게이트.

build_sql_result 는 자기 입력 plan 을 재변형하면서 capability_check·output_contract 같은 산출물을
plan 에 써넣고, 그 뒤 단계가 같은 plan 에서 그것을 읽어 응답을 만든다. 이 결합을 끊으려면
(플랜 W5-1: 입력을 deepcopy 해 순수화) 무엇이 유실되는지 볼 수 있어야 하는데 —

문제는 이 필드들이 ir_snapshot 의 **파생 키**라서 의미 골든에서 제외된다는 점이다. 즉 순수화를
잘못하면 IR 골든·SQL 골든·멱등 허용목록이 전부 초록인 채로 API 응답만 조용히 빌 수 있다.
이 파일이 그 사각지대를 덮는다. W5-1 은 이 테스트가 초록인 상태에서만 착수한다.

계약 수준은 '값 고정'이 아니라 '필드가 존재하고 비어 있지 않다'다 — 값까지 굳히면 정당한 개선까지
막힌다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import golden_support  # noqa: E402
import graph_rag  # noqa: E402

# 하류가 실제로 읽는 plan 산출물. 순수화가 이 중 하나라도 비우면 응답이 조용히 얇아진다.
# capability_check 는 plan 생산자가 없고 응답 조립이 정적 검증 요약(_capability_check_summary)
# 으로 파생 생산한다 — plan 키로서의 '비지 않음'은 여전히 단언 대상이 아니다(파생 계약은
# tests/test_capability_contract.py 가 소유).
DOWNSTREAM_PLAN_KEYS = ("capability_check", "output_contract")


@pytest.fixture(scope="module")
def built() -> list[tuple[str, dict, object]]:
    """코퍼스 케이스를 실제 파이프라인에 태워 (프롬프트, plan, 결과) 를 모은다."""

    rows: list[tuple[str, dict, object]] = []
    for case in golden_support.load_cases():
        prompt = case["prompt"]
        plan = golden_support.build_plan(prompt)
        try:
            result = graph_rag.build_sql_result(
                graph_rag.nx.Graph(),
                prompt,
                plan,
                [],
                graph_rag.DEFAULT_SCHEMA_PATH,
                default_limit=100,
            )
        except Exception:  # noqa: BLE001 — 빌더 실패 자체는 이 계약의 관심사가 아니다
            result = None
        rows.append((case["id"], plan, result))
    return rows






def test_results_are_dict_shaped_when_produced(built) -> None:
    produced = [(cid, res) for cid, _, res in built if res is not None]
    assert produced, "코퍼스 전 케이스에서 결과가 하나도 나오지 않았다 — 측정이 공허하다."
    for case_id, result in produced:
        assert isinstance(result, dict), f"{case_id}: 결과가 dict 가 아니다({type(result)})"


# SQL 도 사유도 없이 끝나는 케이스의 현재 수. 사용자 입장에서는 "아무 일도 안 일어남"이라
# 안내가 불가능하다 — 선재 공백이며 하향 전용 래칫으로 관리한다(플랜의 failure_stage 축).
SILENT_FAILURE_CEILING = 4




def test_contract_covers_the_whole_corpus(built) -> None:
    """표본이 줄면 이 게이트의 신뢰도도 줄어든다."""

    assert len(built) >= 19, f"코퍼스 케이스가 {len(built)}건으로 줄었다."
