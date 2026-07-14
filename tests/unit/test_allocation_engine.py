from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.models.pipeline import ConciliatedPayment
from src.models.sheet import SheetRow
from src.models.source import BankMovement, Commission, Payment, Student
from src.rules.allocation_engine import AllocationEngine, Ledger


def _commission(
    *,
    insc: str | None = "54800.00",
    cuota: str | None = "98640.00",
    total: int = 8,
    valor_pago_unico: str | None = None,
) -> Commission:
    return Commission(
        id_comision=10,
        id_curso=60,
        id_organizacion=2,
        nombre="Comisión 2026",
        valor_inscripcion=Decimal(insc) if insc else None,
        valor_inscripcion_promocion=Decimal(insc) if insc else None,
        valor_cuota=Decimal(cuota) if cuota else None,
        valor_cuota_bonificada=Decimal(cuota) if cuota else None,
        valor_pago_unico=Decimal(valor_pago_unico) if valor_pago_unico else None,
        cantidad_cuotas=total,
        duracion_meses=9,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


def _short_commission(
    *,
    insc_full: str = "259000.00",
    insc_promo: str = "229000.00",
    valor_pago_unico: str | None = None,
) -> Commission:
    return Commission(
        id_comision=20,
        id_curso=80,
        id_organizacion=2,
        nombre="Curso corto",
        valor_inscripcion=Decimal(insc_full),
        valor_inscripcion_promocion=Decimal(insc_promo),
        valor_cuota=None,
        valor_cuota_bonificada=None,
        valor_pago_unico=Decimal(valor_pago_unico) if valor_pago_unico else None,
        cantidad_cuotas=0,
        duracion_meses=4,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


def _student() -> Student:
    return Student(
        id_persona=1,
        nombres="Juan",
        apellidos="Pérez",
        apellidos_nombres=None,
        dni="30111222",
        email=None,
        id_estado_academico=None,
        id_estado_administrativo=None,
        eliminado=False,
    )


def _student_with_notes(*, persona: str | None = None, comision: str | None = None) -> Student:
    student = _student()
    student.persona_observaciones = persona
    student.comision_observaciones = comision
    return student


def _cp(*, amount: str, concept_id: int | None = 1, with_movement: bool = True, id_pago: int = 1) -> ConciliatedPayment:
    payment = Payment(
        id_pago_mp=id_pago,
        fecha=datetime(2026, 1, 10, 12, 0, 0),
        monto=Decimal(amount),
        nro_operacion="OP-1",
        id_persona=1,
        id_medio_pago=1,
        fecha_carga=None,
        controlado=False,
        comentario_cliente=None,
        id_concepto_pago=concept_id,
        id_movimiento_bancario=100 if with_movement else None,
        razon_social_originante="Juan Pérez",
        dni_cuit_originante="30111222",
        controlado_auto=False,
        estado_conciliacion_auto=None,
    )
    movement = (
        BankMovement(
            id_movimiento=100,
            id_cuenta_bancaria=1,
            id_persona=1,
            fecha=date(2026, 1, 10),
            referencia="OP-1",
            causal=None,
            concepto=None,
            importe=Decimal(amount),
            conciliado=False,
        )
        if with_movement
        else None
    )
    return ConciliatedPayment(payment=payment, movement=movement, conciliated_by="auto")


def _sheet_row(
    concept: str,
    amount: str,
    *,
    tipo: str = "Venta",
    row: int = 2,
    fecha_val: date = date(2026, 1, 1),
) -> SheetRow:
    return SheetRow(
        row_number=row,
        organizacion="Org",
        curso="Curso",
        comision="Comisión 2026",
        fecha_movimiento=fecha_val,
        tipo_movimiento=tipo,
        dni="30111222",
        concepto=concept,
        monto=Decimal(amount),
        medio_pago="No aplica" if tipo == "Venta" else "Transferencia Bancaria",
        estudiante="Pérez Juan",
        estado_administrativo="Activo",
        estado_deuda=None,
        id_movimiento_bancario=None,
        id_pago_mp=1,
    )


# ---------------------------------------------------------------------------
# Phase 1: Commission exposes valor_pago_unico
# ---------------------------------------------------------------------------


def test_commission_exposes_valor_pago_unico() -> None:
    """Commission model accepts and stores valor_pago_unico field."""
    c = _commission(valor_pago_unico="500000.00")
    assert c.valor_pago_unico == Decimal("500000.00")

    c_none = _commission()
    assert c_none.valor_pago_unico is None


# ---------------------------------------------------------------------------
# Phase 2: Zero-cuotas allocation branch
# ---------------------------------------------------------------------------


def _zero_cuotas_commission(
    *,
    insc: str = "229000.00",
    insc_promo: str = "229000.00",
    valor_pago_unico: str | None = None,
) -> Commission:
    """Commission with cantidad_cuotas=0 and no cuota price (not short course)."""
    return Commission(
        id_comision=30,
        id_curso=90,
        id_organizacion=2,
        nombre="Zero cuotas comm",
        valor_inscripcion=Decimal(insc),
        valor_inscripcion_promocion=Decimal(insc_promo),
        valor_cuota=None,
        valor_cuota_bonificada=None,
        valor_pago_unico=Decimal(valor_pago_unico) if valor_pago_unico else None,
        cantidad_cuotas=0,
        duracion_meses=12,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )


def test_zero_cuotas_matches_inscription() -> None:
    """cantidad_cuotas=0, cuota=None, monto==inscription → Inscripción."""
    comm = _zero_cuotas_commission()
    result = AllocationEngine(comm).allocate(
        [_cp(amount="229000.00", concept_id=1)], [], _student(),
    )
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"


def test_zero_cuotas_matches_promo_inscription() -> None:
    """cantidad_cuotas=0, monto==inscripcion_promocion → Inscripción."""
    comm = _zero_cuotas_commission(insc="300000.00", insc_promo="250000.00")
    result = AllocationEngine(comm).allocate(
        [_cp(amount="250000.00", concept_id=1)], [], _student(),
    )
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"


def test_zero_cuotas_matches_pago_unico() -> None:
    """cantidad_cuotas=0, monto==valor_pago_unico → Pago Único."""
    comm = _zero_cuotas_commission(valor_pago_unico="500000.00")
    result = AllocationEngine(comm).allocate(
        [_cp(amount="500000.00", concept_id=1)], [], _student(),
    )
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Pago Único"


def test_zero_cuotas_unmatched_returns_none() -> None:
    """cantidad_cuotas=0, pago_unico=None, monto≠inscription → ambiguous."""
    comm = _zero_cuotas_commission()
    result = AllocationEngine(comm).allocate(
        [_cp(amount="999999.00", concept_id=1)], [], _student(),
    )
    assert len(result.allocated) == 0
    assert len(result.ambiguous) == 1


# ---------------------------------------------------------------------------
# Phase 3: Pago Único for cuota-based commissions
# ---------------------------------------------------------------------------


def test_cuota_based_matches_pago_unico() -> None:
    """cuotas=8, monto==valor_pago_unico → Pago Único (before cuota matching)."""
    comm = _commission(valor_pago_unico="750000.00")
    # Pay inscription first, then the pago_unico amount
    insc_cp = _cp(amount="54800.00", concept_id=1, id_pago=1)
    insc_cp.payment.fecha = datetime(2026, 1, 5, 12, 0, 0)
    pago_cp = _cp(amount="750000.00", concept_id=2, id_pago=2)
    pago_cp.payment.fecha = datetime(2026, 2, 10, 12, 0, 0)
    result = AllocationEngine(comm).allocate([insc_cp, pago_cp], [], _student())
    concepts = [a.concept for a in result.allocated]
    assert "Pago Único" in concepts
    pago = next(a for a in result.allocated if a.concept == "Pago Único")
    assert pago.amount == Decimal("750000.00")


def test_cuota_based_no_pago_unico_unchanged() -> None:
    """cuotas=8, valor_pago_unico=None, monto==cuota → existing Cuota N behavior."""
    comm = _commission()  # no valor_pago_unico
    result = AllocationEngine(comm).allocate(
        [_cp(amount="54800.00", concept_id=1, id_pago=1), _cp(amount="98640.00", concept_id=2, id_pago=2)],
        [],
        _student(),
    )
    concepts = [a.concept for a in result.allocated]
    assert concepts == ["Inscripción", "Cuota 1"]


# ---------------------------------------------------------------------------
# Phase 4: Candidates + Plausibility
# ---------------------------------------------------------------------------


def test_pago_unico_candidate_generated() -> None:
    """_generate_candidates includes Pago Único when pago_unico_price is set and monto within 10%."""
    comm = _commission(valor_pago_unico="750000.00")
    engine = AllocationEngine(comm)
    # monto within 10% of valor_pago_unico
    cp = _cp(amount="740000.00", concept_id=2)
    ledger = Ledger(inscription_paid=True)
    candidates = engine._generate_candidates(cp, ledger)
    pago_candidates = [c for c in candidates if c.concept == "Pago Único"]
    assert len(pago_candidates) >= 1
    assert pago_candidates[0].amount == Decimal("750000.00")


def test_plausibility_accepts_explicit_pago_unico() -> None:
    """_is_monto_plausible_for_concept returns True when valor_pago_unico matches monto.

    Uses a valor_pago_unico that is far from cuota*cant so only the explicit
    valor_pago_unico candidate makes it plausible.
    """
    from src.orchestrator.pipeline import ConciliationPipeline
    # cuota*cant = 98640*8 = 789120 — a 400000 pago_unico is well outside 30% of that
    comm = _commission(valor_pago_unico="400000.00")
    result = ConciliationPipeline._is_monto_plausible_for_concept(
        Decimal("400000.00"), "Pago Único", comm,
    )
    assert result is True


def test_exact_inscription_allocation() -> None:
    result = AllocationEngine(_commission()).allocate([_cp(amount="54800.00", concept_id=1)], [], _student())
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"
    assert result.allocated[0].amount == Decimal("54800.00")


def test_short_course_single_payment_uses_pago_unico() -> None:
    result = AllocationEngine(_short_commission()).allocate([_cp(amount="229000.00", concept_id=1)], [], _student())
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Pago Único"
    assert result.allocated[0].amount == Decimal("229000.00")


def test_near_inscription_allocation_preserves_actual_amount() -> None:
    result = AllocationEngine(_commission()).allocate([_cp(amount="55000.00", concept_id=1)], [], _student())
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"
    assert result.allocated[0].amount == Decimal("55000.00")


def test_cobro_requires_persisted_conciliation_even_if_movement_exists_locally() -> None:
    cp = _cp(amount="54800.00", concept_id=1, with_movement=True)
    cp.payment.id_movimiento_bancario = None

    result = AllocationEngine(_commission()).allocate([cp], [], _student())

    assert len(result.allocated) == 1
    assert result.allocated[0].generates_cobro is False


def test_exact_cuota_allocation_with_correct_ordinal() -> None:
    result = AllocationEngine(_commission()).allocate([_cp(amount="98640.00", concept_id=2)], [_sheet_row("Inscripción", "54800.00")], _student())
    assert result.allocated[0].concept == "Cuota 1"


def test_combined_inscription_plus_cuota_split() -> None:
    result = AllocationEngine(_commission()).allocate([_cp(amount="153440.00", concept_id=1)], [], _student())
    concepts = [a.concept for a in result.allocated]
    assert concepts == ["Inscripción", "Cuota 1"]


def test_pago_unico_all_remaining_cuotas() -> None:
    """Pago Único is detected when the monto equals ALL remaining cuotas after prior payments."""
    prior_insc = _cp(amount="54800.00", concept_id=1, id_pago=1)
    prior_cuota1 = _cp(amount="98640.00", concept_id=2, id_pago=2)
    prior_cuota2 = _cp(amount="98640.00", concept_id=2, id_pago=3)
    remaining = Decimal("98640.00") * Decimal("6")
    pago_unico = _cp(amount=format(remaining, "f"), concept_id=2, id_pago=4)
    result = AllocationEngine(_commission()).allocate(
        [prior_insc, prior_cuota1, prior_cuota2, pago_unico], [], _student(),
    )
    concepts = [a.concept for a in result.allocated]
    assert "Pago Único" in concepts
    pago = next(a for a in result.allocated if a.concept == "Pago Único")
    assert pago.amount == remaining


def test_inscription_plus_pago_unico_split() -> None:
    remaining = Decimal("98640.00") * Decimal("8")
    total = Decimal("54800.00") + remaining
    result = AllocationEngine(_commission()).allocate([_cp(amount=format(total, "f"), concept_id=1)], [], _student())
    assert [a.concept for a in result.allocated] == ["Inscripción", "Pago Único"]


def test_cuota_ordinal_follows_prior_payments_not_sheet() -> None:
    """Cuota ordinal is determined by prior payments in chronological order, not sheet rows."""
    prior_insc = _cp(amount="54800.00", concept_id=1, id_pago=1)
    prior_cuota1 = _cp(amount="98640.00", concept_id=2, id_pago=2)
    prior_cuota2 = _cp(amount="98640.00", concept_id=2, id_pago=3)
    new_cuota = _cp(amount="98640.00", concept_id=2, id_pago=4)
    result = AllocationEngine(_commission()).allocate(
        [prior_insc, prior_cuota1, prior_cuota2, new_cuota], [], _student(),
    )
    concepts = [a.concept for a in result.allocated]
    assert concepts == ["Inscripción", "Cuota 1", "Cuota 2", "Cuota 3"]


def test_ambiguous_amount_without_price_match_generates_candidates() -> None:
    result = AllocationEngine(_commission()).allocate([_cp(amount="150000.00", concept_id=2)], [], _student())
    assert len(result.allocated) == 0
    assert len(result.ambiguous) == 1
    assert len(result.ambiguous[0].candidates) >= 1


def test_first_cuota_without_inscription_context_goes_ambiguous() -> None:
    """When the student has no inscription context yet, a cuota-only payment
    should not be auto-allocated as Cuota 1.

    This guard prevents historical bad rows where the engine skipped the
    inscription entirely and started generating cuotas directly.
    """
    result = AllocationEngine(_commission()).allocate([_cp(amount="98640.00", concept_id=2)], [], _student())
    assert result.allocated == []
    assert len(result.ambiguous) == 1


def test_db_says_cuota_but_inscription_amount_becomes_inscription() -> None:
    """If DB says CUOTA but the amount exactly matches inscription and there is
    no inscription context yet, prefer Inscripción over Cuota 1.
    """
    result = AllocationEngine(_commission()).allocate([_cp(amount="54800.00", concept_id=2)], [], _student())
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"


def test_db_says_cuota_but_inscription_plus_cuota_splits_correctly() -> None:
    """If DB says CUOTA but the amount equals inscripción + cuota, split it
    instead of forcing a cuota-only allocation.
    """
    result = AllocationEngine(_commission()).allocate([_cp(amount="153440.00", concept_id=2)], [], _student())
    assert [a.concept for a in result.allocated] == ["Inscripción", "Cuota 1"]


def test_conciliated_payment_generates_cobro_even_if_venta_exists() -> None:
    """A conciliated payment whose Venta already exists still emits the full
    allocation so the reconciler can consume the existing Venta and check the Cobro.

    The engine builds the ledger from payments (not sheet rows), so a cuota
    payment with concept_id=2 and the DB-backed wide tolerance (50%-150%)
    will be assigned as Cuota 1 when no prior inscription payment exists.
    The sheet row is OUTPUT, not INPUT for the ledger.
    """
    rows = [_sheet_row("Inscripción", "54800.00")]
    result = AllocationEngine(_commission()).allocate([_cp(amount="54800.00", concept_id=2)], rows, _student())
    assert len(result.allocated) == 1
    # concept_id=2 (CUOTA) + monto in 50%-150% of cuota price -> Cuota 1
    assert result.allocated[0].concept == "Cuota 1"
    assert result.allocated[0].generates_cobro is True
    assert result.allocated[0].generates_venta is True


def test_exact_conciliated_payment_already_reflected_is_consumed_without_ambiguity() -> None:
    rows = [
        _sheet_row("Inscripción", "54800.00", tipo="Venta", fecha_val=date(2026, 1, 10)),
        _sheet_row("Inscripción", "54800.00", tipo="Cobro", row=3, fecha_val=date(2026, 1, 10)),
    ]
    result = AllocationEngine(_commission()).allocate([_cp(amount="54800.00", concept_id=1)], rows, _student())
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"
    assert result.allocated[0].generates_venta is True
    assert result.allocated[0].generates_cobro is True
    assert result.ambiguous == []


def test_near_inscription_payment_already_reflected_is_consumed_without_ambiguity() -> None:
    rows = [
        _sheet_row("Inscripción", "55000.00", tipo="Venta", fecha_val=date(2026, 1, 10)),
        _sheet_row("Inscripción", "55000.00", tipo="Cobro", row=3, fecha_val=date(2026, 1, 10)),
    ]
    result = AllocationEngine(_commission()).allocate([_cp(amount="55000.00", concept_id=1)], rows, _student())
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"
    assert result.allocated[0].amount == Decimal("55000.00")
    assert result.ambiguous == []


def test_combined_payment_already_fully_reflected_does_not_become_ambiguous() -> None:
    commission = Commission(
        id_comision=30,
        id_curso=90,
        id_organizacion=2,
        nombre="La Carlota",
        valor_inscripcion=Decimal("109600.00"),
        valor_inscripcion_promocion=Decimal("54800.00"),
        valor_cuota=Decimal("109600.00"),
        valor_cuota_bonificada=Decimal("98640.00"),
        cantidad_cuotas=9,
        duracion_meses=9,
        fecha_inicio=date(2026, 1, 1),
        borrado=False,
    )
    rows = [
        _sheet_row("Inscripción", "54800.00", tipo="Venta", row=1),
        _sheet_row("Inscripción", "109600.00", tipo="Cobro", row=2),
        _sheet_row("Cuota 1", "98640.00", tipo="Venta", row=3),
        _sheet_row("Cuota 1", "98640.00", tipo="Cobro", row=4),
    ]
    result = AllocationEngine(commission).allocate(
        [_cp(amount="208240.00", concept_id=2)], rows, _student(),
    )
    assert len(result.allocated) == 2
    assert [a.concept for a in result.allocated] == ["Inscripción", "Cuota 1"]
    assert result.ambiguous == []


def test_unconciliated_payment_exact_inscription_gets_allocated_not_ambiguous() -> None:
    """An unconciliated payment matching exact inscription price should be
    allocated deterministically (without Cobro), not sent to ambiguous."""
    result = AllocationEngine(_commission()).allocate(
        [_cp(amount="54800.00", concept_id=1, with_movement=False)], [], _student(),
    )
    assert len(result.allocated) == 1
    assert result.allocated[0].concept == "Inscripción"
    assert result.allocated[0].generates_cobro is False


def test_next_venta_skipped_when_no_payments() -> None:
    result = AllocationEngine(_commission()).allocate([], [], _student())
    assert result.next_venta is None


def test_next_venta_generated_when_payments_exist_and_cuotas_remain() -> None:
    result = AllocationEngine(_commission()).allocate(
        [_cp(amount="54800.00", concept_id=1)],
        [],
        _student(),
    )
    assert result.next_venta is not None
    assert result.next_venta.concepto == "Cuota 1"


def test_next_venta_when_everything_paid_returns_none() -> None:
    rows = [_sheet_row("Inscripción", "54800.00")]
    rows.extend(_sheet_row(f"Cuota {i}", "98640.00", row=i + 2) for i in range(1, 9))
    result = AllocationEngine(_commission()).allocate([], rows, _student())
    assert result.next_venta is None


def test_next_venta_when_pago_unico_plus_inscription_fully_paid_returns_none() -> None:
    rows = [_sheet_row("Inscripción", "54800.00"), _sheet_row("Pago Único", "789120.00", row=3)]
    result = AllocationEngine(_commission()).allocate([], rows, _student())
    assert result.next_venta is None


def test_ledger_building_from_existing_sheet_rows() -> None:
    ledger = Ledger.from_sheet_rows([_sheet_row("Inscripción", "54800.00"), _sheet_row("Cuota 1", "98640.00", row=3), _sheet_row("Cuota 2", "98640.00", row=4), _sheet_row("Pago Único", "591840.00", row=5)])
    assert ledger.inscription_paid is True
    assert ledger.cuotas_paid == 2
    assert ledger.pago_unico is True


def test_empty_payments_returns_empty_allocations_no_next_venta() -> None:
    result = AllocationEngine(_commission()).allocate([], [], _student())
    assert result.allocated == []
    assert result.ambiguous == []
    assert result.next_venta is None


def test_none_prices_send_all_payments_to_ambiguous() -> None:
    result = AllocationEngine(_commission(insc=None, cuota=None)).allocate([_cp(amount="54800.00", concept_id=1), _cp(amount="98640.00", concept_id=2, id_pago=2)], [], _student())
    assert result.allocated == []
    assert len(result.ambiguous) == 2


def test_two_complementary_payments_can_complete_one_cuota() -> None:
    """Two payments that together equal a cuota should be bundled.

    The engine builds its ledger from payments, so we pass a real inscription
    payment first so the ledger knows inscription is paid before processing
    the two cuota-concept payments.

    Note: $90,000 falls within the DB-backed wide tolerance (50%-150% of
    $98,640 cuota price), so the first payment is allocated deterministically
    as Cuota 1.  The second ($8,640) is too small for any match and goes
    ambiguous.  This is the correct production behaviour.
    """
    insc = _cp(amount="54800.00", concept_id=1, id_pago=0)
    insc.payment.fecha = datetime(2026, 1, 5, 12, 0, 0)

    first = _cp(amount="90000.00", concept_id=2, id_pago=1)
    second = _cp(amount="8640.00", concept_id=2, id_pago=2)
    second.payment.fecha = datetime(2026, 1, 13, 12, 0, 0)
    if second.movement is not None:
        second.movement.importe = Decimal("8640.00")
        second.movement.fecha = date(2026, 1, 13)

    result = AllocationEngine(_commission()).allocate(
        [insc, first, second],
        [],
        _student(),
    )

    # insc -> Inscripción, first ($90k in 50-150% of cuota) -> Cuota 1
    cuota_allocs = [a for a in result.allocated if "Cuota" in a.concept]
    assert len(cuota_allocs) == 1
    assert cuota_allocs[0].concept == "Cuota 1"
    assert cuota_allocs[0].amount == Decimal("90000.00")
    # second ($8,640) is too small and goes ambiguous
    assert len(result.ambiguous) == 1


def test_exact_two_cuotas_together_allocate_sequential_cuotas() -> None:
    result = AllocationEngine(_commission()).allocate(
        [_cp(amount="197280.00", concept_id=2)],
        [_sheet_row("Inscripción", "54800.00")],
        _student(),
    )

    assert [a.concept for a in result.allocated] == ["Cuota 1", "Cuota 2"]
    assert all(a.amount == Decimal("98640.00") for a in result.allocated)


def test_discount_observations_boost_discounted_cuota_candidate() -> None:
    """A discounted cuota amount with concept_id=2 and within 50-150% of the
    cuota price is resolved deterministically by the DB-backed wide tolerance
    path — it does NOT go ambiguous even with discount observations.

    $90,000 / $98,640 = 0.91 ratio, well within the 0.50-1.50 range.
    """
    insc = _cp(amount="54800.00", concept_id=1, id_pago=0)
    insc.payment.fecha = datetime(2026, 1, 5, 12, 0, 0)

    cuota_payment = _cp(amount="90000.00", concept_id=2)
    result = AllocationEngine(_commission()).allocate(
        [insc, cuota_payment],
        [],
        _student_with_notes(comision="Beca del 10% sobre las cuotas"),
    )

    # DB-backed wide tolerance allocates deterministically as Cuota 1
    assert len(result.ambiguous) == 0
    assert len(result.allocated) == 2
    cuota_allocs = [a for a in result.allocated if "Cuota" in a.concept]
    assert len(cuota_allocs) == 1
    assert cuota_allocs[0].concept == "Cuota 1"
    assert cuota_allocs[0].amount == Decimal("90000.00")


def test_recargo_like_amount_boosts_cuota_candidate() -> None:
    """A recargo-like amount with concept_id=2 and within 50-150% of the
    cuota price is resolved deterministically by the DB-backed wide tolerance
    path — it does NOT go ambiguous even with recargo observations.

    $109,600 / $98,640 = 1.11 ratio, well within the 0.50-1.50 range.
    """
    insc = _cp(amount="54800.00", concept_id=1, id_pago=0)
    insc.payment.fecha = datetime(2026, 1, 5, 12, 0, 0)

    recargo_payment = _cp(amount="109600.00", concept_id=2)
    result = AllocationEngine(_commission()).allocate(
        [insc, recargo_payment],
        [],
        _student_with_notes(comision="Alumno con recargo por mora"),
    )

    # DB-backed wide tolerance allocates deterministically as Cuota 1
    assert len(result.ambiguous) == 0
    assert len(result.allocated) == 2
    cuota_allocs = [a for a in result.allocated if "Cuota" in a.concept]
    assert len(cuota_allocs) == 1
    assert cuota_allocs[0].concept == "Cuota 1"
    assert cuota_allocs[0].amount == Decimal("109600.00")


# ---------------------------------------------------------------------------
# renumber_allocations with initial_ledger
# ---------------------------------------------------------------------------


def test_renumber_with_initial_ledger_continues_numbering() -> None:
    """Regression: when cutoff skips cuotas 1-3, the 4th cuota payment
    must be numbered Cuota 4, not Cuota 1.

    This is the exact scenario of DNI 46603843: 3 cuotas already in the
    sheet, a new payment arrives and renumber_allocations was resetting
    the ledger to zero, producing 'Cuota 1' instead of 'Cuota 4'.
    """
    engine = AllocationEngine(_commission(total=9))

    # Simulate the new payment that arrives post-cutoff
    cuota4 = _cp(amount="98640.00", concept_id=2, id_pago=87202)
    cuota4.payment.fecha = datetime(2026, 6, 10, 12, 0, 0)

    from src.models.pipeline import Allocation
    allocations = [
        Allocation(payment=cuota4, concept="Cuota 4", amount=Decimal("98640.00"),
                   generates_venta=True, generates_cobro=True),
    ]

    # Ledger from sheet: inscription + 3 cuotas already paid
    pre_ledger = Ledger(
        inscription_paid=True,
        cuotas_paid=3,
        existing_concepts={"Inscripción", "Cuota 1", "Cuota 2", "Cuota 3"},
    )

    renumbered, next_venta = engine.renumber_allocations(
        allocations, _student(), initial_ledger=pre_ledger,
    )

    assert len(renumbered) == 1
    assert renumbered[0].concept == "Cuota 4"
    assert next_venta is not None
    assert next_venta.concepto == "Cuota 5"


def test_renumber_without_initial_ledger_starts_from_one() -> None:
    """Without initial_ledger (no cutoff), renumbering starts from Cuota 1."""
    engine = AllocationEngine(_commission())

    cuota = _cp(amount="98640.00", concept_id=2, id_pago=1)
    cuota.payment.fecha = datetime(2026, 3, 10, 12, 0, 0)

    from src.models.pipeline import Allocation
    allocations = [
        Allocation(payment=cuota, concept="Cuota 5", amount=Decimal("98640.00"),
                   generates_venta=True, generates_cobro=True),
    ]

    renumbered, _ = engine.renumber_allocations(allocations, _student())

    assert renumbered[0].concept == "Cuota 1"


def test_renumber_with_initial_ledger_multiple_new_cuotas() -> None:
    """When initial_ledger has 2 cuotas and 3 new arrive, they get 3, 4, 5."""
    engine = AllocationEngine(_commission(total=8))

    from src.models.pipeline import Allocation
    allocs = []
    for i, (pago_id, month) in enumerate([(100, 3), (101, 4), (102, 5)]):
        cp = _cp(amount="98640.00", concept_id=2, id_pago=pago_id)
        cp.payment.fecha = datetime(2026, month, 10, 12, 0, 0)
        allocs.append(
            Allocation(payment=cp, concept=f"Cuota {i+1}", amount=Decimal("98640.00"),
                       generates_venta=True, generates_cobro=True)
        )

    pre_ledger = Ledger(
        inscription_paid=True,
        cuotas_paid=2,
        existing_concepts={"Inscripción", "Cuota 1", "Cuota 2"},
    )

    renumbered, next_venta = engine.renumber_allocations(
        allocs, _student(), initial_ledger=pre_ledger,
    )

    assert [a.concept for a in renumbered] == ["Cuota 3", "Cuota 4", "Cuota 5"]
    assert next_venta is not None
    assert next_venta.concepto == "Cuota 6"
