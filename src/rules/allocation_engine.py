"""Payment allocation engine — the core of the v2 reconciliation agent.

Replaces ``row_builder.py``.  Instead of mapping one payment → one row, this
engine builds a cumulative *ledger* of what has already been reflected in the
sheet and then *allocates* each new conciliated payment to the correct concept
(Inscripción, Cuota N, Pago Único, or a combination).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date_type
from decimal import Decimal

from src.models.pipeline import (
    Allocation,
    AllocationCandidate,
    AllocationResult,
    AmbiguousPayment,
    ConciliatedPayment,
)
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import Commission, Student
from src.rules.mappers import map_medio

LOGGER = logging.getLogger(__name__)

_DISCOUNT_HINTS = ("descuento", "beca", "bonific", "%", "promo")
_SURCHARGE_HINTS = ("recargo", "mora", "interes", "interés", "atrasad")
_SINGLE_CONCEPT_TOLERANCE = Decimal("0.01")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CUOTA_RE = re.compile(r"Cuota\s+(\d+)", re.IGNORECASE)


def _extract_cuota_number(concepto: str) -> int | None:
    """Return the ordinal from strings like 'Cuota 3', or ``None``."""
    m = _CUOTA_RE.search(concepto)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

@dataclass
class Ledger:
    """Snapshot of what is already reflected in the sheet for one student."""

    inscription_paid: bool = False
    cuotas_paid: int = 0
    pago_unico: bool = False
    fully_paid: bool = False
    existing_concepts: set[str] = field(default_factory=set)

    @classmethod
    def from_sheet_rows(cls, rows: list[SheetRow]) -> "Ledger":
        """Build ledger from existing Venta rows for one student / commission.

        Only Venta rows are considered because they represent expected payments.
        Cobro rows are confirmations and should not inflate the ledger — the
        actual payment data drives allocation, not the Cobro reflection.
        """
        ledger = cls()
        max_cuota = 0

        for row in rows:
            if row.tipo_movimiento.strip().casefold() != "venta":
                continue
            concepto = row.concepto.strip()
            ledger.existing_concepts.add(concepto)

            if "Inscripción" in concepto or "Inscripcion" in concepto:
                ledger.inscription_paid = True

            cuota_n = _extract_cuota_number(concepto)
            if cuota_n is not None and cuota_n > max_cuota:
                max_cuota = cuota_n

            if "Pago Único" in concepto or "Pago Unico" in concepto:
                ledger.pago_unico = True

        ledger.cuotas_paid = max_cuota
        if ledger.pago_unico:
            ledger.fully_paid = ledger.inscription_paid
        return ledger


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AllocationEngine:
    """Allocates conciliated payments to concepts based on commission pricing."""

    def __init__(self, commission: Commission) -> None:
        self.commission = commission
        self.duration_months: int = commission.duracion_meses or 0
        self.inscription_price: Decimal | None = commission.valor_inscripcion_promocion
        self.inscription_price_full: Decimal | None = commission.valor_inscripcion
        # Treat cuota_price=0 as "no cuotas" (single-payment course)
        raw_cuota = commission.valor_cuota_bonificada
        self.cuota_price: Decimal | None = raw_cuota if raw_cuota and raw_cuota > 0 else None
        raw_cuota_full = commission.valor_cuota
        self.cuota_price_full: Decimal | None = raw_cuota_full if raw_cuota_full and raw_cuota_full > 0 else None
        self.total_cuotas: int = commission.cantidad_cuotas or 0
        # If no cuota price, there are no cuotas regardless of cantidad_cuotas
        if self.cuota_price is None:
            self.total_cuotas = 0

        # All known single-concept prices for matching
        self._inscription_prices: list[Decimal] = [
            p for p in [self.inscription_price, self.inscription_price_full] if p is not None and p > 0
        ]
        self._cuota_prices: list[Decimal] = [
            p for p in [self.cuota_price, self.cuota_price_full] if p is not None and p > 0
        ]

    @property
    def is_short_course_single_payment(self) -> bool:
        return self.total_cuotas == 0 and 0 < self.duration_months < 9

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allocate(
        self,
        payments: list[ConciliatedPayment],
        existing_sheet_rows: list[SheetRow],
        student: Student,
        seed_ledger_from_sheet: bool = False,
    ) -> AllocationResult:
        """Main entry point.

        1. Build ledger from *existing_sheet_rows*, excluding Ventas that
           correspond to the payments being allocated (to avoid double-counting).
        2. Identify conciliated payments whose Cobro is missing from the sheet.
        3. Sort payments chronologically.
        4. For each payment, try deterministic allocation; otherwise create
           :class:`AmbiguousPayment` with scored candidates.
        5. Compute the ONE next-venta row.
        6. Return :class:`AllocationResult`.

        When *seed_ledger_from_sheet* is ``True``, the ledger is initialized
        from the existing Venta rows in the sheet.  This is used when
        already-closed payments (Venta+Cobro) have been filtered out upstream,
        so the engine needs to know what cuotas are already covered without
        seeing those payments.
        """
        cobro_rows = [
            row for row in existing_sheet_rows
            if row.tipo_movimiento.strip().casefold() == "cobro"
        ]
        sheet_has_inscription = self._sheet_has_inscription(existing_sheet_rows)
        if seed_ledger_from_sheet:
            # Use sheet Venta rows to pre-populate the ledger with concepts
            # already reflected.  This tells the engine which cuotas /
            # inscription are already covered so new payments get the
            # correct ordinal.
            ledger = Ledger.from_sheet_rows(existing_sheet_rows)
        else:
            # Build ledger from scratch — the payments themselves (in
            # chronological order) are the source of truth for what
            # concepts are covered.
            ledger = Ledger()
        # For courses with no cuotas, inscription = fully paid
        if self.total_cuotas == 0 and ledger.inscription_paid:
            ledger.fully_paid = True
        sorted_payments = sorted(
            payments, key=lambda cp: cp.payment.fecha or _date_type.min
        )

        allocated: list[Allocation] = []
        ambiguous: list[AmbiguousPayment] = []
        notes = self._student_notes(student)

        index = 0
        while index < len(sorted_payments):
            cp = sorted_payments[index]

            result = self._try_allocate(cp, ledger, sheet_has_inscription)

            # If normal allocation fails but the payment is conciliated,
            # try allocation ignoring the ledger — the Ventas exist but
            # the Cobros may be missing or have wrong amounts.
            if result is None and self._has_persisted_conciliation(cp):
                result = self._try_allocate_ignoring_ledger(cp, cobro_rows, ledger, sheet_has_inscription)

            if result is not None:
                for alloc in result:
                    allocated.append(alloc)
                    self._update_ledger(ledger, alloc)
                index += 1
                continue

            bundle = None
            bundle_size = 0
            max_bundle_size = min(4, len(sorted_payments) - index)
            for size in range(2, max_bundle_size + 1):
                candidate_bundle = self._try_allocate_bundle(
                    sorted_payments[index : index + size],
                    ledger,
                    sheet_has_inscription,
                )
                if candidate_bundle is not None:
                    bundle = candidate_bundle
                    bundle_size = size
                    break

            if bundle is not None:
                for alloc in bundle:
                    allocated.append(alloc)
                    self._update_ledger(ledger, alloc)
                index += bundle_size
                continue

            candidates = self._generate_candidates(cp, ledger, notes)
            ambiguous.append(
                AmbiguousPayment(payment=cp, candidates=candidates)
            )
            # NOTE: we intentionally do NOT advance the ledger here.
            # Provisional advance caused cuota numbering drift when the
            # ambiguous resolution differed from the best candidate.
            # Instead, the pipeline calls renumber_allocations() after
            # resolving all ambiguous payments to rebuild correct ordinals.
            index += 1

        # Only generate next_venta if the student has at least one payment.
        # Students with zero payments are just enrolled — no future Venta needed.
        has_any_payment = len(payments) > 0
        latest_date: _date_type | None = None
        if has_any_payment and sorted_payments:
            for cp in reversed(sorted_payments):
                if cp.payment.fecha is not None:
                    latest_date = cp.payment.fecha.date() if hasattr(cp.payment.fecha, 'date') else cp.payment.fecha
                    break
        next_venta = (
            self._compute_next_venta(ledger, student, reference_date=latest_date)
            if has_any_payment
            else None
        )

        return AllocationResult(
            allocated=allocated,
            ambiguous=ambiguous,
            next_venta=next_venta,
        )

    def _build_reflected_single_concept_allocation(
        self,
        cp: ConciliatedPayment,
        existing_sheet_rows: list[SheetRow],
    ) -> list[Allocation] | None:
        """Build allocation for an exact single-concept payment already reflected in sheet.

        If the sheet already has rows that clearly represent this payment
        (same dni/date/monto and recognizable concept), emit the allocation so
        the reconciler consumes those rows instead of reporting them as extras.
        Combined payments are excluded because they may need split recovery.
        """
        monto = cp.payment.monto
        candidate_type: str | None = None
        if self._single_concept_amount(monto, self.inscription_price, self.cuota_price) is not None:
            candidate_type = "inscripcion"
        elif self._single_concept_amount(monto, self.cuota_price, self.inscription_price) is not None:
            candidate_type = "cuota"

        if candidate_type is None:
            return None

        payment_date = cp.payment.fecha.date() if cp.payment.fecha else None
        dni = (cp.payment.dni_cuit_originante or "").strip()

        for row in existing_sheet_rows:
            if payment_date is not None and row.fecha_movimiento != payment_date:
                continue
            if dni and row.dni.strip() != dni:
                continue
            if row.monto != monto:
                continue
            concepto = row.concepto.strip().casefold()
            if candidate_type == "inscripcion" and ("inscripción" in concepto or "inscripcion" in concepto):
                concept = "Pago Único" if self.is_short_course_single_payment else "Inscripción"
                return [
                    Allocation(
                        payment=cp,
                        concept=concept,
                        amount=monto,
                        generates_venta=True,
                        generates_cobro=self._has_persisted_conciliation(cp),
                    )
                ]
            if candidate_type == "cuota" and "cuota" in concepto:
                return [
                    Allocation(
                        payment=cp,
                        concept=row.concepto.strip(),
                        amount=monto,
                        generates_venta=True,
                        generates_cobro=self._has_persisted_conciliation(cp),
                    )
                ]

        return None

    # ------------------------------------------------------------------
    # Deterministic allocation
    # ------------------------------------------------------------------

    def _try_allocate(
        self,
        cp: ConciliatedPayment,
        ledger: Ledger,
        sheet_has_inscription: bool = False,
    ) -> list[Allocation] | None:
        """Try to allocate deterministically.  Returns ``None`` on failure."""
        monto = cp.payment.monto
        has_cobro = self._has_persisted_conciliation(cp)

        insc = self.inscription_price
        cuota = self.cuota_price

        # Cannot allocate without pricing
        if insc is None and cuota is None:
            return None

        # DB concept hint: 1=inscription, 2=cuota, 4=recargo (surcharge on
        # late cuota).  When the DB explicitly marks the payment as cuota or
        # recargo, skip inscription matching even if the monto happens to
        # coincide with the inscription price (e.g. a 50% scholarship makes
        # cuota == inscription price).  Similarly, if the DB says inscription,
        # skip cuota matching first pass.
        db_concept = cp.payment.id_concepto_pago
        db_says_cuota = db_concept in (2, 4)  # CUOTA or RECARGO (late cuota)
        db_says_inscription = db_concept == 1
        inscription_context = self._has_inscription_context(ledger, sheet_has_inscription)

        # --- exact / near inscription (bonified or full price) ---
        if not ledger.inscription_paid and not db_says_cuota:
            for candidate_insc in self._inscription_prices:
                amount = self._single_concept_amount(monto, candidate_insc, cuota)
                if amount is not None:
                    concept = "Pago Único" if self.is_short_course_single_payment else "Inscripción"
                    return [self._make_alloc(cp, concept, amount, has_cobro)]

        # --- exact / near cuota (bonified or full price) ---
        if not db_says_inscription and inscription_context:
            for candidate_cuota in self._cuota_prices:
                if monto == candidate_cuota:
                    next_n = ledger.cuotas_paid + 1
                    return [self._make_alloc(cp, f"Cuota {next_n}", candidate_cuota, has_cobro)]

            for candidate_cuota in self._cuota_prices:
                if self._single_concept_amount(monto, candidate_cuota, insc) is not None:
                    next_n = ledger.cuotas_paid + 1
                    return [self._make_alloc(cp, f"Cuota {next_n}", monto, has_cobro)]

        # --- DB-backed cuota/recargo with wider tolerance ---
        # When the DB explicitly says CUOTA or RECARGO and the monto didn't
        # match any standard price within 1%, trust the DB concept and accept
        # the real amount if it's within a reasonable surcharge/discount range
        # (50%-150% of any known cuota price).  This handles late payments
        # with mora, partial discounts, and recargos that don't match exact prices.
        if db_says_cuota and inscription_context:
            next_n = ledger.cuotas_paid + 1
            for candidate_cuota in self._cuota_prices:
                if candidate_cuota <= 0:
                    continue
                ratio = monto / candidate_cuota
                if Decimal("0.50") <= ratio <= Decimal("1.50"):
                    return [self._make_alloc(cp, f"Cuota {next_n}", monto, has_cobro)]

        # --- Fallback: if DB said cuota but monto didn't match any cuota
        # price, OR DB said inscription but monto didn't match inscription
        # price, try the OTHER concept type as fallback so we don't
        # unnecessarily send to ambiguous. ---
        if db_says_cuota and not ledger.inscription_paid:
            # Already tried cuota above; now try inscription as fallback
            for candidate_insc in self._inscription_prices:
                amount = self._single_concept_amount(monto, candidate_insc, cuota)
                if amount is not None:
                    concept = "Pago Único" if self.is_short_course_single_payment else "Inscripción"
                    return [self._make_alloc(cp, concept, amount, has_cobro)]

        if db_says_inscription:
            # Already tried inscription above; now try cuota as fallback
            for candidate_cuota in self._cuota_prices:
                if monto == candidate_cuota:
                    next_n = ledger.cuotas_paid + 1
                    return [self._make_alloc(cp, f"Cuota {next_n}", candidate_cuota, has_cobro)]
            for candidate_cuota in self._cuota_prices:
                if self._single_concept_amount(monto, candidate_cuota, insc) is not None:
                    next_n = ledger.cuotas_paid + 1
                    return [self._make_alloc(cp, f"Cuota {next_n}", monto, has_cobro)]

        # --- exact multiple cuotas (2 or more, but not all remaining => not Pago Único) ---
        if self.total_cuotas > 0 and inscription_context:
            remaining_count = self.total_cuotas - ledger.cuotas_paid
            if remaining_count > 1:
                for candidate_cuota in self._cuota_prices:
                    for cuotas_count in range(2, remaining_count):
                        if monto == candidate_cuota * cuotas_count:
                            next_n = ledger.cuotas_paid + 1
                            return [
                                self._make_alloc(cp, f"Cuota {next_n + offset}", candidate_cuota, has_cobro)
                                for offset in range(cuotas_count)
                            ]

        # --- inscription + cuota (any price combination) ---
        if not ledger.inscription_paid:
            for candidate_insc in self._inscription_prices:
                for candidate_cuota in self._cuota_prices:
                    if monto == candidate_insc + candidate_cuota:
                        next_n = ledger.cuotas_paid + 1
                        return [
                            self._make_alloc(cp, "Inscripción", candidate_insc, has_cobro),
                            self._make_alloc(cp, f"Cuota {next_n}", candidate_cuota, has_cobro),
                        ]

        # --- inscription + multiple cuotas (any price combination) ---
        #     Excludes the case where cuotas_count == remaining to let Pago Único rule handle it.
        if not ledger.inscription_paid and self.total_cuotas > 0:
            remaining_count = self.total_cuotas - ledger.cuotas_paid
            if remaining_count > 1:
                for candidate_insc in self._inscription_prices:
                    for candidate_cuota in self._cuota_prices:
                        for cuotas_count in range(2, remaining_count):
                            if monto == candidate_insc + (candidate_cuota * cuotas_count):
                                next_n = ledger.cuotas_paid + 1
                                return [self._make_alloc(cp, "Inscripción", candidate_insc, has_cobro)] + [
                                    self._make_alloc(cp, f"Cuota {next_n + offset}", candidate_cuota, has_cobro)
                                    for offset in range(cuotas_count)
                                ]

        # --- pago único (all remaining cuotas) ---
        remaining = self._remaining_cuotas_sum(ledger)
        if inscription_context and remaining is not None and remaining > 0 and monto == remaining:
            return [self._make_alloc(cp, "Pago Único", remaining, has_cobro)]

        # --- inscription + pago único ---
        if (
            insc is not None
            and remaining is not None
            and remaining > 0
            and monto == insc + remaining
            and not ledger.inscription_paid
        ):
            return [
                self._make_alloc(cp, "Inscripción", insc, has_cobro),
                self._make_alloc(cp, "Pago Único", remaining, has_cobro),
            ]

        # --- near inscription + cuota (tolerance on combined amount) ---
        # Only split if the surplus is plausible as a cuota price.
        # Use the STANDARD cuota price for the allocation, not the raw surplus.
        if not ledger.inscription_paid:
            for candidate_insc in self._inscription_prices:
                for candidate_cuota in self._cuota_prices:
                    target = candidate_insc + candidate_cuota
                    if self._is_within_percent_tolerance(monto, target):
                        surplus = monto - candidate_insc
                        if self._is_within_percent_tolerance(surplus, candidate_cuota):
                            next_n = ledger.cuotas_paid + 1
                            return [
                                self._make_alloc(cp, "Inscripción", candidate_insc, has_cobro),
                                self._make_alloc(cp, f"Cuota {next_n}", candidate_cuota, has_cobro),
                            ]

        return None

    def _filter_independent_rows(
        self,
        sheet_rows: list[SheetRow],
        payments: list[ConciliatedPayment],
    ) -> list[SheetRow]:
        """Filter sheet rows to exclude Ventas that were generated by MAKE
        for the same payments being allocated (no id_pago_mp, split amounts).

        Only filters rows with id_pago_mp=None whose concepto+monto match
        a known split of a current payment. Rows with a real id_pago_mp are
        always kept — they are legitimately linked to a specific payment.
        """
        payment_ids = {cp.payment.id_pago_mp for cp in payments}

        # Build set of (concepto_key, monto) that current payments could split into
        payment_footprints: set[tuple[str, Decimal]] = set()
        insc = self.inscription_price
        cuota = self.cuota_price

        for cp in payments:
            monto = cp.payment.monto
            # Only consider combined payments (monto > single concept)
            is_combined = False
            if insc is not None and cuota is not None and monto == insc + cuota:
                is_combined = True
                payment_footprints.add(("inscripción", insc))
                payment_footprints.add(("cuota", cuota))
            if not is_combined and insc is not None and cuota is not None:
                for n in range(2, (self.total_cuotas or 0) + 1):
                    if monto == insc + cuota * n:
                        is_combined = True
                        payment_footprints.add(("inscripción", insc))
                        for _ in range(n):
                            payment_footprints.add(("cuota", cuota))
                        break

        if not payment_footprints:
            return sheet_rows

        remaining: list[SheetRow] = []
        consumed_inscripcion = False
        consumed_cuota_count = 0
        max_cuotas_to_consume = sum(1 for k, _ in payment_footprints if k == "cuota")

        for row in sheet_rows:
            # Keep all non-Venta rows
            if row.tipo_movimiento.strip().casefold() != "venta":
                remaining.append(row)
                continue

            # Keep rows that have a real id_pago_mp (legitimately linked)
            if row.id_pago_mp is not None and row.id_pago_mp in payment_ids:
                remaining.append(row)
                continue

            # Only filter rows without id_pago_mp (MAKE-generated)
            if row.id_pago_mp is not None:
                remaining.append(row)
                continue

            concepto_lower = row.concepto.strip().casefold()
            is_inscripcion = "inscripción" in concepto_lower or "inscripcion" in concepto_lower
            is_cuota = _extract_cuota_number(row.concepto) is not None

            if is_inscripcion and not consumed_inscripcion and ("inscripción", row.monto) in payment_footprints:
                consumed_inscripcion = True
                continue
            if is_cuota and consumed_cuota_count < max_cuotas_to_consume and ("cuota", row.monto) in payment_footprints:
                consumed_cuota_count += 1
                continue

            remaining.append(row)

        return remaining

    def _try_allocate_ignoring_ledger(
        self,
        cp: ConciliatedPayment,
        cobro_rows: list[SheetRow],
        ledger: Ledger | None = None,
        sheet_has_inscription: bool = False,
    ) -> list[Allocation] | None:
        """Allocate a conciliated payment using all known price combinations.

        Respects the ledger for inscription status (won't re-assign inscription
        if already paid) but tries broader combinations than _try_allocate.
        """
        monto = cp.payment.monto
        inscription_already_paid = ledger.inscription_paid if ledger else False
        next_cuota_n = (ledger.cuotas_paid + 1) if ledger else 1
        inscription_context = self._has_inscription_context(ledger or Ledger(), sheet_has_inscription)

        if not self._inscription_prices and not self._cuota_prices:
            return None

        concepts: list[tuple[str, Decimal]] = []
        has_cobro = self._has_persisted_conciliation(cp)

        # Short courses with no cuotas are effectively Pago Único
        if self.is_short_course_single_payment:
            for insc in self._inscription_prices:
                pago_unico_amount = self._single_concept_amount(monto, insc, None)
                if pago_unico_amount is not None:
                    concepts = [("Pago Único", pago_unico_amount)]
                    break

        # Try mixed-price cuota combinations (e.g. 1 full + 1 bonified)
        if not concepts and inscription_context and len(self._cuota_prices) >= 2:
            for i, cuota_a in enumerate(self._cuota_prices):
                for cuota_b in self._cuota_prices:
                    if monto == cuota_a + cuota_b:
                        concepts = [
                            (f"Cuota {next_cuota_n}", cuota_a),
                            (f"Cuota {next_cuota_n + 1}", cuota_b),
                        ]
                        break
                if concepts:
                    break

        if not concepts and not inscription_already_paid:
            # inscription + N cuotas using ANY price combination
            for insc in self._inscription_prices:
                for cuota in self._cuota_prices:
                    if monto == insc + cuota:
                        concepts = [("Inscripción", insc), (f"Cuota {next_cuota_n}", cuota)]
                        break
                    if self._is_within_percent_tolerance(monto, insc + cuota):
                        surplus = monto - insc
                        if self._is_within_percent_tolerance(surplus, cuota):
                            concepts = [("Inscripción", insc), (f"Cuota {next_cuota_n}", cuota)]
                            break
                    for n in range(2, (self.total_cuotas or 0) + 1):
                        if monto == insc + cuota * n:
                            concepts = [("Inscripción", insc)] + [(f"Cuota {next_cuota_n + i}", cuota) for i in range(n)]
                            break
                    if concepts:
                        break
                if concepts:
                    break

        if not concepts and not inscription_already_paid:
            for insc in self._inscription_prices:
                inscription_amount = self._single_concept_amount(monto, insc, None)
                if inscription_amount is not None:
                    concepts = [("Inscripción", inscription_amount)]
                    break

        if not concepts and inscription_context:
            for cuota in self._cuota_prices:
                cuota_amount = self._single_concept_amount(monto, cuota, None)
                if cuota_amount is not None:
                    concepts = [(f"Cuota {next_cuota_n}", cuota_amount)]
                    break

        if not concepts:
            return None

        # Emit the full allocations (Venta + Cobro) and let the reconciler
        # decide whether each row is already present, missing, or needs fixing.
        # Returning an empty list here caused valid reflected payments to
        # disappear from reconciliation and left their rows as EXTRA_ROW.
        allocations = [
            Allocation(
                payment=cp,
                concept=concept,
                amount=amount,
                generates_venta=True,
                generates_cobro=True,
            )
            for concept, amount in concepts
        ]

        return allocations if allocations else None

    def _remaining_cuotas_sum_raw(self) -> Decimal | None:
        """Total of ALL cuotas (not considering ledger)."""
        if self.cuota_price is None or self.total_cuotas <= 0:
            return None
        return self.cuota_price * self.total_cuotas

    def _try_allocate_bundle(
        self,
        payments: list[ConciliatedPayment],
        ledger: Ledger,
        sheet_has_inscription: bool = False,
    ) -> list[Allocation] | None:
        """Try deterministic allocation for consecutive complementary payments.

        Main targets:
        - 90_000 + 8_640 = 98_640 => one cuota completed in multiple payments
        - several consecutive partials summing exact inscription or exact cuota
        """
        if len(payments) < 2:
            return None

        if not self._can_bundle_together(payments):
            return None

        total = sum((cp.payment.monto for cp in payments), Decimal("0"))
        insc = self.inscription_price
        cuota = self.cuota_price
        inscription_context = self._has_inscription_context(ledger, sheet_has_inscription)

        if insc is not None and total == insc and not ledger.inscription_paid:
            return [self._make_alloc(cp, "Inscripción", cp.payment.monto, self._has_persisted_conciliation(cp)) for cp in payments]

        if cuota is not None and total == cuota and inscription_context:
            next_n = ledger.cuotas_paid + 1
            concept = f"Cuota {next_n}"
            return [self._make_alloc(cp, concept, cp.payment.monto, self._has_persisted_conciliation(cp)) for cp in payments]

        return None

    # ------------------------------------------------------------------
    # Candidate generation (for LLM)
    # ------------------------------------------------------------------

    def _generate_candidates(
        self, cp: ConciliatedPayment, ledger: Ledger, notes: str = ""
    ) -> list[AllocationCandidate]:
        """Produce scored candidates when deterministic allocation fails."""
        monto = cp.payment.monto
        candidates: list[AllocationCandidate] = []

        # Near-inscription across all known inscription prices
        if not ledger.inscription_paid:
            for insc in self._inscription_prices:
                ratio = float(monto / insc) if insc else 0.0
                if 0.90 <= ratio <= 1.10:
                    candidates.append(
                        AllocationCandidate(
                            concept="Inscripción",
                            amount=insc,
                            score=max(0.0, 1.0 - abs(1.0 - ratio) * 5),
                            reasoning=(
                                f"Monto {monto} is {ratio:.1%} of inscription "
                                f"price {insc}"
                            ),
                        )
                    )

        # Exact inscription even if ledger already shows a Venta.
        # This avoids falling back to Desconocido when MAKE has already created
        # the Venta row but the payment is still semantically an Inscripción.
        for insc in self._inscription_prices:
            if monto == insc:
                candidates.append(
                    AllocationCandidate(
                        concept="Inscripción",
                        amount=insc,
                        score=0.90,
                        reasoning=f"Monto matches exact inscription price {insc}",
                    )
                )

        # Near-cuota across all known cuota prices
        for cuota in self._cuota_prices:
            ratio = float(monto / cuota) if cuota else 0.0
            if 0.90 <= ratio <= 1.10:
                next_n = ledger.cuotas_paid + 1
                candidates.append(
                    AllocationCandidate(
                        concept=f"Cuota {next_n}",
                        amount=cuota,
                        score=max(0.0, 1.0 - abs(1.0 - ratio) * 5),
                        reasoning=(
                            f"Monto {monto} is {ratio:.1%} of cuota "
                            f"price {cuota}"
                        ),
                    )
                )

        for cuota in self._cuota_prices:
            if monto == cuota:
                next_n = ledger.cuotas_paid + 1
                candidates.append(
                    AllocationCandidate(
                        concept=f"Cuota {next_n}",
                        amount=cuota,
                        score=0.90,
                        reasoning=f"Monto matches exact cuota price {cuota}",
                    )
                )

        # Discounted inscription/c cuota even without explicit notes if DB concept supports it
        db_concepto = cp.payment.id_concepto_pago
        if db_concepto == 1 and not ledger.inscription_paid:
            for insc in self._inscription_prices:
                ratio = float(monto / insc) if insc else 0.0
                if 0.70 <= ratio < 1.0:
                    candidates.append(
                        AllocationCandidate(
                            concept="Inscripción",
                            amount=monto,
                            score=0.78,
                            reasoning=(
                                f"DB concept says Inscripción and monto {monto} is {ratio:.1%} "
                                f"of inscription price {insc}"
                            ),
                        )
                    )
        elif db_concepto in (2, 4):  # CUOTA or RECARGO (late cuota with surcharge)
            next_n = ledger.cuotas_paid + 1
            # Recargos can be up to ~40% over standard price, widen range
            max_ratio = 1.50 if db_concepto == 4 else 1.30
            for cuota in self._cuota_prices:
                ratio = float(monto / cuota) if cuota else 0.0
                if 0.70 <= ratio < max_ratio:
                    score = 0.80 if ratio < 1.0 else 0.78
                    candidates.append(
                        AllocationCandidate(
                            concept=f"Cuota {next_n}",
                            amount=monto,
                            score=score,
                            reasoning=(
                                f"DB concept says {'Recargo' if db_concepto == 4 else 'Cuota'} "
                                f"and monto {monto} is {ratio:.1%} of cuota price {cuota}"
                            ),
                        )
                    )

        # Discount/beca usually distorts cuotas downward.
        if self._has_any_hint(notes, _DISCOUNT_HINTS):
            next_n = ledger.cuotas_paid + 1
            for cuota in self._cuota_prices:
                if monto < cuota:
                    ratio = float(monto / cuota) if cuota else 0.0
                    if 0.50 <= ratio < 1.0:
                        candidates.append(
                            AllocationCandidate(
                                concept=f"Cuota {next_n}",
                                amount=monto,
                                score=0.82,
                                reasoning=(
                                    f"Observations mention discount/beca and monto {monto} is "
                                    f"{ratio:.1%} of cuota price {cuota}"
                                ),
                            )
                        )

        # Recargo / mora usually distorts cuotas upward.
        next_n = ledger.cuotas_paid + 1
        for cuota in self._cuota_prices:
            if monto > cuota:
                ratio = float(monto / cuota) if cuota else 0.0
                if 1.0 < ratio <= 1.30:
                    score = 0.80 if self._has_any_hint(notes, _SURCHARGE_HINTS) else 0.76
                    reason = (
                        f"Monto {monto} is {ratio:.1%} of cuota price {cuota}; "
                        f"likely cuota with recargo/mora"
                    )
                    candidates.append(
                        AllocationCandidate(
                            concept=f"Cuota {next_n}",
                            amount=monto,
                            score=score,
                            reasoning=reason,
                        )
                    )

        # Exact or near mixed combinations (inscripción + cuota(s)) as candidate hints
        for insc in self._inscription_prices:
            for cuota in self._cuota_prices:
                if monto == insc + cuota:
                    candidates.append(
                        AllocationCandidate(
                            concept="Inscripción",
                            amount=insc,
                            score=0.84,
                            reasoning=f"Monto matches exact split Inscripción {insc} + Cuota 1 {cuota}",
                        )
                    )
                for n in range(2, (self.total_cuotas or 0) + 1):
                    if monto == insc + cuota * n:
                        candidates.append(
                            AllocationCandidate(
                                concept="Inscripción",
                                amount=insc,
                                score=0.82,
                                reasoning=f"Monto matches split Inscripción {insc} + {n} cuotas of {cuota}",
                            )
                        )
                        break

        # Fallback: unknown
        if not candidates:
            candidates.append(
                AllocationCandidate(
                    concept="Desconocido",
                    amount=monto,
                    score=0.30,
                    reasoning="Cannot determine concept from amount or DB data",
                )
            )

        deduped: list[AllocationCandidate] = []
        seen: set[tuple[str, str]] = set()
        for candidate in sorted(candidates, key=lambda c: c.score, reverse=True):
            key = (candidate.concept, str(candidate.amount))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)

        return deduped

    # ------------------------------------------------------------------
    # Next-venta
    # ------------------------------------------------------------------

    def _compute_next_venta(
        self,
        ledger: Ledger,
        student: Student,
        reference_date: _date_type | None = None,
    ) -> ExpectedRow | None:
        """Generate ONE future Venta row — the next thing to pay.

        *reference_date* should be the date of the latest accepted payment
        for this student.  Falls back to the commission start date when no
        payments exist.
        """
        if ledger.fully_paid:
            return None
        if ledger.pago_unico and ledger.inscription_paid:
            return None

        if not ledger.inscription_paid and self.inscription_price is not None:
            concept = "Pago Único" if self.is_short_course_single_payment else "Inscripción"
            amount = self.inscription_price
        elif (
            self.cuota_price is not None
            and self.total_cuotas > 0
            and ledger.cuotas_paid < self.total_cuotas
            and not ledger.pago_unico
        ):
            next_n = ledger.cuotas_paid + 1
            concept = f"Cuota {next_n}"
            amount = self.cuota_price
        else:
            return None

        from src.rules.mappers import map_estado_administrativo

        if reference_date is not None:
            venta_date = reference_date
        elif self.commission.fecha_inicio is not None:
            fi = self.commission.fecha_inicio
            venta_date = fi.date() if hasattr(fi, 'date') else fi
        else:
            from datetime import date as date_cls
            venta_date = date_cls.today()

        return ExpectedRow(
            comision=self.commission.nombre.strip(),
            fecha_movimiento=venta_date,
            tipo_movimiento="Venta",
            dni=student.dni,
            concepto=concept,
            monto=amount,
            medio_pago="No aplica",
            estudiante=f"{student.apellidos},{student.nombres}",
            estado_administrativo=map_estado_administrativo(student.id_estado_administrativo),
            id_movimiento_bancario=None,
            id_pago_mp=None,
            source_payment=None,
            source_movement=None,
        )

    # ------------------------------------------------------------------
    # Post-resolution rebuild
    # ------------------------------------------------------------------

    def renumber_allocations(
        self,
        allocations: list[Allocation],
        student: Student,
        initial_ledger: Ledger | None = None,
    ) -> tuple[list[Allocation], ExpectedRow | None]:
        """Rebuild cuota ordinals and next_venta from final accepted allocations.

        Called by the pipeline AFTER all ambiguous payments have been resolved
        (via LLM or manual review).  This is the "Pass 2" that produces
        canonical, chronologically-correct cuota numbering.

        When *initial_ledger* is provided (e.g. built from pre-cutoff sheet
        rows), cuota numbering continues from the ledger state instead of
        starting at 1.  This prevents re-numbering Cuota 4 as Cuota 1 when
        earlier cuotas were skipped via cutoff.

        Returns (renumbered_allocations, new_next_venta).
        """
        # Sort by payment date so cuotas are numbered chronologically
        sorted_allocs = sorted(
            allocations,
            key=lambda a: (
                a.payment.payment.fecha or _date_type.min
            ),
        )

        ledger = Ledger(
            inscription_paid=initial_ledger.inscription_paid,
            cuotas_paid=initial_ledger.cuotas_paid,
            pago_unico=initial_ledger.pago_unico,
            fully_paid=initial_ledger.fully_paid,
            existing_concepts=set(initial_ledger.existing_concepts),
        ) if initial_ledger is not None else Ledger()
        renumbered: list[Allocation] = []

        for alloc in sorted_allocs:
            cuota_n = _extract_cuota_number(alloc.concept)
            if cuota_n is not None:
                # Reassign the cuota number based on fresh ledger state
                new_n = ledger.cuotas_paid + 1
                new_concept = f"Cuota {new_n}"
                alloc = Allocation(
                    payment=alloc.payment,
                    concept=new_concept,
                    amount=alloc.amount,
                    generates_venta=alloc.generates_venta,
                    generates_cobro=alloc.generates_cobro,
                )
            renumbered.append(alloc)
            self._update_ledger(ledger, alloc)

        # Recompute next_venta with the correct ledger and reference date
        latest_date: _date_type | None = None
        for alloc in reversed(sorted_allocs):
            if alloc.payment.payment.fecha is not None:
                fecha = alloc.payment.payment.fecha
                latest_date = fecha.date() if hasattr(fecha, 'date') else fecha
                break

        next_venta = self._compute_next_venta(
            ledger, student, reference_date=latest_date
        )

        return renumbered, next_venta

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _remaining_cuotas_sum(self, ledger: Ledger) -> Decimal | None:
        """Sum of unpaid cuotas."""
        if self.cuota_price is None or self.total_cuotas <= 0:
            return None
        remaining = self.total_cuotas - ledger.cuotas_paid
        if remaining <= 0:
            return None
        return self.cuota_price * remaining

    @staticmethod
    def _can_bundle_together(payments: list[ConciliatedPayment]) -> bool:
        """Whether consecutive payments are close enough to be treated as a bundle."""
        if len(payments) < 2:
            return False
        personas = {cp.payment.id_persona for cp in payments}
        if len(personas) != 1:
            return False
        first_date = payments[0].payment.fecha.date()
        last_date = payments[-1].payment.fecha.date()
        return abs((last_date - first_date).days) <= 31

    @staticmethod
    def _has_any_hint(text: str, hints: tuple[str, ...]) -> bool:
        lowered = (text or "").casefold()
        return any(hint in lowered for hint in hints)

    @staticmethod
    def _student_notes(student: Student) -> str:
        return " | ".join(filter(None, [student.persona_observaciones, student.comision_observaciones]))

    @staticmethod
    def _sheet_has_inscription(rows: list[SheetRow]) -> bool:
        for row in rows:
            if row.tipo_movimiento.strip().casefold() != "venta":
                continue
            concepto = row.concepto.strip().casefold()
            if "inscripción" in concepto or "inscripcion" in concepto:
                return True
        return False

    def _has_inscription_context(self, ledger: Ledger, sheet_has_inscription: bool) -> bool:
        """Whether cuota-only allocations are safe.

        If a commission normally requires an inscription and we don't have any
        evidence of that inscription in the current student state, allocating a
        cuota-only payment is risky: many historical bad rows came from the
        engine putting the inscription amount (or inscription+cuota total) into
        Cuota 1 / Cuota 2 directly.

        In those cases we prefer to leave the payment ambiguous unless we can
        deterministically split/assign an inscription.
        """
        if self.inscription_price is None:
            return True
        if self.is_short_course_single_payment:
            return True
        return ledger.inscription_paid or sheet_has_inscription

    @staticmethod
    def _is_within_percent_tolerance(monto: Decimal, target: Decimal) -> bool:
        if target == 0:
            return False
        return abs(monto - target) / target <= _SINGLE_CONCEPT_TOLERANCE

    @staticmethod
    def _is_competing_single_match(monto: Decimal, other_target: Decimal | None) -> bool:
        if other_target is None:
            return False
        return AllocationEngine._is_within_percent_tolerance(monto, other_target)

    @staticmethod
    def _single_concept_amount(
        monto: Decimal,
        target: Decimal | None,
        competing_target: Decimal | None,
    ) -> Decimal | None:
        if target is None:
            return None
        if monto == target:
            return target
        if (
            AllocationEngine._is_within_percent_tolerance(monto, target)
            and not AllocationEngine._is_competing_single_match(monto, competing_target)
        ):
            return monto
        return None

    @staticmethod
    def _has_persisted_conciliation(cp: ConciliatedPayment) -> bool:
        movement_id = cp.payment.id_movimiento_bancario
        return movement_id is not None and movement_id > 0

    @staticmethod
    def _make_alloc(
        cp: ConciliatedPayment,
        concept: str,
        amount: Decimal,
        has_cobro: bool,
    ) -> Allocation:
        return Allocation(
            payment=cp,
            concept=concept,
            amount=amount,
            generates_venta=True,
            generates_cobro=has_cobro,
        )

    @staticmethod
    def _update_ledger(ledger: Ledger, alloc: Allocation) -> None:
        """Update ledger after an allocation is committed."""
        ledger.existing_concepts.add(alloc.concept)

        if "Inscripción" in alloc.concept or "Inscripcion" in alloc.concept:
            ledger.inscription_paid = True

        cuota_n = _extract_cuota_number(alloc.concept)
        if cuota_n is not None and cuota_n > ledger.cuotas_paid:
            ledger.cuotas_paid = cuota_n

        if "Pago Único" in alloc.concept or "Pago Unico" in alloc.concept:
            ledger.pago_unico = True
            if ledger.inscription_paid:
                ledger.fully_paid = True
