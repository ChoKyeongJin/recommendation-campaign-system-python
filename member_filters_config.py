"""회원 타겟 레지스트리에서 **여러 모듈이 공유해야 하는 값**만 꺼내는 순수 접근자.

배경: ``order_count_targets.behaviors`` 의 키 집합(주문 횟수 행동)을 세 소비자가 각자 다른 방법으로
얻고 있었다 — graph_rag 는 설정 JSON 을 주입하고, confidence 와 canonical_targeting 은
targeting_ir 의 코드 기본값 폴백을 썼다. 그래서 설정에 행동을 하나 추가하면 같은 조건이
graph_rag 에서는 ``order_count_behavior`` 로, 나머지 둘에서는 ``unclassified_behavior`` 로 분류돼
신뢰도 리포트와 레거시 조건 트리에서 조용히 빠졌다. 값이 세 곳에 이중 소유돼 있던 것이다.

이 모듈은 그 값을 한 곳에서 읽는다. graph_rag 를 import 하지 않으므로(순수 모듈) confidence
같은 하위 계층도 순환 없이 쓸 수 있고, targeting_ir 은 여전히 설정을 모른다
(호출자가 주입하는 규약 유지). (당시 세 번째 소비자였던 canonical_targeting 모듈은 이후 삭제됐다.)

경로 규약은 graph_rag 와 동일하다: 환경변수 GRAPH_RAG_MEMBER_TARGET_FILTERS 로 재지정 가능,
기본값은 저장소의 docs/data/runtime/sql/member_target_filters.json. 기본 경로는 **모듈 기준 절대경로**다 —
상대경로면 cwd 가 바뀌는 순간 조용히 빈 설정으로 강등되기 때문이다. 물리 바인딩은 이 JSON만
소유하며 코드 미러는 두지 않는다. 실행 경계는 :func:`load_config` 실패를 명시적인 구성 오류로
처리하고, 어휘 탐색용 접근자는 빈 결과로 fail-close한다.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = Path(
    os.getenv("GRAPH_RAG_MEMBER_TARGET_FILTERS")
    or (_REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json")
)


class MemberFiltersConfigError(RuntimeError):
    """회원 타겟 물리 바인딩 파일을 신뢰할 수 없을 때 발생한다."""


_REQUIRED_MAPPING_SECTIONS: tuple[str, ...] = (
    "base_entity",
    "active_state",
    "birthday_target",
    "signup_target",
    "recent_login_target",
    "order_count_targets",
    "aggregate_targets",
    "cart_targets",
    "campaign_response_targets",
    "cell_rate_targets",
    "region_target",
    "purchase_product_target",
    "entity_set_targets",
    "region_density",
    "group_ranking_axes",
    "member_metric_ranking",
    "validation",
)
_REQUIRED_LIST_SECTIONS: tuple[str, ...] = (
    "eq_filters",
    "activity_filters",
    "numeric_filters",
    "purchase_product_match_columns",
)
_REQUIRED_NESTED_MAPPING_PATHS: tuple[tuple[str, ...], ...] = (
    ("order_count_targets", "behaviors"),
    ("aggregate_targets", "metrics"),
    ("cart_targets", "join"),
    ("cart_targets", "product_join"),
    ("cart_targets", "active_condition"),
    ("cart_targets", "behaviors"),
    ("cart_targets", "aggregate_metrics"),
    ("campaign_response_targets", "member_join"),
    ("campaign_response_targets", "campaign_join"),
    ("campaign_response_targets", "target_group_condition"),
    ("campaign_response_targets", "valid_campaign_condition"),
    ("campaign_response_targets", "contact_member_list"),
    ("campaign_response_targets", "contact_member_list", "member_join"),
    ("campaign_response_targets", "contact_member_list", "campaign_join"),
    ("campaign_response_targets", "contact_member_list", "target_group_condition"),
    ("campaign_response_targets", "contact_member_list", "valid_campaign_condition"),
    ("campaign_response_targets", "frequency_events"),
    ("campaign_response_targets", "boolean_metrics"),
    ("campaign_response_targets", "boolean_metrics", "purchase_response"),
    ("campaign_response_targets", "aggregate_metrics"),
    ("campaign_response_targets", "aggregate_metrics", "campaign_purchase_amount"),
    ("campaign_response_targets", "aggregate_metrics", "used_coupon_count"),
    ("cell_rate_targets", "member_join"),
    ("cell_rate_targets", "response_join"),
    ("region_target", "target_basis"),
    ("region_target", "columns"),
    ("purchase_product_target", "order_header"),
    ("purchase_product_target", "order_detail"),
    ("purchase_product_target", "product"),
    ("aggregate_targets", "grain_axes"),
    ("aggregate_targets", "grain_axes", "per_order"), ("aggregate_targets", "grain_axes", "per_product"),
    ("aggregate_targets", "grain_axes", "per_brand"),
    ("entity_set_targets", "directions"),
    ("entity_set_targets", "measures"),
    ("entity_set_targets", "entities"),
    ("entity_set_targets", "filters"),
    ("entity_set_targets", "relations"),
    ("region_density", "granularity_columns"),
    ("member_metric_ranking", "direction_tokens"),
    ("group_ranking_axes", "age_group", "age_band"),
)
_REQUIRED_NESTED_LIST_PATHS: tuple[tuple[str, ...], ...] = (
    ("order_count_targets", "evidence_tables"),
    ("cart_targets", "cart_types"),
    ("campaign_response_targets", "campaign_join", "conditions"),
    (
        "campaign_response_targets",
        "contact_member_list",
        "campaign_join",
        "conditions",
    ),
    ("cell_rate_targets", "cell_keys"),
    ("purchase_product_target", "match_columns"),
    ("region_density", "granularity_tokens"),
    ("member_metric_ranking", "granularity_tokens"),
    ("member_metric_ranking", "supported_metrics"),
    ("validation", "allowed_table_aliases"),
)
_REQUIRED_STRING_PATHS: tuple[tuple[str, ...], ...] = (
    ("base_entity", "table"),
    ("base_entity", "alias"),
    ("base_entity", "member_key"),
    ("base_entity", "login_id_key"),
    ("base_entity", "age_column"),
    ("base_entity", "date_format"),
    ("active_state", "column"),
    ("active_state", "value"),
    ("birthday_target", "column"),
    ("signup_target", "column"),
    ("signup_target", "table"),
    ("recent_login_target", "column"),
    ("recent_login_target", "table"),
    ("order_count_targets", "table"),
    ("order_count_targets", "join_column"),
    ("order_count_targets", "order_id_column"),
    ("order_count_targets", "order_date_column"),
    ("aggregate_targets", "table"),
    ("aggregate_targets", "join_column"),
    ("aggregate_targets", "date_column"),
    ("aggregate_targets", "grain_axes", "per_order", "table"), ("aggregate_targets", "grain_axes", "per_order", "column"),
    ("aggregate_targets", "grain_axes", "per_product", "table"), ("aggregate_targets", "grain_axes", "per_product", "column"),
    ("aggregate_targets", "grain_axes", "per_brand", "table"), ("aggregate_targets", "grain_axes", "per_brand", "column"),
    ("cart_targets", "table"),
    ("cart_targets", "alias"),
    ("cart_targets", "join", "left"),
    ("cart_targets", "join", "right"),
    ("cart_targets", "active_condition", "column"),
    ("cart_targets", "active_condition", "value"),
    ("cart_targets", "product_join", "table"),
    ("cart_targets", "product_join", "alias"),
    ("cart_targets", "product_join", "left"),
    ("cart_targets", "product_join", "right"),
    ("cart_targets", "quantity_column"),
    ("campaign_response_targets", "table"), ("campaign_response_targets", "alias"),
    ("campaign_response_targets", "member_column"), ("campaign_response_targets", "member_join", "left"),
    ("campaign_response_targets", "member_join", "right"), ("campaign_response_targets", "campaign_join", "table"),
    ("campaign_response_targets", "campaign_join", "alias"), ("campaign_response_targets", "target_group_condition", "column"),
    ("campaign_response_targets", "target_group_condition", "value"), ("campaign_response_targets", "valid_campaign_condition", "expression"),
    ("campaign_response_targets", "campaign_date_column"), ("campaign_response_targets", "campaign_key_expression"),
    ("campaign_response_targets", "response_predicate"), ("campaign_response_targets", "boolean_metrics", "purchase_response", "column"),
    ("campaign_response_targets", "boolean_metrics", "purchase_response", "value"),
    ("campaign_response_targets", "aggregate_metrics", "campaign_purchase_amount", "agg"),
    ("campaign_response_targets", "aggregate_metrics", "campaign_purchase_amount", "column"),
    ("campaign_response_targets", "aggregate_metrics", "used_coupon_count", "agg"),
    ("campaign_response_targets", "aggregate_metrics", "used_coupon_count", "column"),
    ("campaign_response_targets", "contact_member_list", "table"), ("campaign_response_targets", "contact_member_list", "alias"),
    ("campaign_response_targets", "contact_member_list", "member_column"), ("campaign_response_targets", "contact_member_list", "member_join", "left"),
    ("campaign_response_targets", "contact_member_list", "member_join", "right"),
    ("campaign_response_targets", "contact_member_list", "campaign_join", "table"),
    ("campaign_response_targets", "contact_member_list", "campaign_join", "alias"),
    ("campaign_response_targets", "contact_member_list", "target_group_condition", "column"),
    ("campaign_response_targets", "contact_member_list", "target_group_condition", "value"),
    ("campaign_response_targets", "contact_member_list", "valid_campaign_condition", "expression"),
    ("campaign_response_targets", "contact_member_list", "campaign_date_column"),
    ("campaign_response_targets", "contact_member_list", "campaign_key_expression"),
    ("cell_rate_targets", "member_table"), ("cell_rate_targets", "alias"),
    ("cell_rate_targets", "member_column"), ("cell_rate_targets", "member_join", "left"),
    ("cell_rate_targets", "member_join", "right"), ("cell_rate_targets", "response_join", "table"),
    ("cell_rate_targets", "response_join", "alias"), ("cell_rate_targets", "response_join", "buy_predicate"),
    ("cell_rate_targets", "cell_alias"), ("cell_rate_targets", "cell_subquery_alias"),
    ("cell_rate_targets", "contact_success_column"),
    ("region_target", "columns", "sido"), ("region_target", "columns", "sigungu"),
    ("purchase_product_target", "order_header", "table"),
    ("purchase_product_target", "order_header", "alias"),
    ("purchase_product_target", "order_header", "order_id_column"),
    ("purchase_product_target", "order_header", "date_column"),
    ("purchase_product_target", "order_header", "time_column"),
    ("purchase_product_target", "order_detail", "table"),
    ("purchase_product_target", "order_detail", "alias"),
    ("purchase_product_target", "order_detail", "join"),
    ("purchase_product_target", "order_detail", "date_column"),
    ("purchase_product_target", "product", "table"),
    ("purchase_product_target", "product", "alias"),
    ("purchase_product_target", "product", "join"),
    ("purchase_product_target", "product", "brand_name_column"),
    ("region_density", "default_column"),
)
_REQUIRED_POSITIVE_INT_PATHS: tuple[tuple[str, ...], ...] = (
    ("signup_target", "default_days"),
    ("recent_login_target", "default_days"),
    ("cart_targets", "recent_default_days"),
)


def _path_value(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _validate_config(payload: dict[str, Any], target: Path) -> None:
    missing_sections = [
        name for name in _REQUIRED_MAPPING_SECTIONS
        if not isinstance(payload.get(name), dict) or not payload[name]
    ]
    missing_lists = [
        name for name in _REQUIRED_LIST_SECTIONS
        if not isinstance(payload.get(name), list) or not payload[name]
    ]
    missing_nested_mappings = [
        ".".join(path)
        for path in _REQUIRED_NESTED_MAPPING_PATHS
        if not isinstance(_path_value(payload, path), dict)
        or not _path_value(payload, path)
    ]
    missing_nested_lists = [
        ".".join(path)
        for path in _REQUIRED_NESTED_LIST_PATHS
        if not isinstance(_path_value(payload, path), list)
        or not _path_value(payload, path)
    ]
    missing_values = [
        ".".join(path)
        for path in _REQUIRED_STRING_PATHS
        if not isinstance(_path_value(payload, path), str)
        or not str(_path_value(payload, path)).strip()
    ]
    invalid_numbers = [
        ".".join(path)
        for path in _REQUIRED_POSITIVE_INT_PATHS
        if not isinstance(_path_value(payload, path), int)
        or isinstance(_path_value(payload, path), bool)
        or _path_value(payload, path) <= 0
    ]
    invalid_metrics: list[str] = []
    for section in ("aggregate_targets", "cart_targets", "campaign_response_targets"):
        metrics = _path_value(payload, (section, "metrics"))
        if metrics is None:
            metrics = _path_value(payload, (section, "aggregate_metrics"))
        if not isinstance(metrics, dict):
            continue
        for metric_id, spec in metrics.items():
            path = f"{section}.metrics.{metric_id}"
            if not isinstance(spec, dict):
                invalid_metrics.append(path)
                continue
            if isinstance(spec.get("expression"), str) and spec["expression"].strip():
                continue
            if not all(isinstance(spec.get(key), str) and spec[key].strip() for key in ("agg", "column")):
                invalid_metrics.append(path)
    invalid_axes: list[str] = []
    axes = payload.get("group_ranking_axes")
    if isinstance(axes, dict):
        for axis_id, spec in axes.items():
            if str(axis_id).startswith("_"):
                continue
            if not isinstance(spec, dict) or not all(
                isinstance(spec.get(key), str) and spec[key].strip()
                for key in ("group_expr", "select_alias", "coverage_token", "label")
            ) or not isinstance(spec.get("markers"), list) or not spec["markers"]:
                invalid_axes.append(f"group_ranking_axes.{axis_id}")
    age_band = _path_value(payload, ("group_ranking_axes", "age_group", "age_band"))
    if isinstance(age_band, dict):
        bands = age_band.get("bands")
        if (
            not isinstance(age_band.get("column"), str)
            or not age_band["column"].strip()
            or not isinstance(age_band.get("else_label"), str)
            or not age_band["else_label"].strip()
            or not isinstance(bands, list)
            or not bands
            or any(
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], int)
                or isinstance(entry[0], bool)
                or not isinstance(entry[1], str)
                or not entry[1].strip()
                for entry in bands
            )
        ):
            invalid_axes.append("group_ranking_axes.age_group.age_band")
    invalid_events: list[str] = []
    frequency_events = _path_value(payload, ("campaign_response_targets", "frequency_events"))
    if isinstance(frequency_events, dict):
        for event_id, spec in frequency_events.items():
            if not isinstance(spec, dict) or not all(
                isinstance(spec.get(key), str) and spec[key].strip()
                for key in ("source", "predicate")
            ):
                invalid_events.append(
                    f"campaign_response_targets.frequency_events.{event_id}"
                )
    problems = [
        *missing_sections,
        *missing_lists,
        *missing_nested_mappings,
        *missing_nested_lists,
        *missing_values,
        *invalid_numbers,
        *invalid_metrics,
        *invalid_axes,
        *invalid_events,
    ]
    if problems:
        raise MemberFiltersConfigError(
            "member target filters are incomplete at "
            f"{target}: {', '.join(sorted(set(problems)))}"
        )


def load_config(path: Path | None = None) -> dict[str, Any]:
    """검증 가능한 JSON 객체를 읽는다.

    실행 경로에서는 이 엄격한 로더를 사용한다. 파일 부재·JSON 파손·비객체 payload를 코드
    기본값으로 메우면 구 DB 물리명이 되살아나므로, 원인을 보존한 구성 오류로 끝낸다.
    """

    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MemberFiltersConfigError(
            f"member target filters cannot be read: {target}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise MemberFiltersConfigError(
            f"member target filters contain invalid JSON: {target}"
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise MemberFiltersConfigError(
            f"member target filters must be a non-empty JSON object: {target}"
        )
    _validate_config(payload, target)
    return payload


def _load(path: Path | None = None) -> dict[str, Any]:
    """어휘/선택지 탐색용 fail-close 로더(실행 경로는 ``load_config`` 사용)."""

    try:
        return load_config(path)
    except MemberFiltersConfigError:
        return {}


@lru_cache(maxsize=4)
def _behaviors_cached(path_text: str) -> frozenset[str]:
    section = _load(Path(path_text)).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    return frozenset(behaviors) if isinstance(behaviors, dict) else frozenset()


def order_count_behaviors(path: Path | None = None) -> frozenset[str]:
    """주문 횟수 행동의 캐노니컬 키 집합(설정이 단일 소스).

    설정을 못 읽으면 **빈 집합**을 돌려준다 — 코드 기본값으로 조용히 대체하지 않는다. 폴백이
    있으면 설정 파손이 '조금 다른 분류'로 나타나 눈에 띄지 않기 때문이다. 호출자는 빈 집합을
    보고 자기 정책(폴백/차단)을 정한다.
    """
    return _behaviors_cached(str(Path(path) if path is not None else DEFAULT_PATH))


def behavior_spec(behavior: str, path: Path | None = None) -> dict[str, Any] | None:
    """행동 하나의 설정 선언(없으면 None)."""
    section = _load(path).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    if not isinstance(behaviors, dict):
        return None
    spec = behaviors.get(behavior)
    return spec if isinstance(spec, dict) else None


@lru_cache(maxsize=4)
def _aggregate_metrics_cached(path_text: str) -> tuple[tuple[str, str], ...]:
    """(metric_id, spec_json) 튜플 — lru_cache 는 해시 가능 값만 담으므로 직렬화해 캐시한다."""
    section = _load(Path(path_text)).get("aggregate_targets") or {}
    metrics = section.get("metrics") if isinstance(section, dict) else None
    if not isinstance(metrics, dict):
        return ()
    return tuple(
        (metric_id, json.dumps(spec, ensure_ascii=False, sort_keys=True))
        for metric_id, spec in metrics.items()
        if isinstance(metric_id, str) and isinstance(spec, dict)
    )


def aggregate_metrics(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """집계 지표 스펙(aggregate_targets.metrics)의 metric_id → 선언 사본.

    concept_catalog 가 공통 조건 개념을 파생하는 소스다 — 지표·동의어를 카탈로그에 다시
    나열하지 않는다(이중 소유 금지). 설정을 못 읽으면 빈 dict(폴백 없음, order_count_behaviors
    와 같은 정책)."""
    target = str(Path(path) if path is not None else DEFAULT_PATH)
    return {metric_id: json.loads(spec) for metric_id, spec in _aggregate_metrics_cached(target)}


def campaign_response_targets(path: Path | None = None) -> dict[str, Any]:
    """캠페인 반응 집계 SQL 자산 선언의 방어적 사본.

    SemanticPlan 생산자는 이 접근자로 물리 집계·대상군·구매반응 조건이 모두
    존재하는지 확인한다. 설정을 읽지 못하거나 섹션이 불완전하면 빈 dict를 돌려
    주고, 호출자는 합성을 중단한다. 코드 기본값으로 조용히 메우지 않는다.
    """

    section = _load(path).get("campaign_response_targets")
    return section if isinstance(section, dict) else {}


@lru_cache(maxsize=4)
def _eq_filters_cached(path_text: str) -> str:
    entries = _load(Path(path_text)).get("eq_filters")
    return json.dumps(entries if isinstance(entries, list) else [], ensure_ascii=False)


def eq_filters(path: Path | None = None) -> list[dict[str, Any]]:
    """회원 속성 값 사전(eq_filters)의 사본.

    등급 서열·상태 값 어휘의 **단일 소유자**다. 같은 낱말(VIP/골드/휴면…)이 여러 모듈의
    정규식에 재등장하던 이중 소유를 여기서 파생으로 대체한다 — 값이 늘면 설정 한 줄로
    모든 소비자가 함께 열린다. 설정을 못 읽으면 빈 목록(폴백 없음).
    """
    target = str(Path(path) if path is not None else DEFAULT_PATH)
    return [entry for entry in json.loads(_eq_filters_cached(target)) if isinstance(entry, dict)]


def eq_filter_values(category: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """한 범주(grade/state/gender…)의 canonical → {value, rank, synonyms}."""
    values: dict[str, dict[str, Any]] = {}
    for entry in eq_filters(path):
        if str(entry.get("category") or "") != category:
            continue
        canonical = str(entry.get("canonical") or "")
        if not canonical:
            continue
        values[canonical] = {
            "value": entry.get("value"),
            "rank": entry.get("rank"),
            "synonyms": [str(term) for term in entry.get("synonyms") or [] if str(term).strip()],
        }
    return values


def order_count_rule_supported(rule: Any) -> bool:
    """주문 횟수 행동 규칙이 컴파일 가능한 완전한 선언인가.

    ``_supported: false`` 선언(lapsed_buyer)이나 operator/count 도 anti_join 도 없는 불완전 규칙을
    ``.get(..., "=")/.get(..., 1)`` 폴백으로 조용히 '첫 구매'로 컴파일하던 잠복 결함의 차단 술어다.
    빌더는 이 판정이 거짓인 행동을 선택하지 않는다(fail-close — 미지원 행동은 부분추출 고지로 남는다).
    """
    if not isinstance(rule, dict) or rule.get("_supported") is False:
        return False
    if rule.get("anti_join") is True:
        return True
    return isinstance(rule.get("operator"), str) and isinstance(rule.get("count"), int)


def behavior_aggregate_equivalents(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """집계 지표와 등가인 행동 선언(behavior → {metric_id, operator, count}).

    설정의 ``behaviors.*.metric_id`` 가 단일 소스다(코드에 behavior→metric 매핑을 박으면 이중 소유
    재발). anti_join(부재 조건)·미지원(_supported:false)·불완전 규칙은 등가가 아니다 — 부재 조건을
    집계 임계값으로 오인해 강등하면 극성이 반전되므로 여기서부터 제외한다.
    """
    section = _load(path).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    if not isinstance(behaviors, dict):
        return {}
    equivalents: dict[str, dict[str, Any]] = {}
    for behavior, rule in behaviors.items():
        if not order_count_rule_supported(rule) or rule.get("anti_join") is True:
            continue
        metric_id = rule.get("metric_id")
        if isinstance(metric_id, str) and metric_id:
            equivalents[behavior] = {
                "metric_id": metric_id,
                "operator": rule["operator"],
                "count": rule["count"],
            }
    return equivalents


def clear_cache() -> None:
    """설정 파일을 바꿔 끼우는 테스트용."""
    _behaviors_cached.cache_clear()
    _aggregate_metrics_cached.cache_clear()
    _eq_filters_cached.cache_clear()


__all__ = [
    "DEFAULT_PATH",
    "MemberFiltersConfigError",
    "aggregate_metrics",
    "behavior_aggregate_equivalents",
    "behavior_spec",
    "campaign_response_targets",
    "clear_cache",
    "eq_filter_values",
    "eq_filters",
    "load_config",
    "order_count_behaviors",
    "order_count_rule_supported",
]
