"""Conciliation logic for matching unconciliated payments with bank movements."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from src.models.pipeline import ConciliatedPayment
from src.models.source import BankMovement, Payment

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistableConciliationMatch:
    payment: Payment
    movement: BankMovement | None
    status: str
    matched_by: str | None = None
    candidate_ids: tuple[int, ...] = ()


class Conciliator:
    """Attempts to match unconciliated payments with available bank movements.

    It still does not write by itself, but now produces both local
    `ConciliatedPayment` objects for allocation and persistable match
    candidates for Phase 0 write-back.
    """

    def try_conciliate(
        self,
        payment: Payment,
        available_movements: list[BankMovement],
    ) -> ConciliatedPayment:
        """Try to find a matching movement for an unconciliated payment.

        Rules (``logica-conciliacion-pagos.md`` §5.2):
        1. Movement must have ``conciliado = False``.
        2. ``importe == monto``.
        3. AND at least one of:
           a. Exact date match.
           b. Same month **and** year.
           c. ``nro_operacion == referencia`` (both non-empty).

        * Exactly 1 match → ``conciliated_by="auto"``.
        * 0 or >1 matches → ``conciliated_by="unconciliated"``.
        """
        match = self.build_persistable_match(payment, available_movements)

        if match.status == "matched" and match.movement is not None:
            LOGGER.info(
                "Auto-conciliated payment %s with movement %s",
                payment.id_pago_mp,
                match.movement.id_movimiento,
            )
            return ConciliatedPayment(
                payment=payment,
                movement=match.movement,
                conciliated_by="auto",
            )

        if match.status == "multiple":
            LOGGER.warning(
                "Multiple movement candidates for payment %s: %s",
                payment.id_pago_mp,
                list(match.candidate_ids),
            )

        return ConciliatedPayment(
            payment=payment, movement=None, conciliated_by="unconciliated"
        )

    def build_persistable_match(
        self,
        payment: Payment,
        available_movements: list[BankMovement],
    ) -> PersistableConciliationMatch:
        candidates = self._candidate_movements(payment, available_movements)
        if len(candidates) == 1:
            matched_by = self._match_reason(payment, candidates[0])
            return PersistableConciliationMatch(
                payment=payment,
                movement=candidates[0],
                status="matched",
                matched_by=matched_by,
                candidate_ids=(candidates[0].id_movimiento,),
            )
        if len(candidates) > 1:
            return PersistableConciliationMatch(
                payment=payment,
                movement=None,
                status="multiple",
                candidate_ids=tuple(candidate.id_movimiento for candidate in candidates),
            )
        return PersistableConciliationMatch(
            payment=payment,
            movement=None,
            status="none",
        )

    def build_persistable_matches(
        self,
        all_payments: list[Payment],
        available_movements: list[BankMovement],
    ) -> list[PersistableConciliationMatch]:
        result: list[PersistableConciliationMatch] = []
        used_movement_ids: set[int] = set()

        for payment in all_payments:
            if payment.id_movimiento_bancario is not None and payment.id_movimiento_bancario > 0:
                result.append(
                    PersistableConciliationMatch(
                        payment=payment,
                        movement=None,
                        status="existing",
                        candidate_ids=(payment.id_movimiento_bancario,),
                    )
                )
                continue

            remaining = [
                movement
                for movement in available_movements
                if movement.id_movimiento not in used_movement_ids
            ]
            match = self.build_persistable_match(payment, remaining)
            if match.status == "matched" and match.movement is not None:
                used_movement_ids.add(match.movement.id_movimiento)
            result.append(match)

        return result

    def build_conciliated_list(
        self,
        all_payments: list[Payment],
        available_movements: list[BankMovement],
    ) -> list[ConciliatedPayment]:
        """Build the complete list of :class:`ConciliatedPayment` objects.

        1. Payments already conciliated in DB (``id_movimiento_bancario > 0``)
           are marked ``conciliated_by="existing"`` with ``movement=None``
           (the pipeline injects the real movement later from
           ``get_conciliated_payments``).
        2. Unconciliated payments are run through :meth:`try_conciliate`.
        3. Movements consumed by auto-conciliation are excluded from
           subsequent attempts to avoid double-matching.
        """
        result: list[ConciliatedPayment] = []
        for match in self.build_persistable_matches(all_payments, available_movements):
            if match.status == "existing":
                result.append(
                    ConciliatedPayment(
                        payment=match.payment,
                        movement=None,
                        conciliated_by="existing",
                    )
                )
                continue

            if match.status == "matched":
                result.append(
                    ConciliatedPayment(
                        payment=match.payment,
                        movement=match.movement,
                        conciliated_by="auto",
                    )
                )
                continue

            result.append(
                ConciliatedPayment(
                    payment=match.payment,
                    movement=None,
                    conciliated_by="unconciliated",
                )
            )

        return result

    @staticmethod
    def _candidate_movements(payment: Payment, available_movements: list[BankMovement]) -> list[BankMovement]:
        candidates: list[BankMovement] = []
        for mov in available_movements:
            if mov.conciliado:
                continue
            if mov.importe != payment.monto:
                continue
            if Conciliator._match_reason(payment, mov) is not None:
                candidates.append(mov)
        return candidates

    @staticmethod
    def _match_reason(payment: Payment, movement: BankMovement) -> str | None:
        fecha_match = payment.fecha.date() == movement.fecha if payment.fecha else False
        if fecha_match:
            return "exact_date"

        month_match = (
            payment.fecha.month == movement.fecha.month
            and payment.fecha.year == movement.fecha.year
        ) if payment.fecha else False
        if month_match:
            return "same_month"

        ref_match = bool(
            payment.nro_operacion
            and movement.referencia
            and payment.nro_operacion.strip() == movement.referencia.strip()
        )
        if ref_match:
            return "reference"
        return None
