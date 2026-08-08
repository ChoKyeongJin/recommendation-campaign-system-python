"""Boolean 합성 안전성 불변식 — 조각 하나가 AND 문맥에 들어가도 뜻이 변하지 않는다.

이 파일이 고정하는 것은 개별 사례가 아니라 **성질**이다. 개별 사례 테스트는 새 컴파일 경로가
생기면 같은 함정을 다시 판다 — 실제로 그렇게 됐다.

실측(2026-08-08, 라이브 id 55 ``사료, 간식, 장난감 중 하나라도 구매한 회원``)::

    EXISTS (SELECT 1 FROM ... WHERE OD.MEMBER_NO = B.MEMBER_NO
            AND ((…사료…)) OR ((…간식…)) OR ((…장난감…)))

SQL 의 ``AND`` 는 ``OR`` 보다 강하게 묶이므로 이 술어의 뜻은
``(상관 AND 사료) OR 간식 OR 장난감`` 이다. 둘째·셋째 항에 ``B.MEMBER_NO`` 가 없으니
**주문 상세에 간식이 한 줄이라도 있으면 모든 회원이 참**이 된다. 실DB(CRMDW) 실측에서 이
세그먼트는 69,308명 — 정상 상태 전체 회원 수와 정확히 같았다. 캠페인 발송 대상으로 나갔다면
전수 발송이다.

원인은 :func:`event_compiler._combine` 이 피연산자마다 괄호를 씌우면서 **합성 결과 자체는
감싸지 않은** 것이고, 그 조각을 받는 쪽(``plan.where`` → ``" AND ".join``)은 감쌀지 말지를
판단할 근거가 없었다. 그래서 불변식을 조각의 **결합도(precedence)** 로 고정한다.

    컴파일된 조건 조각은 AND 로 이어붙였을 때 뜻이 변하지 않아야 한다.
    즉 괄호 깊이 0 에 ``OR`` 이 남은 조각을 내지 않는다.

성질 검사는 문자열 모양이 아니라 **실행 결과**로 한다(sqlite 인메모리). 문자열만 보면
"괄호가 있다"는 것까지만 알 수 있고, 그 괄호가 옳은 자리인지는 알 수 없다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import date

import pytest

import event_compiler
import event_ir
from sql_dialect import get_dialect

# ── 고정 설비 ────────────────────────────────────────────────────────────────────

_AMOUNT = event_ir.FieldRef(name="purchase.amount")
# 원자 술어가 참이 되는 값. 트리마다 어느 가지가 참인지 달라져야 누수가 드러나므로
# 서로 다른 값 셋을 쓴다.
_ATOM_VALUES = (1, 2, 3)


def _registry() -> dict[str, event_compiler.EventSpec]:
    return event_compiler.resolve_registry({
        "purchase": event_compiler.EventSpec(
            table="orders", alias="EO", subject_key="MEMBER_NO",
            event_subject_key="MEMBER_NO", time_column="ORDER_DATE", time_format="date",
        ),
    })


def _fields(
    registry: dict[str, event_compiler.EventSpec],
) -> dict[str, event_compiler.FieldSpec]:
    return event_compiler.resolve_fields(registry, {
        "purchase.amount": event_compiler.FieldSpec(
            source="purchase", column="AMT", data_type="number"
        ),
        # 주체 컬럼 — 관계 스코프 없이 조각만 컴파일하는 구조 검사용.
        "subject.grade": event_compiler.FieldSpec(
            source="subject", column="GRADE", data_type="number"
        ),
    })


def _compile(expression: event_ir.Condition) -> str:
    registry = _registry()
    return event_compiler.compile_expression_sql(
        expression,
        registry=registry,
        fields=_fields(registry),
        dialect=get_dialect("ansi"),
        today=date(2026, 8, 8),
    )


def _atom(value: int) -> event_ir.Comparison:
    return event_ir.Comparison(
        left=_AMOUNT, operator="=", right=event_ir.Literal(value=value)
    )


def _subject_atom(value: int) -> event_ir.Comparison:
    return event_ir.Comparison(
        left=event_ir.FieldRef(name="subject.grade"),
        operator="=",
        right=event_ir.Literal(value=value),
    )


# ── 조건 트리 전수 생성 ──────────────────────────────────────────────────────────
#
# 무작위 표본(hypothesis)이 아니라 **전수 열거**를 쓴다. 이 저장소에 hypothesis 의존이
# 없기도 하고(§56), 경계가 작아 전수가 가능하며, 전수는 재현 가능하다(§63) — 실패한
# 트리가 실행마다 달라지지 않는다.


def _trees(
    depth: int, atom: Callable[[int], event_ir.Comparison] = _atom
) -> Iterator[event_ir.Condition]:
    """깊이 ``depth`` 이하의 And/Or/Not/Comparison 트리를 전부 만든다."""
    atoms: list[event_ir.Condition] = [atom(value) for value in _ATOM_VALUES]
    if depth <= 0:
        yield from atoms
        return
    smaller = list(_trees(depth - 1, atom))
    yield from smaller
    for left in smaller:
        yield event_ir.Not(operand=left)
        for right in smaller:
            yield event_ir.And(operands=(left, right))
            yield event_ir.Or(operands=(left, right))
    # 3항 분기는 2항 중첩과 렌더가 다르다(#55 가 3항이었다).
    yield event_ir.Or(operands=tuple(atoms))
    yield event_ir.And(operands=tuple(atoms))


def _reference(node: event_ir.Condition, amount: int | None) -> bool:
    """행 하나에 대한 트리의 뜻(파이썬 기준 구현).

    ``Not`` 은 2값이다 — audience 보수는 "술어가 참이 아닌 행"이고 컴파일러도 CASE 로
    2값화한다. 그래서 NULL 행에서 ``Not(비교)`` 는 참이다.
    """
    if isinstance(node, event_ir.Comparison):
        assert isinstance(node.right, event_ir.Literal)
        return amount is not None and amount == node.right.value
    if isinstance(node, event_ir.Not):
        return not _reference(node.operand, amount)
    if isinstance(node, event_ir.And):
        return all(_reference(item, amount) for item in node.operands)
    if isinstance(node, event_ir.Or):
        return any(_reference(item, amount) for item in node.operands)
    raise AssertionError(f"테스트가 만들지 않는 노드입니다: {node!r}")


# 회원 → 그 회원의 주문 금액들. 회원 4 는 주문이 하나도 없다(상관 누수의 표지 —
# 주문이 없는 회원이 매칭되면 상관 술어가 OR 밖으로 샌 것이다).
_MEMBERS: dict[int, tuple[int | None, ...]] = {
    1: (1,),
    2: (2,),
    3: (3, None),
    4: (),
}


def _expected_members(node: event_ir.Condition) -> list[int]:
    return sorted(
        member
        for member, amounts in _MEMBERS.items()
        if any(_reference(node, amount) for amount in amounts)
    )


@pytest.fixture(name="connection")
def _connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE members (MEMBER_NO INTEGER)")
        connection.execute("CREATE TABLE orders (MEMBER_NO INTEGER, AMT INTEGER)")
        connection.executemany(
            "INSERT INTO members VALUES (?)", [(member,) for member in _MEMBERS]
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [
                (member, amount)
                for member, amounts in _MEMBERS.items()
                for amount in amounts
            ],
        )
        yield connection
    finally:
        connection.close()


def _matched(connection: sqlite3.Connection, sql: str) -> list[int]:
    rows = connection.execute(
        f"SELECT MEMBER_NO FROM members B WHERE {sql} ORDER BY MEMBER_NO"
    ).fetchall()
    return [row[0] for row in rows]


def _correlated_exists(where: event_ir.Condition) -> event_ir.Condition:
    return event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="purchase", correlation="subject"), where=where
        )
    )


# ── 불변식 ───────────────────────────────────────────────────────────────────────


def test_disjunctive_filter_keeps_the_correlation_inside_every_branch(
    connection: sqlite3.Connection,
) -> None:
    """#55 회귀 — 최상위가 ``Or`` 인 ``Filter`` 에서 상관 술어가 새지 않는다.

    주문이 하나도 없는 회원 4 가 매칭되면 누수다. 실DB 에서 이 누수는 세그먼트를
    전체 회원(69,308명)으로 만들었다.
    """
    expression = _correlated_exists(
        event_ir.Or(operands=(_atom(1), _atom(2), _atom(3)))
    )

    matched = _matched(connection, _compile(expression))

    assert 4 not in matched, "주문이 없는 회원이 매칭됐다 — 상관 술어가 OR 밖으로 샜다"
    assert matched == [1, 2, 3]


def test_every_boolean_tree_keeps_its_meaning_inside_a_conjunction(
    connection: sqlite3.Connection,
) -> None:
    """전수 트리 — 컴파일된 SQL 의 실행 결과가 기준 구현과 같다.

    ``Filter`` 는 조건을 상관 술어와 **AND 로** 잇는다. 그래서 이 검사는 곧
    "조각을 AND 문맥에 넣어도 뜻이 변하지 않는가"다.
    """
    trees = list(_trees(2))
    assert len(trees) > 100, "전수 열거가 비었다 — 생성기가 깨졌다"

    mismatched: list[tuple[str, list[int], list[int]]] = []
    for tree in trees:
        sql = _compile(_correlated_exists(tree))
        matched = _matched(connection, sql)
        expected = _expected_members(tree)
        if matched != expected:
            mismatched.append((sql, matched, expected))

    assert not mismatched, "\n".join(
        f"{sql}\n  실행={matched} 기대={expected}"
        for sql, matched, expected in mismatched[:5]
    )


def _aggregate_over(relation: event_ir.Relation) -> event_ir.Condition:
    return event_ir.Comparison(
        left=event_ir.Aggregate(function="count", expression=None, relation=relation),
        operator=">=",
        right=event_ir.Literal(value=3),
    )


@pytest.mark.parametrize(
    ("label", "shape", "optimize"),
    [
        # 네 모양은 전부 compile_relation(Filter) 하나를 지난다. 사이트가 넷으로 보여도
        # 뿌리는 하나이므로 고치는 자리도 하나다 — 이 파라미터가 그 사실을 고정한다.
        ("exists", lambda relation: event_ir.Exists(relation=relation), True),
        (
            "not_exists",
            lambda relation: event_ir.Not(operand=event_ir.Exists(relation=relation)),
            True,
        ),
        ("correlated_scalar_aggregate", _aggregate_over, False),
        ("membership_lowering", _aggregate_over, True),
    ],
)
def test_disjunction_stays_bracketed_in_every_correlated_shape(
    label: str,
    shape: Callable[[event_ir.Relation], event_ir.Condition],
    optimize: bool,
) -> None:
    """상관 서브쿼리를 만드는 **모든** 모양에서 분기가 괄호 안에 남는다.

    ``Not(Exists(...))`` 의 누수는 방향만 반대다 — 세그먼트가 전체가 아니라 **공집합**이
    된다. 조용한 오답이라는 점은 같다. 상관 스칼라 집계는 COUNT 가 팩트 테이블 전체를
    세게 된다.
    """
    registry = _registry()
    context = event_compiler.CompileContext(
        subject=event_compiler.SubjectSpec(), registry=registry, fields=_fields(registry),
        dialect=get_dialect("ansi"), literals=True, today=date(2026, 8, 8),
        optimize_aggregate_membership=optimize,
    )
    relation = event_ir.Filter(
        relation=event_ir.Source(name="purchase", correlation="subject"),
        where=event_ir.Or(operands=(_atom(1), _atom(2), _atom(3))),
    )

    sql = event_compiler.compile_condition(shape(relation), context).sql

    assert not event_compiler.has_top_level_disjunction(sql), f"{label}: {sql}"
    # 상관 술어와 분기가 같은 괄호 깊이에서 만나면 안 된다.
    assert "= B.MEMBER_NO AND (EO.AMT = 1) OR" not in sql, f"{label}: {sql}"


def test_no_fragment_enters_a_conjunction_with_a_bare_disjunction() -> None:
    """구조 불변식 — **AND 문맥에 넣은 뒤**의 조각에는 깊이 0 의 ``OR`` 이 없다.

    실행 결과 검사(위)가 뜻을 보장하고, 이 검사는 **왜** 보장되는지를 고정한다. 불변식을
    조각 자체가 아니라 삽입 지점에 거는 이유는 설계가 그렇기 때문이다: 조각은 잉여 괄호 없이
    결합도만 싣고 다니고, 괄호는 :func:`event_compiler.as_conjunct` 가 필요할 때만 씌운다.
    ``_combine`` 이 결합도 선언을 빠뜨리면 이 검사가 즉시 깨진다.
    """
    registry = _registry()
    context = event_compiler.CompileContext(
        subject=event_compiler.SubjectSpec(), registry=registry, fields=_fields(registry),
        dialect=get_dialect("ansi"), literals=True, today=date(2026, 8, 8),
    )

    offenders = [
        conjunct
        for tree in _trees(2, _subject_atom)
        if event_compiler.has_top_level_disjunction(
            conjunct := event_compiler.as_conjunct(
                event_compiler.compile_condition(tree, context)
            )
        )
    ]

    assert not offenders, "AND 로 이어붙이면 뜻이 바뀌는 조각:\n" + "\n".join(offenders[:5])


def test_relation_where_entries_are_all_safe_to_and_join() -> None:
    """실제 누수 지점 고정 — ``RelationPlan.where`` 의 모든 항목이 AND 결합에 안전하다.

    이 목록은 :func:`event_compiler._subquery` 에서 ``" AND ".join`` 으로 이어붙는다.
    항목 하나라도 깊이 0 의 ``OR`` 을 가지면 그 자리에서 상관 술어가 샌다 — #55 가 정확히
    이것이었다.
    """
    registry = _registry()
    context = event_compiler.CompileContext(
        subject=event_compiler.SubjectSpec(), registry=registry, fields=_fields(registry),
        dialect=get_dialect("ansi"), literals=True, today=date(2026, 8, 8),
    )

    offenders: list[str] = []
    for tree in _trees(2):
        plan = event_compiler.compile_relation(
            event_ir.Filter(
                relation=event_ir.Source(name="purchase", correlation="subject"), where=tree
            ),
            context,
        )
        offenders.extend(
            entry for entry in plan.where if event_compiler.has_top_level_disjunction(entry)
        )

    assert not offenders, "WHERE 목록에 AND 결합이 위험한 항목:\n" + "\n".join(offenders[:5])


def test_top_level_disjunction_scanner_ignores_parentheses_and_literals() -> None:
    """스캐너 자체의 계약 — 이 검사가 틀리면 위 불변식이 조용히 무력해진다."""
    assert event_compiler.has_top_level_disjunction("A = 1 OR B = 2")
    assert event_compiler.has_top_level_disjunction("(A = 1) OR (B = 2)")
    assert not event_compiler.has_top_level_disjunction("((A = 1) OR (B = 2))")
    assert not event_compiler.has_top_level_disjunction("A = 1 AND B = 2")
    # 문자열 리터럴 안의 OR 은 연산자가 아니다.
    assert not event_compiler.has_top_level_disjunction("NAME = 'X OR Y'")
    assert not event_compiler.has_top_level_disjunction("NAME = N'A OR B'")
    # 이스케이프된 따옴표('')가 리터럴을 닫는 것으로 오독되면 안 된다.
    assert not event_compiler.has_top_level_disjunction("NAME = 'it''s OR here'")
    # 식별자의 일부인 OR 은 연산자가 아니다.
    assert not event_compiler.has_top_level_disjunction("ORDER_ID = 1")
    assert not event_compiler.has_top_level_disjunction("A = 1 AND SPONSOR = 2")


def test_compiled_condition_declares_its_precedence() -> None:
    """결합도는 조각에 실려 다닌다 — 받는 쪽이 문자열을 다시 파싱하지 않는다."""
    registry = _registry()
    context = event_compiler.CompileContext(
        subject=event_compiler.SubjectSpec(),
        registry=registry,
        fields=_fields(registry),
        dialect=get_dialect("ansi"),
        literals=True,
        today=date(2026, 8, 8),
    )

    disjunction = event_compiler.compile_condition(
        event_ir.Or(operands=(_subject_atom(1), _subject_atom(2))), context
    )
    conjunction = event_compiler.compile_condition(
        event_ir.And(operands=(_subject_atom(1), _subject_atom(2))), context
    )

    assert disjunction.precedence is event_compiler.SqlPrecedence.OR
    assert conjunction.precedence is event_compiler.SqlPrecedence.AND
    # AND 문맥에 넣을 때만 괄호가 붙는다 — 조각 자체는 잉여 괄호를 갖지 않는다(기존 SQL 보존).
    assert event_compiler.as_conjunct(disjunction) == f"({disjunction.sql})"
    assert event_compiler.as_conjunct(conjunction) == conjunction.sql
