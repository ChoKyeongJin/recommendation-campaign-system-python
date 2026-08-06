"""모호한 표현의 기본 해석값 카탈로그를 **읽는 쪽** — 표면어 하나를 관측 창 하나로 바꾼다.

``docs/data/runtime/policies/qualitative_defaults.sample.json`` 은 '요즘', '최근 한 달'처럼
수치가 없는 표현의 기본값을 236항목 적어 둔 참조 자산이다. 이 모듈은 그중 **오디언스 조건의
롤링 창으로 쓸 수 있는 항목만** 골라 조회 가능한 형태로 만든다. 고르는 기준은 둘이고 둘 다
:mod:`event_ir` 이 정한다 — 여기서 두 번째 기간 문법을 만들면 카탈로그가 IR 과 다른 말을 한다.

* ``kind == "relative_window"`` 이고 ``direction == "past"`` 인 항목만. '당분간'(future)은
  오디언스 관측 창이 아니고, '단기간'(duration)은 창이 아니라 길이이며, '오늘'·'지난주'는
  달력 경계라 ``RollingWindow`` 가 아니다.
* 단위가 ``event_ir.WINDOW_UNITS`` 안에 있을 것. 그래서 **'방금 = 최근 1시간'은 제외된다**
  — ``RollingWindow`` 는 hour 를 받지 않는다(``WINDOW_UNITS`` 에 없다). 카탈로그에 적혀
  있다는 것과 창으로 쓸 수 있다는 것은 다르고, 그 차이를 여기서 조용히 메우지 않는다.

**나머지 12개 그룹을 내보내지 않는 이유는 소유자가 이미 있기 때문이다.** 'VIP 고객'·'우수
고객'·'신규 고객'은 :file:`docs/data/runtime/language/normalization_rules.sample.json` 이
회원 **등급 코드**로 매핑한다(실 CRM 에 등급 컬럼이 실재한다). 카탈로그의 행동 기반 정의
('VIP = 최근 180일 구매액 상위 5%')를 여기서 같이 내보내면 같은 표면어에 두 해석이 생긴다.
발송 빈도·예산·디자인 그룹은 애초에 타겟 SQL 이 표현할 수 있는 대상이 아니다.

정책은 **기본으로 꺼져 있다**(:data:`ENABLED_ENV`). 켜지 않은 배포에서 기간 없는 '최근'은
구조화기의 fail-close 가 그대로 결말이다 — 되묻기와 기본값 중 무엇이 옳은지는 원문이 아니라
제품 설정이 정한다는 :mod:`default_period_policy` 의 계층 결정을 그대로 따른다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import event_ir

# 카탈로그 경로(배포가 실데이터본으로 교체할 수 있다).
CATALOG_PATH_ENV = "QUALITATIVE_DEFAULTS_PATH"
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "docs"
    / "data"
    / "runtime"
    / "policies"
    / "qualitative_defaults.sample.json"
)
# 이 정책을 켜는 스위치. 미설정이면 조회 결과가 항상 비어 fail-close 가 유지된다.
ENABLED_ENV = "AUDIENCE_QUALITATIVE_DEFAULTS"
# 창으로 쓸 수 있는 항목을 고르는 그룹·모양.
RECENCY_GROUP_ID = "time_expression"
_RECENCY_KIND = "relative_window"
_RECENCY_DIRECTION = "past"

# 카탈로그가 쓰는 ISO-8601 duration 중 이 모듈이 해석하는 모양(P5D, P2W, PT1H …).
_ISO_DURATION_RE = re.compile(r"^P(?:(?P<count>\d+)(?P<date_unit>[DWMY])|T(?P<hours>\d+)H)$")
_ISO_DATE_UNITS = {"D": "day", "W": "week", "M": "month", "Y": "year"}


@dataclass(frozen=True, slots=True)
class RecencyDefault:
    """표면어 하나와 그 표현이 뜻하는 롤링 창."""

    expression: str
    canonical: str
    value: int
    unit: str

    @property
    def key(self) -> tuple[int, str]:
        return (self.value, self.unit)


def catalog_enabled(raw: str | None = None) -> bool:
    """이 배포가 카탈로그 기본값을 켰는가(미설정이면 꺼짐)."""

    value = os.getenv(ENABLED_ENV, "") if raw is None else raw
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def catalog_path(path: Path | str | None = None) -> Path:
    """읽을 카탈로그 경로. 인자 > 환경변수 > 저장소 기본본 순."""

    if path is not None:
        return Path(path)
    configured = os.getenv(CATALOG_PATH_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CATALOG_PATH


@lru_cache(maxsize=8)
def _load(resolved: str) -> dict[str, Any]:
    payload = json.loads(Path(resolved).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"qualitative defaults catalog must be an object: {resolved}")
    return payload


def load_catalog(path: Path | str | None = None) -> dict[str, Any]:
    """카탈로그 전체를 읽는다(경로 기준 캐시). 파일이 없으면 빈 카탈로그."""

    target = catalog_path(path)
    if not target.is_file():
        return {"groups": []}
    return _load(str(target.resolve()))


def _duration_to_window(duration: Any) -> tuple[int, str] | None:
    """ISO duration 을 ``RollingWindow`` 가 받는 (값, 단위)로. 창이 될 수 없으면 ``None``."""

    if not isinstance(duration, str):
        return None
    match = _ISO_DURATION_RE.match(duration.strip())
    if match is None:
        return None
    if match.group("hours") is not None:
        # 시간 단위 창은 event_ir 이 지원하지 않는다('방금'). 근사하지 않고 버린다.
        return None
    unit = event_ir.canonical_unit(_ISO_DATE_UNITS[match.group("date_unit")])
    if unit is None or unit not in event_ir.WINDOW_UNITS:
        return None
    value = int(match.group("count"))
    return (value, str(unit)) if value > 0 else None


def recency_defaults(path: Path | str | None = None) -> tuple[RecencyDefault, ...]:
    """창으로 쓸 수 있는 기간 표현들. **표면어가 긴 순**으로 준다.

    순서가 계약이다 — '최근 한 달'은 '최근'을 포함하므로, 짧은 쪽이 먼저 걸리면 30일이어야 할
    문장이 5일이 된다.
    """

    catalog = load_catalog(path)
    groups = catalog.get("groups")
    if not isinstance(groups, list):
        return ()

    found: list[RecencyDefault] = []
    for group in groups:
        if not isinstance(group, dict) or group.get("group_id") != RECENCY_GROUP_ID:
            continue
        entries = group.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            default = entry.get("default")
            if not isinstance(default, dict):
                continue
            if default.get("kind") != _RECENCY_KIND:
                continue
            if default.get("direction") != _RECENCY_DIRECTION:
                continue
            window = _duration_to_window(default.get("duration"))
            expression = entry.get("expression")
            canonical = entry.get("canonical")
            if window is None or not isinstance(expression, str) or not isinstance(canonical, str):
                continue
            found.append(
                RecencyDefault(
                    expression=expression,
                    canonical=canonical,
                    value=window[0],
                    unit=window[1],
                )
            )
    return tuple(sorted(found, key=lambda item: (-len(item.expression), item.expression)))


def resolve_recency_marker(
    text: str, path: Path | str | None = None, *, enabled: bool | None = None
) -> RecencyDefault | None:
    """문장 조각에서 기간 표현을 찾아 그 표현의 기본 창을 준다.

    정책이 꺼져 있으면 언제나 ``None`` 이다. 찾지 못해도 ``None`` 이고, 비슷해 보이는 항목으로
    대신하지 않는다 — 그 경우의 결말은 카탈로그가 아니라 호출 계층이 정한다.
    """

    if not (catalog_enabled() if enabled is None else enabled):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    for candidate in recency_defaults(path):
        if candidate.expression in text:
            return candidate
    return None


def reset_cache() -> None:
    """카탈로그 캐시를 비운다(테스트에서 다른 카탈로그를 읽힐 때만 쓴다)."""

    _load.cache_clear()


__all__ = [
    "CATALOG_PATH_ENV",
    "DEFAULT_CATALOG_PATH",
    "ENABLED_ENV",
    "RECENCY_GROUP_ID",
    "RecencyDefault",
    "catalog_enabled",
    "catalog_path",
    "load_catalog",
    "recency_defaults",
    "reset_cache",
    "resolve_recency_marker",
]
