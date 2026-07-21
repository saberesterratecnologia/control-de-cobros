"""Tests for estado_administrativo pure functions."""
from __future__ import annotations

from datetime import date

from scripts.estado_administrativo.actualizar_estado import (
    determine_new_state,
    expected_cuotas_paid,
    extract_max_cuota,
)


# ---------------------------------------------------------------------------
# extract_max_cuota
# ---------------------------------------------------------------------------

class TestExtractMaxCuota:
    def test_cuota_3(self) -> None:
        assert extract_max_cuota("Cuota 3") == 3

    def test_cuota_10(self) -> None:
        assert extract_max_cuota("Cuota 10") == 10

    def test_case_insensitive(self) -> None:
        assert extract_max_cuota("cuota 1") == 1

    def test_inscripcion_returns_none(self) -> None:
        assert extract_max_cuota("Inscripcion") is None

    def test_empty_string_returns_none(self) -> None:
        assert extract_max_cuota("") is None

    def test_extra_text_after(self) -> None:
        assert extract_max_cuota("Cuota 3 - Marzo") == 3


# ---------------------------------------------------------------------------
# expected_cuotas_paid
# ---------------------------------------------------------------------------

class TestExpectedCuotasPaid:
    def test_same_month_returns_zero(self) -> None:
        """Start month is always inside the grace window."""
        assert expected_cuotas_paid(date(2026, 6, 1), date(2026, 6, 15)) == 0

    def test_today_before_start_returns_zero(self) -> None:
        assert expected_cuotas_paid(date(2026, 6, 1), date(2026, 5, 1)) == 0

    def test_following_month_before_grace_day_still_zero(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 4, 1)) == 0

    def test_following_month_grace_day_still_keeps_first_cuota_out(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 4, 15)) == 0

    def test_following_month_day_after_grace_counts_first_cuota(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 4, 16)) == 1

    def test_july_first_only_counts_through_may(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 7, 1)) == 3

    def test_july_fifteenth_keeps_june_in_grace(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 7, 15)) == 3

    def test_july_sixteenth_counts_june(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 7, 16)) == 4

    def test_august_first_keeps_july_in_grace(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 8, 1)) == 4

    def test_august_fifteenth_keeps_july_in_grace(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 8, 15)) == 4

    def test_august_sixteenth_counts_july(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 8, 16)) == 5

    def test_cross_year(self) -> None:
        assert expected_cuotas_paid(date(2025, 11, 1), date(2026, 2, 1)) == 2


# ---------------------------------------------------------------------------
# determine_new_state
# ---------------------------------------------------------------------------

class TestDetermineNewState:
    def test_before_grace_day_missing_previous_month_is_still_sin_deuda(self) -> None:
        assert determine_new_state({1, 2, 3}, date(2026, 3, 1), date(2026, 7, 14), 12) == 5

    def test_grace_day_still_keeps_same_deficit_sin_deuda(self) -> None:
        assert determine_new_state({1, 2, 3}, date(2026, 3, 1), date(2026, 7, 15), 12) == 5

    def test_day_after_grace_moves_same_deficit_to_deuda_1_mes(self) -> None:
        assert determine_new_state({1, 2, 3}, date(2026, 3, 1), date(2026, 7, 16), 12) == 6

    def test_first_day_of_next_month_escalates_same_deficit_to_deuda_2_meses(self) -> None:
        assert determine_new_state({1, 2, 3}, date(2026, 3, 1), date(2026, 8, 1), 12) == 7

    def test_paid_up_to_due_count_stays_sin_deuda(self) -> None:
        assert determine_new_state({1, 2, 3, 4}, date(2026, 3, 1), date(2026, 8, 1), 12) == 5

    def test_zero_expected_zero_paid(self) -> None:
        assert determine_new_state(set(), date(2026, 6, 1), date(2026, 6, 10), 12) == 5

    def test_paid_ahead_sin_deuda(self) -> None:
        assert determine_new_state({1, 2, 3, 4, 5}, date(2026, 3, 1), date(2026, 7, 16), 12) == 5

    def test_gap_uses_oldest_missing_cuota_age(self) -> None:
        assert determine_new_state({1, 3}, date(2026, 3, 1), date(2026, 6, 20), 12) == 7

    def test_total_cuotas_caps_expected(self) -> None:
        assert determine_new_state({1, 2, 3, 4, 5, 6}, date(2026, 3, 1), date(2027, 3, 1), 6) == 5


# ---------------------------------------------------------------------------
# Bug fix scenario: max() vs len()
# ---------------------------------------------------------------------------

class TestBugFixMaxVsLen:
    """Verify the fix: use the real set of paid cuotas, not a simple counter.

    Scenario: student paid Cuota 1 and Cuota 3, skipping Cuota 2.
    The debt state must be based on the missing cuota's age, not only on
    how many cuotas exist.
    """

    def test_skipped_cuota_uses_missing_month_not_only_count(self) -> None:
        cuotas_paid = {1, 3}
        assert determine_new_state(cuotas_paid, date(2026, 3, 1), date(2026, 6, 20), 12) == 7

    def test_sequential_cuotas_same_result(self) -> None:
        """When cuotas ARE sequential, the student stays out of debt."""
        cuotas_paid = {1, 2, 3, 4}
        assert determine_new_state(cuotas_paid, date(2026, 3, 1), date(2026, 7, 16), 12) == 5
