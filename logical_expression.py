"""논리식(AND/OR/GROUP/PREDICATE) 컴파일 계층 — 순수 구조 모듈.

임계값과 서로 다른 지표가 섞인 OR-of-conjunctions("(로그인>=100) OR (구매>=10 AND 마케팅동의)")를 괄호와
AND/OR 의미를 보존한 하나의 SQL 로 컴파일하기 위한 뼈대다. 이 모듈은 **불리언 구조만** 소유한다:
  - 논리식 AST(Or/And/Leaf) 파싱: 명시적 괄호 보존, AND 가 OR 보다 우선(파서 규칙).
  - 각 Leaf(원자 조건 텍스트)를 도메인 컴파일러 콜백으로 SQL fragment 로 바꿔 괄호/AND/OR 로 조립.
  - 조립 결과(fragment 템플릿 + params + 참조 predicate)와 입력 AST 의 의미 대조(검증).

도메인 파싱/컴파일(로그인/구매/회원속성/마케팅/장바구니 → Predicate)은 graph_rag 가 콜백으로 주입한다 —
이 모듈은 어떤 지표도 모른다. 정규식 분리 순서·기존 빌더의 승자 선택에 의존하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ── AST 노드 ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Leaf:
    """원자 조건(더 이상 AND/OR 로 나뉘지 않는 절)의 원문 텍스트."""
    text: str


@dataclass(frozen=True)
class And:
    """AND 결합(2개 이상). AND 가 OR 보다 우선한다."""
    operands: tuple[Any, ...]


@dataclass(frozen=True)
class Or:
    """OR 결합(2개 이상)."""
    operands: tuple[Any, ...]


class ParseError(Exception):
    """괄호 불균형·빈 피연산자·예기치 못한 토큰 등 논리식 파싱 실패."""


class LeafUnsupported(Exception):
    """Leaf 를 지원되는 Predicate 로 컴파일하지 못함(fail-close 트리거)."""

    def __init__(self, text: str, reason: str) -> None:
        super().__init__(f"{reason}: {text!r}")
        self.text = text
        self.reason = reason


# ── 토크나이저 + 재귀 하강 파서 ────────────────────────────────────────────────
def _tokenize(text: str, or_re: "re.Pattern[str]", and_re: "re.Pattern[str]") -> list[tuple[str, str]]:
    """텍스트를 [('TEXT'|'OR'|'AND'|'LP'|'RP', 원문)] 토큰열로 나눈다. OR 를 AND 보다 먼저 시도(길이 무관하게
    OR 연결어가 AND 연결어의 접두여도 OR 로 본다 — '이거나' vs '이고')."""
    tokens: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(text)

    def flush() -> None:
        chunk = "".join(buf).strip()
        if chunk:
            tokens.append(("TEXT", chunk))
        buf.clear()

    while i < n:
        ch = text[i]
        if ch in "()":
            flush()
            tokens.append(("LP" if ch == "(" else "RP", ch))
            i += 1
            continue
        m_or = or_re.match(text, i)
        if m_or is not None:
            flush()
            tokens.append(("OR", m_or.group(0)))
            i = m_or.end()
            continue
        m_and = and_re.match(text, i)
        if m_and is not None:
            flush()
            tokens.append(("AND", m_and.group(0)))
            i = m_and.end()
            continue
        buf.append(ch)
        i += 1
    flush()
    return tokens


def parse(text: str, or_re: "re.Pattern[str]", and_re: "re.Pattern[str]") -> Any:
    """텍스트 → 논리식 AST. AND 가 OR 보다 우선. 괄호 명시 보존. 실패 시 ParseError.

    문법(재귀 하강):
      expr    := or_expr
      or_expr := and_expr (OR and_expr)*
      and_expr:= term (AND term)*
      term    := '(' or_expr ')' | TEXT
    """
    tokens = _tokenize(text, or_re, and_re)
    pos = 0

    def peek() -> str | None:
        return tokens[pos][0] if pos < len(tokens) else None

    def advance() -> tuple[str, str]:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_or() -> Any:
        nodes = [parse_and()]
        while peek() == "OR":
            advance()
            nodes.append(parse_and())
        return nodes[0] if len(nodes) == 1 else Or(tuple(nodes))

    def parse_and() -> Any:
        nodes = [parse_term()]
        while peek() == "AND":
            advance()
            nodes.append(parse_term())
        return nodes[0] if len(nodes) == 1 else And(tuple(nodes))

    def parse_term() -> Any:
        tok = peek()
        if tok == "LP":
            advance()
            node = parse_or()
            if peek() != "RP":
                raise ParseError("괄호 불균형: 닫는 괄호 누락")
            advance()
            return node
        if tok == "TEXT":
            return Leaf(advance()[1])
        if tok in ("OR", "AND"):
            raise ParseError(f"피연산자 없는 연결어: {tok}")
        raise ParseError("빈 피연산자 또는 예기치 못한 토큰")

    if not tokens:
        raise ParseError("빈 논리식")
    result = parse_or()
    if pos != len(tokens):
        raise ParseError("남은 토큰(괄호 불균형/여분 닫는 괄호 의심)")
    return result


# ── 구조 통계 ─────────────────────────────────────────────────────────────────
def has_or(node: Any) -> bool:
    """AST 에 OR 노드가 하나라도 있으면 True(단일 조건·순수 AND 은 False)."""
    if isinstance(node, Or):
        return True
    if isinstance(node, And):
        return any(has_or(c) for c in node.operands)
    return False


def iter_leaves(node: Any):
    if isinstance(node, Leaf):
        yield node
    elif isinstance(node, (And, Or)):
        for c in node.operands:
            yield from iter_leaves(c)


def structure_signature(node: Any) -> Any:
    """AST 의 형태(연결자·중첩·leaf 위치)를 튜플로 직렬화 — 검증에서 조립 구조와 대조."""
    if isinstance(node, Leaf):
        return ("LEAF",)
    if isinstance(node, And):
        return ("AND", tuple(structure_signature(c) for c in node.operands))
    if isinstance(node, Or):
        return ("OR", tuple(structure_signature(c) for c in node.operands))
    raise TypeError(f"알 수 없는 노드: {node!r}")


# ── Leaf 컴파일 결과 + 조립 ────────────────────────────────────────────────────
@dataclass
class LeafCompile:
    """graph_rag 콜백이 Leaf 하나를 컴파일한 결과.

    fragment: 회원 테이블(B) 상관 불리언 SQL(바인드 자리표시자 `@name` 사용). AND 다중 predicate 는 이미
              괄호로 묶여 반환될 수 있다. params: {자리표시자: 숫자값}. predicates: 참조된 원자 조건 메타 목록
              ({domain, metric, operator, value, ...})."""
    fragment: str
    params: dict[str, Any] = field(default_factory=dict)
    predicates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Assembled:
    fragment: str                       # 자리표시자 포함 최종 불리언 SQL(WHERE 절 본문)
    params: dict[str, Any]              # {자리표시자: 값} — 분기 간 이름 충돌 없음
    predicates: list[dict[str, Any]]    # 참조 predicate 메타(순서 = leaf 등장 순서)
    structure: Any                      # 조립 시 구성한 구조 서명(검증용)
    ast: Any


def assemble(node: Any, compile_leaf: Callable[[str, str], LeafCompile]) -> Assembled:
    """AST 를 하나의 불리언 fragment 로 조립한다. 각 Leaf 는 compile_leaf(text, prefix) 로 독립 컴파일하며,
    한 leaf 라도 LeafUnsupported 면 그대로 전파해 **fail-close**(부분 SQL 생성 금지). 각 leaf 에 고유 prefix 를
    주어 분기 간 파라미터 이름이 충돌하지 않게 한다. AND→'(a AND b)', OR→'(a OR b)' 로 괄호를 보존한다."""
    params: dict[str, Any] = {}
    predicates: list[dict[str, Any]] = []
    counter = [0]

    def walk(nd: Any) -> str:
        if isinstance(nd, Leaf):
            prefix = f"L{counter[0]}_"
            counter[0] += 1
            result = compile_leaf(nd.text, prefix)  # LeafUnsupported 전파(fail-close)
            for name, value in result.params.items():
                if name in params:
                    raise ParseError(f"파라미터 이름 충돌: {name}")
                params[name] = value
            predicates.extend(result.predicates)
            return result.fragment
        if isinstance(nd, And):
            return "(" + " AND ".join(walk(c) for c in nd.operands) + ")"
        if isinstance(nd, Or):
            return "(" + " OR ".join(walk(c) for c in nd.operands) + ")"
        raise TypeError(f"알 수 없는 노드: {nd!r}")

    fragment = walk(node)
    return Assembled(fragment=fragment, params=params, predicates=predicates,
                     structure=structure_signature(node), ast=node)


# ── 바인드 자리표시자 → 인라인 렌더(실행 SQL 생성) ─────────────────────────────
_PLACEHOLDER_RE = re.compile(r"@([A-Za-z0-9_]+)")


def render_inline(fragment: str, params: dict[str, Any], format_value: Callable[[Any], str]) -> str:
    """바인드 자리표시자(@name)를 값으로 치환해 실행 가능한 인라인 SQL 로 만든다. 값은 format_value 로 렌더
    (숫자 임계값 전용 — 문자열/열거값은 fragment 에 이미 인라인)."""
    def sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in params:
            raise ParseError(f"미바인드 파라미터: @{name}")
        return format_value(params[name])
    return _PLACEHOLDER_RE.sub(sub, fragment)


# ── 의미 검증(입력 AST ↔ 조립 결과) ───────────────────────────────────────────
def verify(assembled: Assembled, format_value: Callable[[Any], str]) -> list[str]:
    """입력 AST 와 조립 SQL 의 의미를 대조한다. 불일치 목록을 반환(비면 통과).

    검증: ①구조 서명 == AST 서명(OR/AND/괄호 경계·leaf 수 보존), ②leaf 수 == predicate 수(누락/중복 없음),
    ③각 predicate 의 임계값이 자기 자리표시자에 바인드되고 렌더값이 인라인 SQL 에 나타남(임계값이 다른 지표로
    이동하지 않음), ④모든 자리표시자가 바인드됨."""
    issues: list[str] = []

    if assembled.structure != structure_signature(assembled.ast):
        issues.append("구조 불일치: 조립 구조가 입력 AST 와 다름(OR/AND/괄호 경계 훼손)")

    leaf_count = sum(1 for _ in iter_leaves(assembled.ast))
    if leaf_count == 0:
        issues.append("leaf 없음")
    if len(assembled.predicates) < leaf_count:
        issues.append(f"조건 누락: leaf {leaf_count}개 중 predicate {len(assembled.predicates)}개만 컴파일")

    inline = render_inline(assembled.fragment, assembled.params, format_value)

    # 자리표시자 무결성: 모든 @name 이 params 에 있어야 하고, params 는 모두 fragment 에서 참조돼야 한다.
    used = set(_PLACEHOLDER_RE.findall(assembled.fragment))
    unbound = used - set(assembled.params)
    if unbound:
        issues.append(f"미바인드 파라미터: {sorted(unbound)}")
    orphan = set(assembled.params) - used
    if orphan:
        issues.append(f"참조되지 않는 파라미터: {sorted(orphan)}")

    # predicate 별 임계값 추적: 값 지정 predicate 는 자기 값이 인라인 SQL 에 나타나야 한다(지표 이동 방지).
    for pred in assembled.predicates:
        value = pred.get("value")
        if value is None:
            continue
        rendered = format_value(value)
        operator = pred.get("operator")
        needle = f"{operator} {rendered}" if operator else rendered
        if needle not in inline and rendered not in inline:
            issues.append(f"임계값 소실/이동: {pred.get('metric') or pred.get('domain')} {needle}")

    return issues
