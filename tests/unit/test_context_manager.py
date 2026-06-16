from __future__ import annotations

from pathlib import Path

from src.context.context_manager import ContextManager
from src.models.pipeline import (
    Discrepancy,
    DiscrepancyType,
    PatchAction,
    PatchActionType,
    Resolution,
    Severity,
)


def _schema_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "src" / "context" / "schema.sql")


def test_run_lifecycle_start_end_and_current() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run(mode="dry_run")
        current = manager.get_current_run()

        assert current is not None
        assert current["id"] == run_id
        assert current["status"] == "running"

        manager.end_run(run_id, status="completed", summary={"ok": True})
        assert manager.get_current_run() is None


def test_discrepancy_save_and_retrieve() -> None:
    discrepancy = Discrepancy(
        id="disc-1",
        commission="C1",
        dni="30111222",
        discrepancy_type=DiscrepancyType.WRONG_VALUE,
        field="monto",
        expected_value="15000",
        actual_value="14500",
        expected_row=None,
        actual_row=None,
        confidence=0.9,
        severity=Severity.WARNING,
        resolution=Resolution.AUTO_FIX,
        resolved_by="rules",
    )

    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_discrepancy(run_id, discrepancy)

        rows = manager.get_discrepancies(run_id)
        assert len(rows) == 1
        assert rows[0]["dni"] == "30111222"
        assert rows[0]["discrepancy_type"] == "wrong_value"


def test_decision_cache_lookup_by_hash() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_decision(
            run_id=run_id,
            discrepancy_id=None,
            input_hash="hash-abc",
            model_used="gpt-4o-mini",
            decision_json={"action": "correct"},
            confidence=0.92,
        )

        cached = manager.get_decision_by_hash("hash-abc")
        assert cached is not None
        assert cached["model_used"] == "gpt-4o-mini"


def test_patch_idempotency_check() -> None:
    patch = PatchAction(
        id="patch-1",
        action_type=PatchActionType.UPDATE_CELL,
        row_number=3,
        column="H",
        old_value="100",
        new_value="200",
        source_discrepancy_id="disc-1",
        status="applied",
    )

    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_patch(run_id, patch)

        assert manager.is_already_applied(patch.idempotency_key) is True
        assert manager.is_already_applied("not-existing") is False


def test_checkpoint_save_and_restore() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_checkpoint(
            run_id=run_id,
            phase="write",
            checkpoint_data={"last_patch": 50},
            commission_name="Comisión A",
        )

        checkpoint = manager.get_last_checkpoint(run_id, phase="write")
        assert checkpoint is not None
        assert checkpoint["phase"] == "write"
