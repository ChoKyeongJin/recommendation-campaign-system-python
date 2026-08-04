from __future__ import annotations

import api
import db_connections


def test_api_external_target_databases_come_from_connection_registry() -> None:
    assert api._EXTERNAL_TARGET_DBS == frozenset(db_connections.READ_ONLY_DBS)


def test_connection_registry_partition_is_complete() -> None:
    assert set(db_connections.ALL_DBS) == {
        "postgres",
        *db_connections.READ_ONLY_DBS,
    }
    assert "postgres" not in db_connections.READ_ONLY_DBS
