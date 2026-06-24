"""SQL Server connector for reconciliation source data."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pyodbc

from src.models.source import (
    BankMovement,
    Commission,
    Payment,
    PaymentConcept,
    PaymentMethod,
    Student,
)

LOGGER = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    """Raised when connector calls fail."""


class SQLServerConnector:
    """SQL Server access for reconciliation reads and controlled write-back."""

    _PAYMENT_COLUMNS = [
        "id_pago_mp",
        "fecha",
        "monto",
        "nro_operacion",
        "id_persona",
        "id_medio_pago",
        "fecha_carga",
        "controlado",
        "comentario_cliente",
        "id_concepto_pago",
        "id_movimiento_bancario",
        "id_organizacion",
        "razon_social_originante",
        "dni_cuit_originante",
        "controlado_auto",
        "estado_conciliacion_auto",
    ]

    _MOVEMENT_COLUMNS = [
        "id_movimiento",
        "id_cuenta_bancaria",
        "id_persona",
        "fecha",
        "referencia",
        "causal",
        "concepto",
        "importe",
        "conciliado",
        "json_identificacion",
    ]



    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.connection: pyodbc.Connection | None = None
        self.connection_string = self._build_connection_string(config)

    @staticmethod
    def _build_connection_string(config: dict[str, Any]) -> str:
        driver = config.get("driver", "ODBC Driver 17 for SQL Server")
        server = config.get("server", "")
        database = config.get("database", "")
        trusted = bool(config.get("trusted_connection", True))
        trust_cert = bool(config.get("trust_server_certificate", True))

        parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
        if trusted:
            parts.append("Trusted_Connection=yes")
        else:
            user = config.get("username", "")
            password = config.get("password", "")
            parts.append(f"UID={user}")
            # Wrap password in braces to escape special chars like ; and }
            escaped_pwd = password.replace("}", "}}")
            parts.append(f"PWD={{{escaped_pwd}}}")
        if trust_cert:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts)

    def connect(self) -> "SQLServerConnector":
        try:
            self.connection = pyodbc.connect(self.connection_string, autocommit=False)
            return self
        except pyodbc.Error as error:
            LOGGER.exception("sql server connection failed")
            raise ConnectorError("Unable to connect to SQL Server") from error

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "SQLServerConnector":
        return self.connect()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _cursor(self) -> pyodbc.Cursor:
        if self.connection is None:
            raise ConnectorError("SQLServerConnector is not connected")
        try:
            # Test if connection is alive
            self.connection.execute("SELECT 1")
        except (pyodbc.Error, pyodbc.ProgrammingError):
            LOGGER.warning("SQL connection lost, reconnecting...")
            try:
                self.connection = pyodbc.connect(self.connection_string, autocommit=False)
            except pyodbc.Error as error:
                raise ConnectorError("Unable to reconnect to SQL Server") from error
        return self.connection.cursor()

    @staticmethod
    def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
        return {column: row[index] for index, column in enumerate(columns)}

    def _payment_from_row(self, row: Any, offset: int = 0) -> Payment:
        data = self._row_to_dict(row[offset : offset + len(self._PAYMENT_COLUMNS)], self._PAYMENT_COLUMNS)
        if data.get("monto") is not None:
            data["monto"] = Decimal(data["monto"])
        return Payment.model_validate(data)

    def _movement_from_row(self, row: Any, offset: int = 0) -> BankMovement:
        data = self._row_to_dict(row[offset : offset + len(self._MOVEMENT_COLUMNS)], self._MOVEMENT_COLUMNS)
        if data.get("importe") is not None:
            data["importe"] = Decimal(data["importe"])
        return BankMovement.model_validate(data)

    def get_commissions(self, id_curso: int) -> list[Commission]:
        query = """
            SELECT
                c.id_comision,
                c.id_curso,
                c.id_organizacion,
                c.nombre,
                c.valor_inscripcion,
                c.valor_inscripcion_promocion,
                c.valor_cuota,
                c.valor_cuota_bonificada,
                c.cantidad_cuotas,
                c.duracion_meses,
                c.fecha_inicio,
                c.borrado,
                c.analisis_pagos
            FROM COMISIONES c
            WHERE c.id_curso = ? AND c.borrado = 0
              AND c.analisis_pagos = 1
            ORDER BY c.nombre
        """
        cursor = self._cursor()
        rows = cursor.execute(query, (id_curso,)).fetchall()
        columns = [
            "id_comision",
            "id_curso",
            "id_organizacion",
            "nombre",
            "valor_inscripcion",
            "valor_inscripcion_promocion",
            "valor_cuota",
            "valor_cuota_bonificada",
            "cantidad_cuotas",
            "duracion_meses",
            "fecha_inicio",
            "borrado",
            "analisis_pagos",
        ]
        return [Commission.model_validate(self._row_to_dict(row, columns)) for row in rows]

    def get_active_commissions(self, year: int, id_organizacion: int = 2) -> list[Commission]:
        """Get active commissions filtered by organization and year."""
        query = """
            SELECT
                c.id_comision,
                c.id_curso,
                c.id_organizacion,
                c.nombre,
                c.valor_inscripcion,
                c.valor_inscripcion_promocion,
                c.valor_cuota,
                c.valor_cuota_bonificada,
                c.cantidad_cuotas,
                c.duracion_meses,
                c.fecha_inicio,
                c.borrado,
                c.analisis_pagos
            FROM COMISIONES c
            WHERE c.id_organizacion = ?
              AND YEAR(c.fecha_inicio) = ?
              AND c.borrado = 0
              AND c.analisis_pagos = 1
            ORDER BY c.nombre
        """
        cursor = self._cursor()
        rows = cursor.execute(query, (id_organizacion, year)).fetchall()
        columns = [
            "id_comision",
            "id_curso",
            "id_organizacion",
            "nombre",
            "valor_inscripcion",
            "valor_inscripcion_promocion",
            "valor_cuota",
            "valor_cuota_bonificada",
            "cantidad_cuotas",
            "duracion_meses",
            "fecha_inicio",
            "borrado",
            "analisis_pagos",
        ]
        return [Commission.model_validate(self._row_to_dict(row, columns)) for row in rows]

    def get_students(self, id_comision: int) -> list[Student]:
        query = """
            SELECT
                p.id_persona,
                p.nombres,
                p.apellidos,
                p.apellidos_nombres,
                p.dni,
                p.email,
                cp.id_estado_academico,
                cp.id_estado_administrativo,
                cp.eliminado,
                p.observaciones,
                cp.observaciones,
                cp.fechaHora_inscripcion
            FROM COMISIONES_PERSONAS cp
            INNER JOIN PERSONAS p ON p.id_persona = cp.id_persona
            WHERE cp.id_comision = ?
              AND cp.eliminado = 0
              AND p.borrada = 0
            ORDER BY p.apellidos, p.nombres
        """
        cursor = self._cursor()
        rows = cursor.execute(query, (id_comision,)).fetchall()
        columns = [
            "id_persona",
            "nombres",
            "apellidos",
            "apellidos_nombres",
            "dni",
            "email",
            "id_estado_academico",
            "id_estado_administrativo",
            "eliminado",
            "persona_observaciones",
            "comision_observaciones",
            "fecha_hora_inscripcion",
        ]
        return [Student.model_validate(self._row_to_dict(row, columns)) for row in rows]

    def get_active_commissions_for_student(
        self,
        id_persona: int,
        year: int,
        id_organizacion: int = 2,
    ) -> list[Commission]:
        query = """
            SELECT
                c.id_comision,
                c.id_curso,
                c.id_organizacion,
                c.nombre,
                c.valor_inscripcion,
                c.valor_inscripcion_promocion,
                c.valor_cuota,
                c.valor_cuota_bonificada,
                c.cantidad_cuotas,
                c.duracion_meses,
                c.fecha_inicio,
                c.borrado,
                c.analisis_pagos
            FROM COMISIONES_PERSONAS cp
            INNER JOIN COMISIONES c ON c.id_comision = cp.id_comision
            WHERE cp.id_persona = ?
              AND cp.eliminado = 0
              AND c.borrado = 0
              AND c.id_organizacion = ?
              AND YEAR(c.fecha_inicio) = ?
              AND c.analisis_pagos = 1
            ORDER BY c.fecha_inicio, c.nombre
        """
        cursor = self._cursor()
        rows = cursor.execute(query, (id_persona, id_organizacion, year)).fetchall()
        columns = [
            "id_comision",
            "id_curso",
            "id_organizacion",
            "nombre",
            "valor_inscripcion",
            "valor_inscripcion_promocion",
            "valor_cuota",
            "valor_cuota_bonificada",
            "cantidad_cuotas",
            "duracion_meses",
            "fecha_inicio",
            "borrado",
            "analisis_pagos",
        ]
        return [Commission.model_validate(self._row_to_dict(row, columns)) for row in rows]

    def get_payments(
        self,
        id_persona: int,
        year: int | None = None,
        id_organizacion: int | None = None,
    ) -> list[Payment]:
        query = """
            SELECT
                p.id_pago_mp,
                p.fecha,
                p.monto,
                p.nro_operacion,
                p.id_persona,
                p.id_medio_pago,
                p.fecha_carga,
                p.controlado,
                p.comentario_cliente,
                p.id_concepto_pago,
                p.id_movimiento_bancario,
                p.id_organizacion,
                p.razon_social_originante,
                p.dni_cuit_originante,
                p.controlado_auto,
                p.estado_conciliacion_auto
            FROM PAGO_MERCADO_PAGO p
            WHERE p.id_persona = ?
        """
        params: list[Any] = [id_persona]
        if year is not None:
            query += "\n              AND YEAR(p.fecha) = ?"
            params.append(year)
        if id_organizacion is not None:
            query += "\n              AND (p.id_organizacion = ? OR p.id_organizacion IS NULL)"
            params.append(id_organizacion)
        query += "\n            ORDER BY p.fecha DESC"
        cursor = self._cursor()
        rows = cursor.execute(query, tuple(params)).fetchall()
        return [self._payment_from_row(row) for row in rows]

    def get_all_payments(
        self,
        id_persona: int,
        year: int | None = None,
        id_organizacion: int | None = None,
    ) -> list[Payment]:
        """Get all payments for a person, including conciliated and unconciliated."""
        return self.get_payments(id_persona, year=year, id_organizacion=id_organizacion)

    def get_available_movements(self, id_persona: int, year: int | None = None) -> list[BankMovement]:
        """Get bank movements for a person that are not yet conciliated."""
        query = """
            SELECT
                m.id_movimiento,
                m.id_cuenta_bancaria,
                m.id_persona,
                m.fecha,
                m.referencia,
                m.causal,
                m.concepto,
                m.importe,
                m.conciliado,
                m.json_identificacion
            FROM MOVIMIENTO_BANCARIO m
            WHERE m.id_persona = ?
              AND m.conciliado = 0
        """
        params: list[Any] = [id_persona]
        if year is not None:
            query += "\n              AND YEAR(m.fecha) = ?"
            params.append(year)
        query += "\n            ORDER BY m.fecha DESC"
        cursor = self._cursor()
        rows = cursor.execute(query, tuple(params)).fetchall()
        return [self._movement_from_row(row) for row in rows]

    def get_conciliated_payments(
        self,
        id_persona: int,
        year: int | None = None,
        id_organizacion: int | None = None,
    ) -> list[tuple[Payment, BankMovement]]:
        query = """
            SELECT
                p.id_pago_mp,
                p.fecha,
                p.monto,
                p.nro_operacion,
                p.id_persona,
                p.id_medio_pago,
                p.fecha_carga,
                p.controlado,
                p.comentario_cliente,
                p.id_concepto_pago,
                p.id_movimiento_bancario,
                p.id_organizacion,
                p.razon_social_originante,
                p.dni_cuit_originante,
                p.controlado_auto,
                p.estado_conciliacion_auto,
                m.id_movimiento,
                m.id_cuenta_bancaria,
                m.id_persona,
                m.fecha,
                m.referencia,
                m.causal,
                m.concepto,
                m.importe,
                m.conciliado,
                m.json_identificacion
            FROM PAGO_MERCADO_PAGO p
            INNER JOIN MOVIMIENTO_BANCARIO m ON m.id_movimiento = p.id_movimiento_bancario
            WHERE p.id_persona = ?
              AND p.id_movimiento_bancario > 0
              AND m.conciliado = 1
        """
        params: list[Any] = [id_persona]
        if year is not None:
            query += "\n              AND YEAR(p.fecha) = ?"
            params.append(year)
        if id_organizacion is not None:
            query += "\n              AND (p.id_organizacion = ? OR p.id_organizacion IS NULL)"
            params.append(id_organizacion)
        query += "\n            ORDER BY p.fecha DESC"
        cursor = self._cursor()
        rows = cursor.execute(query, tuple(params)).fetchall()
        pairs: list[tuple[Payment, BankMovement]] = []
        for row in rows:
            pairs.append((self._payment_from_row(row, 0), self._movement_from_row(row, len(self._PAYMENT_COLUMNS))))
        return pairs

    def get_bank_movement(self, id_movimiento: int) -> BankMovement | None:
        query = """
            SELECT
                m.id_movimiento,
                m.id_cuenta_bancaria,
                m.id_persona,
                m.fecha,
                m.referencia,
                m.causal,
                m.concepto,
                m.importe,
                m.conciliado,
                m.json_identificacion
            FROM MOVIMIENTO_BANCARIO m
            WHERE m.id_movimiento = ?
        """
        cursor = self._cursor()
        row = cursor.execute(query, (id_movimiento,)).fetchone()
        if row is None:
            return None
        return self._movement_from_row(row)

    def get_unconciliated_payments(
        self,
        year: int | None = None,
        id_organizacion: int | None = None,
    ) -> list[Payment]:
        query = """
            SELECT
                p.id_pago_mp,
                p.fecha,
                p.monto,
                p.nro_operacion,
                p.id_persona,
                p.id_medio_pago,
                p.fecha_carga,
                p.controlado,
                p.comentario_cliente,
                p.id_concepto_pago,
                p.id_movimiento_bancario,
                p.id_organizacion,
                p.razon_social_originante,
                p.dni_cuit_originante,
                p.controlado_auto,
                p.estado_conciliacion_auto
            FROM PAGO_MERCADO_PAGO p
            WHERE p.id_persona IS NOT NULL
              AND (p.id_movimiento_bancario IS NULL OR p.id_movimiento_bancario <= 0)
        """
        params: list[Any] = []
        if year is not None:
            query += "\n              AND YEAR(p.fecha) = ?"
            params.append(year)
        if id_organizacion is not None:
            query += "\n              AND (p.id_organizacion = ? OR p.id_organizacion IS NULL)"
            params.append(id_organizacion)
        query += "\n            ORDER BY p.fecha DESC, p.id_pago_mp DESC"
        cursor = self._cursor()
        rows = cursor.execute(query, tuple(params)).fetchall()
        return [self._payment_from_row(row) for row in rows]

    def update_payment_conciliation(
        self,
        id_pago_mp: int,
        id_movimiento_bancario: int,
        *,
        commit: bool = True,
    ) -> bool:
        cursor = self._cursor()
        cursor.execute(
            "SELECT id_movimiento_bancario FROM PAGO_MERCADO_PAGO WHERE id_pago_mp = ?",
            (id_pago_mp,),
        )
        current = cursor.fetchone()
        if current is None:
            raise ConnectorError(f"Payment {id_pago_mp} not found")

        if current[0] == id_movimiento_bancario:
            return False
        if current[0] is not None and current[0] > 0 and current[0] != id_movimiento_bancario:
            raise ConnectorError(
                f"Payment {id_pago_mp} already conciliated with movement {current[0]}"
            )

        cursor.execute(
            "UPDATE PAGO_MERCADO_PAGO SET id_movimiento_bancario = ? WHERE id_pago_mp = ?",
            (id_movimiento_bancario, id_pago_mp),
        )
        if commit and self.connection is not None:
            self.connection.commit()
        return True

    def mark_movement_conciliated(
        self,
        id_movimiento: int,
        conciliado: bool = True,
        *,
        commit: bool = True,
    ) -> bool:
        cursor = self._cursor()
        cursor.execute(
            "SELECT conciliado FROM MOVIMIENTO_BANCARIO WHERE id_movimiento = ?",
            (id_movimiento,),
        )
        current = cursor.fetchone()
        if current is None:
            raise ConnectorError(f"Bank movement {id_movimiento} not found")

        if bool(current[0]) == conciliado:
            return False

        cursor.execute(
            "UPDATE MOVIMIENTO_BANCARIO SET conciliado = ? WHERE id_movimiento = ?",
            (1 if conciliado else 0, id_movimiento),
        )
        if commit and self.connection is not None:
            self.connection.commit()
        return True

    def persist_payment_movement_conciliation(
        self,
        id_pago_mp: int,
        id_movimiento_bancario: int,
    ) -> str:
        cursor = self._cursor()

        cursor.execute(
            "SELECT id_movimiento_bancario FROM PAGO_MERCADO_PAGO WHERE id_pago_mp = ?",
            (id_pago_mp,),
        )
        payment_row = cursor.fetchone()
        if payment_row is None:
            raise ConnectorError(f"Payment {id_pago_mp} not found")

        cursor.execute(
            "SELECT conciliado FROM MOVIMIENTO_BANCARIO WHERE id_movimiento = ?",
            (id_movimiento_bancario,),
        )
        movement_row = cursor.fetchone()
        if movement_row is None:
            raise ConnectorError(f"Bank movement {id_movimiento_bancario} not found")

        cursor.execute(
            "SELECT TOP 1 id_pago_mp FROM PAGO_MERCADO_PAGO "
            "WHERE id_movimiento_bancario = ? AND id_pago_mp <> ?",
            (id_movimiento_bancario, id_pago_mp),
        )
        linked_payment = cursor.fetchone()

        current_payment_movement = payment_row[0]
        current_movement_conciliated = bool(movement_row[0])

        if linked_payment is not None:
            return "conflict"
        if current_payment_movement == id_movimiento_bancario and current_movement_conciliated:
            return "skipped"
        if current_payment_movement is not None and current_payment_movement > 0:
            return "conflict"
        if current_movement_conciliated:
            return "conflict"

        try:
            self.update_payment_conciliation(
                id_pago_mp,
                id_movimiento_bancario,
                commit=False,
            )
            self.mark_movement_conciliated(
                id_movimiento_bancario,
                conciliado=True,
                commit=False,
            )
        except Exception:
            if self.connection is not None:
                self.connection.rollback()
            raise

        if self.connection is not None:
            self.connection.commit()
        return "updated"

    def get_payment_concepts(self) -> list[PaymentConcept]:
        query = """
            SELECT id_concepto_pago, nombre
            FROM PAGO_CONCEPTO
            ORDER BY id_concepto_pago
        """
        cursor = self._cursor()
        rows = cursor.execute(query).fetchall()
        return [
            PaymentConcept.model_validate({"id_concepto_pago": row[0], "nombre": row[1]})
            for row in rows
        ]

    def get_payment_methods(self) -> list[PaymentMethod]:
        query = """
            SELECT id_medio_pago, nombre
            FROM MEDIO_PAGO
            ORDER BY id_medio_pago
        """
        cursor = self._cursor()
        rows = cursor.execute(query).fetchall()
        return [PaymentMethod.model_validate({"id_medio_pago": row[0], "nombre": row[1]}) for row in rows]
