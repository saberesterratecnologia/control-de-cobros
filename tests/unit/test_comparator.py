from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.comparator.diff_engine import DiffEngine
from src.comparator.scorer import ConfidenceScorer
from src.models.pipeline import DiscrepancyType, Severity
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import Payment


def _payment(id_pago_mp: int, monto: str, id_movimiento: int | None) -> Payment:
    return Payment(
        id_pago_mp=id_pago_mp,
        fecha=datetime(2026, 5, 10, 12, 0, 0),
        monto=Decimal(monto),
        nro_operacion=f"OP-{id_pago_mp}",
        id_persona=100,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=1,
        id_movimiento_bancario=id_movimiento,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )


def _expected(
    *,
    id_pago_mp: int,
    id_movimiento: int | None,
    dni: str = "30111222",
    concepto: str = "Cuota 1",
    monto: str = "10000.00",
    medio_pago: str = "Transferencia",
    fecha: date = date(2026, 5, 10),
    tipo: str = "Cobro",
) -> ExpectedRow:
    return ExpectedRow(
        comision="Comision A",
        fecha_movimiento=fecha,
        tipo_movimiento=tipo,
        dni=dni,
        concepto=concepto,
        monto=Decimal(monto),
        medio_pago=medio_pago,
        estudiante="Perez Juan",
        id_movimiento_bancario=id_movimiento,
        id_pago_mp=id_pago_mp,
        source_payment=_payment(id_pago_mp, monto, id_movimiento),
        source_movement=None,
    )


def _actual(
    *,
    row_number: int,
    id_pago_mp: int | None,
    id_movimiento: int | None,
    dni: str = "30111222",
    concepto: str = "Cuota 1",
    monto: str = "10000.00",
    medio_pago: str = "Transferencia",
    fecha: date | None = date(2026, 5, 10),
    tipo: str = "Cobro",
) -> SheetRow:
    return SheetRow(
        row_number=row_number,
        organizacion="Org",
        curso="Curso",
        comision="Comision A",
        fecha_movimiento=fecha,
        tipo_movimiento=tipo,
        dni=dni,
        concepto=concepto,
        monto=Decimal(monto),
        medio_pago=medio_pago,
        estudiante="Perez Juan",
        estado_administrativo="Regular",
        estado_deuda="Sin deuda",
        id_movimiento_bancario=id_movimiento,
        id_pago_mp=id_pago_mp,
    )


def test_all_expected_rows_match_actual_no_discrepancies() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=1, id_movimiento=10)]
    actual = [_actual(row_number=2, id_pago_mp=1, id_movimiento=10)]

    assert engine.compare(expected, actual) == []


def test_missing_row_in_sheet_reports_missing_discrepancy() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=1, id_movimiento=10)]

    discrepancies = engine.compare(expected, [])

    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.MISSING_ROW


def test_wrong_monto_reports_wrong_value_and_confidence() -> None:
    engine = DiffEngine()
    scorer = ConfidenceScorer()
    expected = [_expected(id_pago_mp=1, id_movimiento=10, monto="10000.00")]
    actual = [_actual(row_number=2, id_pago_mp=1, id_movimiento=10, monto="10400.00")]

    discrepancies = engine.compare(expected, actual)
    monto_discrepancy = next(d for d in discrepancies if d.field == "monto")

    assert monto_discrepancy.discrepancy_type == DiscrepancyType.WRONG_VALUE
    assert scorer.score(monto_discrepancy) == 0.92  # ≤10% diff → auto-fix


def test_wrong_concepto_reports_wrong_value() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=1, id_movimiento=10, concepto="Cuota 1")]
    actual = [_actual(row_number=2, id_pago_mp=1, id_movimiento=10, concepto="Cuota 2")]

    discrepancies = engine.compare(expected, actual)

    assert len(discrepancies) == 1
    assert discrepancies[0].field == "concepto"
    assert discrepancies[0].discrepancy_type == DiscrepancyType.WRONG_VALUE


def test_extra_row_not_in_db_reports_extra_discrepancy() -> None:
    engine = DiffEngine()
    actual = [_actual(row_number=2, id_pago_mp=999, id_movimiento=77)]

    discrepancies = engine.compare([], actual)

    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.EXTRA_ROW


def test_duplicate_rows_detected() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=1, id_movimiento=10)]
    actual = [
        _actual(row_number=2, id_pago_mp=1, id_movimiento=10),
        _actual(row_number=3, id_pago_mp=1, id_movimiento=10),
    ]

    discrepancies = engine.compare(expected, actual)

    assert any(d.discrepancy_type == DiscrepancyType.DUPLICATE for d in discrepancies)


def test_primary_key_matching_works_for_venta() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=3, id_movimiento=None, tipo="Venta", concepto="Inscripcion")]
    actual = [_actual(row_number=2, id_pago_mp=3, id_movimiento=None, tipo="Venta", concepto="Inscripcion")]

    discrepancies = engine.compare(expected, actual)

    assert discrepancies == []


def test_fallback_matching_when_ids_missing() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=5, id_movimiento=88, monto="12000.00")]
    actual = [
        _actual(
            row_number=2,
            id_pago_mp=None,
            id_movimiento=None,
            monto="12000.00",
            fecha=date(2026, 5, 12),
        )
    ]

    discrepancies = engine.compare(expected, actual)

    # Fallback matches by (dni, monto, medio_pago, concepto, date±3d),
    # but _compare_fields then detects the date difference as WRONG_VALUE.
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.WRONG_VALUE
    assert discrepancies[0].field == "fecha_movimiento"


def test_confidence_high_for_exact_id_missing_match() -> None:
    scorer = ConfidenceScorer()
    engine = DiffEngine()
    expected = _expected(id_pago_mp=20, id_movimiento=101)
    discrepancy = engine._build_discrepancy(  # noqa: SLF001
        discrepancy_type=DiscrepancyType.MISSING_ROW,
        expected=expected,
        actual=None,
        field=None,
        expected_value=None,
        actual_value=None,
    )

    assert scorer.score(discrepancy) == 0.98


def test_confidence_low_for_large_monto_difference() -> None:
    scorer = ConfidenceScorer()
    engine = DiffEngine()
    expected = _expected(id_pago_mp=1, id_movimiento=10, monto="10000.00")
    actual = _actual(row_number=2, id_pago_mp=1, id_movimiento=10, monto="13000.00")
    discrepancy = engine._build_discrepancy(  # noqa: SLF001
        discrepancy_type=DiscrepancyType.WRONG_VALUE,
        expected=expected,
        actual=actual,
        field="monto",
        expected_value="10000.00",
        actual_value="13000.00",
    )

    assert scorer.score(discrepancy) == 0.50


def test_severity_assignment_critical_for_missing_rows() -> None:
    scorer = ConfidenceScorer()
    engine = DiffEngine()
    discrepancy = engine._build_discrepancy(  # noqa: SLF001
        discrepancy_type=DiscrepancyType.MISSING_ROW,
        expected=_expected(id_pago_mp=1, id_movimiento=10),
        actual=None,
        field=None,
        expected_value=None,
        actual_value=None,
    )

    assert scorer.assign_severity(discrepancy) == Severity.CRITICAL


def test_fallback_date_tolerance_plus_minus_3_days() -> None:
    engine = DiffEngine()
    expected = [_expected(id_pago_mp=9, id_movimiento=77, fecha=date(2026, 5, 10))]
    actual = [_actual(row_number=2, id_pago_mp=None, id_movimiento=None, fecha=date(2026, 5, 13))]

    discrepancies = engine.compare(expected, actual)

    # Fallback matches within ±3 days tolerance so the row IS matched,
    # but _compare_fields then reports the date difference as WRONG_VALUE.
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.WRONG_VALUE
    assert discrepancies[0].field == "fecha_movimiento"
