"""Confidence and severity scoring for discrepancies."""

from __future__ import annotations

from decimal import Decimal

from src.models.pipeline import Discrepancy, DiscrepancyType, Severity


class ConfidenceScorer:
    """Assigns confidence and severity to discrepancy records."""

    def score(
        self,
        discrepancy: Discrepancy,
        commission_pricing: dict[str, Decimal] | None = None,
    ) -> float:
        if discrepancy.discrepancy_type == DiscrepancyType.MISSING_ROW:
            if self._has_strong_ids(discrepancy):
                return 0.98
            return 0.80

        if discrepancy.discrepancy_type == DiscrepancyType.WRONG_VALUE:
            return self._wrong_value_score(discrepancy, commission_pricing)

        if discrepancy.discrepancy_type == DiscrepancyType.EXTRA_ROW:
            actual = discrepancy.actual_row
            if actual and actual.id_pago_mp is not None:
                return 0.60
            return 0.50

        if discrepancy.discrepancy_type == DiscrepancyType.DUPLICATE:
            if self._is_exact_duplicate(discrepancy):
                return 0.95
            return 0.70

        return 0.50

    def score_with_tier(self, discrepancy: Discrepancy, match_tier: str | None = None) -> float:
        """Score discrepancy with optional match-tier bonus/penalty.

        match_tier: "strong" -> +0.15, "medium" -> +0.0, "weak" -> -0.15.
        Plus ID bonus: if expected/actual IDs match -> +0.05.
        """
        base_score = self.score(discrepancy)
        tier_adjustment = {
            "strong": 0.15,
            "medium": 0.0,
            "weak": -0.15,
        }.get(match_tier or "", 0.0)

        id_bonus = 0.05 if self._has_matching_ids(discrepancy) else 0.0
        final_score = base_score + tier_adjustment + id_bonus
        return max(0.0, min(1.0, final_score))

    def assign_severity(self, discrepancy: Discrepancy) -> Severity:
        if discrepancy.discrepancy_type == DiscrepancyType.MISSING_ROW:
            return Severity.CRITICAL

        if discrepancy.discrepancy_type == DiscrepancyType.WRONG_VALUE:
            if discrepancy.field == "monto":
                percent = self._monto_percent_difference(discrepancy)
                if percent is not None and percent > Decimal("0.10"):
                    return Severity.CRITICAL
            return Severity.WARNING

        if discrepancy.discrepancy_type == DiscrepancyType.EXTRA_ROW:
            if discrepancy.actual_row and discrepancy.actual_row.id_pago_mp is None:
                return Severity.INFO
            return Severity.WARNING

        if discrepancy.discrepancy_type == DiscrepancyType.DUPLICATE:
            return Severity.INFO

        return Severity.INFO

    @staticmethod
    def _has_strong_ids(discrepancy: Discrepancy) -> bool:
        expected = discrepancy.expected_row
        return bool(expected and expected.id_pago_mp and expected.id_movimiento_bancario is not None)

    @staticmethod
    def _has_matching_ids(discrepancy: Discrepancy) -> bool:
        expected = discrepancy.expected_row
        actual = discrepancy.actual_row
        if not expected or not actual:
            return False

        pago_matches = (
            expected.id_pago_mp is not None
            and actual.id_pago_mp is not None
            and expected.id_pago_mp == actual.id_pago_mp
        )
        movimiento_matches = (
            expected.id_movimiento_bancario is not None
            and actual.id_movimiento_bancario is not None
            and expected.id_movimiento_bancario == actual.id_movimiento_bancario
        )
        return pago_matches or movimiento_matches

    def _wrong_value_score(
        self,
        discrepancy: Discrepancy,
        commission_pricing: dict[str, Decimal] | None = None,
    ) -> float:
        if discrepancy.field == "monto":
            # Check if the actual monto is a known combination of commission prices.
            # If so, the expected value is almost certainly correct (it's a split
            # that was loaded as a single row). Give high confidence.
            if commission_pricing and self._is_known_price_combination(discrepancy, commission_pricing):
                return 0.95

            percent = self._monto_percent_difference(discrepancy)
            if percent is None:
                return 0.50
            if percent <= Decimal("0.10"):
                return 0.92
            if percent > Decimal("0.20"):
                return 0.50
            return 0.70

        if discrepancy.field == "concepto":
            return 0.70

        if discrepancy.field == "fecha_movimiento":
            days = self._date_difference_days(discrepancy)
            if days is None:
                return 0.40
            if days <= 30:
                return 0.92
            if days <= 60:
                return 0.70
            return 0.40

        if discrepancy.field == "medio_pago":
            return 0.75

        return 0.60

    @staticmethod
    def _is_known_price_combination(
        discrepancy: Discrepancy,
        pricing: dict[str, Decimal],
    ) -> bool:
        """Check if the actual monto is a sum of known commission prices.

        Detects cases like inscripcion+cuota, 2*cuota, inscripcion+N*cuota
        where the sheet row has the combined total but should be split.
        """
        actual = discrepancy.actual_row
        expected = discrepancy.expected_row
        if not actual or not expected:
            return False

        actual_monto = actual.monto
        insc = pricing.get("inscripcion", Decimal("0"))
        cuota = pricing.get("cuota", Decimal("0"))
        cant = int(pricing.get("cantidad_cuotas", 0))

        if insc <= 0 and cuota <= 0:
            return False

        known_sums: list[Decimal] = []
        # inscripcion + N cuotas
        if insc > 0 and cuota > 0:
            for n in range(1, cant + 1):
                known_sums.append(insc + cuota * n)
        # N cuotas (without inscription)
        if cuota > 0:
            for n in range(2, cant + 1):
                known_sums.append(cuota * n)

        tolerance = Decimal("0.01")
        for target in known_sums:
            if target == 0:
                continue
            if abs(actual_monto - target) / target <= tolerance:
                return True
        return False

    @staticmethod
    def _is_exact_duplicate(discrepancy: Discrepancy) -> bool:
        expected = discrepancy.expected_row
        actual = discrepancy.actual_row
        if not expected or not actual:
            return False

        return (
            expected.dni == actual.dni
            and expected.concepto.strip().casefold() == actual.concepto.strip().casefold()
            and expected.monto == actual.monto
            and expected.medio_pago.strip().casefold() == actual.medio_pago.strip().casefold()
            and expected.fecha_movimiento == actual.fecha_movimiento
        )

    @staticmethod
    def _monto_percent_difference(discrepancy: Discrepancy) -> Decimal | None:
        expected = discrepancy.expected_row
        actual = discrepancy.actual_row
        if not expected or not actual:
            return None
        if expected.monto == Decimal("0"):
            return None
        return abs(actual.monto - expected.monto) / expected.monto

    @staticmethod
    def _date_difference_days(discrepancy: Discrepancy) -> int | None:
        expected = discrepancy.expected_row
        actual = discrepancy.actual_row
        if not expected or not actual or actual.fecha_movimiento is None:
            return None
        return abs((expected.fecha_movimiento - actual.fecha_movimiento).days)
