"""정의됐지만 아무도 부르지 않는 최상위 공개 심볼을 기계로 센다.

배경
----
설정 키와 같은 결함이 코드에도 있다: 함수·클래스·상수를 만들고 **부르는 곳을 안 붙인다.**
증상은 예외가 아니라 침묵이라, 다음 사람은 그 심볼이 살아 있는 경로라고 믿고 고친다.
실제로 이 저장소에서는 재조정 파이프라인 한 벌(4개 진입점), 외부조건 싱글턴, 슬롯 감사
로그의 대체 함수가 그 상태였다.

무엇을 세나
----------
파일별 AST 를 읽어 **모듈 최상위**의 공개 심볼(``_`` 로 시작하지 않는 함수·클래스, 대문자
상수)을 모으고, 저장소 전체에서 그 이름이 참조되는지 센다. 참조는 셋 다 본다:

* ``Name`` / ``Attribute`` 노드 (``foo()`` · ``mod.foo``)
* ``import`` 별칭 (``from mod import foo``)
* 문자열 리터럴 (``getattr(mod, "foo")`` · ``__all__`` 재수출 · 설정이 이름으로 가리키는 경우)

정의 자체는 참조로 세지 않는다(대입 좌변 1회만 보정하면 된다 — 함수·클래스 이름은 ``Name``
노드가 아니라 문자열 속성이라 애초에 안 잡힌다).

분류
----
``dead``      : 자기 파일 안에서도 밖에서도 참조 0
``test_only`` : 프로덕션 참조 0, 테스트만 참조

데코레이터가 붙은 정의(FastAPI 라우트 · ``@lru_cache`` 등)는 이름으로 안 불려도 배선돼 있으므로
``dead`` 후보에서 뺀다. 진입점 스크립트(``tools/`` · ``build_*.py`` 등 ``__main__`` 을 가진 모듈)도
같은 이유로 뺀다.
"""

from __future__ import annotations

import ast
import functools
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git", "artifacts", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
}


def _python_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*.py"):
        if set(path.relative_to(REPO_ROOT).parts) & SKIP_DIRS:
            continue
        files.append(path)
    return sorted(files)


def _parse_all() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in _python_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            trees[relative] = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return trees


def _has_main_guard(tree: ast.Module) -> bool:
    """``if __name__ == "__main__":`` 을 가진 모듈은 CLI 진입점이다."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            return True
    return False


def _top_level_definitions(tree: ast.Module) -> dict[str, tuple[str, int, bool]]:
    """{이름: (종류, 줄번호, 데코레이터 유무)}."""
    found: dict[str, tuple[str, int, bool]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                found[node.name] = ("func", node.lineno, bool(node.decorator_list))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                found[node.name] = ("class", node.lineno, bool(node.decorator_list))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper() and not target.id.startswith("_"):
                    found[target.id] = ("const", node.lineno, False)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id.isupper() and not target.id.startswith("_"):
                found[target.id] = ("const", node.lineno, False)
    return found


def _reference_counts(tree: ast.Module) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            counts[node.id] += 1
        elif isinstance(node, ast.Attribute):
            counts[node.attr] += 1
        elif isinstance(node, ast.alias):
            counts[node.name.split(".")[-1]] += 1
            if node.asname:
                counts[node.asname] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if 0 < len(node.value) <= 90:
                counts[node.value] += 1
    return counts


@functools.lru_cache(maxsize=1)
def _scan_cached() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(dead, test_only). 한 프로세스 안에서 전체 AST 재파싱을 반복하지 않는다."""
    result = _scan()
    return tuple(result["dead"]), tuple(result["test_only"])


def scan() -> dict[str, Any]:
    """호출자가 마음대로 다뤄도 되는 새 dict(캐시는 불변 튜플로 보관한다)."""
    dead, test_only = _scan_cached()
    return {
        "dead": list(dead),
        "test_only": list(test_only),
        "dead_total": len(dead),
        "test_only_total": len(test_only),
    }


def _scan() -> dict[str, Any]:
    trees = _parse_all()
    references = {relative: _reference_counts(tree) for relative, tree in trees.items()}

    dead: list[str] = []
    test_only: list[str] = []
    for relative, tree in trees.items():
        if relative.startswith("tests/") or _has_main_guard(tree):
            continue
        for name, (kind, _lineno, decorated) in _top_level_definitions(tree).items():
            if decorated:
                continue  # 라우트·캐시 등 데코레이터가 배선을 소유한다(이름으로 안 불려도 살아 있다).
            own = references[relative].get(name, 0)
            if kind == "const":
                own -= 1  # 대입 좌변(Store Name) 1회 제외
            if own > 0:
                continue
            outside = {
                other: count
                for other, counts in references.items()
                if other != relative and (count := counts.get(name, 0))
            }
            production = {other for other in outside if not other.startswith("tests/")}
            if production:
                continue
            symbol = f"{relative}::{name}"
            if outside:
                test_only.append(symbol)
            else:
                dead.append(symbol)

    return {
        "dead": sorted(dead),
        "test_only": sorted(test_only),
        "dead_total": len(dead),
        "test_only_total": len(test_only),
    }


def main() -> None:
    print(json.dumps(scan(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
