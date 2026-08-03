"""Read-only PostgreSQL adapter for capability failure diagnostics.

The adapter intentionally selects only technical diagnostic columns.  Prompts,
generated SQL, and database result data never leave the failure-log database
through this interface.  It also starts a read-only transaction and never
creates or alters the logging table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_TECHNICAL_COLUMNS = (
    "failure_log_id",
    "failure_reason",
    "query_plan",
    "missing_input_conditions",
    "clarification_questions",
    "stage_log",
    "context_metadata",
    "created_at",
)


class FailureLogReadError(RuntimeError):
    """Raised when the optional diagnostic log source cannot be read."""


@dataclass(frozen=True)
class PsycopgFailureLogProvider:
    """Load recent technical failure records without mutation authority."""

    conninfo: str = field(repr=False)
    default_limit: int = 1_000
    max_limit: int = 5_000
    connect_timeout_seconds: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.conninfo, str) or not self.conninfo.strip():
            raise ValueError("conninfo must be a non-empty string")
        for name, value in (
            ("default_limit", self.default_limit),
            ("max_limit", self.max_limit),
            ("connect_timeout_seconds", self.connect_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit must not exceed max_limit")

    def load_failure_rows(
        self, limit: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        requested = self.default_limit if limit is None else limit
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise ValueError("limit must be an integer")
        if requested < 1 or requested > self.max_limit:
            raise ValueError(f"limit must be between 1 and {self.max_limit}")

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise FailureLogReadError("psycopg is unavailable") from exc

        try:
            with psycopg.connect(
                self.conninfo,
                row_factory=dict_row,
                connect_timeout=self.connect_timeout_seconds,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        """
                        SELECT failure_log_id,
                               failure_reason,
                               query_plan,
                               missing_input_conditions,
                               clarification_questions,
                               stage_log,
                               context_metadata,
                               created_at
                        FROM campaign_query_failure_logs
                        ORDER BY created_at DESC, failure_log_id DESC
                        LIMIT %s
                        """,
                        (requested,),
                    )
                    # Project again at the trust boundary.  PostgreSQL will
                    # normally return exactly the SELECT list, but defensive
                    # projection prevents a non-standard cursor/adapter from
                    # leaking an unexpected prompt, SQL, or result column.
                    return tuple(
                        {
                            column: row[column]
                            for column in _TECHNICAL_COLUMNS
                            if column in row
                        }
                        for row in cursor.fetchall()
                    )
        except Exception as exc:  # noqa: BLE001 - optional source is fail-open upstream
            raise FailureLogReadError(
                f"failure log read failed: {exc.__class__.__name__}"
            ) from exc


__all__ = ["FailureLogReadError", "PsycopgFailureLogProvider"]
