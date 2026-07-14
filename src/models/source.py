"""Source-of-truth models coming from SQL Server."""

from __future__ import annotations

import json
from typing import Any
from datetime import date, datetime
from decimal import Decimal

import re

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class Payment(BaseModel):
    """From PAGO_MERCADO_PAGO."""

    model_config = ConfigDict(extra="forbid")

    id_pago_mp: int
    fecha: datetime
    monto: Decimal
    nro_operacion: str | None
    id_persona: int | None
    id_medio_pago: int | None
    fecha_carga: datetime | None
    controlado: bool
    comentario_cliente: str | None
    id_concepto_pago: int | None
    id_movimiento_bancario: int | None
    id_organizacion: int | None = None
    razon_social_originante: str | None
    dni_cuit_originante: str | None
    controlado_auto: bool
    estado_conciliacion_auto: str | None


class BankMovement(BaseModel):
    """From MOVIMIENTO_BANCARIO."""

    model_config = ConfigDict(extra="forbid")

    id_movimiento: int
    id_cuenta_bancaria: int
    id_persona: int | None
    fecha: date
    referencia: str | None
    causal: str | None
    concepto: str | None
    importe: Decimal
    conciliado: bool
    json_identificacion: dict[str, Any] | None = None

    @field_validator("json_identificacion", mode="before")
    @classmethod
    def _parse_json_identificacion(cls, value: Any) -> dict[str, Any] | None:
        if value in (None, ""):
            return None
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
        return None


class Commission(BaseModel):
    """From COMISIONES."""

    model_config = ConfigDict(extra="forbid")

    id_comision: int
    id_curso: int
    id_organizacion: int
    nombre: str
    valor_inscripcion: Decimal | None = None
    valor_inscripcion_promocion: Decimal | None
    valor_cuota: Decimal | None = None
    valor_cuota_bonificada: Decimal | None
    valor_pago_unico: Decimal | None = None
    cantidad_cuotas: int | None
    duracion_meses: int | None = None
    fecha_inicio: date | None
    borrado: bool
    analisis_pagos: bool = True


class Student(BaseModel):
    """From PERSONAS joined with COMISIONES_PERSONAS."""

    model_config = ConfigDict(extra="forbid")

    id_persona: int
    nombres: str
    apellidos: str
    apellidos_nombres: str | None
    dni: str
    dni_original: str | None = None

    @field_validator("dni", mode="before")
    @classmethod
    def _normalize_dni(cls, value: Any) -> str:
        if not isinstance(value, str):
            return str(value) if value is not None else ""
        stripped = value.strip()
        digits = re.sub(r"\D", "", stripped)
        if len(digits) == 11:
            return digits[2:10]
        return stripped

    @model_validator(mode="before")
    @classmethod
    def _preserve_original_dni(cls, data: Any) -> Any:
        if isinstance(data, dict) and "dni" in data and "dni_original" not in data:
            data["dni_original"] = str(data["dni"]).strip() if data["dni"] is not None else None
        return data
    email: str | None
    id_estado_academico: int | None
    id_estado_administrativo: int | None
    eliminado: bool
    analisis_pagos: bool = True
    persona_observaciones: str | None = None
    comision_observaciones: str | None = None
    fecha_hora_inscripcion: datetime | None = None


class PaymentConcept(BaseModel):
    """From PAGO_CONCEPTO."""

    model_config = ConfigDict(extra="forbid")

    id_concepto_pago: int
    nombre: str


class PaymentMethod(BaseModel):
    """From MEDIO_PAGO."""

    model_config = ConfigDict(extra="forbid")

    id_medio_pago: int
    nombre: str


