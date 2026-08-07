"""Deterministic counter-evidence for the final LLM SQL semantic verifier.

The module is deliberately independent of ``graph_rag``.  Callers inject consent signal parsing,
physical bindings, labels, subject identity, and SQL dialect.  This keeps catalog ownership at the
application boundary while making AST coverage and verdict reconciliation deterministic and testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp

import consent_cardinality
import plan_decisions
import temporal_clause

CONSENT_CARDINALITY_QUANTIFIER_RE = (
    consent_cardinality.CONSENT_CARDINALITY_QUANTIFIER_RE
)
NEGATION_CUE_RE = re.compile(
    r"없|않|못[한했하받]|아닌|아니|제외|미사용|미구매|미접속|미반응|미결제|미가입|미방문|비동의|취소|해지|중단|"
    r"안\s*[한함했하샀]|\bNOT\b",
    re.IGNORECASE,
)
_JOIN_ISSUE_CUE_RE = re.compile(
    r"join|relationship|matching|mapping|association|조인|연결|연관|매칭|매핑|식별자|키",
    re.IGNORECASE,
)
# 조인 자체를 지목한 판정만 고르는 좁은 단서. '매칭'·'키'는 임계값 누락 같은 다른 판정문에도
# 섞이므로, 컬럼명 없는 판정을 통째로 면제할 때는 관계어(조인/관계)까지 있어야 한다.
_JOIN_RELATION_CUE_RE = re.compile(r"join|relationship|조인|관계", re.IGNORECASE)
_CONSENT_ISSUE_CUE_RE = re.compile(
    r"consent|opt[ -]?in|수신\s*동의|동의\s*(?:여부|조건|필터)?",
    re.IGNORECASE,
)
# 관측 창을 지목한 판정만 고르는 단서. 기간 이야기가 아닌 판정까지 기본 기간 영수증으로 면제하면
# 정말 원문에 없는 필터가 조용히 통과한다. 영문 단서는 단어 경계로 묶는다 — CAMP_SDATE 같은
# 컬럼명이 'date' 로 걸리면 안 된다('_' 는 단어 문자라 경계가 서지 않는다).
_PERIOD_ISSUE_CUE_RE = re.compile(
    r"기간|기준일|최근|관측\s*창|시간\s*창|날짜|일자|\b(?:window|period|date|recent)\b",
    re.IGNORECASE,
)
# 판정문이 **이름 붙여 지목한 창**. 단위가 붙은 수만 본다 — 임계값('3회')이나 렌더된 날짜
# 리터럴('20260802')은 창 크기 주장이 아니라서 비교 대상이 아니다.
_ISSUE_DURATION_RE = re.compile(
    r"(\d+)\s*(일|주일|주|개월|달|년|days?|weeks?|months?|years?)", re.IGNORECASE
)
_ISSUE_DURATION_UNITS = {
    "일": "day", "day": "day", "days": "day",
    "주": "week", "주일": "week", "week": "week", "weeks": "week",
    "개월": "month", "달": "month", "month": "month", "months": "month",
    "년": "year", "year": "year", "years": "year",
}


def _window_in_days(value: int, unit: str) -> int | None:
    """창을 일수로 환산한다. 달·해는 달력 길이가 고정이 아니라 환산하지 않는다(CLAUDE.md §16)."""
    if unit == "day":
        return value
    return value * 7 if unit == "week" else None


def consent_coverage_receipts(
    original_query: str,
    sql: str,
    *,
    requested_signals: Mapping[str, str],
    bindings: Mapping[str, Sequence[str]],
    declined_value: str,
    labels: Mapping[str, str],
    member_table: str,
    member_alias: str,
    dialect: str,
) -> list[dict[str, Any]]:
    """Return one physical SQL coverage receipt per required consent field.

    ``bindings`` entries use the application registry shape ``(category, column, agreed_value)``.
    Cardinality requests (any-of/exact-N) are intentionally deferred: a field-wise receipt cannot prove
    their Boolean/cardinality contract.  Conjunction/all and singular requests remain fail-closed.
    """
    compact_query = re.sub(r"\s+", "", original_query or "").casefold()
    if CONSENT_CARDINALITY_QUANTIFIER_RE.search(compact_query):
        return []
    requested = dict(requested_signals)
    if not requested:
        return []

    try:
        root = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        root = None

    member_aliases: set[str] = {member_table.casefold(), member_alias.casefold()}
    if root is not None:
        member_aliases.update(
            str(table.alias_or_name).casefold()
            for table in root.find_all(exp.Table)
            if str(table.name).casefold() == member_table.casefold()
        )

    def has_member_column(node: exp.Expression, column: str) -> bool:
        return any(
            ref.name.casefold() == column
            and (not ref.table or ref.table.casefold() in member_aliases)
            for ref in node.find_all(exp.Column)
        )

    def literal_values(node: exp.Expression) -> set[str]:
        return {
            str(literal.this).casefold()
            for literal in node.find_all(exp.Literal)
            if literal.is_string
        }

    def is_logically_negated(node: exp.Expression) -> bool:
        negations = 0
        ancestor = node.parent
        while ancestor is not None:
            if isinstance(ancestor, exp.Not):
                negations += 1
            ancestor = ancestor.parent
        return negations % 2 == 1

    def is_disjunctive(node: exp.Expression) -> bool:
        ancestor = node.parent
        while ancestor is not None and not isinstance(ancestor, (exp.Where, exp.Having)):
            if isinstance(ancestor, exp.Or):
                return True
            ancestor = ancestor.parent
        return False

    def comparison_polarity(
        node: exp.Expression, column: str, agreed_value: str,
    ) -> str | None:
        agreed = agreed_value.casefold()
        declined = declined_value.casefold()
        base_polarity: str | None = None
        if isinstance(node, (exp.EQ, exp.NEQ)):
            left, right = node.this, node.expression
            value_side: exp.Expression | None = None
            if has_member_column(left, column):
                value_side = right
            elif has_member_column(right, column):
                value_side = left
            if value_side is not None:
                values = literal_values(value_side)
                if agreed in values:
                    base_polarity = "+" if isinstance(node, exp.EQ) else "-"
                elif declined and declined in values:
                    base_polarity = "-" if isinstance(node, exp.EQ) else "+"
        if isinstance(node, exp.In) and has_member_column(node.this, column):
            values = {
                str(literal.this).casefold()
                for expression in node.expressions
                for literal in expression.find_all(exp.Literal)
                if literal.is_string
            }
            if values == {agreed}:
                base_polarity = "+"
            elif declined and values == {declined}:
                base_polarity = "-"
        if base_polarity is not None and is_logically_negated(node):
            return "-" if base_polarity == "+" else "+"
        return base_polarity

    predicates: list[exp.Expression] = []
    if root is not None:
        predicate_by_id: dict[int, exp.Expression] = {}
        for scope in (*root.find_all(exp.Where), *root.find_all(exp.Having)):
            for predicate_type in (exp.EQ, exp.NEQ, exp.In):
                for predicate in scope.this.find_all(predicate_type):
                    predicate_by_id[id(predicate)] = predicate
        predicates.extend(predicate_by_id.values())

    receipts: list[dict[str, Any]] = []
    for canonical, expected_polarity in requested.items():
        entry = bindings.get(canonical)
        label = labels.get(f"{canonical}:{expected_polarity}", canonical)
        if not isinstance(entry, Sequence) or isinstance(entry, str) or len(entry) < 3:
            receipts.append({
                "canonical": canonical,
                "label": label,
                "status": "binding_missing",
                "satisfied": False,
                "evidence": "member_target_filters.eq_filters",
            })
            continue
        configured_column, agreed_value = str(entry[1]), str(entry[2])
        column = configured_column.split(".")[-1].casefold()
        observations = [
            (polarity, not is_disjunctive(predicate))
            for predicate in predicates
            if (polarity := comparison_polarity(predicate, column, agreed_value)) is not None
        ]
        actual_polarities = {polarity for polarity, _mandatory in observations}
        mandatory_polarities = {polarity for polarity, mandatory in observations if mandatory}
        if actual_polarities == {expected_polarity} and mandatory_polarities == {expected_polarity}:
            status = "covered"
        elif not actual_polarities:
            status = "missing"
        elif actual_polarities == {expected_polarity} and not mandatory_polarities:
            status = "non_conjunctive"
        elif expected_polarity in actual_polarities:
            status = "conflicting_predicates"
        else:
            status = "polarity_mismatch"
        receipts.append({
            "canonical": canonical,
            "label": label,
            "column": configured_column.split(".")[-1].upper(),
            "expected_polarity": "opted_in" if expected_polarity == "+" else "not_opted_in",
            "expected_value": agreed_value,
            "declined_value": declined_value or None,
            "actual_polarities": sorted(
                "opted_in" if polarity == "+" else "not_opted_in" for polarity in actual_polarities
            ),
            "mandatory_polarities": sorted(
                "opted_in" if polarity == "+" else "not_opted_in" for polarity in mandatory_polarities
            ),
            "status": status,
            "satisfied": status == "covered",
            "evidence": "member_target_filters.eq_filters",
        })
    return receipts


def normalize_verdict(
    data: Any, *, allowed_issue_types: set[str] | frozenset[str],
) -> dict[str, Any] | None:
    """Normalize tri-state and legacy Boolean LLM verdicts without making review blocking."""
    if not isinstance(data, dict):
        return None
    raw_status = str(data.get("status") or "").strip().casefold()
    if raw_status in {"pass", "review", "fail"}:
        status = raw_status
    elif isinstance(data.get("faithful"), bool):
        status = "pass" if data["faithful"] else "fail"
    else:
        return None
    raw_issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    issues = [
        {
            "type": issue.get("type") if issue.get("type") in allowed_issue_types else (
                "ambiguous" if status == "review" else "dropped"
            ),
            "condition": str(issue.get("condition") or "").strip(),
            "detail": str(issue.get("detail") or "").strip(),
        }
        for issue in raw_issues
        if isinstance(issue, dict)
    ]
    reason = str(data.get("reason") or "").strip()
    if status == "pass":
        issues = []
    elif status == "review" and not issues:
        issues = [{
            "type": "ambiguous",
            "condition": "복수 해석 가능한 요청",
            "detail": reason or "생성 SQL이 합리적인 해석 중 하나를 충족하지만 의미를 하나로 확정하기 어렵습니다.",
        }]
    elif status == "fail" and not issues:
        issues = [{
            "type": "dropped",
            "condition": "요청한 핵심 의도",
            "detail": reason or "의미 검증기가 SQL과 원문의 불일치를 감지했지만 세부 항목을 반환하지 않았습니다.",
        }]
    return {
        "ran": True,
        "status": status,
        "faithful": status != "fail",
        "reason": reason,
        "issues": issues,
    }


def is_failure(verification: Mapping[str, Any] | None) -> bool:
    """Return true only for a concrete fail, with legacy Boolean fallback."""
    if not isinstance(verification, Mapping) or not verification.get("ran"):
        return False
    status = str(verification.get("status") or "").strip().casefold()
    if status in {"pass", "review", "fail"}:
        return status == "fail"
    return verification.get("faithful") is False


def is_noncredible_inverted_verdict(issue: Mapping[str, Any]) -> bool:
    """Return true when an inverted allegation has no source negation cue."""
    condition = str(issue.get("condition") or "")
    detail = str(issue.get("detail") or "")
    if NEGATION_CUE_RE.search(condition):
        return False
    return bool(condition.strip() or not NEGATION_CUE_RE.search(detail))


def catalog_join_issue_is_covered(
    issue: Mapping[str, Any], join_key_validation: Mapping[str, Any] | None,
) -> bool:
    """Return true when an LLM disputes a join relationship the catalog has already verified.

    Two discharge paths, both requiring the catalog receipt:
      ① 판정문이 verified 관계의 두 컬럼을 이름으로 지목한 경우 — 그 짝만 반증한다.
      ② 판정문이 컬럼명을 대지 않고 조인을 뭉뚱그려 의심한 경우("장바구니-회원 관계의 올바른 조인",
         "잘못된 컬럼 매칭") — 이 SQL 의 등호 조인이 **하나도 빠짐없이** 카탈로그로 증명됐을 때만
         반증한다. 미증명 조인이 하나라도 있으면 그 조인을 가리킨 판정일 수 있으므로 면제하지 않는다.
    ②가 없으면 검증기가 컬럼명을 말해줄 때만 영수증이 작동해, 표현이 추상적일 때 정상 SQL 이 막힌다.
    """
    if not isinstance(join_key_validation, Mapping) or not join_key_validation.get("is_valid"):
        return False
    issue_text = " ".join(
        str(issue.get(key) or "") for key in ("condition", "detail", "expected", "actual")
    ).casefold()
    if not _JOIN_ISSUE_CUE_RE.search(issue_text):
        return False
    verified = [
        relationship
        for relationship in (join_key_validation.get("verified_relationships") or [])
        if isinstance(relationship, Mapping)
    ]
    for relationship in verified:
        columns = [
            str(relationship.get(key) or "").casefold()
            for key in ("left_column", "right_column")
        ]
        if all(columns) and all(re.search(rf"\b{re.escape(column)}\b", issue_text) for column in columns):
            return True
    unverified = [
        relationship
        for relationship in (join_key_validation.get("unverified_relationships") or [])
        if isinstance(relationship, Mapping)
    ]
    return bool(verified) and not unverified and bool(_JOIN_RELATION_CUE_RE.search(issue_text))


def consent_issue_is_covered(
    issue: Mapping[str, Any],
    consent_receipts: Sequence[Mapping[str, Any]] | None,
    *,
    target_terms: Mapping[str, Sequence[str]],
) -> bool:
    """Discharge only the consent field(s) named by the issue and proven by their own receipts."""
    receipts = [receipt for receipt in (consent_receipts or []) if isinstance(receipt, Mapping)]
    if not receipts:
        return False
    issue_text = " ".join(
        str(issue.get(key) or "") for key in ("condition", "detail", "expected", "actual")
    ).casefold()
    if not _CONSENT_ISSUE_CUE_RE.search(issue_text) and not any(
        str(receipt.get("column") or "").casefold() in issue_text for receipt in receipts
    ):
        return False
    matched = []
    for receipt in receipts:
        canonical = str(receipt.get("canonical") or "")
        terms = (
            canonical.casefold(),
            str(receipt.get("column") or "").casefold(),
            *(term.casefold() for term in target_terms.get(canonical, ())),
        )
        if any(term and term in issue_text for term in terms):
            matched.append(receipt)
    relevant = matched or receipts
    return bool(relevant) and all(receipt.get("satisfied") is True for receipt in relevant)


def applied_period_issue_is_covered(
    issue: Mapping[str, Any], applied_period: Mapping[str, Any] | None,
) -> bool:
    """Return true when a '원문에 없는 기간' allegation names the window the caller's policy chose.

    원문이 기간을 말하지 않은 '최근'을 배포 설정이 창 하나로 해석하면, 그 창은 **원문에 없는
    것이 맞다** — 검증기가 spurious 로 부르는 것은 관측으로는 정확하지만 결함이 아니다. 여기서
    반증하지 않으면 정책이 켜진 배포는 정책이 적용될 때마다 자기 SQL 을 막는다. 호출자가 넘기는
    영수증은 이미 결정론이다(``default_period_policy.applied_period_receipt``) — 재구조화 결과가
    그 창을 실제로 들고 있고 원문이 말한 기간을 하나도 잃지 않았을 때만 기록된다.

    좁게 면제한다. (1) 기간을 지목한 판정이어야 하고, (2) 판정문이 **단위가 붙은 창**을 이름으로
    들면 그중 하나가 정책이 고른 창이어야 한다. 원문이 직접 말한 다른 기간을 두고 다투는 판정은
    창이 어긋나 그대로 차단된다. 주(week)를 일수로 부른 표기는 같은 창으로 받는다.
    """
    if not isinstance(applied_period, Mapping):
        return False
    if str(issue.get("type") or "").casefold() not in {"spurious", "wrong_value"}:
        return False
    value, unit = applied_period.get("value"), str(applied_period.get("unit") or "")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or not unit:
        return False

    issue_text = " ".join(
        str(issue.get(key) or "") for key in ("condition", "detail", "expected", "actual")
    )
    evidence_spans = [
        str(span.get("text") or "")
        for span in (applied_period.get("evidence") or [])
        if isinstance(span, Mapping) and str(span.get("text") or "").strip()
    ]
    named = bool(_PERIOD_ISSUE_CUE_RE.search(issue_text)) or any(
        span in issue_text for span in evidence_spans
    )
    if not named:
        return False

    policy_days = _window_in_days(value, unit)
    named_windows = [
        (int(count), _ISSUE_DURATION_UNITS[label.casefold()])
        for count, label in _ISSUE_DURATION_RE.findall(issue_text)
    ]
    return not named_windows or any(
        (count, window_unit) == (value, unit)
        or (policy_days is not None and _window_in_days(count, window_unit) == policy_days)
        for count, window_unit in named_windows
    )


def _recorded_window_scopes(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """플랜의 결정 로그에서 시간 범위 결속 기록만 추린다(없으면 빈 목록).

    결속은 애플리케이션이 내린 **결정**이라 로그가 유일한 진실원천이다 — SQL 이나 원문을 다시
    읽어 추정하면 기록과 다른 답이 나올 수 있고, 그러면 검증기에게 알린 내용과 면제 근거가
    어긋난다.
    """
    if not isinstance(plan, Mapping):
        return []
    return [
        {
            "scope": (entry.get("value") or {}).get("scope"),
            "text": (entry.get("value") or {}).get("text"),
            "window": (entry.get("value") or {}).get("window"),
            "reason": entry.get("reason"),
        }
        for entry in plan_decisions.decisions(dict(plan))
        if isinstance(entry, Mapping)
        and entry.get("filter") == temporal_clause.SCOPE_BINDING_FILTER
    ]


def render_window_scope_context(plan: Mapping[str, Any] | None) -> str:
    """검증기 user content 에 붙일 '기간의 결속 절' 블록(결정이 없으면 빈 문자열).

    이 블록이 없으면 검증기는 원문의 '작년'과 SQL 회원측 무기간을 보고 '기간 누락'으로 판정한다.
    실제로는 애플리케이션이 그 창을 랭킹 모집단에 결속한 것이고, 그 사실은 결정 로그에 있다 —
    판정의 **근거**를 주는 것이지 판정을 막는 것이 아니다. 회원 기간이 명시된 요청에서는 이
    블록이 그 기간을 싣고, SQL 에 없으면 검증기는 그대로 누락을 잡아야 한다.
    """
    listed = _recorded_window_scopes(plan)
    if not listed:
        return ""
    return "\n\n[기간의 결속 절]\n" + json.dumps(listed, ensure_ascii=False, indent=2) + (
        "\n랭킹(모집단) 기간은 '그 상품들이 언제 많이 팔렸나'이고 회원 행동 기간은 "
        "'이 회원이 언제 샀나'다. window 가 null 인 절에는 SQL 에 날짜 필터가 없는 것이 "
        "정상이며 누락(dropped)이 아니다."
    )


def window_scope_issue_is_covered(
    issue: Mapping[str, Any],
    plan: Mapping[str, Any] | None,
    sql: str,
) -> bool:
    """'기간 누락' 판정이 호출자가 기록한 **결속 결정**과 모순인가.

    좁게 면제한다. ① 랭킹 절에 결속된 창이 실제로 SQL 에 있고, ② 회원 절에 결속된 창이 있다면
    그것도 SQL 에 있고, ③ 판정문이 그 랭킹 창을 이름으로 들 때만이다. 회원 기간이 정말 빠진
    SQL 은 ②에서 걸려 그대로 차단된다 — 이 반증은 '자리를 옳게 골랐다'만 말한다.
    """
    if str(issue.get("type") or "").casefold() not in {"dropped", "wrong_value"}:
        return False
    scopes = {
        str(item.get("scope")): item for item in _recorded_window_scopes(plan)
    }
    ranking = (scopes.get(temporal_clause.SCOPE_RANKING) or {}).get("window")
    membership = (scopes.get(temporal_clause.SCOPE_MEMBERSHIP) or {}).get("window")
    if not isinstance(ranking, Mapping):
        return False
    compact_sql = re.sub(r"\s+", "", str(sql))
    for window in (ranking, membership):
        if isinstance(window, Mapping) and not all(
            str(window.get(bound) or "") in compact_sql for bound in ("from", "to")
        ):
            return False
    issue_text = re.sub(
        r"\s+",
        "",
        " ".join(str(issue.get(key) or "") for key in ("condition", "detail", "expected", "actual")),
    )
    # 판정문은 **원문 어휘**로 쓰인다('작년'). 해석된 라벨('2025년')만 대조하면 같은 창을 두고
    # 다투는 판정을 알아보지 못한다 — 표면어·라벨·경계값을 모두 받는다.
    names = [
        re.sub(r"\s+", "", str(name or ""))
        for name in (
            (scopes.get(temporal_clause.SCOPE_RANKING) or {}).get("text"),
            ranking.get("label"),
            ranking.get("from"),
            ranking.get("to"),
        )
    ]
    return any(name and name in issue_text for name in names)


def reconcile_verification(
    verification: Mapping[str, Any], delivery_validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Downgrade an LLM fail only when every reported issue has deterministic counter-evidence."""
    issues = delivery_validation.get("semantic_issues")
    if (
        not is_failure(verification)
        or not isinstance(issues, list)
        or not issues
        or any(not isinstance(issue, dict) or not issue.get("exempt_reason") for issue in issues)
    ):
        return {**verification, **({"issues": issues} if isinstance(issues, list) else {})}
    return {
        **verification,
        "original_status": verification.get("status") or "fail",
        "status": "review",
        "faithful": True,
        "deterministic_override": True,
        "issues": issues,
    }


__all__ = [
    "NEGATION_CUE_RE",
    "applied_period_issue_is_covered",
    "catalog_join_issue_is_covered",
    "consent_coverage_receipts",
    "consent_issue_is_covered",
    "is_failure",
    "is_noncredible_inverted_verdict",
    "normalize_verdict",
    "reconcile_verification",
]
