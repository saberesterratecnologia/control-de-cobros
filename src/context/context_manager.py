"""SQLite-backed runtime context store for reconciliation runs."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.models.pipeline import Discrepancy, PatchAction

LOGGER = logging.getLogger(__name__)


class ContextManagerError(RuntimeError):
    """Raised for runtime context persistence errors."""


class ContextManager:
    """Manage local SQLite state for runs, snapshots, decisions and patches."""

    def __init__(self, db_path: str, schema_path: str | None = None) -> None:
        self.db_path = db_path
        self.schema_path = schema_path or str(Path(__file__).with_name("schema.sql"))
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "ContextManager":
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self._initialize_schema()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.connection is None:
            return
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()
        self.connection = None

    def _initialize_schema(self) -> None:
        conn = self._require_connection()
        try:
            schema = Path(self.schema_path).read_text(encoding="utf-8")
            conn.executescript(schema)
            conn.commit()
        except (OSError, sqlite3.Error) as error:
            LOGGER.exception("failed to initialize sqlite schema")
            raise ContextManagerError("Unable to initialize SQLite schema") from error

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise ContextManagerError("ContextManager is not connected. Use it as a context manager.")
        return self.connection

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat()

    def start_run(self, mode: str = "dry_run", config_snapshot: dict[str, Any] | None = None) -> str:
        conn = self._require_connection()
        run_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO runs (id, started_at, status, mode, config_snapshot)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, self._now(), "running", mode, json.dumps(config_snapshot or {})),
        )
        conn.commit()
        return run_id

    def end_run(self, run_id: str, status: str = "completed", summary: dict[str, Any] | None = None) -> None:
        conn = self._require_connection()
        conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, summary_json = ?
            WHERE id = ?
            """,
            (self._now(), status, json.dumps(summary or {}), run_id),
        )
        conn.commit()

    def get_current_run(self) -> dict[str, Any] | None:
        conn = self._require_connection()
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._require_connection()
        row = conn.execute("SELECT * FROM runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def save_snapshot(self, run_id: str, row_number: int, raw_json: dict[str, Any], hash_value: str) -> None:
        conn = self._require_connection()
        conn.execute(
            """
            INSERT INTO sheet_snapshots (run_id, captured_at, row_number, raw_json, hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, self._now(), row_number, json.dumps(raw_json), hash_value),
        )
        conn.commit()

    def get_snapshot(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._require_connection()
        rows = conn.execute(
            "SELECT * FROM sheet_snapshots WHERE run_id = ? ORDER BY row_number", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def save_discrepancy(self, run_id: str, discrepancy: Discrepancy) -> int:
        conn = self._require_connection()
        cursor = conn.execute(
            """
            INSERT INTO discrepancies (
                run_id, commission, dni, discrepancy_type, field,
                expected_value, actual_value, confidence, resolution,
                resolved_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                discrepancy.commission,
                discrepancy.dni,
                discrepancy.discrepancy_type.value,
                discrepancy.field,
                discrepancy.expected_value,
                discrepancy.actual_value,
                discrepancy.confidence,
                discrepancy.resolution.value if discrepancy.resolution else None,
                discrepancy.resolved_by,
                self._now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def get_discrepancies(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._require_connection()
        rows = conn.execute(
            "SELECT * FROM discrepancies WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def save_decision(
        self,
        run_id: str,
        discrepancy_id: int | None,
        input_hash: str,
        model_used: str,
        decision_json: dict[str, Any],
        confidence: float,
        raw_response: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> int:
        conn = self._require_connection()
        cursor = conn.execute(
            """
            INSERT INTO decisions (
                run_id, discrepancy_id, input_hash, model_used,
                prompt_tokens, completion_tokens, raw_response,
                decision_json, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                discrepancy_id,
                input_hash,
                model_used,
                prompt_tokens,
                completion_tokens,
                raw_response,
                json.dumps(decision_json),
                confidence,
                self._now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def get_decision_by_hash(self, input_hash: str) -> dict[str, Any] | None:
        conn = self._require_connection()
        row = conn.execute(
            "SELECT * FROM decisions WHERE input_hash = ? ORDER BY id DESC LIMIT 1", (input_hash,)
        ).fetchone()
        return dict(row) if row else None

    def save_patch(self, run_id: str, patch: PatchAction) -> int:
        conn = self._require_connection()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO patch_actions (
                run_id, action_type, row_number, column,
                old_value, new_value, status, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                patch.action_type.value,
                patch.row_number,
                patch.column,
                patch.old_value,
                patch.new_value,
                patch.status,
                patch.idempotency_key,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def update_patch_status(self, patch_id: int, status: str) -> None:
        conn = self._require_connection()
        conn.execute(
            "UPDATE patch_actions SET status = ?, applied_at = ? WHERE id = ?",
            (status, self._now(), patch_id),
        )
        conn.commit()

    def get_pending_patches(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._require_connection()
        rows = conn.execute(
            "SELECT * FROM patch_actions WHERE run_id = ? AND status = 'planned' ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _has_open_ambiguous_review_for_payment(self, payment_id: int) -> int | None:
        """Return the id of an existing open ambiguous review for this payment_id, or None."""
        conn = self._require_connection()
        row = conn.execute(
            """
            SELECT id FROM pending_reviews
            WHERE status = 'open'
              AND reason LIKE '%ambiguous%'
              AND json_extract(context_json, '$.payment_id') = ?
            LIMIT 1
            """,
            (payment_id,),
        ).fetchone()
        return row[0] if row else None

    def save_pending_review(
        self,
        run_id: str,
        discrepancy_id: int | None,
        reason: str,
        context_json: dict[str, Any] | None = None,
        status: str = "open",
    ) -> int:
        conn = self._require_connection()

        # Creation-time dedup guard: skip insert for duplicate ambiguous reviews
        if "ambiguous" in reason and context_json and context_json.get("payment_id"):
            existing_id = self._has_open_ambiguous_review_for_payment(context_json["payment_id"])
            if existing_id is not None:
                return existing_id

        cursor = conn.execute(
            """
            INSERT INTO pending_reviews (run_id, discrepancy_id, reason, context_json, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, discrepancy_id, reason, json.dumps(context_json or {}), status),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def get_pending_reviews(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._require_connection()
        rows = conn.execute(
            "SELECT * FROM pending_reviews WHERE run_id = ? AND status = 'open' ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all_open_reviews(self, run_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._require_connection()
        if run_id is None:
            rows = conn.execute(
                "SELECT * FROM pending_reviews WHERE status = 'open' ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pending_reviews WHERE status = 'open' AND run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_review_by_id(self, pending_review_id: int) -> dict[str, Any] | None:
        conn = self._require_connection()
        row = conn.execute(
            "SELECT * FROM pending_reviews WHERE id = ? LIMIT 1",
            (pending_review_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_pending_review_resolution(
        self,
        pending_review_id: int,
        reviewer_notes: str,
        status: str = "resolved",
    ) -> None:
        conn = self._require_connection()
        conn.execute(
            """
            UPDATE pending_reviews
            SET status = ?, reviewer_notes = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (status, reviewer_notes, self._now(), pending_review_id),
        )
        conn.commit()

    def save_review_resolution(
        self,
        case_id: str,
        run_id: str | None,
        commission: str,
        dni: str,
        problem: str,
        resolution: str,
        monto: float | None,
        concepto_tipo: str | None,
        pricing_inscripcion: float | None,
        pricing_cuota: float | None,
        monto_ratio: float | None,
    ) -> int:
        conn = self._require_connection()
        cursor = conn.execute(
            """
            INSERT INTO review_resolutions (
                case_id,
                run_id,
                commission,
                dni,
                problem,
                resolution,
                monto,
                concepto_tipo,
                pricing_inscripcion,
                pricing_cuota,
                monto_ratio,
                resolved_at,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                run_id = excluded.run_id,
                commission = excluded.commission,
                dni = excluded.dni,
                problem = excluded.problem,
                resolution = excluded.resolution,
                monto = excluded.monto,
                concepto_tipo = excluded.concepto_tipo,
                pricing_inscripcion = excluded.pricing_inscripcion,
                pricing_cuota = excluded.pricing_cuota,
                monto_ratio = excluded.monto_ratio,
                resolved_at = excluded.resolved_at
            """,
            (
                case_id,
                run_id,
                commission,
                dni,
                problem,
                resolution,
                monto,
                concepto_tipo,
                pricing_inscripcion,
                pricing_cuota,
                monto_ratio,
                self._now(),
                self._now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def has_review_resolution(self, case_id: str) -> bool:
        conn = self._require_connection()
        row = conn.execute(
            "SELECT 1 FROM review_resolutions WHERE case_id = ? LIMIT 1",
            (case_id,),
        ).fetchone()
        return row is not None

    def find_similar_resolutions(
        self,
        monto_ratio: float,
        concepto_tipo: str,
        tolerance: float = 0.05,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        conn = self._require_connection()
        rows = conn.execute(
            """
            SELECT *
            FROM review_resolutions
            WHERE concepto_tipo = ?
              AND monto_ratio IS NOT NULL
              AND ABS(monto_ratio - ?) <= ?
            ORDER BY resolved_at DESC
            LIMIT ?
            """,
            (concepto_tipo, monto_ratio, tolerance, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_checkpoint(
        self,
        run_id: str,
        phase: str,
        checkpoint_data: dict[str, Any],
        commission_name: str | None = None,
        student_dni: str | None = None,
        status: str = "in_progress",
    ) -> int:
        conn = self._require_connection()
        cursor = conn.execute(
            """
            INSERT INTO sync_state (
                run_id, commission_name, student_dni, phase,
                status, checkpoint_data, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                commission_name,
                student_dni,
                phase,
                status,
                json.dumps(checkpoint_data),
                self._now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def get_last_checkpoint(self, run_id: str, phase: str | None = None) -> dict[str, Any] | None:
        conn = self._require_connection()
        if phase is None:
            query = "SELECT * FROM sync_state WHERE run_id = ? ORDER BY updated_at DESC LIMIT 1"
            args: tuple[Any, ...] = (run_id,)
        else:
            query = (
                "SELECT * FROM sync_state WHERE run_id = ? AND phase = ? "
                "ORDER BY updated_at DESC LIMIT 1"
            )
            args = (run_id, phase)
        row = conn.execute(query, args).fetchone()
        return dict(row) if row else None

    def is_already_applied(self, idempotency_key: str) -> bool:
        conn = self._require_connection()
        row = conn.execute(
            """
            SELECT 1
            FROM patch_actions
            WHERE idempotency_key = ? AND status IN ('applied', 'skipped')
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def save_rollback_snapshot(
        self,
        run_id: str,
        action_id: str,
        action_type: str,
        row_number: int | None,
        row_snapshot: str | list[str] | dict[str, Any] | None,
    ) -> int:
        conn = self._require_connection()
        cursor = conn.execute(
            """
            INSERT INTO rollback_snapshots (
                run_id, action_id, action_type, row_number, row_snapshot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                action_id,
                action_type,
                row_number,
                json.dumps(row_snapshot) if row_snapshot is not None else None,
                self._now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def get_rollback_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        conn = self._require_connection()
        rows = conn.execute(
            "SELECT * FROM rollback_snapshots WHERE run_id = ? ORDER BY id DESC",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Stale-review evaluation helpers ---

    def get_open_reviews_for_student(self, commission: str, dni: str) -> list[dict[str, Any]]:
        """Return open guard reviews matching commission+DNI via json_extract."""
        conn = self._require_connection()
        rows = conn.execute(
            """
            SELECT * FROM pending_reviews
            WHERE status = 'open'
              AND reason LIKE 'guard:%'
              AND json_extract(context_json, '$.commission') = ?
              AND json_extract(context_json, '$.dni') = ?
            ORDER BY id
            """,
            (commission, dni),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_open_anomaly_reviews_for_rows(self, row_numbers: list[int]) -> list[dict[str, Any]]:
        """Return open anomaly reviews whose row_number is in the given set."""
        if not row_numbers:
            return []
        conn = self._require_connection()
        placeholders = ",".join("?" * len(row_numbers))
        rows = conn.execute(
            f"""
            SELECT * FROM pending_reviews
            WHERE status = 'open'
              AND reason LIKE 'anomaly:%'
              AND json_extract(context_json, '$.row_number') IN ({placeholders})
            ORDER BY id
            """,
            row_numbers,
        ).fetchall()
        return [dict(row) for row in rows]

    def close_review(self, review_id: int, reason: str) -> None:
        """Mark review as resolved with auto-close reason."""
        self.update_pending_review_resolution(review_id, reviewer_notes=reason, status="resolved")
