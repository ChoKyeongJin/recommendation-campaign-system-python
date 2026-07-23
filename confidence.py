"""타겟팅 SQL 신뢰도 스코어러(결정론).

생성된 타겟팅 SQL에 대해 0~100 신뢰도와 조건별 근거를 '규칙 기반'으로 산정한다. LLM 이 임의로
점수를 정하지 않고, 아래 5개 축의 실제 근거 신호로만 계산한다.

  1) 요청↔SQL 조건 일치도   : query_plan 조건이 SQL 에 실제 반영됐는지(coverage) + 미언급 조건 추가 여부
  2) 스키마 일치            : 조건이 참조하는 테이블/컬럼이 schema_catalog 에 실재하는지 + sql_guard 테이블 허용
  3) 정책/기존 SQL 유사도   : 정규화 사전(normalization_rules) 매칭 + 검색된 policy/sql_example 노드 근거
  4) 날짜/필터 명확성        : 코드값·날짜창 등 결정론 확정 vs free-text LIKE/LLM 추론
  5) 정적 검증 결과         : sql_guard 통과 여부와 경고

각 WHERE 조건별로도 점수·근거·경고를 낸다. 근거에는 문서명/문서 ID/스키마 정의 출처를 표기하고,
문서·스키마에서 '직접 확인(confirmed)'한 것과 'AI 추론(inferred)'을 구분한다. 확인 불가 조건은
점수를 낮추고 경고로 표시한다.

graph_rag 가 이 모듈을 import 한다(단방향). 순환 방지를 위해 여기서는 graph_rag 를 import 하지 않고,
plain dict/Path 만 입력으로 받는다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA_PATH = Path("docs/data/schema_catalog.json")
DEFAULT_MEMBER_FILTERS_PATH = Path("docs/data/member_target_filters.json")
DEFAULT_NORMALIZATION_DOC = "normalization_rules.sample.json"

# 5개 축 가중치(합 1.0). 조정 가능 — 근거 신호 자체는 결정론이고 이 가중치만 정책값이다.
DIMENSION_WEIGHTS = {
    "request_sql_match": 0.25,
    "schema_match": 0.25,
    "static_validation": 0.20,
    "clarity": 0.15,
    "policy_similarity": 0.15,
}

LEVEL_THRESHOLDS = [(85, "높음"), (65, "보통"), (0, "낮음")]

GENDER_KO = {"female": "여성", "male": "남성"}
GRADE_KO = {
    "welcome_grade": "웰컴 등급", "family_grade": "패밀리 등급", "silver_grade": "실버 등급",
    "gold_grade": "골드 등급", "vip": "VIP 등급",
}
BEHAVIOR_KO = {
    "no_purchase": "구매 이력 없음(무구매)", "first_purchase": "첫 구매",
    "repeat_buyer": "재구매(2회 이상)", "cart_abandoner": "장바구니 이탈",
}


@lru_cache(maxsize=4)
def _schema_columns(schema_path_text: str) -> dict[str, set[str]]:
    path = Path(schema_path_text)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, set[str]] = {}
    for name, meta in payload.get("tables", {}).items():
        if isinstance(meta, dict):
            result[name] = {c.get("name") for c in meta.get("columns", []) if isinstance(c, dict) and c.get("name")}
    return result


@lru_cache(maxsize=4)
def _member_filters(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _level(score: int) -> str:
    for threshold, label in LEVEL_THRESHOLDS:
        if score >= threshold:
            return label
    return "낮음"


def _ev(source_type: str, ref: str, detail: str, kind: str) -> dict[str, str]:
    """근거 한 건. kind: confirmed(문서/스키마 직접 확인) | inferred(AI 추론)."""
    return {"source_type": source_type, "ref": ref, "detail": detail, "kind": kind}


def _matched_term_for(query_plan: dict[str, Any], canonical: str) -> dict[str, Any] | None:
    for match in query_plan.get("matched_terms", []):
        if match.get("canonical") == canonical:
            return match
    return None


def _column_in_schema(schema_cols: dict[str, set[str]], table: str, column: str) -> bool:
    return column in schema_cols.get(table, set())


# --------------------------------------------------------------------------- #
# 조건 추출: query_plan → 의미 조건 목록(근거·점수는 아래 _score_condition 에서 부여)
# --------------------------------------------------------------------------- #
def _extract_conditions(query_plan: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    tu = query_plan.get("target_user", {})
    exclude = query_plan.get("exclude", {})
    conditions: list[dict[str, Any]] = []

    def add(**kwargs: Any) -> None:
        conditions.append(kwargs)

    gender = tu.get("gender")
    if gender:
        add(key="gender", value=gender, ko=f"성별: {GENDER_KO.get(gender, gender)}", kind="eq_filter", category="gender")

    if isinstance(tu.get("age_min"), int):
        add(key="age_min", value=tu["age_min"], ko=f"{tu['age_min']}세 이상", kind="age", category="age")
    if isinstance(tu.get("age_max"), int):
        add(key="age_max", value=tu["age_max"], ko=f"{tu['age_max']}세 이하", kind="age", category="age")

    for lifecycle in tu.get("lifecycle", []):
        if lifecycle == "new_user":
            continue  # signup_target 로 처리
        if lifecycle in GRADE_KO:
            add(key=lifecycle, value=lifecycle, ko=f"회원등급: {GRADE_KO[lifecycle]}", kind="eq_filter", category="grade")
        elif lifecycle == "dormant":
            add(key="dormant", value="dormant", ko="휴면 회원", kind="eq_filter", category="state")
        elif lifecycle.startswith("inactive_"):
            add(key=lifecycle, value=lifecycle, ko=f"{lifecycle.replace('inactive_', '').replace('d', '')}일 이상 미접속",
                kind="activity", category="activity")
        else:
            add(key=lifecycle, value=lifecycle, ko=f"생애주기: {lifecycle}", kind="unknown", category="lifecycle")

    signup = tu.get("signup_target")
    if isinstance(signup, dict) or "new_user" in (tu.get("lifecycle") or []):
        days = signup.get("days") if isinstance(signup, dict) else None
        add(key="signup_target", value=days, ko=f"최근 {days or 90}일 이내 가입", kind="signup", category="date")

    birthday = tu.get("birthday_target")
    if isinstance(birthday, dict):
        gran = "이달" if birthday.get("granularity") == "month" else "오늘"
        add(key="birthday_target", value=gran, ko=f"생일 {gran}", kind="birthday", category="date")

    inactivity = tu.get("purchase_inactivity")
    if isinstance(inactivity, dict) and isinstance(inactivity.get("min_days"), int):
        add(key="purchase_inactivity", value=inactivity["min_days"], ko=f"최근 {inactivity['min_days']}일 미구매",
            kind="order_window", category="date")

    for behavior in tu.get("behaviors", []):
        if behavior in ("no_purchase", "first_purchase", "repeat_buyer"):
            add(key=behavior, value=behavior, ko=BEHAVIOR_KO.get(behavior, behavior), kind="order_count", category="behavior")
        elif behavior == "cart_abandoner":
            add(key=behavior, value=behavior, ko=BEHAVIOR_KO[behavior], kind="cart", category="behavior")

    purchase_object = tu.get("purchase_object")
    if purchase_object:
        add(key="purchase_object", value=purchase_object, ko=f"상품 구매 이력: '{purchase_object}'",
            kind="free_text", category="purchase")

    purchase_date = tu.get("purchase_date")
    if isinstance(purchase_date, dict) and purchase_date.get("from") and purchase_date.get("to"):
        add(key="purchase_date", value=f"{purchase_date['from']}~{purchase_date['to']}",
            ko=purchase_date.get("label") or f"구매 날짜 {purchase_date['from']}~{purchase_date['to']}",
            kind="order_window", category="date")

    for dimension_filter in query_plan.get("dimension_filters", []):
        names = dimension_filter.get("names") or dimension_filter.get("codes") or []
        column = (dimension_filter.get("column") or "").split(".")[-1]
        add(key=f"dimension:{column}", value=", ".join(map(str, names)), ko=f"{column}: {', '.join(map(str, names))}",
            kind="dimension", category="dimension", dimension_filter=dimension_filter)

    for field in ("gender", "lifecycle", "interests"):
        for value in exclude.get(field, []):
            add(key=f"exclude.{field}:{value}", value=value, ko=f"제외 - {field}: {value}", kind="exclude", category="exclude")

    # 주입된 기본 게이트(정상 회원) — 사용자가 요청하지 않았는데 정책상 붙는 조건.
    probe = candidate.get("cardinality_probe") or {}
    if any(p.get("injected_default") for p in probe.get("predicates", [])):
        add(key="active_state_gate", value="normal", ko="정상 회원(기본 정책 주입)", kind="injected_state", category="state")

    return conditions


# --------------------------------------------------------------------------- #
# 조건별 채점
# --------------------------------------------------------------------------- #
def _score_condition(
    cond: dict[str, Any], query_plan: dict[str, Any], candidate: dict[str, Any],
    schema_cols: dict[str, set[str]], filters: dict[str, Any], sql_lower: str,
) -> dict[str, Any]:
    evidence: list[dict[str, str]] = []
    warnings: list[str] = []
    kind = cond["kind"]
    base_table = "CRM_MB_BASEINFO"

    # --- 값/스키마 출처 + 명확성(kind 별) ---
    schema_ok = True
    value_confirmed = True
    clarity = 100

    if kind == "eq_filter":
        entry = next((f for f in filters.get("eq_filters", []) if f.get("canonical") == cond["value"]), None)
        if entry:
            column = entry["column"].split(".")[-1]
            evidence.append(_ev("filter_registry", f"member_target_filters.json: eq_filters[{cond['value']}]",
                                 f"{entry['column']} = {entry['value']} (코드값 확정)", "confirmed"))
            schema_ok = _column_in_schema(schema_cols, base_table, column)
            if schema_ok:
                evidence.append(_ev("schema", f"schema_catalog.json: {base_table}.{column}", "컬럼 실재 확인", "confirmed"))
        else:
            value_confirmed = False
    elif kind == "activity":
        entry = next((f for f in filters.get("activity_filters", []) if f.get("canonical") == cond["value"]), None)
        col_ok = _column_in_schema(schema_cols, base_table, "LAST_LOGIN_DATE")
        if entry:
            evidence.append(_ev("filter_registry", f"member_target_filters.json: activity_filters[{cond['value']}]",
                                 f"LAST_LOGIN_DATE <= -{entry.get('days')}일", "confirmed"))
        if col_ok:
            evidence.append(_ev("schema", f"schema_catalog.json: {base_table}.LAST_LOGIN_DATE", "컬럼 실재 확인", "confirmed"))
        schema_ok = col_ok
    elif kind == "signup":
        cfg = filters.get("signup_target", {})
        column = cfg.get("column", "REG_DT")
        schema_ok = _column_in_schema(schema_cols, base_table, column)
        evidence.append(_ev("filter_registry", "member_target_filters.json: signup_target",
                             f"{column} 최근 {cond['value'] or cfg.get('default_days', 90)}일 창 (anchor={cfg.get('anchor')})", "confirmed"))
        if schema_ok:
            evidence.append(_ev("schema", f"schema_catalog.json: {base_table}.{column}", "가입일 컬럼 실재 확인", "confirmed"))
        if cond["value"] is None:
            warnings.append("가입 기간(일수)이 프롬프트에 명시되지 않아 기본값 90일을 적용했습니다.")
            clarity = 70
    elif kind == "birthday":
        column = filters.get("birthday_target", {}).get("column", "BIRTHDAY")
        schema_ok = _column_in_schema(schema_cols, base_table, column)
        evidence.append(_ev("filter_registry", "member_target_filters.json: birthday_target",
                             f"{column} 월일(MMDD) 비교", "confirmed"))
        if schema_ok:
            evidence.append(_ev("schema", f"schema_catalog.json: {base_table}.{column}", "생일 컬럼 실재 확인", "confirmed"))
    elif kind in ("order_count", "order_window"):
        cfg = filters.get("order_count_targets", {})
        table = cfg.get("table", "CRM_SL_ORDERHEADERMALL")
        schema_ok = table in schema_cols
        rule = cfg.get("behaviors", {}).get(cond["value"]) if kind == "order_count" else {"anti_join": True}
        evidence.append(_ev("filter_registry", "member_target_filters.json: order_count_targets",
                             f"{table} 회원별 주문 집계 ({rule})", "confirmed"))
        if schema_ok:
            evidence.append(_ev("schema", f"schema_catalog.json: {table}", "주문 테이블 실재 확인", "confirmed"))
    elif kind == "free_text":
        cols = filters.get("purchase_product_match_columns", [])
        evidence.append(_ev("filter_registry", "member_target_filters.json: purchase_product_match_columns",
                             f"상품 {len(cols)}개 컬럼 LIKE N'%{cond['value']}%'", "confirmed"))
        evidence.append(_ev("inference", "AI 추론", f"'{cond['value']}' 는 자유 텍스트 부분일치라 상품 매핑이 확정 코드가 아닙니다", "inferred"))
        value_confirmed = False
        clarity = 55
        warnings.append(f"상품 조건 '{cond['value']}' 은 코드가 아닌 텍스트 LIKE 매칭이라 오탐/누락 가능성이 있습니다.")
    elif kind == "dimension":
        df = cond.get("dimension_filter", {})
        src = df.get("source") or "dimension_catalog"
        column = (df.get("column") or "").split(".")[-1]
        table = df.get("table", base_table)
        schema_ok = _column_in_schema(schema_cols, table, column) if column else True
        evidence.append(_ev("dimension_catalog", f"{src}: {table}.{column}",
                             f"이름→코드 해석 {df.get('codes')}", "confirmed"))
        if schema_ok and column:
            evidence.append(_ev("schema", f"schema_catalog.json: {table}.{column}", "컬럼 실재 확인", "confirmed"))
    elif kind == "age":
        schema_ok = _column_in_schema(schema_cols, base_table, "AGE")
        evidence.append(_ev("request_literal", "요청 문구", f"연령 값 {cond['value']} 을 프롬프트에서 직접 추출", "confirmed"))
        if schema_ok:
            evidence.append(_ev("schema", f"schema_catalog.json: {base_table}.AGE", "연령 컬럼 실재 확인", "confirmed"))
    elif kind == "injected_state":
        cfg = filters.get("active_state", {})
        column = cfg.get("column", "MEMBER_STATE_CD")
        schema_ok = _column_in_schema(schema_cols, base_table, column)
        evidence.append(_ev("filter_registry", "member_target_filters.json: active_state",
                             f"{column} = {cfg.get('value')} (탈퇴/휴면 제외 기본 정책)", "confirmed"))
        warnings.append("이 조건은 사용자 요청에 없고 발송 대상 기본 정책으로 자동 추가된 것입니다.")
    elif kind == "exclude":
        evidence.append(_ev("filter_registry", "member_target_filters.json", f"제외 조건('{cond['value']}') 부정 술어", "confirmed"))
    else:  # unknown lifecycle 등 확정 불가
        value_confirmed = False
        schema_ok = False
        clarity = 40
        warnings.append(f"'{cond['value']}' 은 레지스트리/스키마에서 확인되지 않아 신뢰할 수 없습니다.")
        evidence.append(_ev("inference", "AI 추론", "매핑 근거를 찾지 못했습니다", "inferred"))

    if not schema_ok:
        warnings.append("참조 컬럼/테이블을 schema_catalog 에서 확인하지 못했습니다.")

    # --- 요청 매칭(정규화 문서/요청 문구) ---
    request_score = 60
    if kind == "injected_state":
        request_score = 20  # 요청에 없음
    else:
        match = _matched_term_for(query_plan, cond["value"] if isinstance(cond["value"], str) else "")
        if match:
            evidence.append(_ev("normalization_doc", f"{DEFAULT_NORMALIZATION_DOC}: rule_id={match.get('rule_id')}",
                                 f"요청 문구 '{match.get('matched_text')}' → {match.get('canonical')}", "confirmed"))
            request_score = 100
        elif cond["category"] in ("age", "date"):
            request_score = 90  # 요청 문구에서 직접 추출(숫자/기간)
        elif cond["category"] == "dimension":
            request_score = 85

    # --- SQL 반영 여부(정적 확인) ---
    in_sql = False
    probe = candidate.get("cardinality_probe") or {}
    check_tokens = []
    if kind == "eq_filter":
        entry = next((f for f in filters.get("eq_filters", []) if f.get("canonical") == cond["value"]), None)
        if entry:
            check_tokens = [entry["value"].casefold()]
    elif kind == "signup":
        check_tokens = [filters.get("signup_target", {}).get("column", "reg_dt").casefold()]
    elif kind == "birthday":
        check_tokens = [filters.get("birthday_target", {}).get("column", "birthday").casefold()]
    elif kind in ("order_count", "order_window"):
        check_tokens = [filters.get("order_count_targets", {}).get("table", "crm_sl_orderheadermall").casefold()]
    elif kind == "free_text" and isinstance(cond["value"], str):
        check_tokens = [cond["value"].casefold()]
    elif kind == "injected_state":
        check_tokens = [filters.get("active_state", {}).get("value", "normal").casefold()]
    elif kind == "age":
        check_tokens = [str(cond["value"]).casefold()]
    if check_tokens:
        in_sql = all(token in sql_lower for token in check_tokens)
    else:
        in_sql = True  # 토큰 규칙이 없는 조건은 반영 여부 판정 보류(중립)
    if not in_sql:
        warnings.append("이 조건이 생성 SQL 에 반영되지 않았습니다(부분추출/누락 가능).")

    # --- 조건 점수 합성 ---
    schema_score = 100 if schema_ok else 35
    value_score = 100 if value_confirmed else 55
    static_score = 100 if in_sql else 40
    score = round(
        0.28 * request_score + 0.24 * schema_score + 0.20 * value_score + 0.14 * clarity + 0.14 * static_score
    )
    verified = value_confirmed and schema_ok and any(e["kind"] == "confirmed" for e in evidence) and kind not in ("free_text", "unknown")

    return {
        "key": cond["key"],
        "ko_label": cond["ko"],
        "score": score,
        "verified": verified,
        "evidence": evidence,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 전체 채점
# --------------------------------------------------------------------------- #
def score_targeting_confidence(
    query_plan: dict[str, Any],
    candidate: dict[str, Any],
    context_nodes: list[dict[str, Any]] | None = None,
    *,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    member_filters_path: Path = DEFAULT_MEMBER_FILTERS_PATH,
) -> dict[str, Any]:
    """선택된 candidate(sql, validation, coverage, unmentioned_conditions, cardinality_probe 포함)에 대해
    전체·조건별 신뢰도와 근거를 낸다."""
    schema_cols = _schema_columns(str(schema_path))
    filters = _member_filters(str(member_filters_path))
    sql = candidate.get("sql") or ""
    sql_lower = sql.casefold()
    context_nodes = context_nodes or []

    conditions = _extract_conditions(query_plan, candidate)
    scored = [_score_condition(c, query_plan, candidate, schema_cols, filters, sql_lower) for c in conditions]

    # ---- 5개 축 ----
    coverage = candidate.get("coverage", {})
    validation = candidate.get("validation", {})
    unmentioned = candidate.get("unmentioned_conditions", {})

    required = coverage.get("required_count", 0)
    matched = coverage.get("matched_count", 0)
    request_sql_match = 100 if required == 0 else round(100 * matched / required)
    if not unmentioned.get("is_satisfied", True):
        request_sql_match = min(request_sql_match, 60)

    if scored:
        schema_match = round(sum(100 if any(e["source_type"] == "schema" for e in s["evidence"]) else 40 for s in scored) / len(scored))
    else:
        schema_match = 100
    if validation.get("tables") and not validation.get("is_valid", True):
        schema_match = min(schema_match, 50)

    doc_matches = len(query_plan.get("matched_terms", []))
    policy_nodes = sum(1 for n in context_nodes if isinstance(n, dict) and n.get("node_type") in ("business_policy", "sql_example"))
    policy_similarity = min(100, 40 + 20 * doc_matches + 15 * policy_nodes)

    if scored:
        clarity = round(sum(s["score"] for s in scored) / len(scored))  # 조건 점수 평균을 명확성 대리로
    else:
        clarity = 80

    issues = validation.get("issues", [])
    has_error = any(i.get("severity") == "error" for i in issues)
    warn_count = sum(1 for i in issues if i.get("severity") == "warning")
    static_validation = 0 if has_error else max(60, 100 - 8 * warn_count)

    dimensions = {
        "request_sql_match": request_sql_match,
        "schema_match": schema_match,
        "policy_similarity": policy_similarity,
        "clarity": clarity,
        "static_validation": static_validation,
    }
    overall = round(sum(dimensions[k] * w for k, w in DIMENSION_WEIGHTS.items()))

    warnings: list[str] = []
    if candidate.get("source") == "llm_generated":
        warnings.append("이 SQL 은 결정론 템플릿이 아니라 LLM 이 생성한 것이라 신뢰도를 보수적으로 낮췄습니다.")
        overall = min(overall, 75)
    for dropped in candidate.get("dropped_conditions", []) or []:
        warnings.append(f"실DB 미지원으로 제외된 조건이 있습니다: {dropped}")
    for s in scored:
        warnings.extend(s["warnings"])

    return {
        "overall_score": overall,
        "level": _level(overall),
        "dimensions": dimensions,
        "dimension_weights": DIMENSION_WEIGHTS,
        "conditions": scored,
        "warnings": _dedupe(warnings),
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 렌더링(사용자 예시 형식)
# --------------------------------------------------------------------------- #
def render_confidence_report(confidence: dict[str, Any]) -> str:
    lines = [
        f"전체 신뢰도: {confidence['overall_score']}점",
        f"신뢰도 수준: {confidence['level']}",
        "",
        "축별 점수:",
    ]
    axis_ko = {
        "request_sql_match": "요청↔SQL 일치도", "schema_match": "스키마 일치",
        "policy_similarity": "정책/기존SQL 유사도", "clarity": "조건 명확성", "static_validation": "정적 검증",
    }
    for key, value in confidence["dimensions"].items():
        lines.append(f"* {axis_ko.get(key, key)}: {value}점")
    lines.append("")
    lines.append("조건별 근거:")
    for cond in confidence["conditions"]:
        tag = "✓확인" if cond["verified"] else "⚠추론"
        lines.append("")
        lines.append(f"* {cond['ko_label']}: {cond['score']}점 [{tag}]")
        for ev in cond["evidence"]:
            mark = "확인" if ev["kind"] == "confirmed" else "추론"
            lines.append(f"  * 근거({mark}): {ev['ref']} — {ev['detail']}")
        for warn in cond["warnings"]:
            lines.append(f"  * ⚠ 경고: {warn}")
    if confidence["warnings"]:
        lines.append("")
        lines.append("전체 경고:")
        for warn in confidence["warnings"]:
            lines.append(f"* ⚠ {warn}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 렌더링(프론트 노출용 마크다운, GFM)
# --------------------------------------------------------------------------- #
AXIS_KO = {
    "request_sql_match": "요청↔SQL 일치도", "schema_match": "스키마 일치",
    "policy_similarity": "정책·기존 SQL 유사도", "clarity": "조건 명확성", "static_validation": "정적 검증",
}
_LEVEL_EMOJI = {"높음": "🟢", "보통": "🟡", "낮음": "🔴"}
_SOURCE_EMOJI = {
    "schema": "🗂️", "filter_registry": "📄", "normalization_doc": "📘",
    "request_literal": "💬", "dimension_catalog": "🗂️", "inference": "🤖", "request_literal_doc": "💬",
}


def _md_escape(text: str) -> str:
    """마크다운 표/인라인에서 깨질 수 있는 문자를 최소 이스케이프한다."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_confidence_markdown(confidence: dict[str, Any]) -> str:
    """프론트 화면에 그대로 붙일 수 있는 GitHub-flavored Markdown 을 생성한다.

    전체 신뢰도 배지 + 축별 점수 표 + 조건별 점수/근거(문서·스키마 출처, 확인/추론 구분) +
    경고를 담는다. LLM 없이 결정론 산정 결과(score_targeting_confidence)만으로 렌더한다.
    """
    level = confidence.get("level", "")
    badge = _LEVEL_EMOJI.get(level, "⚪")
    lines: list[str] = [
        f"### {badge} 타겟팅 신뢰도 {confidence['overall_score']}점 · {level}",
        "",
        "| 평가 축 | 점수 |",
        "| :-- | --: |",
    ]
    for key, value in confidence["dimensions"].items():
        lines.append(f"| {AXIS_KO.get(key, key)} | {value} |")

    lines.append("")
    lines.append("**조건별 신뢰도·근거**")
    if not confidence["conditions"]:
        lines.append("")
        lines.append("- (평가할 조건이 없습니다)")
    for cond in confidence["conditions"]:
        verified_badge = "✅ 확인" if cond["verified"] else "🟠 추론"
        lines.append("")
        lines.append(f"- **{_md_escape(cond['ko_label'])}** — {cond['score']}점 · {verified_badge}")
        for ev in cond["evidence"]:
            emoji = _SOURCE_EMOJI.get(ev["source_type"], "📄")
            kind_ko = "확인" if ev["kind"] == "confirmed" else "추론"
            lines.append(f"  - {emoji} {kind_ko} · `{_md_escape(ev['ref'])}` — {_md_escape(ev['detail'])}")
        for warn in cond["warnings"]:
            lines.append(f"  - ⚠️ **경고**: {_md_escape(warn)}")

    if confidence["warnings"]:
        lines.append("")
        lines.append("**⚠️ 전체 경고**")
        lines.append("")
        for warn in confidence["warnings"]:
            lines.append(f"- {_md_escape(warn)}")

    return "\n".join(lines)
