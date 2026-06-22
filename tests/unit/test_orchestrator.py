from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from src.models.pipeline import Discrepancy, DiscrepancyType, Resolution, Severity
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
        "get_all_payments",
        lambda _id, year=None, id_organizacion=None: [],
    )
    monkeypatch.setattr(
        pipeline.sql,
        "get_conciliated_payments",
        lambda _id, year=None, id_organizacion=None: [],
    )
    monkeypatch.setattr(
        pipeline.sql,
        "get_available_movements",
        lambda _id, year=None: [],
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
    assert pending.resolution == Resolution.PENDING_REVIEW
    assert len(resolved) == 1


def test_pipeline_generates_summary(sample_config):
    pipeline = ConciliationPipeline(sample_config)
    summary = pipeline._generate_summary("run-1")
    assert summary["run_id"] == "run-1"
    assert "discrepancies_total" in summary
    assert "payments_conciliated" in summary
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


def test_inline_conciliation_updates_payment_in_dry_run(sample_config):
    pipeline = ConciliationPipeline(sample_config)
    pipeline._run_id = "run-1"

    payment = Payment(
        id_pago_mp=101,
        fecha=datetime(2026, 5, 10, 12, 0, 0),
        monto=Decimal("15000.00"),
        nro_operacion="REF-101",
        id_persona=10,
        id_medio_pago=2,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=2,
        id_movimiento_bancario=-1,
        id_organizacion=2,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=False,
        estado_conciliacion_auto="pendiente",
    )
    movement = BankMovement(
        id_movimiento=202,
        id_cuenta_bancaria=1,
        id_persona=10,
        fecha=date(2026, 5, 10),
        referencia="REF-101",
        causal=None,
        concepto=None,
        importe=Decimal("15000.00"),
        conciliado=False,
    )

    updated = pipeline._try_persist_conciliation(
        [payment], [movement], dry_run=True,
    )

    assert pipeline._counters.payments_conciliated == 1
    assert updated[0].id_movimiento_bancario == movement.id_movimiento


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
