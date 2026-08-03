"""§8 계약의 증거 — 금지된 호출이 **정적 타입 검사에서** 실패한다.

이 저장소의 안전망은 대부분 런타임 검사다. 그런데 이번 리팩터링의 핵심 주장("검증되지
않은 요구는 SQL 컴파일러에 도달할 수 없다")은 시그니처로 지켜지므로, 증거도 타입 검사로
남겨야 한다. 런타임 테스트로 대신하면 그 주장이 지켜지는지를 아무도 확인하지 않는다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = REPO_ROOT / "tests" / "type_contracts" / "forbidden_calls.py"

# 파일 안의 함수 이름 → 그 호출이 반려되어야 하는 이유(에러 메시지에 나타나는 기대 타입).
EXPECTED_REJECTIONS: tuple[tuple[str, str], ...] = (
    ("AudienceRequirement", "create_plan"),
    ("AudienceRequirement", "compile"),
    ("ProposedExpression", "compile"),
)


def _mypy_available() -> bool:
    if shutil.which("mypy"):
        return True
    probe = subprocess.run(
        [sys.executable, "-m", "mypy", "--version"],
        capture_output=True,
        cwd=REPO_ROOT,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _mypy_available(), reason="mypy 가 설치돼 있지 않다")
def test_forbidden_calls_fail_static_type_check() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", str(FORBIDDEN.relative_to(REPO_ROOT))],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0, (
        "요구를 실행 계층에 넘기는 호출이 타입 검사를 통과했다:\n" + result.stdout
    )
    output = result.stdout
    for actual, method in EXPECTED_REJECTIONS:
        assert any(
            actual in line and method in line and "incompatible type" in line
            for line in output.splitlines()
        ), f"{actual} → {method} 반려가 없다:\n{output}"


@pytest.mark.skipif(not _mypy_available(), reason="mypy 가 설치돼 있지 않다")
def test_query_pipeline_package_passes_strict_type_check() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "mypy"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout
