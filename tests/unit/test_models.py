from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.models.pipeline import Discrepancy, DiscrepancyType, PatchAction, PatchActionType, Severity
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import BankMovement, Payment, Student


def test_payment_valid_data() -> None:
    payment = Payment(
        id_pago_mp=10,
        fecha=datetime(2026, 5, 10, 12, 30, 0),
        monto=Decimal("12345.67"),
        nro_operacion="OP-001",
        id_persona=100,
        id_medio_pago=2,
        fecha_carga=datetime(2026, 5, 10, 13, 0, 0),
        controlado=False,
        comentario_cliente="Pago realizado",
        id_concepto_pago=1,
        id_movimiento_bancario=None,
        razon_social_originante="ACME SA",
        dni_cuit_originante="20123456789",
        controlado_auto=False,
        estado_conciliacion_auto="pendiente",
    )

    assert payment.id_pago_mp == 10
    assert payment.monto == Decimal("12345.67")


def test_payment_unlinked_with_minus_one() -> None:
    payment = Payment(
        id_pago_mp=11,
        fecha=datetime(2026, 5, 10, 12, 30, 0),
        monto=Decimal("100.00"),
        nro_operacion=None,
        id_persona=None,
        id_medio_pago=None,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=None,
        id_movimiento_bancario=-1,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )

    assert payment.id_movimiento_bancario == -1


def test_sheet_row_parsing_from_dict() -> None:
    row = SheetRow.model_validate(
        {
            "row_number": 7,
            "organizacion": "Org",
            "curso": "Curso",
            "comision": "Comision A",
            "fecha_movimiento": "2026-05-10",
            "tipo_movimiento": "Cobro",
            "dni": "30111222",
            "concepto": "Cuota 1",
            "monto": "5000.00",
            "medio_pago": "Transferencia Bancaria",
            "estudiante": "Perez Juan",
            "estado_administrativo": "Regular",
            "estado_deuda": "Sin deuda",
            "id_movimiento_bancario": 55,
            "id_pago_mp": 999,
        }
    )

    assert row.fecha_movimiento == date(2026, 5, 10)
    assert row.monto == Decimal("5000.00")


def test_bank_movement_parses_json_identification_string() -> None:
    movement = BankMovement(
        id_movimiento=55,
        id_cuenta_bancaria=3,
        id_persona=-1,
        fecha=date(2026, 5, 10),
        referencia="REF-55",
        causal="493",
        concepto="TRANSFERENCIA 20123456789",
        importe=Decimal("15000.00"),
        conciliado=False,
        json_identificacion='{"path_used":"cuit_dni","confidence":0.95}',
    )

    assert movement.json_identificacion == {
        "path_used": "cuit_dni",
        "confidence": 0.95,
    }





def test_student_cuit_as_dni_extracts_middle_8_digits() -> None:
    student = Student(
        id_persona=1,
        nombres="Juan",
        apellidos="Pérez",
        apellidos_nombres=None,
        dni="20314057458",
        email=None,
        id_estado_academico=None,
        id_estado_administrativo=None,
        eliminado=False,
    )

    assert student.dni == "31405745"
    assert student.dni_original == "20314057458"


def test_student_normal_dni_stays_unchanged() -> None:
    student = Student(
        id_persona=2,
        nombres="Ana",
        apellidos="López",
        apellidos_nombres=None,
        dni="30111222",
        email=None,
        id_estado_academico=None,
        id_estado_administrativo=None,
        eliminado=False,
    )

    assert student.dni == "30111222"
    assert student.dni_original == "30111222"


def test_expected_row_generation() -> None:
    payment = Payment(
        id_pago_mp=12,
        fecha=datetime(2026, 5, 10, 8, 0, 0),
        monto=Decimal("3000.00"),
        nro_operacion="REF-12",
        id_persona=200,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=True,
        comentario_cliente=None,
        id_concepto_pago=2,
        id_movimiento_bancario=99,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=True,
        estado_conciliacion_auto="confirmado",
    )

    expected = ExpectedRow(
        comision="Comision B",
        fecha_movimiento=date(2026, 5, 10),
        tipo_movimiento="Cobro",
        dni="33444555",
        concepto="Cuota 2",
        monto=Decimal("3000.00"),
        medio_pago="Mercado Pago",
        estudiante="Gomez Ana",
        id_movimiento_bancario=99,
        id_pago_mp=12,
        source_payment=payment,
        source_movement=None,
    )

    assert expected.id_pago_mp == payment.id_pago_mp
    assert expected.source_payment.monto == Decimal("3000.00")


def test_discrepancy_creation() -> None:
    discrepancy = Discrepancy(
        id="disc-1",
        commission="Comision C",
        dni="11222333",
        discrepancy_type=DiscrepancyType.WRONG_VALUE,
        field="monto",
        expected_value="2500.00",
        actual_value="2400.00",
        expected_row=None,
        actual_row=None,
        confidence=0.95,
        severity=Severity.WARNING,
        resolution=None,
        resolved_by=None,
    )

    assert discrepancy.discrepancy_type == DiscrepancyType.WRONG_VALUE
    assert discrepancy.field == "monto"


def test_patch_action_idempotency_key_generation() -> None:
    action = PatchAction(
        id="patch-1",
        action_type=PatchActionType.UPDATE_CELL,
        row_number=12,
        column="H",
        old_value="2400.00",
        new_value="2500.00",
        source_discrepancy_id="disc-1",
        status="planned",
    )

    assert len(action.idempotency_key) == 64
    assert action.idempotency_key
