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

    # Priority order for guard:invalid_sequence reasons (higher index = more severe).
    _GUARD_REASON_SEVERITY: dict[str, int] = {
        "cuota_1_matches_inscription_amount": 1,
        "cuota_1_combines_inscription_and_cuota": 2,
        "inscription_with_non_standard_amount": 3,
        "missing_inscription_with_existing_cuotas": 4,
        "duplicate_cuota": 5,
        "missing_cuotas_before": 6,
        "cuota_exceeds_total": 7,
    }

    def __init__(self, sheets_connector: Any, context_manager: Any, config: dict[str, Any]):
        self.sheets = sheets_connector
        self.context = context_manager
        self.config = config

    @staticmethod
    def _format_monto(value: Any) -> str:
        """Format a monetary value for display.

        Takes a string like "52050.0000" or a number and returns "$52.050"
        (dot as thousands separator, no decimals if all zeros, 2 decimals otherwise).
        Returns "s/dato" for None or unparseable values.
        """
        if value is None:
            return "s/dato"
        try:
            text = str(value).replace("$", "").replace(" ", "").strip()
            if not text:
                return "s/dato"
            num = Decimal(text)
            # Check if the fractional part is zero
            if num == num.to_integral_value():
                integer_part = int(num)
                # Format with dot as thousands separator
                formatted = f"{abs(integer_part):,}".replace(",", ".")
                if integer_part < 0:
                    formatted = f"-{formatted}"
                return f"${formatted}"
            else:
                # Keep 2 decimal places, comma as decimal separator
                integer_part = int(num)
                frac = abs(num - integer_part)
                frac_str = f"{frac:.2f}"[1:]  # ".XX"
                formatted_int = f"{abs(integer_part):,}".replace(",", ".")
                if integer_part < 0:
                    formatted_int = f"-{formatted_int}"
                return f"${formatted_int}{frac_str.replace('.', ',')}"
        except (InvalidOperation, ValueError, TypeError):
            return "s/dato"

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
                monto_fmt = self._format_monto(monto) if monto else ""
                comision_name = str(context_json.get("commission") or "").strip()
                comision_part = f" en {comision_name}" if comision_name else ""
                return (
                    "Anomalía de hoja",
                    f"DNI {dni}{comision_part} — Cobro {concepto} {monto_fmt} "
                    f"tiene 'No aplica' como medio de pago (los Cobros deben tener medio real)",
                )
            if anomaly == "negative_monto":
                row_number = context_json.get("row_number") or context_json.get("row") or "?"
                monto_fmt = self._format_monto(monto) if monto else "negativo"
                concepto = context_json.get("concepto") or "?"
                return (
                    "Anomalía de hoja",
                    f"DNI {dni} — Fila {row_number}: {concepto} tiene monto {monto_fmt}"
                    f" (el monto no debería ser negativo)",
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
            monto_fmt = self._format_monto(monto)

            # Check if all candidates are "Desconocido" or empty
            all_unknown = all(
                (c.get("concept") or "").strip().casefold() == "desconocido"
                for c in candidates
            ) if candidates else True

            # Build pricing suffix if commission_prices available
            commission_prices = context_json.get("commission_prices") or {}
            pricing_suffix = ""
            if commission_prices.get("inscripcion") or commission_prices.get("cuota"):
                insc_fmt = self._format_monto(commission_prices.get("inscripcion"))
                cuota_fmt = self._format_monto(commission_prices.get("cuota"))
                parts = [f"Inscripción={insc_fmt}", f"Cuota={cuota_fmt}"]
                if commission_prices.get("pago_unico"):
                    pu_fmt = self._format_monto(commission_prices.get("pago_unico"))
                    parts.append(f"Pago Único={pu_fmt}")
                pricing_suffix = f" ({', '.join(parts)})"

            if all_unknown:
                return (
                    "Requiere definición de concepto",
                    f"Pago {payment_id or '?'} del {fecha} por {monto_fmt} — "
                    f"No se pudo determinar el concepto automáticamente. "
                    f"Revisar manualmente qué concepto corresponde.{pricing_suffix}",
                )

            candidates_str = ", ".join(
                f"{c.get('concept', '?')} ({self._format_monto(c.get('amount'))})"
                for c in candidates
            )
            return (
                "Requiere definición de concepto",
                f"Pago {payment_id or '?'} del {fecha} por {monto_fmt} — "
                f"candidatos: {candidates_str}{pricing_suffix}",
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

        # --- Guard: invalid sequence ---
        if reason == "guard:invalid_sequence":
            return self._build_guard_summary(dni, context_json)

        # --- Generic fallback ---
        dtype = context_json.get("type") or reason
        return (
            "Requiere revisión",
            f"DNI {dni} — {dtype}",
        )

    def _build_guard_summary(self, dni: str, context_json: dict[str, Any]) -> tuple[str, str]:
        """Build problema/detalle for guard:invalid_sequence reviews.

        Maps each guard reason to a specific, operator-friendly category and
        detail message with commission pricing context.
        """
        reasons: list[str] = context_json.get("reasons") or []
        pricing_insc = self._format_monto(context_json.get("pricing_inscripcion"))
        pricing_cuota = self._format_monto(context_json.get("pricing_cuota"))
        cantidad_cuotas = context_json.get("cantidad_cuotas", "?")

        if not reasons:
            return ("Requiere revisión", f"DNI {dni} — guard:invalid_sequence (sin razones)")

        # Classify each reason into (problema, detalle) pairs
        classified: list[tuple[str, str, int]] = []
        for r in reasons:
            problema, detalle, severity = self._classify_guard_reason(
                r, dni, pricing_insc, pricing_cuota, cantidad_cuotas
            )
            classified.append((problema, detalle, severity))

        # Pick the most severe reason for the problema column
        classified.sort(key=lambda x: x[2], reverse=True)
        problema = classified[0][0]

        # Combine all details
        if len(classified) == 1:
            detalle = classified[0][1]
        else:
            detalle = " | ".join(c[1] for c in classified)

        return (problema, detalle)

    def _classify_guard_reason(
        self,
        reason: str,
        dni: str,
        pricing_insc: str,
        pricing_cuota: str,
        cantidad_cuotas: Any,
    ) -> tuple[str, str, int]:
        """Classify a single guard reason into (problema, detalle, severity)."""
        if reason == "missing_inscription_with_existing_cuotas":
            return (
                "Falta inscripción",
                f"DNI {dni} — Tiene cuotas cargadas pero no aparece la inscripción"
                f" (Inscripción={pricing_insc})",
                self._GUARD_REASON_SEVERITY.get("missing_inscription_with_existing_cuotas", 0),
            )

        if reason.startswith("duplicate_cuota_"):
            cuota_num = reason.replace("duplicate_cuota_", "")
            return (
                "Cuota duplicada",
                f"DNI {dni} — Cuota {cuota_num} aparece más de una vez en la hoja",
                self._GUARD_REASON_SEVERITY.get("duplicate_cuota", 0),
            )

        if reason.startswith("missing_cuotas_before_"):
            # Format: missing_cuotas_before_5:1,2,3
            tail = reason.replace("missing_cuotas_before_", "")
            parts = tail.split(":", 1)
            cuota_num = parts[0] if parts else "?"
            missing_list = parts[1] if len(parts) > 1 else "?"
            return (
                "Cuotas faltantes",
                f"DNI {dni} — Faltan cuotas {missing_list} antes de la {cuota_num}"
                f" (Cuota={pricing_cuota})",
                self._GUARD_REASON_SEVERITY.get("missing_cuotas_before", 0),
            )

        if reason == "cuota_1_matches_inscription_amount":
            return (
                "Cuota 1 con monto de inscripción",
                f"DNI {dni} — La Cuota 1 tiene el monto de inscripción ({pricing_insc})"
                f" en vez de cuota ({pricing_cuota})",
                self._GUARD_REASON_SEVERITY.get("cuota_1_matches_inscription_amount", 0),
            )

        if reason == "cuota_1_combines_inscription_and_cuota":
            combined = self._compute_combined_amount(
                context_json_val=None,
                pricing_insc_str=pricing_insc,
                pricing_cuota_str=pricing_cuota,
            )
            return (
                "Cuota 1 combina inscripción + cuota",
                f"DNI {dni} — La Cuota 1 tiene {combined}"
                f" que parece ser inscripción + cuota juntas",
                self._GUARD_REASON_SEVERITY.get("cuota_1_combines_inscription_and_cuota", 0),
            )

        if reason == "inscription_with_non_standard_amount":
            return (
                "Inscripción con monto irregular",
                f"DNI {dni} — La inscripción tiene un monto diferente al esperado"
                f" ({pricing_insc})",
                self._GUARD_REASON_SEVERITY.get("inscription_with_non_standard_amount", 0),
            )

        if reason.startswith("cuota_exceeds_total:"):
            # Format: cuota_exceeds_total:12>9
            tail = reason.replace("cuota_exceeds_total:", "")
            parts = tail.split(">", 1)
            cuota_num = parts[0] if parts else "?"
            total = parts[1] if len(parts) > 1 else str(cantidad_cuotas)
            return (
                "Cuota excede el total",
                f"DNI {dni} — Tiene Cuota {cuota_num} pero la comisión solo tiene {total} cuotas",
                self._GUARD_REASON_SEVERITY.get("cuota_exceeds_total", 0),
            )

        # Unknown guard reason — generic with the reason text
        return (
            "Requiere revisión",
            f"DNI {dni} — {reason}",
            0,
        )

    @staticmethod
    def _compute_combined_amount(
        context_json_val: Any,
        pricing_insc_str: str,
        pricing_cuota_str: str,
    ) -> str:
        """Compute the sum of inscription + cuota for display, or return a placeholder."""
        try:
            insc_text = pricing_insc_str.replace("$", "").replace(".", "").replace(",", ".").strip()
            cuota_text = pricing_cuota_str.replace("$", "").replace(".", "").replace(",", ".").strip()
            if insc_text and cuota_text and insc_text != "s/dato" and cuota_text != "s/dato":
                combined = Decimal(insc_text) + Decimal(cuota_text)
                return ReviewManager._format_monto(combined)
        except (InvalidOperation, ValueError, TypeError):
            pass
        return f"{pricing_insc_str}+{pricing_cuota_str}"

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
        seen_payment_ids: set[int] = set()
        for review in open_reviews:
            case_id = f"REV-{review['id']}"
            if case_id in existing_case_ids:
                skipped += 1
                continue

            context_json = json.loads(review.get("context_json") or "{}")
            reason = review.get("reason", "")
            problema, detalle = self.build_problem_summary(reason, context_json)
            comision = str(context_json.get("commission") or "").strip()
            dni = str(context_json.get("dni") or "").strip()

            # Skip reviews without both comision AND dni — unactionable for operators
            if not comision and not dni:
                LOGGER.warning(
                    "skipping review %s: missing both comision and dni", case_id
                )
                skipped += 1
                continue

            # Export-time dedup: only one ambiguous review per payment_id
            payment_id = context_json.get("payment_id")
            if payment_id is not None and "ambiguous" in reason:
                if payment_id in seen_payment_ids:
                    skipped += 1
                    continue
                seen_payment_ids.add(payment_id)

            content_key = self._dedup_key(comision, dni, problema, detalle)
            if content_key in existing_content_keys:
                skipped += 1
                continue
            existing_content_keys.add(content_key)

            rows_to_append.append(
                [case_id, comision, dni, problema, detalle, ""]
            )

        # Sort by comision (column index 1) so reviews are grouped naturally
        rows_to_append.sort(key=lambda row: row[1])

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
