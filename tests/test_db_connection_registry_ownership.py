from __future__ import annotations

import pytest

import api
import db_connections


def test_mssql_query_timeout_default_is_one_minute() -> None:
    assert db_connections.DEFAULT_MSSQL_QUERY_TIMEOUT_SECONDS == 60


def test_mssql_query_timeout_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSSQL_QUERY_TIMEOUT_SECONDS", "90")
    assert db_connections.mssql_query_timeout_seconds() == 90


@pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-1"])
def test_mssql_query_timeout_never_falls_back_to_unlimited_wait(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    # pymssql 은 timeout=0 을 '무제한 대기'로 해석한다. 미설정/형식오류/0 이하는 모두
    # 기본값으로 되돌려, 잘못된 환경변수가 커넥션을 영원히 붙잡지 못하게 한다.
    monkeypatch.setenv("MSSQL_QUERY_TIMEOUT_SECONDS", raw)
    assert (
        db_connections.mssql_query_timeout_seconds()
        == db_connections.DEFAULT_MSSQL_QUERY_TIMEOUT_SECONDS
    )


def test_mssql_query_timeout_default_applies_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MSSQL_QUERY_TIMEOUT_SECONDS", raising=False)
    assert (
        db_connections.mssql_query_timeout_seconds()
        == db_connections.DEFAULT_MSSQL_QUERY_TIMEOUT_SECONDS
    )


def test_api_external_target_databases_come_from_connection_registry() -> None:
    assert api._EXTERNAL_TARGET_DBS == frozenset(db_connections.READ_ONLY_DBS)


def test_connection_registry_partition_is_complete() -> None:
    assert set(db_connections.ALL_DBS) == {
        "postgres",
        *db_connections.READ_ONLY_DBS,
    }
    assert "postgres" not in db_connections.READ_ONLY_DBS
