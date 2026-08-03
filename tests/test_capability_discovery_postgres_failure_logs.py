from __future__ import annotations

import re
import sys
import types
from collections.abc import Iterable
from typing import Any

import pytest

from capability_discovery.postgres_failure_logs import (
    FailureLogReadError,
    PsycopgFailureLogProvider,
)


TECHNICAL_COLUMNS = {
    "failure_log_id",
    "failure_reason",
    "query_plan",
    "missing_input_conditions",
    "clarification_questions",
    "stage_log",
    "context_metadata",
    "created_at",
}


class _FakeCursor:
    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        self.rows = tuple(rows)
        self.executions: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self, statement: str, parameters: tuple[Any, ...] | None = None
    ) -> None:
        self.executions.append((statement, parameters))

    def fetchall(self) -> tuple[dict[str, Any], ...]:
        return self.rows


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.cursor_calls = 0

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        self.cursor_calls += 1
        return self._cursor


def _install_fake_psycopg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: Iterable[dict[str, Any]] = (),
    connect_error: Exception | None = None,
) -> tuple[_FakeCursor, list[tuple[str, dict[str, Any]]]]:
    cursor = _FakeCursor(rows)
    connection = _FakeConnection(cursor)
    connect_calls: list[tuple[str, dict[str, Any]]] = []

    psycopg = types.ModuleType("psycopg")
    psycopg_rows = types.ModuleType("psycopg.rows")
    sentinel_dict_row = object()
    psycopg_rows.dict_row = sentinel_dict_row  # type: ignore[attr-defined]

    def connect(conninfo: str, **kwargs: Any) -> _FakeConnection:
        connect_calls.append((conninfo, kwargs))
        if connect_error is not None:
            raise connect_error
        return connection

    psycopg.connect = connect  # type: ignore[attr-defined]
    psycopg.rows = psycopg_rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", psycopg_rows)
    return cursor, connect_calls


def test_provider_uses_read_only_transaction_and_only_technical_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor, connect_calls = _install_fake_psycopg(
        monkeypatch,
        rows=[
            {
                "failure_log_id": 7,
                "failure_reason": "catalog_symbol_unresolved",
                "query_plan": {"decisions": []},
                "created_at": "2026-08-03T00:00:00Z",
                # A non-standard cursor must not widen this trust boundary.
                "prompt": "private user request",
                "generated_sql": "SELECT secret",
                "database_result": [{"secret": True}],
            }
        ],
    )
    provider = PsycopgFailureLogProvider(
        "postgresql://diagnostic-reader:secret@db/app",
        connect_timeout_seconds=4,
    )

    rows = provider.load_failure_rows(limit=17)

    assert connect_calls == [
        (
            "postgresql://diagnostic-reader:secret@db/app",
            {"row_factory": sys.modules["psycopg.rows"].dict_row, "connect_timeout": 4},
        )
    ]
    assert len(cursor.executions) == 2
    transaction_sql, transaction_parameters = cursor.executions[0]
    select_sql, select_parameters = cursor.executions[1]
    assert " ".join(transaction_sql.split()).upper() == "SET TRANSACTION READ ONLY"
    assert transaction_parameters is None
    assert select_parameters == (17,)

    normalized_select = " ".join(select_sql.split()).lower()
    assert normalized_select.startswith("select ")
    assert " from campaign_query_failure_logs " in normalized_select
    assert normalized_select.endswith("limit %s")
    assert "*" not in normalized_select
    for column in TECHNICAL_COLUMNS:
        assert re.search(rf"\b{re.escape(column)}\b", normalized_select)
    for forbidden_column in (
        "prompt",
        "original_query",
        "generated_sql",
        "database_result",
    ):
        assert not re.search(rf"\b{forbidden_column}\b", normalized_select)
    for mutation in ("insert", "update", "delete", "alter", "drop", "truncate"):
        assert not re.search(rf"\b{mutation}\b", normalized_select)

    assert rows == (
        {
            "failure_log_id": 7,
            "failure_reason": "catalog_symbol_unresolved",
            "query_plan": {"decisions": []},
            "created_at": "2026-08-03T00:00:00Z",
        },
    )
    assert set(rows[0]) <= TECHNICAL_COLUMNS
    assert "private user request" not in repr(rows)
    assert "SELECT secret" not in repr(rows)


def test_default_and_explicit_limits_are_parameterized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor, _ = _install_fake_psycopg(monkeypatch)
    provider = PsycopgFailureLogProvider("postgresql://db/app", default_limit=23)

    provider.load_failure_rows()
    provider.load_failure_rows(41)

    assert cursor.executions[1][1] == (23,)
    assert cursor.executions[3][1] == (41,)
    assert "23" not in cursor.executions[1][0]
    assert "41" not in cursor.executions[3][0]


@pytest.mark.parametrize("invalid_limit", [0, -1, 5_001, True, 1.5, "2"])
def test_invalid_runtime_limit_is_rejected_before_connect(
    monkeypatch: pytest.MonkeyPatch, invalid_limit: object
) -> None:
    _cursor, connect_calls = _install_fake_psycopg(monkeypatch)
    provider = PsycopgFailureLogProvider("postgresql://db/app")

    with pytest.raises(ValueError, match="limit"):
        provider.load_failure_rows(invalid_limit)  # type: ignore[arg-type]

    assert connect_calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_limit": 0},
        {"max_limit": 0},
        {"connect_timeout_seconds": 0},
        {"default_limit": 11, "max_limit": 10},
    ],
)
def test_invalid_provider_configuration_is_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        PsycopgFailureLogProvider("postgresql://db/app", **kwargs)


def test_connection_error_is_sanitized_and_conninfo_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DatabaseBoom(RuntimeError):
        pass

    secret_conninfo = "postgresql://reader:super-secret-password@db/private"
    provider = PsycopgFailureLogProvider(secret_conninfo)
    _install_fake_psycopg(
        monkeypatch,
        connect_error=DatabaseBoom(
            f"could not connect to {secret_conninfo}; query=SELECT private_data"
        ),
    )

    with pytest.raises(FailureLogReadError) as captured:
        provider.load_failure_rows()

    assert str(captured.value) == "failure log read failed: DatabaseBoom"
    assert "super-secret-password" not in str(captured.value)
    assert "private_data" not in str(captured.value)
    assert secret_conninfo not in repr(provider)


@pytest.mark.parametrize("conninfo", ["", "   ", None])
def test_conninfo_must_be_non_empty(conninfo: object) -> None:
    with pytest.raises(ValueError, match="conninfo"):
        PsycopgFailureLogProvider(conninfo)  # type: ignore[arg-type]
