r"""규칙 인벤토리 — 코드에 박힌 표면어 규칙을 어휘형/문법형/업무의미형/낱말집합으로 분류한다.

이행(정규식 → 렉시콘)의 작업 목록을 사람 기억이 아니라 파일로 만든다. 판정 기준은 하나다:

    이 규칙이 열거하는 것이 **단어 목록**인가, **구조**인가?

  * lexical  (어휘형)   — 리터럴 표면어의 나열. 새 표현이 생길 때마다 코드를 고치게 만드는 것들이다.
                          **데이터(:mod:`lexicon_patterns`)로 옮길 대상.**
  * grammar  (문법형)   — 수량자·문자클래스·룩어라운드·경계 등 구조를 표현. 코드에 남는다.
  * domain   (업무의미형) — 어휘와 구조가 섞여 업무 규칙을 인코딩. 보통 어휘 부분만 데이터로 빼고
                          구조는 남긴다(개별 판단).
  * wordlist (낱말집합)  — 정규식이 아닌 한글 표면어 집합 상수(``{"상품", "제품", …}``). 정규식과
                          형태만 다르고 성질이 같은 두더지다 — 같은 이행 대상이라 같이 센다.
  * composed (조립형)   — 사전 어휘를 끼워 넣어 조립한 구조(``rf"(?:{alternation('identity_same')})\s*…"``).
                          **이행 완료 형태**라 상한을 걸지 않는다 — 걸면 이관이 지표상 손해가 된다.

스캔 범위(사각지대 없애기)
-------------------------
표면어 규칙은 ``NAME = re.compile("리터럴")`` 형태로만 오지 않는다. 이 스캐너는 넷을 모두 본다:

  1. ``NAME = re.compile(...)``            — 상수 정규식(모듈 최상위·함수 내부 모두)
  2. ``NAME = (re.compile(...), ...)``     — 튜플/리스트/집합/딕트에 담긴 정규식 묶음
  3. ``re.search("한글…", text)``          — 이름 없이 인라인으로 호출되는 한글 정규식
  4. ``NAME = frozenset({"상품", …})``     — 한글 낱말집합 상수

2~4 는 이전 스캐너가 못 보던 구멍이었고, 실제로 그 구멍에 규칙이 쌓였다(예: 튜플로 묶인
``_SAME_PRODUCT_PATTERNS``, 낱말집합 ``_GENERIC_PRODUCT_NOUNS``). 세지 않으면 래칫이 못 막는다.

동적으로 조립된 패턴(f-string·`_alt()` 합성)은 리터럴이 없어 분류가 불가능하므로 ``pattern: None``
으로 남기고 ``domain`` 으로 센다 — 사람이 소스를 봐야 한다는 표시다.

:func:`counts` 는 계약 테스트의 **래칫**이 쓴다. ``lexical``·``domain``·``wordlist`` 가 기준선을
넘으면 "새 표현을 또 코드로 받았다"는 뜻이므로 테스트가 깨진다(``grammar`` 는 구조라서 제외).
CLI 는 ``tools/regex_inventory.py``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
# 인벤토리 자신과 이관 대상 어휘를 담은 모듈은 스캔에서 뺀다(자기 참조·중복 집계 방지).
EXCLUDED_MODULES = frozenset({"regex_inventory.py"})
BASELINE_PATH = ROOT / "docs" / "data" / "regex_inventory_baseline.json"

# 구조를 나타내는 메타문자(하나라도 있으면 순수 어휘형이 아니다).
_STRUCTURAL = re.compile(r"""\\[dswbDSWB]|\[|\{\d|\(\?[=!<:P]|[+*]|\.\.|(?<!\\)\.""")
# 순수 어휘형이 허용하는 것: 교대(|)와 리터럴뿐. 그룹·수량자·앵커가 하나라도 있으면 구조가 섞인
# 것이므로 domain 으로 내린다 — 조사 결합('(?:이|가|을)?')은 문법이라 통째로 데이터화할 수 없다.
_ALLOWED_IN_LEXICAL = re.compile(r"^[^\\\[\]{}()+*.^$?]*$")
# 한글 표면어가 실제로 들어 있는가(어휘형의 필요조건).
_HANGUL = re.compile(r"[가-힣]")

CLASS_ORDER = {"lexical": 0, "wordlist": 1, "domain": 2, "composed": 3, "grammar": 4}
# 래칫이 상한을 거는 분류. grammar 는 구조라 코드에 남는 것이 정상이고, composed 는 이행 **완료** 형태
# (구조만 코드, 낱말은 사전)이므로 둘 다 상한을 걸지 않는다 — 걸면 이행이 지표상 손해가 된다.
RATCHETED_CLASSES = ("lexical", "wordlist", "domain")
# 어휘를 사전에서 끼워 넣었다는 표시. 이 이름을 참조해 조립된 정규식은 composed 로 센다.
_LEXICON_MARKERS = ("lexicon_patterns", "_alt(")
# 인라인 한글 정규식을 만드는 re 함수(이름 없이 호출되는 규칙).
_INLINE_RE_FUNCTIONS = frozenset({"search", "match", "fullmatch", "sub", "subn", "findall", "finditer", "split"})
# 낱말집합으로 셀 최소 한글 낱말 수. 1개는 상수 하나이지 '목록'이 아니다.
WORDLIST_MIN_TERMS = 2
# 낱말집합은 **규칙 상수**만 센다(상수 이름 규약). LLM 프롬프트를 조립하는 지역 리스트
# (``action_terms``·``prompt_lines`` 등)는 파서 규칙이 아니므로, 프롬프트 문구를 손볼 때마다
# 래칫이 깨지면 래칫이 신뢰를 잃는다 — 규칙과 문구를 이름 규약으로 가른다.
_RULE_CONSTANT_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


def classify(pattern: str) -> tuple[str, str]:
    """(분류, 사유). 자동 초안이며 사람이 뒤집을 수 있다."""
    alternatives = [part for part in pattern.split("|") if part]
    literal_like = _ALLOWED_IN_LEXICAL.match(pattern) is not None
    has_hangul = bool(_HANGUL.search(pattern))
    structural = bool(_STRUCTURAL.search(pattern))

    if literal_like and has_hangul and len(alternatives) >= 2:
        return "lexical", f"메타문자 없는 표면어 교대 {len(alternatives)}개 — 렉시콘으로 옮길 수 있다"
    if literal_like and has_hangul:
        return "lexical", "메타문자 없는 단일 표면어"
    if has_hangul and structural:
        return "domain", "한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단"
    if structural and not has_hangul:
        return "grammar", "표면어 없이 구조만 — 코드에 남는다"
    return "domain", "자동 판정 불가 — 사람이 본다"


def _is_re_compile(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compile"
        and bool(node.args)
    )


def _literal_pattern(call: ast.Call) -> str | None:
    arg = call.args[0]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _lexicon_derived_names(tree: ast.AST) -> frozenset[str]:
    """이 모듈에서 사전(lexicon)으로부터 만들어진 이름들. 조립 정규식의 어휘 출처 판정에 쓴다."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        source = ast.unparse(node.value)
        if any(marker in source for marker in _LEXICON_MARKERS):
            names.add(target.id)
    return frozenset(names)


def _is_lexicon_composed(call: ast.Call, lexicon_names: frozenset[str]) -> bool:
    """조립 정규식이 사전 어휘로 만들어졌는지(직접 호출 또는 사전 파생 이름 참조)."""
    source = ast.unparse(call)
    if any(marker in source for marker in _LEXICON_MARKERS):
        return True
    return any(
        isinstance(node, ast.Name) and node.id in lexicon_names
        for node in ast.walk(call)
    )


def _row(
    path: Path, node: ast.AST, name: str, pattern: str | None, source: str, *, composed: bool = False
) -> dict[str, Any]:
    if pattern is None:
        if composed:
            kind, reason = "composed", "사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다)"
        else:
            kind, reason = "domain", "동적으로 조립된 패턴 — 소스를 직접 읽어야 한다"
        alternatives = 0
    else:
        kind, reason = classify(pattern)
        alternatives = len([part for part in pattern.split("|") if part])
    return {
        "file": path.name,
        "line": getattr(node, "lineno", 0),
        "name": name,
        "pattern": pattern if pattern is None or len(pattern) <= 240 else pattern[:240] + "…",
        "alternatives": alternatives,
        "class": kind,
        "reason": reason,
        "source": source,
        "decision": "",
    }


def _korean_terms(node: ast.AST) -> list[str]:
    """집합/튜플/리스트 리터럴에 담긴 한글 문자열 낱말을 뽑는다(중첩은 보지 않는다)."""
    elements: list[ast.AST] = []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements = list(node.elts)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"set", "frozenset"}:
        if node.args:
            return _korean_terms(node.args[0])
        return []
    return [
        element.value
        for element in elements
        if isinstance(element, ast.Constant) and isinstance(element.value, str) and _HANGUL.search(element.value)
    ]


def _wordlist_row(path: Path, node: ast.AST, name: str, terms: list[str]) -> dict[str, Any]:
    preview = ", ".join(terms[:8]) + ("…" if len(terms) > 8 else "")
    return {
        "file": path.name,
        "line": getattr(node, "lineno", 0),
        "name": name,
        "pattern": "{" + preview + "}",
        "alternatives": len(terms),
        "class": "wordlist",
        "reason": f"코드에 박힌 한글 낱말집합 {len(terms)}개 — 렉시콘/레지스트리로 옮길 수 있다",
        "source": "wordlist",
        "decision": "",
    }


def _collect_wordlists(path: Path, node: ast.Assign, name: str) -> list[dict[str, Any]]:
    """낱말집합 상수. 딕트는 값마다(깊이 2까지) 따로 센다 — 키가 곧 어휘 그룹 이름이다."""
    if not _RULE_CONSTANT_NAME.match(name):
        return []
    value = node.value
    terms = _korean_terms(value)
    if len(terms) >= WORDLIST_MIN_TERMS:
        return [_wordlist_row(path, node, name, terms)]
    rows: list[dict[str, Any]] = []
    if isinstance(value, ast.Dict):
        for key, item in zip(value.keys, value.values):
            key_text = key.value if isinstance(key, ast.Constant) else "?"
            nested = _korean_terms(item)
            if len(nested) >= WORDLIST_MIN_TERMS:
                rows.append(_wordlist_row(path, node, f'{name}["{key_text}"]', nested))
    return rows


def collect(path: Path) -> list[dict[str, Any]]:
    """모듈 하나에서 표면어 규칙 4종(상수 정규식·묶음 정규식·인라인 정규식·낱말집합)을 뽑는다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    claimed: set[int] = set()  # 이름으로 이미 센 re.compile 호출(인라인 스캔에서 중복 집계 방지)
    lexicon_names = _lexicon_derived_names(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        value = node.value

        if _is_re_compile(value):
            claimed.add(id(value))
            rows.append(_row(path, node, name, _literal_pattern(value), "constant",
                             composed=_is_lexicon_composed(value, lexicon_names)))
            continue

        # 묶음: 튜플/리스트/집합/딕트에 담긴 정규식(이전 스캐너의 사각지대).
        members: list[tuple[str, ast.AST]] = []
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            members = [(f"{name}[{index}]", element) for index, element in enumerate(value.elts)]
        elif isinstance(value, ast.Dict):
            members = [
                (f'{name}["{key.value if isinstance(key, ast.Constant) else "?"}"]', item)
                for key, item in zip(value.keys, value.values)
            ]
        compiled_members = [(member_name, item) for member_name, item in members if _is_re_compile(item)]
        for member_name, item in compiled_members:
            claimed.add(id(item))
            rows.append(_row(path, node, member_name, _literal_pattern(item), "collection",
                             composed=_is_lexicon_composed(item, lexicon_names)))
        if compiled_members:
            continue

        rows.extend(_collect_wordlists(path, node, name))

    # 인라인 한글 정규식: 이름 없이 호출되는 규칙. 한글이 없는 것(공백 정리 등)은 구조라 세지 않는다.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in claimed:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not node.args:
            continue
        # 이름에 담기지 않은 re.compile 도 인라인이다(호출 인자·컴프리헨션 안에서 만들어지는 규칙).
        if func.attr not in _INLINE_RE_FUNCTIONS and func.attr != "compile":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "re"):
            continue
        pattern = _literal_pattern(node)
        if pattern is None or not _HANGUL.search(pattern):
            continue
        rows.append(_row(path, node, f"<inline re.{func.attr}>", pattern, "inline"))

    return rows


def scan(root: Path = ROOT) -> list[dict[str, Any]]:
    """저장소 최상위 모듈 전체를 훑어 정렬된 인벤토리를 만든다."""
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.py")):
        if path.name in EXCLUDED_MODULES:
            continue
        try:
            rows.extend(collect(path))
        except SyntaxError:
            continue
    rows.sort(key=lambda row: (CLASS_ORDER[row["class"]], -(row.get("alternatives") or 0),
                               row["file"], row["line"]))
    return rows


def counts(rows: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """분류별 개수. lexical·wordlist·domain 이 이행 진행도이자 래칫 대상이다."""
    rows = scan() if rows is None else rows
    return {kind: sum(1 for row in rows if row["class"] == kind) for kind in CLASS_ORDER}


def baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    """기준선 파일 전체(counts + 사유 + 스코프 버전)."""
    return json.loads(path.read_text(encoding="utf-8"))


def regressions(current: dict[str, int], recorded: dict[str, int]) -> list[str]:
    """래칫 대상 분류에서 기준선을 넘은 항목의 사람이 읽을 설명."""
    return [
        f"{kind}: {current.get(kind, 0)} > 기준선 {recorded[kind]}"
        for kind in RATCHETED_CLASSES
        if kind in recorded and current.get(kind, 0) > recorded[kind]
    ]


def write_baseline(current: dict[str, int], *, reason: str = "", path: Path = BASELINE_PATH) -> dict[str, Any]:
    """기준선을 갱신한다. 래칫 대상 분류를 **올리는** 갱신은 사유 없이는 거부한다.

    "올리려면 사유가 필요하다"를 문서가 아니라 코드로 만든다 — 스코프 확대처럼 정당한 상향은
    사유와 함께 파일에 남고, 규칙을 하나 더 넣으려고 슬쩍 올리는 것은 여기서 막힌다.
    """
    recorded = baseline(path)["counts"] if path.exists() else {}
    raised = [kind for kind in RATCHETED_CLASSES if current.get(kind, 0) > recorded.get(kind, current.get(kind, 0))]
    if raised and not reason.strip():
        raise ValueError(
            "기준선 상향에는 사유가 필요하다(--reason). 올라가는 분류: " + ", ".join(raised)
        )
    payload = {
        "description": "코드 규칙 래칫 상한. 이행이 진행되면 내려간다. 올리려면 사유가 필요하다.",
        "ratcheted": list(RATCHETED_CLASSES),
        "counts": current,
        "raise_reason": reason.strip(),
        "previous_counts": recorded,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
