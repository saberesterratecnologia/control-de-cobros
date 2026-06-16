from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sqlserver import SQLServerConnector


TARGET = [
    "INDUSTRIALIZACIÓN-D-JUNIO-2026",
    "LIDERAZGO Y GERENCIA EN LA AGROINDUSTRIA-JUNIO-2026",
    "PERITO-D-FEBRERO-2026",
    "PERITO-D-ABRIL-2026",
    "PRODUCCIÓN EXTENSIVA-D-ABRIL-2026",
    "APICULTURA-D-FEBRERO-2026",
]


def main() -> None:
    config = load_config("config/settings.yaml")
    sql = SQLServerConnector(config["database"])
    sql.connect()
    cur = sql.connection.cursor()

    for name in TARGET:
        cur.execute(
            """
            SELECT id_comision, id_curso, nombre,
                   valor_inscripcion, valor_inscripcion_promocion,
                   valor_cuota, valor_cuota_bonificada,
                   cantidad_cuotas, fecha_inicio
            FROM COMISIONES
            WHERE nombre = ?
            """,
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            print(f"NOT FOUND: {name}")
            continue
        print(f"\n{row[2].strip()}")
        print(f"  curso={row[1]} id={row[0]} inicio={row[8]}")
        print(f"  inscripcion={row[3]} promo_insc={row[4]}")
        print(f"  cuota={row[5]} promo_cuota={row[6]} cuotas={row[7]}")

    sql.connection.close()


if __name__ == "__main__":
    main()
