"""의미 검증 v2 오케스트레이터(§전체 흐름) — 구조화 요구 → AST 의미 → 규칙 매핑 → (제한적)LLM → 판정.

단계(§핵심 요구사항):
  1) build_target_specification: query_plan(구조화 슬롯)에서 TargetSpecification 생성. NL 재파싱이 아니라
     이미 파서가 확정한 슬롯을 요구사항으로 승격한다(각 요구에 고유 id 부여).
  2) extract_sql_semantics: sqlglot AST 로 SqlSemantics 추출(파싱 실패는 기술 오류 → review).
  3) map_requirements: 규칙 기반 매핑/동치 판정.
  4) refine_checks: 규칙 미결 항목만 LLM 근거 탐색(주입식, 없으면 no-op).
  5) decide: pass/review/fail(구체 근거 있는 fail 만).
  6) (선택) 실행 기반 assertion 을 execution_assertions 로 합류.

shadow 모드(§12): run_shadow_validation 은 신규 검증을 돌려 결과를 dict 로 돌리되, 예외를 삼키지 않고
review 로 안전 폴백한다 — 호출자(build_sql_result)가 사용자 응답은 기존 게이트로 유지하고 차이만 로깅한다.

이 모듈만 graph_rag 로부터 호출된다. graph_rag 를 import 하지 않는다(순수 in/out; LLM client 는 주입).
"""

from __future__ import annotations

from typing import Any, Callable

from sql_semantics import SqlParseError, SqlSemantics, extract_sql_semantics
from semantic_mapping import evaluate, map_requirements, detect_extra_restrictions, decide
from target_spec import (
    Exclusion,
    ExecutionAssertion,
    PolicyViolation,
    Requirement,
    RetrievedEvidence,
    TargetSpecification,
    ValidationResult,
)
import llm_evidence


# ------------------------------------------------------------------ 1) 구조화 요구 생성

def build_target_specification(
    query_plan: dict[str, Any],
    *,
    dedup_key: str | None = None,
    retrieved_evidence: list[dict[str, Any]] | None = None,
    field_columns: dict[str, str] | None = None,
    membership_tables: dict[str, str] | None = None,
    code_filter_values: set[str] | frozenset[str] | None = None,
    lifecycle_columns: dict[str, str] | None = None,
) -> TargetSpecification:
    """query_plan 의 확정 슬롯을 TargetSpecification 으로 승격한다. 각 요구에 고유 id(R1, R2, ...) 부여.

    NL 을 다시 파싱하지 않는다 — 파서가 이미 확정한 target_user/exclude 슬롯이 단일 진실 소스다.
    field_columns: 논리 필드(gender/lifecycle 등)→실제 SQL 컬럼 매핑. 범주형 값이 코드로 확장되는
    (여성→GENDER_CD IN(...)) 필드를 SQL 컬럼과 정렬해 '누락' 오판을 막는다(호출자가 레지스트리에서 공급).
    membership_tables: 논리 팩트(orders/cart/campaign)→실제 테이블명. EXISTS/JOIN 매칭을 실제 테이블로 좁힌다.
    code_filter_values: lifecycle 값 중 실제 '코드 컬럼 등호 필터'로 컴파일되는 값 집합. 여기 없는 값
    (예: 'dormant'→날짜창, 'sms_optin'→Y/N)은 컬럼 필터가 아니므로 요구로 만들지 않는다(false-missing 방지)."""
    field_columns = field_columns or {}
    membership_tables = membership_tables or {}
    code_filter_values = code_filter_values or frozenset()
    lifecycle_columns = lifecycle_columns or {}
    tu = query_plan.get("target_user", {}) if isinstance(query_plan.get("target_user"), dict) else {}
    exclude = query_plan.get("exclude", {}) if isinstance(query_plan.get("exclude"), dict) else {}

    reqs: list[Requirement] = []
    excs: list[Exclusion] = []
    ambiguous: list[str] = []
    counter = _IdGen("R")
    exc_counter = _IdGen("E")

    # 성별. 범주형: 자연어 값(female)이 코드(GENDER_CD IN ...)로 확장되므로 컬럼 존재+극성만 본다.
    # 컬럼은 field_columns['gender'] → 값 접두어(GENDER_CD.FEMALE) → 'gender' 순으로 정한다.
    if tu.get("gender"):
        col = field_columns.get("gender") or _code_field(tu["gender"], "gender")
        reqs.append(Requirement(counter.next(), "filter", col, "=", tu["gender"],
                                categorical=True, source_span="성별"))

    # 라이프사이클/회원상태(정상 회원·등급 등): 실제 코드 컬럼 등호로 컴파일되는 값만 범주형 요구로.
    # 'dormant'(→날짜창)·'sms_optin'(→Y/N) 처럼 컬럼 등호가 아닌 값은 다른 슬롯/생성기가 표현하므로 제외.
    for value in tu.get("lifecycle", []) or []:
        if not isinstance(value, str) or not value:
            continue
        if code_filter_values and value not in code_filter_values:
            continue
        # 값마다 실제 컬럼이 다르다(등급=EMART_GRADE_CD, SMS동의=SMS_YN, 회원상태=MEMBER_STATE_CD).
        col = lifecycle_columns.get(value) or field_columns.get("lifecycle") or _code_field(value, "lifecycle")
        reqs.append(Requirement(counter.next(), "filter", col, "=", value,
                                categorical=True, source_span="회원상태/등급"))
    # 연령.
    if tu.get("age_min") is not None:
        reqs.append(Requirement(counter.next(), "filter", "age", ">=", tu["age_min"], source_span="연령 하한"))
    if tu.get("age_max") is not None:
        reqs.append(Requirement(counter.next(), "filter", "age", "<=", tu["age_max"], source_span="연령 상한"))

    # 잔액/수치 임계(balance_conditions): 컬럼 직접 비교.
    for cond in tu.get("balance_conditions", []) or []:
        if not isinstance(cond, dict):
            continue
        col = cond.get("column")
        if not col:
            continue
        null_mode = cond.get("null_mode")
        if null_mode == "is_null":
            reqs.append(Requirement(counter.next(), "filter", col, "is_null", None,
                                    source_span=str(cond.get("label") or col)))
        elif null_mode == "null_or_zero":
            reqs.append(Requirement(counter.next(), "filter", col, "=", 0,
                                    source_span=str(cond.get("label") or col)))
        elif cond.get("operator") in ("=", ">", ">=", "<", "<="):
            reqs.append(Requirement(counter.next(), "filter", col, cond["operator"],
                                    cond.get("threshold"), source_span=str(cond.get("label") or col)))

    # 집계 조건(aggregate_conditions): 구매 N회/금액 등.
    for cond in tu.get("aggregate_conditions", []) or []:
        if not isinstance(cond, dict):
            continue
        metric = cond.get("metric_id")
        func = _metric_aggregate_func(metric)
        reqs.append(Requirement(
            counter.next(), "aggregate", metric or "aggregate", cond.get("operator"),
            cond.get("threshold"), metric=metric, aggregate_func=func,
            window_days=cond.get("window_days"), source_span=str(cond.get("label") or metric)))

    login_col = field_columns.get("last_login", "last_login")
    # 최근 로그인/접속(긍정 날짜 창).
    rl = tu.get("recent_login")
    if isinstance(rl, dict) and rl.get("min_days"):
        reqs.append(Requirement(counter.next(), "date_window", login_col, ">=", None,
                                window_days=rl.get("min_days"), source_span="최근 로그인"))
    # 미접속(휴면; 부정 날짜 창).
    inact = tu.get("inactivity_period")
    if isinstance(inact, dict) and inact.get("min_days"):
        reqs.append(Requirement(counter.next(), "date_window", login_col, "<=", None,
                                window_days=inact.get("min_days"), negated=True, source_span="장기 미접속"))
    orders_table = membership_tables.get("orders", "orders")
    cart_table = membership_tables.get("cart", "cart")
    campaign_table = membership_tables.get("campaign", "campaign")

    # 단순 구매 존재("구매한 회원")와 선택적 최근 창. 과거에는 상품/건수 없는 순수 구매 존재가
    # 스펙에 없어 회원 테이블만 조회해도 의미 검증 대상 자체가 생기지 않았다.
    purchase_membership = tu.get("purchase_membership")
    if isinstance(purchase_membership, dict) and purchase_membership.get("operator") == "exists":
        reqs.append(Requirement(
            counter.next(), "membership", orders_table, "exists", orders_table,
            window_days=purchase_membership.get("window_days"), source_span="구매 이력 존재",
        ))

    # 최근 N일 미구매(부재 창).
    pi = tu.get("purchase_inactivity")
    if isinstance(pi, dict) and pi.get("min_days"):
        reqs.append(Requirement(counter.next(), "not_membership", orders_table, "not_exists",
                                orders_table, window_days=pi.get("min_days"), negated=True,
                                source_span="최근 미구매"))

    # 행동(behaviors): 구매/장바구니/무구매 등 존재·부재.
    for behavior in tu.get("behaviors", []) or []:
        if behavior == "no_purchase":
            reqs.append(Requirement(counter.next(), "not_membership", orders_table, "not_exists",
                                    orders_table, negated=True, source_span="무구매"))
        elif behavior == "cart_abandoner":
            reqs.append(Requirement(counter.next(), "membership", cart_table, "exists", cart_table,
                                    source_span="장바구니 보유/이탈"))

    # 캠페인 반응(EXISTS/NOT EXISTS).
    for resp in tu.get("campaign_responses", []) or []:
        if not isinstance(resp, dict):
            continue
        negated = bool(resp.get("negated"))
        reqs.append(Requirement(
            counter.next(), "not_membership" if negated else "membership",
            campaign_table, "not_exists" if negated else "exists", campaign_table,
            negated=negated, source_span=str(resp.get("canonical") or "campaign_response")))

    # 명시적 미지원 슬롯이 있으면 모호로 기록(임의 확정 금지, §1).
    unsupported = query_plan.get("unsupported")
    if isinstance(unsupported, dict) and unsupported.get("reason"):
        ambiguous.append(f"unsupported:{unsupported.get('reason')}")

    # 필수 제외.
    for field_name, values in (exclude or {}).items():
        for v in (values if isinstance(values, list) else [values]):
            if v is None:
                continue
            excs.append(Exclusion(exc_counter.next(), field_name, "=", v, note="exclude"))

    # 중복 제거 키(회원 유일 키). 명시 없으면 관례 회원키.
    dedup = dedup_key
    spec = TargetSpecification(
        target_entity="customer",
        requirements=reqs,
        exclusions=excs,
        deduplication_key=dedup,
        ambiguous_requirements=ambiguous,
        retrieved_evidence=[RetrievedEvidence(**e) if isinstance(e, dict) else e
                            for e in (retrieved_evidence or [])],
    )
    # 중복 제거 요구를 명시 dedup_key 가 있을 때만 요구사항으로 추가(없으면 판정에서 강제하지 않음).
    if dedup:
        spec.requirements.append(Requirement(counter.next(), "dedup", dedup, None, None,
                                             source_span="중복 제거 키"))
    return spec


class _IdGen:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n}"


def _code_field(value: Any, default: str) -> str:
    """CRMDW 코드값('GENDER_CD.FEMALE')에서 컬럼명(GENDER_CD)을 뽑는다. 접두어가 없으면 default.

    코드 컬럼 저장값이 '컬럼명.값' 형태라는 규칙(외부 실DB CRMDW)에 기대 필드명을 SQL 컬럼과 정렬한다.
    이 규칙이 안 맞는 배포에선 값에 '.'이 없어 default 로 폴백하므로 안전하다."""
    if isinstance(value, str) and "." in value:
        prefix = value.split(".")[0].strip()
        if prefix and prefix.replace("_", "").isalnum():
            return prefix
    return default


def _metric_aggregate_func(metric_id: str | None) -> str | None:
    if not metric_id:
        return None
    m = metric_id.lower()
    if "count" in m or "order_count" in m or "번" in m or "distinct" in m:
        return "count_distinct" if "distinct" in m else "count"
    if "amount" in m or "amt" in m or "금액" in m or "sum" in m:
        return "sum"
    return None


# ------------------------------------------------------------------ 2~5) 오케스트레이션

def validate_target_sql(
    original_query: str,
    sql: str,
    spec: TargetSpecification,
    *,
    dialect: str | None = None,
    chat: llm_evidence.ChatClient | None = None,
    policy_violations: list[PolicyViolation] | None = None,
    execution_assertions: list[ExecutionAssertion] | None = None,
    evidence_version_mismatch: bool = False,
) -> ValidationResult:
    """전체 검증. 파싱 실패는 review(파서 오류), 정상은 규칙+선택적 LLM 근거로 판정."""
    try:
        semantics: SqlSemantics | None = extract_sql_semantics(sql, dialect=dialect)
        parser_errors: list[str] = []
    except SqlParseError as exc:
        semantics = None
        parser_errors = [str(exc)]

    if semantics is None:
        # AST 미가용: 의미 fail 이 아니라 기술 오류 → review(§2).
        return decide(spec, [], parser_errors=parser_errors,
                      policy_violations=policy_violations,
                      execution_assertions=execution_assertions,
                      evidence_version_mismatch=evidence_version_mismatch)

    rule_checks = map_requirements(spec, semantics)
    extras = detect_extra_restrictions(spec, semantics)

    # 규칙 미결(모호/부분) 항목만 LLM 근거 탐색(주입식). 없으면 규칙 결과 유지.
    checks, ambiguous_terms = llm_evidence.refine_checks(
        original_query, sql, spec, semantics, rule_checks, chat)

    result = decide(
        spec, checks,
        parser_errors=parser_errors,
        policy_violations=policy_violations,
        extra_restrictions=extras,
        execution_assertions=execution_assertions,
        evidence_version_mismatch=evidence_version_mismatch,
    )
    if ambiguous_terms:
        result.ambiguous_requirements = sorted(set(result.ambiguous_requirements) | set(ambiguous_terms))
    return result


# ------------------------------------------------------------------ §12 호환 어댑터 + shadow 모드

def to_legacy_semantic_verification(result: ValidationResult) -> dict[str, Any]:
    """기존 semantic_verification {ran, faithful, issues} 형태로 변환(§12 호환 어댑터).

    pass/review→faithful, fail→not faithful. review는 status로 구분되는 비차단 판정이며 issues는 위반 근거다."""
    faithful = result.status != "fail"
    issues: list[dict[str, Any]] = []
    for check in result.checks:
        if check.status == "contradicted":
            issues.append({"type": "inverted", "condition": check.requirement_id, "detail": check.reason})
        elif check.status == "missing":
            issues.append({"type": "dropped", "condition": check.requirement_id, "detail": check.reason})
    for pv in result.policy_violations:
        issues.append({"type": "policy", "condition": pv.code, "detail": pv.message})
    return {"ran": True, "faithful": faithful, "issues": issues,
            "status": result.status, "v2": True}


def run_shadow_validation(
    original_query: str,
    sql: str,
    query_plan: dict[str, Any],
    *,
    dialect: str | None = None,
    dedup_key: str | None = None,
    chat: llm_evidence.ChatClient | None = None,
    retrieved_evidence: list[dict[str, Any]] | None = None,
    field_columns: dict[str, str] | None = None,
    membership_tables: dict[str, str] | None = None,
    code_filter_values: set[str] | frozenset[str] | None = None,
    lifecycle_columns: dict[str, str] | None = None,
) -> dict[str, Any]:
    """shadow 모드 실행(§12): 신규 검증 결과를 dict 로 돌린다. 예외는 review 로 안전 폴백(호출자 흐름 불변).

    호출자는 이 결과를 sql_result['semantic_validation_v2'] 로 실어 트레이스/로깅하고, enforce 모드가 아니면
    사용자 응답 결정에는 쓰지 않는다."""
    try:
        spec = build_target_specification(query_plan, dedup_key=dedup_key,
                                          retrieved_evidence=retrieved_evidence,
                                          field_columns=field_columns,
                                          membership_tables=membership_tables,
                                          code_filter_values=code_filter_values,
                                          lifecycle_columns=lifecycle_columns)
        result = validate_target_sql(original_query, sql, spec, dialect=dialect, chat=chat)
        return {
            "ran": True,
            "spec": spec.to_dict(),
            "result": result.to_dict(),
            "legacy": to_legacy_semantic_verification(result),
        }
    except Exception as exc:  # noqa: BLE001 - shadow 는 절대 사용자 흐름을 깨지 않는다
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}
