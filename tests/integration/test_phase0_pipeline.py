from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.models.source import BankMovement, Payment
from src.orchestrator.pipeline import ConciliationPipeline


class _CM:
    def __init__(self, target):
        self.target = target

    def __enter__(self):
        return self.target

    def __exit__(self, *_):
        return None


def test_inline_conciliation_feeds_allocation_with_cobro(
    sample_config,
    sample_commission,
    sample_student,
    monkeypatch,
):
    """Unconciliated payment + available movement → inline conciliation →
    allocation produces Cobro (because id_movimiento_bancario is now set)."""
    pipeline = ConciliationPipeline(sample_config)

    payment = Payment(
        id_pago_mp=501,
        fecha=datetime(2026, 1, 10, 12, 0, 0),
        monto=Decimal("10000.00"),
        nro_operacion="REF-501",
        id_persona=sample_student.id_persona,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=1,
        id_movimiento_bancario=-1,
        id_organizacion=sample_commission.id_organizacion,
        razon_social_originante=None,
        dni_cuit_originante=None,
        controlado_auto=False,
        estado_conciliacion_auto="pendiente",
    )
    movement = BankMovement(
        id_movimiento=601,
        id_cuenta_bancaria=1,
        id_persona=sample_student.id_persona,
        fecha=date(2026, 1, 10),
        referencia="REF-501",
        causal=None,
        concepto=None,
        importe=Decimal("10000.00"),
        conciliado=False,
    )

    monkeypatch.setattr(pipeline.sql, "connect", lambda: _CM(pipeline.sql))
    monkeypatch.setattr(pipeline.sheets, "connect", lambda: _CM(pipeline.sheets))
    monkeypatch.setattr(pipeline.sheets, "read_all_rows", lambda: [])

    monkeypatch.setattr(pipeline.sql, "get_active_commissions", lambda year, id_organizacion=2: [sample_commission])
    monkeypatch.setattr(pipeline.sql, "get_students", lambda _id: [sample_student])
    monkeypatch.setattr(pipeline.sql, "get_all_payments", lambda _id, year=None, id_organizacion=None: [payment])
    monkeypatch.setattr(pipeline.sql, "get_conciliated_payments", lambda _id, year=None, id_organizacion=None: [])
    monkeypatch.setattr(pipeline.sql, "get_available_movements", lambda _id, year=None: [movement])
    monkeypatch.setattr(
        pipeline.sql,
        "get_active_commissions_for_student",
        lambda _id, year, id_organizacion=2: [sample_commission],
    )

    summary = pipeline.run(dry_run=True)

    assert summary["payments_conciliated"] == 1
    # Inscription allocated: should produce at least 1 Venta + 1 Cobro = 2 inserts
    assert summary["patch_summary"]["by_type"]["insert_row"] >= 2
