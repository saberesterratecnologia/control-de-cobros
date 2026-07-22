from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.connectors.sheets import SheetsConnector
from src.connectors.sqlserver import SQLServerConnector


def test_sheets_connector_parses_rows_and_empty_cells() -> None:
    connector = SheetsConnector(
        {
            "credentials_file": "credentials.json",
            "spreadsheet_name": "Test",
            "worksheet_name": "COBROS",
        }
    )

    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [
        [
            "org",
            "curso",
            "comision",
            "fecha",
            "tipo",
            "dni",
            "concepto",
            "monto",
            "medio",
            "estudiante",
            "est_admin",
            "est_deuda",
            "id_mov",
            "id_pago",
        ],
        [
            "Saberes",
            "Curso 1",
            "Com 10",
            "12/05/2026",
            "Cobro",
            "30111222",
            "CUOTA",
            "$54.500",
            "Transferencia",
            "Juan Perez",
            "Activo",
            "Sin deuda",
            "100",
            "900",
        ],
        ["Saberes", "Curso 1", "Com 10", "", "Venta", "", "", "", "", "", "", "", "", ""],
    ]

    connector._worksheet = worksheet
    rows = connector.read_all_rows()

    assert len(rows) == 2
    assert rows[0].comision == "Com 10"
    assert rows[0].fecha_movimiento == date(2026, 5, 12)
    assert rows[0].id_movimiento_bancario == 100
    assert rows[1].fecha_movimiento is None
    assert rows[1].id_movimiento_bancario is None


def test_argentine_money_format_parsing() -> None:
    assert SheetsConnector._parse_money("$54.500") == Decimal("54500")
    assert SheetsConnector._parse_money("$54.500,25") == Decimal("54500.25")


def test_date_parsing_ddmmyyyy() -> None:
    assert SheetsConnector._parse_date("01/02/2026") == date(2026, 2, 1)


@patch("src.connectors.sqlserver.pyodbc.connect")
def test_sqlserver_connector_queries_are_parameterized(mock_connect: MagicMock) -> None:
    fake_cursor = MagicMock()
    fake_connection = MagicMock()
    fake_connection.cursor.return_value = fake_cursor
    mock_connect.return_value = fake_connection

    fake_cursor.execute.return_value.fetchall.return_value = [
        (
            10,                           # id_comision
            60,                           # id_curso
            1,                            # id_organizacion
            "Comisión A",                 # nombre
            Decimal("20000"),             # valor_inscripcion
            Decimal("10000"),             # valor_inscripcion_promocion
            Decimal("24000"),             # valor_cuota
            Decimal("12000"),             # valor_cuota_bonificada
            None,                         # valor_cuota_recargo
            None,                         # valor_pago_unico
            None,                         # valor_certificacion
            5,                            # cantidad_cuotas
            9,                            # duracion_meses
            datetime(2026, 1, 1).date(),  # fecha_inicio
            False,                        # borrado
            True,                         # analisis_pagos
        )
    ]

    connector = SQLServerConnector(
        {
            "driver": "ODBC Driver 17 for SQL Server",
            "server": "localhost",
            "database": "test",
            "trusted_connection": True,
        }
    )
    connector.connect()

    commissions = connector.get_commissions(60)
    assert len(commissions) == 1

    executed_query, executed_params = fake_cursor.execute.call_args[0]
    assert "id_curso = ?" in executed_query
    assert "analisis_pagos = 1" in executed_query
    assert executed_params == (60,)



