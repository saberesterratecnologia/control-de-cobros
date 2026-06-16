"""Diff engine to compare expected rows against sheet rows."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from itertools import count

from src.models.pipeline import Discrepancy, DiscrepancyType, Severity
from src.models.sheet import ExpectedRow, SheetRow


class DiffEngine:
    """Compares expected rows (DB/rules) vs actual sheet rows."""

    DATE_TOLERANCE_DAYS = 3

    def __init__(self) -> None:
        self._id_counter = count(1)

    def compare(
        self,
        expected_rows: list[ExpectedRow],
        actual_rows: list[SheetRow],
    ) -> list[Discrepancy]:
        primary_index = self._build_primary_index(actual_rows)
        fallback_index = self._build_fallback_index(actual_rows)
        matched_actual_indexes: set[int] = set()
        discrepancies: list[Discrepancy] = []

        for expected in expected_rows:
            candidates = self._primary_candidates(expected, primary_index, matched_actual_indexes)
            if not candidates:
                candidates = self._fallback_candidates(expected, fallback_index, matched_actual_indexes)

            if not candidates:
                discrepancies.append(
                    self._build_discrepancy(
                        discrepancy_type=DiscrepancyType.MISSING_ROW,
                        expected=expected,
                        actual=None,
                        field=None,
                        expected_value=None,
                        actual_value=None,
                    )
                )
                continue

            chosen_index, chosen_row = candidates[0]
            matched_actual_indexes.add(chosen_index)

            if len(candidates) > 1:
                discrepancies.append(
                    self._build_discrepancy(
                        discrepancy_type=DiscrepancyType.DUPLICATE,
                        expected=expected,
                        actual=chosen_row,
                        field=None,
                        expected_value=None,
                        actual_value=None,
                    )
                )

            discrepancies.extend(self._compare_fields(expected, chosen_row))

        for actual_index, actual in enumerate(actual_rows):
            if actual_index in matched_actual_indexes:
                continue
            discrepancies.append(
                self._build_discrepancy(
                    discrepancy_type=DiscrepancyType.EXTRA_ROW,
                    expected=None,
                    actual=actual,
                    field=None,
                    expected_value=None,
                    actual_value=None,
                )
            )

        return discrepancies

    def _build_primary_index(self, rows: list[SheetRow]) -> dict[tuple[str, ...], list[tuple[int, SheetRow]]]:
        """Build lookup dict by primary keys."""
        index: dict[tuple[str, ...], list[tuple[int, SheetRow]]] = defaultdict(list)

        for row_index, row in enumerate(rows):
            if row.id_pago_mp is None:
                continue

            if row.id_movimiento_bancario is not None:
                key = ("cobro", str(row.id_pago_mp), str(row.id_movimiento_bancario))
                index[key].append((row_index, row))

            key = (
                "venta",
                str(row.id_pago_mp),
                row.tipo_movimiento.strip().casefold(),
                row.concepto.strip().casefold(),
            )
            index[key].append((row_index, row))

        return index

    def _build_fallback_index(
        self,
        rows: list[SheetRow],
    ) -> dict[tuple[str, Decimal, str], list[tuple[int, SheetRow]]]:
        """Build lookup dict by (dni, monto, medio_pago)."""
        index: dict[tuple[str, Decimal, str], list[tuple[int, SheetRow]]] = defaultdict(list)

        for row_index, row in enumerate(rows):
            key = self._fallback_base_key(row.dni, row.monto, row.medio_pago)
            index[key].append((row_index, row))

        return index

    def _compare_fields(self, expected: ExpectedRow, actual: SheetRow) -> list[Discrepancy]:
        """Field-by-field comparison, returning WRONG_VALUE discrepancies."""
        discrepancies: list[Discrepancy] = []

        comparable_fields = (
            ("fecha_movimiento", expected.fecha_movimiento.isoformat(), self._date_str(actual.fecha_movimiento)),
            ("concepto", expected.concepto, actual.concepto),
            ("monto", self._decimal_str(expected.monto), self._decimal_str(actual.monto)),
            ("medio_pago", expected.medio_pago, actual.medio_pago),
        )

        for field, expected_value, actual_value in comparable_fields:
            if self._normalize_value(expected_value) == self._normalize_value(actual_value):
                continue

            discrepancies.append(
                self._build_discrepancy(
                    discrepancy_type=DiscrepancyType.WRONG_VALUE,
                    expected=expected,
                    actual=actual,
                    field=field,
                    expected_value=expected_value,
                    actual_value=actual_value,
                )
            )

        return discrepancies

    def _primary_candidates(
        self,
        expected: ExpectedRow,
        primary_index: dict[tuple[str, ...], list[tuple[int, SheetRow]]],
        matched_actual_indexes: set[int],
    ) -> list[tuple[int, SheetRow]]:
        if expected.id_movimiento_bancario is not None:
            key = ("cobro", str(expected.id_pago_mp), str(expected.id_movimiento_bancario))
            return self._filter_unmatched(primary_index.get(key, []), matched_actual_indexes)

        if expected.tipo_movimiento.strip().casefold() == "venta":
            key = (
                "venta",
                str(expected.id_pago_mp),
                "venta",
                expected.concepto.strip().casefold(),
            )
            return self._filter_unmatched(primary_index.get(key, []), matched_actual_indexes)

        return []

    def _fallback_candidates(
        self,
        expected: ExpectedRow,
        fallback_index: dict[tuple[str, Decimal, str], list[tuple[int, SheetRow]]],
        matched_actual_indexes: set[int],
    ) -> list[tuple[int, SheetRow]]:
        key = self._fallback_base_key(expected.dni, expected.monto, expected.medio_pago)
        bucket = self._filter_unmatched(fallback_index.get(key, []), matched_actual_indexes)

        matched: list[tuple[int, SheetRow]] = []
        for row_index, row in bucket:
            if not self._is_within_date_tolerance(expected, row):
                continue
            if row.concepto.strip().casefold() != expected.concepto.strip().casefold():
                continue
            matched.append((row_index, row))

        return matched

    def _is_within_date_tolerance(self, expected: ExpectedRow, actual: SheetRow) -> bool:
        if actual.fecha_movimiento is None:
            return False
        diff = abs((expected.fecha_movimiento - actual.fecha_movimiento).days)
        return diff <= self.DATE_TOLERANCE_DAYS

    @staticmethod
    def _fallback_base_key(dni: str, monto: Decimal, medio_pago: str) -> tuple[str, Decimal, str]:
        return (dni.strip(), monto, medio_pago.strip().casefold())

    @staticmethod
    def _filter_unmatched(
        rows: list[tuple[int, SheetRow]],
        matched_actual_indexes: set[int],
    ) -> list[tuple[int, SheetRow]]:
        return [(row_index, row) for row_index, row in rows if row_index not in matched_actual_indexes]

    def _build_discrepancy(
        self,
        discrepancy_type: DiscrepancyType,
        expected: ExpectedRow | None,
        actual: SheetRow | None,
        field: str | None,
        expected_value: str | None,
        actual_value: str | None,
    ) -> Discrepancy:
        commission = expected.comision if expected else (actual.comision or "")
        dni = expected.dni if expected else actual.dni

        return Discrepancy(
            id=f"disc-{next(self._id_counter)}",
            commission=commission,
            dni=dni,
            discrepancy_type=discrepancy_type,
            field=field,
            expected_value=expected_value,
            actual_value=actual_value,
            expected_row=expected,
            actual_row=actual,
            confidence=0.0,
            severity=Severity.INFO,
            resolution=None,
            resolved_by=None,
        )

    @staticmethod
    def _normalize_value(value: str | None) -> str:
        if value is None:
            return ""
        return value.strip().casefold()

    @staticmethod
    def _decimal_str(value: Decimal) -> str:
        return format(value, "f")

    @staticmethod
    def _date_str(value: object) -> str:
        if value is None:
            return ""
        return str(value)
