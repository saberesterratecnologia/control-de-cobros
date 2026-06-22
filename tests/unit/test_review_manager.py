"""Unit tests for ReviewManager.build_problem_summary and _format_monto.

These tests exercise pure functions that do NOT require database or
Google Sheets connections.  ReviewManager is instantiated with None
dependencies since the tested methods never touch them.
"""

from __future__ import annotations

import pytest

from src.reviews.review_manager import ReviewManager


# ---------------------------------------------------------------------------
# Helper: create a ReviewManager with no real dependencies
# ---------------------------------------------------------------------------

def _make_rm() -> ReviewManager:
    """Return a ReviewManager with mocked/None deps for pure-function tests."""
    return ReviewManager(
        sheets_connector=None,
        context_manager=None,
        config={},
    )


RM = _make_rm()


# ===================================================================
# 1. _format_monto
# ===================================================================

class TestFormatMonto:
    """Verify _format_monto with various inputs."""

    def test_integer_string_with_trailing_zeros(self):
        assert ReviewManager._format_monto("52050.0000") == "$52.050"

    def test_integer_number(self):
        assert ReviewManager._format_monto(10000) == "$10.000"

    def test_none_returns_s_dato(self):
        assert ReviewManager._format_monto(None) == "s/dato"

    def test_empty_string_returns_s_dato(self):
        assert ReviewManager._format_monto("") == "s/dato"

    def test_small_integer(self):
        assert ReviewManager._format_monto(500) == "$500"

    def test_large_integer(self):
        assert ReviewManager._format_monto("1234567") == "$1.234.567"

    def test_decimal_with_real_fraction(self):
        result = ReviewManager._format_monto("1500.75")
        assert result == "$1.500,75"

    def test_string_with_dollar_sign(self):
        assert ReviewManager._format_monto("$52050.0000") == "$52.050"

    def test_zero(self):
        assert ReviewManager._format_monto(0) == "$0"

    def test_negative_integer(self):
        assert ReviewManager._format_monto(-5000) == "$-5.000"

    def test_unparseable_returns_s_dato(self):
        assert ReviewManager._format_monto("abc") == "s/dato"

    def test_decimal_object(self):
        from decimal import Decimal
        assert ReviewManager._format_monto(Decimal("54800.0000")) == "$54.800"


# ===================================================================
# 2. guard:invalid_sequence — each reason
# ===================================================================

class TestGuardInvalidSequence:
    """Verify build_problem_summary for guard:invalid_sequence with each reason."""

    BASE_CTX = {
        "commission": "Comision A",
        "dni": "12345678",
        "pricing_inscripcion": "52050.0000",
        "pricing_cuota": "10000.0000",
        "cantidad_cuotas": 9,
    }

    def _ctx(self, reasons: list[str], **overrides) -> dict:
        ctx = {**self.BASE_CTX, "reasons": reasons}
        ctx.update(overrides)
        return ctx

    def test_missing_inscription(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["missing_inscription_with_existing_cuotas"]),
        )
        assert p == "Falta inscripción"
        assert "Tiene cuotas cargadas pero no aparece la inscripción" in d
        assert "$52.050" in d

    def test_duplicate_cuota(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["duplicate_cuota_3"]),
        )
        assert p == "Cuota duplicada"
        assert "Cuota 3 aparece más de una vez" in d

    def test_missing_cuotas_before(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["missing_cuotas_before_5:1,2,3"]),
        )
        assert p == "Cuotas faltantes"
        assert "Faltan cuotas 1,2,3 antes de la 5" in d
        assert "$10.000" in d

    def test_cuota_1_matches_inscription_amount(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["cuota_1_matches_inscription_amount"]),
        )
        assert p == "Cuota 1 con monto de inscripción"
        assert "$52.050" in d
        assert "$10.000" in d
        assert "en vez de cuota" in d

    def test_cuota_1_combines_inscription_and_cuota(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["cuota_1_combines_inscription_and_cuota"]),
        )
        assert p == "Cuota 1 combina inscripción + cuota"
        assert "inscripción + cuota juntas" in d
        # Should have the combined amount ($62.050 = 52050 + 10000)
        assert "$62.050" in d

    def test_inscription_with_non_standard_amount(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["inscription_with_non_standard_amount"]),
        )
        assert p == "Inscripción con monto irregular"
        assert "monto diferente al esperado" in d
        assert "$52.050" in d

    def test_cuota_exceeds_total(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["cuota_exceeds_total:12>9"]),
        )
        assert p == "Cuota excede el total"
        assert "Cuota 12" in d
        assert "solo tiene 9 cuotas" in d

    def test_multiple_reasons_picks_most_severe(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx([
                "missing_inscription_with_existing_cuotas",
                "cuota_exceeds_total:12>9",
            ]),
        )
        # cuota_exceeds_total has severity 7, highest
        assert p == "Cuota excede el total"
        # Both details combined with " | "
        assert " | " in d
        assert "Cuota 12" in d
        assert "inscripción" in d.lower()

    def test_no_reasons_fallback(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx([]),
        )
        assert p == "Requiere revisión"
        assert "sin razones" in d

    def test_unknown_guard_reason(self):
        p, d = RM.build_problem_summary(
            "guard:invalid_sequence",
            self._ctx(["some_new_unknown_reason"]),
        )
        assert p == "Requiere revisión"
        assert "some_new_unknown_reason" in d


# ===================================================================
# 3. Ambiguous with only "Desconocido" candidates
# ===================================================================

class TestAmbiguousDesconocido:
    """Verify build_problem_summary for ambiguous payments with only unknown candidates."""

    def test_all_desconocido(self):
        ctx = {
            "commission": "Comision B",
            "dni": "99887766",
            "payment_id": 79636,
            "monto": "54800.0000",
            "fecha": "2026-02-13",
            "candidates": [
                {"concept": "Desconocido", "amount": "54800.0000"},
            ],
        }
        p, d = RM.build_problem_summary("ambiguous", ctx)
        assert p == "Requiere definición de concepto"
        assert "No se pudo determinar el concepto automáticamente" in d
        assert "$54.800" in d
        assert "79636" in d

    def test_no_candidates(self):
        ctx = {
            "commission": "Comision C",
            "dni": "11223344",
            "payment_id": 100,
            "monto": "5000",
            "fecha": "2026-03-01",
            "candidates": [],
        }
        p, d = RM.build_problem_summary("ambiguous", ctx)
        assert p == "Requiere definición de concepto"
        assert "No se pudo determinar el concepto" in d


# ===================================================================
# 4. Ambiguous with real candidates
# ===================================================================

class TestAmbiguousRealCandidates:
    """Verify build_problem_summary for ambiguous payments with real candidates."""

    def test_real_candidates_formatted(self):
        ctx = {
            "commission": "Comision D",
            "dni": "55667788",
            "payment_id": 12345,
            "monto": "10000.0000",
            "fecha": "2026-04-15",
            "candidates": [
                {"concept": "Cuota 3", "amount": "10000.0000"},
                {"concept": "Inscripción", "amount": "10000.0000"},
            ],
        }
        p, d = RM.build_problem_summary("ambiguous", ctx)
        assert p == "Requiere definición de concepto"
        assert "candidatos:" in d
        assert "Cuota 3" in d
        assert "$10.000" in d

    def test_real_candidates_with_commission_prices(self):
        ctx = {
            "commission": "Comision E",
            "dni": "99001122",
            "payment_id": 999,
            "monto": "15000",
            "fecha": "2026-05-01",
            "candidates": [
                {"concept": "Cuota 1", "amount": "15000"},
            ],
            "commission_prices": {
                "inscripcion": "52050",
                "cuota": "10000",
            },
        }
        p, d = RM.build_problem_summary("ambiguous", ctx)
        assert "Inscripción=$52.050" in d
        assert "Cuota=$10.000" in d

    def test_desconocido_with_commission_prices(self):
        """Even unknown candidates should show pricing when available."""
        ctx = {
            "commission": "Comision F",
            "dni": "11111111",
            "payment_id": 888,
            "monto": "54800",
            "fecha": "2026-06-01",
            "candidates": [
                {"concept": "Desconocido", "amount": "54800"},
            ],
            "commission_prices": {
                "inscripcion": "52050",
                "cuota": "10000",
            },
        }
        p, d = RM.build_problem_summary("ambiguous", ctx)
        assert "No se pudo determinar el concepto" in d
        assert "Inscripción=$52.050" in d
        assert "Cuota=$10.000" in d


# ===================================================================
# 5. Anomalies
# ===================================================================

class TestAnomalies:
    """Verify build_problem_summary for anomaly reasons."""

    def test_cobro_no_aplica_with_commission(self):
        ctx = {
            "dni": "44556677",
            "commission": "Comision G",
            "concepto": "Cuota 2",
            "monto": "10000.0000",
        }
        p, d = RM.build_problem_summary("anomaly:cobro_no_aplica", ctx)
        assert p == "Anomalía de hoja"
        assert "en Comision G" in d
        assert "$10.000" in d
        assert "No aplica" in d

    def test_cobro_no_aplica_without_commission(self):
        ctx = {
            "dni": "44556677",
            "concepto": "Cuota 2",
            "monto": "10000",
        }
        p, d = RM.build_problem_summary("anomaly:cobro_no_aplica", ctx)
        assert p == "Anomalía de hoja"
        # No "en " prefix since commission is empty
        assert "en " not in d.split("—")[0]

    def test_negative_monto(self):
        ctx = {
            "dni": "33445566",
            "row_number": 42,
            "monto": "-5000",
            "concepto": "Cuota 1",
        }
        p, d = RM.build_problem_summary("anomaly:negative_monto", ctx)
        assert p == "Anomalía de hoja"
        assert "Fila 42" in d
        assert "Cuota 1" in d
        assert "no debería ser negativo" in d

    def test_venta_with_movement(self):
        ctx = {
            "dni": "22334455",
            "concepto": "Inscripción",
            "monto": "52050",
            "id_movimiento_bancario": 777,
        }
        p, d = RM.build_problem_summary("anomaly:venta_with_movement", ctx)
        assert p == "Anomalía de hoja"
        assert "Venta Inscripción" in d
        assert "777" in d

    def test_generic_anomaly(self):
        ctx = {
            "dni": "11112222",
            "description": "Something weird happened",
        }
        p, d = RM.build_problem_summary("anomaly:unknown_thing", ctx)
        assert p == "Anomalía de hoja"
        assert "Something weird happened" in d


# ===================================================================
# 6. Existing categories (uncontrolled, fecha, monto, medio, missing_row)
# ===================================================================

class TestExistingCategories:
    """Verify that existing problem categories still work correctly."""

    def test_pago_no_controlado(self):
        ctx = {
            "dni": "11223344",
            "payment_id": 500,
            "fecha": "2026-01-15T10:30:00",
            "monto": "25000",
        }
        p, d = RM.build_problem_summary("pago_no_controlado", ctx)
        assert p == "Pago no controlado"
        assert "500" in d
        assert "2026-01-15" in d

    def test_fecha_faltante(self):
        ctx = {
            "dni": "55667788",
            "type": "wrong_value",
            "field": "fecha",
            "expected_value": "2026-03-10",
            "actual_value": "",
            "concepto": "Cuota 4",
        }
        p, d = RM.build_problem_summary("fecha_issue", ctx)
        assert p == "Fecha faltante"
        assert "Cuota 4" in d
        assert "2026-03-10" in d

    def test_monto_no_coincide(self):
        ctx = {
            "dni": "99887766",
            "type": "wrong_value",
            "field": "monto",
            "expected_value": "10000",
            "actual_value": "9500",
            "concepto": "Cuota 1",
        }
        p, d = RM.build_problem_summary("value_mismatch", ctx)
        assert p == "Monto no coincide"
        assert "$9500" in d
        assert "$10000" in d

    def test_medio_pago_incorrecto(self):
        ctx = {
            "dni": "44332211",
            "type": "wrong_value",
            "field": "medio_pago",
            "expected_value": "Transferencia",
            "actual_value": "Efectivo",
            "concepto": "Inscripción",
        }
        p, d = RM.build_problem_summary("medio_issue", ctx)
        assert p == "Medio de pago incorrecto"
        assert "Efectivo" in d
        assert "Transferencia" in d

    def test_missing_row(self):
        ctx = {
            "dni": "77665544",
            "commission": "Comision H",
        }
        p, d = RM.build_problem_summary("missing_row", ctx)
        assert p == "Falta fila en hoja"
        assert "Comision H" in d
        assert "77665544" in d

    def test_generic_fallback(self):
        ctx = {
            "dni": "00000000",
            "type": "unknown_type",
        }
        p, d = RM.build_problem_summary("something_new", ctx)
        assert p == "Requiere revisión"
        assert "unknown_type" in d
