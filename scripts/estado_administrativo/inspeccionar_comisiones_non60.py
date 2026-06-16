from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from main import load_config
from src.connectors.sqlserver import SQLServerConnector


def main() -> None:
    config = load_config("config/settings.yaml")
    sql = SQLServerConnector(config["database"])
    sql.connect()
    cursor = sql.connection.cursor()

    print("Date-related columns in COMISIONES:")
    rows = cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'COMISIONES'
          AND COLUMN_NAME LIKE '%fecha%'
        ORDER BY ORDINAL_POSITION
        """
    ).fetchall()
    for row in rows:
        print(f"  - {row[0]}")

    print("\nNon-curso-60 commissions at the tail of the background run:")
    rows = cursor.execute(
        """
        SELECT TOP 5
            c.id_comision,
            c.id_curso,
            c.nombre,
            c.id_estado_comision,
            c.fecha_inicio,
            c.fecha_finalizacion,
            c.duracion_meses,
            c.cantidad_cuotas
        FROM COMISIONES c
        WHERE c.id_organizacion = 2
          AND c.borrado = 0
          AND YEAR(c.fecha_inicio) = 2026
          AND c.id_curso != 60
        ORDER BY c.nombre DESC
        """
    ).fetchall()
    for row in rows:
        print(
            f"  id={row[0]} curso={row[1]} estado={row[3]} "
            f"inicio={row[4]} fin={row[5]} meses={row[6]} cuotas={row[7]} nombre={row[2]}"
        )

    sql.connection.close()


if __name__ == "__main__":
    main()
