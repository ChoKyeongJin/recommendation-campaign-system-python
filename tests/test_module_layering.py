"""rag/ 패키지의 의존 방향 계약 — 분할이 순환 import 로 퇴화하는 것을 막는다.

graph_rag.py 를 rag/ 하위 모듈로 나누는 동안, 가장 흔한 사고는 옮긴 모듈이 남은
graph_rag 의 헬퍼 하나를 필요로 해서 되돌아 import 하는 것이다. 그 순간 두 파일은
"나뉜 한 파일"이 되고, 분할의 이득(독립 이해·독립 테스트·무거운 의존 격리)이 사라진다.

분할 착수 시점(2026-08-01)에 graph_rag 내부 호출그래프의 강결합 성분(SCC)이 정확히
0개임을 측정으로 확인했다 — 즉 순환은 **원래 없었다**. 이 테스트는 그 사실을 계약으로
고정한다. 나중에 순환이 생기면 "원래 있었다"가 아니라 "이 변경이 만들었다"가 된다.

계약 4종:
1. rag.* 는 graph_rag 를 import 하지 않는다(단방향).
2. graph_rag 가 rag.* 로 넘긴 심볼은 façade 에서 계속 접근 가능하다.
3. rag.* 하위 모듈끼리도 순환하지 않는다.
4. 진단 리프 모듈(LEAF_DIAGNOSTIC_MODULES)은 **stdlib 만** import 한다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAG_PKG = REPO_ROOT / "rag"

# 실패 진단 계층의 리프. 이 모듈들은 응답 조립·트레이스·실패로그에서 **무조건** 호출되므로,
# 코어 스키마를 로드하는 모듈을 하나라도 끌어오면 진단 자체가 그 로딩 실패에 묶인다
# (같은 계층의 failure_messages 가 정확히 이 이유로 semantic_plan 을 함수 안에서 지연 import 한다).
# 대가로 이들은 키·사유를 리터럴로 들고 있고, 소유 모듈과의 일치는 각자의 계약 테스트가 잰다.
LEAF_DIAGNOSTIC_MODULES: tuple[str, ...] = (
    "audience_failure.py",
    "rag/failure_stage.py",
)


def _rag_modules() -> list[Path]:
    return sorted(p for p in RAG_PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(path: Path) -> set[str]:
    """이 파일이 import 하는 최상위 모듈 이름(패키지 상대 import 는 rag.<x> 로 정규화)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # from . import x / from .search import y
                names.add("rag")
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def test_rag_package_exists_and_is_scanned() -> None:
    """공허한 통과 방지 — 스캔 대상이 없으면 green 이 의미가 없다."""

    modules = _rag_modules()
    assert modules, "rag/ 패키지에 파이썬 모듈이 없다."
    assert any(p.name != "__init__.py" for p in modules), (
        "rag/ 에 __init__.py 뿐이다 — 실제 반출 모듈이 있어야 이 계약이 의미를 갖는다."
    )


def test_rag_modules_never_import_graph_rag() -> None:
    """의존 방향은 graph_rag → rag.* 단방향이다."""

    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _rag_modules()
        if "graph_rag" in _imported_modules(p)
    ]
    assert not offenders, (
        "rag/ 하위 모듈이 graph_rag 를 import 한다 — 순환이 생기면 분할의 이득이 사라진다.\n  "
        + "\n  ".join(offenders)
        + "\n\n필요한 헬퍼가 있다면 common_utils 같은 리프 모듈로 내리거나, "
        "호출자가 인자로 주입하라."
    )


def test_rag_subpackage_imports_are_acyclic() -> None:
    """rag.* 내부끼리도 순환하지 않는다(하위 모듈이 늘어날 때를 대비한 계약)."""

    graph: dict[str, set[str]] = {}
    for path in _rag_modules():
        if path.name == "__init__.py":
            continue
        module = f"rag.{path.stem}"
        deps: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("rag."):
                deps.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("rag."):
                        deps.add(alias.name)
        graph[module] = deps

    visiting: set[str] = set()
    done: set[str] = set()
    cycles: list[str] = []

    def walk(node: str, trail: list[str]) -> None:
        if node in visiting:
            cycles.append(" -> ".join([*trail, node]))
            return
        if node in done or node not in graph:
            return
        visiting.add(node)
        for dep in sorted(graph[node]):
            walk(dep, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for module in sorted(graph):
        walk(module, [])

    assert not cycles, "rag.* 하위 모듈 사이에 순환 import 가 있다:\n  " + "\n  ".join(cycles)


def test_leaf_diagnostic_modules_import_only_stdlib() -> None:
    """진단 리프가 프로젝트 모듈을 끌어오면 실패 진단이 그 모듈의 로딩 실패에 묶인다.

    실패 응답을 만드는 계층은 **가장 적게 의존해야** 한다 — 실패 진단이 실패하면 남는 것은
    처리되지 않은 500 이고, 그때 사용자는 "왜 SQL 이 안 나왔나"조차 못 듣는다.
    """
    offenders: list[str] = []
    for relative in LEAF_DIAGNOSTIC_MODULES:
        path = REPO_ROOT / relative
        assert path.exists(), f"선언된 리프 모듈이 없다: {relative} — 이 계약이 공허해진다."
        for name in sorted(_imported_modules(path)):
            if name not in sys.stdlib_module_names:
                offenders.append(f"{relative}: {name}")
    assert not offenders, (
        "진단 리프가 stdlib 밖을 import 한다:\n  " + "\n  ".join(offenders)
        + "\n\n필요한 값이 있다면 리터럴로 들고 계약 테스트로 드리프트를 잡아라 "
        "— 그것이 이 계층이 순수성을 사는 방식이다."
    )


def test_graph_rag_still_exposes_symbols_moved_into_rag() -> None:
    """반출한 심볼이 façade 에서 사라지지 않았는지 — 이관 누락을 즉시 잡는다."""

    import graph_rag
    import rag.search

    moved_public = [
        name
        for name in dir(rag.search)
        if not name.startswith("_") and name in getattr(rag.search, "__dict__", {})
    ]
    # rag.search 가 자체 정의한 공개 심볼 중, 외부 계약(façade 목록)에 있는 것만 검사한다.
    from tests.test_graph_rag_facade import FACADE_SYMBOLS

    expected = sorted(set(moved_public) & set(FACADE_SYMBOLS))
    assert expected, "rag.search 에서 façade 계약 심볼을 하나도 찾지 못했다 — 검사가 공허하다."

    missing = [name for name in expected if not hasattr(graph_rag, name)]
    assert not missing, (
        f"rag.search 로 옮긴 뒤 graph_rag 재수출이 빠진 심볼: {missing}"
    )
