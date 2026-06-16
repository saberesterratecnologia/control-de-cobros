from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.comparator.sheet_reconciler import SheetReconciler
from src.models.pipeline import Allocation, ConciliatedPayment, DiscrepancyType
from src.models.source import BankMovement, Payment
from src.rules.allocation_engine import AllocationEngine


def _payment(*, id_pago: int = 1, amount: str = "98640.00") -> Payment:
    return Payment(
        id_pago_mp=id_pago,
        fecha=datetime(2026, 1, 10, 12, 0, 0),
        monto=Decimal(amount),
        nro_operacion=f"OP-{id_pago}",
        id_persona=1,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=2,
        id_movimiento_bancario=200 + id_pago,
        razon_social_originante="Juan Pérez",
        dni_cuit_originante="30111222",
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )


def _movement(*, id_mov: int = 201, amount: str = "98640.00") -> BankMovement:
    return BankMovement(
        id_movimiento=id_mov,
        id_cuenta_bancaria=1,
        id_persona=1,
        fecha=date(2026, 1, 10),
        referencia="OP-1",
        causal=None,
        concepto=None,
        importe=Decimal(amount),
        conciliado=False,
    )


def _allocation() -> Allocation:
    cp = ConciliatedPayment(payment=_payment(), movement=_movement(), conciliated_by="auto")
    return Allocation(
        payment=cp,
        concept="Cuota 1",
        amount=Decimal("98640.00"),
        generates_venta=True,
        generates_cobro=True,
    )


def _row(*, row: int, concepto: str = "Cuota 1", monto: str = "98640.00", tipo: str = "Cobro", dni: str = "30111222", comision: str = "Comisión 2026", fecha_val: date = date(2026, 1, 10), id_mov: int | None = 201, id_pago: int | None = 1):
    from src.models.sheet import SheetRow

    return SheetRow(
        row_number=row,
        organizacion="Org",
        curso="Curso",
        comision=comision,
        fecha_movimiento=fecha_val,
        tipo_movimiento=tipo,
        dni=dni,
        concepto=concepto,
        monto=Decimal(monto),
        medio_pago="Transferencia Bancaria" if tipo == "Cobro" else "No aplica",
        estudiante="Pérez Juan",
        estado_administrativo="Activo",
        estado_deuda=None,
        id_movimiento_bancario=id_mov,
        id_pago_mp=id_pago,
    )


def test_all_allocations_match_sheet_no_discrepancies() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile([_allocation()], [_row(row=2, tipo="Venta", id_mov=None), _row(row=3)], None)
    assert discrepancies == []


def test_missing_row_discrepancy() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile([_allocation()], [], None)
    assert any(d.discrepancy_type == DiscrepancyType.MISSING_ROW for d in discrepancies)


def test_wrong_value_discrepancy() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile([_allocation()], [_row(row=2, tipo="Venta", id_mov=None, monto="90000.00"), _row(row=3, monto="90000.00")], None)
    assert any(d.discrepancy_type == DiscrepancyType.WRONG_VALUE for d in discrepancies)


def test_same_numeric_monto_with_trailing_zeros_is_not_wrong_value() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile(
        [_allocation()],
        [_row(row=2, tipo="Venta", id_mov=None, monto="98640"), _row(row=3, monto="98640")],
        None,
    )
    assert discrepancies == []


def test_extra_row_discrepancy() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile([], [_row(row=2)], None)
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.EXTRA_ROW


def test_strong_match_works_all_fields_match() -> None:
    rec = SheetReconciler()
    assert rec.reconcile([_allocation()], [_row(row=2, tipo="Venta", id_mov=None), _row(row=3)], None) == []


def test_medium_match_works_without_concept_match() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile([_allocation()], [_row(row=2, tipo="Venta", concepto="Otra cosa", id_mov=None), _row(row=3, concepto="Otro concepto")], None)
    assert any(d.field == "concepto" for d in discrepancies)


def test_weak_match_works_with_dni_and_tipo() -> None:
    rec = SheetReconciler()
    discrepancies = rec.reconcile([_allocation()], [_row(row=2, tipo="Venta", concepto="X", monto="1.00", id_mov=None), _row(row=3, concepto="Y", monto="2.00")], None)
    assert any(d.field == "monto" for d in discrepancies)


def test_split_detection_corrects_combined_row_and_inserts_rest() -> None:
    """A sheet row with inscripcion+cuota combined should be split:
    correct the existing row to inscripcion and insert cuota as new."""
    pay = _payment(id_pago=10, amount="153440.00")
    mov = _movement(id_mov=210, amount="153440.00")
    cp = ConciliatedPayment(payment=pay, movement=mov, conciliated_by="existing")

    # AllocationEngine would split $153440 = $54800 (inscripcion) + $98640 (cuota)
    alloc_insc = Allocation(
        payment=cp,
        concept="Inscripción",
        amount=Decimal("54800.00"),
        generates_venta=True,
        generates_cobro=True,
    )
    alloc_cuota = Allocation(
        payment=cp,
        concept="Cuota 1",
        amount=Decimal("98640.00"),
        generates_venta=True,
        generates_cobro=True,
    )

    # Sheet has ONE combined row
    combined_row = _row(
        row=50,
        concepto="Inscripción",
        monto="153440.00",
        tipo="Cobro",
        id_pago=10,
        id_mov=210,
    )

    rec = SheetReconciler()
    discrepancies = rec.reconcile([alloc_insc, alloc_cuota], [combined_row], None)

    types = [d.discrepancy_type for d in discrepancies]

    # Should NOT have EXTRA_ROW (the combined row is consumed)
    assert DiscrepancyType.EXTRA_ROW not in types

    # Should have WRONG_VALUE for monto correction on the existing row
    monto_fixes = [d for d in discrepancies if d.discrepancy_type == DiscrepancyType.WRONG_VALUE and d.field == "monto"]
    assert len(monto_fixes) >= 1
    assert monto_fixes[0].expected_value == "54800"
    assert monto_fixes[0].actual_value == "153440"
    assert monto_fixes[0].actual_row is not None
    assert monto_fixes[0].actual_row.row_number == 50

    # Should have MISSING_ROWs for the remaining concepts (Cuota 1 Venta+Cobro, possibly Inscripcion Venta)
    missing = [d for d in discrepancies if d.discrepancy_type == DiscrepancyType.MISSING_ROW]
    missing_concepts = [d.expected_row.concepto for d in missing if d.expected_row]
    assert "Cuota 1" in missing_concepts


def test_next_venta_generates_missing_row_when_student_has_payments(sample_commission_with_prices, sample_student) -> None:
    from src.models.pipeline import ConciliatedPayment
    cp = ConciliatedPayment(
        payment=_payment(id_pago=10, amount="54800.00"),
        movement=_movement(id_mov=210, amount="54800.00"),
        conciliated_by="existing",
    )
    result = AllocationEngine(sample_commission_with_prices).allocate([cp], [], sample_student)
    rec = SheetReconciler()
    discrepancies = rec.reconcile([], [], result.next_venta)
    assert any(d.discrepancy_type == DiscrepancyType.MISSING_ROW for d in discrepancies)
