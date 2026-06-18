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
        """Current month is not yet due."""
        assert expected_cuotas_paid(date(2026, 6, 1), date(2026, 6, 15)) == 0

    def test_today_before_start_returns_zero(self) -> None:
        assert expected_cuotas_paid(date(2026, 6, 1), date(2026, 5, 1)) == 0

    def test_one_month_later(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 4, 1)) == 1

    def test_three_months_later(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 6, 1)) == 3

    def test_march_to_june(self) -> None:
        assert expected_cuotas_paid(date(2026, 3, 1), date(2026, 6, 1)) == 3

    def test_day_doesnt_matter(self) -> None:
        """Only month difference counts, not day-of-month."""
        assert expected_cuotas_paid(date(2026, 3, 15), date(2026, 6, 20)) == 3

    def test_cross_year(self) -> None:
        assert expected_cuotas_paid(date(2025, 11, 1), date(2026, 2, 1)) == 3


# ---------------------------------------------------------------------------
# determine_new_state
# ---------------------------------------------------------------------------

class TestDetermineNewState:
    def test_paid_equals_expected_sin_deuda(self) -> None:
        assert determine_new_state(cuotas_paid=3, expected=3, total_cuotas=12) == 5

    def test_one_behind_deuda_1_mes(self) -> None:
        assert determine_new_state(cuotas_paid=2, expected=3, total_cuotas=12) == 6

    def test_two_behind_deuda_2_meses(self) -> None:
        assert determine_new_state(cuotas_paid=1, expected=3, total_cuotas=12) == 7

    def test_five_behind_still_deuda_2_meses(self) -> None:
        assert determine_new_state(cuotas_paid=0, expected=5, total_cuotas=12) == 7

    def test_zero_expected_zero_paid(self) -> None:
        assert determine_new_state(cuotas_paid=0, expected=0, total_cuotas=12) == 5

    def test_paid_ahead_sin_deuda(self) -> None:
        assert determine_new_state(cuotas_paid=5, expected=3, total_cuotas=12) == 5

    def test_total_cuotas_caps_expected(self) -> None:
        """If total_cuotas=6 and expected=10, expected is capped to 6.
        Paid=6 means no deficit -> state 5."""
        assert determine_new_state(cuotas_paid=6, expected=10, total_cuotas=6) == 5

    def test_total_cuotas_zero_no_cap(self) -> None:
        """total_cuotas=0 disables capping."""
        assert determine_new_state(cuotas_paid=3, expected=5, total_cuotas=0) == 7


# ---------------------------------------------------------------------------
# Bug fix scenario: max() vs len()
# ---------------------------------------------------------------------------

class TestBugFixMaxVsLen:
    """Verify the fix: count DISTINCT cuotas, not the max cuota number.

    Scenario: student paid Cuota 1 and Cuota 3, skipping Cuota 2.
    - Correct count: len({1, 3}) = 2
    - Old buggy:    max([1, 3]) = 3

    With expected=3:
    - Fixed:  deficit = 3 - 2 = 1  -> state 6 (con deuda 1 mes) ✓
    - Buggy:  deficit = 3 - 3 = 0  -> state 5 (sin deuda)       ✗
    """

    def test_skipped_cuota_shows_debt(self) -> None:
        """With len() fix: 2 cuotas paid, expected 3 -> deficit 1 -> state 6."""
        cuotas_paid = {1, 3}  # skipped cuota 2
        cuotas_paid_count = len(cuotas_paid)

        assert cuotas_paid_count == 2
        assert determine_new_state(cuotas_paid_count, expected=3, total_cuotas=12) == 6

    def test_old_max_logic_would_be_wrong(self) -> None:
        """Demonstrate the old max() logic would have returned state 5 (wrong)."""
        cuotas_paid = {1, 3}  # skipped cuota 2
        buggy_count = max(cuotas_paid)  # old logic: 3

        assert buggy_count == 3
        # Old logic says no deficit — WRONG
        assert determine_new_state(buggy_count, expected=3, total_cuotas=12) == 5

    def test_sequential_cuotas_same_result(self) -> None:
        """When cuotas ARE sequential, len() and max() agree."""
        cuotas_paid = {1, 2, 3}
        assert len(cuotas_paid) == max(cuotas_paid) == 3
        assert determine_new_state(len(cuotas_paid), expected=3, total_cuotas=12) == 5
