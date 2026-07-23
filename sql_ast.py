"""타겟 추출 SQL 의 AST(추상 구문 트리) + 렌더러 + 검증기.

파이프라인에서의 위치:
    사용자 질문 → 의도·복잡도 판별 → (Tool Calling 구조화 파싱) →
    단순 질의: 조건 → SelectAst / 복잡 질의: Query Plan → 빌더 → SelectAst →
    validate_select_ast(Validation) → render_select_ast(SQL Builder 최종 렌더) → SQL

빌더(graph_rag._sql_target_builders)는 문자열 join 대신 SelectAst 를 만들고, 렌더러가 기존과
바이트 단위로 동일한 SQL 텍스트를 생성한다(기존 259개 회귀 테스트가 렌더 동등성을 보장).
술어(leaf)는 원문 SQL 문자열로 둔다 — 구조(SELECT/FROM/JOIN/WHERE·AND 결합)가 AST 의 대상이고,
리프까지 완전 파싱하는 것은 이 시스템 범위에서 얻는 것보다 위험이 크다.

검증기(validate_select_ast)는 member_target_filters.json 의 `validation` 설정(예전엔 아무 코드도
안 읽던 죽은 설정)을 소비한다: 테이블 별칭 허용 목록, 최대 조건 수, OR 분기 수, raw SQL 토큰 금지.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 서브쿼리/조인 별칭 추출: "FROM <테이블> <별칭>" / "JOIN <테이블> <별칭>" (ON 앞).
_ALIAS_PATTERN = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+(?!ON\b|WHERE\b|GROUP\b|ORDER\b|HAVING\b)([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
# raw SQL 위험 토큰(allow_raw_sql=false 일 때 금지). 문자열 리터럴 안의 '--' 같은 오탐을 피하기 위해
# 따옴표 밖 토큰만 검사한다(_strip_string_literals).
_RAW_SQL_TOKENS = (";", "--", "/*", "xp_", "exec ", "execute ")


@dataclass
class SelectAst:
    """타겟 추출 SELECT 문 AST. 리프(컬럼/조인 행/술어)는 원문 SQL 문자열.

    columns: SELECT 목록(예: "B.MEMBER_NO AS CUST_ID"). distinct 는 SELECT DISTINCT 로 렌더.
    from_lines: 첫 줄 "FROM <표> <별칭>", 이후 조인 줄("     INNER JOIN ... ON ...") — 들여쓰기 포함 원문.
    where: AND 로 결합되는 술어 목록(각각 완결된 불리언 식; OR 는 술어 내부 괄호로 표현).
    group_by/having/order_by: 집계·랭킹 빌더용(없으면 빈 값).
    """

    columns: list[str]
    from_lines: list[str]
    where: list[str] = field(default_factory=list)
    distinct: bool = False
    group_by: list[str] = field(default_factory=list)
    having: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)


def render_select_ast(ast: SelectAst) -> str:
    """SelectAst → SQL 텍스트. 기존 빌더들의 '\n'.join 포맷과 동일한 모양을 유지한다."""
    lines = [("SELECT DISTINCT " if ast.distinct else "SELECT ") + ", ".join(ast.columns)]
    lines.extend(ast.from_lines)
    if ast.where:
        lines.append("WHERE " + "\n  AND ".join(ast.where))
    if ast.group_by:
        lines.append("GROUP BY " + ", ".join(ast.group_by))
    if ast.having:
        lines.append("HAVING " + "\n   AND ".join(ast.having))
    if ast.order_by:
        lines.append("ORDER BY " + ", ".join(ast.order_by))
    return "\n".join(lines)


def _strip_string_literals(sql: str) -> str:
    """따옴표 문자열 리터럴을 제거해 리터럴 내부 토큰('--' 등) 오탐을 막는다."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def collect_aliases(ast: SelectAst) -> set[str]:
    """FROM/JOIN(서브쿼리 포함)에 등장하는 테이블 별칭을 수집한다."""
    text = "\n".join([*ast.from_lines, *ast.where, *ast.having])
    return {match.group(2) for match in _ALIAS_PATTERN.finditer(text)}


def validate_select_ast(ast: SelectAst, config: dict[str, Any] | None) -> list[str]:
    """validation 설정 기준으로 AST 를 검증해 위반 메시지 목록을 돌려준다(빈 목록 = 통과).

    설정 키(member_target_filters.json "validation"):
      allowed_table_aliases: 허용 별칭 목록(벗어나면 위반)
      max_conditions: WHERE/HAVING 술어 수 상한
      max_or_branches: 술어 내부 OR 분기 수 상한
      allow_raw_sql: false 면 ';'/'--'/'/*' 등 raw SQL 토큰 금지
    설정이 없으면 검증 없이 통과한다(기존 동작 보존).
    """
    if not isinstance(config, dict):
        return []
    issues: list[str] = []

    allowed_aliases = config.get("allowed_table_aliases")
    if isinstance(allowed_aliases, list) and allowed_aliases:
        allowed = {str(alias).upper() for alias in allowed_aliases}
        for alias in collect_aliases(ast):
            if alias.upper() not in allowed:
                issues.append(f"허용되지 않은 테이블 별칭: {alias}")

    max_conditions = config.get("max_conditions")
    if isinstance(max_conditions, int) and max_conditions > 0:
        condition_count = len(ast.where) + len(ast.having)
        if condition_count > max_conditions:
            issues.append(f"조건 수 초과: {condition_count} > {max_conditions}")

    max_or = config.get("max_or_branches")
    if isinstance(max_or, int) and max_or > 0:
        or_count = sum(len(re.findall(r"\bOR\b", _strip_string_literals(p), re.IGNORECASE)) for p in [*ast.where, *ast.having])
        if or_count > max_or:
            issues.append(f"OR 분기 수 초과: {or_count} > {max_or}")

    if config.get("allow_raw_sql") is False:
        rendered = _strip_string_literals(render_select_ast(ast)).casefold()
        for token in _RAW_SQL_TOKENS:
            if token in rendered:
                issues.append(f"금지된 raw SQL 토큰: {token.strip()}")

    return issues
