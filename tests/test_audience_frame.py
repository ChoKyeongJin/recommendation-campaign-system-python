"""프레임·절 구조 원자의 단위 계약, 그리고 문형이 되돌아오는 것을 막는 래칫.

닫힌 문형 정규식이 다섯 모듈에 하나씩 자라던 것을 원자 네 개로 대체했다. 그 원자가 옳다는 것은
"같은 뜻의 변형에는 같은 답, 조건이 하나라도 늘면 다른 답"으로만 확인된다 — 그래서 긍정 케이스와
부정 케이스를 짝으로 둔다.

마지막 테스트는 성격이 다르다. 부채가 **같은 모양으로 되돌아오는 것**을 막는 유일한 안전망이라,
계약이 아니라 소스를 본다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_frame  # noqa: E402
import lexicon_patterns  # noqa: E402

QUERY = "최근 30일 장바구니에 담아두고 결제하지 않은 회원"
CART_SPAN = (QUERY.index("장바구니"), QUERY.index("장바구니") + len("장바구니"))
NEGATION_SPAN = (QUERY.index("결제"), QUERY.index("않은") + len("않은"))
DURATION_SPAN = (QUERY.index("30일"), QUERY.index("30일") + len("30일"))
STEM_SPAN = (QUERY.index("담아두고"), QUERY.index("담아두고") + len("담아두고"))
OWNED = (DURATION_SPAN, CART_SPAN, STEM_SPAN, NEGATION_SPAN)
RECENCY = lexicon_patterns.vocabulary("temporal_recency_marker")


# ── 잔여물이 프레임뿐인가 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "owned", "extra"),
    [
        pytest.param(QUERY, OWNED, RECENCY, id="live-11-original"),
        pytest.param(
            "사료 외 상품을 구매한 회원을 찾아줘.", ((0, 11),), (), id="request-verb"
        ),
        pytest.param(
            "사료 외 상품을 구매했던 고객님 추출해 주세요", ((0, 11),), (), id="other-ending"
        ),
        pytest.param("사료 외 상품을 구매한 사람", ((0, 11),), (), id="bare-noun"),
        pytest.param("사료 외 상품을 구매", ((0, 11),), (), id="nothing-left"),
    ],
)
def test_frame_residue_accepts_wording_that_adds_no_condition(
    query: str, owned: tuple, extra: tuple
) -> None:
    assert audience_frame.is_frame_only(query, owned, extra_terms=extra)


@pytest.mark.parametrize(
    ("query", "owned", "why"),
    [
        pytest.param(
            "최근 30일 장바구니에 담아두고 결제하지 않은 여성 회원",
            OWNED,
            "성별 조건",
            id="gender",
        ),
        pytest.param(
            "최근 30일 장바구니에 담아두고 결제하지 않은 회원 또는 VIP 회원",
            OWNED,
            "OR 분기",
            id="or-branch",
        ),
        pytest.param(
            "최근 30일 장바구니에 담아두고 결제하지 않은 회원 중 30세 이상",
            OWNED,
            "범위 표지와 추가 임계",
            id="scope-marker",
        ),
        pytest.param(
            "서울 거주 사료 외 상품을 구매한 회원", ((5, 16),), "앞선 지역 조건", id="prefix"
        ),
        pytest.param(
            "사료 외 상품을 구매한 회원, 골드 회원", ((0, 11),), "나열 쉼표", id="comma"
        ),
    ],
)
def test_frame_residue_rejects_any_surviving_condition(
    query: str, owned: tuple, why: str
) -> None:
    assert not audience_frame.is_frame_only(
        query, owned, extra_terms=RECENCY
    ), why


def test_recency_marker_is_frame_only_when_the_caller_declares_it() -> None:
    """'최근'을 삼키는 근거는 코드가 아니라 원장이다 — 선언 없이는 조건으로 남는다."""
    assert not audience_frame.is_frame_only(QUERY, OWNED)
    assert audience_frame.is_frame_only(QUERY, OWNED, extra_terms=RECENCY)


def test_frame_vocabulary_excludes_the_role_nouns_that_carry_a_condition() -> None:
    """'구매자'가 프레임이면 구매 조건이 잉여어로 위장해 통과한다."""
    terms = set(audience_frame.frame_terms())

    assert {"회원", "고객", "사용자", "유저", "고객님", "사람"} <= terms
    assert "구매자" not in terms and "소비자" not in terms


# ── 두 스팬이 한 절인가 ──────────────────────────────────────────────────────────────


def test_two_surfaces_of_one_clause_are_in_the_same_clause() -> None:
    assert audience_frame.in_same_clause(
        QUERY, CART_SPAN, NEGATION_SPAN, stems=("담",)
    )


def test_an_unreadable_span_is_not_the_same_clause() -> None:
    """스팬을 못 읽었으면 '겹쳤다'가 아니라 모르는 것이다(fail-open 금지)."""
    assert not audience_frame.in_same_clause(QUERY, (5, 3), NEGATION_SPAN)
    assert not audience_frame.in_same_clause(QUERY, CART_SPAN, (900, 950))


@pytest.mark.parametrize(
    ("query", "left_text", "right_text", "content", "why"),
    [
        pytest.param(
            "최근 30일 장바구니에 담은 회원 중 구매 이력이 없는 회원",
            "장바구니",
            "구매 이력이 없는",
            (),
            "프레임 명사와 범위 표지가 절을 가른다",
            id="member-and-scope-marker",
        ),
        pytest.param(
            "최근 30일 장바구니에 담아두고 다른 상품을 결제하지 않은 회원",
            "장바구니",
            "결제하지 않은",
            ("상품",),
            "사이의 내용어가 새 범위 조건이다",
            id="scope-noun",
        ),
        pytest.param(
            "장바구니에 담고 그리고 결제하지 않은 회원",
            "장바구니",
            "결제하지 않은",
            (),
            "접속어가 절을 가른다",
            id="connective",
        ),
        pytest.param(
            "장바구니에 담고, 결제하지 않은 회원",
            "장바구니",
            "결제하지 않은",
            (),
            "쉼표가 절을 가른다",
            id="comma",
        ),
    ],
)
def test_a_boundary_between_two_surfaces_means_two_clauses(
    query: str, left_text: str, right_text: str, content: tuple, why: str
) -> None:
    left = (query.index(left_text), query.index(left_text) + len(left_text))
    right = (query.index(right_text), query.index(right_text) + len(right_text))

    assert not audience_frame.in_same_clause(
        query, left, right, stems=("담",), content_terms=content
    ), why


# ── 국소 부정 · 어간 활용 · 좌표 변환 ────────────────────────────────────────────────


def test_local_negation_reaches_the_alias_it_actually_negates() -> None:
    spans = audience_frame.local_negation_spans(QUERY, ("구매", "주문", "결제"))

    assert [QUERY[start:end] for start, end in spans] == ["결제하지 않은"]


def test_a_distant_negation_does_not_belong_to_the_alias() -> None:
    query = "결제 금액이 큰 회원 중 최근 로그인 이력이 없는 회원"

    assert audience_frame.local_negation_spans(query, ("결제",)) == ()


def test_prefixed_negation_is_one_span_with_its_alias() -> None:
    query = "미구매 회원"

    assert [
        query[start:end]
        for start, end in audience_frame.local_negation_spans(query, ("구매",))
    ] == ["미구매"]


def test_verb_stems_come_from_catalog_aliases_not_from_code() -> None:
    """'담기'가 별칭에 있으므로 '담아두고'를 코드에 적을 필요가 없다."""
    stems = audience_frame.alias_stems(("장바구니", "카트", "담기"))

    assert stems == ("담",)
    assert [
        QUERY[start:end] for start, end in audience_frame.stem_inflection_spans(QUERY, stems)
    ] == ["담아두고"]


def test_a_stem_inside_another_word_is_not_an_inflection() -> None:
    assert audience_frame.stem_inflection_spans("부담이 큰 회원", ("담",)) == ()


def test_compact_coordinates_come_back_to_the_source_text() -> None:
    compact = QUERY.replace(" ", "").casefold()
    start = compact.index("결제하지않은")

    assert audience_frame.compact_to_source_span(
        QUERY, start, start + len("결제하지않은")
    ) == NEGATION_SPAN


def test_compact_conversion_fails_closed_when_folding_changes_length() -> None:
    """접기로 글자 수가 달라지면 대응이 1:1이 아니다 — 추측하지 않는다."""
    assert audience_frame.compact_to_source_span("ß 회원", 0, 1) is None
    assert audience_frame.compact_to_source_span(QUERY, 5, 3) is None


# ── 래칫: 문형이 되돌아오지 못하게 ──────────────────────────────────────────────────

GUARDED_MODULES = (
    "audience_frame.py",
    "event_state_selection.py",
    "rolling_absence_claims.py",
    "open_text_scope_claims.py",
    # profile_metric_claims.py 는 2026-08-05 폐기 축 이행에서 삭제됐다.
    "campaign_metric_claims.py",
)
_QUERY_NAMES = frozenset({"query", "text", "prompt", "source_text"})


def _module_level_regex_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "compile"
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
        ):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    return names


def _is_request_derived(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id in _QUERY_NAMES
        for child in ast.walk(node)
    )


@pytest.mark.parametrize("module", GUARDED_MODULES)
def test_no_module_regex_matches_the_whole_request(module: str) -> None:
    """모듈 상수 정규식을 원문(또는 그 조각)에 통째로 맞추는 자리는 곧 문형이다.

    그것이 부채 ①의 모양이었다 — 커버리지 단위가 문장이라 어미 하나만 달라도 무효였고, 확장은
    케이스마다 정규식 한 줄이었다. 인라인 ``re.fullmatch(r"\\s+", query[a:b])`` 같은 구조 검사는
    대상이 아니다(그건 절 안쪽의 접착부이지 문형이 아니다).
    """
    path = REPO_ROOT / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    regex_names = _module_level_regex_names(tree)

    offenders = [
        f"{module}:{node.lineno} -> {node.func.value.id}.{node.func.attr}(...)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"fullmatch", "match"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in regex_names
        and any(_is_request_derived(argument) for argument in node.args)
    ]

    assert not offenders, (
        "문장 템플릿이 되돌아왔다:\n  "
        + "\n  ".join(offenders)
        + "\n\n요청 전체(또는 그 접미/접두)를 한 정규식에 맞추는 대신 "
        "audience_frame 의 절 구조 판정을 쓴다."
    )
