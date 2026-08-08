"""Deterministic checks for model-authored audience clarification claims."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

import lexicon_patterns

_DURATION_RE = re.compile(
    r"(?:\d+(?:\.\d+)?|한|두|세|네|반)\s*(?:시간|일|주일|주|개월|달|분기|년)"
)
_EXTRA_TEMPORAL_QUALIFIERS = ("지난", "동안", "오랫동안", "장기", "예전", "과거")


def _term_pattern(term: str) -> re.Pattern[str] | None:
    normalized = unicodedata.normalize("NFKC", term).strip()
    if not normalized:
        return None
    pieces = [re.escape(piece) for piece in normalized.split()]
    body = r"\s*".join(pieces)
    if normalized.isascii() and all(char.isalnum() or char in "_-" for char in normalized):
        body = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])"
    return re.compile(body, re.IGNORECASE)


def _term_spans(query: str, terms: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in terms:
        pattern = _term_pattern(term)
        if pattern is not None:
            spans.extend((match.start(), match.end()) for match in pattern.finditer(query))
    return spans


def _current_subject_value_spans(
    query: str, catalog: Mapping[str, Any]
) -> list[tuple[int, int]]:
    fields = catalog.get("fields")
    domains = catalog.get("value_domains")
    if not isinstance(fields, Mapping) or not isinstance(domains, Mapping):
        return []
    spans: list[tuple[int, int]] = []
    for field_id, field in fields.items():
        if not str(field_id).startswith("subject.") or not isinstance(field, Mapping):
            continue
        domain = domains.get(field.get("value_domain"))
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            continue
        for canonical, declaration in values.items():
            aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
            terms = [str(canonical), *(str(alias) for alias in aliases or () if alias)]
            spans.extend(_term_spans(query, terms))
    # A long catalog phrase owns temporal-looking words inside it (for example
    # a declared value alias that itself starts with '최근').
    return sorted(spans, key=lambda span: (span[0], -(span[1] - span[0])))


def _has_external_temporal_qualifier(
    query: str, claim_spans: list[tuple[int, int]]
) -> bool:
    temporal_terms = {
        *_EXTRA_TEMPORAL_QUALIFIERS,
        *lexicon_patterns.vocabulary("source_latest_selector"),
        *lexicon_patterns.vocabulary("calendar_previous_month"),
        *lexicon_patterns.vocabulary("temporal_within_marker"),
        *lexicon_patterns.vocabulary("temporal_after_marker"),
    }
    temporal_spans = _term_spans(query, sorted(temporal_terms))
    temporal_spans.extend(
        (match.start(), match.end()) for match in _DURATION_RE.finditer(query)
    )
    return any(
        not any(start >= claim_start and end <= claim_end for claim_start, claim_end in claim_spans)
        for start, end in temporal_spans
    )


def fabricated_period_issue_for_current_catalog_value(
    query: str,
    issue: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> bool:
    """Reject a period omission invented for a catalog-grounded current value.

    This is deliberately one-way evidence. It proves only that a current
    subject value is already named and no separate temporal qualifier exists;
    inactivity phrases without such a catalog value remain untouched.
    """

    if issue.get("code") != "missing_argument" or issue.get("argument") != "period":
        return False
    claim_spans = _current_subject_value_spans(query, catalog)
    return bool(claim_spans) and not _has_external_temporal_qualifier(query, claim_spans)


def _bare_period_issue_span(query: str, issue: Mapping[str, Any]) -> tuple[int, int] | None:
    """기간을 품지 않은 기간 결핍 신고의 근거 구간. 그 밖이면 ``None``.

    근거 구간이 기간을 품고 있으면 이 판정의 소관이 아니다. 그때의 신고는 '원문이 말한 기간과
    모순'이고, 그 반박의 소유자는 :mod:`temporal_clause` 다(반박 근거가 둘이면 갈라진다).
    """

    if issue.get("code") != "missing_argument" or issue.get("argument") != "period":
        return None
    evidence = issue.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    start, end = evidence.get("start"), evidence.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end <= len(query)
    ):
        return None
    if _DURATION_RE.search(query[start:end]):
        return None
    return start, end


def bare_period_span_owned_by_spans(
    query: str,
    span: tuple[int, int],
    spans: Iterable[tuple[int, int]],
) -> bool:
    """시간 낱말 구간이 **이미 컴파일된 절**과 같은 절에 있는가.

    :func:`bare_period_issue_owned_by_spans` 의 알맹이다. 좌표로 묻는 갈래를 따로 두는 이유는
    생산자가 둘이기 때문이다 — 모델이 신고한 기간 결핍(issue)과 애플리케이션이 원문에서
    직접 검출한 결핍(:mod:`audience_validators`)은 같은 질문을 하지만 들고 오는 것이 다르다.
    판정이 갈리면 한쪽이 연 것을 다른 쪽이 닫는다(실측 2026-08-08: 모델이 옳은 전이 표현을
    냈는데도 검증기가 같은 자리에 기간을 요구해 재시도가 소진됐다).
    """

    import audience_frame  # 지연 import(순환 방지)

    return any(
        audience_frame.in_same_clause(query, span, (int(start), int(end)))
        for start, end in spans or ()
    )


def period_span_owned_by_lowered_clause(
    query: str,
    span: tuple[int, int],
    *,
    today: Any = None,
) -> bool:
    """그 시간 낱말이 붙은 절이 **지금 이대로 낮춰지는가**(그러면 기간은 결핍이 아니다)."""

    import lowering_planner  # 지연 import(순환 방지)

    try:
        plans = lowering_planner.plans_for_query(query, today=today)
    # 계획을 못 세우면 반박하지 않는다(추측 금지) — 판정 불가는 결핍의 근거가 아니다.
    except Exception:
        return False
    return bare_period_span_owned_by_spans(
        query, span, (plan.obligation.source_span for plan in plans)
    )


def bare_period_issue_owned_by_spans(
    query: str,
    issue: Mapping[str, Any],
    spans: Iterable[tuple[int, int]],
) -> bool:
    """기간 결핍 신고가 **이미 컴파일된 절**과 같은 절에 붙었는가.

    맨 '최근'은 언제나 결핍이 아니다. 월 스냅샷의 전이 조건('등급이 승급한')은 관측 자체가
    '직전 관측 대비 이번 관측'이므로, 그 절에 붙은 '최근'은 as_of 의 동어반복이지 사용자가
    빠뜨린 값이 아니다. 반대로 같은 낱말이 구매 집계 절에 붙으면 진짜 결핍이다 — 그 절은
    창이 없으면 뜻이 정해지지 않는다.

    그 둘을 가르는 근거를 표면어 목록으로 적지 않는다. 근거는 호출자가 넘기는 ``spans``,
    곧 **실제로 낮춰진 절이 소유한 원문 구간**이다. 창이 있어야 성립하는 절이라면 애초에
    낮춰지지 않으므로 넘길 구간도 없다.

    구간을 '덮는가'가 아니라 '같은 절인가'로 묻는 이유는 자리다 — 기간 결핍 신고는 조건 절이
    아니라 그 옆의 시간 낱말에 붙으므로, 포함 관계로는 영원히 만나지 않는다.
    """

    span = _bare_period_issue_span(query, issue)
    if span is None:
        return False
    return bare_period_span_owned_by_spans(query, span, spans)


def period_issue_owned_by_lowered_clause(
    query: str,
    issue: Mapping[str, Any],
    *,
    today: Any = None,
) -> bool:
    """기간 결핍 신고가 **스스로 창을 확정하는 절**에 붙었는지 판정한다.

    :func:`bare_period_issue_owned_by_spans` 와 같은 규칙이고, 근거로 삼는 구간을
    :mod:`lowering_planner` 에게서 받는다는 점만 다르다 — 질문은 하나다: **그 절이 지금
    이대로 낮춰지는가.** 낮춰졌다면 창은 이미 확정된 것이고, 창이 있어야 성립하는 절이라면
    낮춤이 애초에 없다(그 판정자는 전부-또는-아무것도이고 fail-safe 다). 그래서 새 지표·새
    시간 문법이 카탈로그에 들어와도 이 함수는 그대로다.

    이 판정은 :func:`fabricated_period_issue_for_current_catalog_value` 와 마찬가지로
    **단방향 증거**다 — 낮춤이 없으면 아무것도 주장하지 않고 모델의 신고를 그대로 둔다.

    (2026-08-05 에 지워진 ``latest_transition_owns_period_issue`` 가 하던 일이지만 근거가
    다르다. 그 함수는 표면어로 '최근이 최신 스냅샷 선택임'을 선언했고, 이 함수는 그 절이
    실제로 컴파일된다는 사실을 근거로 삼는다.)
    """

    if _bare_period_issue_span(query, issue) is None:
        return False

    import lowering_planner  # 지연 import(순환 방지)

    try:
        plans = lowering_planner.plans_for_query(query, today=today)
    # 계획을 못 세우면 반박하지 않는다(추측 금지) — 판정 불가는 결핍의 근거가 아니다.
    except Exception:
        return False
    return bare_period_issue_owned_by_spans(
        query, issue, (plan.obligation.source_span for plan in plans)
    )


# `latest_transition_owns_period_issue` 는 2026-08-05 제거됐다(위 함수들의 docstring 참고).


__all__ = [
    "bare_period_issue_owned_by_spans",
    "bare_period_span_owned_by_spans",
    "fabricated_period_issue_for_current_catalog_value",
    "period_issue_owned_by_lowered_clause",
    "period_span_owned_by_lowered_clause",
]
