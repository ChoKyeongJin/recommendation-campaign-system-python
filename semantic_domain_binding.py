"""도메인 플러그인 바인딩 — 범용 코어가 도메인 지식을 **이름으로만** 참조하는 지점.

코어 모듈(semantic_normalizers / temporal_semantics)은 도메인 값을 하드코딩하지도,
도메인 모듈을 직접 import 하지도 않는다. 대신 여기서 얻는다:

    binding.vocabulary("<어휘 키>")          → 그 어휘의 닫힌 값 목록      [도메인 선언]
    binding.plan_container("member_condition") → 실행 플랜 컨테이너 이름   [도메인 선언]

플러그인 모듈 이름은 **설정값**이다(`SEMANTIC_DOMAIN_PLUGIN`, 기본 `targeting_domain`).
코드 안의 분기가 아니라 조립 지점의 선택이므로, 다른 도메인을 얹을 때 코어는 그대로다.

선언이 없거나 플러그인이 없으면 **제약 없음**으로 강등한다(빈 어휘 = enum 미노출).
조용한 오답이 아니라 '느슨한 검증'이 되는 방향이며, 실제 부재는 드리프트 가드가 잡는다.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Mapping

DOMAIN_PLUGIN_ENV = "SEMANTIC_DOMAIN_PLUGIN"
DEFAULT_DOMAIN_PLUGIN = "targeting_domain"

_PLUGIN: Any | None = None
_RESOLVED_NAME: str | None = None


def plugin_name() -> str:
    return os.getenv(DOMAIN_PLUGIN_ENV) or DEFAULT_DOMAIN_PLUGIN


def plugin() -> Any | None:
    """설정된 도메인 플러그인 모듈(없으면 None — 코어는 제약 없이 동작한다)."""
    global _PLUGIN, _RESOLVED_NAME
    name = plugin_name()
    if _PLUGIN is not None and _RESOLVED_NAME == name:
        return _PLUGIN
    try:
        module = importlib.import_module(name)
    except ImportError:
        _PLUGIN, _RESOLVED_NAME = None, name
        return None
    _PLUGIN, _RESOLVED_NAME = module, name
    return module


def reset() -> None:
    """테스트에서 플러그인을 갈아 끼울 때."""
    global _PLUGIN, _RESOLVED_NAME
    _PLUGIN = _RESOLVED_NAME = None


def _call(attribute: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    module = plugin()
    getter: Callable[..., Any] | None = getattr(module, attribute, None) if module else None
    if getter is None:
        return default
    try:
        value = getter(*args, **kwargs)
    except Exception:  # noqa: BLE001 — 도메인 선언 파손이 코어를 죽이면 안 된다.
        return default
    return default if value is None else value


# ── 코어가 쓰는 조회들 ───────────────────────────────────────────────────────────
def vocabulary(name: str) -> tuple[str, ...]:
    """노드 필드의 닫힌 값 목록(선언 없으면 빈 튜플 = 제약 없음)."""
    values = _call("vocabulary", name, default=())
    return tuple(str(item) for item in values) if isinstance(values, (list, tuple)) else ()


def vocabulary_glossary(name: str) -> dict[str, str]:
    """어휘 값의 짧은 설명(LLM 스키마 description 파생용). 선언 없으면 빈 dict."""
    values = _call("vocabulary_glossary", name, default={})
    return {str(k): str(v) for k, v in values.items()} if isinstance(values, Mapping) else {}


def plan_container(kind: str = "member_condition") -> str | None:
    value = _call("plan_container", kind, default=None)
    return str(value) if isinstance(value, str) and value else None


def capability_axes() -> tuple[tuple[str, str], ...]:
    axes = _call("capability_axes", default=())
    return tuple(
        (str(key), str(field))
        for key, field in axes
        if isinstance(key, str) and isinstance(field, str)
    ) if isinstance(axes, (list, tuple)) else ()


def subject_fields() -> tuple[str, ...]:
    values = _call("subject_fields", default=())
    return tuple(str(item) for item in values) if isinstance(values, (list, tuple)) else ()


def identity_fields() -> tuple[str, ...]:
    values = _call("identity_fields", default=())
    return tuple(str(item) for item in values) if isinstance(values, (list, tuple)) else ()


def entity_aliases() -> dict[str, str]:
    values = _call("entity_aliases", default={})
    return {str(k): str(v) for k, v in values.items()} if isinstance(values, Mapping) else {}


def counter_units() -> dict[str, str]:
    values = _call("counter_units", default={})
    return {str(k): str(v) for k, v in values.items()} if isinstance(values, Mapping) else {}


def bind_counter_unit(
    surface_unit: str,
    *,
    text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> str | None:
    """도메인 플러그인에 문맥 있는 계수 단위 결속을 위임한다."""

    value = _call(
        "bind_counter_unit",
        surface_unit,
        text=text,
        start=start,
        end=end,
        default=None,
    )
    return str(value) if isinstance(value, str) and value else None


def temporal_aliases() -> dict[str, str]:
    values = _call("temporal_relation_aliases", default={})
    return {str(k): str(v) for k, v in values.items()} if isinstance(values, Mapping) else {}


def condition_label(node_type: str) -> str:
    value = _call("condition_label", node_type, default=node_type)
    return str(value)


def user_omission_reason(text: str) -> dict[str, str] | None:
    """이 구절의 결핍이 **사용자 정보 누락**인가(맞으면 물어볼 질문 포함).

    코어는 '무엇이 자리표시자인지'를 모른다 — 도메인만 안다. 이 판정이 없으면 사용자가
    답할 수 있는 유일한 결핍까지 구조화기 실패로 뭉개진다.
    """
    value = _call("user_omission_reason", text, default=None)
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, Mapping) else None


def node_field_bindings() -> dict[str, Any]:
    """'노드타입.필드 → 닫힌 어휘' 결속 선언(정규화 계층의 타입 판정 입력).

    코어는 이 선언을 **양방향**으로 쓴다: 값 검증(metric 이 이 scope 의 어휘에 있는가)과
    역인덱스(이 metric 이면 scope 는 무엇인가). 둘 다 같은 선언 하나에서 나온다.
    """
    values = _call("node_field_vocabularies", default={})
    return dict(values) if isinstance(values, Mapping) else {}


def temporal_operator_of(node: Any) -> str | None:
    """노드의 시간 축을 **범용 연산자**로 읽는다(없으면 None = 시간 축 아님).

    `temporal` 필드가 있으면 그것을, 없으면 도메인 관계명을 별칭 표로 해소한다.
    """
    import temporal_semantics  # 코어끼리라 순환 없음

    values = getattr(node, "values", None)
    raw = None
    if isinstance(values, Mapping):
        raw = values.get("temporal") or values.get("relation")
    elif node is not None and not hasattr(node, "values"):
        raw = node
    try:
        qualifier = temporal_semantics.normalize(raw, aliases=temporal_aliases())
    except temporal_semantics.TemporalSemanticsError:
        return None
    return qualifier.operator if qualifier else None


def temporal_subinterval_unit() -> str:
    """하위 구간 연산자(EVERY_SUBINTERVAL)의 기본 단위 — 데이터 그레인이 정하는 값."""
    value = _call("temporal_subinterval_unit", default=None)
    return str(value) if isinstance(value, str) and value else "month"


def execution_operator(operator: str, *, anchored: bool = False) -> str | None:
    """범용 시간 연산자 → 실행 컴파일러의 연산자 이름(도메인 선언)."""
    value = _call("execution_operator", operator, anchored=anchored, default=None)
    return str(value) if isinstance(value, str) and value else None


__all__ = [
    "DEFAULT_DOMAIN_PLUGIN",
    "DOMAIN_PLUGIN_ENV",
    "capability_axes",
    "bind_counter_unit",
    "condition_label",
    "counter_units",
    "entity_aliases",
    "execution_operator",
    "identity_fields",
    "node_field_bindings",
    "plan_container",
    "plugin",
    "plugin_name",
    "reset",
    "subject_fields",
    "temporal_aliases",
    "temporal_operator_of",
    "temporal_subinterval_unit",
    "user_omission_reason",
    "vocabulary",
    "vocabulary_glossary",
]
