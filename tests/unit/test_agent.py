from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.agent.decision_engine import DecisionEngine
from src.models.pipeline import Discrepancy, DiscrepancyType, Severity
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import Payment


def _config() -> dict:
    return {
        "llm": {
            "primary_model": "gpt-4o-mini",
            "fallback_model": "gpt-4o",
            "temperature": 0.1,
            "max_retries": 1,
            "confidence_threshold_flagged": 0.75,
        }
    }


def _discrepancy() -> Discrepancy:
    payment = Payment(
        id_pago_mp=10,
        fecha=datetime(2026, 5, 10, 12, 0, 0),
        monto=Decimal("54500.00"),
        nro_operacion="OP-10",
        id_persona=1,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=1,
        id_movimiento_bancario=100,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )
    expected = ExpectedRow(
        comision="Com A",
        fecha_movimiento=date(2026, 5, 10),
        tipo_movimiento="Cobro",
        dni="30111222",
        concepto="Inscripción",
        monto=Decimal("54500.00"),
        medio_pago="Transferencia",
        estudiante="Perez Juan",
        id_movimiento_bancario=100,
        id_pago_mp=10,
        source_payment=payment,
        source_movement=None,
    )
    actual = SheetRow(
        row_number=2,
        organizacion="Org",
        curso="Curso",
        comision="Com A",
        fecha_movimiento=date(2026, 5, 10),
        tipo_movimiento="Cobro",
        dni="30111222",
        concepto="Cuota 1",
        monto=Decimal("54500.00"),
        medio_pago="Transferencia",
        estudiante="Perez Juan",
        estado_administrativo="Regular",
        estado_deuda="Sin deuda",
        id_movimiento_bancario=100,
        id_pago_mp=10,
    )

    return Discrepancy(
        id="disc-1",
        commission="Com A",
        dni="30111222",
        discrepancy_type=DiscrepancyType.WRONG_VALUE,
        field="concepto",
        expected_value="Inscripción",
        actual_value="Cuota 1",
        expected_row=expected,
        actual_row=actual,
        confidence=0.6,
        severity=Severity.WARNING,
        resolution=None,
        resolved_by=None,
    )


def _context_data() -> dict:
    return {
        "payment_history": ["Inscripción", "Cuota 1"],
        "commission_prices": {"inscripcion": 54500, "cuota": 27000},
        "student_info": {"dni": "30111222", "name": "Perez Juan"},
    }


def test_decision_with_cached_result_returns_cached_no_api_call() -> None:
    context = MagicMock()
    context.get_decision_by_hash.return_value = {
        "decision_json": json.dumps(
            {
                "discrepancy_id": "disc-1",
                "action": "fix",
                "reasoning": "cached",
                "confidence": 0.9,
                "suggested_value": "Cuota 2",
            }
        ),
        "model_used": "gpt-4o-mini",
    }
    engine = DecisionEngine(_config(), context)

    engine._call_llm = MagicMock()  # type: ignore[method-assign]
    decision = engine.decide(_discrepancy(), _context_data())

    assert decision.action == "fix"
    assert decision.reasoning == "cached"
    engine._call_llm.assert_not_called()  # type: ignore[attr-defined]


def test_decision_with_fresh_input_calls_api_and_caches_result() -> None:
    context = MagicMock()
    context.get_decision_by_hash.return_value = None
    context.get_current_run.return_value = {"id": "run-1"}
    engine = DecisionEngine(_config(), context)

    engine._call_llm = MagicMock(  # type: ignore[method-assign]
        return_value={
            "model": "gpt-4o-mini",
            "content": json.dumps(
                {
                    "action": "fix",
                    "suggested_value": "Cuota 2",
                    "confidence": 0.92,
                    "reasoning": "ok",
                }
            ),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )

    decision = engine.decide(_discrepancy(), _context_data())

    assert decision.action == "fix"
    assert decision.model_used == "gpt-4o-mini"
    assert context.save_decision.call_count == 1


def test_fallback_from_mini_to_4o_when_low_confidence() -> None:
    context = MagicMock()
    context.get_decision_by_hash.return_value = None
    context.get_current_run.return_value = {"id": "run-1"}
    engine = DecisionEngine(_config(), context)

    engine._call_llm = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "model": "gpt-4o-mini",
                "content": json.dumps(
                    {
                        "action": "flag_review",
                        "confidence": 0.5,
                        "reasoning": "low confidence",
                    }
                ),
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            {
                "model": "gpt-4o",
                "content": json.dumps(
                    {
                        "action": "fix",
                        "suggested_value": "Cuota 2",
                        "confidence": 0.9,
                        "reasoning": "fallback better",
                    }
                ),
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            },
        ]
    )

    decision = engine.decide(_discrepancy(), _context_data())

    assert decision.model_used == "gpt-4o"
    assert engine._call_llm.call_count == 2  # type: ignore[attr-defined]


def test_parse_valid_json_response_returns_llmdecision() -> None:
    context = MagicMock()
    engine = DecisionEngine(_config(), context)

    decision = engine._parse_response(
        {
            "model": "gpt-4o-mini",
            "content": json.dumps(
                {
                    "action": "skip",
                    "confidence": 0.81,
                    "reasoning": "legit",
                    "suggested_value": None,
                }
            ),
        }
    )

    assert decision.action == "skip"
    assert decision.confidence == pytest.approx(0.81)


def test_parse_invalid_json_retries_with_fallback_model() -> None:
    context = MagicMock()
    context.get_decision_by_hash.return_value = None
    context.get_current_run.return_value = {"id": "run-1"}
    engine = DecisionEngine(_config(), context)

    engine._call_llm = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            {"model": "gpt-4o-mini", "content": "{invalid", "usage": {}},
            {
                "model": "gpt-4o",
                "content": json.dumps(
                    {
                        "action": "fix",
                        "confidence": 0.91,
                        "reasoning": "fallback parse ok",
                        "suggested_value": "Cuota 2",
                    }
                ),
                "usage": {},
            },
        ]
    )

    decision = engine.decide(_discrepancy(), _context_data())

    assert decision.action == "fix"
    assert decision.model_used == "gpt-4o"


def test_input_hash_is_deterministic() -> None:
    context = MagicMock()
    engine = DecisionEngine(_config(), context)

    first = engine._compute_input_hash(_discrepancy(), _context_data())
    second = engine._compute_input_hash(_discrepancy(), _context_data())

    assert first == second


def test_cache_hit_and_miss_behavior() -> None:
    context = MagicMock()
    engine = DecisionEngine(_config(), context)

    context.get_decision_by_hash.return_value = None
    assert engine._check_cache("abc") is None

    context.get_decision_by_hash.return_value = {
        "decision_json": json.dumps(
            {
                "discrepancy_id": "disc-1",
                "action": "skip",
                "reasoning": "already reviewed",
                "confidence": 0.8,
                "suggested_value": None,
            }
        ),
        "model_used": "gpt-4o-mini",
    }
    hit = engine._check_cache("abc")
    assert hit is not None
    assert hit.action == "skip"


def test_system_prompt_loading() -> None:
    context = MagicMock()
    engine = DecisionEngine(_config(), context)

    content = engine._load_system_prompt()

    assert "payment reconciliation expert" in content


def test_few_shot_examples_included_in_prompt() -> None:
    context = MagicMock()
    engine = DecisionEngine(_config(), context)

    messages = engine._build_prompt(_discrepancy(), _context_data())

    assert messages[0]["role"] == "system"
    # 5 examples => 10 messages + 1 user payload + system
    assert len(messages) == 12
    assert messages[-1]["role"] == "user"
