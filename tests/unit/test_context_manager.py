from __future__ import annotations

from pathlib import Path

from src.context.context_manager import ContextManager
from src.models.pipeline import (
    Discrepancy,
    DiscrepancyType,
    PatchAction,
    PatchActionType,
    Resolution,
    Severity,
)


def _schema_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "src" / "context" / "schema.sql")


def test_run_lifecycle_start_end_and_current() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run(mode="dry_run")
        current = manager.get_current_run()

        assert current is not None
        assert current["id"] == run_id
        assert current["status"] == "running"

        manager.end_run(run_id, status="completed", summary={"ok": True})
        assert manager.get_current_run() is None


def test_discrepancy_save_and_retrieve() -> None:
    discrepancy = Discrepancy(
        id="disc-1",
        commission="C1",
        dni="30111222",
        discrepancy_type=DiscrepancyType.WRONG_VALUE,
        field="monto",
        expected_value="15000",
        actual_value="14500",
        expected_row=None,
        actual_row=None,
        confidence=0.9,
        severity=Severity.WARNING,
        resolution=Resolution.AUTO_FIX,
        resolved_by="rules",
    )

    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_discrepancy(run_id, discrepancy)

        rows = manager.get_discrepancies(run_id)
        assert len(rows) == 1
        assert rows[0]["dni"] == "30111222"
        assert rows[0]["discrepancy_type"] == "wrong_value"


def test_decision_cache_lookup_by_hash() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_decision(
            run_id=run_id,
            discrepancy_id=None,
            input_hash="hash-abc",
            model_used="gpt-4o-mini",
            decision_json={"action": "correct"},
            confidence=0.92,
        )

        cached = manager.get_decision_by_hash("hash-abc")
        assert cached is not None
        assert cached["model_used"] == "gpt-4o-mini"


def test_patch_idempotency_check() -> None:
    patch = PatchAction(
        id="patch-1",
        action_type=PatchActionType.UPDATE_CELL,
        row_number=3,
        column="H",
        old_value="100",
        new_value="200",
        source_discrepancy_id="disc-1",
        status="applied",
    )

    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_patch(run_id, patch)

        assert manager.is_already_applied(patch.idempotency_key) is True
        assert manager.is_already_applied("not-existing") is False


def test_checkpoint_save_and_restore() -> None:
    with ContextManager(":memory:", schema_path=_schema_path()) as manager:
        run_id = manager.start_run()
        manager.save_checkpoint(
            run_id=run_id,
            phase="write",
            checkpoint_data={"last_patch": 50},
            commission_name="Comisión A",
        )

        checkpoint = manager.get_last_checkpoint(run_id, phase="write")
        assert checkpoint is not None
        assert checkpoint["phase"] == "write"


# ===================================================================
# Dedup guard: save_pending_review — creation-time dedup by payment_id
# ===================================================================


class TestSavePendingReviewDedup:
    """Verify creation-time dedup guard for ambiguous reviews by payment_id."""

    def test_skips_duplicate_ambiguous_payment(self) -> None:
        """Two ambiguous reviews with same payment_id: second is skipped, only 1 row."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            ctx = {"payment_id": 100, "commission": "C1", "dni": "111"}

            first_id = mgr.save_pending_review(run_id, None, "ambiguous_allocation:auto", ctx)
            second_id = mgr.save_pending_review(run_id, None, "ambiguous_allocation:manual", ctx)

            assert second_id == first_id
            reviews = mgr.get_all_open_reviews(run_id)
            assert len(reviews) == 1

    def test_returns_existing_id_on_dedup(self) -> None:
        """Dedup hit must return the id of the first review, not a new one."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            ctx = {"payment_id": 200, "commission": "C2", "dni": "222"}

            first_id = mgr.save_pending_review(run_id, None, "ambiguous", ctx)
            dup_id = mgr.save_pending_review(run_id, None, "ambiguous", ctx)

            assert dup_id == first_id
            assert isinstance(first_id, int)
            assert first_id > 0

    def test_allows_different_payment_ids(self) -> None:
        """Two ambiguous reviews with different payment_ids: both inserted."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            ctx_a = {"payment_id": 300, "commission": "C3", "dni": "333"}
            ctx_b = {"payment_id": 301, "commission": "C3", "dni": "333"}

            id_a = mgr.save_pending_review(run_id, None, "ambiguous_allocation:auto", ctx_a)
            id_b = mgr.save_pending_review(run_id, None, "ambiguous_allocation:auto", ctx_b)

            assert id_a != id_b
            reviews = mgr.get_all_open_reviews(run_id)
            assert len(reviews) == 2

    def test_allows_pago_no_controlado_same_payment(self) -> None:
        """Ambiguous + pago_no_controlado with same payment_id: both exist."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            ctx = {"payment_id": 400, "commission": "C4", "dni": "444"}

            amb_id = mgr.save_pending_review(run_id, None, "ambiguous_allocation:auto", ctx)
            pnc_id = mgr.save_pending_review(run_id, None, "pago_no_controlado", ctx)

            assert amb_id != pnc_id
            reviews = mgr.get_all_open_reviews(run_id)
            assert len(reviews) == 2

    def test_inserts_without_payment_id(self) -> None:
        """Ambiguous review with no payment_id: normal insert (no dedup)."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            ctx_no_pid = {"commission": "C5", "dni": "555"}

            id1 = mgr.save_pending_review(run_id, None, "ambiguous_allocation:auto", ctx_no_pid)
            id2 = mgr.save_pending_review(run_id, None, "ambiguous_allocation:manual", ctx_no_pid)

            assert id1 != id2
            reviews = mgr.get_all_open_reviews(run_id)
            assert len(reviews) == 2


# ===================================================================
# Stale review evaluation — ContextManager query & mutation methods
# ===================================================================

import json


class TestGetOpenReviewsForStudent:
    """get_open_reviews_for_student: open guard reviews by commission+DNI."""

    def test_returns_matching_guard_reviews(self) -> None:
        """Seeded guard reviews for matching commission+DNI are returned."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            ctx = {"commission": "Com A", "dni": "30111222", "reasons": ["missing_inscription_with_existing_cuotas"]}
            mgr.save_pending_review(run_id, None, "guard:invalid_sequence", ctx)
            # Different commission — should NOT match
            mgr.save_pending_review(run_id, None, "guard:invalid_sequence", {"commission": "Com B", "dni": "30111222", "reasons": ["x"]})

            results = mgr.get_open_reviews_for_student("Com A", "30111222")
            assert len(results) == 1
            assert results[0]["reason"] == "guard:invalid_sequence"
            parsed = json.loads(results[0]["context_json"])
            assert parsed["commission"] == "Com A"

    def test_returns_empty_when_no_match(self) -> None:
        """No matching reviews → empty list."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            mgr.start_run()
            results = mgr.get_open_reviews_for_student("Com X", "99999999")
            assert results == []


class TestGetOpenAnomalyReviewsForRows:
    """get_open_anomaly_reviews_for_rows: open anomaly reviews by row_number."""

    def test_returns_matching_anomaly_reviews(self) -> None:
        """Seeded anomaly reviews whose row_number is in the input list are returned."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            mgr.save_pending_review(run_id, None, "anomaly:cobro_no_aplica", {"row_number": 5})
            mgr.save_pending_review(run_id, None, "anomaly:negative_monto", {"row_number": 10})
            # row 99 — should NOT match
            mgr.save_pending_review(run_id, None, "anomaly:missing_medio", {"row_number": 99})

            results = mgr.get_open_anomaly_reviews_for_rows([5, 10])
            assert len(results) == 2
            row_numbers = {json.loads(r["context_json"])["row_number"] for r in results}
            assert row_numbers == {5, 10}

    def test_returns_empty_for_empty_input(self) -> None:
        """Empty row_numbers → empty list (short-circuit)."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            mgr.start_run()
            results = mgr.get_open_anomaly_reviews_for_rows([])
            assert results == []


class TestCloseReview:
    """close_review: mark review as resolved with auto-close reason."""

    def test_sets_status_resolved_and_reviewer_notes(self) -> None:
        """close_review must set status='resolved' and reviewer_notes=reason."""
        with ContextManager(":memory:", schema_path=_schema_path()) as mgr:
            run_id = mgr.start_run()
            review_id = mgr.save_pending_review(
                run_id, None, "guard:invalid_sequence",
                {"commission": "Com A", "dni": "30111222", "reasons": ["dup"]},
            )

            mgr.close_review(review_id, "auto_close:guard_resolved")

            conn = mgr._require_connection()
            row = conn.execute("SELECT * FROM pending_reviews WHERE id = ?", (review_id,)).fetchone()
            assert row is not None
            assert dict(row)["status"] == "resolved"
            assert dict(row)["reviewer_notes"] == "auto_close:guard_resolved"
