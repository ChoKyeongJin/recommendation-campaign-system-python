"""Resolution 정책의 **선언** — 코드에 박힌 숫자가 아니라 설정이 정한다.

이 파일이 답하는 질문은 둘이다.

1. 어떤 결핍을 정책이 대신 채워도 되는가(``allowed_auto_resolution``).
2. 어떤 결핍은 채우면 안 되고 반드시 물어야 하는가(``require_clarification``).

값 자체(예: 기간 없는 '최근'을 며칠로 읽는가)는 **여기에 적지 않는다**. 그 값은 이미
:mod:`default_period_policy` 가 소유하고 있고(배포 env · 표현별 카탈로그), 같은 사실을 두 곳에
적으면 둘이 갈리는 순간 "구조화기가 채운 창"과 "정책이 인정하는 창"이 다른 말을 한다 —
저장소가 실측으로 다친 자리다. 그래서 이 설정은 **판정만** 선언하고 값은 소유자에게 묻는다.

모드(:class:`ResolutionMode`)는 위험도별 자동 확정 허용선이다.

    STRICT          자동 확정 없음 — 결핍은 전부 되묻기
    SAFE_DEFAULTS   LOW 만 자동(선언된 운영 기본값)
    BEST_EFFORT     LOW + MEDIUM 자동

HIGH 는 어떤 모드에서도 자동으로 사라지지 않는다(§34-H). row/subject grain, AND/OR, 부재
의미, 엔터티 식별자가 여기 속한다 — 잘못 고르면 대상 집합이 통째로 달라지기 때문이다.

설정을 읽지 못하면 **STRICT 로 닫는다**. 오타 하나가 조용히 기본값 적용으로 흐르면 이 계층이
막으려던 바로 그 상태가 된다.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from resolution.issues import (
    ISSUE_KIND_SPECS,
    IssueFamily,
    ResolutionRisk,
    UnknownIssueKindError,
    issue_kind_spec,
)

logger = logging.getLogger("resolution.config")

#: 설정 파일 경로(배포가 교체할 수 있다).
CONFIG_PATH_ENV = "RESOLUTION_POLICY_PATH"
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "data"
    / "runtime"
    / "policies"
    / "resolution_policy.sample.json"
)
#: 배포가 모드를 덮어쓰는 스위치(설정 파일의 ``mode`` 보다 우선한다).
MODE_ENV = "AUDIENCE_RESOLUTION_MODE"


class ResolutionMode(StrEnum):
    STRICT = "strict"
    SAFE_DEFAULTS = "safe_defaults"
    BEST_EFFORT = "best_effort"


#: 모드별 자동 확정 허용 위험도. HIGH 는 어느 모드에도 없다 — 그것이 §34-H 의 구조적 보장이다.
MODE_AUTO_RISKS: dict[ResolutionMode, frozenset[ResolutionRisk]] = {
    ResolutionMode.STRICT: frozenset(),
    ResolutionMode.SAFE_DEFAULTS: frozenset({ResolutionRisk.LOW}),
    ResolutionMode.BEST_EFFORT: frozenset({ResolutionRisk.LOW, ResolutionRisk.MEDIUM}),
}


class ResolutionConfigError(ValueError):
    """설정이 읽히지 않거나 선언되지 않은 이름을 쓴다. 조용한 기본값으로 흐르지 않는다."""


@dataclass(frozen=True, slots=True)
class EntityArgument:
    """결핍 보고의 ``argument`` 하나가 가리키는 엔터티 종류와 사람이 읽는 이름."""

    entity_type: str
    label: str


@dataclass(frozen=True, slots=True)
class ResolutionPolicyConfig:
    """판정 선언 한 벌."""

    mode: ResolutionMode = ResolutionMode.SAFE_DEFAULTS
    allowed_auto_resolution: Mapping[str, bool] = field(default_factory=dict)
    require_clarification: Mapping[str, bool] = field(default_factory=dict)
    #: 결핍 보고의 argument 이름 → 엔터티 선언. 여기 있는 argument 만 '값을 물어야 하는 것'이 된다.
    entity_arguments: Mapping[str, EntityArgument] = field(default_factory=dict)
    #: 한 응답에 실을 질문 수 상한. 0 이면 제한 없음.
    max_questions: int = 0
    source: str = "builtin"

    def auto_resolution_allowed(self, kind: str) -> bool:
        return bool(self.allowed_auto_resolution.get(kind, False))

    def clarification_required(self, kind: str) -> bool:
        return bool(self.require_clarification.get(kind, False))

    def entity_argument(self, argument: str) -> EntityArgument | None:
        return self.entity_arguments.get(argument)

    def with_mode(self, mode: ResolutionMode) -> ResolutionPolicyConfig:
        from dataclasses import replace

        return replace(self, mode=mode)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "allowed_auto_resolution": dict(self.allowed_auto_resolution),
            "require_clarification": dict(self.require_clarification),
            "entity_arguments": {
                name: {"entity_type": item.entity_type, "label": item.label}
                for name, item in self.entity_arguments.items()
            },
            "max_questions": self.max_questions,
            "source": self.source,
        }


#: 설정을 읽지 못했을 때의 귀결. 자동 확정이 하나도 없으므로 없는 값을 지어내지 않는다.
STRICT_FALLBACK_CONFIG = ResolutionPolicyConfig(
    mode=ResolutionMode.STRICT, source="strict_fallback"
)


def config_path(path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CONFIG_PATH


def _mode(raw: Any, *, origin: str) -> ResolutionMode:
    if raw is None:
        return ResolutionMode.SAFE_DEFAULTS
    try:
        return ResolutionMode(str(raw).strip().casefold())
    except ValueError as exc:
        raise ResolutionConfigError(
            f"{origin}: 선언되지 않은 resolution mode 입니다: {raw!r} "
            f"(가능한 값: {', '.join(sorted(item.value for item in ResolutionMode))})"
        ) from exc


def _flag_table(raw: Any, *, origin: str, field_name: str) -> dict[str, bool]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ResolutionConfigError(f"{origin}: {field_name} 은 객체여야 합니다")
    table: dict[str, bool] = {}
    for key, value in raw.items():
        # 선언되지 않은 kind 를 조용히 무시하면 설정 오타가 '정책 없음'으로 보인다.
        try:
            issue_kind_spec(str(key))
        except UnknownIssueKindError as exc:
            raise ResolutionConfigError(f"{origin}: {field_name}.{key} — {exc}") from exc
        if not isinstance(value, bool):
            raise ResolutionConfigError(
                f"{origin}: {field_name}.{key} 는 boolean 이어야 합니다(받은 값 {value!r})"
            )
        table[str(key)] = value
    return table


def _entity_arguments(raw: Any, *, origin: str) -> dict[str, EntityArgument]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ResolutionConfigError(f"{origin}: entity_arguments 는 객체여야 합니다")
    table: dict[str, EntityArgument] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            raise ResolutionConfigError(
                f"{origin}: entity_arguments.{key} 는 객체여야 합니다"
            )
        entity_type = value.get("entity_type")
        label = value.get("label")
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise ResolutionConfigError(
                f"{origin}: entity_arguments.{key}.entity_type 이 비었습니다"
            )
        if not isinstance(label, str) or not label.strip():
            raise ResolutionConfigError(
                f"{origin}: entity_arguments.{key}.label 이 비었습니다"
            )
        table[str(key)] = EntityArgument(entity_type=entity_type, label=label)
    return table


def parse_config(payload: Any, *, origin: str) -> ResolutionPolicyConfig:
    """JSON 객체 하나를 판정 선언으로. 선언되지 않은 이름은 오류다."""

    if not isinstance(payload, Mapping):
        raise ResolutionConfigError(f"{origin}: resolution policy 는 객체여야 합니다")
    unexpected = sorted(
        set(payload)
        - {
            "version",
            "mode",
            "allowed_auto_resolution",
            "require_clarification",
            "entity_arguments",
            "max_questions",
            "description",
        }
    )
    if unexpected:
        raise ResolutionConfigError(
            f"{origin}: 선언되지 않은 키가 있습니다: {', '.join(unexpected)}"
        )
    max_questions = payload.get("max_questions", 0)
    if not isinstance(max_questions, int) or isinstance(max_questions, bool) or max_questions < 0:
        raise ResolutionConfigError(f"{origin}: max_questions 는 0 이상의 정수여야 합니다")

    allowed = _flag_table(
        payload.get("allowed_auto_resolution"),
        origin=origin,
        field_name="allowed_auto_resolution",
    )
    # 미지원 계열은 애초에 채울 수 있는 값이 아니다. 설정으로 열리면 §25 가 깨진다.
    for kind, enabled in allowed.items():
        if enabled and issue_kind_spec(kind).family is IssueFamily.UNSUPPORTED:
            raise ResolutionConfigError(
                f"{origin}: {kind} 는 미지원 계열이라 자동 확정할 수 없습니다"
            )
    return ResolutionPolicyConfig(
        mode=_mode(payload.get("mode"), origin=origin),
        allowed_auto_resolution=allowed,
        require_clarification=_flag_table(
            payload.get("require_clarification"),
            origin=origin,
            field_name="require_clarification",
        ),
        entity_arguments=_entity_arguments(payload.get("entity_arguments"), origin=origin),
        max_questions=max_questions,
        source=origin,
    )


@lru_cache(maxsize=8)
def _load(resolved: str) -> ResolutionPolicyConfig:
    payload = json.loads(Path(resolved).read_text(encoding="utf-8"))
    return parse_config(payload, origin=resolved)


def load_config(path: Path | str | None = None) -> ResolutionPolicyConfig:
    """설정을 읽는다. 파일이 없거나 잘못되면 :class:`ResolutionConfigError`."""

    target = config_path(path)
    if not target.is_file():
        raise ResolutionConfigError(f"resolution policy 설정을 찾지 못했습니다: {target}")
    return _load(str(target.resolve()))


def resolved_config(path: Path | str | None = None) -> ResolutionPolicyConfig:
    """운영 경로가 쓰는 설정. 읽지 못하면 **STRICT** 로 닫고 시끄럽게 기록한다.

    자동 확정을 전부 끄는 방향이라, 설정 사고가 "없는 값을 지어냄"으로는 절대 이어지지 않는다.
    """

    try:
        config = load_config(path)
    except (ResolutionConfigError, OSError, json.JSONDecodeError) as exc:
        logger.error(
            "resolution_policy_config_unreadable error=%s:%s",
            exc.__class__.__name__,
            exc,
        )
        return STRICT_FALLBACK_CONFIG
    override = os.getenv(MODE_ENV, "").strip()
    if not override:
        return config
    try:
        return config.with_mode(_mode(override, origin=MODE_ENV))
    except ResolutionConfigError as exc:
        logger.error("resolution_policy_mode_invalid error=%s", exc)
        return config.with_mode(ResolutionMode.STRICT)


def reset_cache() -> None:
    """설정 캐시를 비운다(테스트에서 다른 설정을 읽힐 때만 쓴다)."""

    _load.cache_clear()


def declared_kinds() -> tuple[str, ...]:
    return tuple(sorted(ISSUE_KIND_SPECS))


__all__ = [
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_PATH",
    "MODE_AUTO_RISKS",
    "MODE_ENV",
    "STRICT_FALLBACK_CONFIG",
    "EntityArgument",
    "ResolutionConfigError",
    "ResolutionMode",
    "ResolutionPolicyConfig",
    "config_path",
    "declared_kinds",
    "load_config",
    "parse_config",
    "reset_cache",
    "resolved_config",
]
