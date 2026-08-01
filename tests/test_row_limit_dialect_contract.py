"""행 수 제한(상위 N)의 렌더 문법은 방언이 정한다.

빌더가 컬럼 문자열에 ``TOP n`` 을 박으면 그 SQL 은 그 엔진 전용이 된다 — 실DB 가 바뀌면
문법이 통째로 틀리는데 아무 테스트도 그것을 말해 주지 않는다. SelectAst 는 limit 을 **값으로**
들고, 위치(앞의 TOP vs 뒤의 LIMIT)는 방언 어댑터가 결정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import sql_dialect  # noqa: E402
from sql_ast import SelectAst, render_select_ast  # noqa: E402


def _ast(limit: int | None) -> SelectAst:
    return SelectAst(
        columns=["B.SIGUNGU AS SIGUNGU", "COUNT(DISTINCT B.MEMBER_NO) AS MEMBER_COUNT"],
        from_lines=["FROM CRM_MB_BASEINFO B"],
        where=["B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"],
        group_by=["B.SIGUNGU"],
        order_by=["COUNT(DISTINCT B.MEMBER_NO) DESC"],
        limit=limit,
    )


def test_tsql_renders_top_prefix() -> None:
    sql = render_select_ast(_ast(5), sql_dialect.get_dialect("tsql"))
    assert sql.splitlines()[0].startswith("SELECT TOP 5 ")
    assert "LIMIT" not in sql


def test_ansi_renders_limit_suffix() -> None:
    sql = render_select_ast(_ast(5), sql_dialect.get_dialect("ansi"))
    assert sql.splitlines()[0].startswith("SELECT B.SIGUNGU")
    assert sql.splitlines()[-1] == "LIMIT 5"


def test_mysql_and_postgres_use_the_suffix_form() -> None:
    for name in ("mysql", "postgres"):
        sql = render_select_ast(_ast(3), sql_dialect.get_dialect(name))
        assert sql.rstrip().endswith("LIMIT 3"), name
        assert " TOP " not in sql, name


def test_no_limit_renders_no_row_cap() -> None:
    """제한을 요청하지 않았으면 어떤 방언에서도 행 수 절이 나오면 안 된다."""
    for name in ("tsql", "ansi", "mysql", "postgres"):
        sql = render_select_ast(_ast(None), sql_dialect.get_dialect(name))
        assert " TOP " not in sql and "LIMIT" not in sql, name


def test_every_dialect_answers_both_positions() -> None:
    """새 방언을 추가하면서 한쪽만 구현하면 제한이 조용히 사라진다."""
    for name in ("tsql", "ansi", "mysql", "postgres"):
        dialect = sql_dialect.get_dialect(name)
        rendered = dialect.row_limit_prefix(7) + "|" + dialect.row_limit_suffix(7)
        assert "7" in rendered, f"{name} 방언이 행 수 제한을 어느 위치에도 렌더하지 않는다"


def test_builders_pass_the_execution_dialect_to_the_renderer() -> None:
    """방언 배선이 끊기면 기본값(ANSI)이라 MSSQL 대상에 LIMIT n 이 나간다 — 조용한 문법 오류.

    렌더러는 방언을 인자로만 알기 때문에, '전달했는가'를 확인하는 것은 이 배선뿐이다.
    """
    import graph_rag  # 지연 import — 이 파일의 다른 계약은 graph_rag 없이도 성립한다

    candidate = graph_rag._select_ast_candidate(
        "sql_template:dialect_probe", "방언 배선 확인", 1.0, _ast(5), "sql_template"
    )
    engine = graph_rag._member_dialect()
    expected_prefix = engine.row_limit_prefix(5)
    if expected_prefix:
        assert expected_prefix in candidate["sql"], (
            f"실행 방언({engine.name})의 행 수 제한 문법이 렌더된 SQL 에 없다 — 방언 전달이 끊겼다\n{candidate['sql']}"
        )
    else:
        assert candidate["sql"].rstrip().endswith(engine.row_limit_suffix(5))
