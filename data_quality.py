"""Column availability and post-execution sanity contracts for target SQL."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any
import re

from analytical_intent import (
    DEFAULT_ANALYTICS_REGISTRY_PATH,
    load_analytics_registry,
    resolve_dimension_mapping,
)


PROFILE_STATUSES = frozenset({
    "USABLE", "SPARSE", "UNUSABLE_ALL_NULL", "UNUSABLE_CONSTANT", "STALE", "UNKNOWN",
})
BLOCKING_PROFILE_STATUSES = frozenset({"UNUSABLE_ALL_NULL", "UNUSABLE_CONSTANT", "STALE"})


@dataclass(frozen=True)
class ColumnProfile:
    table: str
    column: str
    row_count: int
    non_null_count: int
    null_ratio: float
    distinct_count: int | None = None
    min_value: Any | None = None
    max_value: Any | None = None
    last_profiled_at: datetime | None = None
    status: str = "UNKNOWN"

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, table: str = "", column: str = "") -> "ColumnProfile":
        rows = max(0, int(raw.get("row_count") or 0))
        non_null = max(0, int(raw.get("non_null_count") or 0))
        ratio = raw.get("null_ratio")
        ratio = float(ratio) if isinstance(ratio, (int, float)) else (1.0 - non_null / rows if rows else 0.0)
        status = str(raw.get("status") or "").upper()
        distinct = raw.get("distinct_count")
        if status not in PROFILE_STATUSES:
            if rows > 0 and non_null == 0:
                status = "UNUSABLE_ALL_NULL"
            elif non_null > 0 and isinstance(distinct, int) and distinct <= 1:
                status = "UNUSABLE_CONSTANT"
            elif ratio >= 0.95:
                status = "SPARSE"
            elif rows > 0:
                status = "USABLE"
            else:
                status = "UNKNOWN"
        profiled = raw.get("last_profiled_at")
        try:
            profiled_at = datetime.fromisoformat(str(profiled).replace("Z", "+00:00")) if profiled else None
        except ValueError:
            profiled_at = None
        return cls(
            table=str(raw.get("table") or table), column=str(raw.get("column") or column),
            row_count=rows, non_null_count=non_null, null_ratio=ratio,
            distinct_count=distinct if isinstance(distinct, int) else None,
            min_value=raw.get("min_value"), max_value=raw.get("max_value"),
            last_profiled_at=profiled_at, status=status,
        )


def load_column_profiles(schema_path: Path) -> dict[tuple[str, str], ColumnProfile]:
    """Load optional profiles from either table columns or top-level ``column_profiles``."""
    try:
        payload = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles: dict[tuple[str, str], ColumnProfile] = {}
    for table, meta in (payload.get("tables") or {}).items():
        if not isinstance(meta, dict):
            continue
        for item in meta.get("columns") or []:
            if not isinstance(item, dict) or not isinstance(item.get("profile"), dict) or not item.get("name"):
                continue
            profile = ColumnProfile.from_dict(item["profile"], table=str(table), column=str(item["name"]))
            profiles[(profile.table.casefold(), profile.column.casefold())] = profile
    top_level = payload.get("column_profiles") or []
    if isinstance(top_level, dict):
        top_level = [dict(value, **({} if "." not in key else {
            "table": key.rsplit(".", 1)[0], "column": key.rsplit(".", 1)[1],
        })) for key, value in top_level.items() if isinstance(value, dict)]
    for raw in top_level if isinstance(top_level, list) else []:
        if not isinstance(raw, dict) or not raw.get("table") or not raw.get("column"):
            continue
        profile = ColumnProfile.from_dict(raw)
        profiles[(profile.table.casefold(), profile.column.casefold())] = profile
    return profiles


def validate_metric_profile(
    intent: dict[str, Any] | None,
    schema_path: Path,
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> dict[str, Any]:
    """Fail closed only when a selected analytical measure has a known unusable profile."""
    if not isinstance(intent, dict) or not intent.get("metric"):
        return {"ran": False, "valid": True, "status": "UNKNOWN", "issues": []}
    registry = load_analytics_registry(str(registry_path))
    metric = next((item for item in registry.get("metrics", []) if item.get("id") == intent.get("metric")), None)
    if not isinstance(metric, dict):
        return {"ran": False, "valid": True, "status": "UNKNOWN", "issues": []}
    source = metric.get("rankingSource") if intent.get("query_type") == "ranking" else next(
        (item for item in metric.get("sources", []) if item.get("id") == intent.get("source_id")), None,
    )
    measure = source.get("measure") if isinstance(source, dict) else None
    if not isinstance(measure, dict) or not measure.get("table") or not measure.get("column"):
        return {"ran": False, "valid": True, "status": "UNKNOWN", "issues": []}
    table, column = str(measure["table"]), str(measure["column"])
    profile = load_column_profiles(schema_path).get((table.casefold(), column.casefold()))
    if profile is None:
        return {"ran": False, "valid": True, "status": "UNKNOWN", "table": table, "column": column, "issues": []}
    invalid = profile.status in BLOCKING_PROFILE_STATUSES
    reason = "UNUSABLE_METRIC_COLUMN" if invalid else None
    profile_payload = asdict(profile)
    if profile.last_profiled_at is not None:
        profile_payload["last_profiled_at"] = profile.last_profiled_at.isoformat()
    return {
        "ran": True, "valid": not invalid, "status": profile.status,
        "table": table, "column": column, "profile": profile_payload,
        "reason_code": reason,
        "issues": [] if not invalid else [{
            "code": "unusable_metric_column", "reason_code": reason, "severity": "error",
            "message": f"지표 컬럼 {table}.{column}의 데이터 프로파일이 {profile.status} 상태입니다.",
        }],
    }


def analyze_execution_result(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    analytical_intent: dict[str, Any] | None = None,
    join_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate SQL execution success from whether the result can answer the question."""
    row_count = len(rows)
    all_null_columns = [column for column in columns if rows and all(row.get(column) is None for row in rows)]
    issues: list[dict[str, Any]] = []
    status = "VALID_RESULT"
    intent = analytical_intent if isinstance(analytical_intent, dict) else {}
    expected_shape = intent.get("result_shape")

    match_rate = (join_quality or {}).get("match_rate")
    if match_rate == 0:
        status = "INVALID_EMPTY_RESULT"
        issues.append({"reason_code": "ZERO_JOIN_MATCH_RATE", "message": "검증된 조인의 매칭률이 0입니다."})
    elif row_count == 0:
        if intent.get("query_type") in {"aggregate", "grouped_aggregate", "ranking"}:
            status = "SUSPICIOUS_EMPTY_RESULT"
            issues.append({"reason_code": "SUSPICIOUS_EMPTY_RESULT", "message": "집계/극값 요청이 0행을 반환했습니다."})
        else:
            status = "VALID_EMPTY_RESULT"
    elif expected_shape == "scalar" and row_count != 1:
        status = "INVALID_RESULT_SHAPE"
        issues.append({"reason_code": "RESULT_SHAPE_MISMATCH", "message": "단일 집계 요청이 한 행이 아닌 결과를 반환했습니다."})
    elif expected_shape == "single_member" and row_count != 1:
        status = "INVALID_RESULT_SHAPE"
        issues.append({"reason_code": "RESULT_SHAPE_MISMATCH", "message": "단일 극값 요청이 한 회원이 아닌 결과를 반환했습니다."})

    if rows and intent and columns:
        id_names = {"cust_id", "member_no", "member_id", "user_id"}
        measure_columns = [column for column in columns if column.casefold() not in id_names]
        if measure_columns and all(column in all_null_columns for column in measure_columns):
            status = "INVALID_EMPTY_RESULT"
            issues.append({"reason_code": "METRIC_ALL_NULL", "message": "반환된 지표 값이 모두 NULL입니다."})

        # Optional dimension value profiles catch a semantically wrong column
        # even when it was aliased to the requested name.  For example, store
        # ids such as CAB4 must never pass as a registration device channel.
        registry = load_analytics_registry(str(DEFAULT_ANALYTICS_REGISTRY_PATH))
        metric = next(
            (item for item in registry.get("metrics", []) if item.get("id") == intent.get("metric")),
            None,
        )
        source = next(
            (item for item in (metric or {}).get("sources", []) if item.get("id") == intent.get("source_id")),
            None,
        )
        folded_columns = {str(column).casefold(): str(column) for column in columns}
        for dimension_id in intent.get("dimensions", []) or []:
            spec = (registry.get("dimensions") or {}).get(dimension_id) or {}
            pattern = spec.get("valuePattern")
            mapping = resolve_dimension_mapping(registry, source, str(dimension_id)) or {}
            output_name = mapping.get("outputAlias") or mapping.get("column")
            result_column = folded_columns.get(str(output_name or "").casefold())
            if not pattern or not result_column:
                continue
            values = [row.get(result_column) for row in rows if row.get(result_column) is not None]
            if values and any(re.fullmatch(str(pattern), str(value), re.IGNORECASE) is None for value in values):
                status = "INVALID_RESULT_SEMANTICS"
                issues.append({
                    "reason_code": "SEMANTIC_DIMENSION_VALUE_MISMATCH",
                    "message": f"{dimension_id} 결과 값이 등록된 의미 도메인과 일치하지 않습니다.",
                    "dimension": dimension_id,
                    "column": result_column,
                })

    return {
        "ran": True, "valid": not issues, "status": status, "row_count": row_count,
        "column_count": len(columns), "all_null_columns": all_null_columns,
        "join_match_rate": match_rate, "issues": issues,
    }
