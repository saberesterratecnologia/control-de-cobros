"""Models representing Google Sheet rows and expected rows."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.models.source import BankMovement, Payment


class SheetRow(BaseModel):
    """Represents one row in the COBROS sheet."""

    model_config = ConfigDict(extra="forbid")

    row_number: int
    organizacion: str | None
    curso: str | None
    comision: str | None
    fecha_movimiento: date | None
    tipo_movimiento: str
    dni: str
    concepto: str
    monto: Decimal
    medio_pago: str
    estudiante: str | None
    estado_administrativo: str | None
    estado_deuda: str | None
    id_movimiento_bancario: int | None
    id_pago_mp: int | None


class ExpectedRow(BaseModel):
    """Row that SHOULD exist in the sheet based on DB data."""

    model_config = ConfigDict(extra="forbid")

    comision: str
    fecha_movimiento: date
    tipo_movimiento: str
    dni: str
    concepto: str
    monto: Decimal
    medio_pago: str
    estudiante: str
    estado_administrativo: str | None = None
    id_movimiento_bancario: int | None = None
    id_pago_mp: int | None = None
    source_payment: Payment | None = None
    source_movement: BankMovement | None = None
