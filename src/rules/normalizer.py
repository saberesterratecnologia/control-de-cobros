"""Sheet row normalizer and anomaly detector."""

from __future__ import annotations

from src.models.pipeline import Anomaly, Severity
from src.models.sheet import SheetRow
from src.rules.mappers import normalize_medio


class SheetNormalizer:
    """Normalizes sheet rows and detects anomalies before comparison."""

    def normalize(self, rows: list[SheetRow]) -> tuple[list[SheetRow], list[Anomaly]]:
        """Return (normalized_rows, anomalies).

        Normalization rules:
        1. Normalize medio_pago via ``normalize_medio``.
        2. Strip whitespace from key string fields.
        3. Set id_movimiento_bancario / id_pago_mp to ``None`` when <= 0.

        Anomaly detection (rows are still returned; they also appear in the
        anomalies list so the pipeline can flag them):
        1. ``cobro_no_aplica``: Cobro with medio_pago == "No aplica".
        2. ``venta_with_movement``: Venta with a non-null id_movimiento_bancario.
        3. ``missing_medio``: Cobro with empty/blank medio_pago.
        4. ``negative_monto``: Any row with monto <= 0.
        """
        normalized: list[SheetRow] = []
        anomalies: list[Anomaly] = []

        for row in rows:
            updates: dict = {
                "medio_pago": normalize_medio(row.medio_pago),
                "concepto": row.concepto.strip() if row.concepto else "",
                "dni": row.dni.strip() if row.dni else "",
            }
            if row.comision is not None:
                updates["comision"] = row.comision.strip()
            if row.id_movimiento_bancario is not None and row.id_movimiento_bancario <= 0:
                updates["id_movimiento_bancario"] = None
            if row.id_pago_mp is not None and row.id_pago_mp <= 0:
                updates["id_pago_mp"] = None

            norm = row.model_copy(update=updates)

            # --- anomaly detection ---
            if norm.tipo_movimiento == "Cobro" and norm.medio_pago == "No aplica":
                anomalies.append(
                    Anomaly(
                        row_number=norm.row_number,
                        anomaly_type="cobro_no_aplica",
                        description=f"Cobro row {norm.row_number} has 'No aplica' as medio de pago",
                        severity=Severity.WARNING,
                    )
                )

            if norm.tipo_movimiento == "Venta" and norm.id_movimiento_bancario is not None:
                anomalies.append(
                    Anomaly(
                        row_number=norm.row_number,
                        anomaly_type="venta_with_movement",
                        description=(
                            f"Venta row {norm.row_number} has "
                            f"id_movimiento_bancario={norm.id_movimiento_bancario}"
                        ),
                        severity=Severity.WARNING,
                    )
                )

            if norm.tipo_movimiento == "Cobro" and not norm.medio_pago.strip():
                anomalies.append(
                    Anomaly(
                        row_number=norm.row_number,
                        anomaly_type="missing_medio",
                        description=f"Cobro row {norm.row_number} has no medio de pago",
                        severity=Severity.WARNING,
                    )
                )

            if norm.monto <= 0:
                anomalies.append(
                    Anomaly(
                        row_number=norm.row_number,
                        anomaly_type="negative_monto",
                        description=f"Row {norm.row_number} has non-positive monto: {norm.monto}",
                        severity=Severity.CRITICAL,
                    )
                )

            normalized.append(norm)

        return normalized, anomalies
