"""Shared member-universe policy used by targeting and analytical queries.

The physical state column and stored values belong to
``member_target_filters.json``.  Keeping the lookup here prevents analytical
queries and audience builders from drifting to different member populations.
"""

from __future__ import annotations

import sql_dialect

import functools
import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_MEMBER_POLICY_PATH = Path(
    os.getenv(
        "GRAPH_RAG_MEMBER_TARGET_FILTERS",
        "docs/data/runtime/sql/member_target_filters.json",
    )
)

_ALL_MEMBER_RE = re.compile(r"(?:전체|모든|전부)\s*(?:회원|고객|사용자|가입자)")
_INCLUDE_DORMANT_RE = re.compile(r"휴면\s*(?:회원\s*)?(?:도\s*)?(?:포함|포괄)")
_INCLUDE_WITHDRAWN_RE = re.compile(r"탈퇴\s*(?:회원\s*)?(?:도\s*)?(?:포함|포괄)")


@functools.lru_cache(maxsize=4)
def load_member_policy(path_text: str = str(DEFAULT_MEMBER_POLICY_PATH)) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def active_member_definition(path: Path = DEFAULT_MEMBER_POLICY_PATH) -> dict[str, str]:
    """Return the configured base table, alias, state column, and normal value."""
    config = load_member_policy(str(path))
    base = config.get("base_entity") if isinstance(config.get("base_entity"), dict) else {}
    state = config.get("active_state") if isinstance(config.get("active_state"), dict) else {}
    return {
        "table": str(base.get("table") or "CRM_MB_BASEINFO"),
        "alias": str(base.get("alias") or "B"),
        "column": str(state.get("column") or "MEMBER_STATE_CD").split(".")[-1],
        "value": str(state.get("value") or "MEMBER_STATE_CD.NORMAL"),
    }


def _state_value(canonical: str, path: Path = DEFAULT_MEMBER_POLICY_PATH) -> str | None:
    config = load_member_policy(str(path))
    for item in config.get("eq_filters", []) or []:
        if (
            isinstance(item, dict)
            and item.get("category") == "state"
            and item.get("canonical") == canonical
            and item.get("value")
        ):
            return str(item["value"])
    return None


def member_eq_filter(canonical: str, path: Path = DEFAULT_MEMBER_POLICY_PATH) -> dict[str, str] | None:
    """Return the physical mapping of one ``eq_filters`` canonical.

    Analytical aggregates and audience builders must agree on which column and
    which stored value mean ``여성``/``VIP``/``앱 로그인``.  Both read this one
    registry entry instead of repeating the literal, so a code value change
    cannot leave the count and the list describing different populations.
    """
    config = load_member_policy(str(path))
    base = config.get("base_entity") if isinstance(config.get("base_entity"), dict) else {}
    table = str(base.get("table") or "CRM_MB_BASEINFO")
    for item in config.get("eq_filters", []) or []:
        if not isinstance(item, dict) or item.get("canonical") != canonical:
            continue
        column = str(item.get("column") or "")
        if not column or item.get("value") is None:
            return None
        return {
            "category": str(item.get("category") or ""),
            "table": table,
            "column": column.split(".")[-1],
            "value": str(item["value"]),
        }
    return None


def member_activity_filter(canonical: str, path: Path = DEFAULT_MEMBER_POLICY_PATH) -> dict[str, Any] | None:
    """Return the physical definition of one ``activity_filters`` canonical.

    Inactivity and recency conditions (``휴면``/``최근 접속``) are date predicates
    with a configured window, comparison direction, and NULL handling.  Analytical
    counts read the same definition the audience builders use.
    """
    config = load_member_policy(str(path))
    base = config.get("base_entity") if isinstance(config.get("base_entity"), dict) else {}
    for item in config.get("activity_filters", []) or []:
        if not isinstance(item, dict) or item.get("canonical") != canonical:
            continue
        days = item.get("days") if isinstance(item.get("days"), int) else item.get("default_days")
        return {
            "table": str(base.get("table") or "CRM_MB_BASEINFO"),
            "column": str(item.get("column") or "LAST_LOGIN_DATE").split(".")[-1],
            "operator": str(item.get("operator") or "<"),
            "days": days if isinstance(days, int) else None,
            "include_null": bool(item.get("include_null")),
        }
    return None


def member_condition_canonicals(path: Path = DEFAULT_MEMBER_POLICY_PATH) -> dict[str, dict[str, Any]]:
    """모든 회원 조건 canonical 의 (범주, 설명 표면어). 닫힌 어휘가 필요한 소비자용.

    LLM 에게 자유 서술 대신 이 어휘만 고르게 하면, 존재하지 않는 컬럼·값을 지어낼 여지가 사라진다.
    """
    config = load_member_policy(str(path))
    catalog: dict[str, dict[str, Any]] = {}
    for item in config.get("eq_filters", []) or []:
        if not isinstance(item, dict) or not item.get("canonical"):
            continue
        catalog[str(item["canonical"])] = {
            "category": str(item.get("category") or ""),
            "terms": [str(term) for term in (item.get("synonyms") or []) + (item.get("surface_terms") or [])],
        }
    for item in config.get("activity_filters", []) or []:
        if not isinstance(item, dict) or not item.get("canonical"):
            continue
        if not isinstance(item.get("days"), int):
            continue  # 파라미터형(recent_login)은 기간 인자가 필요해 닫힌 어휘로 노출하지 않는다.
        catalog[str(item["canonical"])] = {
            "category": "activity",
            "terms": [str(term) for term in item.get("synonyms") or []],
        }
    return catalog


def resolve_member_scope(query: str, path: Path = DEFAULT_MEMBER_POLICY_PATH) -> dict[str, Any]:
    """Resolve the default active-member policy and explicit user overrides.

    ``전체 회원`` removes the default policy.  ``휴면 포함`` and ``탈퇴
    포함`` widen it only to the requested states, preserving an auditable
    structured policy instead of silently dropping the state predicate.
    """
    definition = active_member_definition(path)
    if _ALL_MEMBER_RE.search(query):
        return {"mode": "all", "states": [], "policy_id": "active_member"}

    states = [definition["value"]]
    if _INCLUDE_DORMANT_RE.search(query):
        dormant = _state_value("dormant", path)
        if dormant and dormant not in states:
            states.append(dormant)
    if _INCLUDE_WITHDRAWN_RE.search(query):
        withdrawn = _state_value("withdrawn", path)
        if withdrawn and withdrawn not in states:
            states.append(withdrawn)
    return {
        "mode": "default" if len(states) == 1 else "expanded",
        "states": states,
        "policy_id": "active_member",
    }


def active_member_filter(
    query: str,
    *,
    table: str | None = None,
    alias: str | None = None,
    path: Path = DEFAULT_MEMBER_POLICY_PATH,
) -> dict[str, Any] | None:
    """Build an aggregation filter from the shared member-state policy."""
    definition = active_member_definition(path)
    scope = resolve_member_scope(query, path)
    if scope["mode"] == "all":
        return None
    states = scope["states"]
    return {
        "id": "policy_active_member",
        "entity": "member",
        "field": "memberStateCode",
        "table": table or definition["table"],
        "column": definition["column"],
        "expression": f"{alias or definition['alias']}.{definition['column']}",
        "operator": "eq" if len(states) == 1 else "in",
        "value": states[0] if len(states) == 1 else states,
        "policy": True,
        "policyMode": scope["mode"],
    }


def active_member_predicate(alias: str = "B", path: Path = DEFAULT_MEMBER_POLICY_PATH) -> str:
    definition = active_member_definition(path)
    return f"{alias}.{definition['column']} = {sql_dialect.quote_literal(definition['value'])}"
