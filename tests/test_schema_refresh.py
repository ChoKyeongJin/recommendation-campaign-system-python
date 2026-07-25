"""외부 실DB 카탈로그 리프레시(schema_extract --refresh-external) 회귀 — DB 이식성 D단계.

배경(docs/operations/db_portability_audit.md §4-D): 실DB 카탈로그는 승인 테이블만 큐레이션된
파일이라 전체 덤프가 아니라 리프레시가 맞다 — 구조(컬럼/타입/PK)는 실DB에서 갱신하되 사람이
쓴 지식(description_llm/join_hints/human_note/important/references)은 보존해야 한다.

고정 내용:
  - 타입 표기: MSSQL 은 DATA_TYPE+길이 조합(nvarchar(100)/nvarchar(max)/decimal(p,s)),
    MySQL 은 COLUMN_TYPE 완성형 그대로.
  - 병합: 사람 주석 보존 + 신규 컬럼(빈 주석) 추가 + 사라진 컬럼 제거를 diff 로 보고.
  - 리프레시: 커넥션/테이블 필터, 실DB에 없는 테이블은 기존 항목 보존 + missing_in_db 보고.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_schema_refresh.py -q
"""

import schema_extract as se


# ── 타입 표기 ────────────────────────────────────────────────────────────────

def test_mssql_type_labels():
    assert se._external_type_label({"data_type": "nvarchar", "char_len": 100}, "tsql") == "nvarchar(100)"
    assert se._external_type_label({"data_type": "nvarchar", "char_len": -1}, "tsql") == "nvarchar(max)"
    assert se._external_type_label({"data_type": "bigint", "char_len": None}, "tsql") == "bigint"
    assert se._external_type_label({"data_type": "decimal", "num_prec": 18, "num_scale": 2}, "tsql") == "decimal(18,2)"


def test_mysql_type_label_uses_full_column_type():
    row = {"data_type": "varchar", "char_len": 100, "full_type": "VARCHAR(100)"}
    assert se._external_type_label(row, "mysql") == "varchar(100)"


# ── 병합(사람 지식 보존) ─────────────────────────────────────────────────────

def _meta_with_annotations() -> dict:
    return {
        "object_type": "table",
        "database": "CRMDW",
        "description_llm": "회원 기본정보(사람이 쓴 설명 — 보존돼야 함)",
        "join_hints": ["ZIP_CD -> CRM_CM_ADDRESS.ZIP_CODE"],
        "columns": [
            {
                "name": "MEMBER_NO",
                "type": "bigint",
                "nullable": False,
                "primary_key": True,
                "references": None,
                "important": True,
                "human_note": "회원번호 PK — 보존돼야 함",
            },
            {
                "name": "LEGACY_COL",
                "type": "int",
                "nullable": True,
                "primary_key": False,
                "references": {"table": "OLD", "column": "ID"},
                "important": False,
                "human_note": "실DB에서 사라진 컬럼",
            },
        ],
        "primary_key": ["MEMBER_NO"],
        "foreign_keys": [{"columns": ["ZIP_CD"], "references": {"table": "CRM_CM_ADDRESS", "columns": ["ZIP_CODE"]}}],
        "indexes": [],
    }


def test_merge_preserves_human_knowledge_and_reports_diff():
    meta = _meta_with_annotations()
    live = {
        "object_type": "table",
        "primary_key": ["MEMBER_NO"],
        "columns": [
            {"name": "MEMBER_NO", "type": "bigint", "nullable": False},
            {"name": "NEW_COL", "type": "nvarchar(50)", "nullable": True},
        ],
    }
    diff = se._merge_live_structure(meta, live)

    assert diff == {"added": ["NEW_COL"], "removed": ["LEGACY_COL"]}
    by_name = {column["name"]: column for column in meta["columns"]}
    # 사람 주석/중요 표시 보존 + 구조는 실DB 기준.
    assert by_name["MEMBER_NO"]["human_note"] == "회원번호 PK — 보존돼야 함"
    assert by_name["MEMBER_NO"]["important"] is True
    assert by_name["MEMBER_NO"]["primary_key"] is True
    # 신규 컬럼은 빈 주석으로 시작한다.
    assert by_name["NEW_COL"]["human_note"] == "" and by_name["NEW_COL"]["important"] is False
    assert "LEGACY_COL" not in by_name
    # 테이블 레벨 사람 지식은 건드리지 않는다.
    assert meta["description_llm"].startswith("회원 기본정보")
    assert meta["join_hints"] == ["ZIP_CD -> CRM_CM_ADDRESS.ZIP_CODE"]
    assert meta["foreign_keys"][0]["references"]["table"] == "CRM_CM_ADDRESS"
    assert meta["description_source"] == "live_db_information_schema"


def test_merge_updates_type_and_nullable_from_live():
    meta = _meta_with_annotations()
    live = {
        "object_type": "table",
        "primary_key": [],
        "columns": [{"name": "MEMBER_NO", "type": "nvarchar(20)", "nullable": True}],
    }
    se._merge_live_structure(meta, live)
    column = meta["columns"][0]
    assert column["type"] == "nvarchar(20)" and column["nullable"] is True
    # 실DB PK 를 못 읽었으면(빈 목록) 기존 primary_key 표시를 유지한다.
    assert column["primary_key"] is True
    assert meta["primary_key"] == ["MEMBER_NO"]


# ── refresh_external_catalog (인트로스펙션 스텁) ────────────────────────────

def _catalog() -> dict:
    return {
        "tables": {
            "CRM_MB_BASEINFO": _meta_with_annotations(),
            "T_TARGET": {"database": "quadmax_sdz", "columns": [{"name": "ID", "type": "int", "nullable": False}]},
            "local_demo": {"columns": []},  # database 없음 → 로컬(postgres) 소관, 건드리지 않는다
        }
    }


def test_refresh_filters_and_reports(monkeypatch):
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def fake_introspect(connection, table_names, dialect):
        calls.append((connection, tuple(table_names), dialect))
        if connection == "CRMDW":
            return {
                "CRM_MB_BASEINFO": {
                    "object_type": "table",
                    "primary_key": ["MEMBER_NO"],
                    "columns": [{"name": "MEMBER_NO", "type": "bigint", "nullable": False}],
                }
            }
        return {}  # quadmax: 실DB에 테이블 없음

    monkeypatch.setattr(se, "_introspect_external_tables", fake_introspect)
    catalog = _catalog()
    summary = se.refresh_external_catalog(catalog)

    assert ("CRMDW", ("CRM_MB_BASEINFO",), "tsql") in calls
    assert ("quadmax_sdz", ("T_TARGET",), "mysql") in calls
    assert summary["connections"]["CRMDW"] == "refreshed 1/1"
    assert summary["missing_in_db"] == ["quadmax_sdz:T_TARGET"]
    # 실DB에 없는 테이블은 기존 항목이 그대로 남는다.
    assert catalog["tables"]["T_TARGET"]["columns"][0]["name"] == "ID"
    # 변경 diff 보고(LEGACY_COL 제거).
    assert summary["changed_tables"]["CRM_MB_BASEINFO"]["removed"] == ["LEGACY_COL"]


def test_refresh_connection_filter(monkeypatch):
    calls: list[str] = []

    def fake_introspect(connection, table_names, dialect):
        calls.append(connection)
        return {}

    monkeypatch.setattr(se, "_introspect_external_tables", fake_introspect)
    se.refresh_external_catalog(_catalog(), connection_filter="CRMDW")
    assert calls == ["CRMDW"]
