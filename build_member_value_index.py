"""CRM_MB_BASEINFO(실회원 테이블)의 저카디널리티 문자열 컬럼 값을 실DB에서 스냅샷해
member_value_index.json 을 생성한다.

목적: 회원 속성 타겟팅을 컬럼별 수동 큐레이션(TARGETABLE_OVERRIDES, MEMBER_EQ_FILTERS 추가) 없이
지원한다. 이 인덱스가 "프롬프트 토큰(값) -> 실컬럼 + 저장값" 해석의 단일 소스가 되고,
graph_rag._apply_member_value_filters 가 런타임에 결정론적으로 매칭한다. 새 컬럼/값은 이 스크립트를
다시 실행하면 자동 반영된다(큐레이션 소멸).

포함 규칙(안전 게이트):
  - 문자열 컬럼만, DISTINCT 값 수 1~MAX_DISTINCT(기본 300)만 — 식별자/날짜/고카디널리티 자동 제외.
  - Y/N 플래그 컬럼 제외(값 전부 Y/N).
  - 규칙 엔진(MEMBER_EQ_FILTERS/MEMBER_ACTIVITY_FILTERS)이 이미 소유한 컬럼 제외(이중 술어 방지).
  - 코드 저장값('MEM_GRADE_CD.VIP' 형태)은 CRM_CM_CODE(CD -> CD_NAME)로 사람이 쓰는 이름을 해석해
    함께 저장한다(매칭은 이름 기준, SQL 은 저장값 기준).

실행(컨테이너): docker compose exec python python build_member_value_index.py
출력: docs/data/member_value_index.json (build_rag_knowledge 가 dimension_value 노드로도 적재)
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_connections import run_read_query

TABLE = "CRM_MB_BASEINFO"
CONNECTION = "CRMDW"
MAX_DISTINCT = 300
# 회원 속성이 별도 테이블에 있는 경우(회원키 조인 필요). 지금 비어 있어도 등록해 두면 데이터가
# 적재되는 순간 재실행만으로 인덱스에 포함되고, graph_rag 가 회원키 서브쿼리로 자동 타겟팅한다.
# 예: 직업(JOB_CD)은 ODS_MALL_MMS_MEMBER_ZTS 에 있으나 2026-07 현재 0행(미적재).
AUX_ATTRIBUTE_TABLES = [
    {"table": "ODS_MALL_MMS_MEMBER_ZTS", "join_column": "MEMBER_NO", "columns": ["JOB_CD"]},
]
_STRING_TYPES = ("nvarchar", "varchar", "nchar", "char")
_CODE_VALUE = re.compile(r"^[A-Z0-9_]+\.[^.].*$")
# 이름에 허용하는 문자: 출력 가능한 ASCII + 한글(음절/자모) + 가운뎃점. 그 외(인코딩 깨진 값 등)는 제외.
_CLEAN_NAME = re.compile(r"^[\x20-\x7E가-힣ㄱ-ㅎㅏ-ㅣ·]+$")


def _rule_engine_columns() -> set[str]:
    """graph_rag 규칙 엔진이 이미 술어를 만드는 컬럼(이중 필터 방지). import 로 자동 동기화한다."""
    from graph_rag import MEMBER_EQ_FILTERS

    owned = {column.split(".")[-1].upper() for _, column, _ in MEMBER_EQ_FILTERS.values()}
    # 연령(AGE)과 미접속(LAST_LOGIN_DATE)은 범위 술어로 별도 컴파일된다.
    return owned | {"AGE", "LAST_LOGIN_DATE"}


def _rule_engine_value_names() -> set[str]:
    """규칙 엔진이 소유한 값 어휘(VIP/GOLD/FEMALE …). 다른 컬럼의 동명 값(예: NEPA_GRADE_CD 의
    'VIP')이 인덱스로 들어가면 'VIP 고객' 이 엉뚱한 컬럼에 걸리므로 제외한다."""
    from graph_rag import MEMBER_EQ_FILTERS

    return {value.split(".")[-1].casefold() for _, _, value in MEMBER_EQ_FILTERS.values()}


def fetch_string_columns() -> list[str]:
    rows = run_read_query(
        CONNECTION,
        f"""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = '{TABLE}'
        ORDER BY ORDINAL_POSITION
        """,
        enforce_select=False,
    )
    return [row["COLUMN_NAME"] for row in rows if str(row.get("DATA_TYPE", "")).lower() in _STRING_TYPES]


def fetch_region_hierarchy() -> dict[str, list[str]]:
    """시군구 -> 소속 시도 목록(주소 마스터 기준). '금천구랑 인천' 같은 복수 지역 표현이 나열(OR)인지
    수식(AND, 예: '인천 서구')인지 컴파일러가 데이터로 판별하는 근거. 동명 시군구(서구 등)는 여러
    시도에 속할 수 있어 목록이다."""
    rows = run_read_query(
        CONNECTION,
        """
        SELECT DISTINCT SIDO, SIGUNGU
        FROM DBO.CRM_CM_ADDRESS
        WHERE SIDO IS NOT NULL AND LTRIM(RTRIM(SIDO)) <> ''
          AND SIGUNGU IS NOT NULL AND LTRIM(RTRIM(SIGUNGU)) <> ''
        """,
        enforce_select=False,
    )
    mapping: dict[str, list[str]] = {}
    for row in rows:
        sido = str(row.get("SIDO") or "").strip()
        sigungu = str(row.get("SIGUNGU") or "").strip()
        if not sido or not sigungu:
            continue
        mapping.setdefault(sigungu, [])
        if sido not in mapping[sigungu]:
            mapping[sigungu].append(sido)
    return mapping


def fetch_code_names() -> dict[str, str]:
    """CRM_CM_CODE 의 접두어 포함 코드(CD) -> 사람이 쓰는 이름(CD_NAME)."""
    rows = run_read_query(
        CONNECTION,
        "SELECT CD, CD_NAME FROM CRM_CM_CODE WHERE CD LIKE '%.%'",
        enforce_select=False,
    )
    names: dict[str, str] = {}
    for row in rows:
        code = str(row.get("CD") or "").strip()
        name = str(row.get("CD_NAME") or "").strip()
        if code and name and code not in names:
            names[code] = name
    return names


def fetch_column_values(column: str, table: str = TABLE) -> list[dict[str, Any]] | None:
    """컬럼의 (값, 행수) 목록. 카디널리티 초과/빈 컬럼이면 None."""
    rows = run_read_query(
        CONNECTION,
        f"""
        SELECT TOP {MAX_DISTINCT + 1} [{column}] AS value, COUNT(*) AS cnt
        FROM {table}
        WHERE [{column}] IS NOT NULL AND LTRIM(RTRIM([{column}])) <> ''
        GROUP BY [{column}]
        ORDER BY COUNT(*) DESC
        """,
        enforce_select=False,
    )
    if not rows or len(rows) > MAX_DISTINCT:
        return None
    values = []
    for row in rows:
        value = str(row.get("value") or "").strip()
        if value:
            values.append({"value": value, "count": int(row.get("cnt") or 0)})
    return values or None


def _clean_values(
    values: list[dict[str, Any]], code_names: dict[str, str], owned_names: set[str]
) -> list[dict[str, Any]]:
    """이름 해석 + 매칭 가능 값만 남긴다(숫자뿐·인코딩 깨짐·규칙 엔진 소유 동명 값 제외)."""
    from graph_rag import _matchable_value_name

    matchable = []
    for entry in values:
        # 코드 저장값이면 사람이 쓰는 이름을 해석(없으면 접미어라도). 일반 값은 값 자체가 이름.
        if _CODE_VALUE.match(entry["value"]):
            entry["name"] = code_names.get(entry["value"], entry["value"].split(".", 1)[1])
        else:
            entry["name"] = entry["value"]
        if (
            _matchable_value_name(entry["name"])
            and _CLEAN_NAME.match(entry["name"])
            and entry["name"].casefold() not in owned_names
        ):
            matchable.append(entry)
    return matchable


def build_index() -> dict[str, Any]:
    skip_columns = _rule_engine_columns()
    owned_names = _rule_engine_value_names()
    code_names = fetch_code_names()
    columns_out = []
    for column in fetch_string_columns():
        # 식별자 번호 컬럼(_NO)은 값이 사람이 쓰는 토큰이 아니다(예: CUSTOMER_NO 지수표기 쓰레기값).
        if column.upper() in skip_columns or column.upper().endswith("_NO"):
            continue
        values = fetch_column_values(column)
        if values is None:
            continue
        # Y/N 플래그 컬럼 제외(타겟 값으로 매칭할 이름이 없다).
        if {entry["value"].upper() for entry in values} <= {"Y", "N"}:
            continue
        matchable = _clean_values(values, code_names, owned_names)
        if not matchable:
            continue
        columns_out.append({"column": column, "values": matchable})

    # 보조 속성 테이블(예: 직업 JOB_CD). 비어 있으면 자동 제외 — 적재되면 재실행만으로 포함된다.
    for aux in AUX_ATTRIBUTE_TABLES:
        for column in aux["columns"]:
            values = fetch_column_values(column, table=aux["table"])
            if values is None:
                continue
            if {entry["value"].upper() for entry in values} <= {"Y", "N"}:
                continue
            matchable = _clean_values(values, code_names, owned_names)
            if not matchable:
                continue
            columns_out.append(
                {
                    "column": column,
                    "source_table": aux["table"],
                    "join_column": aux["join_column"],
                    "values": matchable,
                }
            )
    return {
        "version": "1.0",
        "description": (
            "실회원 테이블 저카디널리티 컬럼 값 스냅샷. 프롬프트 토큰 -> 실컬럼/저장값 해석의 단일 소스. "
            "build_member_value_index.py 로 실DB에서 자동 생성(수동 큐레이션 없음)."
        ),
        "table": TABLE,
        "connection": CONNECTION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_distinct": MAX_DISTINCT,
        "column_count": len(columns_out),
        # 시군구 -> 시도 소속(주소 마스터). 복수 지역 조건의 OR(나열)/AND(수식) 판별용.
        "region_hierarchy": {"sigungu_to_sido": fetch_region_hierarchy()},
        "columns": columns_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build member column value index from the live member table.")
    parser.add_argument("--output", "-o", type=Path, default=Path("docs/data/member_value_index.json"))
    args = parser.parse_args()

    payload = build_index()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "column_count": payload["column_count"],
                "value_count": sum(len(column["values"]) for column in payload["columns"]),
                "columns": [column["column"] for column in payload["columns"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
