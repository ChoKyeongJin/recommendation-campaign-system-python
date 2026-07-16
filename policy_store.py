"""DB 기반 정책(JSON) 저장소.

``docs/policies/*.json`` 정책을 파일 대신 PostgreSQL의 ``campaign_policies``
테이블에 보관하고, 프로세스 내 인메모리 캐시로 서빙한다. 캐시는 최초 접근 시
지연 로딩하며, 정책 수정/삭제/시딩 이후 ``reload()``로 갱신한다.

로딩 우선순위(호출부 ``api._load_ctr_model_policy`` 기준):
    DB(이 모듈) -> 파일(docs/policies) -> 코드 내 하드코딩 기본값

DB 연결 실패 시 ``get_policy``는 ``None``을 반환해 파일/하드코딩 fallback으로
자연스럽게 넘어간다. 연결 실패가 반복될 때 매 조회마다 재연결로 지연되는 것을
막기 위해 짧은 쿨다운을 둔다.

``prompt_store``와 동일한 캐시/쿨다운 전략을 따르되, 본문은 TEXT가 아니라
JSON 객체이므로 ``content`` 컬럼을 JSONB로 저장한다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("policy_store")

POLICY_TABLE = "campaign_policies"

# DB 연결 실패 후 재시도까지의 최소 대기 시간(초).
_RETRY_COOLDOWN_SECONDS = 30.0

_cache: dict[str, dict[str, Any]] = {}
_cache_ready = False
_next_retry_at = 0.0


def postgres_conninfo() -> str:
    """api._postgres_conninfo와 동일한 환경변수로 접속 문자열을 구성한다."""
    return " ".join(
        [
            f"host={os.getenv('POSTGRES_HOST', 'postgres')}",
            f"port={os.getenv('POSTGRES_PORT', '5432')}",
            f"dbname={os.getenv('POSTGRES_DB', 'campaign_db')}",
            f"user={os.getenv('POSTGRES_USER', 'postgres')}",
            f"password={os.getenv('POSTGRES_PASSWORD', '1234')}",
        ]
    )


def _connect(conninfo: str | None = None):
    import psycopg

    return psycopg.connect(conninfo or postgres_conninfo(), connect_timeout=5)


def _as_jsonb(value: dict[str, Any]):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def ensure_table(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {POLICY_TABLE} (
            name        VARCHAR(120) PRIMARY KEY,
            content     JSONB NOT NULL,
            description TEXT,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _load_cache(conninfo: str | None = None) -> dict[str, dict[str, Any]]:
    global _cache, _cache_ready, _next_retry_at
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(f"SELECT name, content FROM {POLICY_TABLE}")
            rows = cursor.fetchall()
    _cache = {name: content for name, content in rows}
    _cache_ready = True
    _next_retry_at = 0.0
    return _cache


def _ensure_cache(conninfo: str | None = None) -> None:
    global _next_retry_at
    if _cache_ready:
        return
    if time.monotonic() < _next_retry_at:
        return
    try:
        _load_cache(conninfo)
        logger.info("policy_store_cache_loaded count=%s", len(_cache))
    except Exception as exc:  # noqa: BLE001 - fallback 유지가 목적
        _next_retry_at = time.monotonic() + _RETRY_COOLDOWN_SECONDS
        logger.warning(
            "policy_store_cache_load_failed error=%s:%s",
            exc.__class__.__name__,
            exc,
        )


def get_policy(name: str, conninfo: str | None = None) -> dict[str, Any] | None:
    """캐시(=DB)에서 정책 객체를 조회한다. 없거나 DB 미사용이면 ``None``.

    환경변수 ``CAMPAIGN_POLICY_SOURCE=file``이면 DB를 건너뛰고 항상 ``None``을
    반환한다(테스트/오프라인에서 파일 기반 동작을 유지하기 위함).
    """
    if os.getenv("CAMPAIGN_POLICY_SOURCE", "db").lower() == "file":
        return None
    _ensure_cache(conninfo)
    value = _cache.get(name)
    return dict(value) if isinstance(value, dict) else None


def reload(conninfo: str | None = None) -> int:
    """캐시를 비우고 DB에서 다시 로딩한다. 로딩된 정책 개수를 반환한다."""
    global _cache_ready, _next_retry_at
    _cache_ready = False
    _next_retry_at = 0.0
    _load_cache(conninfo)
    return len(_cache)


def list_policies(conninfo: str | None = None) -> list[dict[str, Any]]:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"SELECT name, content, description, updated_at FROM {POLICY_TABLE} ORDER BY name"
            )
            rows = cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


def get_one(name: str, conninfo: str | None = None) -> dict[str, Any] | None:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"SELECT name, content, description, updated_at FROM {POLICY_TABLE} WHERE name = %s",
                (name,),
            )
            row = cursor.fetchone()
    return _row_to_dict(row) if row is not None else None


def upsert(
    name: str,
    content: dict[str, Any],
    description: str | None = None,
    conninfo: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise ValueError("policy_content_must_be_object")
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"""
                INSERT INTO {POLICY_TABLE} (name, content, description, updated_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (name) DO UPDATE
                    SET content = EXCLUDED.content,
                        description = COALESCE(EXCLUDED.description, {POLICY_TABLE}.description),
                        updated_at = CURRENT_TIMESTAMP
                RETURNING name, content, description, updated_at
                """,
                (name, _as_jsonb(content), description),
            )
            row = cursor.fetchone()
    reload(conninfo)
    return _row_to_dict(row)


def delete(name: str, conninfo: str | None = None) -> bool:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"DELETE FROM {POLICY_TABLE} WHERE name = %s RETURNING name",
                (name,),
            )
            row = cursor.fetchone()
    reload(conninfo)
    return row is not None


def seed_from_dir(policy_dir: str | Path, conninfo: str | None = None) -> list[dict[str, Any]]:
    """``policy_dir`` 내 ``*.json`` 정책 파일을 테이블로 upsert한다.

    파일명(확장자 제외, 예: ``ctr-model-policy``)을 ``name``으로 사용하므로
    ``api._load_ctr_model_policy``의 조회 키와 정확히 일치한다.
    """
    directory = Path(policy_dir)
    seeded: list[dict[str, Any]] = []
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            for path in sorted(directory.glob("*.json")):
                raw = path.read_text(encoding="utf-8").strip()
                if not raw:
                    continue
                content = json.loads(raw)
                if not isinstance(content, dict):
                    logger.warning("policy_store_seed_skip_non_object path=%s", path)
                    continue
                cursor.execute(
                    f"""
                    INSERT INTO {POLICY_TABLE} (name, content, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (name) DO UPDATE
                        SET content = EXCLUDED.content,
                            updated_at = CURRENT_TIMESTAMP
                    RETURNING name, content, description, updated_at
                    """,
                    (path.stem, _as_jsonb(content)),
                )
                row = cursor.fetchone()
                seeded.append(_row_to_dict(row))
    reload(conninfo)
    return seeded


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "name": row[0],
        "content": row[1],
        "description": row[2],
        "updated_at": row[3],
    }
