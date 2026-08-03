"""계층 방향 가드 — 문서의 화살표를 **강제**로 만든다.

리팩터링의 이득(검증 전 IR 이 SQL 로 못 간다)은 import 방향이 지켜질 때만 유지된다.
방향이 한 번 뒤집히면 그다음부터는 어느 계층이 무엇을 아는지가 코드 리뷰로만 지켜지고,
이 저장소의 규모에서 그것은 지켜지지 않는다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "query_pipeline"

# 계층 → 그 계층이 import 해도 되는 계층. base 는 어느 계층에도 속하지 않는 공통 기반이다.
ALLOWED: dict[str, frozenset[str]] = {
    "base": frozenset(),
    "event_query": frozenset({"base"}),
    "requirement": frozenset({"base", "event_query"}),
    "planning": frozenset({"base", "event_query"}),
    "compiler": frozenset({"base", "event_query", "planning"}),
    # 조립 지점과 호환 어댑터는 아래 네 계층을 전부 안다(그것이 존재 이유다).
    "pipeline": frozenset({"base", "event_query", "requirement", "planning", "compiler"}),
    "compatibility": frozenset(
        {"base", "event_query", "requirement", "planning", "compiler"}
    ),
}


def _layer_of(path: Path) -> str:
    relative = path.relative_to(PACKAGE)
    if relative.parent == Path("."):
        return relative.stem
    return relative.parts[0]


def _imported_layers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    layers: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        for module in modules:
            parts = module.split(".")
            if parts[0] != "query_pipeline" or len(parts) < 2:
                continue
            layers.add(parts[1])
    return layers


def _modules() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "__init__.py"
    )


@pytest.mark.parametrize("path", _modules(), ids=lambda path: str(path.name))
def test_layer_imports_only_flow_downstream(path: Path) -> None:
    layer = _layer_of(path)
    assert layer in ALLOWED, f"알 수 없는 계층입니다: {layer}"
    allowed = ALLOWED[layer] | {layer}
    violations = sorted(_imported_layers(path) - allowed)
    assert not violations, (
        f"{layer} 계층이 허용되지 않은 계층을 import 합니다: {', '.join(violations)}"
    )


def test_requirement_layer_never_imports_the_compiler() -> None:
    """§11 의 명시 금지 조항을 이름으로 못박는다."""
    for path in _modules():
        if _layer_of(path) != "requirement":
            continue
        assert "compiler" not in _imported_layers(path), path


# 이 배포의 오디언스 **도메인**을 담은 저장소 루트 모듈. 계층 가드가 패키지 내부 방향만
# 보므로, 범용 계층이 도메인 계층이 되는 경로는 이쪽으로 열린다 — 검증기를 요구 계층에
# 그대로 옮겼다면 정확히 여기서 막혔을 것이다.
#
# `event_ir`·`event_compiler`·`sql_dialect`·`calendar_window` 는 여기 없다: 각각 사건 대수,
# 물리 렌더 엔진, 방언 문법, 달력 문법의 **단일 소유자**이고 특정 오디언스 스키마를 모른다.
FORBIDDEN_DOMAIN_MODULES: frozenset[str] = frozenset(
    {
        "audience_runtime",
        "audience_validators",
        "canonical_audience_claims",
        "event_parser",
        "execution_assets",
        "graph_rag",
        "plan_decisions",
        "query_structurer",
        "semantic_plan",
    }
)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _modules(), ids=lambda path: str(path.name))
def test_package_never_imports_the_audience_domain(path: Path) -> None:
    violations = sorted(_imported_roots(path) & FORBIDDEN_DOMAIN_MODULES)
    assert not violations, (
        f"{path.name} 이 도메인 모듈을 직접 import 합니다: {', '.join(violations)}."
        " 도메인이 필요하면 프로토콜로 주입하십시오"
        " (query_pipeline.requirement.validation.ExpressionValidator 참고)."
    )


def test_every_layer_has_a_declared_direction() -> None:
    """새 계층이 생기면 방향 선언 없이는 통과하지 못한다."""
    discovered = {_layer_of(path) for path in _modules()}
    assert discovered <= set(ALLOWED), sorted(discovered - set(ALLOWED))
