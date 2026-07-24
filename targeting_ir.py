"""타겟 조건 도메인 IR(중간 표현) + 중앙 조건 레지스트리.

배경: 조건 유형을 하나 추가할 때마다 신호 감지(_has_member_target_signal 한 줄), 빌더 defer 목록,
confidence 조건 수집/라벨까지 서로 다른 곳에 손배선해야 했다 — Builder 가 도메인 의미·정책·실행 계획의
책임까지 떠안고 있었기 때문이다. 이 모듈은 그 도메인 지식을 Builder 앞의 별도 계층으로 올린다:

  ConditionSpec  — 조건 유형 하나의 도메인 선언(어디서 추출하나, 어떤 팩트가 필요한가, 신호로 치나,
                   confidence 에 어떻게 보이나). CONDITION_SPECS 가 단일 소스.
  TargetCondition — query_plan 에서 추출된 조건 인스턴스(IR 노드). extract_target_conditions 가 생성.

파생 규칙(소비자는 이 IR 만 보면 된다):
  - 신호 감지: spec.signals_target 인 조건이 하나라도 추출되면 실추출 가능 신호
    (graph_rag._has_member_target_signal 이 회원속성 컴파일 신호와 OR).
  - 빌더 defer: spec.fact_join 인 조건은 전용 팩트조인 빌더가 소유한다 — EXISTS-류 범용 빌더는
    이런 조건이 있으면 양보한다(graph_rag 캠페인 반응 빌더 등).
  - 빌더 소유권: fact_join 조건 kind ↔ 빌더 매핑은 graph_rag._sql_target_builder_registry 가 선언하고,
    테스트가 '모든 fact_join kind 는 정확히 하나의 빌더가 소유한다'를 강제한다(죽은 레지스트리 방지).
  - confidence: spec.confidence 메타로 조건 수집/한글 라벨이 자동 파생된다(confidence._extract_conditions).

순환 방지: 이 모듈은 graph_rag/confidence 를 import 하지 않는다(plain dict 입력). 설정 의존 값
(주문횟수 행동 집합)은 호출자가 order_count_behaviors 로 주입한다 — 기본값은 코드 상수와 동일.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 주문 횟수 행동(집계 기준은 member_target_filters.json 의 order_count_targets.behaviors 가 소유).
# graph_rag 는 로드된 설정 키 집합을 주입하고, 이 기본값은 설정 부재 시 폴백이다.
DEFAULT_ORDER_COUNT_BEHAVIORS = frozenset({"first_purchase", "repeat_buyer", "no_purchase"})
CART_BEHAVIOR = "cart_abandoner"

# 행동 한글 라벨(도메인 어휘 — confidence 가 재사용).
BEHAVIOR_KO = {
    "no_purchase": "구매 이력 없음(무구매)",
    "first_purchase": "첫 구매",
    "repeat_buyer": "재구매(2회 이상)",
    "cart_abandoner": "장바구니 이탈",
}


@dataclass(frozen=True)
class ConfidenceMeta:
    """조건이 confidence 리포트에 어떻게 나타나는지(수집 키/값/한글 라벨/채점 kind)."""

    kind: str  # confidence._score_condition 의 채점 분기 kind
    category: str
    key: Callable[[dict[str, Any]], str]
    value: Callable[[dict[str, Any]], Any]
    ko: Callable[[dict[str, Any]], str]
    # 파라미터가 채점 가능한 꼴인지(현행 confidence 의 isinstance 가드 보존). False 면 수집하지 않는다.
    applies: Callable[[dict[str, Any]], bool] = lambda params: True


# ── LLM 구조화 슬롯 스키마(단일 소스) ──────────────────────────────────────────────
# 배경: LLM 파서에 준 tool 스키마의 target_user 가 불투명 {"type":"object"} 라 LLM 이 어떤 구조화
# 슬롯(가입창/로그인창/카트/캠페인 등)이 있는지 몰랐고, coerce 도 그 슬롯을 받지 않아 결정론 정규식만이
# 유일한 소스였다. SlotShape 는 슬롯 하나의 (i) LLM tool JSON-schema 조각과 (ii) 닫힌 어휘 검증/정규화
# coerce 를 한 곳에서 선언한다 — graph_rag 는 이걸로 tool 스키마를 생성하고 LLM 출력을 검증한다.
# 순수 모듈 불변식 유지: graph_rag 를 import 하지 않는다. 런타임 렉시콘 의존 어휘(등급/카트 canonical)는
# 호출자가 coerce(raw, allowed=...) 로 주입한다.

# 캐노니컬 기간 단위 → 일수(LLM 슬롯 정규화 전용). graph_rag 의 한글 토큰 기간표(_DURATION_UNIT_DAYS)와
# 별개다 — 이건 정규화된 영문 단위값을 다룬다. 기존 파서가 쓰는 값 표기('days'/'months')와 호환.
UNIT_DAYS: dict[str, int] = {"days": 1, "weeks": 7, "months": 30, "years": 365}
# LLM/정규식이 낼 수 있는 단위 표기(한글·단수·복수)를 캐노니컬로 접는다.
_UNIT_ALIASES: dict[str, str] = {
    "일": "days", "day": "days", "days": "days",
    "주": "weeks", "주일": "weeks", "week": "weeks", "weeks": "weeks",
    "개월": "months", "달": "months", "month": "months", "months": "months",
    "년": "years", "해": "years", "year": "years", "years": "years",
}
OPERATORS: frozenset[str] = frozenset({">=", ">", "<=", "<"})
_OPERATOR_ALIASES: dict[str, str] = {
    "이상": ">=", "초과": ">", "이하": "<=", "미만": "<",
    ">=": ">=", ">": ">", "<=": "<=", "<": "<",
}


def _pos_int(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _pos_number(raw: Any) -> float | int | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(value) if float(value).is_integer() else value


def _canon_unit(raw: Any) -> str | None:
    return _UNIT_ALIASES.get(str(raw).strip().casefold()) if raw is not None else None


def _canon_operator(raw: Any) -> str | None:
    return _OPERATOR_ALIASES.get(str(raw).strip()) if raw is not None else None


def _window_days(raw: dict[str, Any]) -> tuple[int, str, int] | None:
    """{value,unit} 또는 {min_days}/{days} 를 (value, canonical_unit, min_days) 로 정규화."""
    value = _pos_int(raw.get("value"))
    unit = _canon_unit(raw.get("unit"))
    if value and unit:
        return value, unit, value * UNIT_DAYS[unit]
    min_days = _pos_int(raw.get("min_days")) or _pos_int(raw.get("days"))
    if min_days:
        return min_days, "days", min_days
    return None


def _coerce_window(raw: Any, *, sql_interval: bool, allowed: Any = None) -> dict[str, Any] | None:
    """recent_login/purchase_inactivity 창 슬롯: {value,unit,min_days[,sql_interval]}."""
    if not isinstance(raw, dict):
        return None
    parts = _window_days(raw)
    if parts is None:
        return None
    value, unit, min_days = parts
    out: dict[str, Any] = {"value": value, "unit": unit, "min_days": min_days}
    if sql_interval:
        out["sql_interval"] = f"{value} {unit}"
    return out


def _coerce_signup(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """signup_target: {days:int|None}. 'days' 명시(또는 None=기본창) 또는 {value,unit} 정규화."""
    if not isinstance(raw, dict):
        return None
    if "days" in raw:
        if raw["days"] is None:
            return {"days": None}
        days = _pos_int(raw["days"])
        return {"days": days} if days else None
    parts = _window_days(raw)
    return {"days": parts[2]} if parts else None


def _coerce_threshold_list(raw: Any, *, allowed: Any = None) -> list[dict[str, Any]] | None:
    """aggregate_conditions: [{metric_id, operator, threshold, window_days?, label?}]. metric_id∈allowed."""
    if not isinstance(raw, list):
        return None
    metrics = allowed if isinstance(allowed, (set, frozenset, dict)) else None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        metric_id = item.get("metric_id")
        operator = _canon_operator(item.get("operator"))
        threshold = _pos_number(item.get("threshold"))
        if not (isinstance(metric_id, str) and metric_id and operator and threshold is not None):
            continue
        if metrics is not None and metric_id not in metrics:
            continue
        cond: dict[str, Any] = {"metric_id": metric_id, "operator": operator, "threshold": threshold}
        window = _pos_int(item.get("window_days"))
        cond["window_days"] = window
        if isinstance(item.get("label"), str) and item["label"]:
            cond["label"] = item["label"]
        out.append(cond)
    return out or None


def _coerce_freq(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """campaign_response_frequency: {operator, count, window_days?, label?}."""
    if not isinstance(raw, dict):
        return None
    operator = _canon_operator(raw.get("operator"))
    count = _pos_int(raw.get("count"))
    if not (operator and count):
        return None
    out: dict[str, Any] = {"operator": operator, "count": count, "window_days": _pos_int(raw.get("window_days"))}
    if isinstance(raw.get("label"), str) and raw["label"]:
        out["label"] = raw["label"]
    return out


def _coerce_buy_amount(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """campaign_buy_amount: {operator, amount, window_days?, label?}."""
    if not isinstance(raw, dict):
        return None
    operator = _canon_operator(raw.get("operator"))
    amount = _pos_number(raw.get("amount"))
    if not (operator and amount is not None):
        return None
    out: dict[str, Any] = {"operator": operator, "amount": amount, "window_days": _pos_int(raw.get("window_days"))}
    if isinstance(raw.get("label"), str) and raw["label"]:
        out["label"] = raw["label"]
    return out


def _coerce_rate(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    operator = _canon_operator(raw.get("operator"))
    value = _pos_number(raw.get("value"))
    if not (operator and value is not None and 0 < value <= 100):
        return None
    return {"operator": operator, "value": float(value), "inferred": bool(raw.get("inferred", False))}


def _coerce_cell_rate(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """cell_rate_target: {success_rate:{operator,value,inferred}|None, buy_rate:{...}|None, label}."""
    if not isinstance(raw, dict):
        return None
    success = _coerce_rate(raw.get("success_rate"))
    buy = _coerce_rate(raw.get("buy_rate"))
    if success is None and buy is None:
        return None
    out: dict[str, Any] = {"success_rate": success, "buy_rate": buy}
    if isinstance(raw.get("label"), str) and raw["label"]:
        out["label"] = raw["label"]
    return out


def _coerce_cart_retention(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """cart_retention: {min_days}|{max_days} (+label). direction 또는 min/max_days 직접."""
    if not isinstance(raw, dict):
        return None
    min_days = _pos_int(raw.get("min_days"))
    max_days = _pos_int(raw.get("max_days"))
    if min_days is None and max_days is None:
        parts = _window_days(raw)
        if parts is None:
            return None
        direction = str(raw.get("direction", "min")).strip().casefold()
        if direction in ("max", "이내", "이하", "미만", "within"):
            max_days = parts[2]
        else:
            min_days = parts[2]
    out: dict[str, Any] = {}
    if min_days is not None:
        out["min_days"] = min_days
    if max_days is not None:
        out["max_days"] = max_days
    if isinstance(raw.get("label"), str) and raw["label"]:
        out["label"] = raw["label"]
    return out or None


def _coerce_cart_aggregate(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """cart_aggregate: {metric, operator, threshold}. metric∈allowed(카트 지표 집합)."""
    if not isinstance(raw, dict):
        return None
    metric = raw.get("metric")
    operator = _canon_operator(raw.get("operator"))
    threshold = _pos_int(raw.get("threshold"))
    if not (isinstance(metric, str) and metric and operator and threshold):
        return None
    if isinstance(allowed, (set, frozenset, dict)) and metric not in allowed:
        return None
    return {"metric": metric, "operator": operator, "threshold": threshold}


def _coerce_birthday(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """birthday_target: {granularity: 'month'|'today', column?}."""
    if not isinstance(raw, dict):
        return None
    granularity = str(raw.get("granularity", "")).strip().casefold()
    if granularity not in ("month", "today", "day"):
        return None
    return {"granularity": "month" if granularity == "month" else "today"}


def _coerce_purchase_date(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """purchase_date: {from, to, label?}. from/to 는 YYYY 이상 날짜 토큰(자릿수 검증)."""
    if not isinstance(raw, dict):
        return None
    frm = str(raw.get("from", "")).strip()
    to = str(raw.get("to", "")).strip()

    def _has_year(token: str) -> bool:
        # 연도 필수: YYYYMM(6)·YYYYMMDD(8), 또는 YYYY(4)는 그럴듯한 연도 범위일 때만(MMDD 오인 방지).
        if not token.isdigit():
            return False
        if len(token) in (6, 8):
            return True
        return len(token) == 4 and 1900 <= int(token) <= 2100

    if not (_has_year(frm) and _has_year(to)):
        return None
    out: dict[str, Any] = {"from": frm, "to": to}
    if isinstance(raw.get("label"), str) and raw["label"]:
        out["label"] = raw["label"]
    return out


def _coerce_string(raw: Any, *, allowed: Any = None) -> str | None:
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _coerce_bool_true(raw: Any, *, allowed: Any = None) -> bool | None:
    """부재형 불리언 슬롯: 명시적 True 만 인정(그 외는 슬롯 미설정으로 drop)."""
    return True if raw is True else None


def _coerce_campaign_responses(raw: Any, *, allowed: Any = None) -> list[dict[str, Any]] | None:
    """campaign_responses: [{canonical, predicate[, negated, source]}]. canonical∈allowed(→predicate/source 맵).

    allowed 는 {canonical: {"predicate":..., "source":...}} 형태로 graph_rag 가 주입한다 — LLM 은 canonical
    (+ negated)만 고르고, SQL 술어는 결정론 매핑에서 채워 임의 SQL 주입을 막는다."""
    if not isinstance(raw, list) or not isinstance(allowed, dict):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        canonical = item.get("canonical")
        mapping = allowed.get(canonical) if isinstance(canonical, str) else None
        if not isinstance(mapping, dict):
            continue
        entry: dict[str, Any] = {"canonical": canonical, "predicate": mapping["predicate"]}
        if mapping.get("source"):
            entry["source"] = mapping["source"]
        if item.get("negated"):
            entry["negated"] = True
        out.append(entry)
    return out or None


def _coerce_cart_type(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """cart_type: {canonical, value, label, unpaid_only}. canonical∈allowed(→전체 shape 맵)."""
    if not isinstance(raw, dict) or not isinstance(allowed, dict):
        return None
    key = raw.get("canonical") or raw.get("value")
    shape = allowed.get(key) if isinstance(key, str) else None
    return dict(shape) if isinstance(shape, dict) else None


def _coerce_ranking_dict(raw: Any, *, allowed: Any = None) -> dict[str, Any] | None:
    """plan-level 랭킹 슬롯(dict) 통과 검증 — 최소한 dict 이고 비어있지 않으면 유지(세부는 빌더가 검증)."""
    return raw if isinstance(raw, dict) and raw else None


@dataclass(frozen=True)
class SlotShape:
    """구조화 슬롯 하나의 LLM 스키마 조각 + 닫힌 어휘 coerce(단일 소스)."""

    name: str
    container: str  # "target_user" | "plan"
    schema: dict[str, Any]  # LLM tool 에 노출할 JSON-schema 조각
    coerce: Callable[..., Any]  # (raw, *, allowed=None) -> normalized | None
    allowed_key: str | None = None  # graph_rag 가 주입할 렉시콘 어휘 키(cart_type/campaign_responses/등)


# JSON-schema 조각 헬퍼(간결 표기 — 세부 검증은 coerce 가 담당하므로 스키마는 느슨하게).
def _obj_schema(desc: str) -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _list_schema(desc: str) -> dict[str, Any]:
    return {"type": "array", "description": desc, "items": {"type": "object"}}


# 구조화 슬롯 레지스트리(단일 소스). ConditionSpec.slot 이 이 중 하나를 참조한다.
_UNIT_HINT = "기간 단위 unit ∈ days|weeks|months|years, 또는 min_days 정수"
_OP_HINT = "연산자 operator ∈ >=|>|<=|<"
SLOT_SHAPES: dict[str, SlotShape] = {
    "signup_target": SlotShape("signup_target", "target_user",
        _obj_schema(f"최근 N기간 이내 가입. {{days:int}} 또는 {{value,unit}}({_UNIT_HINT}). days=null 이면 기본창."),
        _coerce_signup),
    "recent_login": SlotShape("recent_login", "target_user",
        _obj_schema(f"최근 N기간 이내 로그인/접속. {{value,unit}} 또는 {{min_days}}. {_UNIT_HINT}"),
        lambda raw, *, allowed=None: _coerce_window(raw, sql_interval=True, allowed=allowed)),
    "purchase_inactivity": SlotShape("purchase_inactivity", "target_user",
        _obj_schema(f"최근 N기간 미구매(창 anti-join). {{value,unit}} 또는 {{min_days}}. {_UNIT_HINT}"),
        lambda raw, *, allowed=None: _coerce_window(raw, sql_interval=False, allowed=allowed)),
    "cart_retention": SlotShape("cart_retention", "target_user",
        _obj_schema(f"장바구니 보관 기간. {{min_days}}(이상) 또는 {{max_days}}(이내), 또는 {{value,unit,direction}}."),
        _coerce_cart_retention),
    "cart_aggregate": SlotShape("cart_aggregate", "target_user",
        _obj_schema(f"장바구니 개수/수량 임계. {{metric, operator, threshold}}. {_OP_HINT}"),
        _coerce_cart_aggregate, allowed_key="cart_aggregate_metrics"),
    "cart_type": SlotShape("cart_type", "target_user",
        _obj_schema("장바구니 유형. {value:<canonical>} (정기배송/픽업/일반 등 허용 canonical)."),
        _coerce_cart_type, allowed_key="cart_types"),
    "birthday_target": SlotShape("birthday_target", "target_user",
        _obj_schema("생일 타겟. {granularity: 'month'(이달)|'today'(오늘)}."),
        _coerce_birthday),
    "campaign_responses": SlotShape("campaign_responses", "target_user",
        _list_schema("캠페인 반응 리스트. [{canonical:<접촉/오퍼/구매반응/쿠폰 canonical>, negated?:bool}]."),
        _coerce_campaign_responses, allowed_key="campaign_responses"),
    "campaign_response_frequency": SlotShape("campaign_response_frequency", "target_user",
        _obj_schema(f"캠페인 반응 횟수 임계. {{operator, count, window_days?}}. {_OP_HINT}"),
        _coerce_freq),
    "campaign_buy_amount": SlotShape("campaign_buy_amount", "target_user",
        _obj_schema(f"캠페인 귀속 구매금액 임계. {{operator, amount, window_days?}}. {_OP_HINT}"),
        _coerce_buy_amount),
    "cell_rate_target": SlotShape("cell_rate_target", "target_user",
        _obj_schema(f"셀 성공률/구매율. {{success_rate:{{operator,value}}, buy_rate:{{operator,value}}}} (value 0~100). {_OP_HINT}"),
        _coerce_cell_rate),
    "purchase_date": SlotShape("purchase_date", "target_user",
        _obj_schema("절대 구매 날짜창. {from:'YYYYMMDD', to:'YYYYMMDD'} (연도 필수)."),
        _coerce_purchase_date),
    "purchase_object": SlotShape("purchase_object", "target_user",
        {"type": "string", "description": "구매한 상품/브랜드 자유 텍스트(부분일치)."},
        _coerce_string),
    "cart_absence": SlotShape("cart_absence", "target_user",
        {"type": "boolean", "description": "장바구니(보관 상품)가 없는 회원. true 만 설정('장바구니 없는/생성 안 한')."},
        _coerce_bool_true),
    "aggregate_conditions": SlotShape("aggregate_conditions", "target_user",
        _list_schema(f"누적 지표 임계 리스트. [{{metric_id, operator, threshold, window_days?}}]. {_OP_HINT}"),
        _coerce_threshold_list, allowed_key="aggregate_metrics"),
    "region_density_target": SlotShape("region_density_target", "plan",
        _obj_schema("밀집 지역 랭킹 타겟(코호트 조건으로 지역 랭킹)."),
        _coerce_ranking_dict),
    "member_metric_ranking": SlotShape("member_metric_ranking", "plan",
        _obj_schema("회원 지표 상위 N 랭킹."),
        _coerce_ranking_dict),
    "purchase_count_ranking": SlotShape("purchase_count_ranking", "plan",
        _obj_schema("기간 내 구매 상위 N 랭킹."),
        _coerce_ranking_dict),
}


@dataclass(frozen=True)
class ConditionSpec:
    """조건 유형 하나의 도메인 선언(단일 소스). 추출/팩트/신호/confidence 파생이 전부 여기서 나온다."""

    kind: str
    fact: str  # 필요한 팩트 계열: member | order | cart | campaign | region
    fact_join: bool  # 전용 팩트조인 빌더가 소유(EXISTS-류 범용 빌더는 양보)
    signals_target: bool  # 실추출 가능 타겟 신호로 인정(intent 승격/필수조건 검증)
    extract: Callable[[dict[str, Any], frozenset[str]], list[dict[str, Any]]]
    confidence: ConfidenceMeta | None = None
    slot: SlotShape | None = None  # LLM 구조화 슬롯 스키마/coerce(없으면 coarse-only 슬롯)


@dataclass(frozen=True)
class TargetCondition:
    """query_plan 에서 추출된 조건 인스턴스(IR 노드)."""

    kind: str
    params: dict[str, Any]
    spec: ConditionSpec


def _tu(plan: dict[str, Any]) -> dict[str, Any]:
    target_user = plan.get("target_user")
    return target_user if isinstance(target_user, dict) else {}


def _tu_dict(name: str) -> Callable[[dict[str, Any], frozenset[str]], list[dict[str, Any]]]:
    """target_user.<name> 이 dict 면 그것을 파라미터로 추출(현행 isinstance dict 신호 판정과 동일)."""

    def extract(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
        value = _tu(plan).get(name)
        return [value] if isinstance(value, dict) else []

    return extract


def _plan_dict(name: str) -> Callable[[dict[str, Any], frozenset[str]], list[dict[str, Any]]]:
    """plan 최상위 <name> 이 dict 면 추출(밀집지역/랭킹류)."""

    def extract(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
        value = plan.get(name)
        return [value] if isinstance(value, dict) else []

    return extract


def _extract_signup(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    tu = _tu(plan)
    signup = tu.get("signup_target")
    if isinstance(signup, dict):
        return [{"days": signup.get("days")}]
    if "new_user" in (tu.get("lifecycle") or []):
        return [{"days": None}]
    return []


def _extract_order_count_behaviors(plan: dict[str, Any], behaviors_ctx: frozenset[str]) -> list[dict[str, Any]]:
    return [{"behavior": b} for b in _tu(plan).get("behaviors", []) if b in behaviors_ctx]


def _extract_cart_abandoner(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    return [{"behavior": CART_BEHAVIOR}] if CART_BEHAVIOR in _tu(plan).get("behaviors", []) else []


def _extract_unclassified_behaviors(plan: dict[str, Any], behaviors_ctx: frozenset[str]) -> list[dict[str, Any]]:
    """지원 집합 밖의 행동(예: office_worker). 신호로 치지 않지만(현행 intent 미승격 보존)
    팩트조인 필요 조건으로는 남겨 EXISTS-류 빌더가 조용히 삼키지 않게 한다(현행 defer 보존)."""
    return [
        {"behavior": b}
        for b in _tu(plan).get("behaviors", [])
        if b not in behaviors_ctx and b != CART_BEHAVIOR
    ]


def _extract_purchase_object(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    tu = _tu(plan)
    value = tu.get("purchase_object")
    if isinstance(value, str) and value:
        return [{"value": value, "object_kind": tu.get("purchase_object_kind")}]
    return []


def _extract_campaign_responses(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    responses = _tu(plan).get("campaign_responses")
    return [{"responses": responses}] if isinstance(responses, list) and responses else []


def _extract_cart_absence(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    return [{}] if _tu(plan).get("cart_absence") else []


def _extract_aggregate_conditions(plan: dict[str, Any], _behaviors: frozenset[str]) -> list[dict[str, Any]]:
    conditions = _tu(plan).get("aggregate_conditions")
    return [{"conditions": conditions}] if isinstance(conditions, list) and conditions else []


def _cart_retention_ko(params: dict[str, Any]) -> str:
    days = params.get("min_days") or params.get("max_days")
    direction = "이상" if params.get("min_days") else "이내"
    return params.get("label") or f"장바구니 보관 {days}일 {direction}"


# 조건 레지스트리(단일 소스). 순서는 confidence 조건 목록 표시 순서를 겸한다(기존 수작업 순서 보존:
# 가입 → 생일 → 최근로그인 → 미구매창 → 캠페인반응횟수 → 카트보관/유형 → 행동 → 상품/날짜 구매이력).
CONDITION_SPECS: tuple[ConditionSpec, ...] = (
    # ── 회원 술어 계열(compile_member_target_conditions 가 컴파일; 신호도 그쪽 has_signal 이 담당) ──
    ConditionSpec(
        kind="signup_target", fact="member", fact_join=False, signals_target=False,
        extract=_extract_signup,
        confidence=ConfidenceMeta(
            kind="signup", category="date",
            key=lambda p: "signup_target", value=lambda p: p.get("days"),
            ko=lambda p: f"최근 {p.get('days') or 90}일 이내 가입",
        ),
    ),
    ConditionSpec(
        kind="birthday_target", fact="member", fact_join=False, signals_target=True,
        extract=_tu_dict("birthday_target"),
        confidence=ConfidenceMeta(
            kind="birthday", category="date",
            key=lambda p: "birthday_target",
            value=lambda p: "이달" if p.get("granularity") == "month" else "오늘",
            ko=lambda p: f"생일 {'이달' if p.get('granularity') == 'month' else '오늘'}",
        ),
    ),
    ConditionSpec(
        kind="recent_login", fact="member", fact_join=False, signals_target=False,
        extract=_tu_dict("recent_login"),
        confidence=ConfidenceMeta(
            kind="recent_login", category="date",
            key=lambda p: "recent_login", value=lambda p: p.get("min_days"),
            ko=lambda p: f"최근 {p.get('min_days')}일 이내 로그인",
            applies=lambda p: isinstance(p.get("min_days"), int),
        ),
    ),
    # ── 주문 팩트 계열 ──
    ConditionSpec(
        kind="purchase_inactivity", fact="order", fact_join=True, signals_target=True,
        extract=_tu_dict("purchase_inactivity"),
        confidence=ConfidenceMeta(
            kind="order_window", category="date",
            key=lambda p: "purchase_inactivity", value=lambda p: p.get("min_days"),
            ko=lambda p: f"최근 {p.get('min_days')}일 미구매",
            applies=lambda p: isinstance(p.get("min_days"), int),
        ),
    ),
    # ── 캠페인 반응 팩트 계열 ──
    ConditionSpec(
        kind="campaign_response_frequency", fact="campaign", fact_join=True, signals_target=True,
        extract=_tu_dict("campaign_response_frequency"),
        confidence=ConfidenceMeta(
            kind="campaign_response", category="behavior",
            key=lambda p: "campaign_response_frequency", value=lambda p: p.get("count"),
            ko=lambda p: p.get("label") or f"캠페인 {p.get('count')}회 이상 반응",
            applies=lambda p: isinstance(p.get("count"), int),
        ),
    ),
    # 셀 단위 비율 타겟('발송 성공률 높고 구매율 낮은 셀의 회원') — 회원 조건이 아니라 셀을 비율로
    # 고른 뒤 그 셀의 발송 대상 회원을 타겟한다(Z_CAMP_MBR 셀 집계 HAVING → 셀 회원 조인).
    ConditionSpec(
        kind="cell_rate_target", fact="campaign", fact_join=True, signals_target=True,
        extract=_tu_dict("cell_rate_target"),
        confidence=ConfidenceMeta(
            kind="campaign_response", category="behavior",
            key=lambda p: "cell_rate_target",
            value=lambda p: p.get("label") or "cell_rate",
            ko=lambda p: p.get("label") or "셀 성공률/구매율 조건",
            applies=lambda p: bool(p.get("success_rate") or p.get("buy_rate")),
        ),
    ),
    # 캠페인 '귀속 구매금액'(반응 팩트 BUY_AMT 회원별 합계) — 전 생애 주문 합(aggregate_conditions
    # purchase_amount)과 다른 지표라 별도 kind. 반응 횟수와 같은 캠페인 팩트 집계 빌더가 소유한다.
    ConditionSpec(
        kind="campaign_buy_amount", fact="campaign", fact_join=True, signals_target=True,
        extract=_tu_dict("campaign_buy_amount"),
        confidence=ConfidenceMeta(
            kind="campaign_response", category="purchase",
            key=lambda p: "campaign_buy_amount", value=lambda p: p.get("amount"),
            ko=lambda p: p.get("label") or f"캠페인 구매금액 {p.get('amount')} 조건",
            applies=lambda p: isinstance(p.get("amount"), (int, float)),
        ),
    ),
    # 캠페인 반응(EXISTS ≥1회)은 회원키 EXISTS 술어로 어느 빌더에나 AND 결합 가능 → fact_join 아님.
    ConditionSpec(
        kind="campaign_responses", fact="campaign", fact_join=False, signals_target=True,
        extract=_extract_campaign_responses,
    ),
    # 장바구니 부재('장바구니 없는/생성 안 한')는 회원키 NOT EXISTS 술어라 캠페인 반응과 동일하게 어느
    # 빌더에나 AND 결합된다(fact_join 아님, 전용 빌더 불필요). 구매 부재는 기존 no_purchase 트랙이 소유.
    ConditionSpec(
        kind="cart_absence", fact="cart", fact_join=False, signals_target=True,
        extract=_extract_cart_absence,
        confidence=ConfidenceMeta(
            kind="cart", category="behavior",
            key=lambda p: "cart_absence", value=lambda p: "cart_absence",
            ko=lambda p: "장바구니 없음(미보관)",
        ),
    ),
    # ── 장바구니 팩트 계열 ──
    ConditionSpec(
        kind="cart_retention", fact="cart", fact_join=True, signals_target=True,
        extract=_tu_dict("cart_retention"),
        confidence=ConfidenceMeta(
            kind="cart", category="date",
            key=lambda p: "cart_retention",
            value=lambda p: p.get("min_days") or p.get("max_days"),
            ko=_cart_retention_ko,
            applies=lambda p: bool(p.get("min_days") or p.get("max_days")),
        ),
    ),
    ConditionSpec(
        kind="cart_type", fact="cart", fact_join=True, signals_target=True,
        extract=_tu_dict("cart_type"),
        confidence=ConfidenceMeta(
            kind="cart", category="cart_type",
            key=lambda p: "cart_type", value=lambda p: p.get("value"),
            ko=lambda p: p.get("label") or p.get("value"),
            applies=lambda p: bool(p.get("value")),
        ),
    ),
    ConditionSpec(
        kind="cart_aggregate", fact="cart", fact_join=True, signals_target=True,
        extract=_tu_dict("cart_aggregate"),
    ),
    # ── 행동(behaviors 리스트에서 분류) ──
    ConditionSpec(
        kind="order_count_behavior", fact="order", fact_join=True, signals_target=True,
        extract=_extract_order_count_behaviors,
        confidence=ConfidenceMeta(
            kind="order_count", category="behavior",
            key=lambda p: p["behavior"], value=lambda p: p["behavior"],
            ko=lambda p: BEHAVIOR_KO.get(p["behavior"], p["behavior"]),
        ),
    ),
    ConditionSpec(
        kind="cart_abandoner", fact="cart", fact_join=True, signals_target=True,
        extract=_extract_cart_abandoner,
        confidence=ConfidenceMeta(
            kind="cart", category="behavior",
            key=lambda p: CART_BEHAVIOR, value=lambda p: CART_BEHAVIOR,
            ko=lambda p: BEHAVIOR_KO[CART_BEHAVIOR],
        ),
    ),
    ConditionSpec(
        kind="unclassified_behavior", fact="order", fact_join=True, signals_target=False,
        extract=_extract_unclassified_behaviors,
    ),
    # ── 구매 이력(상품/날짜) — purchase_history 빌더 소유 ──
    ConditionSpec(
        kind="purchase_object", fact="order", fact_join=True, signals_target=True,
        extract=_extract_purchase_object,
        confidence=ConfidenceMeta(
            kind="free_text", category="purchase",
            key=lambda p: "purchase_object", value=lambda p: p["value"],
            ko=lambda p: ("브랜드 구매 이력" if p.get("object_kind") == "brand" else "상품 구매 이력")
            + f": '{p['value']}'",
        ),
    ),
    ConditionSpec(
        kind="purchase_date", fact="order", fact_join=True, signals_target=True,
        extract=_tu_dict("purchase_date"),
        confidence=ConfidenceMeta(
            kind="order_window", category="date",
            key=lambda p: "purchase_date", value=lambda p: f"{p.get('from')}~{p.get('to')}",
            ko=lambda p: p.get("label") or f"구매 날짜 {p.get('from')}~{p.get('to')}",
            applies=lambda p: bool(p.get("from") and p.get("to")),
        ),
    ),
    # ── 집계/랭킹/지역(plan 최상위) ──
    ConditionSpec(
        kind="aggregate_conditions", fact="order", fact_join=True, signals_target=True,
        extract=_extract_aggregate_conditions,
    ),
    ConditionSpec(
        kind="purchase_count_ranking", fact="order", fact_join=True, signals_target=True,
        extract=_plan_dict("purchase_count_ranking"),
    ),
    ConditionSpec(
        kind="member_metric_ranking", fact="order", fact_join=True, signals_target=True,
        extract=_plan_dict("member_metric_ranking"),
    ),
    ConditionSpec(
        kind="region_density_target", fact="region", fact_join=True, signals_target=True,
        extract=_plan_dict("region_density_target"),
    ),
)

_SPECS_BY_KIND = {spec.kind: spec for spec in CONDITION_SPECS}
assert len(_SPECS_BY_KIND) == len(CONDITION_SPECS), "CONDITION_SPECS kind 중복"


def extract_target_conditions(
    query_plan: dict[str, Any],
    *,
    order_count_behaviors: frozenset[str] = DEFAULT_ORDER_COUNT_BEHAVIORS,
) -> list[TargetCondition]:
    """query_plan → 타겟 조건 IR 목록. 레지스트리 선언 순서대로 추출한다.

    order_count_behaviors: 주문횟수 집계로 컴파일되는 행동 집합(설정 소유 — graph_rag 가
    order_count_targets.behaviors 키 집합을 주입). 이 집합/cart_abandoner 밖의 행동은
    unclassified_behavior 로 분류된다."""
    conditions: list[TargetCondition] = []
    for spec in CONDITION_SPECS:
        for params in spec.extract(query_plan, order_count_behaviors):
            conditions.append(TargetCondition(kind=spec.kind, params=params, spec=spec))
    return conditions


def fact_join_kinds() -> frozenset[str]:
    """전용 팩트조인 빌더 소유가 필요한 조건 kind 집합(소유권 불변식 검증용)."""
    return frozenset(spec.kind for spec in CONDITION_SPECS if spec.fact_join)


# ── LLM 구조화 슬롯 파생(graph_rag 가 tool 스키마·coerce 를 여기서 파생) ──────────────
_SPEC_KINDS = frozenset(spec.kind for spec in CONDITION_SPECS)
# 불변식: 모든 구조화 슬롯 이름은 조건 레지스트리의 kind 여야 한다(고아 슬롯 방지).
assert set(SLOT_SHAPES) <= _SPEC_KINDS, f"SLOT_SHAPES 에 미등록 kind: {set(SLOT_SHAPES) - _SPEC_KINDS}"


def structured_slot_shapes() -> tuple[SlotShape, ...]:
    """LLM 이 채울 수 있는 구조화 슬롯 목록(tool 스키마·coerce 파생의 단일 소스)."""
    return tuple(SLOT_SHAPES.values())


def slot_coercers() -> tuple[SlotShape, ...]:
    """coerce 대상 슬롯(현재는 전 슬롯). structured_slot_shapes 의 별칭 — 의미 분리용."""
    return structured_slot_shapes()
