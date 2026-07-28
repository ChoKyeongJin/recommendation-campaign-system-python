"""정규식 인벤토리 — 코드에 박힌 패턴을 어휘형/문법형/업무의미형으로 분류한다.

이행(정규식 → 렉시콘)의 작업 목록을 사람 기억이 아니라 파일로 만든다. 판정 기준은 하나다:

    이 패턴이 열거하는 것이 **단어 목록**인가, **구조**인가?

  * lexical  (어휘형)   — 리터럴 표면어의 나열. 새 표현이 생길 때마다 코드를 고치게 만드는 것들이다.
                          **데이터(:mod:`lexicon_patterns`)로 옮길 대상.**
  * grammar  (문법형)   — 수량자·문자클래스·룩어라운드·경계 등 구조를 표현. 코드에 남는다.
  * domain   (업무의미형) — 어휘와 구조가 섞여 업무 규칙을 인코딩. 보통 어휘 부분만 데이터로 빼고
                          구조는 남긴다(개별 판단).

:func:`counts` 는 계약 테스트의 **래칫**이 쓴다 — 어휘형 개수가 기준선을 넘으면 "새 표현을 또 코드로
받았다"는 뜻이므로 테스트가 깨진다. CLI 는 ``tools/regex_inventory.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
# 인벤토리 자신과 이관 대상 어휘를 담은 모듈은 스캔에서 뺀다(자기 참조·중복 집계 방지).
EXCLUDED_MODULES = frozenset({"regex_inventory.py"})

# 구조를 나타내는 메타문자(하나라도 있으면 순수 어휘형이 아니다).
_STRUCTURAL = re.compile(r"""\\[dswbDSWB]|\[|\{\d|\(\?[=!<:P]|[+*]|\.\.|(?<!\\)\.""")
# 순수 어휘형이 허용하는 것: 교대(|)와 리터럴뿐. 그룹·수량자·앵커가 하나라도 있으면 구조가 섞인
# 것이므로 domain 으로 내린다 — 조사 결합('(?:이|가|을)?')은 문법이라 통째로 데이터화할 수 없다.
_ALLOWED_IN_LEXICAL = re.compile(r"^[^\\\[\]{}()+*.^$?]*$")
# 한글 표면어가 실제로 들어 있는가(어휘형의 필요조건).
_HANGUL = re.compile(r"[가-힣]")

CLASS_ORDER = {"lexical": 0, "domain": 1, "grammar": 2}


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


def collect(path: Path) -> list[dict[str, Any]]:
    """모듈 최상위의 ``NAME = re.compile("...")`` 상수를 뽑는다(동적 조립은 대상 밖)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "compile" and call.args):
            continue
        arg = call.args[0]
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            rows.append({
                "file": path.name, "line": node.lineno, "name": target.id,
                "pattern": None, "alternatives": 0, "class": "domain",
                "reason": "동적으로 조립된 패턴 — 소스를 직접 읽어야 한다", "decision": "",
            })
            continue
        pattern = arg.value
        kind, reason = classify(pattern)
        rows.append({
            "file": path.name, "line": node.lineno, "name": target.id,
            "pattern": pattern if len(pattern) <= 240 else pattern[:240] + "…",
            "alternatives": len([p for p in pattern.split("|") if p]),
            "class": kind, "reason": reason, "decision": "",
        })
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
    """분류별 개수. 어휘형 수치가 이행 진행도이자 래칫 대상이다."""
    rows = scan() if rows is None else rows
    return {kind: sum(1 for row in rows if row["class"] == kind) for kind in CLASS_ORDER}
