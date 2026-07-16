"""DB 기반 프롬프트 템플릿 저장소.

프롬프트는 PostgreSQL의 ``campaign_prompt_templates`` 테이블에 보관하고,
프로세스 내 인메모리 캐시로 서빙한다. 캐시는 최초 접근 시 지연 로딩하며,
프롬프트 수정/삭제/시딩 이후 ``reload()``로 갱신한다.

로딩 우선순위(호출부 ``graph_rag._read_prompt_template`` 기준):
    DB(이 모듈) -> 파일(prompt_dir) -> 코드 내 하드코딩 fallback

DB 연결 실패 시 ``get_template``은 ``None``을 반환해 파일/하드코딩 fallback으로
자연스럽게 넘어간다. 연결 실패가 반복될 때 매 프롬프트 호출마다 재연결로
지연되는 것을 막기 위해 짧은 쿨다운을 둔다.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("prompt_store")

PROMPT_TABLE = "campaign_prompt_templates"

# DB 연결 실패 후 재시도까지의 최소 대기 시간(초).
_RETRY_COOLDOWN_SECONDS = 30.0

_cache: dict[str, str] = {}
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


def ensure_table(cursor: Any) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROMPT_TABLE} (
            name        VARCHAR(120) PRIMARY KEY,
            content     TEXT NOT NULL,
            description TEXT,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _load_cache(conninfo: str | None = None) -> dict[str, str]:
    global _cache, _cache_ready, _next_retry_at
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(f"SELECT name, content FROM {PROMPT_TABLE}")
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
        logger.info("prompt_store_cache_loaded count=%s", len(_cache))
    except Exception as exc:  # noqa: BLE001 - fallback 유지가 목적
        _next_retry_at = time.monotonic() + _RETRY_COOLDOWN_SECONDS
        logger.warning(
            "prompt_store_cache_load_failed error=%s:%s",
            exc.__class__.__name__,
            exc,
        )


def get_template(name: str, conninfo: str | None = None) -> str | None:
    """캐시(=DB)에서 프롬프트 본문을 조회한다. 없거나 DB 미사용이면 ``None``.

    환경변수 ``GRAPH_RAG_PROMPT_SOURCE=file``이면 DB를 건너뛰고 항상 ``None``을
    반환한다(테스트/오프라인에서 파일 기반 동작을 유지하기 위함).
    """
    if os.getenv("GRAPH_RAG_PROMPT_SOURCE", "db").lower() == "file":
        return None
    _ensure_cache(conninfo)
    return _cache.get(name)


def reload(conninfo: str | None = None) -> int:
    """캐시를 비우고 DB에서 다시 로딩한다. 로딩된 프롬프트 개수를 반환한다."""
    global _cache_ready, _next_retry_at
    _cache_ready = False
    _next_retry_at = 0.0
    _load_cache(conninfo)
    return len(_cache)


def list_templates(conninfo: str | None = None) -> list[dict[str, Any]]:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"SELECT name, content, description, updated_at FROM {PROMPT_TABLE} ORDER BY name"
            )
            rows = cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


def get_one(name: str, conninfo: str | None = None) -> dict[str, Any] | None:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"SELECT name, content, description, updated_at FROM {PROMPT_TABLE} WHERE name = %s",
                (name,),
            )
            row = cursor.fetchone()
    return _row_to_dict(row) if row is not None else None


def upsert(
    name: str,
    content: str,
    description: str | None = None,
    conninfo: str | None = None,
) -> dict[str, Any]:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"""
                INSERT INTO {PROMPT_TABLE} (name, content, description, updated_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (name) DO UPDATE
                    SET content = EXCLUDED.content,
                        description = COALESCE(EXCLUDED.description, {PROMPT_TABLE}.description),
                        updated_at = CURRENT_TIMESTAMP
                RETURNING name, content, description, updated_at
                """,
                (name, content, description),
            )
            row = cursor.fetchone()
    reload(conninfo)
    return _row_to_dict(row)


def delete(name: str, conninfo: str | None = None) -> bool:
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            cursor.execute(
                f"DELETE FROM {PROMPT_TABLE} WHERE name = %s RETURNING name",
                (name,),
            )
            row = cursor.fetchone()
    reload(conninfo)
    return row is not None


def seed_from_dir(prompt_dir: str | Path, conninfo: str | None = None) -> list[dict[str, Any]]:
    """``prompt_dir`` 내 ``*.txt`` 프롬프트 파일을 테이블로 upsert한다.

    파일명(예: ``query_plan_system.txt``)을 그대로 ``name``으로 사용하므로
    ``graph_rag._read_prompt_template``의 조회 키와 정확히 일치한다.
    """
    directory = Path(prompt_dir)
    seeded: list[dict[str, Any]] = []
    with _connect(conninfo) as conn:
        with conn.cursor() as cursor:
            ensure_table(cursor)
            for path in sorted(directory.glob("*.txt")):
                content = path.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                cursor.execute(
                    f"""
                    INSERT INTO {PROMPT_TABLE} (name, content, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (name) DO UPDATE
                        SET content = EXCLUDED.content,
                            updated_at = CURRENT_TIMESTAMP
                    RETURNING name, content, description, updated_at
                    """,
                    (path.name, content),
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
