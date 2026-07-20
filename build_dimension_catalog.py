"""t_xlig_dimension_list(ML_DS_DIV_CD='DS')에서 디멘션 '정의'를 뽑아 dimension_catalog.sample.json 생성.

값(코드/이름)은 저장하지 않는다 — 디멘션마다 값이 매우 많을 수 있으므로 런타임에 DS_SQL 을 실행해
동적으로 해석한다(graph_rag._apply_dimension_filters). 이 스크립트는 '정의'(프롬프트 라벨/동의어,
DBMS, DS_SQL, 연산자, 타겟 컬럼)만 스냅샷한다.

타겟 컬럼(target_column/target_table)은 t_xlig_query_prompt.field 가 쿼리별 별칭(A./C.)이라 테이블을
자동 특정할 수 없으므로, 안전하게 '큐레이션된' 매핑만 채운다(TARGETABLE_OVERRIDES). 나머지는 null 로
두어 카탈로그/값해석에는 쓰되 타겟팅 SQL 조건은 만들지 않는다.

실행: (컨테이너 안, pymysql 필요)
    python build_dimension_catalog.py -o docs/data/dimension_catalog.sample.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from db_connections import run_read_query

# DBMS_ID -> db_connections.run_read_query 의 db 이름. 여기에 없는 DBMS 는 런타임 값해석 불가(정의만 등록).
DBMS_CONNECTION_MAP = {
    "CRMDW": "CRMDW",
    "CRMAN": "CRMAN",
    "QUADMAX_SDZ": "quadmax_sdz",
}

# 타겟팅 SQL 로 걸 수 있는 '큐레이션된' 컬럼 매핑. key = PRMP_KWD(@@ 포함).
# query_prompt.field 는 쿼리별 별칭이라 자동으로 테이블을 특정할 수 없어 수동 확정분만 넣는다.
TARGETABLE_OVERRIDES = {
    "@@거래브랜드@@": {"target_table": "CRM_CM_PRODUCT", "target_column": "CRM_CM_PRODUCT.BRAND_ID"},
}

# query_prompt label_kr 에 섞여 있는 UI 위젯/노이즈 라벨(동의어로 쓰지 않는다).
_NOISE_LABEL = re.compile(r"팝업|콤보|셀렉트|체크|check|popup|text|combo|multiselect|버전업|테스트|test|::op::", re.IGNORECASE)


def _slug(prmp_kwd: str) -> str:
    return prmp_kwd.strip("@").strip()


def _clean_label(label: Any) -> str | None:
    if not isinstance(label, str):
        return None
    label = label.strip()
    if not label or _NOISE_LABEL.search(label):
        return None
    return label


def fetch_dimensions() -> list[dict[str, Any]]:
    rows = run_read_query(
        "quadmax_sdz",
        """
        SELECT PRMP_KWD, PRMP_KWD_NM, DBMS_ID, DS_SQL
        FROM quadmax_sdz.t_xlig_dimension_list
        WHERE ML_DS_DIV_CD = 'DS' AND DEL_F = 'N'
        ORDER BY DBMS_ID, PRMP_KWD
        """,
        # enforce_select=False: 자동 LIMIT 부착을 피해 전체 행을 가져온다(quadmax 는 서버 레벨 read-only).
        enforce_select=False,
    )
    prompt_rows = run_read_query(
        "quadmax_sdz",
        """
        SELECT PRMP_KWD,
               JSON_UNQUOTE(JSON_EXTRACT(PRMP_JSON_INFO, '$.label.kr')) AS label_kr,
               PRMP_OP
        FROM quadmax_sdz.t_xlig_query_prompt
        """,
        enforce_select=False,
    )
    labels_by_kwd: dict[str, list[str]] = {}
    ops_by_kwd: dict[str, list[str]] = {}
    for row in prompt_rows:
        kwd = row.get("PRMP_KWD")
        if not kwd:
            continue
        label = _clean_label(row.get("label_kr"))
        if label:
            labels_by_kwd.setdefault(kwd, [])
            if label not in labels_by_kwd[kwd]:
                labels_by_kwd[kwd].append(label)
        op = row.get("PRMP_OP")
        if isinstance(op, str) and op.upper() in {"IN", "="}:
            ops_by_kwd.setdefault(kwd, [])
            if op.upper() not in ops_by_kwd[kwd]:
                ops_by_kwd[kwd].append(op.upper())

    dimensions = []
    for row in rows:
        prmp_kwd = row.get("PRMP_KWD")
        ds_sql = row.get("DS_SQL")
        if not prmp_kwd or not isinstance(ds_sql, str) or not ds_sql.strip():
            continue
        prmp_kwd_nm = row.get("PRMP_KWD_NM") or _slug(prmp_kwd)
        labels = labels_by_kwd.get(prmp_kwd, [])
        # prompt_label: 프롬프트에서 실제로 쓰는 사용자 라벨을 우선(가장 흔한 clean label), 없으면 디멘션명.
        prompt_label = labels[0] if labels else prmp_kwd_nm
        synonyms = []
        for candidate in [prompt_label, prmp_kwd_nm, *labels]:
            candidate = candidate.strip() if isinstance(candidate, str) else ""
            if candidate and candidate not in synonyms and not _NOISE_LABEL.search(candidate):
                synonyms.append(candidate)
        operators = ops_by_kwd.get(prmp_kwd, [])
        operator = "IN" if "IN" in operators or not operators else operators[0]
        override = TARGETABLE_OVERRIDES.get(prmp_kwd, {})
        dimensions.append(
            {
                "dimension_id": _slug(prmp_kwd),
                "prompt_label": prompt_label,
                "prmp_kwd": prmp_kwd,
                "prmp_kwd_nm": prmp_kwd_nm,
                "dbms_id": row.get("DBMS_ID"),
                "connection": DBMS_CONNECTION_MAP.get(row.get("DBMS_ID")),
                "operator": operator,
                "synonyms": synonyms,
                "target_table": override.get("target_table"),
                "target_column": override.get("target_column"),
                "ds_sql": ds_sql.strip(),
            }
        )
    return dimensions


def build_payload(dimensions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": "2.0",
        "description": (
            "프롬프트 키워드(디멘션) 정의 스냅샷. 값(코드/이름)은 저장하지 않고 런타임에 DS_SQL 로 동적 해석한다. "
            "t_xlig_dimension_list(ML_DS_DIV_CD='DS', DEL_F='N') + t_xlig_query_prompt(label/operator)에서 생성."
        ),
        "source": {
            "dimension_table": "quadmax_sdz.t_xlig_dimension_list",
            "query_prompt_table": "quadmax_sdz.t_xlig_query_prompt",
            "filter": "ML_DS_DIV_CD='DS' AND DEL_F='N'",
        },
        "value_resolution": "dynamic",
        "value_column_convention": "DS_SQL 결과의 첫 컬럼=코드, 둘째 컬럼=이름",
        "dbms_connection_map": DBMS_CONNECTION_MAP,
        "dimension_count": len(dimensions),
        "dimensions": dimensions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dimension catalog (definitions only) from t_xlig_dimension_list.")
    parser.add_argument("--output", "-o", type=Path, default=Path("docs/data/dimension_catalog.sample.json"))
    args = parser.parse_args()

    dimensions = fetch_dimensions()
    payload = build_payload(dimensions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    targetable = sum(1 for dimension in dimensions if dimension.get("target_column"))
    resolvable = sum(1 for dimension in dimensions if dimension.get("connection"))
    print(
        json.dumps(
            {
                "output": str(args.output),
                "dimension_count": len(dimensions),
                "value_resolvable": resolvable,
                "targetable": targetable,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
