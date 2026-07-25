"""DB 스왑 프리플라이트(db_swap_preflight.py) 회귀 — DB 이식성 게이트.

배경(docs/operations/db_swap_runbook.md): DB 스왑의 1순위 실패모드는 레지스트리가 참조하는
테이블/컬럼이 새 카탈로그에 없는데 조용히 통과되는 것. 프리플라이트가 그 불일치를 반드시
잡아야(그리고 정상일 때는 통과해야) 게이트로서 의미가 있다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_db_swap_preflight.py -q
"""

import json

import db_swap_preflight as pf


def _write(tmp_path, catalog: dict, registry: dict, metrics: dict | None = None):
    catalog_path = tmp_path / "schema_catalog.json"
    mtf_path = tmp_path / "member_target_filters.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    mtf_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    paths = [mtf_path]
    if metrics is not None:
        metrics_path = tmp_path / "member_metrics.json"
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")
        paths.append(metrics_path)
    return catalog_path, paths


def _catalog() -> dict:
    return {
        "databases": {"CRMDW": "SQL Server"},
        "tables": {
            "MEMBER": {
                "database": "CRMDW",
                "columns": [{"name": "MID"}, {"name": "GENDER"}, {"name": "GRADE"}, {"name": "STATE"}],
            },
            "ORDERS": {"database": "CRMDW", "columns": [{"name": "MID"}, {"name": "AMT"}]},
        },
    }


def _good_registry() -> dict:
    return {
        "base_entity": {"table": "MEMBER", "alias": "B", "member_key": "MID"},
        "active_state": {"column": "STATE", "value": "NORMAL"},
        "eq_filters": [
            {"canonical": "female", "category": "gender", "column": "B.GENDER", "value": "F"},
            {"canonical": "vip", "category": "grade", "column": "B.GRADE", "value": "VIP"},
        ],
        "order_count_targets": {"table": "ORDERS", "join_column": "MID"},
    }


def test_passes_when_registry_matches_catalog(tmp_path, monkeypatch):
    catalog_path, reg_paths = _write(tmp_path, _catalog(), _good_registry())
    monkeypatch.setattr(pf, "SCHEMA_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(pf, "REGISTRY_PATHS", reg_paths)
    result = pf.run_preflight()
    assert result["ok"] is True, result["problems"]
    assert result["problems"] == []


def test_catches_missing_table(tmp_path, monkeypatch):
    registry = _good_registry()
    registry["order_count_targets"]["table"] = "ORDER_HEADER_NEW"  # 새 DB에서 이름이 바뀐 주문 테이블
    catalog_path, reg_paths = _write(tmp_path, _catalog(), registry)
    monkeypatch.setattr(pf, "SCHEMA_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(pf, "REGISTRY_PATHS", reg_paths)
    result = pf.run_preflight()
    assert result["ok"] is False
    assert any("ORDER_HEADER_NEW" in problem for problem in result["problems"])


def test_catches_missing_base_column(tmp_path, monkeypatch):
    registry = _good_registry()
    registry["eq_filters"][0]["column"] = "B.SEX"  # 카탈로그엔 GENDER 인데 레지스트리는 SEX 로 참조
    catalog_path, reg_paths = _write(tmp_path, _catalog(), registry)
    monkeypatch.setattr(pf, "SCHEMA_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(pf, "REGISTRY_PATHS", reg_paths)
    result = pf.run_preflight()
    assert result["ok"] is False
    assert any("MEMBER.SEX" in problem for problem in result["problems"])


def test_catches_missing_base_table(tmp_path, monkeypatch):
    registry = _good_registry()
    registry["base_entity"]["table"] = "MEMBER_V2"
    catalog_path, reg_paths = _write(tmp_path, _catalog(), registry)
    monkeypatch.setattr(pf, "SCHEMA_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(pf, "REGISTRY_PATHS", reg_paths)
    result = pf.run_preflight()
    assert result["ok"] is False
    assert any("MEMBER_V2" in problem for problem in result["problems"])


def test_metrics_registry_table_checked(tmp_path, monkeypatch):
    metrics = {"value_table": "CRM_MB_MONTHCRMINFO", "metrics": []}  # 카탈로그에 없는 테이블
    catalog_path, reg_paths = _write(tmp_path, _catalog(), _good_registry(), metrics=metrics)
    monkeypatch.setattr(pf, "SCHEMA_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(pf, "REGISTRY_PATHS", reg_paths)
    result = pf.run_preflight()
    assert result["ok"] is False
    assert any("CRM_MB_MONTHCRMINFO" in problem for problem in result["problems"])
