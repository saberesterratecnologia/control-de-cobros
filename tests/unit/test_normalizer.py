from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.models.sheet import SheetRow
from src.rules.normalizer import SheetNormalizer


def _row(**updates: object) -> SheetRow:
    base = SheetRow(
        row_number=2,
        organizacion="Org",
        curso="Curso",
        comision=" Comisión A ",
        fecha_movimiento=date(2026, 1, 10),
        tipo_movimiento="Cobro",
        dni=" 30111222 ",
        concepto=" Inscripción ",
        monto=Decimal("54800.00"),
        medio_pago="Transferencia",
        estudiante="Perez Juan",
        estado_administrativo="Activo",
        estado_deuda=None,
        id_movimiento_bancario=100,
        id_pago_mp=200,
    )
    return base.model_copy(update=updates)


def test_normalize_transferencia_alias() -> None:
    normalized, anomalies = SheetNormalizer().normalize([_row(medio_pago="Transferencia")])
    assert normalized[0].medio_pago == "Transferencia Bancaria"
    assert anomalies == []


def test_strip_whitespace_concepto_dni_comision() -> None:
    normalized, _ = SheetNormalizer().normalize([_row(concepto=" Cuota 1 ", dni=" 30111222 ", comision=" Comisión B ")])
    assert normalized[0].concepto == "Cuota 1"
    assert normalized[0].dni == "30111222"
    assert normalized[0].comision == "Comisión B"


def test_id_movimiento_bancario_less_or_equal_zero_to_none() -> None:
    normalized, _ = SheetNormalizer().normalize([_row(id_movimiento_bancario=0), _row(row_number=3, id_movimiento_bancario=-1)])
    assert normalized[0].id_movimiento_bancario is None
    assert normalized[1].id_movimiento_bancario is None


def test_id_pago_mp_less_or_equal_zero_to_none() -> None:
    normalized, _ = SheetNormalizer().normalize([_row(id_pago_mp=0), _row(row_number=3, id_pago_mp=-10)])
    assert normalized[0].id_pago_mp is None
    assert normalized[1].id_pago_mp is None


def test_detect_cobro_no_aplica_anomaly() -> None:
    _, anomalies = SheetNormalizer().normalize([_row(medio_pago="No aplica")])
    assert any(a.anomaly_type == "cobro_no_aplica" for a in anomalies)


def test_detect_venta_with_movement_anomaly() -> None:
    _, anomalies = SheetNormalizer().normalize([_row(tipo_movimiento="Venta", id_movimiento_bancario=100)])
    assert any(a.anomaly_type == "venta_with_movement" for a in anomalies)


def test_detect_missing_medio_anomaly() -> None:
    _, anomalies = SheetNormalizer().normalize([_row(medio_pago="   ")])
    assert any(a.anomaly_type == "missing_medio" for a in anomalies)


def test_detect_negative_monto_anomaly() -> None:
    _, anomalies = SheetNormalizer().normalize([_row(monto=Decimal("-1.00"))])
    assert any(a.anomaly_type == "negative_monto" for a in anomalies)


def test_anomalous_rows_are_still_returned() -> None:
    rows = [_row(medio_pago="No aplica"), _row(row_number=3, monto=Decimal("-500.00"))]
    normalized, anomalies = SheetNormalizer().normalize(rows)
    assert len(normalized) == 2
    assert len(anomalies) >= 2
