"""형제 레포(BFF)가 실제로 읽는 응답 키의 계약.

배경: 타겟팅 결과 화면은 이 저장소와 형제 레포 `recommendation-campaign-system-frontend`
두 곳에 걸쳐 있고, BFF(`app/api/targeting/route.ts`)는 `api_response` 의 특정 키를 이름으로
읽는다. 그런데 백엔드 쪽에는 "이 키들이 외부 계약이다"라는 표시가 어디에도 없었다 —
키 하나를 이름만 바꿔도 pytest 는 전부 초록인 채 화면의 진단·실패 배지가 조용히 사라진다.

canonical 플랜 0-0 은 응답 키를 바꾸기 전에 외부 소비를 열거하라고 요구한다. 이 파일이 그
열거의 **기계 검증본**이다. 2026-08-02 기준 실제 소비 지점:

  route.ts getDiagnosticsFromPythonResponse  status / failure_reason /
      unsupported_conditions / unsupported_condition_labels /
      dropped_conditions / dropped_condition_labels /
      missing_input_conditions / clarification_questions /
      database_execution.cardinality_diagnostic{cause, member_total,
      culprit_predicates, injected_default_is_culprit}
  route.ts getConfidenceFromPythonResponse   confidence.overall_score (**숫자여야 통과**)
  route.ts getFailureStageFromPythonResponse failure_stage{code,label,order,total,pipeline[]}
      — 넷 중 하나라도 없으면 BFF 가 통째로 null 처리한다(배지 미노출)

주의(실측): 라벨 키는 `unsupported_condition_labels` / `dropped_condition_labels` 로
**단수 condition** 이다. 복수형으로 착각해 바꾸면 라벨만 조용히 사라진다.

이 파일은 값의 내용을 검사하지 않는다. **이름과 모양**만 고정한다 — 그것이 외부 계약이다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402

# BFF 가 api_response 최상위에서 이름으로 읽는 키.
BFF_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "status",
    "failure_reason",
    "unsupported_conditions",
    "unsupported_condition_labels",
    "dropped_conditions",
    "dropped_condition_labels",
    "missing_input_conditions",
    "clarification_questions",
    "confidence",
    "failure_stage",
    "sql",
    "database_execution",
)

# BFF 가 failure_stage 안에서 요구하는 필드. 하나라도 없으면 배지 전체가 사라진다.
BFF_FAILURE_STAGE_FIELDS: tuple[str, ...] = ("code", "label", "order", "total", "pipeline")

# BFF 가 database_execution.cardinality_diagnostic 안에서 읽는 필드.
BFF_CARDINALITY_FIELDS: tuple[str, ...] = (
    "cause",
    "member_total",
    "culprit_predicates",
    "injected_default_is_culprit",
)


def _sources() -> str:
    """응답을 조립하는 모듈 소스 전체(키 생산 여부를 이름으로 확인한다)."""
    texts = []
    for name in ("graph_rag.py", "api.py", "rag/failure_stage.py", "rag/trace.py"):
        texts.append((REPO_ROOT / name).read_text(encoding="utf-8"))
    return "\n".join(texts)


@pytest.mark.parametrize("key", BFF_TOP_LEVEL_KEYS)
def test_backend_still_produces_every_bff_top_level_key(key: str) -> None:
    """BFF 가 읽는 키가 백엔드에서 사라지면 화면이 조용히 빈다."""
    assert re.search(rf'["\']{re.escape(key)}["\']', _sources()), (
        f"BFF 가 읽는 응답 키 {key!r} 를 백엔드가 더 이상 만들지 않는다.\n"
        "이름을 바꿨다면 형제 레포 app/api/targeting/route.ts 를 함께 고쳐야 한다 — "
        "pytest 는 전부 초록인 채 화면의 진단만 사라진다."
    )


@pytest.mark.parametrize("field", BFF_FAILURE_STAGE_FIELDS)
def test_failure_stage_fields_survive(field: str) -> None:
    """BFF 는 code/label/order/total 중 하나라도 없으면 failure_stage 를 통째로 버린다."""
    stage_source = (REPO_ROOT / "rag" / "failure_stage.py").read_text(encoding="utf-8")
    assert re.search(rf'["\']{re.escape(field)}["\']', stage_source), (
        f"failure_stage.{field} 가 사라졌다 — BFF 는 이 필드가 없으면 실패 단계 배지를 "
        "아예 렌더링하지 않는다(부분 표시가 아니라 전무)."
    )


def test_label_keys_are_singular_condition() -> None:
    """실측된 함정: 라벨 키는 `*_condition_labels`(단수) 다.

    복수형(`*_conditions_labels`)으로 바꾸면 BFF 의 toStringList 가 빈 배열을 받아
    라벨만 조용히 사라진다 — 조건 목록 자체는 남아 있어 눈치채기 어렵다.
    """
    source = _sources()
    assert '"unsupported_condition_labels"' in source
    assert '"dropped_condition_labels"' in source
    assert '"unsupported_conditions_labels"' not in source, "복수형 오타가 들어왔다."
    assert '"dropped_conditions_labels"' not in source, "복수형 오타가 들어왔다."


def test_confidence_overall_score_is_numeric_when_confidence_exists() -> None:
    """BFF 는 `typeof confidence.overall_score !== "number"` 면 confidence 전체를 버린다."""
    low = graph_rag._low_confidence() if hasattr(graph_rag, "_low_confidence") else None
    if low is None:
        pytest.skip("저신뢰 confidence 팩토리가 없다 — api.py 인라인 구성만 존재")
    assert isinstance(low.get("overall_score"), (int, float)), (
        "overall_score 가 숫자가 아니면 BFF 가 신뢰도 패널을 통째로 숨긴다."
    )


def test_api_module_builds_confidence_with_numeric_score() -> None:
    """api.py 가 실행 실패 시 조립하는 저신뢰 응답도 같은 계약을 지켜야 한다."""
    api_source = (REPO_ROOT / "api.py").read_text(encoding="utf-8")
    match = re.search(r'"overall_score"\s*:\s*([^,\n]+)', api_source)
    assert match, "api.py 가 overall_score 를 만들지 않는다."
    assert match.group(1).strip().rstrip(",").isdigit(), (
        f"overall_score 가 숫자 리터럴이 아니다: {match.group(1)!r}"
    )


@pytest.mark.parametrize("field", BFF_CARDINALITY_FIELDS)
def test_cardinality_diagnostic_fields_survive(field: str) -> None:
    source = _sources()
    assert re.search(rf'["\']{re.escape(field)}["\']', source), (
        f"cardinality_diagnostic.{field} 가 사라졌다 — BFF 의 부분추출 진단 힌트가 빈다."
    )


def test_contract_list_is_not_empty() -> None:
    """목록이 비면 이 가드는 아무것도 지키지 않는다."""
    assert len(BFF_TOP_LEVEL_KEYS) >= 10
    assert len(BFF_FAILURE_STAGE_FIELDS) >= 4


def test_sibling_repo_reference_is_recorded() -> None:
    """계약의 상대가 누구인지 소스에 남아 있어야 한다(다음 사람이 찾을 수 있게)."""
    assert "recommendation-campaign-system-frontend" in Path(__file__).read_text(encoding="utf-8")
