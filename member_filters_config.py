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
상대경로면 cwd 가 바뀌는 순간 조용히 빈 설정으로 강등되기 때문이다.
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


def _load(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    "aggregate_metrics",
    "behavior_aggregate_equivalents",
    "behavior_spec",
    "clear_cache",
    "eq_filter_values",
    "eq_filters",
    "order_count_behaviors",
    "order_count_rule_supported",
]
