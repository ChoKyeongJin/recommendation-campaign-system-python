"""미해석 표현 수집과 A/B/C 분류 — "새 단어마다 코딩"을 "주 1회 분류"로 바꾸는 루틴.

이행의 마지막 조각은 도구가 아니라 **루프**다. 파서가 못 푼 표현이 어딘가에 쌓이고, 사람이 주기적으로
셋 중 하나로 판정하고, 판정에 따라 서로 다른 비용의 작업이 열린다.

    A  어휘   기존 능력의 새 표면어        → docs/data/parser_lexicon.json 에 한 줄 (배포 불필요)
    B  파라미터 기존 팩트의 새 조건 모양     → 레지스트리 한 줄 (targeting_ir / _FilterSpec)
    C  능력   새 grain·조인이 필요          → 코드 (드물게, 제대로)

C 가 코드인 것은 정상이다. 문제는 A 가 코드로 가는 것이었다. 이 모듈은 그 비율을 보이게 만든다.

전제 조건이 하나 있다: **미해석은 fail-close 로 표시돼야 한다.** 조건이 조용히 사라지면 여기 안 쌓이고,
안 쌓이면 루프가 돌지 않는다(:mod:`slot_policy` 의 silent_loss_slots 가 그 구멍을 센다).

순수 모듈 불변식: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import lexicon_patterns

CLASS_LEXICON = "A"      # 어휘 — 데이터 한 줄
CLASS_PARAMETER = "B"    # 파라미터 모양 — 레지스트리 한 줄
CLASS_CAPABILITY = "C"   # 새 능력 — 코드
CLASSES = (CLASS_LEXICON, CLASS_PARAMETER, CLASS_CAPABILITY)

CLASS_LABEL = {
    CLASS_LEXICON: "A 어휘(사전 한 줄)",
    CLASS_PARAMETER: "B 파라미터(레지스트리 한 줄)",
    CLASS_CAPABILITY: "C 능력(코드)",
}

LOG_PATH_ENV = "UNRESOLVED_LOG"

# 플랜에서 '못 풀었다'는 사실이 남는 자리들. 어느 하나라도 있으면 그 요청은 미해석 사례다.
UNRESOLVED_PLAN_KEYS = ("unresolved_source_conditions", "failure_log", "unsupported")

# ── 조건 0개 탐지 ─────────────────────────────────────────────────────────────
# 위 세 표시는 **이미 아는 신호를 놓쳤을 때만** 남는다. 신호 감지 자체가 정규식이라, 완전히 낯선
# 표현('혼수 준비중인', '웨딩 단계 이탈')은 신호로도 인식되지 않아 아무 표시 없이 통과한다 —
# 모르는 것을 모른다고조차 못 하는 상태이고, 그러면 이 큐가 영영 비어 루프가 돌지 않는다.
#
# 그래서 표시에 의존하지 않는 탐지를 하나 더 둔다: **오디언스를 서술했는데 타겟 조건이 하나도
# 안 붙은 경우**. 오탐이 거의 없는 대신(전수 탐지는 아니다) 완전 미해석을 확실히 잡는다.
# 부분 소실('안양'만 빠짐)은 이 방법으로 못 잡는다 — 그건 shadow 비교의 only_candidate 판정이
# 담당한다(두 탐지가 서로의 사각을 덮는다).
NO_CONDITION_MARKER = "no_target_conditions"
# 오디언스 전체를 가리키는 표현('전체 회원'). 조건이 없는 것이 정상이므로 탐지에서 뺀다.
# 낱말은 사전이 소유한다 — 새 표현('싹 다' 등)은 parser_lexicon.json 의 whole_audience 에 추가한다.
_WHOLE_AUDIENCE_RE = lexicon_patterns.pattern("whole_audience")
# 타겟 조건이 사는 컨테이너(이 안이 비면 오디언스가 무제한이라는 뜻).
_TARGET_CONTAINERS = ("target_user", "exclude")


def _has_target_conditions(plan: dict[str, Any]) -> bool:
    for container in _TARGET_CONTAINERS:
        holder = plan.get(container)
        if isinstance(holder, dict) and any(
            value not in (None, [], {}, "", False)
            for key, value in holder.items()
            if isinstance(key, str) and not key.startswith("_")
        ):
            return True
    return False


def _describes_an_audience(query: str) -> bool:
    """문장이 '어떤 회원'을 서술하고 있는가 — 회원 명사 + 조건 언어가 함께 있을 때만 참."""
    if not query or _WHOLE_AUDIENCE_RE.search(query):
        return False
    has_member_noun = any(noun in query for noun in lexicon_patterns.terms("purchase_rank_target"))
    has_condition_language = any(term in query for term in lexicon_patterns.terms("condition_language"))
    return has_member_noun and has_condition_language


def detect_no_condition(plan: dict[str, Any], query: str | None) -> str | None:
    """오디언스를 서술했는데 타겟 조건이 하나도 없으면 사유 문자열, 아니면 None."""
    text = str(query or plan.get("original_query") or plan.get("raw_query") or "")
    if not _describes_an_audience(text) or _has_target_conditions(plan):
        return None
    return "오디언스를 서술했는데 타겟 조건이 하나도 만들어지지 않았다(무제한 오디언스)"


def log_path() -> Path | None:
    raw = os.getenv(LOG_PATH_ENV, "").strip()
    return Path(raw) if raw else None


def extract(plan: dict[str, Any], *, query: str | None = None) -> dict[str, Any] | None:
    """플랜에서 미해석 사례를 뽑는다. 미해석이 없으면 None.

    미해석의 근거는 세 곳에서 온다: 명시적 미지원 표시(``unsupported``), 소스 조건 미해소
    (``unresolved_source_conditions``), 실패 로그(``failure_log``).
    """
    evidence: dict[str, Any] = {}
    for key in UNRESOLVED_PLAN_KEYS:
        value = plan.get(key)
        if value:
            evidence[key] = value
    no_condition = detect_no_condition(plan, query)
    if no_condition:
        evidence[NO_CONDITION_MARKER] = no_condition
    if not evidence:
        return None
    return {
        "query": query or plan.get("original_query") or plan.get("raw_query"),
        "intent": plan.get("intent"),
        "evidence": evidence,
    }


def record(case: dict[str, Any], *, path: Path | None = None) -> bool:
    """미해석 사례 하나를 큐에 덧붙인다. 쓰기 실패는 삼킨다(관찰이 요청을 깨뜨리지 않는다)."""
    target = path or log_path()
    if target is None or not case:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def load(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or log_path()
    if target is None or not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("query"):
            rows.append(payload)
    return rows


# ── 자동 분류 초안 ────────────────────────────────────────────────────────────
# 사람의 판단을 대신하지 않는다. 목적은 "무엇부터 볼지" 정렬해 주는 것이다.
_TOKEN_RE = re.compile(r"[가-힣]{2,}")
# 파라미터(B) 냄새: 값·단위·비교어가 있는데 조건이 안 붙었다면, 어휘가 아니라 모양의 문제일 확률이 높다.
_PARAMETER_HINT_RE = re.compile(r"\d[\d,]*\s*(?:회|번|개|건|명|원|일|주|개월|달|년|%)|이상|이하|초과|미만|이내")


def _all_vocabulary_terms() -> set[str]:
    terms: set[str] = set()
    for name in lexicon_patterns.pattern_names():
        terms.update(lexicon_patterns.terms(name))
    return terms


def classify(query: str, evidence: dict[str, Any] | None = None) -> tuple[str, str]:
    """(분류 초안, 사유). 규칙은 셋뿐이고, 애매하면 가장 비싼 C 로 보낸다.

    보수적으로 C 로 보내는 이유: A/B 로 잘못 분류되면 "사전 한 줄이면 된다"고 믿고 닫히지만,
    C 로 잘못 분류되면 검토 과정에서 A/B 임이 드러난다. 틀렸을 때 회복 가능한 쪽으로 기운다.
    """
    tokens = set(_TOKEN_RE.findall(query or ""))
    known = _all_vocabulary_terms()

    # 이미 아는 낱말이 문장의 뼈대를 이루는데 조건이 안 붙었다면, 남은 낱말 하나가 모자란 것이다.
    overlap = tokens & known
    novel = tokens - known
    if overlap and len(novel) <= 2 and novel:
        return CLASS_LEXICON, f"기존 어휘({', '.join(sorted(overlap)[:3])})와 함께 낯선 낱말 {sorted(novel)} 만 남는다"
    if _PARAMETER_HINT_RE.search(query or ""):
        return CLASS_PARAMETER, "수치·비교어가 있는데 조건이 안 붙었다 — 어휘가 아니라 파라미터 모양 문제로 보인다"
    return CLASS_CAPABILITY, "기존 어휘와 겹치는 뼈대가 없다 — 새 능력일 가능성이 높다"


def triage(cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """미해석 사례들을 표현별로 묶어 빈도순 분류 초안 목록을 만든다."""
    grouped: dict[str, dict[str, Any]] = {}
    for case in cases:
        query = str(case.get("query") or "").strip()
        if not query:
            continue
        entry = grouped.setdefault(query, {"query": query, "count": 0, "evidence_keys": Counter()})
        entry["count"] += 1
        for key in (case.get("evidence") or {}):
            entry["evidence_keys"][key] += 1

    rows: list[dict[str, Any]] = []
    for entry in grouped.values():
        kind, reason = classify(entry["query"])
        rows.append({
            "query": entry["query"],
            "count": entry["count"],
            "evidence": sorted(entry["evidence_keys"]),
            "class_draft": kind,
            "reason": reason,
            "decision": "",  # 사람이 채운다
        })
    rows.sort(key=lambda row: (-row["count"], CLASSES.index(row["class_draft"]), row["query"]))
    return rows


def summary(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["class_draft"] for row in rows)
    return {kind: counts.get(kind, 0) for kind in CLASSES}
