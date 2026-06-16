from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.models.source import BankMovement, Payment
from src.rules.conciliator import Conciliator


def _payment(*, amount: str = "10000.00", when: datetime = datetime(2026, 1, 10, 12, 0, 0), ref: str | None = "OP-1", id_mov: int | None = None, id_pago: int = 1) -> Payment:
    return Payment(
        id_pago_mp=id_pago,
        fecha=when,
        monto=Decimal(amount),
        nro_operacion=ref,
        id_persona=1,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=2,
        id_movimiento_bancario=id_mov,
        razon_social_originante="Juan",
        dni_cuit_originante="30111222",
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )


def _movement(*, mid: int, amount: str = "10000.00", when: date = date(2026, 1, 10), ref: str | None = "OP-1", conciliated: bool = False) -> BankMovement:
    return BankMovement(
        id_movimiento=mid,
        id_cuenta_bancaria=1,
        id_persona=1,
        fecha=when,
        referencia=ref,
        causal=None,
        concepto=None,
        importe=Decimal(amount),
        conciliado=conciliated,
    )


def test_exact_match_auto_conciliated() -> None:
    cp = Conciliator().try_conciliate(_payment(), [_movement(mid=10)])
    assert cp.conciliated_by == "auto"
    assert cp.movement is not None


def test_same_amount_same_month_auto_conciliated() -> None:
    cp = Conciliator().try_conciliate(
        _payment(when=datetime(2026, 1, 4, 9, 0, 0), ref=None),
        [_movement(mid=11, when=date(2026, 1, 25), ref=None)],
    )
    assert cp.conciliated_by == "auto"


def test_same_amount_matching_reference_auto_conciliated() -> None:
    cp = Conciliator().try_conciliate(
        _payment(when=datetime(2026, 2, 1, 9, 0, 0), ref="REF-X"),
        [_movement(mid=12, when=date(2026, 3, 1), ref="REF-X")],
    )
    assert cp.conciliated_by == "auto"


def test_multiple_matches_results_unconciliated() -> None:
    cp = Conciliator().try_conciliate(_payment(), [_movement(mid=13), _movement(mid=14)])
    assert cp.conciliated_by == "unconciliated"
    assert cp.movement is None


def test_no_matches_results_unconciliated() -> None:
    cp = Conciliator().try_conciliate(_payment(amount="10000.00"), [_movement(mid=15, amount="9999.99")])
    assert cp.conciliated_by == "unconciliated"


def test_already_conciliated_payment_marked_existing() -> None:
    result = Conciliator().build_conciliated_list([_payment(id_mov=99)], [_movement(mid=16)])
    assert result[0].conciliated_by == "existing"
    assert result[0].movement is None


def test_used_movements_not_reused_for_subsequent_payments() -> None:
    conciliator = Conciliator()
    movements = [_movement(mid=20)]
    result = conciliator.build_conciliated_list([_payment(id_pago=1), _payment(id_pago=2, ref="OP-2")], movements)
    assert result[0].movement is not None
    assert result[1].movement is None
    assert result[1].conciliated_by == "unconciliated"


def test_build_persistable_match_reports_match_reason() -> None:
    match = Conciliator().build_persistable_match(_payment(), [_movement(mid=30)])

    assert match.status == "matched"
    assert match.matched_by == "exact_date"
    assert match.movement is not None


def test_build_persistable_matches_marks_existing_payment() -> None:
    matches = Conciliator().build_persistable_matches([_payment(id_mov=99)], [_movement(mid=31)])

    assert matches[0].status == "existing"
    assert matches[0].candidate_ids == (99,)


def test_build_persistable_matches_reports_multiple_candidates() -> None:
    matches = Conciliator().build_persistable_matches(
        [_payment()],
        [_movement(mid=40), _movement(mid=41)],
    )

    assert matches[0].status == "multiple"
    assert matches[0].candidate_ids == (40, 41)
