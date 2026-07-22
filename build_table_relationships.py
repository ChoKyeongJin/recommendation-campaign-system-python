"""테이블 간 관계(FK) 큐레이션을 schema_catalog.json 에 주입하고 관계도(mermaid)를 생성한다.

실DB(CRMDW)에는 선언된 외래키가 하나도 없어(get_foreign_keys → []), 테이블 관계가
결정론 빌더의 실제 조인 술어에만 암묵적으로 존재했다. 이 스크립트는 그 관계를 명시적
구조(schema_catalog.tables[child].foreign_keys)로 승격해 RAG 스키마 그래프(_add_schema_edges)와
FK 임베딩(render_foreign_keys)이 활용하게 하고, 동시에 사람이 볼 관계도를 같은 소스에서 만든다.

관계 출처(confidence):
  - verified   : 결정론 빌더 SQL 또는 실DB 조인 오버랩으로 확인됨(실추출에 이미 사용 중)
  - human_hint : 사람이 넣은 join_hints (schema_catalog 의 기존 힌트)
  - inferred   : 동일 키 컬럼명 기반 추론(실행으로 확증 전, 회원 하위·캠페인 체인)

활성 사용 테이블(회원·주문몰·상품·브랜드·장바구니·주소·매장·캠페인 Z_*)만 대상으로 한다.

실행(컨테이너):
  docker compose exec -w /app -e PYTHONPATH=/app api python build_table_relationships.py
  docker compose exec -w /app -e PYTHONPATH=/app api python build_rag_knowledge.py   # 그래프 반영
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_CATALOG_PATH = Path("docs/data/schema_catalog.json")
DIAGRAM_PATH = Path("docs/data/table_relationships.md")

# (child_table, [child_cols], parent_table, [parent_cols], confidence, source)
# child 가 FK 를 보유(다:1의 '다' 쪽), parent 의 PK/식별키를 가리킨다.
RELATIONSHIPS: list[tuple[str, list[str], str, list[str], str, str]] = [
    # --- 회원 허브(CRM_MB_BASEINFO: MEMBER_NO bigint / MEMBER_ID 문자열) ---
    ("CRM_SL_ORDERHEADERMALL", ["MEMBER_NO"], "CRM_MB_BASEINFO", ["MEMBER_NO"], "verified", "sql_builder:order_count_targets"),
    ("CRM_SL_ORDERDETAILMALL", ["MEMBER_NO"], "CRM_MB_BASEINFO", ["MEMBER_NO"], "verified", "sql_builder:purchase_history_targets"),
    ("CRM_MB_MONTHCRMINFO", ["MEMBER_NO"], "CRM_MB_BASEINFO", ["MEMBER_NO"], "verified", "sql_builder:dense_region_targets"),
    ("CRM_MB_BABY", ["MEMBER_NO"], "CRM_MB_BASEINFO", ["MEMBER_NO"], "inferred", "shared_key:MEMBER_NO"),
    ("CRM_MB_MEMBERBUYPROPERTY", ["MEMBER_NO"], "CRM_MB_BASEINFO", ["MEMBER_NO"], "inferred", "shared_key:MEMBER_NO"),
    # 장바구니: CART_ID 가 회원 문자열키(MEMBER_ID)에 대응(빌더 조인 관례)
    ("ODS_MALL_OMS_CART", ["CART_ID"], "CRM_MB_BASEINFO", ["MEMBER_ID"], "verified", "sql_builder:cart_targets"),
    ("ODS_MALL_OMS_CART", ["PRODUCT_ID"], "CRM_CM_PRODUCT", ["PRODUCT_ID"], "verified", "sql_builder:cart_dimension_targets"),
    # --- 회원 → 공통 마스터(기존 join_hints) ---
    ("CRM_MB_BASEINFO", ["ZIP_CD"], "CRM_CM_ADDRESS", ["ZIP_CODE"], "human_hint", "join_hint"),
    ("CRM_MB_BASEINFO", ["REG_OFFSHOP_ID"], "CRM_CM_OFFSHOP", ["OFFSHOP_ID"], "human_hint", "join_hint"),
    # --- 주문(몰) ---
    ("CRM_SL_ORDERDETAILMALL", ["ORDER_ID"], "CRM_SL_ORDERHEADERMALL", ["ORDER_ID"], "verified", "live_join_check:200of200"),
    ("CRM_SL_ORDERDETAILMALL", ["PRODUCT_ID"], "CRM_CM_PRODUCT", ["PRODUCT_ID"], "verified", "sql_builder:purchase_history_targets"),
    # --- 상품 → 브랜드 ---
    ("CRM_CM_PRODUCT", ["BRAND_ID"], "CRM_CM_BRAND", ["BRAND_ID"], "verified", "live_join_check"),
    # --- 캠페인 체인(CAMP_ID; 컬럼 존재는 실DB 확인, 오버랩 미검증) ---
    ("Z_CAMP_CELL", ["CAMP_ID"], "Z_CAMPAIGN", ["CAMP_ID"], "inferred", "shared_key_live:CAMP_ID"),
    ("Z_CAMP_MBR", ["CAMP_ID"], "Z_CAMPAIGN", ["CAMP_ID"], "inferred", "shared_key_live:CAMP_ID"),
    ("MCS_CAMP_MBR_RSPN_FT", ["CAMP_ID"], "Z_CAMPAIGN", ["CAMP_ID"], "inferred", "shared_key_live:CAMP_ID"),
]


def _validate(catalog: dict[str, Any]) -> list[str]:
    """관계에 등장하는 테이블/컬럼이 카탈로그에 실제 존재하는지 검증한다(오타 방어)."""
    tables = catalog.get("tables", {})
    problems: list[str] = []

    def _has_column(table: str, column: str) -> bool:
        meta = tables.get(table)
        if not isinstance(meta, dict):
            return False
        return any(col.get("name") == column for col in meta.get("columns", []))

    for child, child_cols, parent, parent_cols, _confidence, _source in RELATIONSHIPS:
        if child not in tables:
            problems.append(f"child 테이블 없음: {child}")
        if parent not in tables:
            problems.append(f"parent 테이블 없음: {parent}")
        for column in child_cols:
            if child in tables and not _has_column(child, column):
                problems.append(f"컬럼 없음: {child}.{column}")
        for column in parent_cols:
            if parent in tables and not _has_column(parent, column):
                problems.append(f"컬럼 없음: {parent}.{column}")
        if len(child_cols) != len(parent_cols):
            problems.append(f"컬럼 개수 불일치: {child}{child_cols} -> {parent}{parent_cols}")
    return problems


def apply_relationships(catalog: dict[str, Any]) -> int:
    """관계를 child 테이블의 foreign_keys 로 주입한다(테이블별로 그룹핑, 관리 대상만 교체)."""
    tables = catalog["tables"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for child, child_cols, parent, parent_cols, confidence, source in RELATIONSHIPS:
        grouped.setdefault(child, []).append(
            {
                "columns": child_cols,
                "references": {"table": parent, "columns": parent_cols},
                "confidence": confidence,
                "source": source,
            }
        )
    for child, foreign_keys in grouped.items():
        tables[child]["foreign_keys"] = foreign_keys
    return len(RELATIONSHIPS)


def _sanitize(node: str) -> str:
    """mermaid erDiagram 식별자용(영숫자/언더스코어만)."""
    return node


def render_diagram(catalog: dict[str, Any]) -> str:
    """관계도(mermaid erDiagram) + 출처 표를 생성한다."""
    lines = ["erDiagram"]
    for child, child_cols, parent, parent_cols, _confidence, _source in RELATIONSHIPS:
        label = child_cols[0] if len(child_cols) == 1 else "+".join(child_cols)
        # child(다) }o--|| parent(1): child 여러 행이 parent 한 행을 가리킴
        lines.append(f'    {_sanitize(parent)} ||--o{{ {_sanitize(child)} : "{label}"')
    diagram = "\n".join(lines)

    rows = ["| child.컬럼 | → | parent.컬럼 | 신뢰도 | 출처 |", "|---|---|---|---|---|"]
    for child, child_cols, parent, parent_cols, confidence, source in RELATIONSHIPS:
        rows.append(
            f"| `{child}.{','.join(child_cols)}` | → | `{parent}.{','.join(parent_cols)}` | {confidence} | {source} |"
        )
    table = "\n".join(rows)

    return (
        "# 테이블 관계도 (활성 사용 테이블)\n\n"
        "> 자동 생성: `build_table_relationships.py`. 수정은 스크립트의 `RELATIONSHIPS` 를 고칠 것.\n\n"
        "CRMDW 에는 선언된 외래키가 없어, 아래 관계는 결정론 빌더의 실제 조인(verified)·기존 join_hints"
        "(human_hint)·동일 키 추론(inferred)에서 큐레이션했다.\n\n"
        "```mermaid\n" + diagram + "\n```\n\n"
        "## 관계 출처\n\n" + table + "\n"
    )


def main() -> None:
    catalog = json.loads(SCHEMA_CATALOG_PATH.read_text(encoding="utf-8"))

    problems = _validate(catalog)
    if problems:
        raise SystemExit("관계 검증 실패:\n  - " + "\n  - ".join(problems))

    applied = apply_relationships(catalog)
    SCHEMA_CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    DIAGRAM_PATH.write_text(render_diagram(catalog), encoding="utf-8")

    child_tables = sorted({rel[0] for rel in RELATIONSHIPS})
    print(f"관계 {applied}개 주입 완료 → {SCHEMA_CATALOG_PATH}")
    print(f"관계 보유 테이블 {len(child_tables)}개: {', '.join(child_tables)}")
    print(f"관계도 생성 → {DIAGRAM_PATH}")


if __name__ == "__main__":
    main()
