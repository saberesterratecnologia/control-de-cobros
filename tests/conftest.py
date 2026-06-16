"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.context.context_manager import ContextManager
from src.models.pipeline import Allocation, ConciliatedPayment
from src.models.sheet import SheetRow
from src.models.source import BankMovement, Commission, Payment, Student


@pytest.fixture
def sample_config(tmp_path: Path) -> dict:
    return {
        "database": {"driver": "ODBC Driver 17 for SQL Server", "server": "", "database": "", "trusted_connection": True},
        "sheets": {
            "credentials_file": "credentials.json",
            "spreadsheet_name": "test-sheet",
            "worksheet_name": "COBROS",
        },
        "llm": {
            "primary_model": "gpt-4o-mini",
            "fallback_model": "gpt-4o",
            "confidence_threshold_auto": 0.90,
            "confidence_threshold_flagged": 0.75,
            "max_retries": 1,
            "temperature": 0.1,
        },
        "agent": {
            "dry_run": True,
            "id_organizacion": 2,
            "year": 2026,
            "skip_write_back": False,
            "batch_size": 20,
            "checkpoint_interval": 20,
            "max_pending_review": 100,
        },
        "sqlite": {"db_path": str(tmp_path / "context.db")},
        "logging": {"level": "INFO", "format": "json", "file": str(tmp_path / "agent.log")},
    }


@pytest.fixture
def sample_payment() -> Payment:
    return Payment(
        id_pago_mp=101,
        fecha=datetime(2026, 1, 10, 10, 30, 0),
        monto=Decimal("10000"),
        nro_operacion="OP-1",
        id_persona=1,
        id_medio_pago=2,
        fecha_carga=datetime(2026, 1, 10, 10, 31, 0),
        controlado=True,
        comentario_cliente=None,
        id_concepto_pago=1,
        id_movimiento_bancario=201,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=True,
        estado_conciliacion_auto=None,
    )


@pytest.fixture
def sample_movement() -> BankMovement:
    return BankMovement(
        id_movimiento=201,
        id_cuenta_bancaria=1,
        id_persona=1,
        fecha=date(2026, 1, 10),
        referencia="REF",
        causal="Pago",
        concepto="Cuota",
        importe=Decimal("10000"),
        conciliado=True,
    )


@pytest.fixture
def sample_commission() -> Commission:
    return Commission(
        id_comision=10,
        id_curso=60,
        id_organizacion=1,
        nombre="Comisión A",
        valor_inscripcion_promocion=Decimal("10000"),
        valor_cuota_bonificada=Decimal("5000"),
        cantidad_cuotas=12,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


@pytest.fixture
def sample_commission_with_prices() -> Commission:
    return Commission(
        id_comision=11,
        id_curso=61,
        id_organizacion=2,
        nombre="Comisión 2026",
        valor_inscripcion_promocion=Decimal("54800.00"),
        valor_cuota_bonificada=Decimal("98640.00"),
        cantidad_cuotas=8,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


@pytest.fixture
def sample_student() -> Student:
    return Student(
        id_persona=1,
        nombres="Juan",
        apellidos="Pérez",
        apellidos_nombres="Pérez Juan",
        dni="30111222",
        email="juan@example.com",
        id_estado_academico=1,
        id_estado_administrativo=1,
        eliminado=False,
    )


@pytest.fixture
def sample_sheet_row(sample_student: Student) -> SheetRow:
    return SheetRow(
        row_number=2,
        organizacion="Org",
        curso="Curso",
        comision="Comisión A",
        fecha_movimiento=date(2026, 1, 10),
        tipo_movimiento="Cobro",
        dni=sample_student.dni,
        concepto="Inscripción",
        monto=Decimal("10000"),
        medio_pago="Mercado Pago",
        estudiante="Pérez Juan",
        estado_administrativo="Activo",
        estado_deuda=None,
        id_movimiento_bancario=201,
        id_pago_mp=101,
    )


@pytest.fixture
def sample_conciliated_payment(sample_payment: Payment, sample_movement: BankMovement) -> ConciliatedPayment:
    return ConciliatedPayment(
        payment=sample_payment,
        movement=sample_movement,
        conciliated_by="existing",
    )


@pytest.fixture
def sample_unconciliated_payment(sample_payment: Payment) -> ConciliatedPayment:
    payment = sample_payment.model_copy(update={"id_movimiento_bancario": None})
    return ConciliatedPayment(
        payment=payment,
        movement=None,
        conciliated_by="unconciliated",
    )


@pytest.fixture
def sample_allocation(sample_conciliated_payment: ConciliatedPayment) -> Allocation:
    return Allocation(
        payment=sample_conciliated_payment,
        concept="Cuota 1",
        amount=Decimal("10000.00"),
        generates_venta=True,
        generates_cobro=True,
    )


@pytest.fixture
def sample_normalized_row(sample_sheet_row: SheetRow) -> SheetRow:
    return sample_sheet_row.model_copy(
        update={
            "medio_pago": "Transferencia Bancaria",
            "concepto": sample_sheet_row.concepto.strip(),
            "dni": sample_sheet_row.dni.strip(),
            "comision": sample_sheet_row.comision.strip() if sample_sheet_row.comision else None,
            "id_movimiento_bancario": sample_sheet_row.id_movimiento_bancario,
            "id_pago_mp": sample_sheet_row.id_pago_mp,
        }
    )


@pytest.fixture
def in_memory_context(tmp_path: Path) -> ContextManager:
    return ContextManager(db_path=str(tmp_path / "test_context.db"))


@pytest.fixture
def mock_connectors(monkeypatch: pytest.MonkeyPatch):
    class _FakeConnector:
        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("src.orchestrator.pipeline.SQLServerConnector", lambda *_args, **_kwargs: _FakeConnector())
    monkeypatch.setattr("src.orchestrator.pipeline.SheetsConnector", lambda *_args, **_kwargs: _FakeConnector())
    return _FakeConnector
