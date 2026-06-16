"""Human review export/sync manager for REVISIONES worksheet."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread

LOGGER = logging.getLogger(__name__)


class ReviewManager:
    HEADER = ["case_id", "comision", "dni", "problema", "detalle", "resolucion"]

    def __init__(self, sheets_connector: Any, context_manager: Any, config: dict[str, Any]):
        self.sheets = sheets_connector
        self.context = context_manager
        self.config = config

    def _get_revisiones_sheet(self) -> Any | None:
        if self.sheets._client is None:  # noqa: SLF001
            LOGGER.warning("sheets client unavailable, skipping REVISIONES sync/export")
            return None

        sheet_cfg = self.config.get("sheets", {})
        spreadsheet_id = sheet_cfg.get("spreadsheet_id")
        spreadsheet_name = sheet_cfg.get("spreadsheet_name", "")

        if spreadsheet_id:
            spreadsheet = self.sheets._client.open_by_key(spreadsheet_id)  # noqa: SLF001
        else:
            spreadsheet = self.sheets._client.open(spreadsheet_name)  # noqa: SLF001

        try:
            worksheet = spreadsheet.worksheet("REVISIONES")
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="REVISIONES", rows=200, cols=6)

        self._ensure_header(worksheet)
        return worksheet

    def _ensure_header(self, worksheet: Any) -> None:
        row1 = worksheet.row_values(1)
        if row1[:6] != self.HEADER:
            worksheet.batch_update(
                [{"range": "A1:F1", "values": [self.HEADER]}],
                value_input_option="RAW",
            )

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            text = str(value).replace("$", "").replace(" ", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            if not text:
                return None
            return float(Decimal(text))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _infer_concepto_tipo(reason: str, context_json: dict[str, Any]) -> str:
        normalized = f"{reason} {json.dumps(context_json, ensure_ascii=False)}".casefold()
        if "inscrip" in normalized:
            return "inscripcion"
        if "cuota" in normalized:
            return "cuota"
        if "pago_unico" in normalized or "pago unico" in normalized or "único" in normalized:
            return "pago_unico"
        return "desconocido"

    @staticmethod
    def _extract_similarity_fields(reason: str, context_json: dict[str, Any]) -> dict[str, Any]:
        monto = ReviewManager._safe_float(context_json.get("monto"))

        commission_prices = context_json.get("commission_prices") or {}
        pricing_inscripcion = ReviewManager._safe_float(
            commission_prices.get("inscripcion") or context_json.get("pricing_inscripcion")
        )
        pricing_cuota = ReviewManager._safe_float(
            commission_prices.get("cuota") or context_json.get("pricing_cuota")
        )

        concepto_tipo = ReviewManager._infer_concepto_tipo(reason, context_json)
        relevant_price: float | None = None
        if concepto_tipo == "inscripcion":
            relevant_price = pricing_inscripcion
        elif concepto_tipo == "cuota":
            relevant_price = pricing_cuota
        elif concepto_tipo == "pago_unico":
            relevant_price = pricing_cuota or pricing_inscripcion

        monto_ratio = (monto / relevant_price) if (monto is not None and relevant_price and relevant_price > 0) else None

        return {
            "monto": monto,
            "concepto_tipo": concepto_tipo,
            "pricing_inscripcion": pricing_inscripcion,
            "pricing_cuota": pricing_cuota,
            "monto_ratio": monto_ratio,
        }

    def build_problem_summary(self, reason: str, context_json: dict[str, Any]) -> tuple[str, str]:
        """Return (problema_category, detalle) for the REVISIONES sheet.

        ``problema`` is a short fixed category.
        ``detalle`` uses stable identifiers (comision, DNI, concepto, monto)
        instead of row numbers, which drift when the agent inserts/deletes
        rows for other students during the same run.
        """
        commission = str(context_json.get("commission") or "?").strip()
        dni = str(context_json.get("dni") or "?").strip()
        monto = context_json.get("monto")
        payment_id = context_json.get("payment_id")

        # --- Anomalies ---
        if reason.startswith("anomaly:"):
            anomaly = reason.split(":", 1)[1]
            description = context_json.get("description") or ""
            if anomaly == "venta_with_movement":
                mov_id = context_json.get("id_movimiento_bancario") or ""
                concepto = context_json.get("concepto") or "?"
                monto_str = f"${monto}" if monto else ""
                return (
                    "Anomalía de hoja",
                    f"DNI {dni} — Venta {concepto} {monto_str} "
                    f"tiene id_movimiento={mov_id} (las Ventas no deberían tener movimiento bancario)",
                )
            if anomaly == "cobro_no_aplica":
                concepto = context_json.get("concepto") or "?"
                monto_str = f"${monto}" if monto else ""
                return (
                    "Anomalía de hoja",
                    f"DNI {dni} — Cobro {concepto} {monto_str} "
                    f"tiene 'No aplica' como medio de pago (los Cobros deben tener medio real)",
                )
            return (
                "Anomalía de hoja",
                f"DNI {dni} — {description or anomaly}",
            )

        # --- Uncontrolled payment ---
        if "pago_no_controlado" in reason:
            raw_fecha = context_json.get("fecha") or "?"
            fecha = str(raw_fecha).split("T")[0] if raw_fecha else "?"
            return (
                "Pago no controlado",
                f"Pago {payment_id or '?'} del {fecha} por ${monto or '?'} — "
                f"el informe no fue controlado por el proceso habitual",
            )

        # --- Ambiguous payments ---
        if "ambiguous" in reason:
            candidates = context_json.get("candidates") or []
            raw_fecha = context_json.get("fecha") or "?"
            fecha = str(raw_fecha).split("T")[0] if raw_fecha else "?"
            candidates_str = ", ".join(
                f"{c.get('concept', '?')} (${c.get('amount', '?')})"
                for c in candidates
            ) if candidates else "sin candidatos claros"
            return (
                "Requiere definición de concepto",
                f"Pago {payment_id or '?'} del {fecha} por ${monto or '?'} — "
                f"candidatos: {candidates_str}",
            )

        # --- Missing date / wrong value in fecha ---
        if "fecha" in reason.casefold() or context_json.get("type") == "wrong_value" and "fecha" in str(context_json.get("field") or ""):
            expected = context_json.get("expected_value") or context_json.get("expected_date") or "s/dato"
            actual = context_json.get("actual_value") or "vacía"
            concepto = context_json.get("concepto") or "?"
            return (
                "Fecha faltante",
                f"DNI {dni} — {concepto} — "
                f"fecha actual: {actual}, esperada: {expected}",
            )

        # --- Monto mismatch ---
        if context_json.get("type") == "wrong_value" and "monto" in str(context_json.get("field") or ""):
            expected = context_json.get("expected_value") or "?"
            actual = context_json.get("actual_value") or "?"
            concepto = context_json.get("concepto") or "?"
            return (
                "Monto no coincide",
                f"DNI {dni} — {concepto} — "
                f"monto actual: ${actual}, esperado: ${expected}",
            )

        # --- Medio de pago ---
        if context_json.get("type") == "wrong_value" and "medio" in str(context_json.get("field") or ""):
            expected = context_json.get("expected_value") or "?"
            actual = context_json.get("actual_value") or "?"
            concepto = context_json.get("concepto") or "?"
            return (
                "Medio de pago incorrecto",
                f"DNI {dni} — {concepto} — "
                f"medio actual: {actual}, esperado: {expected}",
            )

        # --- Missing row ---
        if "missing_row" in reason:
            return (
                "Falta fila en hoja",
                f"DNI {dni} en {commission} — no tiene fila correspondiente en la planilla",
            )

        # --- Generic fallback ---
        dtype = context_json.get("type") or reason
        return (
            "Requiere revisión",
            f"DNI {dni} — {dtype}",
        )

    @staticmethod
    def _dedup_key(comision: str, dni: str, problema: str, detalle: str) -> str:
        """Build a content-based key to detect duplicate reviews across runs.

        Uses stable identifiers (comision, dni, problema, detalle) instead of
        row numbers, which drift when the agent inserts/deletes rows during a run.
        """
        return f"{comision.strip()}|{dni.strip()}|{problema.strip()}|{detalle.strip()}"

    def export_to_sheet(self, run_id: str | None = None) -> dict[str, int]:
        worksheet = self._get_revisiones_sheet()
        if worksheet is None:
            return {"exported": 0, "skipped": 0}
        open_reviews = self.context.get_all_open_reviews(run_id=run_id)

        existing_case_ids = set()
        existing_content_keys: set[str] = set()
        all_values = worksheet.get_all_values()
        for row in all_values[1:]:
            if not row:
                continue
            if row[0].strip():
                existing_case_ids.add(row[0].strip())
            # Build content key from columns: comision(1), dni(2), problema(3), detalle(4)
            if len(row) >= 5:
                existing_content_keys.add(
                    self._dedup_key(row[1], row[2], row[3], row[4])
                )

        rows_to_append: list[list[str]] = []
        skipped = 0
        for review in open_reviews:
            case_id = f"REV-{review['id']}"
            if case_id in existing_case_ids:
                skipped += 1
                continue

            context_json = json.loads(review.get("context_json") or "{}")
            problema, detalle = self.build_problem_summary(review.get("reason", ""), context_json)
            comision = str(context_json.get("commission") or "").strip()
            dni = str(context_json.get("dni") or "").strip()

            content_key = self._dedup_key(comision, dni, problema, detalle)
            if content_key in existing_content_keys:
                skipped += 1
                continue
            existing_content_keys.add(content_key)

            rows_to_append.append(
                [case_id, comision, dni, problema, detalle, ""]
            )

        if rows_to_append:
            worksheet.append_rows(rows_to_append, value_input_option="RAW")

        return {"exported": len(rows_to_append), "skipped": skipped}

    def sync_resolutions(self) -> dict[str, Any]:
        worksheet = self._get_revisiones_sheet()
        if worksheet is None:
            return {"synced": 0, "errors": []}
        rows = worksheet.get_all_values()

        synced = 0
        errors: list[str] = []
        rows_to_delete: list[int] = []

        for idx, row in enumerate(rows[1:], start=2):
            if len(row) < 6:
                continue

            case_id = (row[0] or "").strip()
            commission = (row[1] or "").strip()
            dni = (row[2] or "").strip()
            problem = (row[3] or "").strip()
            detalle = (row[4] or "").strip()
            resolution = (row[5] or "").strip()

            if not case_id or not resolution:
                continue

            try:
                match = re.match(r"^REV-(\d+)$", case_id)
                if match is None:
                    raise ValueError(f"case_id inválido: {case_id}")
                pending_review_id = int(match.group(1))

                open_reviews = self.context.get_all_open_reviews()
                pending = next((r for r in open_reviews if int(r["id"]) == pending_review_id), None)
                if pending is None:
                    raise ValueError(f"No se encontró pending_review abierto para {case_id}")

                context_json = json.loads(pending.get("context_json") or "{}")
                similarity = self._extract_similarity_fields(pending.get("reason", ""), context_json)

                full_problem = f"{problem}: {detalle}" if detalle else problem
                self.context.save_review_resolution(
                    case_id=case_id,
                    run_id=pending.get("run_id"),
                    commission=commission or str(context_json.get("commission") or ""),
                    dni=dni or str(context_json.get("dni") or ""),
                    problem=full_problem,
                    resolution=resolution,
                    monto=similarity["monto"],
                    concepto_tipo=similarity["concepto_tipo"],
                    pricing_inscripcion=similarity["pricing_inscripcion"],
                    pricing_cuota=similarity["pricing_cuota"],
                    monto_ratio=similarity["monto_ratio"],
                )
                self.context.update_pending_review_resolution(pending_review_id, reviewer_notes=resolution)

                rows_to_delete.append(idx)
                synced += 1
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("failed to sync resolution", extra={"row": idx, "case_id": case_id})
                errors.append(f"row {idx} ({case_id}): {error}")

        for row_num in sorted(rows_to_delete, reverse=True):
            worksheet.delete_rows(row_num)

        return {"synced": synced, "errors": errors}
