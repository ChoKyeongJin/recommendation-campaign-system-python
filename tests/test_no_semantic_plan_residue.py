"""SemanticPlanV2 부활 방지 래칫.

2026-08-05 이행에서 오디언스 IR 은 canonical Event IR 하나가 됐고, 중간표현 스택 17종이
**모듈 파일까지** 삭제됐다. 이런 삭제가 되돌아오는 방식은 "다시 만들자"가 아니다. 어느 날
누군가 옛 노트나 옛 문서를 근거로 `import semantic_plan` 한 줄을 되살리고, 그 한 줄이 통과하면
그 옆에 스키마 표면 한 칸, 플랜 키 하나가 따라 붙는다. 그렇게 두 번째 해석 계층이 조용히
복원되고, 그 시점에는 아무도 "언제부터 두 계층이었나"를 짚지 못한다.

여기서 다섯 가지를 잰다.

1. 삭제된 모듈을 import 하는 파일이 하나도 없다(저장소 전 ``*.py`` AST).
2. LLM 노출 스키마에 ``semantic_plan`` 표면이 없다(properties·required 양쪽).
3. canonical 표식 집합 두 사본이 **일치한다**.
4. 폐기된 플랜 키가 스키마에 없다.
5. 계층 테스트와 투영기의 모듈 이름 목록에 삭제된 파일명이 없다.

3번만 방향이 다르다. ``"semantic_plan"`` 문자열은 **남기는 것이 계약**이다 — 생산자는
폐기됐지만 그 표식을 가진 **저장된 페이로드**가 있으면 값을 빼는 순간 라우팅이 canonical →
legacy 로 조용히 뒤집힌다. 그래서 여기서는 "빠졌는가"가 아니라 **두 사본이 같은가**를 잰다.
한쪽만 정리하는 것이 정확히 그 사고의 모양이기 때문이다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_authority  # noqa: E402
import plan_schema  # noqa: E402
from query_pipeline.plan_payload import event_expression_payload  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
)

# 2026-08-05 삭제된 SemanticPlanV2 스택. 이름 하나를 지우려면 그 모듈이 **왜** 되살아났는지가
# 커밋 메시지에 있어야 한다 — 되살아난 모듈은 곧 두 번째 오디언스 해석 계층이다.
RETIRED_MODULES: frozenset[str] = frozenset({
    "compile_contract",
    "legacy_plan_compiler",
    "member_attribute_history",
    "profile_metric_claims",
    "requirement_ledger",
    "semantic_candidates",
    "semantic_capability",
    "semantic_coverage",
    "semantic_pipeline",
    "semantic_plan",
    "semantic_plan_bridge",
    "semantic_plan_event_lowering",
    "semantic_plan_llm",
    "semantic_receipts",
    "semantic_reemission",
    "semantic_relation_ownership",
    "semantic_retype",
})

# 폐기된 플랜 키(생산자가 없으면 조건 키는 거짓 광고다).
RETIRED_PLAN_KEYS: tuple[str, ...] = ("semantic_plan", "semantic_pipeline", "requirements")

# 모듈 이름 목록을 리터럴로 들고 있는 계층·투영 테스트. 목록이 스테일이 되면 그 테스트는
# 존재하지 않는 파일을 감시하며 초록이 된다(= 가드가 아니라 가드가 있다는 믿음).
MODULE_LIST_OWNERS: tuple[str, ...] = (
    "tests/test_module_layering.py",
    "tests/test_generic_core_layering.py",
    "tests/test_query_pipeline_layering.py",
    # 투영 쪽은 테스트가 아니라 투영기 자신이 소스 파일명을 리터럴로 든다(AST 증거 목록).
    # 없는 파일을 required 소스로 두면 투영이 통째로 ok=False 가 되어 기동·도구가 죽는다.
    "capability_discovery/projection.py",
)

_SKIP_DIR_PARTS = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"})


def _python_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*.py")
        if not (_SKIP_DIR_PARTS & set(path.parts))
    ]


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _assigned_strings(path: Path) -> set[str]:
    """모듈 수준 대입문 안의 문자열 리터럴 전부(주석·docstring 은 제외)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        for child in ast.walk(node.value):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                values.add(child.value)
    return values


# ── ① 삭제된 모듈을 import 하는 곳이 없다 ─────────────────────────────────────────


def test_the_retired_modules_are_actually_gone() -> None:
    """래칫이 공허하지 않으려면 파일 부재가 먼저 참이어야 한다."""
    alive = sorted(name for name in RETIRED_MODULES if (REPO_ROOT / f"{name}.py").exists())
    assert not alive, (
        f"삭제됐다고 선언한 모듈이 저장소에 있다: {alive}. "
        "되살렸다면 RETIRED_MODULES 에서 빼고 **왜 두 번째 해석 계층이 필요한지**를 적어라."
    )


def test_no_module_imports_a_retired_semantic_plan_module() -> None:
    files = _python_files()
    assert len(files) > 100, f"스캔 대상이 너무 적다({len(files)}) — 스캐너가 공허하다."

    offenders: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        revived = sorted(_imported_roots(tree) & RETIRED_MODULES)
        if revived:
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {revived}")

    assert not offenders, (
        "폐기된 SemanticPlanV2 모듈을 import 하는 지점:\n  " + "\n  ".join(offenders)
    )


def test_the_import_scanner_would_catch_a_revival(tmp_path: Path) -> None:
    """탐지력 검증 — 이 가드가 조용히 무력해지는 것을 막는다."""
    planted = tmp_path / "revived.py"
    planted.write_text("from semantic_plan import SemanticPlanV2\n", encoding="utf-8")

    tree = ast.parse(planted.read_text(encoding="utf-8"))
    assert _imported_roots(tree) & RETIRED_MODULES == {"semantic_plan"}


# ── ② LLM 노출 스키마에 표면이 없다 ───────────────────────────────────────────────


def test_the_llm_schema_has_no_semantic_plan_surface() -> None:
    """노출면이 남아 있으면 모델이 그 키를 채우고, 채워진 키는 소비자를 부른다."""
    properties = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]
    required = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA.get("required") or []

    assert "semantic_plan" not in properties, (
        f"V4 노출 스키마에 semantic_plan 표면이 되살아났다: {sorted(properties)}"
    )
    assert "semantic_plan" not in set(required)


# ── ③ canonical 표식은 더 이상 라우팅을 정하지 않는다 ─────────────────────────────


def test_the_source_marker_no_longer_decides_the_execution_lane() -> None:
    """표식은 출처 라벨로만 남는다 — 경로를 가르던 사본이 사라졌다.

    폐쇄 전에는 이 표식 집합이 `audience_authority` 와 페이로드 어댑터 **양쪽**에 있었고,
    한쪽만 정리하면 그 표식을 가진 저장분의 라우팅이 canonical → legacy 로 조용히 뒤집혔다.
    legacy 레인이 닫힌 지금 라우팅 갈래 자체가 없으므로 `audience_authority` 쪽 사본을
    지웠다. 다시 만들면 두 번째 판정자가 부활한다.
    """
    assert not hasattr(audience_authority, "CANONICAL_EVENT_EXPRESSION_SOURCES"), (
        "라우팅을 정하던 표식 사본이 되살아났다 — 권위 판정은 audience_authority 하나다."
    )
    # 어댑터 쪽 사본은 출처 라벨(`EventExpressionPayload.is_canonical`)로 계속 쓰인다.
    assert "semantic_plan" in event_expression_payload.CANONICAL_SOURCES
    assert "audience_requirement" in event_expression_payload.CANONICAL_SOURCES


# ── ④ 폐기된 플랜 키가 없다 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("key", RETIRED_PLAN_KEYS)
def test_retired_plan_keys_have_no_declaration(key: str) -> None:
    assert plan_schema.kind_of(key) is None, (
        f"폐기된 플랜 키가 스키마에 되살아났다: {key!r}(kind={plan_schema.kind_of(key)!r}). "
        "생산자 없는 키는 거짓 광고다."
    )


# ── ⑤ 계층·투영 테스트의 목록이 스테일이 아니다 ──────────────────────────────────


@pytest.mark.parametrize("owner", MODULE_LIST_OWNERS)
def test_layering_module_lists_name_no_deleted_file(owner: str) -> None:
    path = REPO_ROOT / owner
    assert path.exists(), f"감시 대상 테스트가 없다: {owner}"

    literals = _assigned_strings(path)
    assert literals, f"{owner} 에서 대입 문자열을 하나도 못 찾았다 — 스캐너가 공허하다."
    # 스캐너가 **모듈 이름 자리**에 닿는지 확인한다. 엉뚱한 문자열만 걷어 오면 스테일 목록을
    # 영원히 못 잡으면서 초록이다.
    assert any(
        (REPO_ROOT / value).is_file() or (REPO_ROOT / f"{value}.py").is_file()
        for value in literals
        if value and "\n" not in value
    ), f"{owner} 의 문자열 중 실재하는 모듈이 없다 — 목록 자리를 못 읽고 있다."

    stale = sorted(
        value
        for value in literals
        if Path(value).name.removesuffix(".py") in RETIRED_MODULES
    )
    assert not stale, (
        f"{owner} 의 모듈 이름 목록이 삭제된 파일을 가리킨다: {stale}. "
        "목록에서 빼라 — 없는 파일을 감시하는 목록은 초록인 채로 아무것도 지키지 않는다."
    )
