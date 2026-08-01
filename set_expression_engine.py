from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import lexicon_patterns
from common_utils import compact as _compact

DEFAULT_NORMALIZATION_PATH = Path(
    "docs/data/runtime/language/normalization_rules.sample.json"
)
OPERATOR_WORDS = {
    "합집합": "+",
    "or": "+",
    "union": "+",
    "교집합": "*",
    "그리고": "*",
    "and": "*",
    "intersection": "*",
    "차집합": "-",
    "제외": "-",
    "빼고": "-",
    "except": "-",
    "minus": "-",
}
OPERATOR_WORDS.update({word: "+" for word in lexicon_patterns.vocabulary("or_connective")})
OPERATOR_LABELS = {
    "+": "union",
    "*": "intersection",
    "-": "difference",
}


def parse_set_expressions_from_query(
    query: str,
    normalization_path: Path = DEFAULT_NORMALIZATION_PATH,
) -> list[dict[str, Any]]:
    term_catalog = load_set_term_catalog(normalization_path)
    parsed = _parse_set_expression(query, term_catalog)
    if parsed is None:
        return []
    return [parsed]


def load_set_term_catalog(normalization_path: Path = DEFAULT_NORMALIZATION_PATH) -> list[dict[str, str]]:
    if not normalization_path.exists():
        return []
    payload = json.loads(normalization_path.read_text(encoding="utf-8"))
    terms: list[dict[str, str]] = []
    for rule in payload.get("normalization_rules", []):
        if not isinstance(rule, dict) or not isinstance(rule.get("canonical"), str):
            continue
        # ``prmp_kwd`` rows describe metrics/dimensions used by other parsers
        # (for example purchase-count), not named/member sets.  Treating every
        # normalization row as a Set operand can turn an Event aggregate OR into
        # a fake named segment and erase its aggregate predicate.
        if rule.get("source") == "prmp_kwd":
            continue
        aliases = [rule.get("canonical"), rule.get("ko_label"), *rule.get("synonyms", [])]
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                terms.append(
                    {
                        "term": alias,
                        "compact_term": _compact(alias),
                        "canonical": rule["canonical"],
                        "label": rule.get("ko_label", rule["canonical"]),
                    }
                )
    terms.sort(key=lambda item: len(item["compact_term"]), reverse=True)
    return terms


def _parse_set_expression(query: str, term_catalog: list[dict[str, str]]) -> dict[str, Any] | None:
    # detection: 어느 파싱 전략이 집합식을 만들었나. natural/postfix 는 명시적 집합-구조 표지(포함·남기고·
    # 그중·중에서·교집합·합집합·차집합·에서…제외)를 요구하는 '진짜 집합연산'이고, operator_scan 은 접속어
    # (또는/그리고/and/or)만으로 발동하는 일반 폴백이다. 후자는 '서울 또는 경기' 같은 평범한 dimension OR 을
    # 집합식으로 오탐할 수 있어, 상위(graph_rag)가 dimension-소유 여부로 리던던시를 판정할 때 이 태그로 구분한다.
    natural_ast = _parse_korean_natural_set_expression(query, term_catalog)
    if natural_ast is not None:
        return _set_expression_result(query, natural_ast, "natural")

    # "남성을 제외한 고객"은 독립적인 성별 제외 필터이지 두 세그먼트의 차집합 표현이 아니다.
    # operator_scan 으로 넘기면 '제외'만 '-'로 치환되고 활용 어미인 '한 고객'이 unknown operand 로 남아
    # 전체 SQL 출고를 막는다. 'A에서/B 중 C 제외' 같은 실제 집합 범위는 위 natural parser가 먼저 소비한다.
    if re.search(
        r"(?:제외(?:한|하고|하여|해서|해(?:줘|주세요)?|하세요|한다)|빼고)\s*(?:고객|회원|사용자)?\s*[.!?。]*$",
        query.strip(),
    ):
        return None

    postfix_ast = _parse_korean_postfix_set_expression(query, term_catalog)
    if postfix_ast is not None:
        return _set_expression_result(query, postfix_ast, "postfix")

    tokens = _scan_set_tokens(query, term_catalog)
    if not any(token["kind"] == "op" for token in tokens):
        return None
    ast = _tokens_to_ast(tokens)
    if ast is None:
        return {
            "expression_id": "segment_set_expression",
            "ko_label": "세그먼트 집합식",
            "expression_text": query,
            "set_ast": None,
            "requires_clarification": True,
            "clarification_question": "집합식의 괄호나 연산자 위치를 해석할 수 없습니다.",
            "source": "rules_set_expression",
            "detection": "operator_scan",
        }

    return _set_expression_result(query, ast, "operator_scan")


def _set_expression_result(query: str, ast: dict[str, Any], detection: str) -> dict[str, Any]:
    unknown_terms = _unknown_terms(ast)
    return {
        "expression_id": "segment_set_expression",
        "ko_label": "세그먼트 집합식",
        "expression_text": query,
        "set_ast": ast,
        "requires_clarification": bool(unknown_terms),
        "clarification_question": _clarification_question(unknown_terms),
        "source": "rules_set_expression",
        "detection": detection,
    }


def _parse_korean_natural_set_expression(query: str, term_catalog: list[dict[str, str]]) -> dict[str, Any] | None:
    working_query = _strip_query_tail(query)
    include_text, exclude_text = _split_exclusion_clause(working_query)
    # "대상으로"만 있는 문장("20대 여성 고객을 대상으로 …")은 집합 연산이 아니라 그냥 "누구를 타겟한다"는
    # 뜻이므로 집합식으로 보지 않는다. 실제 집합 연산은 포함/남기고(대상으로 하되 X만 포함)나 제외/그중/
    # 중에서 같은 정제 표지가 있을 때만 성립한다. (포함/남기고가 있으면 _split_required_include_clauses 가
    # "대상으로"를 기준으로 base/required 를 나눈다.) 이 표지 없이 "대상으로"만으로 집합식을 만들면
    # "20대 여성"이 age_20s * female 교집합(실DB 미지원)이 돼 연령·성별이 통째로 사라지고 SQL 이 막혔다.
    has_natural_structure = exclude_text is not None or re.search(r"포함|남기고|그중|중에서", include_text)
    if not has_natural_structure:
        return None

    include_ast = _parse_include_clause(include_text, term_catalog)
    if include_ast is None:
        return None
    if exclude_text is None:
        return include_ast
    exclude_ast = _parse_operand_group(exclude_text, term_catalog, conjunction_op="+")
    if exclude_ast is None:
        return include_ast
    return _binary_set_ast(include_ast, exclude_ast, "-")


def _split_exclusion_clause(query: str) -> tuple[str, str | None]:
    # 한국어 활용형은 \b 경계로 자를 수 없다('제외한'에서 외/한 모두 word 문자). 지원 활용형 전체를
    # 소비해야 operator_scan 에 '한 고객'/'해' 같은 가짜 피연산자가 남지 않는다.
    matches = list(re.finditer(
        r"(?:빼고|제외(?:하고|한|하여|해서|해(?:줘|주세요)?|하세요|한다)?)(?=\s|[,\.!?。]|$)",
        query,
    ))
    if not matches:
        return query, None
    marker = matches[-1]
    before_marker = query[: marker.start()].strip(" ,")

    comma_index = before_marker.rfind(",")
    if comma_index >= 0:
        return before_marker[:comma_index].strip(" ,"), _strip_operand_suffix(before_marker[comma_index + 1 :])

    split_match = re.search(r"(?P<left>.+?)(?:중에서|에서|중)\s*(?P<right>.+)$", before_marker)
    if split_match:
        return split_match.group("left").strip(" ,"), _strip_operand_suffix(split_match.group("right"))

    # 범위 표지(쉼표·중에서)가 없어도 포함 표지가 있으면 그 뒤가 제외 대상이다("A는 포함하고 B는 제외해줘").
    # 여기서 포기하고 query 전체를 include 로 돌려주면 제외 절이 통째로 사라져(포함 조건으로 흡수돼)
    # 극성이 조용히 뒤집힌다 — 제외 표지가 존재하는 한 제외 절은 반드시 어딘가로 귀결돼야 한다.
    include_markers = list(re.finditer(
        r"(?:포함(?:하고|해서|하되|하며|하여|한|해\s*줘|해줘)?|남기고)(?=\s|[,\.!?。]|$)", before_marker
    ))
    if include_markers:
        marker_end = include_markers[-1].end()
        remainder = _strip_operand_suffix(before_marker[marker_end:])
        if remainder:
            return before_marker[:marker_end].strip(" ,"), remainder
    return query, None


def _parse_include_clause(text: str, term_catalog: list[dict[str, str]]) -> dict[str, Any] | None:
    base_text, required_texts = _split_required_include_clauses(text)
    base_ast = _parse_operand_group(base_text, term_catalog, conjunction_op="*")
    if base_ast is None:
        return None

    ast = base_ast
    for required_text in required_texts:
        required_ast = _parse_operand_group(required_text, term_catalog, conjunction_op="*")
        if required_ast is not None:
            ast = _binary_set_ast(ast, required_ast, "*") or ast
    return ast


def _split_required_include_clauses(text: str) -> tuple[str, list[str]]:
    parts = re.split(r"(?:을|를)?\s*대상으로\s*(?:하되|하고|해서|하여|삼되)?", text, maxsplit=1)
    if len(parts) == 1:
        return text, []

    base_text = parts[0]
    remainder = parts[1]
    required_texts = []
    for match in re.finditer(r"(?P<required>[^,]+?)(?:만)?\s*(?:포함|남기고)", remainder):
        required_texts.append(_strip_clause_prefix(match.group("required")))
    return base_text, required_texts


def _parse_operand_group(text: str, term_catalog: list[dict[str, str]], conjunction_op: str) -> dict[str, Any] | None:
    cleaned = _strip_operand_suffix(_strip_query_tail(_strip_clause_prefix(text)))
    if not cleaned:
        return None
    normalized = re.sub(r"\s*(?:와|과|및|하고|그리고)\s*", f" {conjunction_op} ", cleaned)
    or_alternation = lexicon_patterns.alternation("or_connective")
    normalized = re.sub(rf"\s*(?:{or_alternation})\s*", " + ", normalized)
    tokens = _scan_set_tokens(normalized, term_catalog)
    if any(token["kind"] == "op" for token in tokens):
        return _tokens_to_ast(tokens)
    return _text_to_operand_ast(normalized, term_catalog)


def _strip_clause_prefix(text: str) -> str:
    return re.sub(r"^\s*(?:,|그중|중에서|그리고|또|또한|하되|하고|있는|있고|대상으로|고객만|사용자만)\s*", "", text.strip())


def _parse_korean_postfix_set_expression(query: str, term_catalog: list[dict[str, str]]) -> dict[str, Any] | None:
    union_or_intersection = re.search(
        r"(?P<left>.+?)(?:와|과|및|하고)\s*(?P<right>.+?)(?:의)?\s*(?P<op>합집합|교집합)\b",
        query,
    )
    if union_or_intersection:
        op = "+" if union_or_intersection.group("op") == "합집합" else "*"
        return _binary_set_ast(
            _text_to_operand_ast(_strip_operand_suffix(union_or_intersection.group("left")), term_catalog),
            _text_to_operand_ast(_strip_operand_suffix(union_or_intersection.group("right")), term_catalog),
            op,
        )

    difference = re.search(r"(?P<left>.+?)에서\s*(?P<right>.+?)(?:을|를)?\s*(?:제외|빼고)\b", query)
    if difference:
        return _binary_set_ast(
            _text_to_operand_ast(_strip_operand_suffix(difference.group("left")), term_catalog),
            _text_to_operand_ast(_strip_operand_suffix(difference.group("right")), term_catalog),
            "-",
        )

    difference_of = re.search(
        r"(?P<left>.+?)(?:와|과|및|하고)\s*(?P<right>.+?)(?:의)?\s*차집합\b",
        query,
    )
    if difference_of:
        return _binary_set_ast(
            _text_to_operand_ast(_strip_operand_suffix(difference_of.group("left")), term_catalog),
            _text_to_operand_ast(_strip_operand_suffix(difference_of.group("right")), term_catalog),
            "-",
        )
    return None


def _binary_set_ast(left: dict[str, Any] | None, right: dict[str, Any] | None, op: str) -> dict[str, Any] | None:
    if left is None or right is None:
        return None
    return {"type": "set_op", "op": op, "operation": OPERATOR_LABELS[op], "left": left, "right": right}


def _strip_operand_suffix(text: str) -> str:
    return re.sub(r"(?:의|을|를|은|는|이|가|만|으로|로|인)\s*$", "", text.strip())


# 조건이 아니라 '요청 꼬리'인 어구. 집합식 피연산자 자리에 이것만 남으면 조건이 아니라 잡음이다 —
# 잡음을 unknown_operand 로 남기면 실제 조건(예: SIDO NOT IN)이 멀쩡한데도 전체가 확인 요청으로 막힌다.
_QUERY_TAIL_VERBS = (
    "찾아줘", "찾아", "조회해줘", "조회", "보여줘", "알려줘", "추천해줘", "추천해",
    "추출해줘", "추출해", "추출", "뽑아줘", "뽑아", "뽑기", "만들어줘", "만들어",
    "정리해줘", "정리해", "골라줘", "골라", "선정해줘", "선정",
    "해줘", "해주세요", "주세요", "줘",
)
# 조건 의미가 없는 명사/조사류(피연산자가 이것들로만 이뤄지면 잡음).
_NOISE_OPERAND_TOKENS = (
    "고객", "사용자", "사람", "세그먼트", "집합", "대상", "추천", "캠페인", "목록", "리스트", "명단",
    "중", "에서", "의", "인", "인사람", "인고객", "좀",
    "만", "포함하고", "포함", "남기고", "대상으로", "하되", "있는", "있고",
    # 연산자 어구('제외한/제외해서')에서 연산자만 소비되고 남은 활용 어미. 조건 의미가 없다.
    "한", "하여", "해서", "된", "되는", "및",
)

_QUERY_TAIL_RE = re.compile(
    r"(?:" + "|".join(sorted(_QUERY_TAIL_VERBS, key=len, reverse=True)) + r")\s*[.!?。]*\s*$"
)
_NOISE_OPERAND_RE = re.compile(
    r"(?:" + "|".join(sorted((*_NOISE_OPERAND_TOKENS, *_QUERY_TAIL_VERBS), key=len, reverse=True)) + r")"
)


def _strip_query_tail(text: str) -> str:
    return _QUERY_TAIL_RE.sub("", text.strip()).strip(" ,")


def _is_noise_operand_text(compact_text: str) -> bool:
    return not _NOISE_OPERAND_RE.sub("", compact_text)


def _set_ast_contains_age_range(ast: Any) -> bool:
    if not isinstance(ast, dict):
        return False
    if ast.get("type") == "age_range":
        return True
    return _set_ast_contains_age_range(ast.get("left")) or _set_ast_contains_age_range(ast.get("right"))


def _scan_set_tokens(query: str, term_catalog: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized_query = _normalize_operator_words(query)
    tokens: list[dict[str, Any]] = []
    current: list[str] = []
    for char in normalized_query:
        if char in "+*-()":
            _append_operand_token(tokens, "".join(current), term_catalog)
            current = []
            if char in "+*-":
                tokens.append({"kind": "op", "value": char})
            else:
                tokens.append({"kind": "paren", "value": char})
            continue
        current.append(char)
    _append_operand_token(tokens, "".join(current), term_catalog)
    return _trim_noise_tokens(tokens)


def _normalize_operator_words(query: str) -> str:
    normalized = query
    for word, operator in OPERATOR_WORDS.items():
        normalized = re.sub(re.escape(word), f" {operator} ", normalized, flags=re.IGNORECASE)
    return normalized


def _append_operand_token(tokens: list[dict[str, Any]], text: str, term_catalog: list[dict[str, str]]) -> None:
    operand = _text_to_operand_ast(text, term_catalog)
    if operand is not None:
        tokens.append({"kind": "operand", "ast": operand})


def _text_to_operand_ast(text: str, term_catalog: list[dict[str, str]]) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    compact_text = _compact(stripped)
    operands: list[dict[str, Any]] = []
    # 이미 다른 피연산자가 소비한 문자 구간(compact 좌표). 구간이 겹치는 후보는 중복으로 보고 버린다.
    # 카탈로그가 긴 표현부터 오도록 정렬돼 있어(load_set_term_catalog) greedy-longest 로 구간을 선점한다.
    # 예) "VIP 등급 고객"("vip등급고객")에서 VIP등급이 "vip등급"[0,5)을 먹으면, member_grade 의 "등급 고객"
    # ("등급고객"[3,7))은 "등급"에서 겹쳐 스킵된다 — 예전 substring 가드는 이 부분겹침을 못 걸러 member_grade
    # 가 헛매칭돼 (VIP등급 * member_grade) 교집합이 만들어졌고, 값 없는 member_grade 때문에 SQL 이 막혔다.
    claimed_spans: list[tuple[int, int]] = []
    seen_canonicals: set[str] = set()

    for age_operand in _age_operands(stripped):
        span = _find_unclaimed_span(compact_text, _compact(str(age_operand.get("matched_text", ""))), claimed_spans)
        if span is not None:
            claimed_spans.append(span)
        operands.append(age_operand)
        seen_canonicals.add(age_operand.get("canonical"))

    for term in term_catalog:
        compact_term = term["compact_term"]
        if not compact_term or term["canonical"] in seen_canonicals:
            continue
        span = _find_unclaimed_span(compact_text, compact_term, claimed_spans)
        if span is None:
            continue
        operands.append(
            {
                "type": "operand",
                "canonical": term["canonical"],
                "label": term["label"],
                "matched_text": term["term"],
            }
        )
        seen_canonicals.add(term["canonical"])
        claimed_spans.append(span)

    if not operands:
        if _is_noise_operand_text(compact_text):
            return None
        return {"type": "unknown_operand", "text": stripped}
    if len(operands) == 1:
        return operands[0]
    ast = operands[0]
    for operand in operands[1:]:
        ast = {"type": "set_op", "op": "*", "left": ast, "right": operand}
    return ast


def _find_unclaimed_span(haystack: str, needle: str, claimed_spans: list[tuple[int, int]]) -> tuple[int, int] | None:
    """needle 이 claimed_spans 어디와도 겹치지 않게 등장하는 첫 위치의 (start, end) 를 준다(없으면 None)."""
    if not needle:
        return None
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        if not any(start < claimed_end and claimed_start < end for claimed_start, claimed_end in claimed_spans):
            return (start, end)
        start = haystack.find(needle, start + 1)
    return None


def _age_operands(text: str) -> list[dict[str, Any]]:
    operands = []
    for match in re.finditer(r"(?P<decade>[1-9]\d)\s*대", text):
        decade = int(match.group("decade"))
        operands.append(
            {
                "type": "age_range",
                "canonical": f"age_{decade}s",
                "label": f"{decade}대",
                "age_min": decade,
                "age_max": decade + 9,
                "matched_text": match.group(0),
            }
        )
    return operands


def _tokens_to_ast(tokens: list[dict[str, Any]]) -> dict[str, Any] | None:
    output: list[dict[str, Any]] = []
    operators: list[str] = []
    precedence = {"+": 1, "-": 1, "*": 2}

    for token in tokens:
        if token["kind"] == "operand":
            output.append(token["ast"])
        elif token["kind"] == "op":
            while operators and operators[-1] != "(" and precedence[operators[-1]] >= precedence[token["value"]]:
                if not _reduce_operator(output, operators.pop()):
                    return None
            operators.append(token["value"])
        elif token["kind"] == "paren" and token["value"] == "(":
            operators.append("(")
        elif token["kind"] == "paren" and token["value"] == ")":
            while operators and operators[-1] != "(":
                if not _reduce_operator(output, operators.pop()):
                    return None
            if not operators or operators[-1] != "(":
                return None
            operators.pop()

    while operators:
        op = operators.pop()
        if op == "(":
            return None
        if not _reduce_operator(output, op):
            return None
    return output[0] if len(output) == 1 else None


def _reduce_operator(output: list[dict[str, Any]], op: str) -> bool:
    if len(output) < 2:
        return False
    right = output.pop()
    left = output.pop()
    output.append({"type": "set_op", "op": op, "operation": OPERATOR_LABELS[op], "left": left, "right": right})
    return True


def _trim_noise_tokens(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    while tokens and tokens[0]["kind"] == "op":
        tokens.pop(0)
    while tokens and tokens[-1]["kind"] == "op":
        tokens.pop()
    return tokens


def _unknown_terms(ast: Any) -> list[str]:
    if not isinstance(ast, dict):
        return []
    if ast.get("type") == "unknown_operand":
        return [ast.get("text", "")]
    return [*_unknown_terms(ast.get("left")), *_unknown_terms(ast.get("right"))]


def clarification_question(unknown_terms: list[str]) -> str | None:
    """미해결 피연산자 목록 → 확인 질문(없으면 None).

    파싱 직후뿐 아니라 소유권 조정(condition_reconciliation) 이후 **남은 미해결만** 다시 묻기 위해
    공개한다 — 이미 다른 슬롯이 해석한 항목을 질문에 남기지 않으려면 같은 문구 생성기를 써야 한다.
    """
    unknown_terms = [term for term in unknown_terms if term]
    if not unknown_terms:
        return None
    return "집합식의 다음 항목을 정규화 사전에서 찾지 못했습니다: " + ", ".join(unknown_terms)


def _clarification_question(unknown_terms: list[str]) -> str | None:
    return clarification_question(unknown_terms)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse user segment set expressions.")
    parser.add_argument("query")
    parser.add_argument("--normalization", type=Path, default=DEFAULT_NORMALIZATION_PATH)
    args = parser.parse_args()
    expressions = parse_set_expressions_from_query(args.query, normalization_path=args.normalization)
    print(json.dumps({"set_expressions": expressions}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
