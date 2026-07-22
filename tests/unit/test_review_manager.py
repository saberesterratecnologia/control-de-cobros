"""Unit tests for ReviewManager.build_problem_summary, _format_monto, and export dedup.

These tests exercise pure functions that do NOT require database or
Google Sheets connections.  ReviewManager is instantiated with None
dependencies since the tested methods never touch them.
Export dedup tests use lightweight mocks for sheets and context_manager.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

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

    def test_pricing_suffix_includes_pago_unico(self):
        """Pricing suffix renders Pago Único when present in commission_prices."""
        ctx = {
            "commission": "Comision PU",
            "dni": "77778888",
            "payment_id": 1234,
            "monto": "500000",
            "fecha": "2026-07-01",
            "candidates": [
                {"concept": "Pago Único", "amount": "500000"},
            ],
            "commission_prices": {
                "inscripcion": "52050",
                "cuota": "10000",
                "pago_unico": "500000",
            },
        }
        p, d = RM.build_problem_summary("ambiguous", ctx)
        assert p == "Requiere definición de concepto"
        assert "Inscripción=$52.050" in d
        assert "Cuota=$10.000" in d
        assert "Pago Único=$500.000" in d

    def test_pricing_suffix_omits_pago_unico_when_absent(self):
        """Pricing suffix does NOT include Pago Único when key is missing."""
        ctx = {
            "commission": "Comision NoPU",
            "dni": "99990000",
            "payment_id": 5678,
            "monto": "15000",
            "fecha": "2026-07-01",
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
        assert "Pago Único" not in d


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


# ===================================================================
# 7. Export dedup by payment_id
# ===================================================================


def _make_export_rm(open_reviews: list[dict]) -> tuple[ReviewManager, MagicMock]:
    """Create a ReviewManager wired for export_to_sheet tests.

    Returns (rm, worksheet_mock).
    """
    worksheet = MagicMock()
    worksheet.row_values.return_value = ReviewManager.HEADER
    worksheet.get_all_values.return_value = [ReviewManager.HEADER]

    sheets = MagicMock()
    sheets._client = MagicMock()
    spreadsheet = MagicMock()
    sheets._client.open_by_key.return_value = spreadsheet
    spreadsheet.worksheet.return_value = worksheet

    ctx_mgr = MagicMock()
    ctx_mgr.get_all_open_reviews.return_value = open_reviews

    config = {"sheets": {"spreadsheet_id": "fake-id"}}
    rm = ReviewManager(sheets_connector=sheets, context_manager=ctx_mgr, config=config)
    return rm, worksheet


def _make_sync_rm(sheet_rows: list[list[str]]) -> tuple[ReviewManager, MagicMock, MagicMock]:
    worksheet = MagicMock()
    worksheet.row_values.return_value = ReviewManager.HEADER
    worksheet.get_all_values.return_value = [ReviewManager.HEADER, *sheet_rows]

    sheets = MagicMock()
    sheets._client = MagicMock()
    spreadsheet = MagicMock()
    sheets._client.open_by_key.return_value = spreadsheet
    spreadsheet.worksheet.return_value = worksheet

    ctx_mgr = MagicMock()

    config = {"sheets": {"spreadsheet_id": "fake-id"}}
    rm = ReviewManager(sheets_connector=sheets, context_manager=ctx_mgr, config=config)
    return rm, worksheet, ctx_mgr


def _make_cleanup_rm(open_tasks: list[dict]) -> tuple[ReviewManager, MagicMock, MagicMock]:
    worksheet = MagicMock()
    worksheet.row_values.return_value = ReviewManager.CLEANUP_HEADER
    worksheet.get_all_values.return_value = [ReviewManager.CLEANUP_HEADER]

    sheets = MagicMock()
    sheets._client = MagicMock()
    spreadsheet = MagicMock()
    sheets._client.open_by_key.return_value = spreadsheet
    spreadsheet.worksheet.side_effect = lambda name: worksheet if name == "LIMPIEZA_HOJA" else worksheet

    ctx_mgr = MagicMock()
    ctx_mgr.get_all_open_cleanup_tasks.return_value = open_tasks

    config = {"sheets": {"spreadsheet_id": "fake-id"}}
    rm = ReviewManager(sheets_connector=sheets, context_manager=ctx_mgr, config=config)
    return rm, worksheet, ctx_mgr


def _make_cleanup_sync_rm(sheet_rows: list[list[str]]) -> tuple[ReviewManager, MagicMock, MagicMock]:
    worksheet = MagicMock()
    worksheet.row_values.return_value = ReviewManager.CLEANUP_HEADER
    worksheet.get_all_values.return_value = [ReviewManager.CLEANUP_HEADER, *sheet_rows]

    sheets = MagicMock()
    sheets._client = MagicMock()
    spreadsheet = MagicMock()
    sheets._client.open_by_key.return_value = spreadsheet
    spreadsheet.worksheet.side_effect = lambda name: worksheet if name == "LIMPIEZA_HOJA" else worksheet

    ctx_mgr = MagicMock()

    config = {"sheets": {"spreadsheet_id": "fake-id"}}
    rm = ReviewManager(sheets_connector=sheets, context_manager=ctx_mgr, config=config)
    return rm, worksheet, ctx_mgr


class TestExportDedupByPaymentId:
    """Verify export_to_sheet deduplicates ambiguous reviews by payment_id."""

    def test_export_dedup_by_payment_id(self) -> None:
        """Two ambiguous reviews with same payment_id: only the first (lowest id) is exported."""
        reviews = [
            {
                "id": 1,
                "reason": "ambiguous_allocation:auto",
                "context_json": json.dumps({
                    "payment_id": 500,
                    "commission": "C1",
                    "dni": "111",
                    "monto": "10000",
                    "fecha": "2026-01-01",
                    "candidates": [{"concept": "Cuota 1", "amount": "10000"}],
                }),
            },
            {
                "id": 2,
                "reason": "ambiguous_allocation:manual",
                "context_json": json.dumps({
                    "payment_id": 500,
                    "commission": "C2",
                    "dni": "222",
                    "monto": "10000",
                    "fecha": "2026-01-01",
                    "candidates": [{"concept": "Cuota 2", "amount": "10000"}],
                }),
            },
        ]
        rm, ws = _make_export_rm(reviews)
        result = rm.export_to_sheet()

        assert result["exported"] == 1
        assert result["skipped"] == 1
        # Verify the exported row is REV-1 (lowest id)
        appended = ws.append_rows.call_args[0][0]
        assert len(appended) == 1
        assert appended[0][0] == "REV-1"

    def test_export_no_dedup_for_non_ambiguous(self) -> None:
        """pago_no_controlado reviews with same payment_id: both exported."""
        reviews = [
            {
                "id": 10,
                "reason": "pago_no_controlado",
                "context_json": json.dumps({
                    "payment_id": 600,
                    "commission": "C3",
                    "dni": "333",
                    "monto": "5000",
                    "fecha": "2026-02-01",
                }),
            },
            {
                "id": 11,
                "reason": "pago_no_controlado",
                "context_json": json.dumps({
                    "payment_id": 600,
                    "commission": "C4",
                    "dni": "444",
                    "monto": "5000",
                    "fecha": "2026-02-01",
                }),
            },
        ]
        rm, ws = _make_export_rm(reviews)
        result = rm.export_to_sheet()

        assert result["exported"] == 2

    def test_export_no_dedup_without_payment_id(self) -> None:
        """Ambiguous reviews without payment_id: all exported through existing rules."""
        reviews = [
            {
                "id": 20,
                "reason": "ambiguous_allocation:auto",
                "context_json": json.dumps({
                    "commission": "C5",
                    "dni": "555",
                    "monto": "8000",
                    "fecha": "2026-03-01",
                    "candidates": [{"concept": "Cuota 1", "amount": "8000"}],
                }),
            },
            {
                "id": 21,
                "reason": "ambiguous_allocation:manual",
                "context_json": json.dumps({
                    "commission": "C6",
                    "dni": "666",
                    "monto": "8000",
                    "fecha": "2026-03-01",
                    "candidates": [{"concept": "Cuota 2", "amount": "8000"}],
                }),
            },
        ]
        rm, ws = _make_export_rm(reviews)
        result = rm.export_to_sheet()

        assert result["exported"] == 2

    def test_export_groups_wrong_value_reviews_by_payment_id(self) -> None:
        reviews = [
            {
                "id": 30,
                "reason": "gpt-4o",
                "context_json": json.dumps({
                    "payment_id": 900,
                    "commission": "C7",
                    "dni": "777",
                    "type": "wrong_value",
                    "field": "fecha_movimiento",
                    "concepto": "Cuota 8",
                    "monto": "10000",
                }),
            },
            {
                "id": 31,
                "reason": "gpt-4o",
                "context_json": json.dumps({
                    "payment_id": 900,
                    "commission": "C7",
                    "dni": "777",
                    "type": "wrong_value",
                    "field": "concepto",
                    "concepto": "Cuota 8",
                    "monto": "10000",
                }),
            },
        ]
        rm, ws = _make_export_rm(reviews)

        result = rm.export_to_sheet()

        assert result["exported"] == 1
        appended = ws.append_rows.call_args[0][0]
        assert appended[0][0] == "GRP-900-WF"
        assert appended[0][3] == "Revisar fecha/concepto"
        assert "Pago 900" in appended[0][4]


class TestGroupedReviewSync:
    def test_sync_grouped_review_resolves_all_members(self) -> None:
        rm, ws, ctx = _make_sync_rm([["GRP-900-WF", "Com A", "30111222", "Revisar fecha/concepto", "Pago 900 — Cuota 8", "Resolver asi"]])
        ctx.get_open_grouped_reviews.return_value = [
            {
                "id": 30,
                "run_id": "run-1",
                "reason": "gpt-4o",
                "context_json": json.dumps({
                    "commission": "Com A",
                    "dni": "30111222",
                    "payment_id": 900,
                    "type": "wrong_value",
                    "field": "fecha_movimiento",
                    "concepto": "Cuota 8",
                    "monto": "10000",
                    "commission_prices": {"cuota": "10000"},
                }),
            },
            {
                "id": 31,
                "run_id": "run-1",
                "reason": "gpt-4o",
                "context_json": json.dumps({
                    "commission": "Com A",
                    "dni": "30111222",
                    "payment_id": 900,
                    "type": "wrong_value",
                    "field": "concepto",
                    "concepto": "Cuota 8",
                    "monto": "10000",
                    "commission_prices": {"cuota": "10000"},
                }),
            },
        ]

        result = rm.sync_resolutions()

        assert result["synced"] == 2
        assert ctx.save_review_resolution.call_count == 2
        assert ctx.update_pending_review_resolution.call_count == 2
        ws.delete_rows.assert_called_once_with(2)


class TestCleanupTasks:
    def test_build_cleanup_tasks_from_non_blocking_guard(self) -> None:
        tasks = RM.build_cleanup_tasks(
            "guard:invalid_sequence",
            {
                "commission": "Com A",
                "dni": "30111222",
                "blocking": False,
                "reasons": [
                    "missing_inscription_with_existing_cuotas",
                    "missing_cuotas_before_5:3,4",
                ],
            },
        )

        assert len(tasks) == 2
        assert {task["task_type"] for task in tasks} == {"Inscripción", "Secuencia"}

    def test_build_cleanup_tasks_ignores_blocking_guard(self) -> None:
        tasks = RM.build_cleanup_tasks(
            "guard:invalid_sequence",
            {
                "commission": "Com B",
                "dni": "22334455",
                "blocking": True,
                "reasons": ["cuota_exceeds_total:12>9"],
            },
        )
        assert tasks == []

    def test_build_cleanup_task_from_anomaly(self) -> None:
        tasks = RM.build_cleanup_tasks(
            "anomaly:cobro_no_aplica",
            {
                "commission": "Com C",
                "dni": "33445566",
            },
        )
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "Cobro"
        assert tasks[0]["summary"] == "Corregir medio de cobro"

    def test_export_cleanup_to_sheet(self) -> None:
        tasks = [
            {
                "id": 10,
                "commission": "Com D",
                "dni": "44556677",
                "task_type": "Secuencia",
                "summary": "Ordenar cuotas",
            }
        ]
        rm, ws, _ctx = _make_cleanup_rm(tasks)

        result = rm.export_cleanup_to_sheet()

        assert result == {"exported": 1, "skipped": 0}
        appended = ws.append_rows.call_args[0][0]
        assert appended == [["CLN-10", "Com D", "44556677", "Secuencia", "Ordenar cuotas", "PENDIENTE", ""]]

    def test_sync_cleanup_status_marks_done_and_deletes_row(self) -> None:
        rm, ws, ctx = _make_cleanup_sync_rm([["CLN-10", "Com D", "44556677", "Secuencia", "Ordenar cuotas", "HECHO", "listo"]])
        ctx.get_cleanup_task_by_id.return_value = {"id": 10, "status": "open"}

        result = rm.sync_cleanup_statuses()

        assert result["synced"] == 1
        ctx.update_cleanup_task_status.assert_called_once_with(10, reviewer_notes="listo", status="resolved")
        ws.delete_rows.assert_called_once_with(2)


class TestSyncResolutionLifecycle:
    def test_sync_resolves_open_review_and_deletes_row(self) -> None:
        rm, ws, ctx = _make_sync_rm([["REV-10", "Com A", "30111222", "Problema", "Detalle", "Resolver asi"]])
        ctx.get_pending_review_by_id.return_value = {
            "id": 10,
            "run_id": "run-1",
            "reason": "ambiguous_allocation:auto",
            "context_json": json.dumps({
                "commission": "Com A",
                "dni": "30111222",
                "monto": "10000",
                "commission_prices": {"cuota": "10000"},
            }),
            "status": "open",
        }

        result = rm.sync_resolutions()

        assert result["synced"] == 1
        assert result["removed_stale"] == 0
        ctx.save_review_resolution.assert_called_once()
        ctx.update_pending_review_resolution.assert_called_once_with(10, reviewer_notes="Resolver asi")
        ws.delete_rows.assert_called_once_with(2)

    def test_sync_removes_stale_row_for_closed_review(self) -> None:
        rm, ws, ctx = _make_sync_rm([["REV-11", "Com A", "30111222", "Problema", "Detalle", "Resolver asi"]])
        ctx.get_pending_review_by_id.return_value = {
            "id": 11,
            "status": "resolved",
            "context_json": "{}",
        }

        result = rm.sync_resolutions()

        assert result["synced"] == 0
        assert result["removed_stale"] == 1
        ctx.save_review_resolution.assert_not_called()
        ctx.update_pending_review_resolution.assert_not_called()
        ws.delete_rows.assert_called_once_with(2)

    def test_sync_removes_already_synced_row_when_pending_missing(self) -> None:
        rm, ws, ctx = _make_sync_rm([["REV-12", "Com A", "30111222", "Problema", "Detalle", "Resolver asi"]])
        ctx.get_pending_review_by_id.return_value = None
        ctx.has_review_resolution.return_value = True

        result = rm.sync_resolutions()

        assert result["synced"] == 0
        assert result["removed_stale"] == 1
        ws.delete_rows.assert_called_once_with(2)
