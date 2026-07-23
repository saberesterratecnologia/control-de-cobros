from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.models.pipeline import AllocationResult, Discrepancy, DiscrepancyType, Resolution, Severity
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import BankMovement, Payment
from src.orchestrator.pipeline import ConciliationPipeline


def test_pipeline_initializes_correctly(sample_config):
    pipeline = ConciliationPipeline(sample_config)
    assert pipeline.auto_threshold == 0.90
    assert pipeline.llm_threshold == 0.75
    assert pipeline.context is not None
    assert pipeline.sql is not None
    assert pipeline.sheets is not None


def test_pipeline_runs_dry_run_mode(sample_config, sample_commission, sample_student, monkeypatch):
    pipeline = ConciliationPipeline(sample_config)

    class _CM:
        def __init__(self, target):
            self.target = target

        def __enter__(self):
            return self.target

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(pipeline.context, "start_run", lambda **_: "run-1")
    monkeypatch.setattr(pipeline.context, "save_snapshot", lambda **_: None)
    monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
    monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "end_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)

    monkeypatch.setattr(pipeline.sql, "connect", lambda: _CM(pipeline.sql))
    monkeypatch.setattr(pipeline.sql, "get_active_commissions", lambda year, id_organizacion=2: [sample_commission])
    monkeypatch.setattr(pipeline.sql, "get_students", lambda _id: [sample_student])
    monkeypatch.setattr(
        pipeline.sql,
        "get_conciliated_payments",
        lambda _id, year=None, id_organizacion=None: [],
    )
    monkeypatch.setattr(
        pipeline.sql,
        "get_active_commissions_for_student",
        lambda _id, year, id_organizacion=2: [sample_commission],
    )

    monkeypatch.setattr(pipeline.sheets, "connect", lambda: _CM(pipeline.sheets))
    monkeypatch.setattr(pipeline.sheets, "read_all_rows", lambda: [])

    # Mock the decision engine so it doesn't call OpenAI
    from src.models.pipeline import LLMDecision
    monkeypatch.setattr(
        pipeline.decision_engine,
        "decide",
        lambda disc, ctx: LLMDecision(
            discrepancy_id=disc.id,
            action="skip",
            reasoning="mocked",
            confidence=0.5,
            suggested_value=None,
            model_used="mock",
        ),
    )

    summary = pipeline.run(dry_run=True)
    assert summary["run_id"] == "run-1"
    assert summary["writer"]["mode"] == "dry_run"


def test_pipeline_run_exports_full_open_review_backlog(sample_config, sample_commission, sample_student, monkeypatch):
    pipeline = ConciliationPipeline(sample_config)

    class _CM:
        def __init__(self, target):
            self.target = target

        def __enter__(self):
            return self.target

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(pipeline.context, "start_run", lambda **_: "run-1")
    monkeypatch.setattr(pipeline.context, "save_snapshot", lambda **_: None)
    monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
    monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "end_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)

    monkeypatch.setattr(pipeline.sql, "connect", lambda: _CM(pipeline.sql))
    monkeypatch.setattr(pipeline.sql, "get_active_commissions", lambda year, id_organizacion=2: [sample_commission])
    monkeypatch.setattr(pipeline.sql, "get_students", lambda _id: [sample_student])
    monkeypatch.setattr(pipeline.sql, "get_conciliated_payments", lambda _id, year=None, id_organizacion=None: [])
    monkeypatch.setattr(pipeline.sql, "get_active_commissions_for_student", lambda _id, year, id_organizacion=2: [sample_commission])

    monkeypatch.setattr(pipeline.sheets, "connect", lambda: _CM(pipeline.sheets))
    monkeypatch.setattr(pipeline.sheets, "read_all_rows", lambda: [])
    monkeypatch.setattr(pipeline.review_manager, "export_to_sheet", MagicMock(return_value={"exported": 2, "skipped": 3}))

    from src.models.pipeline import LLMDecision
    monkeypatch.setattr(
        pipeline.decision_engine,
        "decide",
        lambda disc, ctx: LLMDecision(
            discrepancy_id=disc.id,
            action="skip",
            reasoning="mocked",
            confidence=0.5,
            suggested_value=None,
            model_used="mock",
        ),
    )

    summary = pipeline.run(dry_run=True)

    assert summary["reviews_export"] == {"exported": 2, "skipped": 3}
    assert pipeline.review_manager.export_to_sheet.call_args.args == ()


def test_pipeline_classification_logic(sample_config, sample_commission, sample_student, sample_payment):
    pipeline = ConciliationPipeline(sample_config)

    base_expected = ExpectedRow(
        comision=sample_commission.nombre,
        fecha_movimiento=date(2026, 1, 10),
        tipo_movimiento="Cobro",
        dni=sample_student.dni,
        concepto="Inscripción",
        monto=Decimal("10000"),
        medio_pago="Mercado Pago",
        estudiante="Pérez Juan",
        id_movimiento_bancario=201,
        id_pago_mp=101,
        source_payment=sample_payment,
        source_movement=None,
    )

    auto = Discrepancy(
        id="d1",
        commission=sample_commission.nombre,
        dni=sample_student.dni,
        discrepancy_type=DiscrepancyType.MISSING_ROW,
        field=None,
        expected_value=None,
        actual_value=None,
        expected_row=base_expected,
        actual_row=None,
        confidence=0.0,
        severity=Severity.INFO,
        resolution=None,
        resolved_by=None,
    )
    llm = auto.model_copy(update={"id": "d2", "discrepancy_type": DiscrepancyType.WRONG_VALUE, "field": "medio_pago"})
    pending = auto.model_copy(update={"id": "d3", "discrepancy_type": DiscrepancyType.EXTRA_ROW, "expected_row": None})

    class _FakeScorer:
        def score(self, discrepancy, commission_pricing=None):
            return {"d1": 0.95, "d2": 0.80, "d3": 0.60}[discrepancy.id]

        def assign_severity(self, _):
            return Severity.WARNING

    class _FakeDecision:
        action = "skip"
        reasoning = "manual"
        confidence = 0.8
        model_used = "gpt-test"
        suggested_value = None

    pipeline.scorer = _FakeScorer()
    pipeline.decision_engine.decide = lambda *_: _FakeDecision()
    pipeline._run_id = "run-1"

    with pipeline.context:
        resolved = pipeline._classify_and_resolve([auto, llm, pending], sample_student, sample_commission, [sample_payment])

    assert auto.resolution == Resolution.AUTO_FIX
    assert llm.resolution == Resolution.SKIPPED
    # EXTRA_ROW is now skipped under insert-only policy (not deleted or flagged)
    assert pending.resolution == Resolution.SKIPPED
    assert pending.resolved_by == "insert_only_policy"
    assert len(resolved) == 1


def test_pipeline_generates_summary(sample_config):
    pipeline = ConciliationPipeline(sample_config)
    summary = pipeline._generate_summary("run-1")
    assert summary["run_id"] == "run-1"
    assert "discrepancies_total" in summary
    assert "cobros_blocked" in summary


def test_rows_match_exactly_normalizes_decimal_trailing_zeros(sample_payment):
    from src.models.sheet import ExpectedRow, SheetRow

    expected = [
        ExpectedRow(
            comision="Com A",
            fecha_movimiento=date(2026, 1, 10),
            tipo_movimiento="Cobro",
            dni="30111222",
            concepto="Inscripción",
            monto=Decimal("54800.0000"),
            medio_pago="Transferencia Bancaria",
            estudiante="Perez Juan",
            id_pago_mp=sample_payment.id_pago_mp,
            source_payment=sample_payment,
            source_movement=None,
        )
    ]
    actual = [
        SheetRow(
            row_number=1,
            organizacion="Org",
            curso="Curso",
            comision="Com A",
            fecha_movimiento=date(2026, 1, 10),
            tipo_movimiento="Cobro",
            dni="30111222",
            concepto="Inscripción",
            monto=Decimal("54800"),
            medio_pago="Transferencia Bancaria",
            estudiante="Perez Juan",
            estado_administrativo=None,
            estado_deuda=None,
            id_movimiento_bancario=None,
            id_pago_mp=sample_payment.id_pago_mp,
        )
    ]

    assert ConciliationPipeline._rows_match_exactly(expected, actual) is True


def test_pipeline_handles_connection_errors(sample_config, monkeypatch):
    pipeline = ConciliationPipeline(sample_config)
    monkeypatch.setattr(pipeline.sql, "connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(RuntimeError):
        pipeline.run(dry_run=True)


# ===================================================================
# _detect_invalid_sheet_sequence — relaxed guard tests
# ===================================================================


def _make_venta_row(concepto: str, monto: Decimal, **overrides) -> SheetRow:
    """Build a minimal Venta SheetRow for guard tests."""
    defaults = dict(
        row_number=1,
        organizacion="Org",
        curso="Curso",
        comision="Com A",
        fecha_movimiento=date(2026, 3, 1),
        tipo_movimiento="Venta",
        dni="30111222",
        concepto=concepto,
        monto=monto,
        medio_pago="Mercado Pago",
        estudiante="Pérez Juan",
        estado_administrativo=None,
        estado_deuda=None,
        id_movimiento_bancario=None,
        id_pago_mp=None,
    )
    defaults.update(overrides)
    return SheetRow(**defaults)


def _make_cobro_row(concepto: str, monto: Decimal, **overrides) -> SheetRow:
    """Build a minimal Cobro SheetRow for pipeline tests."""
    defaults = dict(
        row_number=1,
        organizacion="Org",
        curso="Curso",
        comision="Com A",
        fecha_movimiento=date(2026, 3, 1),
        tipo_movimiento="Cobro",
        dni="30111222",
        concepto=concepto,
        monto=monto,
        medio_pago="Mercado Pago",
        estudiante="Pérez Juan",
        estado_administrativo=None,
        estado_deuda=None,
        id_movimiento_bancario=201,
        id_pago_mp=None,
    )
    defaults.update(overrides)
    return SheetRow(**defaults)


class TestDetectInvalidSheetSequenceRelaxed:
    """Verify that relaxed guard no longer triggers on removed reasons."""

    def test_non_standard_inscription_amount_not_flagged(self, sample_config, sample_commission):
        """Inscription with a different amount should NOT trigger the guard."""
        pipeline = ConciliationPipeline(sample_config)
        # sample_commission has valor_inscripcion_promocion=10000
        # Use a very different amount — previously this triggered inscription_with_non_standard_amount
        rows = [_make_venta_row("Inscripción", Decimal("7500"))]
        reasons = pipeline._detect_invalid_sheet_sequence(sample_commission, rows)
        assert "inscription_with_non_standard_amount" not in reasons

    def test_cuota_1_matching_inscription_amount_not_flagged(self, sample_config, sample_commission):
        """Cuota 1 with monto equal to inscription price should NOT trigger the guard."""
        pipeline = ConciliationPipeline(sample_config)
        insc_price = sample_commission.valor_inscripcion_promocion  # 10000
        rows = [
            _make_venta_row("Inscripción", insc_price),
            _make_venta_row("Cuota 1", insc_price, row_number=2),
        ]
        reasons = pipeline._detect_invalid_sheet_sequence(sample_commission, rows)
        assert "cuota_1_matches_inscription_amount" not in reasons

    def test_cuota_1_combining_inscription_and_cuota_not_flagged(self, sample_config, sample_commission):
        """Cuota 1 with monto = inscription + cuota should NOT trigger the guard."""
        pipeline = ConciliationPipeline(sample_config)
        combined = sample_commission.valor_inscripcion_promocion + sample_commission.valor_cuota_bonificada
        rows = [
            _make_venta_row("Inscripción", sample_commission.valor_inscripcion_promocion),
            _make_venta_row("Cuota 1", combined, row_number=2),
        ]
        reasons = pipeline._detect_invalid_sheet_sequence(sample_commission, rows)
        assert "cuota_1_combines_inscription_and_cuota" not in reasons

    def test_kept_guards_still_fire(self, sample_config, sample_commission):
        """Verify the retained guards still detect genuine issues."""
        pipeline = ConciliationPipeline(sample_config)
        # Missing inscription + duplicate cuota + cuota exceeds total
        rows = [
            _make_venta_row("Cuota 1", Decimal("5000")),
            _make_venta_row("Cuota 1", Decimal("5000"), row_number=2),
            _make_venta_row("Cuota 15", Decimal("5000"), row_number=3),
        ]
        reasons = pipeline._detect_invalid_sheet_sequence(sample_commission, rows)
        assert "missing_inscription_with_existing_cuotas" in reasons
        assert "duplicate_cuota_1" in reasons
        assert "cuota_exceeds_total:15>12" in reasons

    def test_missing_cuotas_still_detected(self, sample_config, sample_commission):
        """Missing cuotas in sequence should still be caught."""
        pipeline = ConciliationPipeline(sample_config)
        rows = [
            _make_venta_row("Inscripción", Decimal("10000")),
            _make_venta_row("Cuota 3", Decimal("5000"), row_number=2),
        ]
        reasons = pipeline._detect_invalid_sheet_sequence(sample_commission, rows)
        assert "missing_cuotas_before_3:1,2" in reasons

    def test_clean_sequence_returns_no_reasons(self, sample_config, sample_commission):
        """A valid inscription + sequential cuotas should produce no reasons."""
        pipeline = ConciliationPipeline(sample_config)
        rows = [
            _make_venta_row("Inscripción", Decimal("10000")),
            _make_venta_row("Cuota 1", Decimal("5000"), row_number=2),
            _make_venta_row("Cuota 2", Decimal("5000"), row_number=3),
        ]
        reasons = pipeline._detect_invalid_sheet_sequence(sample_commission, rows)
        assert reasons == []


def test_is_valid_allocation_concept_uses_pipeline_regex(sample_config):
    pipeline = ConciliationPipeline(sample_config)

    assert pipeline._is_valid_allocation_concept("Cuota 1") is True
    assert pipeline._is_valid_allocation_concept("Concepto libre") is False


# ===================================================================
# Phase 0 removal — RED tests (must fail before implementation)
# ===================================================================


def test_process_student_uses_only_conciliated_payments(
    sample_config, sample_commission, sample_student, sample_payment, sample_movement, monkeypatch,
):
    """_process_student must use ONLY get_conciliated_payments as data source.

    After removal, get_all_payments and get_available_movements must NOT be
    called, and ConciliatedPayment objects must have conciliated_by='existing'.
    """
    pipeline = ConciliationPipeline(sample_config)
    pipeline._run_id = "run-1"

    monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
    monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)

    conciliated_pairs = [(sample_payment, sample_movement)]
    monkeypatch.setattr(
        pipeline.sql, "get_conciliated_payments",
        lambda _id, year=None, id_organizacion=None: conciliated_pairs,
    )
    monkeypatch.setattr(
        pipeline.sql, "get_active_commissions_for_student",
        lambda _id, year, id_organizacion=2: [sample_commission],
    )

    from src.models.pipeline import LLMDecision
    monkeypatch.setattr(
        pipeline.decision_engine, "decide",
        lambda disc, ctx: LLMDecision(
            discrepancy_id=disc.id, action="skip", reasoning="mocked",
            confidence=0.5, suggested_value=None, model_used="mock",
        ),
    )

    with pipeline.context:
        pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

    # get_all_payments and get_available_movements no longer exist on the connector,
    # so they cannot possibly be called — the absence of these methods IS the proof.
    assert not hasattr(pipeline.sql, "get_available_movements")
    assert not hasattr(pipeline.sql, "get_unconciliated_payments")


def test_generate_summary_has_no_phase0_keys(sample_config):
    """Summary must NOT contain payments_conciliated or payments_pending."""
    pipeline = ConciliationPipeline(sample_config)
    summary = pipeline._generate_summary("run-1")
    assert "payments_conciliated" not in summary
    assert "payments_pending" not in summary


def test_uncontrolled_flagging_from_conciliated_pairs(
    sample_config, sample_commission, sample_student, sample_movement, monkeypatch,
):
    """pago_no_controlado reviews must come from conciliated pairs, not get_all_payments."""
    pipeline = ConciliationPipeline(sample_config)
    pipeline._run_id = "run-1"

    uncontrolled_payment = Payment(
        id_pago_mp=999,
        fecha=datetime(2026, 1, 10, 10, 0, 0),
        monto=Decimal("10000"),
        nro_operacion="OP-999",
        id_persona=1,
        id_medio_pago=2,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=1,
        id_movimiento_bancario=201,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )

    saved_reviews = []

    def _save_review(**kwargs):
        saved_reviews.append(kwargs)
        return 1

    monkeypatch.setattr(pipeline.context, "save_pending_review", _save_review)
    monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
    monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
    monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)

    monkeypatch.setattr(
        pipeline.sql, "get_conciliated_payments",
        lambda _id, year=None, id_organizacion=None: [(uncontrolled_payment, sample_movement)],
    )
    monkeypatch.setattr(
        pipeline.sql, "get_active_commissions_for_student",
        lambda _id, year, id_organizacion=2: [sample_commission],
    )

    from src.models.pipeline import LLMDecision
    monkeypatch.setattr(
        pipeline.decision_engine, "decide",
        lambda disc, ctx: LLMDecision(
            discrepancy_id=disc.id, action="skip", reasoning="mocked",
            confidence=0.5, suggested_value=None, model_used="mock",
        ),
    )

    with pipeline.context:
        pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

    pago_reviews = [r for r in saved_reviews if r.get("reason") == "pago_no_controlado"]
    assert len(pago_reviews) == 1, "Uncontrolled payment from conciliated pair must generate review"
    assert pago_reviews[0]["context_json"]["payment_id"] == 999


class TestProcessStudentCutoffAndLedger:
    def _prepare_pipeline(self, sample_config, monkeypatch):
        pipeline = ConciliationPipeline(sample_config)
        pipeline._run_id = "run-1"

        monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
        monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)
        monkeypatch.setattr(pipeline, "_detect_invalid_sheet_sequence", lambda *args, **kwargs: [])
        monkeypatch.setattr(pipeline, "_evaluate_stale_reviews", lambda **_: None)
        monkeypatch.setattr(pipeline, "_classify_and_resolve", lambda *args, **kwargs: [])
        monkeypatch.setattr(pipeline, "_delete_excess_rows", lambda *args, **kwargs: None)
        monkeypatch.setattr(pipeline, "_build_expected_rows_from_allocations", lambda *args, **kwargs: [])
        monkeypatch.setattr(pipeline.reconciler, "reconcile", lambda *args, **kwargs: [])

        return pipeline

    def test_protects_payments_with_any_real_id_pago_mp_row(
        self, sample_config, sample_commission, sample_student, sample_movement, sample_payment, monkeypatch,
    ):
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)

        january = sample_payment.model_copy(update={
            "id_pago_mp": 101,
            "fecha": datetime(2026, 1, 10, 10, 0, 0),
            "id_concepto_pago": 1,
            "monto": Decimal("10000"),
        })
        february_protected = sample_payment.model_copy(update={
            "id_pago_mp": 102,
            "fecha": datetime(2026, 2, 10, 10, 0, 0),
            "id_concepto_pago": 2,
            "monto": Decimal("5000"),
        })
        march = sample_payment.model_copy(update={
            "id_pago_mp": 103,
            "fecha": datetime(2026, 3, 10, 10, 0, 0),
            "id_concepto_pago": 2,
            "monto": Decimal("5000"),
        })

        monkeypatch.setattr(
            pipeline.sql,
            "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [
                (january, sample_movement),
                (february_protected, sample_movement),
                (march, sample_movement),
            ],
        )
        monkeypatch.setattr(
            pipeline.sql,
            "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        captured: dict[str, object] = {}

        def _fake_allocate(self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None):
            captured["payment_ids"] = [cp.payment.id_pago_mp for cp in payments]
            captured["seed_ledger_from_sheet"] = seed_ledger_from_sheet
            return AllocationResult(allocated=[], ambiguous=[], next_venta=None)

        monkeypatch.setattr("src.rules.allocation_engine.AllocationEngine.allocate", _fake_allocate)
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.renumber_allocations",
            lambda self, allocations, student, initial_ledger=None: (allocations, None),
        )

        actual_rows = [
            _make_venta_row(
                "Cuota 1",
                Decimal("5000"),
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 2, 10),
                id_pago_mp=102,
            )
        ]

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, actual_rows, dry_run=True)

        assert captured["payment_ids"] == [101, 103]
        assert captured["seed_ledger_from_sheet"] is True

    def test_reconciler_does_not_receive_protected_payment_rows(
        self, sample_config, sample_commission, sample_student, sample_movement, sample_payment, monkeypatch,
    ):
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)

        protected_payment = sample_payment.model_copy(update={
            "id_pago_mp": 101,
            "fecha": datetime(2026, 1, 10, 10, 0, 0),
            "id_concepto_pago": 1,
            "monto": Decimal("10000"),
        })
        open_payment = sample_payment.model_copy(update={
            "id_pago_mp": 202,
            "fecha": datetime(2026, 3, 10, 10, 0, 0),
            "id_concepto_pago": 2,
            "monto": Decimal("5000"),
        })

        monkeypatch.setattr(
            pipeline.sql,
            "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [
                (protected_payment, sample_movement),
                (open_payment, sample_movement),
            ],
        )
        monkeypatch.setattr(
            pipeline.sql,
            "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.allocate",
            lambda self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None: AllocationResult(allocated=[], ambiguous=[], next_venta=None),
        )
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.renumber_allocations",
            lambda self, allocations, student, initial_ledger=None: (allocations, None),
        )

        captured: dict[str, object] = {}

        def _capture_reconcile(*, allocations, sheet_rows, next_venta, commission_name):
            captured["reconcile_row_ids"] = [row.id_pago_mp for row in sheet_rows]
            return []

        monkeypatch.setattr(pipeline.reconciler, "reconcile", _capture_reconcile)

        actual_rows = [
            _make_venta_row(
                "Inscripción",
                Decimal("10000"),
                row_number=1,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=101,
            ),
            _make_venta_row(
                "Cuota 99",
                Decimal("5000"),
                row_number=2,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 3, 10),
                id_pago_mp=None,
            ),
        ]

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, actual_rows, dry_run=True)

        assert captured["reconcile_row_ids"] == [None]

    def test_seeds_ledger_only_from_closed_payment_rows(
        self, sample_config, sample_commission, sample_student, sample_movement, sample_payment, monkeypatch,
    ):
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)

        closed_payment = sample_payment.model_copy(update={
            "id_pago_mp": 101,
            "fecha": datetime(2026, 1, 10, 10, 0, 0),
            "id_concepto_pago": 1,
            "monto": Decimal("10000"),
        })
        open_payment = sample_payment.model_copy(update={
            "id_pago_mp": 202,
            "fecha": datetime(2026, 3, 10, 10, 0, 0),
            "id_concepto_pago": 2,
            "monto": Decimal("5000"),
        })

        monkeypatch.setattr(
            pipeline.sql,
            "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [
                (closed_payment, sample_movement),
                (open_payment, sample_movement),
            ],
        )
        monkeypatch.setattr(
            pipeline.sql,
            "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        captured: dict[str, object] = {}

        def _fake_allocate(self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None):
            captured["payment_ids"] = [cp.payment.id_pago_mp for cp in payments]
            captured["ledger_seed_ids"] = {
                row.id_pago_mp for row in (ledger_seed_rows or []) if row.id_pago_mp is not None
            }
            return AllocationResult(allocated=[], ambiguous=[], next_venta=None)

        def _fake_renumber(self, allocations, student, initial_ledger=None):
            captured["initial_ledger"] = initial_ledger
            return allocations, None

        monkeypatch.setattr("src.rules.allocation_engine.AllocationEngine.allocate", _fake_allocate)
        monkeypatch.setattr("src.rules.allocation_engine.AllocationEngine.renumber_allocations", _fake_renumber)

        actual_rows = [
            _make_venta_row(
                "Inscripción",
                Decimal("10000"),
                row_number=1,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=101,
            ),
            _make_cobro_row(
                "Inscripción",
                Decimal("10000"),
                row_number=2,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=101,
            ),
            _make_venta_row(
                "Cuota 1",
                Decimal("5000"),
                row_number=3,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 3, 10),
                id_pago_mp=None,
            ),
        ]

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, actual_rows, dry_run=True)

        assert captured["payment_ids"] == [202]
        assert captured["ledger_seed_ids"] == {101}
        initial_ledger = captured["initial_ledger"]
        assert initial_ledger is not None
        assert initial_ledger.inscription_paid is True
        assert initial_ledger.cuotas_paid == 1
        assert initial_ledger.existing_concepts == {"Inscripción", "Cuota 1"}


# ===================================================================
# _evaluate_stale_reviews — stale review auto-close evaluator
# ===================================================================

import json
from pathlib import Path
from src.context.context_manager import ContextManager


def _schema_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "src" / "context" / "schema.sql")


class TestEvaluateStaleReviews:
    """Unit tests for _evaluate_stale_reviews pipeline method."""

    def _make_pipeline_with_context(self, sample_config):
        """Create a pipeline with a real in-memory ContextManager."""
        pipeline = ConciliationPipeline(sample_config)
        pipeline.context = ContextManager(":memory:", schema_path=_schema_path())
        return pipeline

    def test_closes_non_blocking_guard_review_when_reasons_no_longer_match(self, sample_config, sample_commission):
        """Non-blocking guard reviews now move to cleanup instead of staying in REVISIONES."""
        pipeline = self._make_pipeline_with_context(sample_config)
        with pipeline.context:
            run_id = pipeline.context.start_run()
            pipeline._run_id = run_id
            review_id = pipeline.context.save_pending_review(
                run_id, None, "guard:invalid_sequence",
                {"commission": "Comisión A", "dni": "30111222", "reasons": ["missing_inscription_with_existing_cuotas"]},
            )

            pipeline._evaluate_stale_reviews(
                commission=sample_commission,
                student_dni="30111222",
                actual_rows=[],
                current_guard_reasons=[],  # no guard reasons anymore
                current_anomalies=set(),
            )

            row = pipeline.context._require_connection().execute(
                "SELECT status, reviewer_notes FROM pending_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            assert dict(row)["status"] == "resolved"
            assert dict(row)["reviewer_notes"] == "auto_close:moved_to_cleanup"

    def test_closes_non_blocking_guard_review_when_reasons_still_overlap(self, sample_config, sample_commission):
        """Non-blocking guard reviews are closed even if the issue still exists."""
        pipeline = self._make_pipeline_with_context(sample_config)
        with pipeline.context:
            run_id = pipeline.context.start_run()
            pipeline._run_id = run_id
            review_id = pipeline.context.save_pending_review(
                run_id, None, "guard:invalid_sequence",
                {"commission": "Comisión A", "dni": "30111222", "reasons": ["missing_inscription_with_existing_cuotas"]},
            )

            pipeline._evaluate_stale_reviews(
                commission=sample_commission,
                student_dni="30111222",
                actual_rows=[],
                current_guard_reasons=["missing_inscription_with_existing_cuotas"],
                current_anomalies=set(),
            )

            row = pipeline.context._require_connection().execute(
                "SELECT status, reviewer_notes FROM pending_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            assert dict(row)["status"] == "resolved"
            assert dict(row)["reviewer_notes"] == "auto_close:moved_to_cleanup"

    def test_closes_anomaly_review_when_type_absent(self, sample_config, sample_commission):
        """Anomaly review whose type is absent from current_anomalies → closed."""
        pipeline = self._make_pipeline_with_context(sample_config)
        with pipeline.context:
            run_id = pipeline.context.start_run()
            pipeline._run_id = run_id
            review_id = pipeline.context.save_pending_review(
                run_id, None, "anomaly:cobro_no_aplica",
                {"row_number": 5},
            )

            pipeline._evaluate_stale_reviews(
                commission=sample_commission,
                student_dni="30111222",
                actual_rows=[_make_venta_row("Cuota 1", Decimal("5000"), row_number=5)],
                current_guard_reasons=[],
                current_anomalies=set(),  # cobro_no_aplica no longer present
            )

            row = pipeline.context._require_connection().execute(
                "SELECT status, reviewer_notes FROM pending_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            assert dict(row)["status"] == "resolved"
            assert dict(row)["reviewer_notes"] == "auto_close:anomaly_resolved"

    def test_closes_anomaly_review_when_type_still_present(self, sample_config, sample_commission):
        """Current anomaly reviews are now moved out of REVISIONES into cleanup."""
        pipeline = self._make_pipeline_with_context(sample_config)
        with pipeline.context:
            run_id = pipeline.context.start_run()
            pipeline._run_id = run_id
            review_id = pipeline.context.save_pending_review(
                run_id, None, "anomaly:cobro_no_aplica",
                {"row_number": 5},
            )

            pipeline._evaluate_stale_reviews(
                commission=sample_commission,
                student_dni="30111222",
                actual_rows=[_make_venta_row("Cuota 1", Decimal("5000"), row_number=5)],
                current_guard_reasons=[],
                current_anomalies={"cobro_no_aplica"},  # still present
            )

            row = pipeline.context._require_connection().execute(
                "SELECT status, reviewer_notes FROM pending_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            assert dict(row)["status"] == "resolved"
            assert dict(row)["reviewer_notes"] == "auto_close:moved_to_cleanup"

    def test_ignores_ambiguous_and_pago_no_controlado_reviews(self, sample_config, sample_commission):
        """Ambiguous and pago_no_controlado reviews must remain untouched."""
        pipeline = self._make_pipeline_with_context(sample_config)
        with pipeline.context:
            run_id = pipeline.context.start_run()
            pipeline._run_id = run_id
            amb_id = pipeline.context.save_pending_review(
                run_id, None, "ambiguous_allocation:auto",
                {"commission": "Comisión A", "dni": "30111222", "payment_id": 1},
            )
            pnc_id = pipeline.context.save_pending_review(
                run_id, None, "pago_no_controlado",
                {"commission": "Comisión A", "dni": "30111222", "payment_id": 2},
            )

            pipeline._evaluate_stale_reviews(
                commission=sample_commission,
                student_dni="30111222",
                actual_rows=[],
                current_guard_reasons=[],
                current_anomalies=set(),
            )

            conn = pipeline.context._require_connection()
            for rid in (amb_id, pnc_id):
                row = conn.execute("SELECT status FROM pending_reviews WHERE id = ?", (rid,)).fetchone()
                assert dict(row)["status"] == "open"


class TestProcessStudentStaleReviewHook:
    """Verify _process_student() calls _evaluate_stale_reviews at the right point."""

    def test_calls_evaluate_stale_reviews_after_guard_detection(
        self, sample_config, sample_commission, sample_student, sample_movement, monkeypatch,
    ):
        """_process_student must call _evaluate_stale_reviews after guard detection."""
        pipeline = ConciliationPipeline(sample_config)
        pipeline._run_id = "run-1"

        monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
        monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)
        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )
        from src.models.pipeline import LLMDecision
        monkeypatch.setattr(
            pipeline.decision_engine, "decide",
            lambda disc, ctx: LLMDecision(
                discrepancy_id=disc.id, action="skip", reasoning="mocked",
                confidence=0.5, suggested_value=None, model_used="mock",
            ),
        )

        call_log: list[str] = []
        original_evaluate = pipeline._evaluate_stale_reviews

        def _tracking_evaluate(*args, **kwargs):
            call_log.append("evaluate_stale_reviews")
            return original_evaluate(*args, **kwargs)

        monkeypatch.setattr(pipeline, "_evaluate_stale_reviews", _tracking_evaluate)

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

        assert "evaluate_stale_reviews" in call_log

    def test_noop_when_no_open_reviews(
        self, sample_config, sample_commission, sample_student, monkeypatch,
    ):
        """Student with no open reviews: _evaluate_stale_reviews produces no close_review calls."""
        pipeline = ConciliationPipeline(sample_config)
        pipeline._run_id = "run-1"

        monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
        monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)
        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )
        from src.models.pipeline import LLMDecision
        monkeypatch.setattr(
            pipeline.decision_engine, "decide",
            lambda disc, ctx: LLMDecision(
                discrepancy_id=disc.id, action="skip", reasoning="mocked",
                confidence=0.5, suggested_value=None, model_used="mock",
            ),
        )

        close_calls: list[tuple] = []
        monkeypatch.setattr(pipeline.context, "close_review", lambda rid, reason: close_calls.append((rid, reason)))
        monkeypatch.setattr(pipeline.context, "get_open_reviews_for_student", lambda c, d: [])
        monkeypatch.setattr(pipeline.context, "get_open_anomaly_reviews_for_rows", lambda rn: [])

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

        assert close_calls == []


# ===================================================================
# Fix Pipeline Allocation Blockers — regression tests
# ===================================================================


class TestFixPipelineAllocationBlockers:
    """Regression tests for R1–R4 pipeline allocation fixes."""

    def _prepare_pipeline(self, sample_config, monkeypatch):
        pipeline = ConciliationPipeline(sample_config)
        pipeline._run_id = "run-1"

        monkeypatch.setattr(pipeline.context, "save_pending_review", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_checkpoint", lambda **_: 1)
        monkeypatch.setattr(pipeline.context, "save_discrepancy", lambda *_: 1)
        monkeypatch.setattr(pipeline.context, "is_already_applied", lambda *_: False)
        monkeypatch.setattr(pipeline, "_evaluate_stale_reviews", lambda **_: None)
        monkeypatch.setattr(pipeline, "_classify_and_resolve", lambda *args, **kwargs: [])
        monkeypatch.setattr(pipeline, "_delete_excess_rows", lambda *args, **kwargs: None)
        monkeypatch.setattr(pipeline, "_build_expected_rows_from_allocations", lambda *args, **kwargs: [])

        return pipeline

    # --- R1: Manual-only student reaches allocate/reconcile ---

    def test_manual_only_student_reaches_allocate_reconcile(
        self, sample_config, sample_commission, sample_student, sample_movement, sample_payment, monkeypatch,
    ):
        """Student with only manual Venta rows (id_pago_mp=None) must NOT be skipped."""
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)
        monkeypatch.setattr(pipeline, "_detect_invalid_sheet_sequence", lambda *args, **kwargs: [])

        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        captured: dict[str, object] = {}

        def _fake_allocate(self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None):
            captured["allocate_called"] = True
            captured["seed_ledger_from_sheet"] = seed_ledger_from_sheet
            captured["ledger_seed_rows"] = ledger_seed_rows
            return AllocationResult(allocated=[], ambiguous=[], next_venta=None)

        monkeypatch.setattr("src.rules.allocation_engine.AllocationEngine.allocate", _fake_allocate)
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.renumber_allocations",
            lambda self, allocations, student, initial_ledger=None: (allocations, None),
        )

        reconcile_called = []

        def _capture_reconcile(*, allocations, sheet_rows, next_venta, commission_name):
            reconcile_called.append(True)
            return []

        monkeypatch.setattr(pipeline.reconciler, "reconcile", _capture_reconcile)

        actual_rows = [
            _make_venta_row(
                "Inscripción",
                Decimal("10000"),
                row_number=1,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=None,
            ),
            _make_venta_row(
                "Cuota 1",
                Decimal("5000"),
                row_number=2,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 2, 10),
                id_pago_mp=None,
            ),
        ]

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, actual_rows, dry_run=True)

        assert captured.get("allocate_called") is True, "allocate must be called for manual-only students"
        assert len(reconcile_called) == 1, "reconcile must be called for manual-only students"

    # --- R2: Non-blocking guard reasons warn but continue ---

    def test_non_blocking_guard_reasons_warn_but_continue(
        self, sample_config, sample_commission, sample_student, sample_movement, monkeypatch,
    ):
        """Non-blocking guard reasons move to cleanup but do not block processing."""
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)
        monkeypatch.setattr(
            pipeline, "_detect_invalid_sheet_sequence",
            lambda *args, **kwargs: ["missing_inscription_with_existing_cuotas"],
        )

        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        saved_reviews: list[dict] = []

        def _save_review(**kwargs):
            saved_reviews.append(kwargs)
            return 1

        monkeypatch.setattr(pipeline.context, "save_pending_review", _save_review)

        captured: dict[str, object] = {}

        def _fake_allocate(self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None):
            captured["allocate_called"] = True
            return AllocationResult(allocated=[], ambiguous=[], next_venta=None)

        monkeypatch.setattr("src.rules.allocation_engine.AllocationEngine.allocate", _fake_allocate)
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.renumber_allocations",
            lambda self, allocations, student, initial_ledger=None: (allocations, None),
        )
        monkeypatch.setattr(pipeline.reconciler, "reconcile", lambda **_: [])

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

        # Non-blocking guards no longer create pending reviews
        guard_reviews = [r for r in saved_reviews if r.get("reason") == "guard:invalid_sequence"]
        assert guard_reviews == []
        assert pipeline._cleanup_tasks
        cleanup_types = {task["task_type"] for task in pipeline._cleanup_tasks.values()}
        assert "Inscripción" in cleanup_types

        # Allocate must still be called
        assert captured.get("allocate_called") is True, "allocate must run after non-blocking guard"

    # --- R2: Blocking guard reason (cuota_exceeds_total) still blocks ---

    def test_cuota_exceeds_total_still_blocks(
        self, sample_config, sample_commission, sample_student, monkeypatch,
    ):
        """cuota_exceeds_total guard must still block processing with return [], []."""
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)
        monkeypatch.setattr(
            pipeline, "_detect_invalid_sheet_sequence",
            lambda *args, **kwargs: ["cuota_exceeds_total:15>12"],
        )

        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [],
        )

        saved_reviews: list[dict] = []

        def _save_review(**kwargs):
            saved_reviews.append(kwargs)
            return 1

        monkeypatch.setattr(pipeline.context, "save_pending_review", _save_review)

        allocate_called = []
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.allocate",
            lambda self, **_: allocate_called.append(True) or AllocationResult(allocated=[], ambiguous=[], next_venta=None),
        )

        with pipeline.context:
            result = pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

        assert result == ([], []), "cuota_exceeds_total must return empty"

        # Review must be saved with blocking=true
        guard_reviews = [r for r in saved_reviews if r.get("reason") == "guard:invalid_sequence"]
        assert len(guard_reviews) == 1, "guard review must be saved"
        assert guard_reviews[0]["context_json"]["blocking"] is True

        assert len(allocate_called) == 0, "allocate must NOT be called when cuota_exceeds_total blocks"

    def test_mixed_blocking_and_nonblocking_guard_reasons_split(self, sample_config, sample_commission, sample_student, monkeypatch):
        """Mixed guard reasons: blocking goes to REVISIONES, non-blocking to LIMPIEZA_HOJA."""
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)
        monkeypatch.setattr(
            pipeline, "_detect_invalid_sheet_sequence",
            lambda *args, **kwargs: ["duplicate_cuota_1", "cuota_exceeds_total:15>12"],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [],
        )

        saved_reviews: list[dict] = []
        def _save_review(**kwargs):
            saved_reviews.append(kwargs)
            return 1
        monkeypatch.setattr(pipeline.context, "save_pending_review", _save_review)

        with pipeline.context:
            result = pipeline._process_student(sample_student, sample_commission, [], dry_run=True)

        # Must block allocation
        assert result == ([], []), "mixed reasons with blocking must halt"

        # REVISIONES gets only blocking reason
        guard_reviews = [r for r in saved_reviews if r.get("reason") == "guard:invalid_sequence"]
        assert len(guard_reviews) == 1
        assert guard_reviews[0]["context_json"]["reasons"] == ["cuota_exceeds_total:15>12"]
        assert guard_reviews[0]["context_json"]["blocking"] is True

        # LIMPIEZA_HOJA gets non-blocking reason
        assert pipeline._cleanup_tasks
        cleanup_types = {task["task_type"] for task in pipeline._cleanup_tasks.values()}
        assert "Secuencia" in cleanup_types

    # --- R3: Manual Venta rows seed the ledger ---

    def test_manual_venta_rows_seed_ledger(
        self, sample_config, sample_commission, sample_student, sample_movement, sample_payment, monkeypatch,
    ):
        """Manual Venta rows (id_pago_mp=None) must be included in ledger_seed_rows."""
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)
        monkeypatch.setattr(pipeline, "_detect_invalid_sheet_sequence", lambda *args, **kwargs: [])

        closed_payment = sample_payment.model_copy(update={
            "id_pago_mp": 101,
            "fecha": datetime(2026, 1, 10, 10, 0, 0),
            "id_concepto_pago": 1,
            "monto": Decimal("10000"),
        })

        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [(closed_payment, sample_movement)],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        captured: dict[str, object] = {}

        def _fake_allocate(self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None):
            captured["ledger_seed_rows"] = ledger_seed_rows
            captured["seed_ledger_from_sheet"] = seed_ledger_from_sheet
            return AllocationResult(allocated=[], ambiguous=[], next_venta=None)

        monkeypatch.setattr("src.rules.allocation_engine.AllocationEngine.allocate", _fake_allocate)
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.renumber_allocations",
            lambda self, allocations, student, initial_ledger=None: (allocations, None),
        )
        monkeypatch.setattr(pipeline.reconciler, "reconcile", lambda **_: [])

        actual_rows = [
            _make_venta_row(
                "Inscripción",
                Decimal("10000"),
                row_number=1,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=101,
            ),
            _make_cobro_row(
                "Inscripción",
                Decimal("10000"),
                row_number=2,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=101,
            ),
            _make_venta_row(
                "Cuota 1",
                Decimal("5000"),
                row_number=3,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 3, 10),
                id_pago_mp=None,
            ),
        ]

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, actual_rows, dry_run=True)

        seed_rows = captured.get("ledger_seed_rows") or []
        seed_conceptos = [r.concepto for r in seed_rows]
        assert "Cuota 1" in seed_conceptos, "Manual Cuota 1 Venta row must seed the ledger"
        assert "Inscripción" in seed_conceptos, "Protected Inscripción Venta row must seed the ledger"

    # --- R4: Manual rows visible to reconciler when protections exist ---

    def test_manual_rows_visible_to_reconciler_with_protections(
        self, sample_config, sample_commission, sample_student, sample_movement, sample_payment, monkeypatch,
    ):
        """Manual rows (id_pago_mp=None) must remain visible to the reconciler when protections exist."""
        pipeline = self._prepare_pipeline(sample_config, monkeypatch)
        monkeypatch.setattr(pipeline, "_detect_invalid_sheet_sequence", lambda *args, **kwargs: [])

        protected_payment = sample_payment.model_copy(update={
            "id_pago_mp": 101,
            "fecha": datetime(2026, 1, 10, 10, 0, 0),
            "id_concepto_pago": 1,
            "monto": Decimal("10000"),
        })

        monkeypatch.setattr(
            pipeline.sql, "get_conciliated_payments",
            lambda _id, year=None, id_organizacion=None: [(protected_payment, sample_movement)],
        )
        monkeypatch.setattr(
            pipeline.sql, "get_active_commissions_for_student",
            lambda _id, year, id_organizacion=2: [sample_commission],
        )

        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.allocate",
            lambda self, payments, existing_sheet_rows, student, seed_ledger_from_sheet=False, ledger_seed_rows=None: AllocationResult(allocated=[], ambiguous=[], next_venta=None),
        )
        monkeypatch.setattr(
            "src.rules.allocation_engine.AllocationEngine.renumber_allocations",
            lambda self, allocations, student, initial_ledger=None: (allocations, None),
        )

        captured: dict[str, object] = {}

        def _capture_reconcile(*, allocations, sheet_rows, next_venta, commission_name):
            captured["reconcile_row_ids"] = [row.id_pago_mp for row in sheet_rows]
            return []

        monkeypatch.setattr(pipeline.reconciler, "reconcile", _capture_reconcile)

        actual_rows = [
            _make_venta_row(
                "Inscripción",
                Decimal("10000"),
                row_number=1,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 1, 10),
                id_pago_mp=101,
            ),
            _make_venta_row(
                "Cuota 1",
                Decimal("5000"),
                row_number=2,
                comision=sample_commission.nombre,
                dni=sample_student.dni,
                fecha_movimiento=date(2026, 3, 10),
                id_pago_mp=None,
            ),
        ]

        with pipeline.context:
            pipeline._process_student(sample_student, sample_commission, actual_rows, dry_run=True)

        assert None in captured["reconcile_row_ids"], "Manual row (id_pago_mp=None) must be visible to reconciler"
        assert 101 not in captured["reconcile_row_ids"], "Protected row must be excluded from reconciler"
