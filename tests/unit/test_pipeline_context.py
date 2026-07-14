"""Unit tests for commission_prices in ambiguous review context_json.

These tests verify that _resolve_ambiguous() includes commission_prices
(with pago_unico when available) in the context_json passed to
save_pending_review.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.models.pipeline import (
    AllocationCandidate,
    AmbiguousPayment,
    ConciliatedPayment,
)
from src.models.source import BankMovement, Commission, Payment, Student
from src.orchestrator.pipeline import ConciliationPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _commission(*, pago_unico: str | None = None) -> Commission:
    return Commission(
        id_comision=10,
        id_curso=60,
        id_organizacion=2,
        nombre="Comisión Test",
        valor_inscripcion=Decimal("52050"),
        valor_inscripcion_promocion=Decimal("52050"),
        valor_cuota=Decimal("10000"),
        valor_cuota_bonificada=Decimal("10000"),
        valor_pago_unico=Decimal(pago_unico) if pago_unico else None,
        cantidad_cuotas=9,
        duracion_meses=9,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


def _student() -> Student:
    return Student(
        id_persona=1,
        nombres="Test",
        apellidos="Student",
        apellidos_nombres="Student, Test",
        dni="12345678",
        email=None,
        id_estado_academico=None,
        id_estado_administrativo=1,
        eliminado=False,
    )


def _payment(monto: str = "500000") -> Payment:
    return Payment(
        id_pago_mp=100,
        monto=Decimal(monto),
        fecha=datetime(2026, 3, 15),
        nro_operacion="OP-TEST",
        id_persona=1,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=True,
        comentario_cliente=None,
        id_concepto_pago=None,
        id_movimiento_bancario=200,
        razon_social_originante="Test Student",
        dni_cuit_originante="12345678",
        controlado_auto=True,
        estado_conciliacion_auto=None,
    )


def _conciliated(monto: str = "500000") -> ConciliatedPayment:
    return ConciliatedPayment(
        payment=_payment(monto),
        movement=BankMovement(
            id_movimiento=200,
            id_cuenta_bancaria=1,
            id_persona=1,
            fecha=date(2026, 3, 15),
            referencia="OP-TEST",
            causal=None,
            concepto=None,
            importe=Decimal(monto),
            conciliado=False,
        ),
        conciliated_by="existing",
    )


def _ambiguous(monto: str = "500000") -> AmbiguousPayment:
    # Use 2 candidates to bypass single-candidate deterministic shortcut
    return AmbiguousPayment(
        payment=_conciliated(monto),
        candidates=[
            AllocationCandidate(concept="Cuota 1", amount=Decimal(monto), score=0.3, reasoning="guess"),
            AllocationCandidate(concept="Inscripción", amount=Decimal(monto), score=0.3, reasoning="guess"),
        ],
    )


@pytest.fixture()
def pipeline():
    """Build a ConciliationPipeline with mocked dependencies for unit tests."""
    with patch("src.orchestrator.pipeline.ContextManager"), \
         patch("src.orchestrator.pipeline.SQLServerConnector"), \
         patch("src.orchestrator.pipeline.SheetsConnector"), \
         patch("src.orchestrator.pipeline.ReviewManager"), \
         patch("src.orchestrator.pipeline.DecisionEngine"), \
         patch("src.orchestrator.pipeline.SheetWriter"), \
         patch("src.orchestrator.pipeline.PatchBuilder"):

        config = {
            "sqlite": {"db_path": ":memory:"},
            "database": {},
            "sheets": {"spreadsheet_id": "fake"},
            "agent": {"year": "2026", "id_organizacion": "2"},
            "llm": {
                "confidence_threshold_auto": "0.90",
                "confidence_threshold_flagged": "0.75",
            },
        }
        p = ConciliationPipeline(config)

        # Make decision engine return "flag_review" to force save_pending_review
        mock_decision = MagicMock()
        mock_decision.action = "flag_review"
        mock_decision.confidence = 0.5
        mock_decision.model_dump.return_value = {"action": "flag_review"}
        p.decision_engine.decide.return_value = mock_decision

        p._run_id = "test-run"
        yield p


# ===================================================================
# Tests
# ===================================================================


class TestCommissionPricesInContext:
    """Verify _resolve_ambiguous includes commission_prices in context_json."""

    def test_commission_prices_includes_pago_unico(self, pipeline) -> None:
        """When commission has valor_pago_unico, context_json must include it."""
        commission = _commission(pago_unico="500000")
        ambiguous = _ambiguous()

        pipeline._resolve_ambiguous(
            ambiguous,
            _student(),
            commission,
            [_payment()],
            [commission],
        )

        call_args = pipeline.context.save_pending_review.call_args
        assert call_args is not None, "save_pending_review was not called"
        context_json = call_args.kwargs.get("context_json")

        assert context_json is not None, "context_json not found in save_pending_review call"
        assert "commission_prices" in context_json, "commission_prices missing from context_json"
        assert context_json["commission_prices"]["pago_unico"] == "500000"
        assert context_json["commission_prices"]["inscripcion"] == "52050"
        assert context_json["commission_prices"]["cuota"] == "10000"

    def test_commission_prices_omits_pago_unico_when_none(self, pipeline) -> None:
        """When commission has no valor_pago_unico, pago_unico key must be None."""
        commission = _commission(pago_unico=None)
        ambiguous = _ambiguous(monto="15000")

        pipeline._resolve_ambiguous(
            ambiguous,
            _student(),
            commission,
            [_payment("15000")],
            [commission],
        )

        call_args = pipeline.context.save_pending_review.call_args
        assert call_args is not None, "save_pending_review was not called"
        context_json = call_args.kwargs.get("context_json")

        assert context_json is not None, "context_json not found in save_pending_review call"
        assert "commission_prices" in context_json, "commission_prices missing from context_json"
        pago_unico = context_json["commission_prices"].get("pago_unico")
        assert pago_unico is None, f"pago_unico should be None when valor_pago_unico is None, got {pago_unico}"
