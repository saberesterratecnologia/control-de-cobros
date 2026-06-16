from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.models.source import BankMovement, Commission, Payment, Student
from src.rules.mappers import is_excluded_medio, map_cobro_medio, map_concept, map_medio
from src.rules.row_builder_v1_deprecated import VentaCobroBuilder


def _commission() -> Commission:
    return Commission(
        id_comision=10,
        id_curso=60,
        id_organizacion=1,
        nombre="Comisión 2026",
        valor_inscripcion_promocion=Decimal("10000.00"),
        valor_cuota_bonificada=Decimal("15000.00"),
        cantidad_cuotas=12,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


def _student() -> Student:
    return Student(
        id_persona=100,
        nombres="Juan",
        apellidos="Pérez",
        apellidos_nombres=None,
        dni="30111222",
        email=None,
        id_estado_academico=None,
        id_estado_administrativo=None,
        eliminado=False,
    )


def _movement() -> BankMovement:
    return BankMovement(
        id_movimiento=999,
        id_cuenta_bancaria=1,
        id_persona=100,
        fecha=date(2026, 5, 11),
        referencia="REF-001",
        causal=None,
        concepto=None,
        importe=Decimal("15000.00"),
        conciliado=True,
    )


def _payment(concept_id: int, amount: str, medio: int = 2) -> Payment:
    return Payment(
        id_pago_mp=1,
        fecha=datetime(2026, 5, 10, 12, 30, 0),
        monto=Decimal(amount),
        nro_operacion="OP-001",
        id_persona=100,
        id_medio_pago=medio,
        fecha_carga=None,
        controlado=True,
        comentario_cliente=None,
        id_concepto_pago=concept_id,
        id_movimiento_bancario=999,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=True,
        estado_conciliacion_auto="confirmado",
    )


def test_concept_mapping_known_values() -> None:
    assert map_concept(0) == "NO DEFINIDO"
    assert map_concept(1) == "Inscripción"
    assert map_concept(2, cuota_number=3) == "Cuota 3"
    assert map_concept(3) == "Derecho Examen"
    assert map_concept(4) == "Recargo"
    assert map_concept(5) == "Inscripción Seminario"
    assert map_concept(6) == "Certificación"


def test_concept_mapping_unknown_returns_no_definido() -> None:
    assert map_concept(99) == "NO DEFINIDO"


def test_medio_mapping_and_excluded_medio() -> None:
    assert map_medio(1) == "Transferencia Bancaria"
    assert map_medio(2) == "Mercado Pago"
    assert map_medio(3) == "Pago Profesor"
    assert map_cobro_medio(2, has_bank_movement=True) == "Transferencia Bancaria"
    assert map_cobro_medio(3, has_bank_movement=True) == "Pago Profesor"
    assert is_excluded_medio(3) is True
    assert is_excluded_medio(1) is False


def test_simple_inscription_payment_generates_venta_and_cobro() -> None:
    builder = VentaCobroBuilder(_commission())
    payment = _payment(concept_id=1, amount="10000.00")
    rows = builder.build_expected_rows(payment=payment, movement=_movement(), student=_student())

    assert len(rows) == 2
    assert rows[0].tipo_movimiento == "Venta"
    assert rows[0].medio_pago == "No aplica"
    assert rows[1].tipo_movimiento == "Cobro"
    assert rows[1].medio_pago == "Mercado Pago"
    assert rows[1].id_movimiento_bancario == 999


def test_simple_cuota_payment_generates_cuota_n_rows() -> None:
    builder = VentaCobroBuilder(_commission())
    payment = _payment(concept_id=2, amount="15000.00")
    rows = builder.build_expected_rows(
        payment=payment,
        movement=_movement(),
        student=_student(),
        cuota_number=2,
    )

    assert len(rows) == 2
    assert rows[0].concepto == "Cuota 2"
    assert rows[1].concepto == "Cuota 2"


def test_combined_payment_generates_two_ventas_and_two_cobros() -> None:
    builder = VentaCobroBuilder(_commission())
    payment = _payment(concept_id=0, amount="25000.00")
    rows = builder.build_expected_rows(
        payment=payment,
        movement=_movement(),
        student=_student(),
        cuota_number=1,
    )

    assert len(rows) == 4
    ventas = [r for r in rows if r.tipo_movimiento == "Venta"]
    cobros = [r for r in rows if r.tipo_movimiento == "Cobro"]
    assert len(ventas) == 2
    assert len(cobros) == 2
    assert {r.concepto for r in rows} == {"Inscripción", "Cuota 1"}


def test_unconciliated_payment_generates_only_venta() -> None:
    builder = VentaCobroBuilder(_commission())
    payment = _payment(concept_id=1, amount="10000.00")
    rows = builder.build_expected_rows(payment=payment, movement=None, student=_student())

    assert len(rows) == 1
    assert rows[0].tipo_movimiento == "Venta"


def test_determine_cuota_number_from_history() -> None:
    builder = VentaCobroBuilder(_commission())
    student = _student()
    history = [
        _payment(concept_id=1, amount="10000.00"),
        _payment(concept_id=2, amount="15000.00"),
        _payment(concept_id=2, amount="15000.00"),
    ]

    assert builder.determine_cuota_number(student, history) == 3


def test_detect_combined_payment_matching_and_non_matching() -> None:
    commission = _commission()
    builder = VentaCobroBuilder(commission)

    only_inscripcion = builder.detect_combined_payment(_payment(1, "10000.00"), commission)
    assert only_inscripcion == [("Inscripción", Decimal("10000.00"))]

    only_cuota = builder.detect_combined_payment(_payment(2, "15000.00"), commission)
    assert only_cuota == [("Cuota", Decimal("15000.00"))]

    combined = builder.detect_combined_payment(_payment(0, "25000.00"), commission)
    assert combined == [
        ("Inscripción", Decimal("10000.00")),
        ("Cuota", Decimal("15000.00")),
    ]

    non_matching = builder.detect_combined_payment(_payment(0, "12345.00"), commission)
    assert non_matching == []


def test_detect_combined_payment_handles_none_prices() -> None:
    commission = _commission().model_copy(
        update={"valor_inscripcion_promocion": None, "valor_cuota_bonificada": None}
    )
    builder = VentaCobroBuilder(commission)

    assert builder.detect_combined_payment(_payment(0, "25000.00"), commission) == []
