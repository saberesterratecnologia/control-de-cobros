"""Build expected sheet rows from conciliated payments."""

from __future__ import annotations

from decimal import Decimal

from src.models.sheet import ExpectedRow
from src.models.source import BankMovement, Commission, Payment, Student
from src.rules.mappers import map_concept, map_medio


class VentaCobroBuilder:
    def __init__(self, commission: Commission):
        self.commission = commission

    def build_expected_rows(
        self,
        payment: Payment,
        movement: BankMovement | None,
        student: Student,
        cuota_number: int | None = None,
    ) -> list[ExpectedRow]:
        rows: list[ExpectedRow] = []

        concepts_amounts = self.detect_combined_payment(payment, self.commission)
        if not concepts_amounts:
            concept_name = map_concept(payment.id_concepto_pago or 0, cuota_number)
            concepts_amounts = [(concept_name, payment.monto)]

        for concept_name, amount in concepts_amounts:
            if concept_name == "Cuota" and cuota_number is not None:
                concept_name = f"Cuota {cuota_number}"

            rows.append(
                ExpectedRow(
                    comision=self.commission.nombre,
                    fecha_movimiento=payment.fecha.date(),
                    tipo_movimiento="Venta",
                    dni=student.dni,
                    concepto=concept_name,
                    monto=amount,
                    medio_pago="No aplica",
                    estudiante=f"{student.apellidos} {student.nombres}",
                    id_movimiento_bancario=None,
                    id_pago_mp=payment.id_pago_mp,
                    source_payment=payment,
                    source_movement=movement,
                )
            )

            if movement is not None:
                rows.append(
                    ExpectedRow(
                        comision=self.commission.nombre,
                        fecha_movimiento=movement.fecha,
                        tipo_movimiento="Cobro",
                        dni=student.dni,
                        concepto=concept_name,
                        monto=amount,
                        medio_pago=map_medio(payment.id_medio_pago or 0),
                        estudiante=f"{student.apellidos} {student.nombres}",
                        id_movimiento_bancario=movement.id_movimiento,
                        id_pago_mp=payment.id_pago_mp,
                        source_payment=payment,
                        source_movement=movement,
                    )
                )

        return rows

    def detect_combined_payment(
        self, payment: Payment, commission: Commission
    ) -> list[tuple[str, Decimal]]:
        inscripcion = commission.valor_inscripcion_promocion
        cuota = commission.valor_cuota_bonificada

        if inscripcion is None or cuota is None:
            return []

        if payment.monto == inscripcion:
            return [("Inscripción", inscripcion)]
        if payment.monto == cuota:
            return [("Cuota", cuota)]
        if payment.monto == inscripcion + cuota:
            return [("Inscripción", inscripcion), ("Cuota", cuota)]

        return []

    def determine_cuota_number(self, student: Student, existing_payments: list[Payment]) -> int:
        previous_cuotas = [
            payment
            for payment in existing_payments
            if payment.id_persona == student.id_persona and payment.id_concepto_pago == 2
        ]
        return len(previous_cuotas) + 1
