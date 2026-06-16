"""Sheet reconciler using graduated matching tiers.

Replaces strict ID-based diffing with evidence-based matching where IDs are
bonus signals and not mandatory.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import count

from src.models.pipeline import Allocation, Discrepancy, DiscrepancyType, Severity
from src.models.sheet import ExpectedRow, SheetRow
from src.rules.mappers import map_cobro_medio, map_medio


class SheetReconciler:
    """Compares allocations against normalized sheet rows.

    Uses graduated matching instead of primary key lookup:
    - Strong: comision + dni + concepto + monto + tipo_movimiento (± date tolerance)
    - Medium: comision + dni + monto + tipo_movimiento
    - Weak: comision + dni + tipo_movimiento (needs LLM confirmation)

    IDs (id_pago_mp, id_movimiento_bancario) are used as BONUS evidence
    to increase confidence, but are NEVER required for a match.
    """

    DATE_TOLERANCE_DAYS = 3

    def __init__(self) -> None:
        self._id_counter = count(1)

    def reconcile(
        self,
        allocations: list[Allocation],
        sheet_rows: list[SheetRow],
        next_venta: ExpectedRow | None,
        commission_name: str | None = None,
    ) -> list[Discrepancy]:
        """Compare what SHOULD be in the sheet vs what IS."""
        # Track allocations that skip Venta generation (Ventas already exist)
        self._cobro_only_concepts: set[tuple[str, str]] = set()
        for alloc in allocations:
            if not alloc.generates_venta and alloc.generates_cobro:
                dni = (alloc.payment.payment.dni_cuit_originante or "").strip()
                self._cobro_only_concepts.add((self._normalize(dni), self._normalize(alloc.concept)))

        expected_rows = self._build_expected_rows(allocations)
        # Use explicit commission name if provided, otherwise infer from context
        default_commission = (commission_name or "").strip() or self._resolve_commission(next_venta, sheet_rows)
        if default_commission:
            expected_rows = [
                row if row.comision.strip() else row.model_copy(update={"comision": default_commission})
                for row in expected_rows
            ]
        matched_indexes: set[int] = set()
        discrepancies: list[Discrepancy] = []

        # 1) Resolve all expected rows from allocations.
        for expected in expected_rows:
            available = [
                (idx, row) for idx, row in enumerate(sheet_rows) if idx not in matched_indexes
            ]
            matched = self._try_match(expected, available)
            if matched is None:
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

            matched_index, actual, _match_tier = matched
            matched_indexes.add(matched_index)
            discrepancies.extend(self._compare_fields(expected, actual))

        # 2) Validate next_venta (if present) only as existence signal.
        if next_venta is not None:
            available = [
                (idx, row) for idx, row in enumerate(sheet_rows) if idx not in matched_indexes
            ]
            matched = self._try_match(next_venta, available)
            if matched is None:
                discrepancies.append(
                    self._build_discrepancy(
                        discrepancy_type=DiscrepancyType.MISSING_ROW,
                        expected=next_venta,
                        actual=None,
                        field=None,
                        expected_value=None,
                        actual_value=None,
                    )
                )
            else:
                matched_index, _actual, _match_tier = matched
                matched_indexes.add(matched_index)

        # 3) Split detection: unmatched sheet rows whose monto equals the sum
        #    of unmatched expected rows from the same payment.
        unmatched_expected = [
            d for d in discrepancies if d.discrepancy_type == DiscrepancyType.MISSING_ROW
        ]
        if unmatched_expected:
            split_resolved = self._resolve_splits(
                unmatched_expected, sheet_rows, matched_indexes,
            )
            if split_resolved:
                # Remove the MISSING_ROWs that were resolved by splitting,
                # replace with WRONG_VALUE corrections + keep remaining MISSING_ROWs.
                resolved_ids = {d.id for resolved in split_resolved for d in resolved["removed"]}
                discrepancies = [d for d in discrepancies if d.id not in resolved_ids]
                for resolved in split_resolved:
                    matched_indexes.add(resolved["sheet_index"])
                    discrepancies.extend(resolved["corrections"])
                    discrepancies.extend(resolved["inserts"])

        # 4) Unmatched actual rows are extra rows — unless they are Ventas
        #    whose Cobro was generated by _try_allocate_ignoring_ledger
        #    (meaning the Venta already existed and is correct).
        for idx, actual in enumerate(sheet_rows):
            if idx in matched_indexes:
                continue
            if self._is_valid_preexisting_venta(actual):
                matched_indexes.add(idx)
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

    @staticmethod
    def _resolve_commission(next_venta: ExpectedRow | None, sheet_rows: list[SheetRow]) -> str:
        if next_venta is not None and next_venta.comision.strip():
            return next_venta.comision.strip()

        for row in sheet_rows:
            if row.comision and row.comision.strip():
                return row.comision.strip()

        return ""

    def _build_expected_rows(self, allocations: list[Allocation]) -> list[ExpectedRow]:
        """Convert Allocation objects to ExpectedRow objects."""
        expected_rows: list[ExpectedRow] = []

        for allocation in allocations:
            payment = allocation.payment.payment
            movement = allocation.payment.movement
            payment_date = payment.fecha.date()
            student_name = payment.razon_social_originante or ""
            medio_pago = map_cobro_medio(payment.id_medio_pago or 0, has_bank_movement=movement is not None)

            if allocation.generates_venta:
                expected_rows.append(
                    ExpectedRow(
                        comision="",
                        fecha_movimiento=payment_date,
                        tipo_movimiento="Venta",
                        dni=payment.dni_cuit_originante or "",
                        concepto=allocation.concept,
                        monto=allocation.amount,
                        medio_pago="No aplica",
                        estudiante=student_name,
                        id_movimiento_bancario=None,
                        id_pago_mp=payment.id_pago_mp,
                        source_payment=payment,
                        source_movement=movement,
                    )
                )

            if allocation.generates_cobro and movement is not None:
                expected_rows.append(
                    ExpectedRow(
                        comision="",
                        fecha_movimiento=movement.fecha,
                        tipo_movimiento="Cobro",
                        dni=payment.dni_cuit_originante or "",
                        concepto=allocation.concept,
                        monto=allocation.amount,
                        medio_pago=medio_pago,
                        estudiante=student_name,
                        id_movimiento_bancario=movement.id_movimiento,
                        id_pago_mp=payment.id_pago_mp,
                        source_payment=payment,
                        source_movement=movement,
                    )
                )

        return expected_rows

    def _try_match(
        self,
        expected: ExpectedRow,
        available: list[tuple[int, SheetRow]],
    ) -> tuple[int, SheetRow, str] | None:
        """Try graduated matching. Returns (index, row, match_tier) or None."""
        for idx, row in available:
            if self._strong_match(expected, row):
                return idx, row, "strong"

        for idx, row in available:
            if self._medium_match(expected, row):
                return idx, row, "medium"

        for idx, row in available:
            if self._weak_match(expected, row):
                return idx, row, "weak"

        return None

    def _strong_match(self, expected: ExpectedRow, row: SheetRow) -> bool:
        """comision + dni + concepto + monto + tipo_movimiento, date within tolerance"""
        return (
            self._normalize(expected.comision) == self._normalize(row.comision)
            and self._normalize(expected.dni) == self._normalize(row.dni)
            and self._normalize(expected.concepto) == self._normalize(row.concepto)
            and expected.monto == row.monto
            and self._normalize(expected.tipo_movimiento) == self._normalize(row.tipo_movimiento)
            and self._is_within_date_tolerance(expected.fecha_movimiento, row.fecha_movimiento)
        )

    def _medium_match(self, expected: ExpectedRow, row: SheetRow) -> bool:
        """comision + dni + monto + tipo_movimiento"""
        return (
            self._normalize(expected.comision) == self._normalize(row.comision)
            and self._normalize(expected.dni) == self._normalize(row.dni)
            and expected.monto == row.monto
            and self._normalize(expected.tipo_movimiento) == self._normalize(row.tipo_movimiento)
        )

    def _weak_match(self, expected: ExpectedRow, row: SheetRow) -> bool:
        """comision + dni + tipo_movimiento"""
        return (
            self._normalize(expected.comision) == self._normalize(row.comision)
            and self._normalize(expected.dni) == self._normalize(row.dni)
            and self._normalize(expected.tipo_movimiento) == self._normalize(row.tipo_movimiento)
        )

    def _resolve_splits(
        self,
        unmatched_expected: list[Discrepancy],
        sheet_rows: list[SheetRow],
        matched_indexes: set[int],
    ) -> list[dict]:
        """Detect sheet rows with combined amounts that should be split.

        For each unmatched sheet row, check if its monto equals the sum of
        unmatched expected rows from the same payment (id_pago_mp). If so,
        reuse the sheet row for the first expected concept (WRONG_VALUE to
        correct monto+concepto) and leave the rest as MISSING_ROW inserts.
        """
        results: list[dict] = []

        # Group unmatched expected rows by (dni, tipo_movimiento).
        # Try to find unmatched sheet rows whose monto equals the sum of the group.
        by_person_type: dict[tuple[str, str], list[Discrepancy]] = {}
        for disc in unmatched_expected:
            exp = disc.expected_row
            if exp is None:
                continue
            key = (self._normalize(exp.dni), self._normalize(exp.tipo_movimiento))
            by_person_type.setdefault(key, []).append(disc)

        consumed_sheet_indexes: set[int] = set()

        for (dni_key, tipo_key), group in by_person_type.items():
            if len(group) < 2:
                continue

            group_sum = sum(d.expected_row.monto for d in group if d.expected_row)

            for idx, actual in enumerate(sheet_rows):
                if idx in matched_indexes or idx in consumed_sheet_indexes:
                    continue
                if self._normalize(actual.tipo_movimiento) != tipo_key:
                    continue
                if self._normalize(actual.dni) != dni_key:
                    continue
                if actual.monto != group_sum:
                    continue

                # Found a split: reuse this row for first concept, insert the rest
                # Sort so Inscripción comes before Cuota
                sorted_group = sorted(group, key=lambda d: d.expected_row.concepto if d.expected_row else "")
                # Try to match existing concepto to keep the row for that concept
                concept_match = next(
                    (d for d in sorted_group if d.expected_row and self._normalize(d.expected_row.concepto) == self._normalize(actual.concepto)),
                    None,
                )
                if concept_match is not None:
                    first = concept_match
                    rest = [d for d in sorted_group if d is not concept_match]
                else:
                    first = sorted_group[0]
                    rest = sorted_group[1:]

                corrections: list[Discrepancy] = []
                if first.expected_row:
                    # Correct monto — high confidence because the split sum is exact
                    if first.expected_row.monto != actual.monto:
                        disc = self._build_discrepancy(
                            discrepancy_type=DiscrepancyType.WRONG_VALUE,
                            expected=first.expected_row,
                            actual=actual,
                            field="monto",
                            expected_value=self._decimal_str(first.expected_row.monto),
                            actual_value=self._decimal_str(actual.monto),
                        )
                        disc.confidence = 0.98
                        disc.resolved_by = "split_detection"
                        corrections.append(disc)
                    # Correct concepto if different
                    if self._normalize(first.expected_row.concepto) != self._normalize(actual.concepto):
                        disc = self._build_discrepancy(
                            discrepancy_type=DiscrepancyType.WRONG_VALUE,
                            expected=first.expected_row,
                            actual=actual,
                            field="concepto",
                            expected_value=first.expected_row.concepto,
                            actual_value=actual.concepto,
                        )
                        disc.confidence = 0.98
                        disc.resolved_by = "split_detection"
                        corrections.append(disc)

                # Rest stay as MISSING_ROW (they'll be inserted)
                inserts = [d for d in rest]

                consumed_sheet_indexes.add(idx)
                results.append({
                    "sheet_index": idx,
                    "removed": sorted_group,  # all original MISSING_ROWs replaced
                    "corrections": corrections,
                    "inserts": inserts,
                })
                break  # one sheet row per payment group

        return results

    def _compare_fields(self, expected: ExpectedRow, actual: SheetRow) -> list[Discrepancy]:
        discrepancies: list[Discrepancy] = []

        comparable_fields = (
            ("fecha_movimiento", expected.fecha_movimiento.isoformat(), self._date_str(actual.fecha_movimiento)),
            ("concepto", expected.concepto, actual.concepto),
            ("monto", self._decimal_str(expected.monto), self._decimal_str(actual.monto)),
            ("medio_pago", expected.medio_pago, actual.medio_pago),
        )

        for field, expected_value, actual_value in comparable_fields:
            if self._normalize(expected_value) == self._normalize(actual_value):
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

    def _is_within_date_tolerance(self, expected_date: date, actual_date: date | None) -> bool:
        if actual_date is None:
            return False
        diff = abs((expected_date - actual_date).days)
        return diff <= self.DATE_TOLERANCE_DAYS

    @staticmethod
    def _decimal_str(value: Decimal) -> str:
        # Normalize trailing zeros so 54800.0000 == 54800
        normalized = value.normalize()
        return format(normalized, "f")

    @staticmethod
    def _date_str(value: object) -> str:
        if value is None:
            return ""
        return str(value)

    def _is_valid_preexisting_venta(self, row: SheetRow) -> bool:
        """Check if an unmatched row is a Venta that already exists for a cobro-only allocation."""
        if self._normalize(row.tipo_movimiento) != "venta":
            return False
        key = (self._normalize(row.dni), self._normalize(row.concepto))
        return key in self._cobro_only_concepts

    @staticmethod
    def _normalize(value: str | None) -> str:
        if value is None:
            return ""
        return value.strip().casefold()
