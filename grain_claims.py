"""임계값의 **적용 grain** — 한 행에 걸리는가, 주체 전체 집계에 걸리는가.

같은 금액, 같은 기간, 같은 비교 연산자인데 뜻이 다른 두 문장이 있다::

    최근 90일 동안 총 구매금액이 30만원 이상인 회원          → 회원별 합계 (subject)
    최근 90일 동안 구매금액이 30만원 이상인 주문을 한 회원    → 그런 주문의 존재 (row)

실측(2026-08-08, 라이브 id 42/43)에서 두 문장은 **바이트 동일한 SQL** 을 냈다(둘 다
``SUM(EO.PAYMENT_AMT) >= 300000``). 실DB(CRMDW)에서 두 해석의 크기는 이렇게 갈린다::

    합계 >= 30만 (출고된 것)      9,585명
    단일 주문 >= 30만 (요청한 것)    688명

14배다. 임계값에 값과 단위만 싣고 **적용 grain** 을 싣지 않으면 이 차이가 어디에서도
드러나지 않는다.

이 모듈의 자리와 책임
---------------------
표면어를 읽는 것은 여기까지다. 이 모듈은 원문에서 grain 을 **주장(claim)** 으로 뽑고,
Event IR 트리가 실제로 **실현한** grain 을 순수 IR 검사로 읽고, 둘이 어긋날 때 트리를
IR→IR 로 낮춘다. 컴파일러와 판정자는 이 모듈이 만든 typed 값만 본다 — 한국어 문자열을
다시 읽지 않는다.

    원문  →  detect_grain_claims        (표면 · 여기)
    IR    →  expression_grains          (순수 IR · 여기)
    IR    →  regrain_to_row             (순수 IR→IR · 여기)

표면어 목록을 이 파일에 새로 적지 않는다. 필요한 어휘는 이미 선언돼 있다 —
``aggregate_sum_marker``(합계·총액·총합·누적·총), ``change_inclusive_bound``(이상·이하),
``change_strict_bound``(초과·미만), ``event_count_unit``(건·회·번·개), ``event_alias_*``.
:mod:`lexicon_patterns` 가 그 목록의 단일 소유자다.

row grain 을 알아보는 규칙이 왜 범용인가
----------------------------------------
임계 뒤에 오는 **머리(head)** 가 사건 명사이거나 계수 단위이면, 그 임계는 사건 하나를
서술한다("…이상인 **주문**", "…이상인 **건**"). 머리가 사건이 아니라면 서술 대상은 주체다.
이 규칙은 '주문'에 대한 규칙이 아니라 **머리에 대한 규칙**이라, 새 사건이 카탈로그에
등록되면(``event_alias_<event>``) 규칙을 고치지 않아도 함께 열린다.

확정할 수 없으면 주장을 만들지 않는다(§12). 주장이 없으면 이 모듈은 아무것도 바꾸지
않는다 — 기존 동작이 그대로 유지된다. 모호한 자리에 기본값을 넣는 것이 곧 추측이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import event_ir
import lexicon_patterns

#: 임계가 사건 한 건(행)에 걸린다.
ROW = "row"
#: 임계가 주체(회원) 단위 집계에 걸린다.
SUBJECT = "subject"

#: 관형형 어미 — 임계 표현과 머리 명사를 잇는다("30만원 이상**인** 주문").
_ADNOMINAL = ("인", "의", "한", "된")


@dataclass(frozen=True)
class GrainClaim:
    """원문이 말한 grain 하나. ``source_span`` 은 그 주장의 근거 구간이다."""

    grain: str
    source_span: tuple[int, int]
    evidence_text: str
    #: 주장을 만든 머리 낱말(row) 또는 집계 표지(subject). 진단 문구가 이 값을 쓴다.
    head: str


def _bound_alternation() -> str:
    return lexicon_patterns.alternation("change_inclusive_bound", "change_strict_bound")


def _row_head_terms() -> tuple[str, ...]:
    """임계의 머리가 되면 row grain 을 뜻하는 낱말 — 사건 명사와 계수 단위.

    ``event_alias_*`` 를 통째로 쓰는 이유는 이 규칙이 특정 사건의 규칙이 아니기 때문이다.
    새 사건 별칭이 어휘에 등록되면 규칙을 고치지 않아도 같이 열린다.
    """
    terms: list[str] = list(lexicon_patterns.vocabulary("event_count_unit"))
    for name in lexicon_patterns.vocabulary_names():
        if name.startswith("event_alias_"):
            terms.extend(lexicon_patterns.vocabulary(name))
    # 긴 낱말 우선(부분 매치 방지). 중복 제거는 순서를 지키며 한다.
    return tuple(sorted(dict.fromkeys(terms), key=lambda word: (-len(word), word)))


def _row_pattern() -> re.Pattern[str] | None:
    """``<임계 경계어> <관형형> <머리>`` — 머리 뒤에는 조사나 절 경계만 올 수 있다.

    오른쪽 경계가 없으면 머리가 **더 긴 낱말의 첫 음절**로 잡힌다. 실측: ``이상인 회원``
    에서 계수 단위 ``회`` 가 ``회원`` 의 앞 음절에 붙어 row 주장이 잘못 섰다. 한글에는
    ``\\b`` 가 없으므로(:data:`lexicon_patterns.HANGUL_LEFT_BOUNDARY` 의 같은 이유) 경계를
    **조사 목록**으로 적는다 — ``주문을`` 은 통과하고 ``회원`` 은 걸린다.
    """
    bounds = _bound_alternation()
    heads = "|".join(re.escape(term) for term in _row_head_terms())
    if not bounds or not heads:
        return None
    adnominal = "|".join(_ADNOMINAL)
    particles = lexicon_patterns.alternation("bound_particle")
    trailing = rf"(?:{particles}|\s|[,.·]|$)" if particles else r"(?:\s|[,.·]|$)"
    return re.compile(
        rf"(?P<bound>{bounds})\s*(?:{adnominal})\s*(?P<head>{heads})(?={trailing})"
    )


def _subject_pattern() -> re.Pattern[str] | None:
    markers = lexicon_patterns.alternation("aggregate_sum_marker")
    if not markers:
        return None
    return re.compile(rf"{lexicon_patterns.HANGUL_LEFT_BOUNDARY}(?P<head>{markers})")


def detect_grain_claims(query: str) -> tuple[GrainClaim, ...]:
    """원문이 **명시한** grain 주장들. 말하지 않았으면 빈 튜플이다.

    빈 튜플은 "주체 집계"라는 뜻이 아니라 "원문이 정하지 않았다"는 뜻이다. 소비자는 그
    자리에서 기존 동작을 유지해야 한다 — 여기서 기본값을 고르면 그것이 곧 추측이다.
    """
    if not query:
        return ()
    claims: list[GrainClaim] = []

    row_pattern = _row_pattern()
    if row_pattern is not None:
        for match in row_pattern.finditer(query):
            claims.append(
                GrainClaim(
                    grain=ROW,
                    source_span=(match.start(), match.end()),
                    evidence_text=match.group(0),
                    head=match.group("head"),
                )
            )

    subject_pattern = _subject_pattern()
    if subject_pattern is not None:
        for match in subject_pattern.finditer(query):
            if not lexicon_patterns.starts_new_token(query, match.start()):
                continue
            claims.append(
                GrainClaim(
                    grain=SUBJECT,
                    source_span=(match.start(), match.end()),
                    evidence_text=match.group(0),
                    head=match.group("head"),
                )
            )

    return tuple(sorted(claims, key=lambda claim: claim.source_span))


# ── IR 이 실제로 실현한 grain ────────────────────────────────────────────────────


def _is_threshold(node: Any) -> bool:
    """필드와 리터럴을 견주는 값 임계인가(시간 비교나 필드끼리 비교는 아니다)."""
    left, right = node.left, node.right
    return (
        isinstance(left, event_ir.FieldRef) and isinstance(right, event_ir.Literal)
    ) or (isinstance(right, event_ir.FieldRef) and isinstance(left, event_ir.Literal))


def _has_aggregate(node: Any) -> bool:
    return isinstance(node, event_ir.Aggregate)


def expression_grains(condition: Any) -> frozenset[str]:
    """트리가 **실현한** grain 집합. 원문을 읽지 않는 순수 IR 검사다.

    집계식의 **안쪽** 필터에 있는 비교는 row 임계가 아니다 — 그것은 집계 모집단을 좁히는
    조건이다. 그래서 집계 안으로 내려갈 때는 row 판정을 끈다.
    """
    found: set[str] = set()

    def walk(node: Any, *, in_exists: bool, in_aggregate: bool) -> None:
        if isinstance(node, event_ir.And | event_ir.Or):
            for operand in node.operands:
                walk(operand, in_exists=in_exists, in_aggregate=in_aggregate)
            return
        if isinstance(node, event_ir.Not):
            walk(node.operand, in_exists=in_exists, in_aggregate=in_aggregate)
            return
        if isinstance(node, event_ir.Exists):
            walk(node.relation, in_exists=True, in_aggregate=in_aggregate)
            return
        if isinstance(node, event_ir.Comparison):
            if _has_aggregate(node.left) or _has_aggregate(node.right):
                found.add(SUBJECT)
            elif in_exists and not in_aggregate and _is_threshold(node):
                found.add(ROW)
            for side in (node.left, node.right):
                walk(side, in_exists=in_exists, in_aggregate=in_aggregate)
            return
        if isinstance(node, event_ir.Aggregate):
            walk(node.relation, in_exists=in_exists, in_aggregate=True)
            return
        if isinstance(node, event_ir.Filter):
            walk(node.relation, in_exists=in_exists, in_aggregate=in_aggregate)
            walk(node.where, in_exists=in_exists, in_aggregate=in_aggregate)
            return
        for attribute in ("relation", "left", "right", "operand"):
            child = getattr(node, attribute, None)
            if child is not None and hasattr(child, "type"):
                walk(child, in_exists=in_exists, in_aggregate=in_aggregate)

    walk(condition, in_exists=False, in_aggregate=False)
    return frozenset(found)


# ── grain 낮추기(IR → IR) ────────────────────────────────────────────────────────

#: row 로 낮출 수 있는 집계 함수. ``sum`` 만 연다 — ``count`` 의 '3건 이상'은 본래 주체
#: 단위 세기라 row 대응이 없고, 평균·최소는 행 임계와 뜻이 다르다. 열지 않은 함수는
#: 낮추지 않고 ``None`` 을 돌려준다(추측 금지).
_REGRAINABLE_FUNCTIONS = frozenset({"sum"})


def regrain_to_row(condition: Any) -> Any | None:
    """트리 안의 주체 집계 임계를 **행 임계의 존재**로 낮춘다. 못 낮추면 ``None``.

    ``SUM(amount) >= 300000`` 은 회원별 합계다. 같은 자리에서 원문이 "…이상인 주문"이라고
    말했다면 뜻은 ``EXISTS(주문 중 amount >= 300000)`` 이다. 두 표현 모두 기존 IR 로
    표현된다 — 새 노드가 필요 없다.

    집계의 모집단 필터(기간 창 등)는 그대로 보존한다. 그것은 grain 과 무관한 조건이다.
    임계가 And/Or/Not 안에 있어도 그 자리만 바꾸고 나머지 절은 건드리지 않는다 — 절의
    소유권을 지킨다.
    """
    if isinstance(condition, event_ir.And | event_ir.Or):
        rewritten = tuple(regrain_to_row(item) or item for item in condition.operands)
        if rewritten == tuple(condition.operands):
            return None
        return type(condition)(operands=rewritten)
    if isinstance(condition, event_ir.Not):
        inner = regrain_to_row(condition.operand)
        return None if inner is None else event_ir.Not(operand=inner)
    return _regrain_comparison(condition)


def _regrain_comparison(condition: Any) -> Any | None:
    if not isinstance(condition, event_ir.Comparison):
        return None
    aggregate, literal = condition.left, condition.right
    if not isinstance(aggregate, event_ir.Aggregate) or not isinstance(
        literal, event_ir.Literal
    ):
        return None  # 집계가 오른쪽인 형태는 낮추지 않는다(연산자 반전은 별개 규칙이다).
    if str(aggregate.function).casefold() not in _REGRAINABLE_FUNCTIONS:
        return None
    if aggregate.distinct or not isinstance(aggregate.expression, event_ir.FieldRef):
        return None

    threshold = event_ir.Comparison(
        operator=condition.operator,
        left=aggregate.expression,
        right=literal,
        evidence=condition.evidence,
    )
    relation = aggregate.relation
    if isinstance(relation, event_ir.Filter):
        # 모집단 필터와 행 임계를 한 Filter 로 합친다(중첩 Filter 보다 SQL 이 읽기 좋다).
        merged = event_ir.Filter(
            relation=relation.relation,
            where=event_ir.And(operands=(relation.where, threshold)),
        )
    else:
        merged = event_ir.Filter(relation=relation, where=threshold)
    return event_ir.Exists(relation=merged, evidence=condition.evidence)


def conflicting_grain(
    query: str, condition: Any
) -> tuple[GrainClaim, frozenset[str]] | None:
    """원문이 말한 grain 과 트리가 실현한 grain 이 어긋나면 그 주장과 실현 집합.

    어긋나지 않거나 원문이 grain 을 말하지 않았으면 ``None``. 판단은 **주장이 있을 때만**
    한다 — 말하지 않은 것을 어겼다고 하지 않는다.
    """
    claims = detect_grain_claims(query)
    if not claims:
        return None
    realized = expression_grains(condition)
    if not realized:
        return None
    for claim in claims:
        if claim.grain not in realized:
            return claim, realized
    return None
