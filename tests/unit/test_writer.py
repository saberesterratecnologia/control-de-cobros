from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from src.context.context_manager import ContextManager
from src.models.pipeline import PatchAction, PatchActionType
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import Payment
from src.writer.patch_builder import PatchBuilder
from src.writer.sheet_writer import SheetWriter


def _schema_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "src" / "context" / "schema.sql")


def _payment() -> Payment:
    return Payment(
        id_pago_mp=900,
        fecha=datetime(2026, 5, 10, 10, 0, 0),
        monto=Decimal("54500"),
        nro_operacion="op-1",
        id_persona=10,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=True,
        comentario_cliente=None,
        id_concepto_pago=2,
        id_movimiento_bancario=333,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=True,
        estado_conciliacion_auto="ok",
    )


def _expected_row() -> ExpectedRow:
    return ExpectedRow(
        comision="Com 10",
        fecha_movimiento=datetime(2026, 5, 10).date(),
        tipo_movimiento="Cobro",
        dni="30111222",
        concepto="Cuota 1",
        monto=Decimal("54500"),
        medio_pago="Transferencia",
        estudiante="Juan Perez",
        id_movimiento_bancario=333,
        id_pago_mp=900,
        source_payment=_payment(),
        source_movement=None,
    )


def _sheet_row(
    *,
    row_number: int = 20,
    concepto: str = "Cuota 1",
    monto: str = "54500",
    id_movimiento_bancario: int | None = 333,
    id_pago_mp: int | None = 900,
) -> SheetRow:
    return SheetRow(
        row_number=row_number,
        organizacion=None,
        curso=None,
        comision="Com 10",
        fecha_movimiento=date(2026, 5, 10),
        tipo_movimiento="Cobro",
        dni="30111222",
        concepto=concepto,
        monto=Decimal(monto),
        medio_pago="Transferencia",
        estudiante="Juan Perez",
        estado_administrativo=None,
        estado_deuda=None,
        id_movimiento_bancario=id_movimiento_bancario,
        id_pago_mp=id_pago_mp,
    )


def test_patch_builder_add_insert_generates_idempotency_key() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        builder = PatchBuilder(manager)
        action = builder.add_insert(_expected_row(), "disc-1")
        assert action.action_type == PatchActionType.INSERT_ROW
        assert len(action.idempotency_key) == 64


def test_patch_builder_add_update_keeps_column_mapping() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        builder = PatchBuilder(manager)
        action = builder.add_update(156, "H", "$54.000", "$54.500", "monto mismatch")
        assert action.action_type == PatchActionType.UPDATE_CELL
        assert action.column == "H"
        assert action.row_number == 156


def test_patch_builder_skips_already_applied_actions() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        action = PatchAction(
            id="existing",
            action_type=PatchActionType.UPDATE_CELL,
            row_number=10,
            column="H",
            old_value="1",
            new_value="2",
            source_discrepancy_id="disc-x",
            status="applied",
        )
        manager.save_patch(run_id, action)

        builder = PatchBuilder(manager)
        builder.actions.append(action)
        plan = builder.build_plan()
        assert plan == []


def test_patch_builder_skips_duplicate_idempotency_within_same_run() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        builder = PatchBuilder(manager)
        a1 = builder.add_update(10, "H", "$54.000", "$54.500", "disc-1")
        a2 = builder.add_update(10, "H", "$54.000", "$54.500", "disc-2")

        plan = builder.build_plan()

        assert len(plan) == 1
        assert plan[0].id == a1.id
        assert a2.status == "skipped"


def test_patch_builder_summary_counts() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        builder = PatchBuilder(manager)
        builder.add_insert(_expected_row(), "disc-1")
        builder.add_update(1, "H", "100", "200", "disc-2")
        builder.add_flag(2, "disc-3", "needs review")
        summary = builder.get_summary()
        assert summary["total_actions"] == 3
        assert summary["by_type"]["insert_row"] == 1
        assert summary["by_type"]["update_cell"] == 1
        assert summary["by_type"]["flag_review"] == 1


def test_patch_builder_add_delete_uses_row_snapshot_for_idempotency() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        builder = PatchBuilder(manager)
        first = builder.add_delete(_sheet_row(row_number=50, concepto="Cuota 1"), "disc-1")
        second = builder.add_delete(_sheet_row(row_number=50, concepto="Cuota 2"), "disc-2")

        assert first.idempotency_key != second.idempotency_key
        assert first.old_value is not None
        assert "Cuota 1" in first.old_value


def test_patch_builder_delete_does_not_skip_new_row_with_reused_row_number() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        old_delete = PatchBuilder(manager).add_delete(_sheet_row(row_number=50, concepto="Cuota 1"), "disc-old")
        patch_id = manager.save_patch(run_id, old_delete)
        manager.update_patch_status(patch_id, "applied")

        builder = PatchBuilder(manager)
        new_delete = builder.add_delete(_sheet_row(row_number=50, concepto="Cuota 2"), "disc-new")
        plan = builder.build_plan()

        assert plan == [new_delete]


def test_sheet_writer_dry_run_generates_readable_report() -> None:
    sheets = MagicMock()
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        writer = SheetWriter(sheets, manager, {"agent": {"batch_size": 50, "checkpoint_interval": 2}})
        builder = PatchBuilder(manager)
        builder.add_insert(_expected_row(), "disc-insert")
        builder.add_update(156, "H", "$54.000", "$54.500", "monto mismatch")
        builder.add_flag(200, "disc-flag", "large amount discrepancy")

        report = writer.execute_dry_run(builder.actions)
        assert "=== DRY RUN REPORT ===" in report
        assert "[INSERT]" in report
        assert "[UPDATE]" in report
        assert "[FLAG]" in report
        assert "$54.500" in report


def test_sheet_writer_live_calls_batch_update_and_marks_applied() -> None:
    sheets = MagicMock()
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        manager.start_run(mode="live")
        writer = SheetWriter(sheets, manager, {"agent": {"batch_size": 50, "checkpoint_interval": 50}})
        builder = PatchBuilder(manager)
        action = builder.add_update(20, "H", "100", "200", "disc-1")

        summary = writer.execute_live([action])
        assert summary["applied"] == 1
        assert sheets.batch_update.called
        pending = manager.get_pending_patches(manager.get_current_run()["id"])
        assert pending == []


def test_sheet_writer_live_handles_individual_failures() -> None:
    sheets = MagicMock()
    sheets.batch_update.side_effect = [Exception("boom"), {"ok": True}]
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        manager.start_run(mode="live")
        writer = SheetWriter(sheets, manager, {"agent": {"batch_size": 50, "checkpoint_interval": 50}})
        builder = PatchBuilder(manager)
        a1 = builder.add_update(10, "H", "1", "2", "disc-1")
        a2 = builder.add_update(11, "H", "1", "3", "disc-2")

        summary = writer.execute_live([a1, a2])
        assert summary["failed"] == 1
        assert summary["applied"] == 1
        assert len(summary["errors"]) == 1


def test_sheet_writer_live_checkpoints_at_intervals() -> None:
    sheets = MagicMock()
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        manager.start_run(mode="live")
        manager.save_checkpoint = MagicMock(wraps=manager.save_checkpoint)
        writer = SheetWriter(sheets, manager, {"agent": {"batch_size": 10, "checkpoint_interval": 2}})
        builder = PatchBuilder(manager)
        actions = [builder.add_update(i, "H", "1", str(i), f"disc-{i}") for i in range(1, 5)]

        writer.execute_live(actions)
        assert manager.save_checkpoint.call_count == 2


def test_idempotency_same_plan_twice_skips_second_run() -> None:
    sheets = MagicMock()
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        manager.start_run(mode="live")
        writer = SheetWriter(sheets, manager, {"agent": {"batch_size": 50, "checkpoint_interval": 50}})
        builder = PatchBuilder(manager)
        action = builder.add_update(20, "H", "100", "200", "disc-1")

        first = writer.execute_live([action])
        second = writer.execute_live([action])
        assert first["applied"] == 1
        assert second["applied"] == 0
        assert second["skipped"] == 1
