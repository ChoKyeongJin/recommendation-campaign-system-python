"""지원 조건 표 최신성 계약(플랜 A-4) — 자동 생성 문서와 실행 자산의 일치를 강제한다.

이 표는 저장소에 처음 생긴 사용자 대면 지원 조건 문서다. 손 갱신 표는 만들자마자
드리프트하므로(supported_condition_hint 가 lapsed_buyer 미지원과 자가모순이던 전례),
문서는 tools/generate_supported_conditions.py 파생으로만 존재하고 여기서 재생성 결과와
파일 내용의 바이트 일치를 강제한다 — 슬롯/라벨/behaviors/capability 가 바뀌면 이 테스트가
빨개지고, 고치는 방법은 재생성 한 줄이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import generate_supported_conditions as gen  # noqa: E402


def test_generated_doc_is_fresh() -> None:
    expected = gen.render()
    assert gen.OUTPUT_PATH.is_file(), (
        f"{gen.OUTPUT_PATH} 가 없다 — python tools/generate_supported_conditions.py 로 생성하라."
    )
    actual = gen.OUTPUT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "지원 조건 표가 실행 자산과 어긋났다. 재생성하라: "
        "python tools/generate_supported_conditions.py"
    )


def test_doc_declares_conditional_supports() -> None:
    """장기 과제 '문서/기억 동기화'의 핵심 3건이 표에 실재하는지 — 공허한 표 방지."""
    content = gen.OUTPUT_PATH.read_text(encoding="utf-8")
    assert "metric_trend" in content and "수치 집계 지표만" in content, "기간비교 조건부 지원 누락"
    assert "same_product_same_order_quantity_v1" in content, "동시구매 서명 조건 누락"
    assert "lapsed_buyer" in content and "미지원" in content, "lapsed_buyer 미지원 명시 누락"
